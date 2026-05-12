"""
Phase 2 of the quarterly landlord targeting pipeline.

Reads [market]_targets.csv produced by identify_target_buildings.py and runs in two sub-phases:

2a. Owner size classification (quick pass via Gemini API)
    - Classifies each owner as small / medium / large based on web research
    - Large owners (REITs, national banks, institutional funds) are written to
      [market]_excluded.csv and dropped from further processing
    - Targets: reduce ~25 candidates to 5-10 for deep research

2b. Deep research (surviving small/medium owners)
    - Pulls Neon DB history (match counts, signed deals, last opportunity date)
    - Calls Gemini API for full owner profile: contacts, outreach method,
      full portfolio, other markets, website, news
    - Outputs [market]_research.md and [market]_research.csv

Usage:
    python research_owners.py --market sf --quarter 2026-Q3
    python research_owners.py --market nyc --quarter 2026-Q3 --limit 8

Requires env vars: GEMINI_API_KEY, NEON_DATABASE_URL
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from google import genai
import psycopg2
from psycopg2.extras import RealDictCursor

OWNER_LOOKUP_PREFIX = """\
The owner name for this building is not known. Before proceeding, use your knowledge \
to identify who owns or manages {address}, {city}. Search for the property owner, \
landlord, or building management company associated with this specific address. \
Use that identified owner as the basis for all analysis below.\n\n"""

CLASSIFICATION_PROMPT = """\
You are a commercial real estate analyst. Based on the owner name and any available \
website / LinkedIn context below, classify this building owner as small, medium, or large.

Definitions:
- large: REITs, national/global banks, institutional investment funds (Blackstone, Vornado, \
  SL Green, Tishman, CBRE, Cushman, JLL, national insurance companies), any entity with \
  50+ properties or publicly traded
- medium: Regional developers, family offices managing 10-50 properties, well-known local \
  developers with significant portfolios
- small: Private individuals, single-family LLCs, owner-operators with fewer than 10 \
  properties, jewelry companies owning one building, local partnerships

Owner name: {owner_name}
Website: {owner_website}
LinkedIn: {owner_linkedin}
Building address: {address}

Respond ONLY with valid JSON (no markdown, no explanation):
{{"size": "small|medium|large", "reason": "one sentence"}}
"""

DEEP_RESEARCH_PROMPT = """\
You are a commercial real estate researcher helping Tandem, a flexible workspace marketplace, \
identify and contact building owners. Research the following owner and provide a structured profile.

Owner: {owner_name}
Building: {address}, {city}, {neighborhood}
Website hint: {owner_website}
LinkedIn hint: {owner_linkedin}
Owner size classification: {owner_size}

Provide:
1. Confirmed company name and legal entity name if different
2. Best contact person (name, title, direct email, phone) — prioritize asset manager or \
   property manager over CEO for large companies; owner directly for small operators
3. Second contact if available
4. Best outreach method (email / phone / LinkedIn) and why
5. Other properties in their portfolio (SF, NYC, Boston if any)
6. Whether they are active in NYC or Boston markets
7. Company website (confirm or find)
8. Any recent news (acquisitions, dispositions, leadership changes in last 2 years)
9. One-sentence outreach angle: why Tandem is specifically relevant to this owner now

Format your response as JSON with these exact keys:
{{
  "company_name": "",
  "legal_entity": "",
  "primary_contact_name": "",
  "primary_contact_title": "",
  "primary_contact_email": "",
  "primary_contact_phone": "",
  "primary_contact_linkedin": "",
  "secondary_contact": "",
  "best_outreach_method": "",
  "other_sf_properties": "",
  "other_nyc_properties": "",
  "other_boston_properties": "",
  "website": "",
  "recent_news": "",
  "outreach_angle": ""
}}
"""

NEON_HISTORY_QUERY = """
SELECT
    COUNT(m.id)                                                         AS total_matches,
    SUM(CASE WHEN m.status IN (
        'MATCH_ACTIVATION','MATCH_LIVE','MATCH_RETIRED',
        'CONTRACT_SIGNED','E_SIGNATURE'
    ) THEN 1 ELSE 0 END)                                               AS active_matches,
    SUM(CASE WHEN m."signedContractDocumentId" IS NOT NULL
             THEN 1 ELSE 0 END)                                        AS signed_deals,
    NULL::timestamptz                                                  AS last_opportunity_date
