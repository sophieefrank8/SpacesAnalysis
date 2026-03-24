"""
Tandem Monthly Newsletter
Queries Neon DB and emails a deal spotlight + market stats for NYC and SF.
"""

import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["NEON_DATABASE_URL"]
SMTP_HOST    = os.environ["SMTP_HOST"]
SMTP_PORT    = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
RECIPIENT    = os.environ.get("RECIPIENT", "sophie@tandem.space")

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

DEAL_SPOTLIGHT_SQL = """
WITH deals AS (
    SELECT
        mai.id,
        s."squareFootage",
        mai.term          AS lease_months,
        COALESCE(lm.new_rent, mai."proposedMonthlyPrice", 0) AS monthly_rent,
        mai."proposedNumberOfDesks"                          AS desks,
        mate.title        AS tenant_name,
        mate.vertical     AS tenant_vertical,
        mate."totalFullTimeEmployees" AS tenant_headcount,
        host.title        AS host_name,
        sl.address,
        sl.neighborhood,
        sl.city,
        mai."contractSignedAt"
    FROM match_activation_info mai
    JOIN match m   ON m.id           = mai."matchId"
    JOIN spaces s  ON m."spaceId"    = s.id
    JOIN space_location sl ON sl.id  = s."locationId"
    JOIN companies mate ON m."mateId" = mate.id
    JOIN companies host ON m."hostId" = host.id
    LEFT JOIN LATERAL (
        SELECT new_rent
        FROM retool_closed_deal_modifications
        WHERE matchid = mai."matchId"
          AND effective_date IS NOT NULL
        ORDER BY effective_date DESC
        LIMIT 1
    ) lm ON true
    WHERE s."sharingKind" = 'FULLY_PRIVATE'
      AND s.term          = 'D2L'
      AND mai."contractSignedAt" >= date_trunc('month', CURRENT_DATE - INTERVAL '1 month')
      AND mai."contractSignedAt" <  date_trunc('month', CURRENT_DATE)
      AND (mai."moveOutDate" IS NULL OR mai."moveOutDate" > mai."contractSignedAt")
      AND {state_filter}
),
ranked AS (
    SELECT *,
        PERCENT_RANK() OVER (ORDER BY "squareFootage"  NULLS FIRST) +
        PERCENT_RANK() OVER (ORDER BY lease_months     NULLS FIRST) +
        PERCENT_RANK() OVER (ORDER BY monthly_rent     NULLS FIRST) AS score
    FROM deals
)
SELECT * FROM ranked ORDER BY score DESC LIMIT 1;
"""

TOUR_COUNT_SQL = """
SELECT COUNT(*) AS tour_count
FROM tour t
JOIN match m  ON t.match_id  = m.id
JOIN spaces s ON m."spaceId" = s.id
JOIN space_location sl ON sl.id = s."locationId"
WHERE t.schedule_at >= CURRENT_DATE - INTERVAL '30 days'
  AND t.status = 'CREATED'
  AND {state_filter};
"""

PAGE_VIEWS_SQL = """
SELECT
    COUNT(DISTINCT s.id)               AS spaces_with_views,
    SUM(COALESCE(v.views_30d, 0))      AS total_views
FROM spaces s
JOIN space_location sl ON sl.id = s."locationId"
LEFT JOIN analytics.space_views_rolling v ON v.space_id = s.id
WHERE s.status = 'PUBLISHED'
  AND {state_filter};
"""

NEW_LISTINGS_SQL = """
SELECT COUNT(*) AS new_listings
FROM spaces s
JOIN space_location sl ON sl.id = s."locationId"
WHERE s.status = 'PUBLISHED'
  AND s."firstSearchableDate" >= CURRENT_DATE - INTERVAL '30 days'
  AND {state_filter};
"""

NEW_SIGNUPS_SQL = """
SELECT COUNT(DISTINCT c.id) AS new_signups
FROM companies c
INNER JOIN space_requirements sr ON sr."companyId" = c.id
LEFT JOIN company_creation_data ccd ON c.id = ccd.company_id
LEFT JOIN company_users cu ON cu.company_id = c.id
LEFT JOIN users u ON u.id = cu.user_id
WHERE c."createdAt" >= CURRENT_DATE - INTERVAL '30 days'
  AND (u.email IS NULL OR u.email NOT LIKE '%@tandem.space%')
  AND c."duplicateOf" IS NULL
  AND (ccd.is_test_company IS NULL OR NOT ccd.is_test_company)
  AND (ccd.created_by_admin IS NULL OR NOT ccd.created_by_admin);
"""

# ---------------------------------------------------------------------------
# City filter helpers
# ---------------------------------------------------------------------------

NYC_FILTER = "sl.state = 'NY'"
SF_FILTER  = "sl.state = 'CA' AND sl.city ILIKE 'San Francisco' AND sl.city NOT ILIKE 'South San Francisco'"

