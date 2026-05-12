"""
Phase 4 of the quarterly landlord targeting pipeline.

Reads [market]_research.csv and (optionally) [market]_costar.csv for each market,
merges them by address, renders an HTML email, and sends it via SMTP.

CoStar columns that are missing are rendered as a visible "⚠ Enrich with CoStar" notice.

Usage:
    # Preview HTML without sending:
    python compile_and_send.py --quarter 2026-Q3 --dry-run

    # Send to recipients in RECIPIENTS env var:
    python compile_and_send.py --quarter 2026-Q3

    # Send for one market only:
    python compile_and_send.py --quarter 2026-Q3 --markets sf

Requires env vars:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
    RECIPIENTS  (comma-separated: sophie@tandem.space,rafi@tandem.space,...)
"""

import argparse
import csv
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

MARKETS = ["sf", "nyc", "boston"]
MARKET_LABELS = {"sf": "San Francisco", "nyc": "New York City", "boston": "Boston"}
MARKET_REPS = {"sf": "Peter Sellick", "nyc": "Allegra Citak", "boston": "Ian Ostberg"}

COSTAR_FIELDS = [
    "legal_owner_confirmed", "asset_manager_name", "asset_manager_email",
    "asset_manager_phone", "portfolio_size", "asking_rent", "recent_transactions",
    "other_markets", "submarket_vacancy", "leasing_broker",
]

