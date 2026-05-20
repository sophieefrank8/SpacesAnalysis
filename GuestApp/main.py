import os
import secrets
import base64
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import load_dotenv

import database
from emails import send_guest_invitation, send_guest_qr, send_tenant_arrival, send_slack_notification, send_walkin_notification, send_high_traffic_host_alert
from qr_utils import generate_qr_base64, generate_qr_bytes

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

LOCATIONS = [
    "128 King St, San Francisco",
    "989 Market St, San Francisco",
]

app = FastAPI(title="Basecamp Guest Registration")
_static_dir = "public/static" if os.path.exists("public/static") else "static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def require_admin(credentials: Annotated[HTTPBasicCredentials, Depends(security)]):
    ok_user = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def fmt_date(d) -> str:
    if hasattr(d, "strftime"):
        return d.strftime("%B %d, %Y").replace(" 0", " ")
    return str(d) if d else ""


def fmt_time(t) -> str:
    if hasattr(t, "strftime"):
        return t.strftime("%I:%M %p").lstrip("0")
    return str(t) if t else ""


def fmt_datetime(dt) -> str:
    if hasattr(dt, "strftime"):
        return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    return str(dt) if dt else "—"


templates.env.filters["fmt_date"] = fmt_date
templates.env.filters["fmt_time"] = fmt_time
templates.env.filters["fmt_datetime"] = fmt_datetime


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=RedirectResponse)
def root(request: Request):
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if "basecamp-sf.com" in host and "www" not in host:
        return RedirectResponse(
            "https://tandemspace.com/offices/san-francisco/south-beach/128-king-st-992b552",
            status_code=307,
        )
    return RedirectResponse("/register")


# --- Tenant Registration ---

@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request,
        "locations": LOCATIONS,
    })


@app.post("/register", response_class=RedirectResponse)
def register_submit(
    request: Request,
    tenant_name: str = Form(...),
    tenant_email: str = Form(...),
    guest_email: str = Form(...),
    location: str = Form(...),
    visit_date: str = Form(...),
    reason: str = Form(...),
):
    if location not in LOCATIONS:
        raise HTTPException(status_code=400, detail="Invalid location")

    reg = database.create_registration(
        tenant_name=tenant_name,
        tenant_email=tenant_email,
        guest_name="",
        guest_email=guest_email,
        location=location,
        visit_date=visit_date,
        reason=reason,
    )

    try:
        send_guest_invitation(reg)
    except Exception as e:
        print(f"[email error] invitation: {e}")

    return RedirectResponse(f"/register/success/{reg['token']}", status_code=303)