FROM spaces s
JOIN space_location sl ON s."locationId" = sl.id
LEFT JOIN "match" m ON m."spaceId" = s.id
WHERE sl."addressLine1" ILIKE %(address)s
  AND sl.city ILIKE %(city)s
"""

RESEARCH_CSV_FIELDS = [
    "address", "city", "neighborhood", "avg_llm_score", "published_spaces",
    "searchable_spaces", "owner_size", "company_name", "legal_entity",
    "primary_contact_name", "primary_contact_title", "primary_contact_email",
    "primary_contact_phone", "primary_contact_linkedin", "secondary_contact",
    "best_outreach_method", "other_sf_properties", "other_nyc_properties",
    "other_boston_properties", "website", "recent_news", "outreach_angle",
    "tandem_total_matches", "tandem_active_matches", "tandem_signed_deals",
    "tandem_last_opportunity_date",
]


def _gemini_call(client: genai.Client, prompt: str) -> str:
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text.strip()


def _unknown_owner(row: dict) -> bool:
    name = (row.get("owner_name") or "").strip().lower()
    return not name or name == "unknown"


def _lookup_prefix(row: dict) -> str:
    if _unknown_owner(row):
        return OWNER_LOOKUP_PREFIX.format(
            address=row.get("address") or "",
            city=row.get("city") or "",
        )
    return ""


def classify_owner(client: genai.Client, row: dict) -> dict:
    prompt = _lookup_prefix(row) + CLASSIFICATION_PROMPT.format(
        owner_name=row.get("owner_name") or "Unknown — identify from address above",
        owner_website=row.get("owner_website") or "none",
        owner_linkedin=row.get("owner_linkedin") or "none",
        address=row.get("address") or "",
    )
    text = _gemini_call(client, prompt)
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"size": "unknown", "reason": "classification failed"}
    return result


def deep_research(client: genai.Client, row: dict, owner_size: str) -> dict:
    prompt = _lookup_prefix(row) + DEEP_RESEARCH_PROMPT.format(
        owner_name=row.get("owner_name") or "Unknown — identify from address above",
        address=row.get("address") or "",
        city=row.get("city") or "",
        neighborhood=row.get("neighborhood") or "",
        owner_website=row.get("owner_website") or "none",
        owner_linkedin=row.get("owner_linkedin") or "none",
        owner_size=owner_size,
    )
    text = _gemini_call(client, prompt)
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {k: "" for k in RESEARCH_CSV_FIELDS}
        result["outreach_angle"] = "Research failed — run manually"
    return result


def get_neon_history(conn, address: str, city: str) -> dict:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            NEON_HISTORY_QUERY,
            {"address": f"%{address}%", "city": f"%{city}%"},
        )
        row = cur.fetchone()
    if not row:
        return {"total_matches": 0, "active_matches": 0, "signed_deals": 0, "last_opportunity_date": ""}
    return dict(row)


def format_markdown_profile(row: dict, research: dict, history: dict) -> str:
    return f"""
### {research.get('company_name') or row.get('owner_name') or 'Unknown Owner'}

**Building:** {row['address']}, {row['neighborhood']}, {row['city']}
**Avg LLM Score:** {row['avg_llm_score']} | **Published spaces:** {row['published_spaces']} | **Searchable:** {row['searchable_spaces']}
**Owner size:** {row.get('owner_size', '')}
**Tandem history:** {history['total_matches']} total matches, {history['active_matches']} active, {history['signed_deals']} signed deals

| Contact | Details |
|---|---|
| Primary | {research.get('primary_contact_name', '')} — {research.get('primary_contact_title', '')} |
| Email | {research.get('primary_contact_email', '')} |
| Phone | {research.get('primary_contact_phone', '')} |
| LinkedIn | {research.get('primary_contact_linkedin', '')} |
| Secondary | {research.get('secondary_contact', '')} |
| Best method | {research.get('best_outreach_method', '')} |

**Website:** {research.get('website', '')}
**Other SF properties:** {research.get('other_sf_properties', '') or 'None identified'}
**NYC properties:** {research.get('other_nyc_properties', '') or 'None'}
**Boston properties:** {research.get('other_boston_properties', '') or 'None'}
**Recent news:** {research.get('recent_news', '') or 'None'}

