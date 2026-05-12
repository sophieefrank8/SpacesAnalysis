"""
Export suggested outreaches assigned to Allegra Citak with status REQUESTED or ENGAGED.
Includes: space title, listing page URL, and all contact emails for each space.
"""
import os
import sys
import csv
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\Sophie\Documents\SpacesAnalysis\UrgentConfirmations\.env", override=True)

DB_URL = os.getenv("NEON_DATABASE_URL")
if not DB_URL:
    sys.exit("NEON_DATABASE_URL not set")

ALLEGRA_ID = '4f01f3bb-0d50-4ec4-900a-c00403ceeff1'
LISTING_BASE = "https://www.tandem.space/spaces/"
OUTPUT = r"c:\Users\Sophie\Documents\YoutubeTutorial\LandlordData\allegra_outreaches.csv"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

# Pull outreaches with space info
cur.execute("""
    SELECT
        so.id,
        so.status,
        so.type,
        so.note,
        so.created_at,
        s.id        AS space_id,
        s.title     AS space_title,
        s.slug_url
    FROM suggested_outreaches so
    LEFT JOIN spaces s ON s.id = so.space_id
    WHERE
        so.assigned_to = %s
        AND so.status IN ('REQUESTED', 'ENGAGED')
        AND so.deleted_at IS NULL
    ORDER BY so.created_at DESC;
""", (ALLEGRA_ID,))
outreaches = cur.fetchall()

# Build a set of space IDs
space_ids = list({r['space_id'] for r in outreaches if r['space_id']})

# Pull all contact emails for those spaces (space_point_of_contact)
cur.execute("""
    SELECT space_id, email, "firstName" AS name
    FROM space_point_of_contact
    WHERE space_id = ANY(%s::uuid[]) AND email IS NOT NULL AND email != ''
    ORDER BY space_id, "isPrimary" DESC NULLS LAST;
""", (space_ids,))
spoc_rows = cur.fetchall()

# Pull all contact emails for those spaces (space_contact -> contacts)
cur.execute("""
    SELECT sc."spaceId" AS space_id, c.email, c.name
    FROM space_contact sc
    JOIN contacts c ON c.id = sc."contactId"
    WHERE sc."spaceId" = ANY(%s::uuid[]) AND c.email IS NOT NULL AND c.email != ''
    ORDER BY sc."spaceId";
""", (space_ids,))
sc_rows = cur.fetchall()

cur.close()
conn.close()

# Build space_id -> list of emails map (deduplicated)
from collections import defaultdict
space_emails = defaultdict(set)
for r in spoc_rows:
    space_emails[r['space_id']].add(r['email'].strip().lower())
for r in sc_rows:
    space_emails[r['space_id']].add(r['email'].strip().lower())

# Write CSV
rows_written = 0
with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow([
        'outreach_id',
        'status',
        'type',
        'space_title',
        'listing_url',
        'note',
        'created_at',
        'contact_emails',
    ])
    for o in outreaches:
        listing_url = (LISTING_BASE + o['slug_url']) if o['slug_url'] else ''
        emails = '; '.join(sorted(space_emails.get(o['space_id'], set())))
        writer.writerow([
            o['id'],
            o['status'],
            o['type'] or '',
            o['space_title'] or '',
            listing_url,
            (o['note'] or '').replace('\n', ' ').strip(),
            str(o['created_at'])[:19] if o['created_at'] else '',
            emails,
        ])
        rows_written += 1

print(f"Done. {rows_written} rows written to {OUTPUT}")
