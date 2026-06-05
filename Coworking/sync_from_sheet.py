"""
Sync Tandem listing links from the Google Sheet into the MD location files on GitHub.

The team fills in "Listing Page Link" in:
  https://docs.google.com/spreadsheets/d/1CtCzMLXI5SPP--bxJ9ssvvzOOmVQfc0EG3_rm30utUg

This script reads that sheet, matches rows to MD files by operator+market+location_name,
and commits any new or updated tandem_listing values to GitHub.

Usage:
    python sync_from_sheet.py
    python sync_from_sheet.py --dry-run   # print matches without committing

Requires env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON  -- path to service account JSON, OR
    GOOGLE_API_KEY               -- simple API key (read-only public sheets)
    GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH, GITHUB_MD_PATH

OR run manually: python sync_from_sheet.py --csv path/to/export.csv
  (export the sheet as CSV from File > Download > CSV)
"""

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

SHEET_ID       = "1CtCzMLXI5SPP--bxJ9ssvvzOOmVQfc0EG3_rm30utUg"
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO    = os.getenv("GITHUB_REPO", "sophieefrank8/SpacesAnalysis")
GITHUB_BRANCH  = os.getenv("GITHUB_BRANCH", "main")
GITHUB_MD_PATH = os.getenv("GITHUB_MD_PATH", "Coworking/locations")


# ---------------------------------------------------------------------------
# Google Sheets reader (CSV export -- no auth required for shared sheets)
# ---------------------------------------------------------------------------

def fetch_sheet_as_csv() -> list[dict]:
    """Download the sheet as CSV. Works for sheets shared as 'Anyone with link can view'."""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=2001046772"
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        sys.exit(f"Could not download sheet: {r.status_code}. Make sure the sheet is shared as 'Anyone with link can view'.")
    reader = csv.DictReader(r.text.splitlines())
    return list(reader)


def load_csv_file(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def list_md_files() -> list[dict]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_MD_PATH}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=gh_headers(), timeout=15)
    if r.status_code != 200:
        sys.exit(f"GitHub API error {r.status_code}: {r.text[:200]}")
    return [f for f in r.json() if f.get("name", "").endswith(".md")]


def get_md_file(filename: str) -> tuple[str, str]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_MD_PATH}/{filename}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=gh_headers(), timeout=15)
    if r.status_code != 200:
        return "", ""
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def put_md_file(filename: str, content: str, sha: str, message: str) -> bool:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_MD_PATH}/{filename}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }
    r = requests.put(url, headers=gh_headers(), json=payload, timeout=15)
    return r.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def parse_md(content: str) -> tuple[dict, str]:
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            return fm, parts[2].strip()
    return {}, content.strip()


def rebuild_md(fm: dict, body: str) -> str:
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{fm_yaml}\n---\n\n{body}"


def normalize(text: str) -> str:
    """Lowercase, strip punctuation for fuzzy matching."""
    return re.sub(r"[^\w\s]", "", str(text).lower()).strip()


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run(dry_run: bool, csv_path: str | None = None):
    # 1. Load sheet rows
    if csv_path:
        rows = load_csv_file(csv_path)
        print(f"Loaded {len(rows)} rows from {csv_path}")
    else:
        rows = fetch_sheet_as_csv()
        print(f"Loaded {len(rows)} rows from Google Sheet")

    # Filter to rows that have a Listing Page Link
    # Column name may vary -- try a few
    listing_col = None
    if rows:
        for col in rows[0].keys():
            if "listing" in col.lower() and "page" in col.lower():
                listing_col = col
                break
        if not listing_col:
            # Fall back to last column
            listing_col = list(rows[0].keys())[-1]

    print(f"Using listing column: '{listing_col}'")

    filled_rows = [r for r in rows if r.get(listing_col, "").strip().startswith("http")]
    print(f"Rows with Tandem listing URL: {len(filled_rows)}")
    if not filled_rows:
        print("Nothing to sync.")
        return

    # 2. Load all MD files
    print("\nFetching MD file list from GitHub...")
    md_files = list_md_files()
    print(f"Found {len(md_files)} MD files")

    # Build lookup: (normalized_operator, normalized_market, normalized_name) -> filename
    md_lookup: dict[tuple, str] = {}
    for f in md_files:
        content, _ = get_md_file(f["name"])
        fm, _ = parse_md(content)
        key = (
            normalize(fm.get("operator", "")),
            normalize(fm.get("market", "")),
            normalize(fm.get("location_name", "")),
        )
        md_lookup[key] = f["name"]
        time.sleep(0.05)  # avoid rate limit

    # 3. Match and update
    updated = skipped = unmatched = 0
    for row in filled_rows:
        listing_url = row.get(listing_col, "").strip()
        op    = row.get("Operator", "") or row.get("operator", "")
        mkt   = row.get("Market", "") or row.get("market", "")
        name  = row.get("Location Name", "") or row.get("location_name", "")

        key = (normalize(op), normalize(mkt), normalize(name))
        filename = md_lookup.get(key)

        if not filename:
            print(f"  UNMATCHED: {op} | {name} ({mkt})")
            unmatched += 1
            continue

        content, sha = get_md_file(filename)
        fm, body = parse_md(content)
        current = str(fm.get("tandem_listing", "") or "").strip()

        if current == listing_url:
            skipped += 1
            continue

        if dry_run:
            print(f"  WOULD UPDATE: {filename}")
            print(f"    tandem_listing: {current or '(empty)'} → {listing_url}")
            updated += 1
            continue

        fm["tandem_listing"] = listing_url
        new_content = rebuild_md(fm, body)
        ok = put_md_file(filename, new_content, sha, f"Sync Tandem listing: {name} ({mkt})")
        if ok:
            print(f"  UPDATED: {filename} → {listing_url}")
            updated += 1
        else:
            print(f"  FAILED: {filename}")
        time.sleep(0.3)

    print(f"\nDone. Updated: {updated}, Skipped (no change): {skipped}, Unmatched: {unmatched}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--csv", help="Path to manually exported CSV instead of fetching from Google")
    args = parser.parse_args()
    run(dry_run=args.dry_run, csv_path=args.csv)
