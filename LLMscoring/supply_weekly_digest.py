"""
Weekly supply ops digest — runs every Tuesday morning.

Queries Neon for each supply team member's pending action items across
5 priority buckets and emails each rep a prioritized CSV attachment.

Priority order:
  1  Active Demand Outreach   — ACTIVE_SEARCH_DEMAND suggested outreaches
  2  Staleness Check          — STALESNESS_CHECK suggested outreaches
  3  Overdue Follow-Up        — opportunity_event rows past follow_up_date
  4  Promising Building       — PROMISING_BUILDING suggested outreaches
  5  Landlord Targeting       — P1/P2 LANDLORD opportunities, not yet champion

Env vars required:
    NEON_DATABASE_URL                          Neon Postgres connection string
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
    ALLEGRA_EMAIL, PETER_EMAIL, IAN_EMAIL

Usage:
    python supply_weekly_digest.py             send emails
    python supply_weekly_digest.py --dry-run   write CSVs locally, no email
"""

import csv
import io
import os
import smtplib
import sys
from datetime import date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras

DASHBOARD_BASE = "https://tandem.space/admin"

TEAM_MEMBERS = [
    {
        "name": "Allegra",
        "email_env": "ALLEGRA_EMAIL",
        "user_id": "4f01f3bb-0d50-4ec4-900a-c00403ceeff1",
    },
    {
        "name": "Pete",
        "email_env": "PETE_EMAIL",
        "user_id": "18f3bde5-c3b3-4500-b0e1-8c443161720c",
    },
    {
        "name": "Ian",
        "email_env": "IAN_EMAIL",
        "user_id": "2c02d9f4-fa41-44c3-b5f3-3999701ba9aa",
    },
]

