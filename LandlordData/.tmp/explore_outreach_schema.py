"""
Explore tables related to outreaches and contacts in the Tandem DB.
"""
import os
import sys
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(r"C:\Users\Sophie\Documents\SpacesAnalysis\UrgentConfirmations\.env", override=True)

DB_URL = os.getenv("NEON_DATABASE_URL")
if not DB_URL:
    sys.exit("NEON_DATABASE_URL not set")

conn = psycopg2.connect(DB_URL)
cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

# List all tables
cur.execute("""
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name;
""")
tables = cur.fetchall()

print("=== ALL TABLES ===")
for t in tables:
    print(f"  {t['table_schema']}.{t['table_name']}")

# Look for outreach-related tables
outreach_tables = [t for t in tables if 'outreach' in t['table_name'].lower() or 'contact' in t['table_name'].lower() or 'lead' in t['table_name'].lower()]
print("\n=== OUTREACH/CONTACT RELATED TABLES ===")
for t in outreach_tables:
    print(f"\n--- {t['table_schema']}.{t['table_name']} ---")
    cur.execute(f"""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position;
    """, (t['table_schema'], t['table_name']))
    cols = cur.fetchall()
    for c in cols:
        print(f"  {c['column_name']} ({c['data_type']}, nullable={c['is_nullable']})")

cur.close()
conn.close()
