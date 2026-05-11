import os
import secrets
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


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for stmt in [
                "ALTER TABLE basecamp_visitor_registration ALTER COLUMN visit_time DROP NOT NULL",
                "ALTER TABLE basecamp_visitor_registration ALTER COLUMN reason DROP NOT NULL",
                "ALTER TABLE basecamp_visitor_registration ALTER COLUMN tenant_company DROP NOT NULL",
                "ALTER TABLE basecamp_visitor_registration ALTER COLUMN guest_company DROP NOT NULL",
            ]:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass


def create_registration(
    tenant_name: str,
    tenant_email: str,
    guest_name: str,
    guest_email: str,
    location: str,
    visit_date: str,
    tenant_company: str = "",
    guest_company: str = "",
    visit_time: str = "",
    reason: str = "",
) -> dict:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO basecamp_visitor_registration
                    (token, tenant_name, tenant_email, tenant_company,
                     guest_name, guest_email, guest_company,
                     location, visit_date, visit_time, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                token, tenant_name, tenant_email, tenant_company,
                guest_name, guest_email, guest_company,
                location, visit_date, visit_time or None, reason or None,
            ))
            return cur.fetchone()


def get_registration(token: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM basecamp_visitor_registration WHERE token = %s", (token,))
            return cur.fetchone()


def confirm_registration(
    token: str,
    confirmed_name: str,
    confirmed_company: str,
    confirmed_email: str,
    office_setup: str,
) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE basecamp_visitor_registration
                SET guest_confirmed_name = %s,
                    guest_confirmed_company = %s,
                    guest_confirmed_email = %s,
                    guest_office_setup = %s,
                    status = 'confirmed',
                    confirmed_at = NOW()
                WHERE token = %s AND status = 'pending'
                RETURNING *
            """, (confirmed_name, confirmed_company, confirmed_email, office_setup, token))
            return cur.fetchone()


def checkin_registration(token: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE basecamp_visitor_registration
                SET status = 'arrived', arrived_at = NOW()
                WHERE token = %s AND status = 'confirmed'
                RETURNING *
            """, (token,))
            return cur.fetchone()


def create_walk_in(
    guest_name: str,
    guest_email: str,
    guest_company: str,
    host_name: str,
    host_email: str,
    location: str,
    reason: str,
) -> dict:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO basecamp_visitor_walk_in
                    (token, guest_name, guest_email, guest_company,
                     host_name, host_email, location, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (token, guest_name, guest_email, guest_company,
                  host_name, host_email, location, reason))
            return cur.fetchone()


def get_all_walk_ins(location_filter: str = "", date_filter: str = "") -> list[dict]:
    clauses = []
    params = []
    if location_filter:
        clauses.append("location = %s")
        params.append(location_filter)
    if date_filter:
        clauses.append("arrived_at::date = %s")
        params.append(date_filter)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM basecamp_visitor_walk_in {where} ORDER BY arrived_at DESC",
                params,
            )
            return cur.fetchall()


def get_host_guest_count_this_week(host_email: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS count FROM (
                    SELECT id FROM basecamp_visitor_registration
                    WHERE LOWER(tenant_email) = LOWER(%s)
                      AND arrived_at >= NOW() - INTERVAL '7 days'
                    UNION ALL
                    SELECT id FROM basecamp_visitor_walk_in
                    WHERE LOWER(host_email) = LOWER(%s)
                      AND arrived_at >= NOW() - INTERVAL '7 days'
                ) combined
            """, (host_email, host_email))
            return cur.fetchone()["count"]


def get_all_registrations(
    location_filter: str = "",
    status_filter: str = "",
    date_filter: str = "",
) -> list[dict]:
    clauses = []
    params = []
    if location_filter:
        clauses.append("location = %s")
        params.append(location_filter)
    if status_filter:
        clauses.append("status = %s")
        params.append(status_filter)
    if date_filter:
        clauses.append("visit_date = %s")
        params.append(date_filter)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM basecamp_visitor_registration {where} ORDER BY visit_date DESC, visit_time DESC",
                params,
            )
            return cur.fetchall()
