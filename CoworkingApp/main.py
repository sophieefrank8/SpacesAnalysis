"""
CoworkingApp -- internal tool for Tandem team to browse coworking location availability
and for Clarisse to update availability from operator email responses.

Routes:
  GET  /                    -- location browser (requires auth)
  GET  /location/{file}     -- location detail view
  GET  /update              -- Clarisse's email paste form
  POST /update/preview      -- parse email with Gemini, show diff
  POST /update/confirm      -- commit updated MD to GitHub
  GET  /auth/google         -- OAuth start
  GET  /auth/google/callback
  GET  /logout

Env vars:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
  SECRET_KEY
  GEMINI_API_KEY
  GITHUB_TOKEN, GITHUB_REPO (e.g. sophieefrank8/SpacesAnalysis),
  GITHUB_BRANCH (default: main), GITHUB_MD_PATH (default: Coworking/locations)
"""

import base64
import json
import os
import re
import secrets
import time
from datetime import date
from urllib.parse import urlencode

import markdown
import requests
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

SECRET_KEY           = os.getenv("SECRET_KEY", "dev-secret-key-change-in-prod")
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "")
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY", "")
GITHUB_TOKEN         = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO          = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH        = os.getenv("GITHUB_BRANCH", "main")
GITHUB_MD_PATH       = os.getenv("GITHUB_MD_PATH", "Coworking/locations")

app = FastAPI(title="Tandem Coworking")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

_static_dir = "public/static" if os.path.exists("public/static") else "static"
if os.path.exists(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

_github_cache: dict = {}   # filename -> (content_str, sha, fetched_at)
CACHE_TTL = 300            # 5 minutes


def _github_headers(write=False):
    headers = {"Accept": "application/vnd.github.v3+json"}
    # Only add auth token if present; reads work on public repos without it
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    elif write:
        raise ValueError("GITHUB_TOKEN is required for write operations")
    return headers


def github_list_files() -> tuple[list[dict], str]:
    """Return (files, error_msg). files is list of {name, path, sha} for all MD files."""
    if not GITHUB_REPO:
        return [], "GITHUB_REPO env var is not set."
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_MD_PATH}?ref={GITHUB_BRANCH}"
    try:
        r = requests.get(url, headers=_github_headers(), timeout=15)
    except Exception as e:
        return [], f"GitHub API request failed: {e}"
    if r.status_code == 401:
        return [], "GitHub token is invalid or expired. Regenerate it in GitHub → Settings → Developer settings → Personal access tokens."
    if r.status_code == 404:
        return [], f"GitHub path not found: {GITHUB_REPO}/{GITHUB_MD_PATH}. Check GITHUB_REPO and GITHUB_MD_PATH env vars."
    if r.status_code != 200:
        return [], f"GitHub API returned {r.status_code}: {r.text[:200]}"
    files = [f for f in r.json() if isinstance(f, dict) and f.get("name", "").endswith(".md")]
    return files, ""


def github_file_url(filename: str) -> str:
    """Return the GitHub web UI link for a file."""
    return f"https://github.com/{GITHUB_REPO}/blob/{GITHUB_BRANCH}/{GITHUB_MD_PATH}/{filename}"


def github_get_file(filename: str) -> tuple[str, str]:
    """Return (content_str, sha) for a file. Cached."""
    now = time.time()
    cached = _github_cache.get(filename)
    if cached and now - cached[2] < CACHE_TTL:
        return cached[0], cached[1]

    path = f"{GITHUB_MD_PATH}/{filename}"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=_github_headers(), timeout=15)
    if r.status_code != 200:
        return "", ""
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha = data["sha"]
    _github_cache[filename] = (content, sha, now)
    return content, sha


def github_put_file(filename: str, content: str, sha: str, message: str) -> bool:
    """Commit updated content to GitHub. Returns True on success."""
    path = f"{GITHUB_MD_PATH}/{filename}"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }
    r = requests.put(url, headers=_github_headers(write=True), json=payload, timeout=15)
    if r.status_code in (200, 201):
        # Invalidate cache
        _github_cache.pop(filename, None)
        return True
    return False


# ---------------------------------------------------------------------------
# MD parsing helpers
# ---------------------------------------------------------------------------