**Outreach angle:** {research.get('outreach_angle', '')}

---
""".lstrip()


def run(market: str, quarter: str, limit: int = 10) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    db_url = os.environ.get("NEON_DATABASE_URL")
    if not api_key:
        sys.exit("GEMINI_API_KEY env var not set")
    if not db_url:
        sys.exit("NEON_DATABASE_URL env var not set")

    out_dir = Path(__file__).parent / "quarterly" / quarter
    targets_path = out_dir / f"{market}_targets.csv"
    if not targets_path.exists():
        sys.exit(f"Targets file not found: {targets_path}. Run identify_target_buildings.py first.")

    with open(targets_path, newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))

    if not targets:
        print(f"No targets found in {targets_path}. Nothing to research.")
        return

    client = genai.Client(api_key=api_key)
    conn = psycopg2.connect(db_url)

    excluded = []
    kept = []

    print(f"\n=== Phase 2a: Owner size classification ({len(targets)} buildings) ===")
    for row in targets:
        owner = row.get("owner_name") or "Unknown"
        print(f"  Classifying: {owner} @ {row.get('address', '')}")
        result = classify_owner(client, row)
        row["owner_size"] = result.get("size", "unknown")
        row["classification_reason"] = result.get("reason", "")
        if result.get("size") == "large":
            excluded.append(row)
            print(f"    → EXCLUDED (large): {result.get('reason', '')}")
        else:
            kept.append(row)
            print(f"    → KEPT ({result.get('size', 'unknown')}): {result.get('reason', '')}")
        time.sleep(0.5)

    excluded_path = out_dir / f"{market}_excluded.csv"
    with open(excluded_path, "w", newline="", encoding="utf-8") as f:
        if excluded:
            writer = csv.DictWriter(f, fieldnames=list(excluded[0].keys()))
            writer.writeheader()
            writer.writerows(excluded)
    print(f"\n  Excluded {len(excluded)} large owners → {excluded_path}")
    print(f"  Proceeding with {len(kept)} small/medium owners (capping at {limit})\n")

    kept = kept[:limit]

    print(f"=== Phase 2b: Deep research ({len(kept)} buildings) ===")
    research_rows = []
    md_sections = []

    for row in kept:
        owner = row.get("owner_name") or "Unknown"
        address = row.get("address", "")
        city = row.get("city", "")
        print(f"  Researching: {owner} @ {address}")

        research = deep_research(client, row, row.get("owner_size", "unknown"))
        history = get_neon_history(conn, address, city)

        out_row = {
            "address": address,
            "city": city,
            "neighborhood": row.get("neighborhood", ""),
            "avg_llm_score": row.get("avg_llm_score", ""),
            "published_spaces": row.get("published_spaces", ""),
            "searchable_spaces": row.get("searchable_spaces", ""),
            "owner_size": row.get("owner_size", ""),
            **research,
            "tandem_total_matches": history.get("total_matches", 0),
            "tandem_active_matches": history.get("active_matches", 0),
            "tandem_signed_deals": history.get("signed_deals", 0),
            "tandem_last_opportunity_date": str(history.get("last_opportunity_date") or ""),
        }
        research_rows.append(out_row)
        md_sections.append(format_markdown_profile(row, research, history))
        time.sleep(1.0)

    conn.close()

    csv_path = out_dir / f"{market}_research.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESEARCH_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(research_rows)

    md_path = out_dir / f"{market}_research.md"
    market_label = {"sf": "San Francisco", "nyc": "New York City", "boston": "Boston"}.get(market, market.upper())
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {market_label} — Quarterly Landlord Target Research\n")
        f.write(f"*Quarter: {quarter} | Generated by research_owners.py*\n\n")
        f.write("---\n\n")
        f.writelines(md_sections)

    print(f"\n  Research CSV → {csv_path}")
    print(f"  Research doc → {md_path}")
    print(f"\nDone. {len(research_rows)} owner profiles written.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 2: classify owners and research targets")
    parser.add_argument("--market", required=True, choices=["sf", "nyc", "boston"])
    parser.add_argument("--quarter", required=True, help="e.g. 2026-Q3")
    parser.add_argument("--limit", type=int, default=10, help="Max owners to deep-research after filtering (default 10)")
    args = parser.parse_args()
    run(args.market, args.quarter, args.limit)