CITIES = [
    {
        "name": "New York City", "short": "NYC", "state_filter": NYC_FILTER,
        "contact_name": "Allegra Citak", "contact_first": "Allegra",
        "contact_title": "NYC Supply Lead",
    },
    {
        "name": "San Francisco", "short": "SF", "state_filter": SF_FILTER,
        "contact_name": "Pete Sellick", "contact_first": "Pete",
        "contact_title": "SF Supply Lead",
    },
]

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def query_one(conn, sql):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchone()


def fetch_city_data(conn, city):
    state_filter = city["state_filter"]

    deal = query_one(conn, DEAL_SPOTLIGHT_SQL.format(state_filter=state_filter))
    tours = query_one(conn, TOUR_COUNT_SQL.format(state_filter=state_filter))
    views = query_one(conn, PAGE_VIEWS_SQL.format(state_filter=state_filter))
    listings = query_one(conn, NEW_LISTINGS_SQL.format(state_filter=state_filter))
    signups = query_one(conn, NEW_SIGNUPS_SQL)  # platform-wide, fetched once

    return {
        "deal":     deal,
        "tours":    int(tours["tour_count"]) if tours else 0,
        "views":    int(views["total_views"]) if views else 0,
        "listings": int(listings["new_listings"]) if listings else 0,
        "signups":  int(signups["new_signups"]) if signups else 0,
    }

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def fmt_money(val):
    if val is None:
        return "undisclosed"
    return f"${int(val):,}"


def fmt_sqft(val):
    if val is None:
        return "an undisclosed"
    return f"{int(val):,}-sq-ft"


def ordinal_suffix(n):
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

# ---------------------------------------------------------------------------
# Newsletter copy generation
# ---------------------------------------------------------------------------

def build_deal_paragraph(city_name, deal):
    if not deal:
        return (
            f"We didn't close a qualifying direct-to-landlord private deal in "
            f"{city_name} last month, but the pipeline is active and we have "
            f"several promising conversations underway. Hopefully we'll have "
            f"something exciting to share next month."
        )

    sqft       = fmt_sqft(deal["squareFootage"])
    desks      = deal["desks"] or "an undisclosed number of"
    tenant     = deal["tenant_name"] or "a growing company"
    vertical   = deal["tenant_vertical"]
    headcount  = deal["tenant_headcount"]
    host       = deal["host_name"] or "a local landlord"
    address    = deal["address"] or ""
    hood       = deal["neighborhood"] or deal["city"] or city_name
    lease      = deal["lease_months"]
    rent       = fmt_money(deal["monthly_rent"])

    tenant_desc = tenant
    if vertical:
        tenant_desc += f", a {vertical.lower()} company"
    if headcount:
        tenant_desc += f" with {headcount} employees"

    lease_str = f"{lease}-month" if lease else "multi-year"

    location_str = address if address else hood
    if address and hood and hood.lower() not in address.lower():
        location_str = f"{address} in {hood}"

    return (
        f"One deal we're especially proud of from last month: {tenant_desc} found "
        f"their new home — {desks} desks of {sqft} private office space at "
        f"{location_str}. They connected with {host} through Tandem and landed "
        f"a {lease_str} lease at {rent}/month. It's the kind of match that reminds us "
        f"why we do this work."
    )


def build_stats_paragraph(city_name, data):
    tours    = data["tours"]
    views    = data["views"]
    listings = data["listings"]
    signups  = data["signups"]

    stats = []

    if tours > 0:
        stats.append(f"<strong>{tours:,} tour{'' if tours == 1 else 's'}</strong> "
                     f"scheduled across {city_name} listings")
    if listings > 0:
        stats.append(f"<strong>{listings:,} new listing{'' if listings == 1 else 's'}</strong> "
                     f"added to our {city_name} inventory")
    if views > 0:
        stats.append(f"<strong>{views:,} listing page views</strong> across "
                     f"{city_name} in the last 30 days")
    if signups > 0 and city_name == "San Francisco":  # report signups once, in SF section
        stats.append(f"<strong>{signups:,} new companies</strong> joined Tandem "
                     f"platform-wide this month")

    if not stats:
        return (
            f"Activity in {city_name} is building — we'll have more to share "
            f"next month as the pipeline matures."
        )

    # pick up to 3 stats
    stats = stats[:3]
    if len(stats) == 1:
        body = stats[0]
    elif len(stats) == 2:
        body = f"{stats[0]} and {stats[1]}"
    else:
        body = f"{stats[0]}, {stats[1]}, and {stats[2]}"

    return (
        f"We're still a small team, but the activity speaks for itself: over the last 30 days "
        f"we saw {body} in {city_name}. We're genuinely grateful to the landlords and brokers "
        f"who trust us to bring quality tenants to their spaces."
    )

# ---------------------------------------------------------------------------
# HTML email assembly
# ---------------------------------------------------------------------------

