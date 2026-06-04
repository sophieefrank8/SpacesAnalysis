"""
Coworking referral agent -- three modes:

  Ingest (supply backlog, run when an operator responds with availability):
    cat operator_email.txt | python coworking_agent.py --ingest --operator "Werqwise" --market SF
    cat operator_email.txt | python coworking_agent.py --ingest --operator "Werqwise" --market SF --dry-run

  Refresh (supply backlog, ad hoc -- draft a fresh availability request to one operator):
    python coworking_agent.py --refresh
    python coworking_agent.py --refresh --operator "Mindspace" --market SF
    python coworking_agent.py --refresh --dry-run

  Mate intake (demand matchmaking, on-demand -- draft broker inquiry emails for a client):
    python coworking_agent.py --mate "Acme Co" --headcount 3 --market SF --budget 4000
    python coworking_agent.py --mate "Acme Co" --headcount 3 --market SF --budget 4000 --send

Inventory source: operator correspondence only. No web crawling.
The agreed cadence per operator is recorded in coworking_strategy.md.

Env vars:
    ANTHROPIC_API_KEY
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
    DEMAND_OPS_EMAIL    Inbox where drafts land for review before sending
    DEMAND_OPS_NAME     Signer name on all outbound emails (default: Sophie)
    COWORKING_CSV       Path to CSV (default: coworking_locations.csv next to this script)
"""

import argparse
import csv
import json
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CSV_PATH    = os.environ.get("COWORKING_CSV", os.path.join(os.path.dirname(__file__), "coworking_locations.csv"))
SENDER_NAME = os.environ.get("DEMAND_OPS_NAME", "Sophie")
MARKETS     = {"NYC": "New York City", "SF": "San Francisco", "BOS": "Boston"}

STALE_DAYS = 14  # flag needs_refresh if last_updated is older than this

CSV_COLUMNS = [
    "operator", "market", "location_name", "address", "neighborhood",
    "monthly_price_from", "price_note",
    "broker_contact_name", "broker_contact_email", "touring_link",
    "last_updated", "notes",
]


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [_normalize_row(r) for r in rows]


def _normalize_row(row):
    """Ensure all CSV columns are present; migrate legacy column names."""
    # Migrate last_crawled -> last_updated for any rows written by the old version
    if "last_crawled" in row and not row.get("last_updated"):
        row["last_updated"] = row.get("last_crawled", "")
    return {col: row.get(col, "") for col in CSV_COLUMNS}


