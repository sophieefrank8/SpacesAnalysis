import hashlib
import hmac
import json as _json
import os
import secrets
import time
import requests
from urllib.parse import quote, urlencode, parse_qs
from typing import List

from fastapi import FastAPI, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

import database
import slack

load_dotenv()

SECRET_KEY          = os.getenv("SECRET_KEY", "dev-secret-key-change-in-prod")
GOOGLE_CLIENT_ID    = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
SLACK_BOT_TOKEN     = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

app = FastAPI(title="Tandem Field App")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

_static_dir = "public/static" if os.path.exists("public/static") else "static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
templates = Jinja2Templates(directory="templates")


def _user(request: Request) -> dict | None:
    name = request.session.get("user_name")
    if name:
        return {"name": name, "id": request.session.get("user_id", "")}
    return None


def _space_status_label(space: dict) -> str:
    if space.get("status") == "PUBLISHED" and space.get("isSearchable"):
        return "live"
    if space.get("status") == "PUBLISHED":
        return "unlisted"
    if space.get("status") == "IN_REVIEW":
        return "review"
    return "draft"


templates.env.globals["space_status_label"] = _space_status_label


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def identity_page(request: Request, error: str = ""):
    if _user(request):
        return RedirectResponse("/search")
    users = database.get_users()
    return templates.TemplateResponse("identity.html", {
        "request": request,
        "users": users,
        "google_oauth_enabled": bool(GOOGLE_CLIENT_ID),
        "error": error,
    })


@app.post("/identity", response_class=RedirectResponse)
def identity_submit(
    request: Request,
    user_name: str = Form(...),
    user_id: str = Form(...),
):
    request.session["user_name"] = user_name
    request.session["user_id"] = user_id
    return RedirectResponse("/search", status_code=303)


@app.get("/auth/google", response_class=RedirectResponse)
def auth_google(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "scope": "openid email profile",
        "response_type": "code",
        "state": state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/auth?" + urlencode(params))


@app.get("/auth/google/callback", response_class=RedirectResponse)
def auth_google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    if error or not code:
        return RedirectResponse("/?error=cancelled")

    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse("/?error=invalid_state")

    token_resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=10)
    token_data = token_resp.json()
    access_token = token_data.get("access_token")

    if not access_token:
        return RedirectResponse("/?error=token_failed")

    user_resp = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    user_info = user_resp.json()
    email = user_info.get("email", "")
    name = user_info.get("name", "")
    sub = user_info.get("sub", "")

    if not email.lower().endswith("@tandem.space"):
        return RedirectResponse("/?error=unauthorized")

    request.session["user_name"] = name
    request.session["user_id"] = sub
    return RedirectResponse("/search", status_code=303)


