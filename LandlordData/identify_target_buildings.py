"""
Phase 1 of the quarterly landlord targeting pipeline.

Queries Neon DB for buildings that:
- Have at least 1 published space (on platform)
- Have no existing opportunity linked via spaceId
- Are in high-demand neighborhoods (by market)
- Have avg llmScore >= 0.55
- Have owner_names populated on at least one space

Buildings with zero searchable spaces are ranked first (best activation opportunity).
Over-fetches 25 rows; Phase 2 (research_owners.py) trims to 5-10 after owner-size filtering.

Usage:
    python identify_target_buildings.py --market sf --quarter 2026-Q3
    python identify_target_buildings.py --market nyc --quarter 2026-Q3
    python identify_target_buildings.py --market boston --quarter 2026-Q3

Requires env var: NEON_DATABASE_URL
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

MARKETS = {
    "sf": {
        "city": "San Francisco",
        "city_exclude": "South San Francisco",
        "neighborhoods": [
            "Mid Market", "South Beach", "SoMa", "SOMA",
            "Financial District", "Union Square", "Mission Bay",
            "Jackson Square", "Hayes Valley",
        ],
        "rep": "Peter Sellick",
    },
    "nyc": {
        "city": "New York",
        "city_exclude": None,
        "neighborhoods": [
            "SoHo", "Flatiron", "Financial District", "FiDi",
            "Chelsea", "NoMad", "Tribeca", "Midtown South",
            "Hudson Square", "West Village", "DUMBO",
        ],
        "rep": "Allegra Citak",
    },
    "boston": {
        "city": "Boston",
        "city_exclude": None,
        "neighborhoods": [
            "Seaport", "Back Bay", "Downtown", "Financial District",
            "South End", "Kendall Square", "Innovation District",
        ],
        "rep": "Ian Ostberg",
        "caveat": (
            "WARNING: Boston shows 0 active matches across all neighborhoods "
            "despite 445 buildings and 2,016 spaces in the DB. Investigate "
            "match conversion before acting on this list. — Ian Ostberg"
        ),
    },
}

QUERY = """
SELECT
    sl.city,
    sl.neighborhood,
    sl."addressLine1"                                                         AS address,
    COUNT(s.id)                                                               AS total_spaces,
    SUM(CASE WHEN s.status = 'PUBLISHED' THEN 1 ELSE 0 END)                  AS published_spaces,
    SUM(CASE WHEN s.status = 'PUBLISHED' AND s."isSearchable" = true
             THEN 1 ELSE 0 END)                                              AS searchable_spaces,
    ROUND(AVG(s."llmScore")::numeric, 3)                                     AS avg_llm_score,
    (array_remove(array_agg(DISTINCT s.owner_names[1]), NULL))[1]            AS owner_name,
    (array_remove(array_agg(DISTINCT s.owner_websites[1]), NULL))[1]         AS owner_website,
    (array_remove(array_agg(DISTINCT s.owner_linkedin_urls[1]), NULL))[1]    AS owner_linkedin
FROM spaces s
JOIN space_location sl ON s."locationId" = sl.id
WHERE sl.city ILIKE %(city)s
  AND (%(city_exclude)s IS NULL OR sl.city NOT ILIKE %(city_exclude)s)
  AND EXISTS (
      SELECT 1 FROM unnest(%(neighborhoods)s::text[]) AS n
      WHERE sl.neighborhood ILIKE ('%%' || n || '%%')
  )
  AND s."llmScore" IS NOT NULL
GROUP BY sl.city, sl.neighborhood, sl."addressLine1"
HAVING
    SUM(CASE WHEN s.status = 'PUBLISHED' THEN 1 ELSE 0 END) >= 1
    AND AVG(s."llmScore") >= 0.55
ORDER BY
    SUM(CASE WHEN s.status = 'PUBLISHED' AND s."isSearchable" = true THEN 1 ELSE 0 END) ASC,
    ROUND(AVG(s."llmScore")::numeric, 3) DESC,
    SUM(CASE WHEN s.status = 'PUBLISHED' THEN 1 ELSE 0 END) DESC
LIMIT 25
"""

COLUMNS = [
    "city", "neighborhood", "address", "total_spaces", "published_spaces",
    "searchable_spaces", "avg_llm_score", "owner_name", "owner_website",
    "owner_linkedin",
]


def run(market: str, quarter: str) -> Path:
    if market not in MARKETS:
        sys.exit(f"Unknown market '{market}'. Choose from: {', '.join(MARKETS)}")

    cfg = MARKETS[market]
    db_url = os.environ.get("NEON_DATABASE_URL")
    if not db_url:
        sys.exit("NEON_DATABASE_URL env var not set")

    out_dir = Path(__file__).parent / "quarterly" / quarter
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{market}_targets.csv"

    print(f"Querying Neon DB for {market.upper()} ({cfg['city']}) target buildings...")

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                QUERY,
                {
                    "city": f"%{cfg['city']}%",
                    "city_exclude": cfg.get("city_exclude"),
                    "neighborhoods": cfg["neighborhoods"],
                },
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print(
            f"  No results for {market.upper()} with current thresholds. "
            "Consider lowering llmScore threshold to 0.45 or expanding neighborhoods."
        )
        rows = []

    print(f"  Found {len(rows)} candidate buildings.")

    if "caveat" in cfg:
        print(f"\n  *** {cfg['caveat']} ***\n")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS + ["boston_caveat"])
        writer.writeheader()
        for row in rows:
            out_row = {col: row.get(col, "") for col in COLUMNS}
            out_row["boston_caveat"] = cfg.get("caveat", "")
            writer.writerow(out_row)

    print(f"  Written to {out_path}")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1: identify target buildings per market")
    parser.add_argument("--market", required=True, choices=list(MARKETS), help="sf, nyc, or boston")
    parser.add_argument("--quarter", required=True, help="e.g. 2026-Q3")
    args = parser.parse_args()
    run(args.market, args.quarter)
