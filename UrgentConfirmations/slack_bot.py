"""
Urgent Space Confirmations Slack Bot
-------------------------------------
Polls Neon DB every run (cron: every 30 min) for new suggested_outreaches
with type=ACTIVE_SEARCH_DEMAND and posts to #urgent-space-confirmations.

Posts a Block Kit message tagging Clarisse with pre-research links and
existing CRM context. Clarisse uses the action buttons to either notify
the market rep (if available) or resolve the item (if not available).

Required env vars:
  NEON_DATABASE_URL          - postgres connection string
  SLACK_BOT_TOKEN            - xoxb-... from Slack app OAuth page
  SLACK_CLARISSE_USER_ID     - Clarisse's Slack user ID
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

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
ADMIN_BASE_URL = "https://tandem.space/admin/spaces"

SLACK_IDS = {
    "clarisse": "U09GDTBSKEV",
    "sophie":   "U08PBSSH9PU",
    "allegra":  "U081LPECUQJ",
    "pete":     "U09AKPARDGT",
    "ian":      "U09QZG54TM4",
}

SLACK_NAMES = {
    "clarisse": "Clarisse",
    "sophie":   "Sophie",
    "allegra":  "Allegra",
    "pete":     "Pete",
    "ian":      "Ian",
}

# ── State helpers ─────────────────────────────────────────────────────────────

def load_last_processed() -> str:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return data["last_processed"]
    ts = datetime.now(timezone.utc).isoformat()
    save_last_processed(ts)
    return ts


def save_last_processed(ts: str):
    STATE_FILE.write_text(json.dumps({"last_processed": ts}))


# ── Routing ───────────────────────────────────────────────────────────────────

def route_to_key(lease_type: str | None, region: str | None) -> str:
    """Returns the name key for the supply rep who should own this outreach."""
    if lease_type == "T2T":
        return "sophie"
    if region == "NEW_YORK_CITY":
        return "allegra"
    if region == "SF":
        return "pete"
    if region == "BOSTON":
        return "ian"
    return "sophie"


# ── DB queries ────────────────────────────────────────────────────────────────

OUTREACH_QUERY = """
SELECT DISTINCT ON (so.id)
    so.id,
    so.created_at,
    so.note,
    u.name          AS requester_name,
    s.id            AS space_id,
    s.title         AS space_title,
    s."leaseType"   AS lease_type,
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

# Pulls recent opportunities at the same building address.
# If opportunities.space_id doesn't exist in the schema, this query will
# fail gracefully — fetch_building_context catches and returns [].
CONTEXT_QUERY = """
SELECT DISTINCT ON (o.id)
    o.id, o.stage, o.updated_at, c.name AS contact_name
FROM opportunities o
JOIN opportunity_contact oc ON oc."opportunityId" = o.id
JOIN contacts c             ON c.id = oc."contactId"
JOIN space_contact sc       ON sc."contactId" = c.id
JOIN spaces s               ON s.id = sc."spaceId"
JOIN space_location sl      ON sl.id = s."locationId"
WHERE sl."addressLine1" ILIKE %(address)s
ORDER BY o.id, o.updated_at DESC NULLS LAST
LIMIT 3
"""

FRONT_QUERY = """
SELECT sender_name, sender_email, subject,
       LEFT(body, 300) AS body_preview,
       message_created_at, assignee_email
FROM front_incoming_emails
WHERE (
    sender_email ILIKE %(contact_email)s
    OR %(contact_email)s = ANY(recipient_emails)
    OR subject ILIKE %(address_pattern)s
)
AND assignee_email ILIKE '%%@tandem.space%%'
ORDER BY message_created_at DESC
LIMIT 4
"""


def fetch_new_outreaches(conn, last_processed: str) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(OUTREACH_QUERY, {"last_processed": last_processed})
        return [dict(row) for row in cur.fetchall()]


def fetch_front_emails(conn, address: str, contact_email: str | None) -> list[dict]:
    if not address:
        return []
    try:
        email_param = contact_email.strip() if contact_email and contact_email.strip() else None
        address_pattern = f"%{address.split(',')[0].strip()}%"
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(FRONT_QUERY, {
                "contact_email": email_param,
                "address_pattern": address_pattern,
            })
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"  [front] Could not fetch Front emails: {e}", file=sys.stderr)
        conn.rollback()
        return []


def fetch_building_context(conn, address: str) -> list[dict]:
    if not address:
        return []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(CONTEXT_QUERY, {"address": f"%{address}%"})
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"  [context] Could not fetch building context: {e}", file=sys.stderr)
        conn.rollback()
        return []


# ── Message builder ───────────────────────────────────────────────────────────

def _fmt_body(raw: str) -> str:
    """Strip newlines and Slack-special chars from an email body preview."""
    text = " ".join(raw.split())  # collapse all whitespace
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text[:200] + ("…" if len(text) > 200 else "")