@app.get("/logout", response_class=RedirectResponse)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/api/autocomplete", response_class=JSONResponse)
def autocomplete(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return []
    results = database.search_addresses(q)
    return [
        {
            "label": r["addressLine1"],
            "sublabel": ", ".join(p for p in [r.get("city", ""), r.get("state", "")] if p),
            "space_count": r.get("space_count", 0),
            "live_count": r.get("live_count", 0),
        }
        for r in results
    ]


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/")
    return templates.TemplateResponse("search.html", {"request": request, "user": user})


@app.post("/search", response_class=RedirectResponse)
def search_submit(query: str = Form(...)):
    return RedirectResponse(f"/building?address={quote(query.strip())}", status_code=303)


@app.get("/building", response_class=HTMLResponse)
def building_page(request: Request, address: str = ""):
    user = _user(request)
    if not user:
        return RedirectResponse("/")
    if not address.strip():
        return RedirectResponse("/search")
    spaces = database.get_spaces_at_building(address)
    return templates.TemplateResponse("building.html", {
        "request": request,
        "user": user,
        "address": address,
        "spaces": spaces,
    })


@app.get("/space/{space_id}", response_class=HTMLResponse)
def space_detail(request: Request, space_id: str):
    user = _user(request)
    if not user:
        return RedirectResponse("/")
    space, contacts = database.get_space_detail(space_id)
    if not space:
        return RedirectResponse("/search")
    return templates.TemplateResponse("space.html", {
        "request": request,
        "user": user,
        "space": space,
        "contacts": contacts,
    })


@app.get("/request/new", response_class=HTMLResponse)
def request_new(request: Request, address: str = "", new_building: str = "", full_address: str = ""):
    user = _user(request)
    if not user:
        return RedirectResponse("/")
    return templates.TemplateResponse("request.html", {
        "request": request,
        "user": user,
        "space": None,
        "address": address,
        "intent": "NEW",
        "new_building": bool(new_building),
        "full_address": full_address,
    })


@app.get("/request/{space_id}", response_class=HTMLResponse)
def request_update(request: Request, space_id: str):
    user = _user(request)
    if not user:
        return RedirectResponse("/")
    space, _ = database.get_space_detail(space_id)
    if not space:
        return RedirectResponse("/search")
    return templates.TemplateResponse("request.html", {
        "request": request,
        "user": user,
        "space": space,
        "address": space["addressLine1"],
        "intent": "UPDATE",
        "new_building": False,
        "full_address": "",
    })



@app.post("/submit", response_class=HTMLResponse)
async def submit_request(
    request: Request,
    intent: str = Form(...),
    space_id: str = Form(""),
    address: str = Form(...),
    unit: str = Form(""),
    sq_footage: str = Form(""),
    min_desks: str = Form(""),
    max_desks: str = Form(""),
    price_per_desk: str = Form(""),
    total_price: str = Form(""),
    term_months: str = Form(""),
    term_note: str = Form(""),
    listing_status: str = Form(""),
    should_publish: str = Form(""),
    price_note: str = Form(""),
    notes: str = Form(""),
    is_new_building: str = Form(""),
    full_address: str = Form(""),
    contact_name: List[str] = Form(default=[]),
    contact_email: List[str] = Form(default=[]),
    contact_phone: List[str] = Form(default=[]),
    contact_role: List[str] = Form(default=[]),
    photos: List[UploadFile] = File(default=[]),
    flyers: List[UploadFile] = File(default=[]),
):
    user = _user(request)
    if not user:
        return RedirectResponse("/")

    contacts = []
    for i, name in enumerate(contact_name):
        if name.strip():
            contacts.append({
                "name": name,
                "email": contact_email[i] if i < len(contact_email) else "",
                "phone": contact_phone[i] if i < len(contact_phone) else "",
                "role": contact_role[i] if i < len(contact_role) else "",
            })

    data = {
        "intent": intent,
        "space_id": space_id,
        "address": address,
        "unit": unit,
        "sq_footage": sq_footage,
        "min_desks": min_desks,
        "max_desks": max_desks,
        "price_per_desk": price_per_desk,
        "total_price": total_price,
        "term_months": term_months,
        "term_note": term_note,
        "listing_status": listing_status,
        "should_publish": should_publish,
        "price_note": price_note,
        "notes": notes,
        "contacts": contacts,
        "user_name": user["name"],
        "is_new_building": is_new_building == "yes",
        "full_address": full_address.strip(),
    }

    photo_files = []
    for photo in photos:
        if photo.filename:
            content = await photo.read()
            if content:
                photo_files.append({
                    "filename": photo.filename,
                    "content": content,
                    "content_type": photo.content_type or "image/jpeg",
                })

    flyer_files = []
    for flyer in flyers:
        if flyer.filename:
            content = await flyer.read()
            if content:
                flyer_files.append({
                    "filename": flyer.filename,
                    "content": content,
                    "content_type": flyer.content_type or "application/octet-stream",
                })

    slack_error = None
    try:
        slack.send_request(data, photo_files, flyer_files)
    except Exception as e:
        slack_error = str(e)
        print(f"[slack error]: {e}")

    return templates.TemplateResponse("confirm.html", {
        "request": request,
        "user": user,
        "data": data,
        "slack_error": slack_error,
    })


# ── Slack interactive callback ────────────────────────────────────────────────
# Called by Slack when Clarisse clicks "Available" or "Not Available" buttons.
# Set Request URL in Slack app settings → Interactivity & Shortcuts.

@app.post("/slack/interactive")
async def slack_interactive(request: Request):
    body_bytes = await request.body()
    body_str   = body_bytes.decode("utf-8")

    # Verify Slack signing secret (prevents spoofed callbacks)
    ts  = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not ts or abs(time.time() - float(ts)) > 300:
        raise HTTPException(status_code=403, detail="Stale request")
    if SLACK_SIGNING_SECRET:
        basestring = f"v0:{ts}:{body_str}"
        expected = "v0=" + hmac.new(
            SLACK_SIGNING_SECRET.encode(), basestring.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(status_code=403, detail="Invalid signature")

    parsed  = parse_qs(body_str)
    payload = _json.loads(parsed.get("payload", ["{}"])[0])

    if payload.get("type") != "block_actions":
        return JSONResponse({"ok": True})

    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse({"ok": True})

    action     = actions[0]
    action_id  = action.get("action_id", "")
    channel    = payload.get("channel", {}).get("id", "")
    thread_ts  = payload.get("message", {}).get("ts", "")
    headers    = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}

    if action_id == "outreach_available":
        parts          = action.get("value", "||").split("|", 2)
        outreach_id    = parts[0] if len(parts) > 0 else ""
        supply_id      = parts[1] if len(parts) > 1 else ""
        supply_name    = parts[2] if len(parts) > 2 else "the supply rep"

        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={
                "channel":   channel,
                "thread_ts": thread_ts,
                "text": f"<@{supply_id}> — research complete. Space confirmed available. Please reach out.",
            },
            timeout=5,
        )
        if outreach_id:
            try:
                database.update_outreach_status(outreach_id, "IN_REVIEW")
            except Exception as e:
                print(f"[slack/interactive] DB update failed: {e}")

    elif action_id == "outreach_not_available":
        outreach_id = action.get("value", "")
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={
                "channel":   channel,
                "thread_ts": thread_ts,
                "text":      "Marked as not available. Removed from queue.",
            },
            timeout=5,
        )
        if outreach_id:
            try:
                database.update_outreach_status(outreach_id, "RESOLVED")
            except Exception as e:
                print(f"[slack/interactive] DB update failed: {e}")

    return JSONResponse({"ok": True})