"""
Urgent Space Confirmations Slack Bot
-------------------------------------
Polls Neon DB every run (cron: every 5 min) for new suggested_outreaches
with type=ACTIVE_SEARCH_DEMAND and posts to #urgent-space-confirmations.

Required env vars:
  NEON_DATABASE_URL  - postgres connection string
  SLACK_BOT_TOKEN    - xoxb-... from Slack app OAuth page
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv(Path(__file__).parent / ".env", override=True)

# ── Config ────────────────────────────────────────────────────────────────────

SLACK_CHANNEL = "urgent-space-confirmations"
STATE_FILE = Path(__file__).parent / "state.json"
SPACE_BASE_URL = "https://tandem.space/office"

SLACK_IDS = {
    "sophie":  "U08PBSSH9PU",
    "allegra": "U081LPECUQJ",
    "pete":    "U09AKPARDGT",
    "ian":     "U09QZG54TM4",
}

THIRD_PARTY_SOURCES = ("costar", "loopnet")
FRESHNESS_STALE_DAYS = 30

# ── State helpers ─────────────────────────────────────────────────────────────

def load_last_processed() -> str:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return data["last_processed"]
    # First run: start from now so we don't flood with old records
    ts = datetime.now(timezone.utc).isoformat()
    save_last_processed(ts)
    return ts


def save_last_processed(ts: str):
    STATE_FILE.write_text(json.dumps({"last_processed": ts}))


# ── Routing ───────────────────────────────────────────────────────────────────

def route_to(lease_type: str | None, region: str | None) -> str:
    if lease_type == "T2T":
        return SLACK_IDS["sophie"]
    if region == "NEW_YORK_CITY":
        return SLACK_IDS["allegra"]
    if region == "SF":
        return SLACK_IDS["pete"]
    if region == "BOSTON":
        return SLACK_IDS["ian"]
    return SLACK_IDS["sophie"]  # fallback: tag Sophie


def is_stale_third_party(source: str | None, updated_at) -> bool:
    if not source:
        return False
    if not any(s in source.lower() for s in THIRD_PARTY_SOURCES):
        return False
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated_at > timedelta(days=FRESHNESS_STALE_DAYS)


# ── DB query ──────────────────────────────────────────────────────────────────

QUERY = """
SELECT DISTINCT ON (so.id)
    so.id,
    so.created_at,
    so.note,
    u.name          AS requester_name,
    s.id            AS space_id,
    s.title         AS space_title,
    s."leaseType"   AS lease_type,
    s.source        AS space_source,
    s."updatedAt"   AS space_updated_at,
    s."freshness_check_in_at",
    sl."addressLine1",
    sl.city,
    sl.state,
    sl.region,
    c.name          AS contact_name,
    c.phone_number  AS contact_phone,
    c.email         AS contact_email
FROM suggested_outreaches so
JOIN spaces s            ON s.id = so.space_id
LEFT JOIN space_location sl   ON sl.id = s."locationId"
LEFT JOIN space_contact sc ON sc."spaceId" = s.id
LEFT JOIN contacts c       ON c.id = sc."contactId"
LEFT JOIN users u          ON u.id = so.assigned_by
WHERE so.type        = 'ACTIVE_SEARCH_DEMAND'
  AND so.status      = 'REQUESTED'
  AND so.deleted_at  IS NULL
  AND so.created_at  > %(last_processed)s::timestamptz
ORDER BY so.id, so.created_at ASC
"""


def fetch_new_outreaches(conn, last_processed: str) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(QUERY, {"last_processed": last_processed})
        rows = cur.fetchall()
        return [dict(row) for row in rows]


# ── Message builder ───────────────────────────────────────────────────────────

def build_message(row: dict) -> str:
    space_id    = row.get("space_id") or ""
    title       = row.get("space_title") or "Unknown Space"
    address     = row.get("addressLine1") or ""
    city        = row.get("city") or ""
    state       = row.get("state") or ""
    region      = row.get("region") or ""
    lease_type  = row.get("lease_type") or "—"
    note        = row.get("note") or "—"
    requester   = row.get("requester_name") or "Demand Team"

    contact_parts = [
        row.get("contact_name"),
        row.get("contact_phone"),
        row.get("contact_email"),
    ]
    contact_str = " | ".join(p for p in contact_parts if p) or "No contact on file"

    space_url    = f"{SPACE_BASE_URL}/{space_id}" if space_id else "No URL"
    supply_id    = route_to(row.get("lease_type"), region)
    location_str = ", ".join(p for p in [address, city, state] if p) or "Unknown location"

    lines = [
        ":rotating_light: *Urgent Space Confirmation Needed*\n",
        f"*Searching for:* {note}",
        f"*Space:* <{space_url}|{title}>",
        f"*Location:* {location_str}",
        f"*Lease Type:* {lease_type}",
        f"*Broker / Contact:* {contact_str}",
        f"*Requested by:* {requester}",
        "",
        f"<@{supply_id}> — please confirm availability ASAP",
    ]

    # Freshness flag
    if (row.get("freshness_check_in_at") is not None
            and is_stale_third_party(row.get("space_source"), row.get("space_updated_at"))):
        lines.append(
            f"\n<@{SLACK_IDS['sophie']}> — this space is third-party sourced "
            f"(CoStar/LoopNet) and hasn't been updated in {FRESHNESS_STALE_DAYS}+ days. "
            "Please check CoStar status before outreach."
        )

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_url = os.getenv("NEON_DATABASE_URL")
    bot_token = os.getenv("SLACK_BOT_TOKEN")

    if not db_url:
        print("ERROR: NEON_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    if not bot_token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    last_processed = load_last_processed()
    print(f"[{datetime.now().isoformat()}] Checking for outreaches since {last_processed}")

    conn = psycopg2.connect(db_url)
    try:
        rows = fetch_new_outreaches(conn, last_processed)
    finally:
        conn.close()

    if not rows:
        print("No new ACTIVE_SEARCH_DEMAND outreaches found.")
        return

    print(f"Found {len(rows)} new outreach(es). Posting to Slack...")

    client = WebClient(token=bot_token)
    latest_ts = last_processed

    for row in rows:
        message = build_message(row)
        try:
            client.chat_postMessage(channel=SLACK_CHANNEL, text=message, mrkdwn=True)
            print(f"  OK Posted for space: {row.get('space_title')} (outreach {row['id']})")
        except SlackApiError as e:
            print(f"  ERR Slack error for outreach {row['id']}: {e.response['error']}", file=sys.stderr)

        # Advance cursor even if Slack post failed, to avoid re-processing
        row_ts = row["created_at"]
        if hasattr(row_ts, "isoformat"):
            row_ts = row_ts.isoformat()
        if row_ts > latest_ts:
            latest_ts = row_ts

    save_last_processed(latest_ts)
    print(f"State updated to {latest_ts}")


if __name__ == "__main__":
    main()