def parse_md(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body. Returns (frontmatter_dict, body_str)."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            return fm, parts[2].strip()
    return {}, content.strip()


def extract_section(body: str, section: str) -> str:
    """Extract content under a ## Section heading."""
    pattern = rf"## {re.escape(section)}\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, body, re.DOTALL)
    return m.group(1).strip() if m else ""


def replace_section(body: str, section: str, new_content: str) -> str:
    """Replace content under a ## Section heading."""
    pattern = rf"(## {re.escape(section)}\n).*?(?=\n## |\Z)"
    replacement = rf"\g<1>{new_content}\n"
    result = re.sub(pattern, replacement, body, flags=re.DOTALL)
    if result == body:
        # Section not found -- append it
        result = body.rstrip() + f"\n\n## {section}\n{new_content}\n"
    return result


def availability_badge(fm: dict) -> str:
    """Return 'green', 'yellow', or 'red' based on last_updated."""
    last = str(fm.get("last_updated", ""))
    if not last or last == str(date.today()):
        # No updates yet -- check if it's the initial generation date
        return "red"
    try:
        from datetime import datetime
        updated = datetime.strptime(last, "%Y-%m-%d").date()
        days = (date.today() - updated).days
        if days <= 30:
            return "green"
        if days <= 90:
            return "yellow"
    except ValueError:
        pass
    return "red"


# ---------------------------------------------------------------------------
# Gemini email parser
# ---------------------------------------------------------------------------

_gemini_client = None


def gemini_client():
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def parse_email_with_gemini(email_text: str, current_availability: str) -> dict:
    """
    Call Gemini to extract availability updates from pasted email.
    Returns parsed dict with keys: available_suites, general_notes, contact_update, summary.
    """
    client = gemini_client()
    if not client:
        return {"error": "GEMINI_API_KEY not set", "summary": "Could not parse email."}

    prompt = f"""You are parsing an email response from a coworking operator about available office space.

Current availability on file:
{current_availability or "No availability on file."}

Email content:
{email_text}

Extract the following as JSON:
{{
  "available_suites": [
    {{
      "office_type": "string (e.g. Interior Office, 4 seats)",
      "team_size": "string (e.g. '4' or '3-5')",
      "monthly_price": "integer or null",
      "price_note": "string or null (e.g. '12-month term')",
      "touring_link": "URL string or null",
      "available": true,
      "notes": "string or null"
    }}
  ],
  "general_notes": "string or null",
  "contact_update": {{
    "name": "string or null (only if a different contact was mentioned)",
    "email": "string or null"
  }},
  "summary": "1-2 sentence plain English summary of what changed"
}}

If no suites are mentioned, return an empty available_suites array.
Return valid JSON only, no explanation, no markdown fences."""

    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception as e:
        return {"error": str(e), "summary": "Failed to parse email.", "available_suites": []}


def build_availability_markdown(parsed: dict) -> str:
    """Convert Gemini-parsed result into a markdown availability section."""
    today = date.today().isoformat()
    lines = [f"*Last updated: {today}*\n"]

    suites = parsed.get("available_suites", [])
    if suites:
        lines.append("| Office Type | Team Size | Price/mo | Notes | Touring Link |")
        lines.append("|-------------|-----------|----------|-------|--------------|")
        for s in suites:
            price = f"${s['monthly_price']:,}" if s.get("monthly_price") else ""
            if s.get("price_note"):
                price = f"{price} ({s['price_note']})" if price else s["price_note"]
            link = f"[Book a tour]({s['touring_link']})" if s.get("touring_link") else "—"
            lines.append(
                f"| {s.get('office_type','—')} | {s.get('team_size','—')} | "
                f"{price or '—'} | {s.get('notes','') or '—'} | {link} |"
            )
    else:
        lines.append("No availability confirmed in this response.")

    if parsed.get("general_notes"):
        lines.append(f"\n*Note: {parsed['general_notes']}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user(request: Request) -> dict | None:
    name = request.session.get("user_name")
    return {"name": name} if name else None


def require_auth(request: Request):
    """Returns None if authenticated, else a RedirectResponse to login."""
    if not current_user(request):
        return RedirectResponse(f"/?next={request.url.path}")
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", next: str = "/locations"):
    if current_user(request):
        return RedirectResponse("/locations")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "next": next,
        "google_enabled": bool(GOOGLE_CLIENT_ID),
    })


