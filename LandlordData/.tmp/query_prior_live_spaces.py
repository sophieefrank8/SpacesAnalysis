"""
Find spaces that were already live (published+searchable) BEFORE this week
at 39 Broadway, 32 Broadway, and 233 Broadway.
"""

import os
import sys
from pathlib import Path
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\Sophie\Documents\SpacesAnalysis\UrgentConfirmations\.env", override=True)

DB_URL = os.getenv("NEON_DATABASE_URL")
if not DB_URL:
    sys.exit("NEON_DATABASE_URL not set")

conn = psycopg2.connect(DB_URL)
cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

WEEK_START = "2026-04-13 00:00:00-04:00"
ADDRESSES  = ("39 Broadway", "32 Broadway", "233 Broadway")

Q = """
SELECT
    sl."addressLine1",
    s.id,
    s.title,
    s.status,
    s."isSearchable",
    GREATEST(s."firstPublishedDate", s."firstSearchableDate")
        AT TIME ZONE 'America/New_York'   AS went_live_et,
    s."firstPublishedDate"  AT TIME ZONE 'America/New_York' AS first_published_et,
    s."firstSearchableDate" AT TIME ZONE 'America/New_York' AS first_searchable_et
FROM spaces s
JOIN space_location sl ON sl.id = s."locationId"
WHERE
    sl."addressLine1" = ANY(%(addrs)s)
    AND s."firstPublishedDate"  IS NOT NULL
    AND s."firstSearchableDate" IS NOT NULL
    AND s."deletedAt" IS NULL
ORDER BY sl."addressLine1", went_live_et;
"""

cur.execute(Q, {"addrs": list(ADDRESSES)})
rows = cur.fetchall()

current_addr = None
for r in rows:
    if r["addressLine1"] != current_addr:
        current_addr = r["addressLine1"]
        print()
        print("=" * 95)
        print(f"  {current_addr}")
        print("=" * 95)
        print(f"  {'Title':<45} {'Status':<12} {'Srchble':<8} {'Went Live (ET)':<22} {'Pre-existing?'}")
        print(f"  {'-'*43} {'-'*10} {'-'*6} {'-'*20} {'-'*13}")

    went_live = r["went_live_et"]
    pre_existing = "YES (before week)" if went_live < __import__("datetime").datetime(2026, 4, 13, 0, 0, 0) else "new this week"

    print(
        f"  {str(r['title'] or ''):<45} "
        f"{str(r['status']):<12} "
        f"{'yes' if r['isSearchable'] else 'no':<8} "
        f"{str(went_live)[:19]:<22} "
        f"{pre_existing}"
    )

print()
cur.close()
conn.close()
