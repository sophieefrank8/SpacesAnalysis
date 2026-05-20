import os
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("NEON_DATABASE_URL")
    or os.getenv("NEON_CONNECTION_STRING")
    or ""
)

DUMMY_MODE = not DATABASE_URL

# ---------------------------------------------------------------------------
# Dummy data (Stage 1 — no DB required)
# ---------------------------------------------------------------------------

_DUMMY_USERS = [
    {"id": "1", "name": "Allegra Citak"},
    {"id": "2", "name": "Ian Ostberg"},
    {"id": "3", "name": "Peter Sellick"},
    {"id": "4", "name": "Sophie Frank"},
]

_DUMMY_SPACES = [
    {
        "id": "abc-111-live",
        "status": "PUBLISHED",
        "isSearchable": True,
        "addressLine1": "989 Market St",
        "addressLine2": "Floor 4",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "squareFootage": 2400,
        "minPricePerDesk": 800,
        "maxPricePerDesk": 1000,
        "minNumberOfDesks": 10,
        "maxNumberOfDesks": 20,
        "minTotalPricePerMonth": 8000,
        "maxTotalPricePerMonth": 20000,
    },
    {
        "id": "abc-222-unlisted",
        "status": "PUBLISHED",
        "isSearchable": False,
        "addressLine1": "989 Market St",
        "addressLine2": "Suite 200",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "squareFootage": 1100,
        "minPricePerDesk": 650,
        "maxPricePerDesk": 750,
        "minNumberOfDesks": 5,
        "maxNumberOfDesks": 10,
        "minTotalPricePerMonth": 3250,
        "maxTotalPricePerMonth": 7500,
    },
    {
        "id": "abc-333-draft",
        "status": "IMPORTED",
        "isSearchable": False,
        "addressLine1": "989 Market St",
        "addressLine2": "Floor 6",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "squareFootage": None,
        "minPricePerDesk": None,
        "maxPricePerDesk": None,
        "minNumberOfDesks": None,
        "maxNumberOfDesks": None,
        "minTotalPricePerMonth": None,
        "maxTotalPricePerMonth": None,
    },
    {
        "id": "king-111-live",
        "status": "PUBLISHED",
        "isSearchable": True,
        "addressLine1": "128 King St",
        "addressLine2": "Floor 2",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94107",
        "squareFootage": 3200,
        "minPricePerDesk": 700,
        "maxPricePerDesk": 900,
        "minNumberOfDesks": 15,
        "maxNumberOfDesks": 30,
        "minTotalPricePerMonth": 10500,
        "maxTotalPricePerMonth": 27000,
    },
    {
        "id": "king-222-draft",
        "status": "IMPORTED",
        "isSearchable": False,
        "addressLine1": "128 King St",
        "addressLine2": "Suite 100",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94107",
        "squareFootage": None,
        "minPricePerDesk": None,
        "maxPricePerDesk": None,
        "minNumberOfDesks": None,
        "maxNumberOfDesks": None,
        "minTotalPricePerMonth": None,
        "maxTotalPricePerMonth": None,
    },
]

_DUMMY_CONTACTS = {
    "abc-111-live": [
        {"name": "John Brokerman", "email": "john@cbre.com", "phone_number": "415-555-1234", "title": "VP Leasing", "company_name": "CBRE", "type": "BROKER"},
        {"name": "Mary Owner", "email": "mary@landlord.com", "phone_number": "415-555-5678", "title": "Property Manager", "company_name": "Market Properties LLC", "type": "LANDLORD"},
    ],
    "abc-222-unlisted": [
        {"name": "John Brokerman", "email": "john@cbre.com", "phone_number": "415-555-1234", "title": "VP Leasing", "company_name": "CBRE", "type": "BROKER"},
    ],
    "abc-333-draft": [],
    "king-111-live": [
        {"name": "Sarah Listing", "email": "sarah@jll.com", "phone_number": "415-555-9012", "title": "Senior Director", "company_name": "JLL", "type": "BROKER"},
    ],
    "king-222-draft": [],
}


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_users() -> list[dict]:
    if DUMMY_MODE:
        return _DUMMY_USERS
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id::text, name
                    FROM users
                    WHERE 'ADMIN' = ANY(roles::text[])
                      AND name IS NOT NULL
                      AND email ILIKE '%@tandem.space%'
                    ORDER BY name ASC
                """)
                return cur.fetchall()
    except Exception as e:
        print(f"[db] get_users fallback to dummy: {e}")
        return _DUMMY_USERS


def search_addresses(query: str) -> list[dict]:
    if DUMMY_MODE:
        return _dummy_address_search(query)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT sl."addressLine1", sl.city, sl.state, sl.zip,
                      COUNT(s.id) AS space_count,
                      COUNT(CASE WHEN s.status = 'PUBLISHED' AND s."isSearchable" = true THEN 1 END) AS live_count
                    FROM space_location sl
                    JOIN spaces s ON s."locationId" = sl.id
                    WHERE sl."addressLine1" ILIKE %s
                      AND s."deletedAt" IS NULL
                    GROUP BY sl."addressLine1", sl.city, sl.state, sl.zip
                    ORDER BY space_count DESC
                    LIMIT 10
                """, (f"%{query}%",))
                return cur.fetchall()
    except Exception as e:
        print(f"[db] search_addresses fallback to dummy: {e}")
        return _dummy_address_search(query)