@app.get("/register/success/{token}", response_class=HTMLResponse)
def register_success(request: Request, token: str):
    reg = database.get_registration(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse("register_success.html", {
        "request": request,
        "reg": reg,
    })


# --- Guest Confirmation ---

@app.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_form(request: Request, token: str):
    reg = database.get_registration(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if reg["status"] != "pending":
        return templates.TemplateResponse("already_confirmed.html", {
            "request": request,
            "reg": reg,
        })
    return templates.TemplateResponse("guest_confirm.html", {
        "request": request,
        "reg": reg,
        "fmt_date": fmt_date,
        "fmt_time": fmt_time,
    })


@app.post("/confirm/{token}", response_class=HTMLResponse)
def confirm_submit(
    request: Request,
    token: str,
    confirmed_name: str = Form(...),
    confirmed_company: str = Form(...),
    confirmed_email: str = Form(...),
    office_setup: str = Form(...),
):
    reg = database.confirm_registration(
        token=token,
        confirmed_name=confirmed_name,
        confirmed_company=confirmed_company,
        confirmed_email=confirmed_email,
        office_setup=office_setup,
    )
    if not reg:
        raise HTTPException(status_code=409, detail="Registration already confirmed or not found")

    verify_url = f"{BASE_URL}/verify/{token}"
    qr_b64 = generate_qr_base64(verify_url)
    qr_raw = generate_qr_bytes(verify_url)

    try:
        send_guest_qr(reg, qr_raw)
    except Exception as e:
        print(f"[email error] QR: {e}")

    return templates.TemplateResponse("guest_qr.html", {
        "request": request,
        "reg": reg,
        "qr_b64": qr_b64,
        "fmt_date": fmt_date,
        "fmt_time": fmt_time,
    })


# --- QR Code page (guest can return to this anytime) ---

@app.get("/qr/{token}", response_class=HTMLResponse)
def qr_page(request: Request, token: str):
    reg = database.get_registration(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Not found")

    verify_url = f"{BASE_URL}/verify/{token}"
    qr_b64 = generate_qr_base64(verify_url)

    return templates.TemplateResponse("guest_qr.html", {
        "request": request,
        "reg": reg,
        "qr_b64": qr_b64,
        "fmt_date": fmt_date,
        "fmt_time": fmt_time,
    })


# --- Security Guard Check-in ---

@app.get("/verify/{token}", response_class=HTMLResponse)
def verify_page(request: Request, token: str):
    reg = database.get_registration(token)
    if not reg:
        return templates.TemplateResponse("verify_error.html", {
            "request": request,
            "message": "This QR code is not recognized.",
        })

    return templates.TemplateResponse("scan.html", {
        "request": request,
        "reg": reg,
        "fmt_date": fmt_date,
        "fmt_time": fmt_time,
    })


@app.post("/verify/{token}", response_class=HTMLResponse)
def verify_checkin(request: Request, token: str):
    reg = database.checkin_registration(token)

    if not reg:
        # Already arrived or not confirmed — fetch current state to show
        reg = database.get_registration(token)
        return templates.TemplateResponse("scan.html", {
            "request": request,
            "reg": reg,
            "fmt_date": fmt_date,
            "fmt_time": fmt_time,
            "already_checked_in": True,
        })

    try:
        send_tenant_arrival(reg)
    except Exception as e:
        print(f"[email error] arrival: {e}")

    try:
        send_slack_notification(reg)
    except Exception as e:
        print(f"[slack error]: {e}")

    try:
        count = database.get_host_guest_count_this_week(reg["tenant_email"])
        if count > 3:
            send_high_traffic_host_alert(reg["tenant_name"], reg["tenant_email"], count, reg["location"])
    except Exception as e:
        print(f"[email error] high-traffic alert: {e}")

    return templates.TemplateResponse("checkin_success.html", {
        "request": request,
        "reg": reg,
        "fmt_date": fmt_date,
        "fmt_time": fmt_time,
    })


# --- Admin ---

@app.get("/admin", response_class=HTMLResponse)
def admin_view(
    request: Request,
    location: str = "",
    status: str = "",
    date: str = "",
    _: str = Depends(require_admin),
):
    rows = database.get_all_registrations(
        location_filter=location,
        status_filter=status,
        date_filter=date,
    )
    walk_ins = database.get_all_walk_ins(
        location_filter=location,
        date_filter=date,
    )
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "rows": rows,
        "walk_ins": walk_ins,
        "locations": LOCATIONS,
        "filter_location": location,
        "filter_status": status,
        "filter_date": date,
        "fmt_date": fmt_date,
        "fmt_time": fmt_time,
        "fmt_datetime": fmt_datetime,
    })


# --- Guest Policy ---

@app.get("/policy", response_class=HTMLResponse)
def policy_page(request: Request):
    return templates.TemplateResponse("policy.html", {"request": request})


# --- Walk-in Registration ---

LOCATION_SLUGS = {
    "128-king-st": "128 King St, San Francisco",
    "989-market-st": "989 Market St, San Francisco",
}


@app.get("/walkin", response_class=HTMLResponse)
def walkin_form(request: Request, location: str = ""):
    resolved = LOCATION_SLUGS.get(location, "")
    return templates.TemplateResponse("walkin.html", {
        "request": request,
        "location": resolved,
        "locations": LOCATIONS,
    })


@app.post("/walkin", response_class=HTMLResponse)
def walkin_submit(
    request: Request,
    guest_name: str = Form(...),
    guest_email: str = Form(...),
    guest_company: str = Form(...),
    host_name: str = Form(...),
    host_email: str = Form(...),
    location: str = Form(...),
    reason: str = Form(...),
):
    if location not in LOCATIONS:
        raise HTTPException(status_code=400, detail="Invalid location")

    walk_in = database.create_walk_in(
        guest_name=guest_name,
        guest_email=guest_email,
        guest_company=guest_company,
        host_name=host_name,
        host_email=host_email,
        location=location,
        reason=reason,
    )

    try:
        send_walkin_notification(walk_in)
    except Exception as e:
        print(f"[email error] walk-in: {e}")

    try:
        count = database.get_host_guest_count_this_week(walk_in["host_email"])
        if count > 3:
            send_high_traffic_host_alert(walk_in["host_name"], walk_in["host_email"], count, walk_in["location"])
    except Exception as e:
        print(f"[email error] high-traffic alert: {e}")

    return templates.TemplateResponse("walkin_success.html", {
        "request": request,
        "walk_in": walk_in,
        "fmt_datetime": fmt_datetime,
    })


# --- Printable Location QR Codes (for posting at front desk) ---

@app.get("/location-qr", response_class=HTMLResponse)
def location_qr(_: str = Depends(require_admin), request: Request = None):
    qr_codes = []
    for slug, label in LOCATION_SLUGS.items():
        url = f"{BASE_URL}/walkin?location={slug}"
        qr_codes.append({"label": label, "url": url, "qr_b64": generate_qr_base64(url)})
    return templates.TemplateResponse("location_qr.html", {
        "request": request,
        "qr_codes": qr_codes,
    })