def build_message_blocks(row: dict, context_rows: list[dict], front_emails: list[dict]) -> tuple[list, str]:
    """Returns (blocks, fallback_text) for chat_postMessage."""
    space_id   = row.get("space_id") or ""
    title      = row.get("space_title") or "Unknown Space"
    address    = row.get("addressLine1") or ""
    city       = row.get("city") or ""
    state      = row.get("state") or ""
    region     = row.get("region") or ""
    lease_type = row.get("lease_type") or "—"
    note       = row.get("note") or "—"
    requester  = row.get("requester_name") or "Demand Team"

    contact_parts = [row.get("contact_name"), row.get("contact_phone"), row.get("contact_email")]
    contact_str = " | ".join(p for p in contact_parts if p) or "No contact on file"
    location_str = ", ".join(p for p in [address, city, state] if p) or "Unknown location"

    space_url  = f"{SPACE_BASE_URL}/{space_id}" if space_id else None
    admin_url  = f"{ADMIN_BASE_URL}/{space_id}" if space_id else None
    space_link = f"<{space_url}|{title}>" if space_url else title

    rep_key      = route_to_key(row.get("lease_type"), region)
    rep_slack_id = SLACK_IDS[rep_key]
    rep_name     = SLACK_NAMES[rep_key]
    outreach_id  = str(row.get("id") or "")

    # Pre-research links (Google, CoStar, admin — Front replaced by live email pull)
    google_query = quote_plus(f"{address} {city} office space availability".strip())
    google_url   = f"https://www.google.com/search?q={google_query}"

    research_lines = [
        f"• <{google_url}|Search Google for public availability →>",
        f"• <https://www.costar.com|Check CoStar pricing →>  _(search: {address})_",
    ]
    if admin_url:
        research_lines.append(f"• <{admin_url}|View space in admin →>")

    # Front email snippets
    if front_emails:
        front_lines = []
        for e in front_emails:
            ts = e.get("message_created_at")
            if ts:
                if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                days_ago = (datetime.now(timezone.utc) - ts).days
                age = f"{days_ago}d ago"
            else:
                age = "?"
            sender  = e.get("sender_name") or e.get("sender_email") or "Unknown"
            subject = e.get("subject") or "(no subject)"
            assignee = (e.get("assignee_email") or "").replace("@tandem.space", "")
            body    = _fmt_body(e.get("body_preview") or "")
            front_lines.append(f"• *{age}* | {subject}\n  _{sender} → {assignee}_ — \"{body}\"")
        front_text = "\n".join(front_lines)
    else:
        front_text = "_No recent Front emails found for this address or contact_"

    # CRM context
    if context_rows:
        ctx_lines = []
        for r in context_rows:
            when = ""
            if r.get("updated_at"):
                updated = r["updated_at"]
                if hasattr(updated, "tzinfo") and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                days_ago = (datetime.now(timezone.utc) - updated).days
                when = f" — {days_ago}d ago"
            contact = r.get("contact_name") or ""
            stage   = r.get("stage") or "unknown stage"
            ctx_lines.append(f"• {stage}{when}{': ' + contact if contact else ''}")
        crm_text = "\n".join(ctx_lines)
    else:
        crm_text = "_No prior outreach found at this address_"

    clarisse_id = SLACK_IDS["clarisse"]
    clarisse_mention = f"<@{clarisse_id}>" if clarisse_id else "Clarisse"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":rotating_light: Urgent Space Confirmation Needed"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Space:*\n{space_link}"},
                {"type": "mrkdwn", "text": f"*Location:*\n{location_str}"},
                {"type": "mrkdwn", "text": f"*Lease Type:*\n{lease_type}"},
                {"type": "mrkdwn", "text": f"*Requested by:*\n{requester}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Searching for:* {note}\n*Broker / Contact:* {contact_str}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Recent Front emails*\n" + front_text},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Existing CRM at this address*\n{crm_text}".format(crm_text=crm_text)},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Pre-Research*\n" + "\n".join(research_lines)},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{clarisse_mention} — please run the checks above, confirm CoStar "
                    f"price/availability, then use the buttons below."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"✅ Available — Notify {rep_name}"},
                    "style": "primary",
                    "action_id": "outreach_available",
                    "value": f"{outreach_id}|{rep_slack_id}|{rep_name}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Not Available — Resolve"},
                    "action_id": "outreach_not_available",
                    "value": outreach_id,
                },
            ],
        },
    ]

    fallback_text = f"Urgent confirmation needed: {title} — {location_str}"
    return blocks, fallback_text


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run   = "--dry-run" in sys.argv
    db_url    = os.getenv("NEON_DATABASE_URL")
    bot_token = os.getenv("SLACK_BOT_TOKEN")

    if not db_url:
        print("ERROR: NEON_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    if not bot_token and not dry_run:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    last_processed = load_last_processed()
    print(f"[{datetime.now().isoformat()}] Checking for outreaches since {last_processed}")

    conn = psycopg2.connect(db_url)
    try:
        rows = fetch_new_outreaches(conn, last_processed)
        for row in rows:
            address = row.get("addressLine1") or ""
            row["_context"] = fetch_building_context(conn, address)
            row["_front"]   = fetch_front_emails(conn, address, row.get("contact_email"))
    finally:
        conn.close()

    if not rows:
        print("No new ACTIVE_SEARCH_DEMAND outreaches found.")
        return

    print(f"Found {len(rows)} new outreach(es).")

    client = None if dry_run else WebClient(token=bot_token)
    latest_ts = last_processed

    for row in rows:
        context       = row.pop("_context", [])
        front_emails  = row.pop("_front", [])
        blocks, fallback_text = build_message_blocks(row, context, front_emails)

        if dry_run:
            print(f"\n--- DRY RUN: {row.get('space_title')} ---")
            print(json.dumps(blocks, indent=2, default=str))
        else:
            try:
                client.chat_postMessage(
                    channel=SLACK_CHANNEL,
                    blocks=blocks,
                    text=fallback_text,
                )
                print(f"  OK Posted for space: {row.get('space_title')} (outreach {row['id']})")
            except SlackApiError as e:
                print(f"  ERR Slack error for outreach {row['id']}: {e.response['error']}", file=sys.stderr)

        row_ts = row["created_at"]
        if hasattr(row_ts, "isoformat"):
            row_ts = row_ts.isoformat()
        if row_ts > latest_ts:
            latest_ts = row_ts

    if not dry_run:
        save_last_processed(latest_ts)
        print(f"State updated to {latest_ts}")


if __name__ == "__main__":
    main()
