import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from dotenv import load_dotenv

load_dotenv()

SENDER = os.getenv("GMAIL_SENDER", "").strip()
APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
APP_NAME = os.getenv("APP_NAME", "Basecamp")

TANDEM_FOOTER = "\n--\nBasecamp is a community workspace by Tandem · tandem.space"


def _send(msg: MIMEMultipart):
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SENDER, APP_PASSWORD)
        server.sendmail(SENDER, msg["To"], msg.as_string())


def send_guest_invitation(registration: dict):
    """Email sent to guest asking them to confirm their visit."""
    confirm_url = f"{BASE_URL}/confirm/{registration['token']}"
    visit_date = registration["visit_date"]

    if hasattr(visit_date, "strftime"):
        visit_date_str = visit_date.strftime("%A, %B %d, %Y")
    else:
        visit_date_str = str(visit_date)

    body = f"""\
Hi,

{registration['tenant_name']} has invited you to visit {APP_NAME}.

Visit details:
  Location:  {registration['location']}
  Date:      {visit_date_str}

To confirm your visit and receive your entry QR code, please click the link below:

{confirm_url}

This link is personal to you — please don't share it.

See you soon,
The {APP_NAME} Team{TANDEM_FOOTER}
"""

    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = registration["guest_email"]
    msg["Subject"] = f"You're invited to {APP_NAME} by {registration['tenant_name']}"
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_guest_qr(registration: dict, qr_bytes: bytes):
    """Email sent to guest after they confirm, with their QR code attached."""
    visit_date = registration.get("visit_date")

    if hasattr(visit_date, "strftime"):
        visit_date_str = visit_date.strftime("%A, %B %d, %Y")
    else:
        visit_date_str = str(visit_date)

    qr_url = f"{BASE_URL}/qr/{registration['token']}"
    guest_name = registration.get("guest_confirmed_name") or registration.get("guest_name") or "there"

    body = f"""\
Hi {guest_name},

Your visit to {APP_NAME} is confirmed. Show the attached QR code to security on arrival.

Visit details:
  Location:  {registration['location']}
  Date:      {visit_date_str}
  Host:      {registration['tenant_name']}

Your QR code is attached to this email. You can also view it at:
{qr_url}

Please have it ready when you arrive.

See you soon,
The {APP_NAME} Team{TANDEM_FOOTER}
"""

    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = registration.get("guest_confirmed_email") or registration["guest_email"]
    msg["Subject"] = f"Your {APP_NAME} visit is confirmed — QR code inside"
    msg.attach(MIMEText(body, "plain"))

    qr_part = MIMEImage(qr_bytes, name="basecamp_entry_qr.png")
    qr_part.add_header("Content-Disposition", "attachment", filename="basecamp_entry_qr.png")
    msg.attach(qr_part)

    _send(msg)


def send_tenant_arrival(registration: dict):
    """Email sent to tenant when their guest checks in."""
    arrived_at = registration.get("arrived_at")
    if hasattr(arrived_at, "strftime"):
        arrived_str = arrived_at.strftime("%I:%M %p")
    else:
        arrived_str = str(arrived_at) if arrived_at else "just now"

    guest_name = registration.get("guest_confirmed_name") or registration.get("guest_name") or "Your guest"
    guest_company = registration.get("guest_confirmed_company") or registration.get("guest_company") or ""
    guest_line = f"{guest_name} from {guest_company}" if guest_company else guest_name

    body = f"""\
Hi {registration['tenant_name']},

{guest_line} has arrived at {APP_NAME} and is on their way up.

Arrival time: {arrived_str}
Location: {registration['location']}

The {APP_NAME} Team{TANDEM_FOOTER}
"""

    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = registration["tenant_email"]
    msg["Subject"] = f"{guest_name} has arrived at {APP_NAME}"
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_walkin_notification(walk_in: dict):
    """Email sent to the host when a walk-in guest arrives."""
    arrived_at = walk_in.get("arrived_at")
    if hasattr(arrived_at, "strftime"):
        arrived_str = arrived_at.strftime("%I:%M %p")
    else:
        arrived_str = "just now"

    body = f"""\
Hi {walk_in['host_name']},

{walk_in['guest_name']} from {walk_in['guest_company']} is waiting at security and needs you to come down to greet them.

Arrival details:
  Location:    {walk_in['location']}
  Time:        {arrived_str}
  Reason:      {walk_in['reason']}
  Guest email: {walk_in['guest_email']}

The {APP_NAME} Team{TANDEM_FOOTER}
"""
    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = walk_in["host_email"]
    msg["Subject"] = f"{walk_in['guest_name']} is here to see you at {APP_NAME}"
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_high_traffic_host_alert(host_name: str, host_email: str, guest_count: int, location: str):
    msg = MIMEMultipart()
    msg["From"] = SENDER
    msg["To"] = "sophie@tandem.space"
    msg["Subject"] = f"High guest volume: {host_name} ({guest_count} guests this week)"
    body = (
        f"{host_name} ({host_email}) has had {guest_count} guests check in "
        f"at {location} in the last 7 days.\n\n"
        f"This may be worth a follow-up.\n\n{TANDEM_FOOTER}"
    )
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_slack_notification(registration: dict):
    """Posts to Slack webhook if configured."""
    import requests

    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    guest_name = registration.get("guest_confirmed_name") or registration.get("guest_name") or "Guest"
    guest_company = registration.get("guest_confirmed_company") or registration.get("guest_company") or ""

    arrived_at = registration.get("arrived_at")
    if hasattr(arrived_at, "strftime"):
        arrived_str = arrived_at.strftime("%I:%M %p")
    else:
        arrived_str = "now"

    guest_line = f"*{guest_name}* from *{guest_company}*" if guest_company else f"*{guest_name}*"
    text = (
        f"{guest_line} has arrived at *{registration['location']}* "
        f"to visit *{registration['tenant_name']}* — {arrived_str}"
    )
    requests.post(webhook_url, json={"text": text}, timeout=5)