def _dummy_address_search(query: str) -> list[dict]:
    q = query.lower()
    seen = {}
    for s in _DUMMY_SPACES:
        addr = s["addressLine1"]
        if q in addr.lower():
            if addr not in seen:
                seen[addr] = {"addressLine1": addr, "city": s["city"], "state": s["state"], "zip": s["zip"], "space_count": 0, "live_count": 0}
            seen[addr]["space_count"] += 1
            if s["status"] == "PUBLISHED" and s["isSearchable"]:
                seen[addr]["live_count"] += 1
    return list(seen.values())


def get_spaces_at_building(address: str) -> list[dict]:
    if DUMMY_MODE:
        return _dummy_spaces_at(address)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id::text, s.status, s."isSearchable",
                      s."squareFootage", s."minPricePerDesk", s."maxPricePerDesk",
                      s."minNumberOfDesks", s."maxNumberOfDesks",
                      s."minTotalPricePerMonth", s."maxTotalPricePerMonth",
                      sl."addressLine1", sl."addressLine2", sl.city, sl.state, sl.zip
                    FROM spaces s
                    JOIN space_location sl ON s."locationId" = sl.id
                    WHERE sl."addressLine1" ILIKE %s
                      AND s."deletedAt" IS NULL
                    ORDER BY
                      CASE s.status WHEN 'PUBLISHED' THEN 0 ELSE 1 END,
                      s."isSearchable" DESC,
                      s."squareFootage" DESC NULLS LAST
                """, (f"%{address}%",))
                return cur.fetchall()
    except Exception as e:
        print(f"[db] get_spaces_at_building fallback to dummy: {e}")
        return _dummy_spaces_at(address)


def _dummy_spaces_at(address: str) -> list[dict]:
    a = address.lower()
    return [s for s in _DUMMY_SPACES if a in s["addressLine1"].lower()]


def get_space_detail(space_id: str) -> tuple[dict | None, list[dict]]:
    if DUMMY_MODE:
        return _dummy_space_detail(space_id)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id::text, s.status, s."isSearchable",
                      s."squareFootage", s."minPricePerDesk", s."maxPricePerDesk",
                      s."minNumberOfDesks", s."maxNumberOfDesks",
                      s."minTotalPricePerMonth", s."maxTotalPricePerMonth",
                      sl."addressLine1", sl."addressLine2", sl.city, sl.state, sl.zip
                    FROM spaces s
                    JOIN space_location sl ON s."locationId" = sl.id
                    WHERE s.id = %s::uuid
                """, (space_id,))
                space = cur.fetchone()

                contacts = []
                if space:
                    cur.execute("""
                        SELECT c.name, c.email, c.phone_number, c.title, c.company_name, c.type
                        FROM contacts c
                        JOIN space_contact sc ON sc."contactId" = c.id
                        WHERE sc."spaceId" = %s::uuid
                        ORDER BY c.is_primary DESC NULLS LAST, c.name
                    """, (space_id,))
                    contacts = cur.fetchall()

                return space, contacts
    except Exception as e:
        print(f"[db] get_space_detail fallback to dummy: {e}")
        return _dummy_space_detail(space_id)


def _dummy_space_detail(space_id: str) -> tuple[dict | None, list[dict]]:
    space = next((s for s in _DUMMY_SPACES if s["id"] == space_id), None)
    contacts = _DUMMY_CONTACTS.get(space_id, []) if space else []
    return space, contacts


def update_outreach_status(outreach_id: str, status: str) -> None:
    if DUMMY_MODE:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE suggested_outreaches SET status = %s WHERE id = %s::uuid",
                (status, outreach_id),
            )