CITY_COLORS = {
    "New York City": "#1a1a2e",
    "San Francisco": "#0f3460",
}

SECTION_TEMPLATE = """
<tr>
  <td style="padding: 32px 40px 8px 40px;">
    <h2 style="margin:0 0 16px 0; font-size:18px; color:{color}; font-family:sans-serif;
               letter-spacing:1px; text-transform:uppercase; border-bottom:2px solid {color};
               padding-bottom:8px;">
      {city_short} Market Update
    </h2>
    <p style="margin:0 0 12px 0; font-size:15px; line-height:1.7; color:#555;
              font-family:sans-serif; font-style:italic;">
      From {contact_name}, {contact_title}
    </p>
    <p style="margin:0 0 16px 0; font-size:15px; line-height:1.7; color:#222;
              font-family:sans-serif;">
      {deal_para}
    </p>
    <p style="margin:0 0 8px 0; font-size:15px; line-height:1.7; color:#222;
              font-family:sans-serif;">
      {stats_para}
    </p>
  </td>
</tr>
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:32px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:8px;overflow:hidden;
                  box-shadow:0 2px 8px rgba(0,0,0,0.08);">

      <!-- Header -->
      <tr>
        <td style="background:#16213e;padding:28px 40px;">
          <p style="margin:0;font-size:22px;font-weight:bold;color:#ffffff;
                    font-family:sans-serif;letter-spacing:0.5px;">
            Tandem Market Update
          </p>
          <p style="margin:6px 0 0 0;font-size:14px;color:#a0aec0;font-family:sans-serif;">
            {month_year}
          </p>
        </td>
      </tr>

      <!-- Intro -->
      <tr>
        <td style="padding:24px 40px 8px 40px;">
          <p style="margin:0;font-size:15px;line-height:1.7;color:#444;font-family:sans-serif;">
            Hi — each month our team shares a quick look at what's been happening on
            the Tandem platform. No fluff, just real highlights: deals we closed,
            tenants we're working with, and spaces getting traction. We think the best
            way to earn your trust is to show you the work.
          </p>
        </td>
      </tr>

      {city_sections}

      <!-- Divider -->
      <tr>
        <td style="padding:8px 40px;">
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:16px 0;">
        </td>
      </tr>

      <!-- CTA -->
      <tr>
        <td style="padding:8px 40px 32px 40px;">
          <p style="margin:0 0 16px 0;font-size:15px;line-height:1.7;color:#222;
                    font-family:sans-serif;">
            If any of this resonates — or if you have a space that might be a good fit
            for our tenant pipeline — Allegra (NYC) or Pete (SF) would love to connect.
            Even a 15-minute call goes a long way. No pressure, just a conversation.
          </p>
          <a href="https://calendly.com/tandem-space"
             style="display:inline-block;background:#16213e;color:#ffffff;
                    text-decoration:none;padding:12px 28px;border-radius:6px;
                    font-size:14px;font-weight:bold;font-family:sans-serif;">
            Schedule a Catch-Up →
          </a>
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="background:#f7f8fa;padding:16px 40px;border-top:1px solid #e2e8f0;">
          <p style="margin:0;font-size:12px;color:#999;font-family:sans-serif;">
            Tandem · tandem.space · You're receiving this because you're part of our
            landlord &amp; broker network.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""


def build_html(month_year, city_data_list):
    sections_html = ""
    for city, data in city_data_list:
        color = CITY_COLORS.get(city["name"], "#16213e")
        deal_para  = build_deal_paragraph(city["name"], data["deal"])
        stats_para = build_stats_paragraph(city["name"], data)
        sections_html += SECTION_TEMPLATE.format(
            city_short=city["short"],
            color=color,
            contact_name=city["contact_name"],
            contact_title=city["contact_title"],
            deal_para=deal_para,
            stats_para=stats_para,
        )
    return HTML_TEMPLATE.format(month_year=month_year, city_sections=sections_html)

# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = RECIPIENT

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, RECIPIENT, msg.as_string())

    print(f"Email sent to {RECIPIENT}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.now()
    # Report is for the previous calendar month
    if now.month == 1:
        report_month = 12
        report_year  = now.year - 1
    else:
        report_month = now.month - 1
        report_year  = now.year

    month_year = datetime(report_year, report_month, 1).strftime("%B %Y")
    subject    = f"Tandem Market Update — {month_year}"

    print(f"Generating newsletter for {month_year}...")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        city_data_list = []
        for city in CITIES:
            print(f"  Fetching data for {city['name']}...")
            data = fetch_city_data(conn, city)
            city_data_list.append((city, data))
            print(f"    deal={'found' if data['deal'] else 'none'}, "
                  f"tours={data['tours']}, views={data['views']}, "
                  f"listings={data['listings']}")
    finally:
        conn.close()

    html = build_html(month_year, city_data_list)
    send_email(subject, html)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
