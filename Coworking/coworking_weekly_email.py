"""
Weekly coworking requirements email to operator contacts.

Queries Neon for teams that entered the active pipeline in the past 7 days
with targetDesks <= 10, groups by market, and emails each operator contact
a summary of that week's requirements (anonymous -- no company names).

Usage:
    python coworking_weekly_email.py
    python coworking_weekly_email.py --dry-run   # print emails, don't send

Requires env vars:
    NEON_DATABASE_URL (or DATABASE_URL)
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

Scheduled via .github/workflows/coworking_weekly_email.yml every Tuesday 9am PT.
"""

import argparse
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Operator contacts (market -> list of (name, email, markets_covered))
# Markets covered: list of region locationNames to include
# ---------------------------------------------------------------------------

OPERATORS = [
    {
        "name": "Jack",
        "email": "jack.ortlieb@wework.com",
        "operator": "WeWork",
        "markets": ["San Francisco", "San Francisco Bay Area"],
        "market_label": "San Francisco",
    },
    {
        "name": "Conor",
        "email": "conor.golden@wework.com",
        "operator": "WeWork",
        "markets": ["New York", "New York Metro Area"],
        "market_label": "New York",
    },
    {
        "name": "Garrett",
        "email": "garrett.mccready@wework.com",
        "operator": "WeWork",
        "markets": ["Boston", "Boston Metro Area"],
        "market_label": "Boston",
    },
    {
        "name": "Julissa",
        "email": "jcajigas@industriousoffice.com",
        "operator": "Industrious",
        "markets": ["San Francisco", "San Francisco Bay Area",
                    "New York", "New York Metro Area",
                    "Boston", "Boston Metro Area"],
        "market_label": "San Francisco, New York, and Boston",
    },
    {
        "name": "Eric",
        "email": "Portals.US@iwgplc.com",
        "operator": "IWG / Regus",
        "markets": ["San Francisco", "San Francisco Bay Area",
                    "New York", "New York Metro Area",
                    "Boston", "Boston Metro Area"],
        "market_label": "San Francisco, New York, and Boston",
    },
    {
        "name": "Sara",
        "email": "mia.king.offices@gmail.com",
        "operator": "IWG / Spaces",
        "markets": ["San Francisco", "San Francisco Bay Area",
                    "New York", "New York Metro Area",
                    "Boston", "Boston Metro Area"],
        "market_label": "San Francisco, New York, and Boston",
    },
    {
        "name": "Chris",
        "email": "chris.c@mindspace.me",
        "operator": "Mindspace",
        "markets": ["San Francisco", "San Francisco Bay Area",
                    "New York", "New York Metro Area",
                    "Boston", "Boston Metro Area"],
        "market_label": "San Francisco, New York, and Boston",
    },
    {
        "name": "Josh",
        "email": "jbarton@tishmanspeyer.com",
        "operator": "Tishman Speyer Studio",
        "markets": ["San Francisco", "San Francisco Bay Area"],
        "market_label": "San Francisco",
    },
    {
        "name": "Carly",
        "email": "carly@expansive.com",
        "operator": "Expansive",
        "markets": ["San Francisco", "San Francisco Bay Area"],
        "market_label": "San Francisco",
    },
]

QUERY = """
SELECT
    r."locationName"            AS market,
    sr."targetDesks"            AS desks,
    sr."budgetCap"              AS budget_cap,
    sr."termLengthInMonths"     AS term_months,
    sr."targetMoveInDate"       AS move_in_date,
    sr."leaseTerm"              AS lease_term
FROM space_requirements sr
JOIN space_requirements_region srr ON srr."spaceRequirementId" = sr.id
JOIN region r ON r.id = srr."regionId"
JOIN companies c ON c.id = sr."companyId"
WHERE sr.stage IN ('ENGAGED', 'QUALIFIED', 'TOURING', 'NEGOTIATING')
  AND sr."targetDesks" <= 10
  AND sr."targetDesks" > 0
  AND sr."createdAt" >= NOW() - INTERVAL '7 days'
  AND sr."deletedAt" IS NULL
  AND c.domain NOT ILIKE '%%@tandem.space%%'
  AND c."duplicateOf" IS NULL
  AND r."locationName" IN %(markets)s
ORDER BY r."locationName", sr."targetDesks"
"""

MARKET_LABELS = {
    "San Francisco": "San Francisco",
    "San Francisco Bay Area": "San Francisco",
    "New York": "New York",
    "New York Metro Area": "New York",
    "Boston": "Boston",
    "Boston Metro Area": "Boston",
}