def save_csv(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def location_key(row):
    """Stable identity: operator + normalized address (lowercased, stripped)."""
    return (row.get("operator", "").lower(), row.get("address", "").lower().strip())


def is_stale(last_updated_str):
    if not last_updated_str:
        return True
    try:
        updated = datetime.strptime(last_updated_str, "%Y-%m-%d").date()
        return (date.today() - updated).days > STALE_DAYS
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def send_email(to_addr, subject, body):
    cfg = {
        "host":     os.environ["SMTP_HOST"],
        "port":     int(os.environ["SMTP_PORT"]),
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


# ---------------------------------------------------------------------------
# Ingest mode
# ---------------------------------------------------------------------------

def run_ingest(args, dry_run=False):
    """Parse an operator response email (via stdin) and update the CSV."""
    email_text = sys.stdin.read()
    if not email_text.strip():
        sys.exit("No email text on stdin. Pipe the operator response: cat email.txt | python coworking_agent.py --ingest ...")

    client      = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    market      = args.market.upper()
    market_name = MARKETS.get(market, market)

    prompt = f"""Parse this availability update email from {args.operator} ({market_name}).

Extract all location updates as a JSON array. Each element must have:
  "location_match"     -- address or location name from the email that identifies this entry
  "price_mo"           -- lowest available monthly price as an integer (e.g. 2500), or null if not given
  "price_note"         -- brief pricing note (e.g. "2-person from $2,500/mo, 4-person from $4,200/mo"), or null
  "touring_link"       -- direct touring or scheduling URL if provided, or null
  "availability_note"  -- one short phrase about current availability (e.g. "2- and 4-person available", "waitlist for 3-person"), or null

Email content:
{email_text[:8000]}

Return valid JSON array only. No markdown fences, no explanation."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    updates = json.loads(raw)

    rows  = load_csv()
    today = date.today().isoformat()
    updated_count = 0

    for update in updates:
        match_key = update.get("location_match", "").lower().strip()
        for row in rows:
            if (row["operator"].lower() == args.operator.lower()
                    and row["market"].upper() == market):
                addr = row.get("address", "").lower()
                name = row.get("location_name", "").lower()
                if match_key and (match_key in addr or match_key in name
                                  or addr in match_key or name in match_key):
                    if update.get("price_mo"):
                        row["monthly_price_from"] = str(update["price_mo"])
                    if update.get("price_note"):
                        row["price_note"] = update["price_note"]
                    if update.get("touring_link"):
                        row["touring_link"] = update["touring_link"]
                    if update.get("availability_note"):
                        row["notes"] = update["availability_note"]
                    row["last_updated"] = today
                    updated_count += 1
                    break

    print(f"Parsed {len(updates)} location update(s) from {args.operator} ({market_name}).")
    print(f"Matched and updated {updated_count} row(s).")

    if dry_run:
        print("Dry run -- nothing written. Parsed updates:")
        for u in updates:
            price = f"${u['price_mo']}/mo" if u.get("price_mo") else u.get("price_note", "no price")
            link  = u.get("touring_link") or "no link"
            print(f"  {u.get('location_match', '?')}: {price}, touring: {link}")
    else:
        save_csv(rows)
        print(f"Saved -> {CSV_PATH}")


# ---------------------------------------------------------------------------
# Refresh mode
# ---------------------------------------------------------------------------

def draft_refresh_email(client, operator, market, locations):
    market_name = MARKETS.get(market, market)

    loc_lines = []
    for r in locations[:10]:
        line = f"  - {r['location_name']} ({r['address']})"
        if r.get("monthly_price_from"):
            line += f" -- on file: ${r['monthly_price_from']}/mo"
        elif r.get("price_note"):
            line += f" -- {r['price_note']}"
        loc_lines.append(line)
    location_list = "\n".join(loc_lines)
    if len(locations) > 10:
        location_list += f"\n  ... and {len(locations) - 10} more locations"

    prompt = f"""Draft a concise, professional availability request email.

From: {SENDER_NAME} at Tandem (tandem.space), a registered broker referral partner
To: {operator} broker partnerships team
Re: {market_name} private offices -- availability update request

We need to ask for:
1. Current private office availability for 2-5 person teams at these {market_name} locations:
{location_list}
2. Current monthly pricing (or confirmation that our on-file rates above are still accurate)
3. A touring link or direct booking link for each location -- we share these directly with
   prospects so they can self-schedule

Tone: collegial, brief, easy to action. We are a reliable referral source.
Sign as {SENDER_NAME}, Tandem | tandem.space
Write the full email body only. No subject line. No placeholders."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def run_refresh(args, dry_run=False):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    rows   = load_csv()

    if not rows:
        sys.exit(f"No locations in {CSV_PATH}. Populate the CSV from the CoStar export first.")

    # Filter to a specific operator/market if requested
    operator_filter = args.operator.lower() if args.operator else None
    market_filter   = args.market.upper() if args.market else None

    groups = {}
    for r in rows:
        if operator_filter and r["operator"].lower() != operator_filter:
            continue
        if market_filter and r["market"].upper() != market_filter:
            continue
        groups.setdefault((r["operator"], r["market"]), []).append(r)

    if not groups:
        sys.exit("No matching locations found. Check --operator and --market values.")

    demand_ops_email = os.environ.get("DEMAND_OPS_EMAIL")
    today            = date.today().strftime("%B %Y")

    print(f"Drafting refresh emails for {len(groups)} operator/market pair(s).\n")

    for (operator, market), locs in sorted(groups.items()):
        market_name   = MARKETS.get(market, market)
        subject       = f"{operator} {market_name} -- Availability + Touring Links | Tandem {today}"
        body          = draft_refresh_email(client, operator, market, locs)
        contact_email = next((r["broker_contact_email"] for r in locs if r.get("broker_contact_email")), None)

        print(f"=== {operator} / {market_name} ({len(locs)} locations) ===")
        print(f"Operator contact: {contact_email or '[not set -- add broker_contact_email to CSV]'}")
        print(f"Subject: {subject}")
        print()
        print(body)
        print()

        if not dry_run and demand_ops_email:
            preamble = (
                f"Review and forward to the {operator} broker contact"
                + (f" ({contact_email})" if contact_email else " (contact TBD -- add to CSV after broker registration)")
                + ".\n\n---\n\n"
            )
            send_email(
                demand_ops_email,
                f"[REVIEW BEFORE SENDING] {subject}",
                preamble + body,
            )
            print(f"  Draft sent to {demand_ops_email}\n")


# ---------------------------------------------------------------------------
# Mate intake mode
# ---------------------------------------------------------------------------

def draft_intake_email(client, mate_name, headcount, market, budget, loc):
    market_name = MARKETS.get(market, market)
    price_info  = (
        f"${loc['monthly_price_from']}/mo (on file)"
        if loc.get("monthly_price_from")
        else loc.get("price_note") or "not on file"
    )
    stale_note = " (may be stale -- confirm current pricing)" if is_stale(loc.get("last_updated")) else ""

    prompt = f"""Draft a short broker inquiry email from {SENDER_NAME} at Tandem (tandem.space)
to the {loc['operator']} broker contact for {loc['location_name']} ({loc['address']}, {market_name}).

Client profile:
  Company: {mate_name}
  Team size: {headcount} people
  Market: {market_name}
  Budget: ~${budget:,}/month
  Need: private office, flexible or short term preferred
  On-file rate for this location: {price_info}{stale_note}

Ask them to:
  1. Confirm current availability and pricing for a {headcount}-person private office
  2. Share a touring link or self-schedule link we can pass to the client

Tone: warm, professional, concise. 3-4 short paragraphs.
Sign as {SENDER_NAME}, Tandem | tandem.space
Write the full email body only. No subject line."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def run_intake(args):
    rows   = load_csv()
    market = args.market.upper()

    matches = [
        r for r in rows
        if r["market"].upper() == market
        and (
            not r.get("monthly_price_from")
            or int(r["monthly_price_from"]) <= args.budget * 1.3
        )
    ]

    if not matches:
        print(f"No locations found for market={market}. Check that coworking_locations.csv is populated.")
        return

    client           = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    demand_ops_email = os.environ.get("DEMAND_OPS_EMAIL")
    market_name      = MARKETS.get(market, market)

    stale = [r for r in matches if is_stale(r.get("last_updated"))]
    if stale:
        print(f"Note: {len(stale)} of {len(matches)} location(s) have stale inventory (last updated over {STALE_DAYS} days ago). "
              f"Consider running --refresh first.\n")

    print(f"{len(matches)} location(s) in {market_name} match {args.mate} "
          f"(headcount={args.headcount}, budget=${args.budget:,}/mo).\n")

    for i, loc in enumerate(matches[:5], 1):
        contact_email = loc.get("broker_contact_email", "")
        subject = f"Broker Inquiry -- {args.headcount}-Person Private Office in {market_name} | {args.mate}"
        body    = draft_intake_email(client, args.mate, args.headcount, market, args.budget, loc)

        stale_flag = " [STALE]" if is_stale(loc.get("last_updated")) else ""
        print(f"--- {i}. {loc['operator']} -- {loc['location_name']}{stale_flag} ---")
        print(f"To: {contact_email or '[broker contact not yet set in CSV]'}")
        print(f"Subject: {subject}")
        print()
        print(body)
        print()

        if args.send and demand_ops_email:
            send_email(
                demand_ops_email,
                f"[DRAFT FOR REVIEW] {subject} -- {loc['operator']} {loc['location_name']}",
                f"Review and forward to {contact_email or 'the operator contact (TBD)'}.\n\n---\n\n{body}",
            )
            print(f"  Draft sent to {demand_ops_email} for review.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Coworking referral agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cat email.txt | python coworking_agent.py --ingest --operator "Werqwise" --market SF
  cat email.txt | python coworking_agent.py --ingest --operator "Werqwise" --market SF --dry-run
  python coworking_agent.py --refresh --dry-run
  python coworking_agent.py --refresh --operator "Mindspace" --market SF
  python coworking_agent.py --mate "Seed Round Co" --headcount 3 --market SF --budget 4000
  python coworking_agent.py --mate "Seed Round Co" --headcount 3 --market SF --budget 4000 --send
        """,
    )
    parser.add_argument("--ingest",    action="store_true",           help="Parse operator response from stdin; update CSV")
    parser.add_argument("--refresh",   action="store_true",           help="Draft availability request email to operator(s)")
    parser.add_argument("--mate",      metavar="COMPANY",             help="Mate intake: company name")
    parser.add_argument("--operator",  metavar="NAME",                help="Operator name (required with --ingest; optional filter with --refresh)")
    parser.add_argument("--market",    choices=["NYC","SF","BOS"],    help="Target market (required with --ingest and --mate; optional filter with --refresh)")
    parser.add_argument("--headcount", type=int,                      help="Team size (required with --mate)")
    parser.add_argument("--budget",    type=int,                      help="Monthly budget USD (required with --mate)")
    parser.add_argument("--send",      action="store_true",           help="Email drafts to DEMAND_OPS_EMAIL for review (--mate only)")
    parser.add_argument("--dry-run",   action="store_true",           help="Print output; do not write files or send email")
    args = parser.parse_args()

    needs_smtp = not args.dry_run and (args.refresh or (args.mate and args.send))
    required   = ["ANTHROPIC_API_KEY"]
    if needs_smtp:
        required += ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    if args.ingest:
        if not args.operator or not args.market:
            parser.error("--ingest requires --operator and --market")
        run_ingest(args, dry_run=args.dry_run)
    elif args.refresh:
        run_refresh(args, dry_run=args.dry_run)
    elif args.mate:
        if not all([args.headcount, args.market, args.budget]):
            parser.error("--mate requires --headcount, --market, and --budget")
        run_intake(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