@app.get("/auth/google", response_class=RedirectResponse)
def auth_google(request: Request, next: str = "/locations"):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    request.session["oauth_next"]  = next
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "scope":         "openid email profile",
        "response_type": "code",
        "state":         state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/auth?" + urlencode(params))


@app.get("/auth/google/callback", response_class=RedirectResponse)
def auth_google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/?error=cancelled")

    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        return RedirectResponse("/?error=invalid_state")

    token_resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code":          code,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=10)
    access_token = token_resp.json().get("access_token")
    if not access_token:
        return RedirectResponse("/?error=token_failed")

    user_info = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    ).json()

    email = user_info.get("email", "")
    if not email.lower().endswith("@tandem.space"):
        return RedirectResponse("/?error=unauthorized")

    request.session["user_name"]  = user_info.get("name", email)
    request.session["user_email"] = email
    next_url = request.session.pop("oauth_next", "/locations")
    return RedirectResponse(next_url, status_code=303)


@app.get("/debug", response_class=HTMLResponse)
def debug(request: Request):
    """Temporary debug route -- shows env var status and GitHub API test."""
    token_set = bool(GITHUB_TOKEN)
    token_preview = (GITHUB_TOKEN[:6] + "..." + GITHUB_TOKEN[-4:]) if len(GITHUB_TOKEN) > 10 else ("(empty)" if not GITHUB_TOKEN else "(short)")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_MD_PATH}?ref={GITHUB_BRANCH}"
    try:
        r = requests.get(url, headers=_github_headers(), timeout=10)
        api_status = r.status_code
        api_body = r.text[:300]
    except Exception as e:
        api_status = "error"
        api_body = str(e)
    html = f"""<pre>
GITHUB_REPO:    '{GITHUB_REPO}'
GITHUB_BRANCH:  '{GITHUB_BRANCH}'
GITHUB_MD_PATH: '{GITHUB_MD_PATH}'
GITHUB_TOKEN:   {'SET: ' + token_preview if token_set else 'NOT SET'}

API URL: {url}
API status: {api_status}
API response (first 300 chars):
{api_body}
</pre>"""
    return HTMLResponse(html)


@app.get("/logout", response_class=RedirectResponse)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


# ---------------------------------------------------------------------------
# Location browser
# ---------------------------------------------------------------------------

@app.get("/locations", response_class=HTMLResponse)
def locations_list(
    request: Request,
    operator: str = "",
    market: str = "",
    availability: str = "",
):
    redirect = require_auth(request)
    if redirect:
        return redirect

    files, github_error = github_list_files()

    locations = []
    for f in files:
        content, sha = github_get_file(f["name"])
        fm, _ = parse_md(content)
        badge = availability_badge(fm)
        locations.append({
            "filename":      f["name"],
            "operator":      fm.get("operator", ""),
            "market":        fm.get("market", ""),
            "location_name": fm.get("location_name", f["name"]),
            "address":       fm.get("address", ""),
            "tandem_listing":fm.get("tandem_listing", "") or "",
            "last_updated":  str(fm.get("last_updated", "")),
            "badge":         badge,
            "github_url":    github_file_url(f["name"]),
        })

    # Apply filters
    if operator:
        locations = [l for l in locations if l["operator"] == operator]
    if market:
        locations = [l for l in locations if l["market"] == market]
    if availability == "has_data":
        locations = [l for l in locations if l["badge"] in ("green", "yellow")]
    elif availability == "no_data":
        locations = [l for l in locations if l["badge"] == "red"]

    locations.sort(key=lambda l: (l["market"], l["operator"], l["location_name"]))

    all_operators = sorted({l["operator"] for l in locations} | {"WeWork", "Industrious", "Spaces"})
    all_markets   = sorted({l["market"] for l in locations} | {"SF", "NYC", "Boston"})

    return templates.TemplateResponse("index.html", {
        "request":        request,
        "user":           current_user(request),
        "locations":      locations,
        "filter_operator": operator,
        "filter_market":   market,
        "filter_avail":    availability,
        "all_operators":  all_operators,
        "all_markets":    all_markets,
        "total":          len(locations),
        "github_error":   github_error,
    })


# ---------------------------------------------------------------------------
# Location detail
# ---------------------------------------------------------------------------

