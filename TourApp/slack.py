import os
import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CLARISSE_USER_ID = os.getenv("SLACK_CLARISSE_USER_ID", "")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "")
TANDEM_BASE = "https://tandem.space/office"


def _fmt_field(label: str, value: str, fallback: str = "—") -> str:
    return f"• *{label}:* {value if value and value.strip() else fallback}"


def _build_message(data: dict, photo_count: int, flyer_count: int) -> str:
    intent = data.get("intent", "NEW")
    user_name = data.get("user_name", "Unknown")
    address = data.get("address", "")
    unit = data.get("unit", "")
    space_id = data.get("space_id", "")
    is_new_building = data.get("is_new_building", False)
    full_address = data.get("full_address", "")

    header_addr = address
    if unit:
        header_addr += f" — {unit}"

    if intent == "UPDATE" and space_id:
        listing_line = f"\n🔗 *Listing:* <{TANDEM_BASE}/{space_id}|View current listing>"
        intent_label = "✏️ *UPDATE LISTING PAGE*"
    elif is_new_building:
        listing_line = ""
        intent_label = "🏗️ *NEW BUILDING — NOT YET IN SYSTEM*"
    else:
        listing_line = ""
        intent_label = "🆕 *NEW LISTING PAGE*"

    geocode_line = f"\n📌 *Verified Address:* {full_address}" if full_address else ""

    # Details
    sq_ft = data.get("sq_footage", "")
    min_desks = data.get("min_desks", "")
    max_desks = data.get("max_desks", "")
    price_per_desk = data.get("price_per_desk", "")
    total_price = data.get("total_price", "")
    term = data.get("term_months", "")
    listing_status = data.get("listing_status", "")
    should_publish = data.get("should_publish", "")

    desks_str = f"{min_desks}–{max_desks}" if min_desks and max_desks else (min_desks or max_desks or "—")
    price_desk_str = f"${price_per_desk}/desk/mo" if price_per_desk else "—"
    total_str = f"${total_price}/mo" if total_price else "—"
    term_str = f"{term} months" if term else "—"
    sq_str = f"{sq_ft} sq ft" if sq_ft else "—"
    price_note = data.get("price_note", "").strip()
    term_note = data.get("term_note", "").strip()

    status_labels = {"DRAFT": "Draft", "IN_REVIEW": "In Review", "PUBLISHED": "Published"}
    status_str = status_labels.get(listing_status, "—")
    searchable_str = "Yes ✅" if should_publish == "yes" else ("No ❌" if should_publish == "no" else "—")

    details_rows = [
        _fmt_field("Sq Ft", sq_str),
        _fmt_field("Desks", desks_str),
        _fmt_field("Price", price_desk_str),
        _fmt_field("Total/mo", total_str),
        *([ f"  ↳ {price_note}"] if price_note else []),
        _fmt_field("Term", term_str),
        *([ f"  ↳ {term_note}"] if term_note else []),
        _fmt_field("Listing Status", status_str),
    ]
    if listing_status == "PUBLISHED":
        details_rows.append(_fmt_field("Searchable", searchable_str))
    details = "\n".join(details_rows)

    # Contacts
    contacts = data.get("contacts", [])
    if contacts:
        contact_lines = []
        for c in contacts:
            parts = [c.get("name", "")]
            if c.get("role"):
                parts.append(c["role"])
            if c.get("email"):
                parts.append(c["email"])
            if c.get("phone"):
                parts.append(c["phone"])
            contact_lines.append("• " + " | ".join(p for p in parts if p))
        contacts_section = "\n*Contacts:*\n" + "\n".join(contact_lines)
    else:
        contacts_section = ""

    # Notes
    notes = data.get("notes", "").strip()
    notes_section = f"\n*Notes:* {notes}" if notes else ""

    # Attachments — files are posted directly to the channel, just show counts
    attachment_parts = []
    if photo_count:
        attachment_parts.append(f"📸 {photo_count} photo{'s' if photo_count != 1 else ''}")
    if flyer_count:
        attachment_parts.append(f"📄 {flyer_count} flyer{'s' if flyer_count != 1 else ''}")
    attachments_section = "\n*Attachments:* " + ", ".join(attachment_parts) if attachment_parts else ""

    msg = (
        f"{intent_label} | {user_name}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Address:* {header_addr}{listing_line}{geocode_line}\n\n"
        f"*Details:*\n{details}"
        f"{contacts_section}"
        f"{notes_section}"
        f"{attachments_section}"
    )
    return msg


def _open_dm_channel() -> str | None:
    if not SLACK_BOT_TOKEN or not SLACK_CLARISSE_USER_ID:
        return None
    try:
        r = requests.post(
            "https://slack.com/api/conversations.open",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"users": SLACK_CLARISSE_USER_ID},
            timeout=10,
        )
        return r.json().get("channel", {}).get("id")
    except Exception as e:
        print(f"[slack] conversations.open error: {e}")
        return None


def _upload_file(file: dict, channel_id: str, caption: str = "") -> bool:
    if not SLACK_BOT_TOKEN:
        return False
    try:
        # Step 1: get upload URL
        r = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            data={"filename": file["filename"], "length": len(file["content"])},
            timeout=10,
        )
        r.raise_for_status()
        resp = r.json()
        if not resp.get("ok"):
            print(f"[slack] getUploadURL error: {resp.get('error')}")
            return False

        upload_url = resp["upload_url"]
        file_id = resp["file_id"]

        # Step 2: upload content
        requests.post(
            upload_url,
            data=file["content"],
            headers={"Content-Type": file.get("content_type", "application/octet-stream")},
            timeout=30,
        ).raise_for_status()

        # Step 3: complete and share directly to channel — file appears inline with preview
        payload: dict = {"files": [{"id": file_id}], "channel_id": channel_id}
        if caption:
            payload["initial_comment"] = caption
        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        resp3 = r3.json()
        if not resp3.get("ok"):
            print(f"[slack] completeUpload error: {resp3.get('error')}")
            return False
        return True
    except Exception as e:
        print(f"[slack] file upload error: {e}")
        return False


def _dm_clarisse(channel_id: str, message: str) -> None:
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": channel_id, "text": message, "mrkdwn": True},
            timeout=10,
        )
    except Exception as e:
        print(f"[slack] DM error: {e}")


def send_request(data: dict, photos: list[dict], flyers: list[dict] | None = None) -> None:
    if not SLACK_WEBHOOK_URL and not SLACK_BOT_TOKEN:
        print("[slack] No credentials configured — printing request:")
        print(data)
        return

    dm_channel_id = _open_dm_channel()
    share_channel = SLACK_CHANNEL_ID or dm_channel_id

    address = data.get("address", "")
    unit = data.get("unit", "")
    caption = f"{address} — {unit}" if unit else address

    photo_count = 0
    flyer_count = 0
    if share_channel:
        photo_count = sum(1 for ph in photos if _upload_file(ph, share_channel, caption=caption))
        flyer_count = sum(1 for f in (flyers or []) if _upload_file(f, share_channel, caption=caption))

    message = _build_message(data, photo_count, flyer_count)

    if SLACK_WEBHOOK_URL:
        try:
            requests.post(
                SLACK_WEBHOOK_URL,
                json={"text": message, "mrkdwn": True},
                timeout=10,
            ).raise_for_status()
        except Exception as e:
            print(f"[slack] webhook error: {e}")

    if dm_channel_id:
        _dm_clarisse(dm_channel_id, message)