CSV_COLUMNS = [
    "priority_rank",
    "contact_type",
    "opportunity_or_space",
    "city",
    "stage_or_status",
    "note",
    "last_event_date",
    "last_event_type",
    "follow_up_date",
    "days_overdue",
    "dashboard_link",
]

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       color: #1a1a1a; max-width: 640px; margin: 0 auto; padding: 24px; }
h1   { font-size: 20px; color: #111; margin-bottom: 4px; }
p    { font-size: 14px; color: #374151; line-height: 1.6; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 16px; }
th   { background: #f3f4f6; text-align: left; padding: 7px 10px;
       border-bottom: 2px solid #d1d5db; }
td   { padding: 6px 10px; border-bottom: 1px solid #e5e7eb; }
.rank { font-weight: 700; color: #6366f1; width: 24px; }
.footer { margin-top: 32px; font-size: 12px; color: #9ca3af;
          border-top: 1px solid #e5e7eb; padding-top: 12px; }
"""

BUCKET_DESCRIPTIONS = [
    ("Active Demand Outreach", "A tenant is actively searching — reach out to the broker/landlord now"),
    ("Staleness Check",        "A live space needs data verified — price, availability, or terms"),
    ("Overdue Follow-Up",      "A scheduled follow-up has passed its due date"),
    ("Promising Building",     "Supply ops flagged this building as worth pursuing"),
    ("Landlord Targeting",     "High-priority landlord in your pipeline — long-term play"),
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def connect():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL env var not set")
    return psycopg2.connect(url, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Bucket queries
# ---------------------------------------------------------------------------

_OUTREACH_SQL = """
SELECT
    so.id,
    so.note,
    so.status,
    sl."addressLine1"  AS address,
    sl.city
FROM suggested_outreaches so
JOIN spaces          s  ON s.id         = so.space_id
JOIN space_location  sl ON sl.id        = s."locationId"
WHERE so.deleted_at IS NULL
  AND so.type        = %(type)s
  AND so.status NOT IN ('RESOLVED', 'PAGE_UPDATED')
  AND so.assigned_to = %(user_id)s
ORDER BY so.created_at ASC
"""

_OVERDUE_SQL = """
SELECT DISTINCT ON (o.id)
    oe.follow_up_date,
    oe.type        AS event_type,
    oe.notes,
    oe.date        AS event_date,
    DATE_PART('day', NOW() - oe.follow_up_date)::int AS days_overdue,
    o.id           AS opportunity_id,
    o.name         AS opportunity_name,
    o.stage
FROM opportunity_event oe
JOIN opportunities o ON o.id = oe.opportunity_id
WHERE oe.follow_up_date < NOW()
  AND o.owner_id = %(user_id)s
ORDER BY o.id, oe.follow_up_date ASC
"""

_LANDLORD_SQL = """
SELECT
    o.id,
    o.name,
    o.stage,
    o.priority,
    (SELECT oe.date  FROM opportunity_event oe WHERE oe.opportunity_id = o.id
     ORDER BY oe.date DESC LIMIT 1) AS last_event_date,
    (SELECT oe.type  FROM opportunity_event oe WHERE oe.opportunity_id = o.id
     ORDER BY oe.date DESC LIMIT 1) AS last_event_type,
    (SELECT oe.notes FROM opportunity_event oe WHERE oe.opportunity_id = o.id
     ORDER BY oe.date DESC LIMIT 1) AS last_notes
FROM opportunities o
WHERE o.owner_id  = %(user_id)s
  AND o.type      = 'LANDLORD'
  AND o.priority  IN ('P1', 'P2')
  AND o.stage NOT IN ('CHAMPION', 'NOT_AVAILABLE', 'ON_HOLD')
ORDER BY o.priority ASC, o.updated_at ASC
"""


def _fmt_date(val):
    if val is None:
        return ""
    return val.strftime("%Y-%m-%d")


def fetch_suggested_outreaches(cur, user_id, outreach_type, priority_rank, label):
    cur.execute(_OUTREACH_SQL, {"type": outreach_type, "user_id": user_id})
    rows = []
    for r in cur.fetchall():
        rows.append({
            "priority_rank":        priority_rank,
            "contact_type":         label,
            "opportunity_or_space": r["address"] or "",
            "city":                 r["city"] or "",
            "stage_or_status":      r["status"] or "",
            "note":                 r["note"] or "",
            "last_event_date":      "",
            "last_event_type":      "",
            "follow_up_date":       "",
            "days_overdue":         "",
            "dashboard_link":       f"{DASHBOARD_BASE}/spaces/{r['id']}",
        })
    return rows


def fetch_overdue_events(cur, user_id):
    cur.execute(_OVERDUE_SQL, {"user_id": user_id})
    rows = []
    for r in cur.fetchall():
        rows.append({
            "priority_rank":        3,
            "contact_type":         "Overdue Follow-Up",
            "opportunity_or_space": r["opportunity_name"] or "",
            "city":                 "",
            "stage_or_status":      r["stage"] or "",
            "note":                 (r["notes"] or "")[:200],
            "last_event_date":      _fmt_date(r["event_date"]),
            "last_event_type":      r["event_type"] or "",
            "follow_up_date":       _fmt_date(r["follow_up_date"]),
            "days_overdue":         r["days_overdue"] if r["days_overdue"] is not None else "",
            "dashboard_link":       f"{DASHBOARD_BASE}/opportunities/{r['opportunity_id']}",
        })
    return rows


def fetch_landlord_opportunities(cur, user_id):
    cur.execute(_LANDLORD_SQL, {"user_id": user_id})
    rows = []
    for r in cur.fetchall():
        rows.append({
            "priority_rank":        5,
            "contact_type":         "Landlord Targeting",
            "opportunity_or_space": r["name"] or "",
            "city":                 "",
            "stage_or_status":      r["stage"] or "",
            "note":                 (r["last_notes"] or "")[:200],
            "last_event_date":      _fmt_date(r["last_event_date"]),
            "last_event_type":      r["last_event_type"] or "",
            "follow_up_date":       "",
            "days_overdue":         "",
            "dashboard_link":       f"{DASHBOARD_BASE}/opportunities/{r['id']}",
        })
    return rows


# ---------------------------------------------------------------------------
# CSV + email
# ---------------------------------------------------------------------------

def build_csv(all_rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(all_rows)
    return buf.getvalue().encode("utf-8")


def build_html(name, rows):
    bucket_counts = {i + 1: sum(1 for r in rows if r["priority_rank"] == i + 1) for i in range(5)}
    table_rows = ""
    for rank, (label, desc) in enumerate(BUCKET_DESCRIPTIONS, start=1):
        count = bucket_counts.get(rank, 0)
        count_str = f"<strong>{count}</strong>" if count > 0 else f'<span style="color:#9ca3af">{count}</span>'
        table_rows += f"<tr><td class='rank'>{rank}</td><td>{label}</td><td>{desc}</td><td>{count_str}</td></tr>\n"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{CSS}</style></head>
<body>
<h1>Hi {name} — your weekly supply digest is attached</h1>
<p>Open the CSV to see your action items in priority order.
<strong>Row 1 is your most urgent item.</strong>
Dashboard links are included in the last column of each row.</p>

<table>
  <thead>
    <tr><th>#</th><th>Bucket</th><th>What it means</th><th>This week</th></tr>
  </thead>
  <tbody>
{table_rows}  </tbody>
</table>

<div class="footer">
  Generated every Tuesday · Questions? sophie@tandem.space
</div>
</body>
</html>"""


def send_email(to_addr, name, rows, report_date):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    filename = f"supply_digest_{name.lower()}_{report_date}.csv"
    subject = f"Your Weekly Supply Digest — {report_date}"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    msg.attach(MIMEText(build_html(name, rows), "html"))

    csv_bytes = build_csv(rows)
    attachment = MIMEBase("text", "csv")
    attachment.set_payload(csv_bytes)
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(attachment)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_addr], msg.as_string())

    print(f"  Sent to {to_addr}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    report_date = date.today().strftime("%Y-%m-%d")

    required_env = ["NEON_DATABASE_URL", "ALLEGRA_EMAIL", "PETE_EMAIL", "IAN_EMAIL"]
    if not dry_run:
        required_env += ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    conn = connect()
    cur = conn.cursor()

    for member in TEAM_MEMBERS:
        name    = member["name"]
        user_id = member["user_id"]
        email   = os.environ[member["email_env"]]

        print(f"\n=== {name} ({email}) ===")

        rows = []
        rows += fetch_suggested_outreaches(cur, user_id, "ACTIVE_SEARCH_DEMAND", 1, "Active Demand Outreach")
        rows += fetch_suggested_outreaches(cur, user_id, "STALESNESS_CHECK",     2, "Staleness Check")
        rows += fetch_overdue_events(cur, user_id)
        rows += fetch_suggested_outreaches(cur, user_id, "PROMISING_BUILDING",   4, "Promising Building")
        rows += fetch_landlord_opportunities(cur, user_id)

        for rank in range(1, 6):
            count = sum(1 for r in rows if r["priority_rank"] == rank)
            print(f"  Priority {rank}: {count} rows")
        print(f"  Total: {len(rows)} rows")

        if dry_run:
            out_path = f"digest_{name.lower()}_{report_date}.csv"
            with open(out_path, "wb") as f:
                f.write(build_csv(rows))
            print(f"  Dry run — CSV written to {out_path}")
        else:
            send_email(email, name, rows, report_date)

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