BOSTON_CAVEAT = (
    "⚠️ <strong>Boston signal check:</strong> The Tandem DB currently shows "
    "0 active matches across all Boston neighborhoods despite 445 buildings "
    "and 2,016 spaces. Ian — please diagnose why before acting on this list "
    "(pricing? search ranking? process gap?)."
)

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       color: #1a1a1a; max-width: 900px; margin: 0 auto; padding: 24px; }
h1 { color: #111; font-size: 22px; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
h2 { color: #374151; font-size: 17px; margin-top: 36px; margin-bottom: 4px; }
h3 { color: #111; font-size: 15px; margin-bottom: 2px; }
.market-header { background: #f9fafb; border-left: 4px solid #6366f1;
                 padding: 12px 16px; margin: 24px 0 16px; }
.market-header h2 { margin: 0; color: #111; font-size: 18px; }
.market-header .rep { color: #6b7280; font-size: 13px; margin-top: 2px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 12px 0 24px; }
th { background: #f3f4f6; text-align: left; padding: 7px 10px;
     border-bottom: 2px solid #d1d5db; }
td { padding: 6px 10px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
tr:hover td { background: #fafafa; }
.score-high { color: #059669; font-weight: 600; }
.score-mid  { color: #d97706; font-weight: 600; }
.enrich { color: #9ca3af; font-style: italic; }
.caveat { background: #fff7ed; border: 1px solid #fed7aa;
          border-radius: 6px; padding: 12px 16px; margin: 12px 0; font-size: 13px; }
.owner-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
              padding: 16px; margin: 12px 0; }
.owner-card .angle { background: #f0fdf4; border-left: 3px solid #22c55e;
                     padding: 8px 12px; margin-top: 10px; font-size: 13px;
                     color: #166534; }
.footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid #e5e7eb;
          font-size: 12px; color: #9ca3af; }
"""


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def merge(research: list[dict], costar: list[dict]) -> list[dict]:
    costar_by_address = {r.get("address", "").lower(): r for r in costar}
    merged = []
    for row in research:
        addr = row.get("address", "").lower()
        cs = costar_by_address.get(addr, {})
        merged.append({**row, **{f: cs.get(f, "") for f in COSTAR_FIELDS}})
    return merged


def score_class(score_str: str) -> str:
    try:
        s = float(score_str)
        return "score-high" if s >= 0.70 else "score-mid"
    except (ValueError, TypeError):
        return ""


def costar_cell(value: str, label: str = "") -> str:
    if value and value.strip():
        return value
    label_str = f" ({label})" if label else ""
    return f'<span class="enrich">⚠ CoStar{label_str}</span>'


def render_market_section(market: str, rows: list[dict]) -> str:
    label = MARKET_LABELS[market]
    rep = MARKET_REPS[market]
    html = f"""
<div class="market-header">
  <h2>{label}</h2>
  <div class="rep">Territory: {rep}</div>
</div>
"""
    if market == "boston":
        html += f'<div class="caveat">{BOSTON_CAVEAT}</div>\n'

    if not rows:
        html += "<p><em>No target buildings identified this quarter.</em></p>\n"
        return html

    html += """
<table>
  <thead>
    <tr>
      <th>Address</th><th>Neighborhood</th><th>Owner</th>
      <th>Published / Searchable</th><th>LLM Score</th>
      <th>Tandem Matches</th><th>Asking Rent</th><th>Portfolio Size</th>
    </tr>
  </thead>
  <tbody>
"""
    for r in rows:
        sc = score_class(r.get("avg_llm_score", ""))
        html += f"""
    <tr>
      <td>{r.get('address','')}</td>
      <td>{r.get('neighborhood','')}</td>
      <td><strong>{r.get('company_name') or r.get('owner_name','')}</strong></td>
      <td>{r.get('published_spaces',0)} pub / {r.get('searchable_spaces',0)} live</td>
      <td class="{sc}">{r.get('avg_llm_score','')}</td>
      <td>{r.get('tandem_total_matches',0)} total, {r.get('tandem_active_matches',0)} active</td>
      <td>{costar_cell(r.get('asking_rent',''), 'asking rent')}</td>
      <td>{costar_cell(r.get('portfolio_size',''), 'portfolio size')}</td>
    </tr>"""

    html += "\n  </tbody>\n</table>\n<h3>Owner Profiles</h3>\n"

    for r in rows:
        company = r.get("company_name") or r.get("owner_name") or "Unknown"
        am = r.get("asset_manager_name") or ""
        am_email = r.get("asset_manager_email") or ""
        am_phone = r.get("asset_manager_phone") or ""
        costar_contact = ""
        if am:
            costar_contact = f"{am}"
            if am_email:
                costar_contact += f" — {am_email}"
            if am_phone:
                costar_contact += f" / {am_phone}"
        else:
            costar_contact = costar_cell("", "asset manager contact")

        html += f"""
<div class="owner-card">
  <h3>{company} — {r.get('address','')}</h3>
  <table>
    <tr><th>Primary contact</th>
        <td>{r.get('primary_contact_name','')} ({r.get('primary_contact_title','')})
            {' — ' + r.get('primary_contact_email','') if r.get('primary_contact_email') else ''}
            {' / ' + r.get('primary_contact_phone','') if r.get('primary_contact_phone') else ''}
        </td></tr>
    <tr><th>CoStar asset manager</th><td>{costar_contact}</td></tr>
    <tr><th>Best outreach</th><td>{r.get('best_outreach_method','')}</td></tr>
    <tr><th>Website</th><td>{r.get('website','') or costar_cell('')}</td></tr>
    <tr><th>Other SF properties</th><td>{r.get('other_sf_properties','') or 'None identified'}</td></tr>
    <tr><th>NYC / Boston</th>
        <td>NYC: {r.get('other_nyc_properties','') or 'None'} |
            Boston: {r.get('other_boston_properties','') or 'None'}</td></tr>
    <tr><th>CoStar: legal owner</th>
        <td>{costar_cell(r.get('legal_owner_confirmed',''), 'confirm owner')}</td></tr>
    <tr><th>Recent transactions</th>
        <td>{costar_cell(r.get('recent_transactions',''), 'transactions')}</td></tr>
    <tr><th>Submarket vacancy</th>
        <td>{costar_cell(r.get('submarket_vacancy',''), 'vacancy')}</td></tr>
    <tr><th>Leasing broker</th>
        <td>{costar_cell(r.get('leasing_broker',''), 'broker')}</td></tr>
    <tr><th>Recent news</th><td>{r.get('recent_news','') or 'None'}</td></tr>
  </table>
  <div class="angle"><strong>Outreach angle:</strong> {r.get('outreach_angle','')}</div>
</div>
"""
    return html


def build_html(quarter: str, market_data: dict) -> str:
    body = ""
    for market in MARKETS:
        rows = market_data.get(market, [])
        body += render_market_section(market, rows)

    total = sum(len(v) for v in market_data.values())
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{CSS}</style></head>
<body>
<h1>Quarterly Landlord Target Report — {quarter}</h1>
<p style="color:#6b7280;font-size:13px;">
  {total} target buildings across SF, NYC, and Boston.
  Fields marked <span class="enrich">⚠ CoStar</span> require enrichment
  by the territory rep before outreach.
</p>
{body}
<div class="footer">
  Generated by the Tandem quarterly landlord targeting pipeline.<br>
  Questions? Contact sophie@tandem.space.
</div>
</body>
</html>"""


def send_email(html: str, quarter: str, recipients: list[str]) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Tandem Q{quarter.split('-Q')[-1]} Landlord Target List — {quarter}"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, recipients, msg.as_string())

    print(f"Email sent to: {', '.join(recipients)}")


def run(quarter: str, markets: list[str], dry_run: bool) -> None:
    out_dir = Path(__file__).parent / "quarterly" / quarter
    if not out_dir.exists():
        sys.exit(f"Quarter directory not found: {out_dir}. Run Phases 1+2 first.")

    market_data = {}
    for market in markets:
        research = load_csv(out_dir / f"{market}_research.csv")
        costar = load_csv(out_dir / f"{market}_costar.csv")
        market_data[market] = merge(research, costar)
        cs_count = sum(1 for r in market_data[market] if r.get("legal_owner_confirmed"))
        print(f"  {market.upper()}: {len(research)} researched, {cs_count} CoStar-enriched")

    html = build_html(quarter, market_data)

    report_path = out_dir / f"quarterly_report_{quarter}.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nReport written to {report_path}")

    if dry_run:
        print("Dry run — email not sent.")
        return

    recipients_raw = os.environ.get("RECIPIENTS", "")
    if not recipients_raw:
        sys.exit("RECIPIENTS env var not set. Set to comma-separated email list.")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    send_email(html, quarter, recipients)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 4: compile report and send email")
    parser.add_argument("--quarter", required=True, help="e.g. 2026-Q3")
    parser.add_argument(
        "--markets", nargs="+", default=MARKETS,
        choices=MARKETS, help="Markets to include (default: all)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Write HTML but don't send email")
    args = parser.parse_args()
    run(args.quarter, args.markets, args.dry_run)
