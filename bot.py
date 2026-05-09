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
        order = data.get(str(user_id), [])
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
#  LINK CHECKER  (core)
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
            InviteHashExpired, InviteHashInvalid,
            UsernameInvalid, UsernameNotOccupied,
            UserAlreadyParticipant, FloodWait,
        )

        is_private, identifier = parse_link(link)
        session = get_session_path(user_id)

        async def _check(app: Client):
            if is_private:
                # For private links, use get_chat with +hash
                # If not joined, try joining temporarily to get info
                try:
                    chat = await app.get_chat(f"+{identifier}")
                except Exception:
                    # Try joining to get info, then leave
                    try:
                        chat = await app.join_chat(f"+{identifier}")
                        # Leave immediately after getting info
                        await asyncio.sleep(1)
                        try:
                            await app.leave_chat(chat.id)
                        except Exception:
                            pass
                    except InviteHashExpired:
                        result["status"] = "expired"
                        return
                    except InviteHashInvalid:
                        result["status"] = "invalid"
                        return
                    except UserAlreadyParticipant:
                        chat = await app.get_chat(f"+{identifier}")
                    except Exception as e:
                        result["status"] = f"error"
                        result["title"] = str(e)[:60]
                        return
            else:
                try:
                    chat = await app.get_chat(identifier)
                except (UsernameInvalid, UsernameNotOccupied):
                    result["status"] = "expired/invalid"
                    return
                except Exception as e:
                    result["status"] = "error"
                    result["title"] = str(e)[:60]
                    return

            result["status"] = "active"
            result["title"] = getattr(chat, "title", None) or getattr(chat, "first_name", "N/A")
            result["type"] = str(getattr(chat, "type", "?")).split(".")[-1].lower()
            result["members"] = getattr(chat, "members_count", "N/A")
            protected = getattr(chat, "has_protected_content", False)
            result["forward"] = "OFF (Restricted)" if protected else "ON"

        if session:
            async with Client(session, api_id=API_ID, api_hash=API_HASH, no_updates=True) as app:
                try:
                    await _check(app)
                except FloodWait as e:
                    await asyncio.sleep(min(e.value, 10))
                    result["status"] = "flood_wait"
        else:
            # No session — only public links work
            if not is_private:
                async with Client(
                    ":memory:", api_id=API_ID, api_hash=API_HASH, no_updates=True
                ) as app:
                    try:
                        await _check(app)
                    except Exception:
                        result["status"] = "error (no account)"
            else:
                result["status"] = "need_account"
                result["forward"] = "Add account to check private links"

    except ImportError:
        result["status"] = "pyrogram not installed"

    return result

def fmt_result(r: dict, i: int) -> str:
    s = r["status"]
    if s == "active":
        s_txt = "ACTIVE"
    elif s in ("expired", "expired/invalid", "invalid"):
        s_txt = "EXPIRED"
    elif s == "need_account":
        s_txt = "Need Account"
    else:
        s_txt = s.upper()

    fwd = r["forward"]
    if "OFF" in str(fwd):
        fwd_txt = "OFF (Restricted)"
    elif fwd == "ON":
        fwd_txt = "ON (Allowed)"
    else:
        fwd_txt = fwd

    return (
        f"\n[{i}] {r['link']}\n"
        f"  Status  : {s_txt}\n"
        f"  Title   : {r['title']}\n"
        f"  Members : {r['members']}\n"
        f"  Type    : {r['type']}\n"
        f"  Forward : {fwd_txt}\n"
    )

# ─────────────────────────────────────────
#  RAW API — Bot API 9.4 Colored Buttons
#
#  python-telegram-bot abhi style field
#  support nahi karta, isliye raw HTTP use
#  karte hain colored buttons ke liye.
#  style: "success"=green, "danger"=red,
#         "primary"=blue, None=default grey
# ─────────────────────────────────────────

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def _btn(text: str, cb: str, style: str | None = None) -> dict:
    """Raw button dict with optional color style."""
    b = {"text": text, "callback_data": cb}
    if style:
        b["style"] = style
    return b

async def _send_raw(chat_id: int, text: str, keyboard: list[list[dict]]) -> dict:
    """Send message with colored inline keyboard via raw HTTP."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
        "parse_mode": "Markdown",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{TG_API}/sendMessage", json=payload) as r:
            return await r.json()

async def _edit_raw(chat_id: int, message_id: int, text: str,
                    keyboard: list[list[dict]] | None = None) -> dict:
    """Edit message with colored inline keyboard via raw HTTP."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
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

# ── Keyboard layouts (raw dicts) ──

def RAW_KB_MAIN() -> list[list[dict]]:
    return [
        [_btn("Link Checker",    "menu_linkchecker", "success")],
        [_btn("Forward Manager", "menu_forwarder",   "primary")],
    ]