def format_budget(row) -> str:
    cap = row.get("budget_cap")
    if cap:
        return f"up to ${int(cap):,}/mo"
    return "budget not specified"


def format_term(row) -> str:
    months = row.get("term_months")
    lease  = row.get("lease_term") or ""
    if months:
        if months <= 3:
            return "1-3 months"
        if months <= 6:
            return "3-6 months"
        if months <= 12:
            return "6-12 months"
        return "12+ months"
    return lease or "flexible"


def format_move_in(row) -> str:
    d = row.get("move_in_date")
    if d:
        if hasattr(d, "strftime"):
            return d.strftime("%b %Y")
        return str(d)[:7]
    return "ASAP"


def build_market_rows(requirements: list) -> dict[str, list]:
    """Group requirements by canonical market label."""
    groups: dict[str, list] = {}
    for row in requirements:
        label = MARKET_LABELS.get(row["market"], row["market"])
        groups.setdefault(label, []).append(row)
    return groups


def build_email_body(operator: dict, market_rows: dict[str, list]) -> str:
    today = date.today().strftime("%B %d, %Y")
    markets_str = operator["market_label"]
    name = operator["name"]
    op_name = operator["operator"]

    intro = (
        f"Hi {name},\n\n"
        f"Here are the new teams that entered our pipeline this week looking for "
        f"flexible private office space for 10 people or fewer in {markets_str}:\n"
    )

    sections = []
    for market, rows in sorted(market_rows.items()):
        if not rows:
            continue
        lines = [f"\n{market} — {len(rows)} new team{'s' if len(rows) != 1 else ''} this week:\n"]
        lines.append(f"{'Desks':<8} {'Budget':<22} {'Move-in':<12} {'Term'}")
        lines.append("-" * 58)
        for row in rows:
            desks   = str(row["desks"])
            budget  = format_budget(row)
            move_in = format_move_in(row)
            term    = format_term(row)
            lines.append(f"{desks:<8} {budget:<22} {move_in:<12} {term}")
        sections.append("\n".join(lines))

    if not sections:
        return ""   # nothing to send

    outro = (
        "\n\nIf you have availability matching any of these profiles, "
        "reply to this email and we'll coordinate a tour.\n\n"
        "Best,\nSophie Frank\nTandem | sophie@tandem.space"
    )

    return intro + "\n".join(sections) + outro


def send_email(to_addr: str, subject: str, body: str):
    cfg = {
        "host":     os.environ["SMTP_HOST"],
        "port":     int(os.environ.get("SMTP_PORT", 587)),
        "user":     os.environ["SMTP_USER"],
        "password": os.environ["SMTP_PASSWORD"],
    }
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["user"]
    msg["To"]      = to_addr
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
        s.starttls()
        s.login(cfg["user"], cfg["password"])
        s.sendmail(cfg["user"], [to_addr], msg.as_string())


def run(dry_run=False):
    db_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("Missing NEON_DATABASE_URL env var")

    conn   = psycopg2.connect(db_url)
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Fetch all markets at once
    all_markets = list({m for op in OPERATORS for m in op["markets"]})
    cursor.execute(QUERY, {"markets": tuple(all_markets)})
    all_rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    today = date.today().strftime("%B %d, %Y")
    sent = skipped = 0

    for operator in OPERATORS:
        # Filter rows relevant to this operator's markets
        relevant = [r for r in all_rows if r["market"] in operator["markets"]]
        if not relevant:
            print(f"  SKIP {operator['operator']} ({operator['name']}): no pipeline activity this week")
            skipped += 1
            continue

        market_rows = build_market_rows(relevant)
        body = build_email_body(operator, market_rows)
        if not body:
            skipped += 1
            continue

        total_teams = sum(len(v) for v in market_rows.values())
        subject = f"Tandem — New Coworking Requirements This Week | {operator['market_label']} | {today}"

        if dry_run:
            print(f"\n{'='*70}")
            print(f"TO: {operator['name']} <{operator['email']}> ({operator['operator']})")
            print(f"SUBJECT: {subject}")
            print(f"TEAMS: {total_teams}")
            print(body)
        else:
            send_email(operator["email"], subject, body)
            print(f"  SENT to {operator['name']} <{operator['email']}> -- {total_teams} teams")
            sent += 1

    print(f"\nDone. {sent} emails sent, {skipped} skipped (no activity).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