@app.get("/location/{filename}", response_class=HTMLResponse)
def location_detail(request: Request, filename: str):
    redirect = require_auth(request)
    if redirect:
        return redirect

    content, _ = github_get_file(filename)
    if not content:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    fm, body = parse_md(content)
    body_html = markdown.markdown(body, extensions=["tables", "fenced_code"])

    return templates.TemplateResponse("location.html", {
        "request":   request,
        "user":      current_user(request),
        "filename":  filename,
        "fm":        fm,
        "body_html": body_html,
        "badge":     availability_badge(fm),
    })


# ---------------------------------------------------------------------------
# Update flow
# ---------------------------------------------------------------------------

@app.get("/update", response_class=HTMLResponse)
def update_form(request: Request, filename: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect

    files, _ = github_list_files()
    location_options = []
    for f in files:
        content, _ = github_get_file(f["name"])
        fm, _ = parse_md(content)
        addr = (fm.get("address", "") or "").split(",")[0].strip()
        label = f"{fm.get('operator','')} | {fm.get('location_name', '')} — {addr} ({fm.get('market','')})"
        location_options.append({
            "filename": f["name"],
            "label":    label,
            "market":   fm.get("market", ""),
        })
    location_options.sort(key=lambda x: (x["market"], x["label"]))

    return templates.TemplateResponse("update.html", {
        "request":          request,
        "user":             current_user(request),
        "locations":        location_options,
        "selected":         filename,
    })


@app.post("/update/preview", response_class=HTMLResponse)
def update_preview(
    request: Request,
    filename: str = Form(...),
    email_text: str = Form(...),
):
    redirect = require_auth(request)
    if redirect:
        return redirect

    content, sha = github_get_file(filename)
    if not content:
        return HTMLResponse("<h1>File not found</h1>", status_code=404)

    fm, body = parse_md(content)
    current_avail = extract_section(body, "Current Availability")

    parsed = parse_email_with_gemini(email_text, current_avail)
    new_avail_md = build_availability_markdown(parsed)

    # Store in session for confirm step
    request.session["pending_update"] = {
        "filename":        filename,
        "sha":             sha,
        "parsed":          parsed,
        "new_avail_md":    new_avail_md,
        "current_avail":   current_avail,
        "email_snippet":   email_text[:300],
    }

    return templates.TemplateResponse("preview.html", {
        "request":       request,
        "user":          current_user(request),
        "filename":      filename,
        "location_name": fm.get("location_name", filename),
        "market":        fm.get("market", ""),
        "operator":      fm.get("operator", ""),
        "summary":       parsed.get("summary", ""),
        "current_avail": current_avail or "*No availability on file.*",
        "new_avail":     new_avail_md,
        "suites":        parsed.get("available_suites", []),
        "error":         parsed.get("error", ""),
    })


@app.post("/update/confirm", response_class=RedirectResponse)
def update_confirm(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect

    pending = request.session.pop("pending_update", None)
    if not pending:
        return RedirectResponse("/update?error=session_expired", status_code=303)

    filename      = pending["filename"]
    sha           = pending["sha"]
    new_avail_md  = pending["new_avail_md"]
    summary       = pending.get("parsed", {}).get("summary", "Availability update")
    email_snippet = pending.get("email_snippet", "")
    user          = current_user(request)

    # Re-fetch latest content (in case of concurrent edit)
    content, current_sha = github_get_file(filename)
    fm, body = parse_md(content)

    # Update availability section
    new_body = replace_section(body, "Current Availability", new_avail_md)

    # Append outreach history row
    today = date.today().isoformat()
    history_row = f"| {today} | Inbound | {summary} |"
    new_body = replace_section(
        new_body,
        "Outreach History",
        extract_section(new_body, "Outreach History").rstrip() + "\n" + history_row,
    )

    # Update frontmatter last_updated
    fm["last_updated"] = today

    # Rebuild full MD
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
    new_content = f"---\n{fm_yaml}\n---\n\n{new_body}"

    user_name = user["name"] if user else "Tandem"
    commit_msg = f"Update availability: {fm.get('location_name', filename)} ({today}) [{user_name}]"

    ok = github_put_file(filename, new_content, current_sha, commit_msg)
    if not ok:
        return RedirectResponse(f"/update?error=github_failed", status_code=303)

    return RedirectResponse(f"/location/{filename}", status_code=303)