def RAW_KB_FORWARDER() -> list[list[dict]]:
    return [
        [
            _btn("Add Account",    "fwd_addaccount",   "success"),
            _btn("Account List",   "fwd_listaccounts", "primary"),
        ],
        [_btn("Remove Account",  "fwd_removeaccount", "danger")],
        [_btn("Start Forward",   "fwd_start",         "success")],
        [_btn("Stop Forward",    "fwd_stop",          "danger")],
        [_btn("Back",            "back_main")],
    ]

def RAW_KB_BACK(cb: str = "back_main") -> list[list[dict]]:
    return [[_btn("Back to Menu", cb)]]

def RAW_KB_STOP() -> list[list[dict]]:
    return [[_btn("STOP FORWARD", "fwd_stop", "danger")]]

# ── python-telegram-bot compatible wrappers (for reply_markup param) ──
# Used only when we can't avoid PTB's send (e.g. update.message.reply_text)
# In those cases we fall back to no-color; main menus use raw API.

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Link Checker",    callback_data="menu_linkchecker")],
        [InlineKeyboardButton("Forward Manager", callback_data="menu_forwarder")],
    ])

def kb_back(cb="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data=cb)]])

def kb_stop():
    return InlineKeyboardMarkup([[InlineKeyboardButton("STOP FORWARD", callback_data="fwd_stop")]])

# ─────────────────────────────────────────
#  FORWARD WORKER
# ─────────────────────────────────────────

forward_tasks:   dict[int, asyncio.Task] = {}
forward_running: dict[int, bool]         = {}

async def resolve_chat_smart(app, link: str):
    """Resolve any link type: public username OR private invite hash."""
    is_private, identifier = parse_link(link)
    if is_private:
        # Try get_chat first (if already joined)
        try:
            return await app.get_chat(f"+{identifier}")
        except Exception:
            pass
        # Try joining
        from pyrogram.errors import UserAlreadyParticipant
        try:
            return await app.join_chat(f"+{identifier}")
        except UserAlreadyParticipant:
            return await app.get_chat(f"+{identifier}")
    else:
        return await app.get_chat(identifier)

async def smart_forward_worker(
    user_id: int,
    source_link: str,
    dest_link: str,
    status_msg: Message,
):
    try:
        from pyrogram import Client
        from pyrogram.errors import FloodWait

        session = get_session_path(user_id)
        if not session:
            await status_msg.edit_text(
                "No account found. Add an account first.",
                reply_markup=kb_back("menu_forwarder"),
            )
            return

        async with Client(session, api_id=API_ID, api_hash=API_HASH) as app:
            # Resolve source & dest
            try:
                src_chat = await resolve_chat_smart(app, source_link)
            except Exception as e:
                await status_msg.edit_text(
                    f"Cannot resolve source:\n{e}\n\n"
                    f"Make sure the account is a member of the source group/channel.",
                    reply_markup=kb_back("menu_forwarder"),
                )
                return

            try:
                dst_chat = await resolve_chat_smart(app, dest_link)
            except Exception as e:
                await status_msg.edit_text(
                    f"Cannot resolve destination:\n{e}\n\n"
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

            # get_chat_history returns newest first; we reverse for chronological order
            messages = []
            async for msg in app.get_chat_history(src_chat.id):
                messages.append(msg)
                if len(messages) > 5000:  # safety cap
                    break
            messages.reverse()  # oldest first

            for msg in messages:
                if not forward_running.get(user_id):
                    break
                try:
                    sent = False
                    # Copy method bypasses forward restriction
                    if msg.text:
                        await app.send_message(dst_chat.id, msg.text)
                        sent = True
                    elif msg.photo:
                        await app.send_photo(dst_chat.id, msg.photo.file_id, caption=msg.caption or "")
                        sent = True
                    elif msg.video:
                        await app.send_video(dst_chat.id, msg.video.file_id, caption=msg.caption or "")
                        sent = True
                    elif msg.document:
                        await app.send_document(dst_chat.id, msg.document.file_id, caption=msg.caption or "")
                        sent = True
                    elif msg.audio:
                        await app.send_audio(dst_chat.id, msg.audio.file_id)
                        sent = True
                    elif msg.voice:
                        await app.send_voice(dst_chat.id, msg.voice.file_id)
                        sent = True
                    elif msg.sticker:
                        await app.send_sticker(dst_chat.id, msg.sticker.file_id)
                        sent = True
                    elif msg.animation:
                        await app.send_animation(dst_chat.id, msg.animation.file_id)
                        sent = True
                    elif msg.video_note:
                        await app.send_video_note(dst_chat.id, msg.video_note.file_id)
                        sent = True
                    else:
                        skipped += 1

                    if sent:
                        forwarded += 1
                        await asyncio.sleep(0.8)

                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 2)
                except Exception:
                    failed += 1

                # Live dashboard update every 4 seconds
                now = time.time()
                if now - last_upd >= 4:
                    last_upd = now
                    elapsed = now - start_t
                    m, s = divmod(int(elapsed), 60)
                    spd = forwarded / elapsed if elapsed > 0 else 0
                    try:
                        await status_msg.edit_text(
                            f"LIVE FORWARD\n"
                            f"From : {src_title}\n"
                            f"To   : {dst_title}\n"
                            f"─────────────────\n"
                            f"Sent    : {forwarded}\n"
                            f"Failed  : {failed}\n"
                            f"Skipped : {skipped}\n"
                            f"Speed   : {spd:.1f} msg/s\n"
                            f"Time    : {m}m {s}s\n"
                            f"Status  : RUNNING",
                            reply_markup=kb_stop(),
                        )
                    except Exception:
                        pass

            # Final summary
            elapsed = time.time() - start_t
            m, s = divmod(int(elapsed), 60)
            done_status = "COMPLETED" if forward_running.get(user_id) else "STOPPED"
            forward_running[user_id] = False
            try:
                await status_msg.edit_text(
                    f"FORWARD {done_status}\n"
                    f"─────────────────\n"
                    f"From    : {src_title}\n"
                    f"To      : {dst_title}\n"
                    f"Sent    : {forwarded}\n"
                    f"Failed  : {failed}\n"
                    f"Skipped : {skipped}\n"
                    f"Time    : {m}m {s}s",
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
#  All menu messages use _send_raw / _edit_raw
#  for Bot API 9.4 colored buttons.
# ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat_id = update.effective_chat.id
    await _send_raw(
        chat_id,
        f"*Welcome, {u.first_name}*\n\n"
        f"*Link Checker* — Check if Telegram links are active, expired, or forward-restricted.\n\n"
        f"*Forward Manager* — Login accounts and forward media even from restricted chats.",
        RAW_KB_MAIN(),
    )

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    d   = q.data
    uid = q.from_user.id
    cid = q.message.chat.id
    mid = q.message.message_id

    # Answer the callback query (removes loading spinner)
    await _answer_cb(q.id)

    if d == "back_main":
        await _edit_raw(
            cid, mid,
            "Main Menu — choose an option:",
            RAW_KB_MAIN(),
        )

    elif d == "menu_linkchecker":
        ctx.user_data["mode"] = "linkchecker"
        await _edit_raw(
            cid, mid,
            "LINK CHECKER\n\n"
            "Send one or more Telegram links (up to 10 per message).\n\n"
            "Supports:\n"
            "  - Public: t.me/username\n"
            "  - Private: t.me/+hash\n\n"
            "Checks: active/expired, forward on/off, title, member count.",
            RAW_KB_BACK(),
        )

    elif d == "menu_forwarder":
        await _edit_raw(
            cid, mid,
            "FORWARD MANAGER\n\n"
            "Manage Telegram accounts and smart-forward messages.\n"
            "Works even on restricted (forward-off) chats.",
            RAW_KB_FORWARDER(),
        )

    elif d == "fwd_addaccount":
        ctx.user_data["mode"] = "add_account"
        await _edit_raw(
            cid, mid,
            "ADD ACCOUNT\n\n"
            "Send your phone number with country code.\n"
            "Example: +919876543210\n\n"
            "An OTP will be sent to your Telegram app.",
            RAW_KB_BACK("menu_forwarder"),
        )

    elif d == "fwd_listaccounts":
        accs = get_user_accounts(uid)
        if not accs:
            txt = "No accounts added yet.\nUse Add Account to add one."
        else:
            lines = [f"Accounts ({len(accs)}):"]
            for i, a in enumerate(accs, 1):
                lines.append(f"{i}. {a['phone']}  (added {a['added']})")
            txt = "\n".join(lines)
        await _edit_raw(cid, mid, txt, RAW_KB_BACK("menu_forwarder"))

    elif d == "fwd_removeaccount":
        accs = get_user_accounts(uid)
        if not accs:
            await _edit_raw(cid, mid, "No accounts to remove.", RAW_KB_BACK("menu_forwarder"))
            return
        rows = [
            [_btn(f"Remove {a['phone']}", f"rm_{a['phone']}", "danger")]
            for a in accs
        ]
        rows.append([_btn("Back", "menu_forwarder")])
        await _edit_raw(cid, mid, "Select account to remove:", rows)

    elif d.startswith("rm_"):
        phone = d[3:]
        remove_user_account(uid, phone)
        sp = os.path.join(SESSIONS_DIR, f"u{uid}_{phone.replace('+','')}.session")
        if os.path.exists(sp):
            os.remove(sp)
        await _edit_raw(
            cid, mid,
            f"Account {phone} removed successfully.",
            RAW_KB_BACK("menu_forwarder"),
        )

    elif d == "fwd_start":
        if not get_user_accounts(uid):
            await _edit_raw(
                cid, mid,
                "No accounts found. Add an account first.",
                RAW_KB_BACK("menu_forwarder"),
            )
            return
        ctx.user_data["mode"] = "fwd_source"
        await _edit_raw(
            cid, mid,
            "START FORWARD — Step 1/2\n\n"
            "Send the SOURCE link (where to copy FROM).\n\n"
            "Accepted formats:\n"
            "  t.me/username\n"
            "  t.me/+invitehash\n\n"
            "The account must be a member of the source.",
            None,
        )

    elif d == "fwd_stop":
        if forward_running.get(uid):
            forward_running[uid] = False
            await _edit_raw(
                cid, mid,
                "Stop signal sent. Forwarding will stop after current message.",
                RAW_KB_BACK("menu_forwarder"),
            )
        else:
            await _answer_cb(q.id, "No active forward running.", alert=True)

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
                "No valid Telegram links found.\n"
                "Send links like: t.me/username or t.me/+hash"
            )
            return

        prog = await update.message.reply_text(f"Checking {len(links)} link(s)...")

        results = []
        for i, lnk in enumerate(links):
            try:
                await prog.edit_text(f"Checking {i+1}/{len(links)}...\n{lnk}")
            except Exception:
                pass
            r = await check_single_link(lnk, uid)
            results.append(r)

        active_n  = sum(1 for r in results if r["status"] == "active")
        expired_n = sum(1 for r in results if r["status"] in ("expired", "expired/invalid", "invalid"))

        header = (
            f"LINK CHECK RESULTS\n"
            f"Total: {len(results)}  Active: {active_n}  Expired: {expired_n}\n"
            f"{'─'*36}\n"
        )

        body = ""
        chunks_to_send = []
        for i, r in enumerate(results, 1):
            block = fmt_result(r, i)
            if len(header + body + block) > 3800:
                chunks_to_send.append(header + body)
                body = block
            else:
                body += block
        chunks_to_send.append(header + body)

        await prog.delete()
        for chunk in chunks_to_send:
            await _send_raw(cid, chunk, RAW_KB_BACK())

    # ── ADD ACCOUNT: phone ──
    elif mode == "add_account":
        if not re.match(r"^\+\d{7,15}$", text):
            await update.message.reply_text("Invalid format. Example: +919876543210")
            return
        w = await update.message.reply_text(f"Sending OTP to {text}...")
        try:
            client, ph_hash, sess = await send_otp(uid, text)
            login_state[uid] = {"phone": text, "client": client, "hash": ph_hash, "session": sess}
            ctx.user_data["mode"] = "wait_otp"
            await w.edit_text(
                f"OTP sent to {text}\n\n"
                f"Enter the code you received in Telegram.\n"
                f"Format: 12345  (no spaces)"
            )
        except Exception as e:
            await w.edit_text(f"Failed to send OTP:\n{e}")

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
                f"Account added successfully: {st['phone']}",
                RAW_KB_FORWARDER(),
            )
        elif res == "need_2fa":
            ctx.user_data["mode"] = "wait_2fa"
            await update.message.reply_text("2FA enabled. Enter your Telegram password:")
        else:
            await update.message.reply_text(f"Login failed: {res}")

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
                f"Account added successfully: {st['phone']}",
                RAW_KB_FORWARDER(),
            )
        except Exception as e:
            await update.message.reply_text(f"Wrong password: {e}")

    # ── FORWARD: source ──
    elif mode == "fwd_source":
        ctx.user_data["fwd_source"] = text
        ctx.user_data["mode"] = "fwd_dest"
        await update.message.reply_text(
            f"Source set: {text}\n\n"
            f"Step 2/2 — Send the DESTINATION link (where to forward TO).\n\n"
            f"The account must be a member or admin of the destination."
        )

    # ── FORWARD: destination ──
    elif mode == "fwd_dest":
        source = ctx.user_data.get("fwd_source", "")
        ctx.user_data["mode"] = ""

        res = await _send_raw(
            cid,
            f"Starting forward...\nFrom: {source}\nTo: {text}\n\nInitializing...",
            RAW_KB_STOP(),
        )
        # Get message_id from raw response to update dashboard
        sm_id = res.get("result", {}).get("message_id")

        class _FakeMsg:
            """Proxy so forward worker can edit via raw API."""
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
                await _edit_raw(self._cid, self._mid, t, kb)

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
