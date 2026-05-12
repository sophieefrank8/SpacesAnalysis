"""
Query: Buildings that went live in NYC this week (week of Apr 13–15, 2026).

Definition:
  - Building = addressLine1 from the space_location table
  - Went live = the FIRST TIME any space at that addressLine1 became
                status='PUBLISHED' AND isSearchable=true, measured as
                GREATEST(firstPublishedDate, firstSearchableDate).
  - "This week" = Monday Apr 13 2026 00:00 ET through now (Apr 15).
  - A building "went live this week" if no space at that address had ever
    previously been published+searchable, and the first such date falls within
    this week.
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

# Week bounds: Monday Apr 13 00:00 ET → now
WEEK_START = "2026-04-13 00:00:00-04:00"

BUILDINGS_LIVE_Q = """
WITH ever_live_spaces AS (
    -- All spaces that were ever both published AND searchable
    -- "went live" = GREATEST(firstPublishedDate, firstSearchableDate)
    -- which is the moment both conditions were simultaneously true for the first time
    SELECT
        s.id                                                          AS space_id,
        sl."addressLine1",
        sl.city,
        sl.neighborhood,
        GREATEST(s."firstPublishedDate", s."firstSearchableDate")     AS went_live_at,
        s.status,
        s."isSearchable"
    FROM spaces s
    JOIN space_location sl ON sl.id = s."locationId"
    WHERE
        s."firstPublishedDate"  IS NOT NULL
        AND s."firstSearchableDate" IS NOT NULL
        AND sl.city ILIKE '%%new york%%'
        AND sl."addressLine1"   IS NOT NULL
        AND s."deletedAt"       IS NULL
),
building_first_live AS (
    -- For each address, find the very first time any space at that address went live
    SELECT
        "addressLine1",
        city,
        neighborhood,
        MIN(went_live_at)                 AS first_ever_live_date,
        COUNT(DISTINCT space_id)          AS total_spaces_ever_live
    FROM ever_live_spaces
    GROUP BY "addressLine1", city, neighborhood
),
current_live AS (
    -- Currently published+searchable spaces per address (for context)
    SELECT
        sl."addressLine1",
        COUNT(DISTINCT s.id)              AS current_live_spaces
    FROM spaces s
    JOIN space_location sl ON sl.id = s."locationId"
    WHERE
        s.status = 'PUBLISHED'
        AND s."isSearchable" = true
        AND sl.city ILIKE '%%new york%%'
        AND sl."addressLine1" IS NOT NULL
        AND s."deletedAt" IS NULL
    GROUP BY sl."addressLine1"
)
SELECT
    b."addressLine1",
    b.city,
    b.neighborhood,
    b.first_ever_live_date AT TIME ZONE 'America/New_York'  AS first_live_date_et,
    b.total_spaces_ever_live,
    COALESCE(c.current_live_spaces, 0)                      AS current_live_spaces
FROM building_first_live b
LEFT JOIN current_live c ON c."addressLine1" = b."addressLine1"
WHERE
    b.first_ever_live_date >= %(week_start)s
    AND b.first_ever_live_date <= NOW()
ORDER BY b.first_ever_live_date;
"""

cur.execute(BUILDINGS_LIVE_Q, {"week_start": WEEK_START})
rows = cur.fetchall()

print("=" * 90)
print(f"NYC BUILDINGS THAT WENT LIVE THIS WEEK (Apr 13–15, 2026)")
print("=" * 90)
print(f"{'Address':<45} {'Neighborhood':<22} {'First Live (ET)':<22} {'Spaces'}")
print(f"{'-'*43} {'-'*20} {'-'*20} {'-'*6}")

for r in rows:
    print(
        f"  {str(r['addressLine1']):<43} "
        f"{str(r['neighborhood'] or ''):<20} "
        f"{str(r['first_live_date_et'])[:19]:<20} "
        f"{r['current_live_spaces']} live / {r['total_spaces_ever_live']} ever"
    )

print()
print(f"Total buildings: {len(rows)}")

# ── Also check how many spaces (not buildings) went live this week ─────────────
SPACES_Q = """
SELECT
    sl."addressLine1",
    s.id,
    s.title,
    s.status,
    s."isSearchable",
    GREATEST(s."firstPublishedDate", s."firstSearchableDate") AT TIME ZONE 'America/New_York' AS went_live_et
FROM spaces s
JOIN space_location sl ON sl.id = s."locationId"
WHERE
    s."firstPublishedDate"  IS NOT NULL
    AND s."firstSearchableDate" IS NOT NULL
    AND sl.city ILIKE '%%new york%%'
    AND s."deletedAt" IS NULL
    AND GREATEST(s."firstPublishedDate", s."firstSearchableDate") >= %(week_start)s
    AND GREATEST(s."firstPublishedDate", s."firstSearchableDate") <= NOW()
ORDER BY went_live_et;
"""

cur.execute(SPACES_Q, {"week_start": WEEK_START})
spaces = cur.fetchall()

print()
print("=" * 90)
print("INDIVIDUAL SPACES that went live this week (for cross-reference)")
print("=" * 90)
print(f"{'Address':<40} {'Title':<35} {'Went Live (ET)'}")
print(f"{'-'*38} {'-'*33} {'-'*20}")
for s in spaces:
    print(
        f"  {str(s['addressLine1']):<38} "
        f"{str(s['title'] or ''):<33} "
        f"{str(s['went_live_et'])[:19]}"
    )
print()
print(f"Total spaces: {len(spaces)}")

cur.close()
conn.close()
