import asyncio
import logging
import os
import json
import re
import time
import aiohttp
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────
#  CONFIG  — fill these in
# ─────────────────────────────────────────
BOT_TOKEN = "8278263110:AAGZdHeRMCRsqMRkWT2dMqikSqqCyDqbmww"
API_ID    = 32003552
API_HASH  = "18e677db0dc3bb8cf89c574a6f460cc3"

ACCOUNTS_FILE = "accounts.json"
SESSIONS_DIR  = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  ACCOUNTS  (JSON storage)
# ─────────────────────────────────────────

def load_accounts() -> dict:
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return {}

def save_accounts(data: dict):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user_accounts(user_id: int) -> list:
    return load_accounts().get(str(user_id), [])

def add_user_account(user_id: int, phone: str, session_name: str):
    data = load_accounts()
    uid = str(user_id)
    data.setdefault(uid, [])
    if phone not in [a["phone"] for a in data[uid]]:
        data[uid].append({
            "phone": phone,
            "session": session_name,
            "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
    save_accounts(data)

def remove_user_account(user_id: int, phone: str):
    data = load_accounts()
    uid = str(user_id)
    if uid in data:
        data[uid] = [a for a in data[uid] if a["phone"] != phone]
    save_accounts(data)

def get_session_path(user_id: int = None) -> str | None:
    """Find any valid .session file. If user_id given, prefer that user's accounts."""
    data = load_accounts()
    order = []
    if user_id:
        order = list(data.get(str(user_id), []))
    for uid, accs in data.items():
        for a in accs:
            if a not in order:
                order.append(a)
    for acc in order:
        sp = os.path.join(SESSIONS_DIR, acc["session"])
        if os.path.exists(sp + ".session"):
            return sp
    return None

# ─────────────────────────────────────────
#  LINK PARSING
# ─────────────────────────────────────────

def extract_links(text: str) -> list[str]:
    raw = re.findall(
        r"(?:https?://)?t\.me/(?:joinchat/|\+)?[a-zA-Z0-9_/+\-]+",
        text,
    )
    out = []
    seen = set()
    for lnk in raw:
        if not lnk.startswith("http"):
            lnk = "https://" + lnk
        if lnk not in seen:
            seen.add(lnk)
            out.append(lnk)
    return out

def parse_link(link: str) -> tuple[bool, str]:
    """
    Returns (is_private, identifier)
    is_private=True  -> identifier is invite hash (without +)
    is_private=False -> identifier is username
    """
    # Private invite: t.me/+HASH or t.me/joinchat/HASH
    m = re.search(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_\-]+)", link)
    if m:
        return True, m.group(1)
    # Public username
    m = re.search(r"t\.me/([a-zA-Z0-9_]+)", link)
    if m:
        return False, m.group(1)
    return False, link

# ─────────────────────────────────────────
#  LINK CHECKER  (core) — FIXED
# ─────────────────────────────────────────

async def check_single_link(link: str, user_id: int) -> dict:
    result = {
        "link": link,
        "status": "unknown",
        "type": "?",
        "title": "N/A",
        "members": "N/A",
        "forward": "unknown",
    }

    try:
        from pyrogram import Client
        from pyrogram.errors import (
            UsernameInvalid, UsernameNotOccupied,
            FloodWait, ChannelPrivate,
        )

        is_private, identifier = parse_link(link)
        session = get_session_path(user_id)

        # ── NO SESSION: private links can't be checked ──
        if not session:
            if is_private:
                result["status"] = "need_account"
                result["title"] = "Add account to check private links"
                result["forward"] = "unknown"
                return result
            # For public links without session, use HTTP preview
            # Don't create `:memory:` client — it will ask for phone number
            result = await check_public_via_http(link, result)
            return result

        # ── WITH SESSION ──
        async def _check(app: Client):
            if is_private:
                # Use raw MTProto: messages.CheckChatInvite
                # Live check every time — NO caching, always fresh from Telegram
                from pyrogram import raw as _raw
                try:
                    r = await app.invoke(
                        _raw.functions.messages.CheckChatInvite(hash=identifier)
                    )
                    if hasattr(r, "chat"):
                        # ChatInviteAlready — already a member
                        c = r.chat
                        result["status"]  = "active"
                        result["title"]   = getattr(c, "title", "N/A")
                        result["members"] = getattr(c, "participants_count", "N/A")
                        result["type"]    = "supergroup" if getattr(c, "megagroup", False) else "channel" if getattr(c, "broadcast", False) else "group"
                        result["forward"] = "OFF (Restricted)" if getattr(c, "noforwards", False) else "ON"
                    else:
                        # ChatInvite — not a member, has preview info
                        result["status"]  = "active"
                        result["title"]   = getattr(r, "title", "N/A")
                        result["members"] = getattr(r, "participants_count", "N/A")
                        result["type"]    = "supergroup" if getattr(r, "megagroup", False) else "channel" if getattr(r, "broadcast", False) else "group"
                        # ChatInvite has noforwards field too
                        result["forward"] = "OFF (Restricted)" if getattr(r, "noforwards", False) else "ON"
                    return
                except Exception as e:
                    err = str(e)
                    if "INVITE_HASH_EXPIRED" in err:
                        result["status"] = "expired"
                        result["title"]  = "Invite link expired"
                        return
                    if "INVITE_HASH_INVALID" in err:
                        result["status"] = "expired"
                        result["title"]  = "Invalid invite link"
                        return
                    result["status"] = "error"
                    result["title"]  = err[:80]
                    return

            else:
                # Public link — resolve by username
                try:
                    chat = await app.get_chat(identifier)
                except UsernameInvalid:
                    result["status"] = "expired"
                    result["title"] = "Username invalid"
                    return
                except UsernameNotOccupied:
                    result["status"] = "expired"
                    result["title"] = "Username not occupied"
                    return
                except ChannelPrivate:
                    result["status"] = "expired"
                    result["title"] = "Channel is private"
                    return
                except Exception as e:
                    result["status"] = "error"
                    result["title"] = str(e)[:80]
                    return

            # Fill result
            result["status"]  = "active"
            result["title"]   = getattr(chat, "title", None) or getattr(chat, "first_name", "N/A")
            result["type"]    = str(getattr(chat, "type", "?")).split(".")[-1].lower()
            result["members"] = getattr(chat, "members_count", "N/A")
            protected = getattr(chat, "has_protected_content", False)
            result["forward"] = "OFF (Restricted)" if protected else "ON"

        try:
            async with Client(session, api_id=API_ID, api_hash=API_HASH, no_updates=True) as app:
                try:
                    await _check(app)
                except FloodWait as e:
                    await asyncio.sleep(min(e.value, 15))
                    result["status"] = "flood_wait"
                    result["title"]  = f"FloodWait {e.value}s"
        except Exception as e:
            result["status"] = "error"
            result["title"]  = f"Session error: {str(e)[:60]}"

    except ImportError:
        result["status"] = "error"
        result["title"]  = "pyrogram not installed"

    return result


async def check_public_via_http(link: str, result: dict) -> dict:
    """
    Fallback: Check public link via Telegram's web page (no account needed).
    Works for public t.me/username links only.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(link, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    html = await r.text()
                    # Extract title
                    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    title = m.group(1) if m else "N/A"
                    # Extract description/members
                    m2 = re.search(r'<meta property="og:description" content="([^"]+)"', html)
                    desc = m2.group(1) if m2 else ""
                    # Member count from description
                    mc = re.search(r"([\d,]+)\s+(?:members?|subscribers?)", desc, re.I)
                    members = mc.group(1).replace(",", "") if mc else "N/A"

                    if title and title != "Telegram":
                        result["status"]  = "active"
                        result["title"]   = title
                        result["members"] = members
                        result["type"]    = "channel/group"
                        result["forward"] = "unknown (no account)"
                    else:
                        result["status"] = "expired"
                        result["title"]  = "Not found"
                else:
                    result["status"] = "expired"
                    result["title"]  = f"HTTP {r.status}"
    except Exception as e:
        result["status"] = "error"
        result["title"]  = f"HTTP error: {str(e)[:50]}"
    return result


# ─────────────────────────────────────────
#  BULK LINK CHECKER — single client, sequential
#  Parallel Pyrogram clients on same .session = SQLite locked
#  Solution: open ONE client, check all links inside it
# ─────────────────────────────────────────

async def check_links_bulk(links: list[str], user_id: int,
                            progress_cb=None, batch_size: int = 10) -> list[dict]:
    from pyrogram import Client, raw as _raw

    session = get_session_path(user_id)
    results = []

    async def _check_one_with_client(app, link: str) -> dict:
        """Check a single link using an already-open Pyrogram client."""
        result = {
            "link": link, "status": "unknown", "type": "?",
            "title": "N/A", "members": "N/A", "forward": "unknown",
        }
        is_private, identifier = parse_link(link)
        try:
            if is_private:
                if not app:
                    result["status"] = "need_account"
                    result["title"]  = "Add account to check private links"
                    return result
                # Raw MTProto — no username resolve, works on hash directly
                r = await app.invoke(
                    _raw.functions.messages.CheckChatInvite(hash=identifier)
                )
                if hasattr(r, "chat"):
                    c = r.chat
                    result["status"]  = "active"
                    result["title"]   = getattr(c, "title", "N/A")
                    result["members"] = getattr(c, "participants_count", "N/A")
                    result["type"]    = "supergroup" if getattr(c, "megagroup", False) else "channel" if getattr(c, "broadcast", False) else "group"
                    result["forward"] = "OFF (Restricted)" if getattr(c, "noforwards", False) else "ON"
                else:
                    result["status"]  = "active"
                    result["title"]   = getattr(r, "title", "N/A")
                    result["members"] = getattr(r, "participants_count", "N/A")
                    result["type"]    = "supergroup" if getattr(r, "megagroup", False) else "channel" if getattr(r, "broadcast", False) else "group"
                    result["forward"] = "OFF (Restricted)" if getattr(r, "noforwards", False) else "ON"
            else:
                if app:
                    from pyrogram.errors import UsernameInvalid, UsernameNotOccupied, ChannelPrivate
                    try:
                        chat = await app.get_chat(identifier)
                        result["status"]  = "active"
                        result["title"]   = getattr(chat, "title", None) or getattr(chat, "first_name", "N/A")
                        result["type"]    = str(getattr(chat, "type", "?")).split(".")[-1].lower()
                        result["members"] = getattr(chat, "members_count", "N/A")
                        result["forward"] = "OFF (Restricted)" if getattr(chat, "has_protected_content", False) else "ON"
                    except (UsernameInvalid, UsernameNotOccupied):
                        result["status"] = "expired"
                        result["title"]  = "Username not found"
                    except ChannelPrivate:
                        result["status"] = "expired"
                        result["title"]  = "Channel is private"
                    except Exception as e:
                        result["status"] = "error"
                        result["title"]  = str(e)[:80]
                else:
                    # No session — use HTTP scrape
                    result = await check_public_via_http(link, result)
        except Exception as e:
            err = str(e)
            if "INVITE_HASH_EXPIRED" in err:
                result["status"] = "expired"
                result["title"]  = "Invite link expired"
            elif "INVITE_HASH_INVALID" in err:
                result["status"] = "invalid"
                result["title"]  = "Invalid invite hash"
            else:
                result["status"] = "error"
                result["title"]  = err[:80]
        return result

    # Open ONE client for all links — avoids database locked
    if session:
        try:
            async with Client(session, api_id=API_ID, api_hash=API_HASH, no_updates=True) as app:
                for i, lnk in enumerate(links):
                    r = await _check_one_with_client(app, lnk)
                    results.append(r)
                    if progress_cb:
                        await progress_cb(i + 1, len(links), lnk)
                    await asyncio.sleep(0.3)  # small delay to avoid flood
        except Exception as e:
            # Session failed entirely
            for lnk in links[len(results):]:
                results.append({"link": lnk, "status": "error", "type": "?",
                                 "title": f"Session error: {str(e)[:50]}", "members": "N/A", "forward": "unknown"})
    else:
        # No session — HTTP scrape for public, skip private
        for i, lnk in enumerate(links):
            r = await _check_one_with_client(None, lnk)
            results.append(r)
            if progress_cb:
                await progress_cb(i + 1, len(links), lnk)

    return results


# ─────────────────────────────────────────
#  RESULT FORMATTER  — Image 1 style
# ─────────────────────────────────────────

def fmt_result(r: dict, i: int, total: int) -> str:
    s = r["status"]
    if s == "active":
        icon  = "✅"
        s_txt = "Working"
    elif s in ("expired", "expired/invalid", "invalid"):
        icon  = "❌"
        s_txt = "Expired"
    elif s == "need_account":
        icon  = "🔐"
        s_txt = "Need Account"
    elif s == "flood_wait":
        icon  = "⏳"
        s_txt = "Flood Wait"
    else:
        icon  = "⚠️"
        s_txt = "Error"

    fwd = r["forward"]
    if "OFF" in str(fwd):
        fwd_txt = "OFF ❌"
    elif fwd == "ON":
        fwd_txt = "ON ✅"
    else:
        fwd_txt = str(fwd)

    members = r.get("members", "N/A")
    title   = r.get("title", "N/A")
    ltype   = r.get("type", "?")
    members_str = f"  👥 {members}" if members not in ("N/A", None, "") else ""

    return (
        f"\n"
        f"{icon} Link {i}/{total} - {s_txt}\n"
        f"🔗 {r['link']}\n"
        f"📝 {title}{members_str}\n"
        f"Forward: {fwd_txt}\n"
    )


def fmt_summary_header(total: int, active: int, expired: int, errors: int) -> str:
    return (
        f"✨ LINK CHECK RESULTS\n\n"
        f"Total links: {total}\n"
        f"Working links: {active}\n"
        f"Expired: {expired}\n"
        f"Error: {errors}\n"
        f"{'─'*32}\n"
    )


# ─────────────────────────────────────────
#  RAW API — Bot API colored Buttons
# ─────────────────────────────────────────

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def _btn(text: str, cb: str, style: str | None = None) -> dict:
    b = {"text": text, "callback_data": cb}
    if style:
        b["style"] = style
    return b

async def _send_raw(chat_id: int, text: str, keyboard: list[list[dict]],
                    disable_preview: bool = True) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{TG_API}/sendMessage", json=payload) as r:
            return await r.json()

async def _edit_raw(chat_id: int, message_id: int, text: str,
                    keyboard: list[list[dict]] | None = None,
                    disable_preview: bool = True) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{TG_API}/editMessageText", json=payload) as r:
            return await r.json()

async def _answer_cb(callback_query_id: str, text: str = "", alert: bool = False):
    payload = {"callback_query_id": callback_query_id, "text": text, "show_alert": alert}
    async with aiohttp.ClientSession() as s:
        await s.post(f"{TG_API}/answerCallbackQuery", json=payload)

# ── Keyboard layouts ──

def RAW_KB_MAIN() -> list[list[dict]]:
    return [
        [_btn("🔗 Link Checker",    "menu_linkchecker", "success")],
        [_btn("📨 Forward Manager", "menu_forwarder",   "primary")],
    ]

def RAW_KB_FORWARDER() -> list[list[dict]]:
    return [
        [
            _btn("➕ Add Account",    "fwd_addaccount",   "success"),
            _btn("📋 Account List",   "fwd_listaccounts", "primary"),
        ],
        [_btn("🗑️ Remove Account",  "fwd_removeaccount", "danger")],
        [_btn("▶️ Start Forward",   "fwd_start",         "success")],
        [_btn("⏹️ Stop Forward",    "fwd_stop",          "danger")],
        [_btn("🔙 Back",            "back_main")],
    ]

def RAW_KB_BACK(cb: str = "back_main") -> list[list[dict]]:
    return [[_btn("🔙 Back to Menu", cb)]]

def RAW_KB_STOP() -> list[list[dict]]:
    return [[_btn("⏹️ STOP FORWARD", "fwd_stop", "danger")]]

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Link Checker",    callback_data="menu_linkchecker")],
        [InlineKeyboardButton("📨 Forward Manager", callback_data="menu_forwarder")],
    ])

def kb_back(cb="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data=cb)]])

def kb_stop():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ STOP FORWARD", callback_data="fwd_stop")]])

# ─────────────────────────────────────────
#  FORWARD WORKER — FIXED
# ─────────────────────────────────────────

forward_tasks:   dict[int, asyncio.Task] = {}
forward_running: dict[int, bool]         = {}


async def resolve_chat_smart(app, link: str):
    """
    Resolve any Telegram link to a chat object.
    Public  (t.me/username)  → get_chat(username)
    Private (t.me/+hash)     → CheckChatInvite raw MTProto
                                 • Already member  → get_chat by chat_id
                                 • Not member      → join_chat(hash)
    """
    from pyrogram import raw as _raw
    from pyrogram.errors import (
        UserAlreadyParticipant, InviteHashExpired,
        InviteHashInvalid, FloodWait,
    )

    is_private, identifier = parse_link(link)

    # ── PUBLIC LINK ──────────────────────────────────────────
    if not is_private:
        return await app.get_chat(identifier)

    # ── PRIVATE INVITE LINK ──────────────────────────────────
    # Step 1: CheckChatInvite — gets info without joining
    try:
        inv = await app.invoke(
            _raw.functions.messages.CheckChatInvite(hash=identifier)
        )
    except Exception as e:
        err = str(e)
        if "INVITE_HASH_EXPIRED" in err:
            raise Exception(
                "The invite link has expired.\n"
                "Forward directly to the bot account first, then try again,\n"
                "or use the group's public @username link."
            )
        if "INVITE_HASH_INVALID" in err:
            raise Exception("Invalid invite link.")
        raise Exception(f"Could not check invite: {err[:80]}")

    # Step 2: ChatInviteAlready = already a member → use chat.id
    if hasattr(inv, "chat") and inv.chat is not None:
        raw_id = inv.chat.id
        # Supergroups/channels use -100XXXXXXXXX format
        for try_id in [raw_id, int(f"-100{abs(raw_id)}")]:
            try:
                return await app.get_chat(try_id)
            except Exception:
                continue
        # Fallback — join won't work for already-member, scan dialogs
        title = getattr(inv.chat, "title", "")
        async for dlg in app.get_dialogs(limit=500):
            if getattr(dlg.chat, "title", "") == title and title:
                return dlg.chat
        raise Exception(
            f"Already a member of '{title}' but could not fetch it.\n"
            "Try using the public @username link instead."
        )

    # Step 3: Not a member → join
    try:
        return await app.join_chat(identifier)
    except UserAlreadyParticipant:
        # Rare race condition — scan dialogs
        title = getattr(inv, "title", "") if inv else ""
        async for dlg in app.get_dialogs(limit=300):
            if title and getattr(dlg.chat, "title", "") == title:
                return dlg.chat
        raise Exception("Already a member but could not fetch the chat.")
    except (InviteHashExpired, InviteHashInvalid) as e:
        raise Exception(f"Invite link error: {e}")
    except FloodWait as fw:
        await asyncio.sleep(fw.value + 1)
        return await app.join_chat(identifier)


async def smart_forward_worker(
    user_id: int,
    source_link: str,
    dest_link: str,
    status_msg,
):
    try:
        from pyrogram import Client
        from pyrogram.errors import FloodWait

        session = get_session_path(user_id)
        if not session:
            await status_msg.edit_text(
                "❌ No account found. Add an account first.",
                reply_markup=kb_back("menu_forwarder"),
            )
            return

        async with Client(session, api_id=API_ID, api_hash=API_HASH) as app:
            # Resolve source & dest
            try:
                src_chat = await resolve_chat_smart(app, source_link)
            except Exception as e:
                await status_msg.edit_text(
                    f"❌ Cannot resolve source:\n{e}\n\n"
                    f"Make sure the account is a member of the source group/channel.",
                    reply_markup=kb_back("menu_forwarder"),
                )
                return

            try:
                dst_chat = await resolve_chat_smart(app, dest_link)
            except Exception as e:
                await status_msg.edit_text(
                    f"❌ Cannot resolve destination:\n{e}\n\n"
                    f"Make sure the account is a member/admin of the destination.",
                    reply_markup=kb_back("menu_forwarder"),
                )
                return

            src_title = getattr(src_chat, "title", source_link)
            dst_title = getattr(dst_chat, "title", dest_link)

            forwarded = failed = skipped = 0
            start_t = time.time()
            last_upd = 0.0
            forward_running[user_id] = True

            # get_chat_history returns newest first; reverse for chronological order
            messages = []
            async for msg in app.get_chat_history(src_chat.id):
                messages.append(msg)
                if len(messages) > 5000:
                    break
            messages.reverse()

            import tempfile, os as _os

            async def send_one(msg) -> str:
                """
                Try to send one message to dst_chat.
                Returns: 'ok' | 'skip' | 'fail'
                """
                # Skip service messages (no text, no media)
                is_service = (
                    msg.service is not None
                    or (msg.text is None and msg.caption is None
                        and msg.photo is None and msg.video is None
                        and msg.document is None and msg.audio is None
                        and msg.voice is None and msg.sticker is None
                        and msg.animation is None and msg.video_note is None)
                )
                if is_service:
                    return "skip"

                cap = msg.caption or ""

                # ── Step 1: Try copy_message (fastest, works for non-restricted) ──
                try:
                    await app.copy_message(
                        chat_id=dst_chat.id,
                        from_chat_id=src_chat.id,
                        message_id=msg.id,
                    )
                    return "ok"
                except Exception as e:
                    err = str(e)
                    # Service / unsupported message types
                    if any(x in err for x in [
                        "MEDIA_EMPTY", "MESSAGE_EMPTY", "MESSAGE_ID_INVALID",
                        "SERVICE_MESSAGE", "poll", "game", "invoice", "cannot be copied"
                    ]):
                        return "skip"
                    # For other errors, fall through to download+upload

                # ── Step 2: Download & re-upload (bypass forward restriction) ──
                tmp_path = None
                try:
                    if msg.text:
                        await app.send_message(dst_chat.id, msg.text)
                        return "ok"

                    elif msg.photo:
                        tmp_path = await app.download_media(msg.photo)
                        await app.send_photo(dst_chat.id, tmp_path, caption=cap)
                        return "ok"

                    elif msg.video:
                        tmp_path = await app.download_media(msg.video)
                        await app.send_video(
                            dst_chat.id, tmp_path,
                            caption=cap,
                            duration=msg.video.duration,
                            width=msg.video.width,
                            height=msg.video.height,
                            supports_streaming=True,
                        )
                        return "ok"

                    elif msg.document:
                        tmp_path = await app.download_media(msg.document)
                        await app.send_document(dst_chat.id, tmp_path, caption=cap,
                                                file_name=msg.document.file_name)
                        return "ok"

                    elif msg.audio:
                        tmp_path = await app.download_media(msg.audio)
                        await app.send_audio(dst_chat.id, tmp_path, caption=cap,
                                             duration=msg.audio.duration,
                                             performer=msg.audio.performer or "",
                                             title=msg.audio.title or "")
                        return "ok"

                    elif msg.voice:
                        tmp_path = await app.download_media(msg.voice)
                        await app.send_voice(dst_chat.id, tmp_path, caption=cap)
                        return "ok"

                    elif msg.video_note:
                        tmp_path = await app.download_media(msg.video_note)
                        await app.send_video_note(dst_chat.id, tmp_path,
                                                   duration=msg.video_note.duration)
                        return "ok"

                    elif msg.sticker:
                        # Stickers use file_id directly (download doesn't always work)
                        try:
                            await app.send_sticker(dst_chat.id, msg.sticker.file_id)
                            return "ok"
                        except Exception:
                            tmp_path = await app.download_media(msg.sticker)
                            await app.send_sticker(dst_chat.id, tmp_path)
                            return "ok"

                    elif msg.animation:
                        tmp_path = await app.download_media(msg.animation)
                        await app.send_animation(dst_chat.id, tmp_path, caption=cap)
                        return "ok"

                    else:
                        return "skip"

                except Exception as e2:
                    err2 = str(e2)
                    if any(x in err2 for x in ["MEDIA_EMPTY", "FILE_REFERENCE"]):
                        return "skip"
                    return "fail"
                finally:
                    # Clean up downloaded temp file
                    if tmp_path and _os.path.exists(tmp_path):
                        try:
                            _os.remove(tmp_path)
                        except Exception:
                            pass

            for msg in messages:
                if not forward_running.get(user_id):
                    break
                try:
                    res = await send_one(msg)
                    if res == "ok":
                        forwarded += 1
                        await asyncio.sleep(0.6)
                    elif res == "skip":
                        skipped += 1
                    else:
                        failed += 1
                except FloodWait as fw:
                    await asyncio.sleep(min(fw.value + 2, 60))
                    # Retry after flood wait
                    try:
                        res = await send_one(msg)
                        if res == "ok":
                            forwarded += 1
                        elif res == "skip":
                            skipped += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
                except Exception:
                    failed += 1

                now = time.time()
                if now - last_upd >= 4:
                    last_upd = now
                    elapsed = now - start_t
                    m, s = divmod(int(elapsed), 60)
                    spd = forwarded / elapsed if elapsed > 0 else 0
                    try:
                        await status_msg.edit_text(
                            f"⚡ *LIVE FORWARD*\n"
                            f"From : {src_title}\n"
                            f"To   : {dst_title}\n"
                            f"─────────────────\n"
                            f"✅ Sent    : {forwarded}\n"
                            f"❌ Failed  : {failed}\n"
                            f"⏭️ Skipped : {skipped}\n"
                            f"🚀 Speed   : {spd:.1f} msg/s\n"
                            f"⏱️ Time    : {m}m {s}s\n"
                            f"📡 Status  : RUNNING",
                            reply_markup=kb_stop(),
                        )
                    except Exception:
                        pass

            elapsed = time.time() - start_t
            m, s = divmod(int(elapsed), 60)
            done_status = "COMPLETED ✅" if forward_running.get(user_id) else "STOPPED ⏹️"
            forward_running[user_id] = False
            try:
                await status_msg.edit_text(
                    f"*FORWARD {done_status}*\n"
                    f"─────────────────\n"
                    f"From    : {src_title}\n"
                    f"To      : {dst_title}\n"
                    f"✅ Sent    : {forwarded}\n"
                    f"❌ Failed  : {failed}\n"
                    f"⏭️ Skipped : {skipped}\n"
                    f"⏱️ Time    : {m}m {s}s",
                    reply_markup=kb_back("menu_forwarder"),
                )
            except Exception:
                pass

    except ImportError:
        await status_msg.edit_text(
            "pyrogram not installed.\nRun: pip install pyrogram tgcrypto",
            reply_markup=kb_back(),
        )
    finally:
        forward_running[user_id] = False

# ─────────────────────────────────────────
#  LOGIN  (Pyrogram)
# ─────────────────────────────────────────

login_state: dict[int, dict] = {}

async def send_otp(user_id: int, phone: str):
    from pyrogram import Client
    sess = os.path.join(SESSIONS_DIR, f"u{user_id}_{phone.replace('+','')}")
    client = Client(sess, api_id=API_ID, api_hash=API_HASH, no_updates=True)
    await client.connect()
    sent = await client.send_code(phone)
    return client, sent.phone_code_hash, sess

async def sign_in(client, phone: str, code: str, ph_hash: str):
    from pyrogram.errors import SessionPasswordNeeded
    try:
        await client.sign_in(phone, ph_hash, code)
        return "ok"
    except SessionPasswordNeeded:
        return "need_2fa"
    except Exception as e:
        return f"error: {e}"

# ─────────────────────────────────────────
#  HANDLERS
# ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat_id = update.effective_chat.id
    await _send_raw(
        chat_id,
        f"*Welcome, {u.first_name}* 👋\n\n"
        f"🔗 *Link Checker* — Check if Telegram links are active, expired, or forward-restricted.\n\n"
        f"📨 *Forward Manager* — Login accounts and forward media even from restricted chats.",
        RAW_KB_MAIN(),
    )

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    d   = q.data
    uid = q.from_user.id
    cid = q.message.chat.id
    mid = q.message.message_id

    await _answer_cb(q.id)

    if d == "back_main":
        ctx.user_data["mode"] = ""
        await _edit_raw(
            cid, mid,
            "Main Menu — choose an option:",
            RAW_KB_MAIN(),
        )

    elif d == "menu_linkchecker":
        ctx.user_data["mode"] = "linkchecker"
        await _edit_raw(
            cid, mid,
            "🔗 *LINK CHECKER*\n\n"
            "Send one or more Telegram links (supports 200-300 links at once).\n\n"
            "Supports:\n"
            "  • Public: `t.me/username`\n"
            "  • Private: `t.me/+hash`\n\n"
            "Checks: active/expired, forward on/off, title, member count.\n\n"
            "💡 Add an account in Forward Manager to check private links.",
            RAW_KB_BACK(),
        )

    elif d == "menu_forwarder":
        await _edit_raw(
            cid, mid,
            "📨 *FORWARD MANAGER*\n\n"
            "Manage Telegram accounts and smart-forward messages.\n"
            "Works even on restricted (forward-off) chats.",
            RAW_KB_FORWARDER(),
        )

    elif d == "fwd_addaccount":
        ctx.user_data["mode"] = "add_account"
        await _edit_raw(
            cid, mid,
            "➕ *ADD ACCOUNT*\n\n"
            "Send your phone number with country code.\n"
            "Example: `+919876543210`\n\n"
            "An OTP will be sent to your Telegram app.",
            RAW_KB_BACK("menu_forwarder"),
        )

    elif d == "fwd_listaccounts":
        accs = get_user_accounts(uid)
        if not accs:
            txt = "No accounts added yet.\nUse Add Account to add one."
        else:
            lines = [f"📋 *Accounts ({len(accs)}):*"]
            for i, a in enumerate(accs, 1):
                lines.append(f"{i}. `{a['phone']}`  (added {a['added']})")
            txt = "\n".join(lines)
        await _edit_raw(cid, mid, txt, RAW_KB_BACK("menu_forwarder"))

    elif d == "fwd_removeaccount":
        accs = get_user_accounts(uid)
        if not accs:
            await _edit_raw(cid, mid, "No accounts to remove.", RAW_KB_BACK("menu_forwarder"))
            return
        rows = [
            [_btn(f"🗑️ Remove {a['phone']}", f"rm_{a['phone']}", "danger")]
            for a in accs
        ]
        rows.append([_btn("🔙 Back", "menu_forwarder")])
        await _edit_raw(cid, mid, "Select account to remove:", rows)

    elif d.startswith("rm_"):
        phone = d[3:]
        remove_user_account(uid, phone)
        sp = os.path.join(SESSIONS_DIR, f"u{uid}_{phone.replace('+','')}.session")
        if os.path.exists(sp):
            os.remove(sp)
        await _edit_raw(
            cid, mid,
            f"✅ Account {phone} removed successfully.",
            RAW_KB_BACK("menu_forwarder"),
        )

    elif d == "fwd_start":
        if not get_user_accounts(uid):
            await _edit_raw(
                cid, mid,
                "❌ No accounts found. Add an account first.",
                RAW_KB_BACK("menu_forwarder"),
            )
            return
        ctx.user_data["mode"] = "fwd_source"
        await _edit_raw(
            cid, mid,
            "▶️ *START FORWARD — Step 1/2*\n\n"
            "Send the SOURCE link (where to copy FROM).\n\n"
            "Accepted formats:\n"
            "  `t.me/username`\n"
            "  `t.me/+invitehash`\n\n"
            "The account must be a member of the source.",
            None,
        )

    elif d == "fwd_stop":
        if forward_running.get(uid):
            forward_running[uid] = False
            await _edit_raw(
                cid, mid,
                "⏹️ Stop signal sent. Forwarding will stop after current message.",
                RAW_KB_BACK("menu_forwarder"),
            )
        else:
            await _answer_cb(q.id, "No active forward running.", alert=True)


async def _run_link_check(links: list[str], uid: int, cid: int, prog):
    """Background task: check all links and send results. Never times out."""
    total = len(links)
    last_prog_update = [time.time()]

    async def progress_cb(done: int, total: int, current_link: str):
        now = time.time()
        if now - last_prog_update[0] >= 3:
            last_prog_update[0] = now
            try:
                await prog.edit_text(
                    f"⏳ Checking... {done}/{total} links\n"
                    f"Current: {current_link[:55]}",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

    results = await check_links_bulk(links, uid, progress_cb=progress_cb)

    active_n  = sum(1 for r in results if r["status"] == "active")
    expired_n = sum(1 for r in results if r["status"] in ("expired", "invalid"))
    error_n   = len(results) - active_n - expired_n

    try:
        await prog.edit_text(
            f"✅ Done! Sending results for {total} links...",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    header = fmt_summary_header(total, active_n, expired_n, error_n)

    def safe(txt: str) -> str:
        """Remove chars that break Telegram Markdown."""
        return str(txt).replace("*","").replace("`","").replace("_","").replace("[","").replace("]","")

    chunks_to_send = []
    current_header = header
    body = ""
    for i, r in enumerate(results, 1):
        r2 = dict(r)
        r2["title"] = safe(r2.get("title", "N/A"))
        block = fmt_result(r2, i, total)
        if len(current_header + body + block) > 3800:
            chunks_to_send.append(current_header + body)
            body = block
            current_header = ""  # only first chunk gets full header
        else:
            body += block
    if body:
        chunks_to_send.append(current_header + body)

    try:
        await prog.delete()
    except Exception:
        pass

    for chunk in chunks_to_send:
        try:
            await _send_raw(cid, chunk, RAW_KB_BACK(), disable_preview=True)
        except Exception:
            # Markdown failed — send as plain text
            try:
                async with aiohttp.ClientSession() as s:
                    await s.post(f"{TG_API}/sendMessage", json={
                        "chat_id": cid,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    })
            except Exception:
                pass
        await asyncio.sleep(0.4)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    cid  = update.effective_chat.id
    text = (update.message.text or "").strip()
    mode = ctx.user_data.get("mode", "")

    # ── LINK CHECKER ──
    if mode == "linkchecker":
        links = extract_links(text)
        if not links:
            await update.message.reply_text(
                "❌ No valid Telegram links found.\n"
                "Send links like: `t.me/username` or `t.me/+hash`",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        total = len(links)
        prog = await update.message.reply_text(
            f"⏳ Checking {total} link(s)...",
            disable_web_page_preview=True,
        )

        # Run as background task — avoids Telegram's 30s handler timeout
        asyncio.create_task(_run_link_check(links, uid, cid, prog))



    # ── ADD ACCOUNT: phone ──
    elif mode == "add_account":
        if not re.match(r"^\+\d{7,15}$", text):
            await update.message.reply_text("❌ Invalid format. Example: `+919876543210`",
                                            parse_mode="Markdown")
            return
        w = await update.message.reply_text(f"📱 Sending OTP to {text}...")
        try:
            client, ph_hash, sess = await send_otp(uid, text)
            login_state[uid] = {"phone": text, "client": client, "hash": ph_hash, "session": sess}
            ctx.user_data["mode"] = "wait_otp"
            await w.edit_text(
                f"✅ OTP sent to `{text}`\n\n"
                f"Enter the code you received in Telegram.\n"
                f"Format: `12345`  (no spaces)",
                parse_mode="Markdown",
            )
        except Exception as e:
            await w.edit_text(f"❌ Failed to send OTP:\n{e}")

    # ── ADD ACCOUNT: OTP ──
    elif mode == "wait_otp":
        code = text.replace(" ", "").replace("-", "")
        st = login_state.get(uid)
        if not st:
            await update.message.reply_text("Session expired. Start again.")
            ctx.user_data["mode"] = ""
            return
        res = await sign_in(st["client"], st["phone"], code, st["hash"])
        if res == "ok":
            await st["client"].disconnect()
            sess_base = os.path.basename(st["session"])
            add_user_account(uid, st["phone"], sess_base)
            login_state.pop(uid, None)
            ctx.user_data["mode"] = ""
            await _send_raw(
                cid,
                f"✅ Account added successfully: `{st['phone']}`",
                RAW_KB_FORWARDER(),
            )
        elif res == "need_2fa":
            ctx.user_data["mode"] = "wait_2fa"
            await update.message.reply_text("🔐 2FA enabled. Enter your Telegram password:")
        else:
            await update.message.reply_text(f"❌ Login failed: {res}")

    # ── ADD ACCOUNT: 2FA ──
    elif mode == "wait_2fa":
        st = login_state.get(uid)
        if not st:
            await update.message.reply_text("Session expired.")
            return
        try:
            await st["client"].check_password(text)
            await st["client"].disconnect()
            sess_base = os.path.basename(st["session"])
            add_user_account(uid, st["phone"], sess_base)
            login_state.pop(uid, None)
            ctx.user_data["mode"] = ""
            await _send_raw(
                cid,
                f"✅ Account added successfully: `{st['phone']}`",
                RAW_KB_FORWARDER(),
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Wrong password: {e}")

    # ── FORWARD: source ──
    elif mode == "fwd_source":
        ctx.user_data["fwd_source"] = text
        ctx.user_data["mode"] = "fwd_dest"
        await update.message.reply_text(
            f"✅ Source set: `{text}`\n\n"
            f"Step 2/2 — Send the DESTINATION link (where to forward TO).\n\n"
            f"The account must be a member or admin of the destination.",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    # ── FORWARD: destination ──
    elif mode == "fwd_dest":
        source = ctx.user_data.get("fwd_source", "")
        ctx.user_data["mode"] = ""

        res = await _send_raw(
            cid,
            f"⚡ Starting forward...\nFrom: `{source}`\nTo: `{text}`\n\nInitializing...",
            RAW_KB_STOP(),
        )
        sm_id = res.get("result", {}).get("message_id")

        class _FakeMsg:
            def __init__(self, chat_id, message_id):
                self._cid = chat_id
                self._mid = message_id
            async def edit_text(self, t, **kw):
                kb = None
                if "reply_markup" in kw:
                    rm = kw["reply_markup"]
                    if isinstance(rm, InlineKeyboardMarkup):
                        kb = [[{"text": b.text, "callback_data": b.callback_data}
                               for b in row] for row in rm.inline_keyboard]
                    else:
                        kb = rm
                await _edit_raw(self._cid, self._mid, t, kb, disable_preview=True)

        sm = _FakeMsg(cid, sm_id)

        if uid in forward_tasks and not forward_tasks[uid].done():
            forward_tasks[uid].cancel()

        forward_tasks[uid] = asyncio.create_task(
            smart_forward_worker(uid, source, text, sm)
        )

    else:
        await _send_raw(
            cid,
            "Use /start to open the menu.",
            RAW_KB_MAIN(),
        )

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
