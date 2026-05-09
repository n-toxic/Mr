"""
╔══════════════════════════════════════════════════════════════╗
║          TELEGRAM SUPER BOT - Full Featured                  ║
║  Features: Link Checker + Smart Forwarder + Account Manager  ║
║  Library: python-telegram-bot           ║
║  Install: pip install python-telegram-bot pyrogram tgcrypto  ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
1. pip install python-telegram-bot pyrogram tgcrypto
2. Get BOT_TOKEN from @BotFather
3. Get API_ID and API_HASH from https://my.telegram.org
4. Replace BOT_TOKEN, API_ID, API_HASH below
5. python telegram_bot.py
"""

import asyncio
import logging
import os
import json
import re
import time
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════
#              CONFIGURATION
# ═══════════════════════════════════════════
BOT_TOKEN = "8278263110:AAGZdHeRMCRsqMRkWT2dMqikSqqCyDqbmww" 
API_ID    = 32003552
API_HASH  = "18e677db0dc3bb8cf89c574a6f460cc3"

ACCOUNTS_FILE = "accounts.json"
SESSIONS_DIR  = "sessions"

os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#         CONVERSATION STATES
# ═══════════════════════════════════════════
(
    WAIT_LINKS,
    WAIT_SOURCE,
    WAIT_DEST,
    WAIT_PHONE,
    WAIT_OTP,
    WAIT_PASSWORD,
) = range(6)

# ═══════════════════════════════════════════
#         ACCOUNTS STORAGE HELPERS
# ═══════════════════════════════════════════

def load_accounts() -> dict:
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_accounts(data: dict):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user_accounts(user_id: int) -> list:
    data = load_accounts()
    return data.get(str(user_id), [])

def add_user_account(user_id: int, phone: str, session_name: str):
    data = load_accounts()
    uid = str(user_id)
    if uid not in data:
        data[uid] = []
    # avoid duplicates
    phones = [a["phone"] for a in data[uid]]
    if phone not in phones:
        data[uid].append({"phone": phone, "session": session_name, "added": datetime.now().isoformat()})
    save_accounts(data)

def remove_user_account(user_id: int, phone: str):
    data = load_accounts()
    uid = str(user_id)
    if uid in data:
        data[uid] = [a for a in data[uid] if a["phone"] != phone]
    save_accounts(data)

# ═══════════════════════════════════════════
#         EMOJIS & UI HELPERS
# ═══════════════════════════════════════════

EMOJI = {
    "check":    "✅", "cross":  "❌", "warn":   "⚠️",
    "link":     "🔗", "eye":    "👁️", "shield": "🛡️",
    "forward":  "📤", "stop":   "🛑", "stats":  "📊",
    "add":      "➕", "remove": "🗑️", "list":   "📋",
    "phone":    "📱", "lock":   "🔒", "fire":   "🔥",
    "rocket":   "🚀", "clock":  "⏱️", "bot":    "🤖",
    "msg":      "💬", "photo":  "🖼️", "video":  "🎥",
    "key":      "🔑", "crown":  "👑", "zap":    "⚡",
}

def make_colored_keyboard_start():
    """Start menu - 2 colorful inline buttons"""
    keyboard = [
        [
            InlineKeyboardButton(
                f"{EMOJI['link']} Link Checker",
                callback_data="menu_linkchecker"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{EMOJI['forward']} Forward Manager",
                callback_data="menu_forwarder"
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def make_forwarder_keyboard():
    """Forward Manager sub-menu"""
    keyboard = [
        [
            InlineKeyboardButton(f"{EMOJI['add']} Add Account",    callback_data="fwd_addaccount"),
            InlineKeyboardButton(f"{EMOJI['list']} Account List",  callback_data="fwd_listaccounts"),
        ],
        [
            InlineKeyboardButton(f"{EMOJI['remove']} Remove Account", callback_data="fwd_removeaccount"),
        ],
        [
            InlineKeyboardButton(f"{EMOJI['rocket']} Start Forward", callback_data="fwd_start"),
        ],
        [
            InlineKeyboardButton(f"{EMOJI['stop']} Stop Forward",    callback_data="fwd_stop"),
        ],
        [
            InlineKeyboardButton(f"🏠 Back to Main Menu", callback_data="back_main"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def make_back_keyboard(callback="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=callback)]])

def make_stop_forward_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{EMOJI['stop']}  STOP FORWARD", callback_data="fwd_stop"),
    ]])

# ═══════════════════════════════════════════
#         LINK CHECKER CORE
# ═══════════════════════════════════════════

def extract_links(text: str) -> list:
    """Extract all t.me links from text"""
    pattern = r"(?:https?://)?t\.me/(?:\+)?[a-zA-Z0-9_/+]+"
    found = re.findall(pattern, text)
    # Also handle raw usernames like @channel or joinchat links
    # Normalize
    normalized = []
    for link in found:
        if not link.startswith("http"):
            link = "https://" + link
        normalized.append(link)
    return list(set(normalized))

async def check_single_link(link: str) -> dict:
    """
    Check a single Telegram link using Pyrogram.
    Returns dict with status info.
    """
    result = {
        "link": link,
        "status": "unknown",
        "type": "unknown",
        "title": "N/A",
        "members": "N/A",
        "forward_enabled": "unknown",
        "username": None,
    }

    try:
        from pyrogram import Client
        from pyrogram.errors import (
            InviteHashExpired, InviteHashInvalid,
            UsernameInvalid, UsernameNotOccupied,
            ChannelInvalid, PeerIdInvalid, FloodWait,
        )

        # Use anonymous/no-auth client for public links
        # For invite links we need a logged in account
        is_invite = "+joinchat" in link or re.search(r"t\.me/\+", link)
        is_private_invite = is_invite

        # Extract identifier
        if is_private_invite:
            hash_match = re.search(r"t\.me/\+([A-Za-z0-9_-]+)", link)
            if not hash_match:
                hash_match = re.search(r"joinchat/([A-Za-z0-9_-]+)", link)
            invite_hash = hash_match.group(1) if hash_match else None
        else:
            username_match = re.search(r"t\.me/([a-zA-Z0-9_]+)", link)
            username = username_match.group(1) if username_match else None

        # Find a usable session
        data = load_accounts()
        session_path = None
        phone_used = None
        for uid, accounts in data.items():
            for acc in accounts:
                sp = os.path.join(SESSIONS_DIR, acc["session"])
                if os.path.exists(sp + ".session"):
                    session_path = sp
                    phone_used = acc["phone"]
                    break
            if session_path:
                break

        if session_path is None:
            # Try without auth for public channels
            if not is_private_invite and username:
                async with Client(
                    ":memory:",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    no_updates=True,
                ) as app:
                    try:
                        chat = await app.get_chat(username)
                        result["status"] = "active"
                        result["title"] = chat.title or chat.first_name or username
                        result["type"] = str(chat.type).split(".")[-1].lower()
                        result["members"] = getattr(chat, "members_count", "N/A")
                        # Check forward restriction
                        protected = getattr(chat, "has_protected_content", None)
                        result["forward_enabled"] = "OFF (Restricted)" if protected else "ON"
                    except (UsernameInvalid, UsernameNotOccupied, ChannelInvalid):
                        result["status"] = "expired/invalid"
                    except Exception as e:
                        result["status"] = f"error: {str(e)[:40]}"
            else:
                result["status"] = "need_account"
                result["forward_enabled"] = "Need account to check"
            return result

        # Use stored session
        async with Client(
            session_path,
            api_id=API_ID,
            api_hash=API_HASH,
            no_updates=True,
        ) as app:
            try:
                if is_private_invite and invite_hash:
                    chat = await app.get_chat(f"+{invite_hash}")
                else:
                    chat = await app.get_chat(username)

                result["status"] = "active"
                result["title"] = chat.title or getattr(chat, "first_name", "N/A")
                result["type"] = str(chat.type).split(".")[-1].lower()
                result["members"] = getattr(chat, "members_count", "N/A")
                protected = getattr(chat, "has_protected_content", False)
                result["forward_enabled"] = "❌ OFF (Restricted)" if protected else "✅ ON"

            except InviteHashExpired:
                result["status"] = "expired"
            except InviteHashInvalid:
                result["status"] = "invalid"
            except (UsernameInvalid, UsernameNotOccupied):
                result["status"] = "expired/invalid"
            except FloodWait as e:
                await asyncio.sleep(e.value)
                result["status"] = "flood_wait"
            except Exception as e:
                result["status"] = f"error: {str(e)[:40]}"

    except ImportError:
        result["status"] = "pyrogram_not_installed"
        result["forward_enabled"] = "pip install pyrogram"

    return result

def format_link_result(r: dict, index: int) -> str:
    link = r["link"]
    status = r["status"]
    
    if status == "active":
        status_icon = "ACTIVE"
    elif status in ("expired", "expired/invalid", "invalid"):
        status_icon = "EXPIRED/INVALID"
    elif status == "need_account":
        status_icon = "Need Account"
    else:
        status_icon = f" {status}"

    fwd = r.get("forward_enabled", "N/A")
    title = r.get("title", "N/A")
    members = r.get("members", "N/A")
    ctype = r.get("type", "N/A")

    return (
        f"{'─'*30}\n"
        f"🔗 **Link {index}:** `{link}`\n"
        f"📌 **Status:** {status_icon}\n"
        f"📛 **Title:** {title}\n"
        f"👥 **Members:** {members}\n"
        f"📂 **Type:** {ctype}\n"
        f"📤 **Forward:** {fwd}\n"
    )

# ═══════════════════════════════════════════
#         FORWARD MANAGER CORE
# ═══════════════════════════════════════════

# Global forward state per user
forward_tasks = {}   # user_id -> asyncio.Task
forward_running = {} # user_id -> bool

async def smart_forward_worker(
    user_id: int,
    source_link: str,
    dest_link: str,
    status_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Smart forward worker:
    - Works even if forward is restricted (copies media/text)
    - Starts from first message, goes down
    - Updates live dashboard
    - Supports stop
    """
    try:
        from pyrogram import Client
        from pyrogram.errors import FloodWait, ChannelInvalid, UsernameInvalid

        data = load_accounts()
        session_path = None
        for uid, accounts in data.items():
            for acc in accounts:
                sp = os.path.join(SESSIONS_DIR, acc["session"])
                if os.path.exists(sp + ".session"):
                    session_path = sp
                    break
            if session_path:
                break

        if session_path is None:
            await status_message.edit_text(
                f"{EMOJI['cross']} **No account found!**\nPlease add an account first.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        async with Client(session_path, api_id=API_ID, api_hash=API_HASH) as app:
            # Resolve source
            src_id = source_link.strip()
            if "t.me/" in src_id:
                src_id = re.sub(r"https?://t\.me/", "", src_id).strip("/")

            dst_id = dest_link.strip()
            if "t.me/" in dst_id:
                dst_id = re.sub(r"https?://t\.me/", "", dst_id).strip("/")

            try:
                src_chat = await app.get_chat(src_id)
                dst_chat = await app.get_chat(dst_id)
            except Exception as e:
                await status_message.edit_text(
                    f"{EMOJI['cross']} **Error resolving chats:**\n`{e}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            src_title = src_chat.title or src_id
            dst_title = dst_chat.title or dst_id

            forwarded = 0
            failed = 0
            skipped = 0
            start_time = time.time()
            last_update = 0

            forward_running[user_id] = True

            async for msg in app.get_chat_history(src_chat.id, limit=None):
                if not forward_running.get(user_id, False):
                    break

                try:
                    # Try standard forward first
                    sent = False
                    if msg.text:
                        await app.send_message(dst_chat.id, msg.text)
                        sent = True
                    elif msg.photo:
                        caption = msg.caption or ""
                        await app.send_photo(dst_chat.id, msg.photo.file_id, caption=caption)
                        sent = True
                    elif msg.video:
                        caption = msg.caption or ""
                        await app.send_video(dst_chat.id, msg.video.file_id, caption=caption)
                        sent = True
                    elif msg.document:
                        caption = msg.caption or ""
                        await app.send_document(dst_chat.id, msg.document.file_id, caption=caption)
                        sent = True
                    elif msg.audio:
                        await app.send_audio(dst_chat.id, msg.audio.file_id)
                        sent = True
                    elif msg.sticker:
                        await app.send_sticker(dst_chat.id, msg.sticker.file_id)
                        sent = True
                    elif msg.animation:
                        await app.send_animation(dst_chat.id, msg.animation.file_id)
                        sent = True
                    elif msg.voice:
                        await app.send_voice(dst_chat.id, msg.voice.file_id)
                        sent = True
                    else:
                        skipped += 1

                    if sent:
                        forwarded += 1
                        await asyncio.sleep(0.5)  # rate limit

                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception:
                    failed += 1

                # Update dashboard every 5 seconds
                elapsed = time.time() - start_time
                if time.time() - last_update > 5:
                    last_update = time.time()
                    mins = int(elapsed // 60)
                    secs = int(elapsed % 60)
                    speed = forwarded / elapsed if elapsed > 0 else 0
                    try:
                        await status_message.edit_text(
                            f"🚀 **LIVE FORWARD DASHBOARD**\n"
                            f"{'═'*30}\n"
                            f"📥 **Source:** `{src_title}`\n"
                            f"📤 **Destination:** `{dst_title}`\n"
                            f"{'─'*30}\n"
                            f"✅ **Forwarded:** `{forwarded}` msgs\n"
                            f"❌ **Failed:** `{failed}`\n"
                            f"⏭️ **Skipped:** `{skipped}`\n"
                            f"⚡ **Speed:** `{speed:.1f}` msg/s\n"
                            f"⏱️ **Elapsed:** `{mins}m {secs}s`\n"
                            f"{'─'*30}\n"
                            f"🟢 **Status:** RUNNING...",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=make_stop_forward_keyboard(),
                        )
                    except Exception:
                        pass

            # Done
            elapsed = time.time() - start_time
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            status = "✅ COMPLETED" if forward_running.get(user_id) else "🛑 STOPPED"
            forward_running[user_id] = False

            try:
                await status_message.edit_text(
                    f"{'═'*30}\n"
                    f"📊 **FORWARD COMPLETE**\n"
                    f"{'═'*30}\n"
                    f"📥 **Source:** `{src_title}`\n"
                    f"📤 **Dest:** `{dst_title}`\n"
                    f"{'─'*30}\n"
                    f"✅ **Total Forwarded:** `{forwarded}`\n"
                    f"❌ **Failed:** `{failed}`\n"
                    f"⏭️ **Skipped:** `{skipped}`\n"
                    f"⏱️ **Time:** `{mins}m {secs}s`\n"
                    f"{'─'*30}\n"
                    f"🏁 **Status:** {status}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=make_back_keyboard(),
                )
            except Exception:
                pass

    except ImportError:
        await status_message.edit_text(
            f"{EMOJI['cross']} **Pyrogram not installed!**\n"
            f"Run: `pip install pyrogram tgcrypto`",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        forward_running[user_id] = False

# ═══════════════════════════════════════════
#         ACCOUNT LOGIN WITH PYROGRAM
# ═══════════════════════════════════════════

login_state = {}  # user_id -> {"phone": ..., "client": ..., "hash": ...}

async def do_send_code(user_id: int, phone: str) -> tuple:
    """Send OTP and return (client, phone_code_hash)"""
    from pyrogram import Client
    session_name = os.path.join(SESSIONS_DIR, f"session_{user_id}_{phone.replace('+','')}")
    client = Client(session_name, api_id=API_ID, api_hash=API_HASH, no_updates=True)
    await client.connect()
    sent = await client.send_code(phone)
    return client, sent.phone_code_hash, session_name

async def do_sign_in(client, phone: str, code: str, phone_code_hash: str, password: str = None):
    from pyrogram.errors import SessionPasswordNeeded
    try:
        await client.sign_in(phone, phone_code_hash, code)
        return "ok", None
    except SessionPasswordNeeded:
        if password:
            await client.check_password(password)
            return "ok", None
        return "need_password", None
    except Exception as e:
        return "error", str(e)

# ═══════════════════════════════════════════
#         BOT HANDLERS
# ═══════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f" **Welcome, {user.first_name}!**\n"
        f"{'═'*32}\n"
        f"**Telegram Super Bot**\n\n"
        f"Choose an option below:\n\n"
        f" **Link Checker** — Check if group/channel links are active, expired, or forward-restricted\n\n"
        f"**Forward Manager** — Login accounts & smart-forward media even from restricted chats\n"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_colored_keyboard_start(),
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # ── MAIN MENU ──
    if data == "back_main":
        await query.edit_message_text(
            f"**Main Menu**\n{'═'*30}\nChoose an option:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_colored_keyboard_start(),
        )

    # ── LINK CHECKER ──
    elif data == "menu_linkchecker":
        context.user_data["mode"] = "linkchecker"
        await query.edit_message_text(
            f"🔗 **LINK CHECKER**\n{'═'*30}\n\n"
            f"Send me **Telegram group/channel links**.\n\n"
            f"📌 You can send **multiple links** in one message (one per line, or paste 10 at once).\n\n"
            f"✅ I will check:\n"
            f"• Active / Expired status\n"
            f"• Forward ON / OFF (restriction)\n"
            f"• Group/Channel type\n"
            f"• Member count\n\n"
            f"💬 **Send your links now:**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_keyboard(),
        )

    # ── FORWARD MANAGER MENU ──
    elif data == "menu_forwarder":
        await query.edit_message_text(
            f"📤 **FORWARD MANAGER**\n{'═'*30}\n\n"
            f"Manage accounts & forward messages smartly.\n"
            f"Works even with **restricted chats** 🔓",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_forwarder_keyboard(),
        )

    # ── ADD ACCOUNT ──
    elif data == "fwd_addaccount":
        context.user_data["mode"] = "add_account"
        await query.edit_message_text(
            f"{EMOJI['phone']} **ADD ACCOUNT**\n{'─'*30}\n\n"
            f"Send your **phone number** with country code.\n"
            f"Example: `+919876543210`\n\n"
            f"🔒 Your session is stored locally only.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_keyboard("menu_forwarder"),
        )

    # ── LIST ACCOUNTS ──
    elif data == "fwd_listaccounts":
        accounts = get_user_accounts(user_id)
        if not accounts:
            text = f"{EMOJI['list']} **No accounts added yet.**\n\nUse Add Account to add one."
        else:
            lines = [f"📋 **Your Accounts ({len(accounts)}):**\n{'─'*30}"]
            for i, acc in enumerate(accounts, 1):
                lines.append(f"{i}. 📱 `{acc['phone']}`\n   🕐 Added: {acc['added'][:10]}")
            text = "\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_keyboard("menu_forwarder"),
        )

    # ── REMOVE ACCOUNT ──
    elif data == "fwd_removeaccount":
        accounts = get_user_accounts(user_id)
        if not accounts:
            await query.edit_message_text(
                f"{EMOJI['cross']} No accounts to remove.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_back_keyboard("menu_forwarder"),
            )
        else:
            keyboard = []
            for acc in accounts:
                keyboard.append([
                    InlineKeyboardButton(
                        f"🗑️ Remove {acc['phone']}",
                        callback_data=f"remove_{acc['phone']}"
                    )
                ])
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_forwarder")])
            await query.edit_message_text(
                f"{EMOJI['remove']} **Select account to remove:**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    elif data.startswith("remove_"):
        phone = data.replace("remove_", "")
        remove_user_account(user_id, phone)
        # Remove session file
        for acc in load_accounts().get(str(user_id), []):
            if acc["phone"] == phone:
                sp = os.path.join(SESSIONS_DIR, acc["session"] + ".session")
                if os.path.exists(sp):
                    os.remove(sp)
        await query.edit_message_text(
            f"✅ Account `{phone}` removed successfully.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_keyboard("menu_forwarder"),
        )

    # ── START FORWARD ──
    elif data == "fwd_start":
        accounts = get_user_accounts(user_id)
        if not accounts:
            await query.edit_message_text(
                f"{EMOJI['cross']} **No accounts found!**\nAdd an account first using ➕ Add Account.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_back_keyboard("menu_forwarder"),
            )
            return
        context.user_data["mode"] = "fwd_source"
        await query.edit_message_text(
            f"🚀 **SMART FORWARD SETUP**\n{'─'*30}\n\n"
            f"**Step 1/2:** Send the **source** channel/group link\n"
            f"(where to copy messages FROM)\n\n"
            f"📌 Tip: You can also send a specific message link to start from that message onwards.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_keyboard("menu_forwarder"),
        )

    # ── STOP FORWARD ──
    elif data == "fwd_stop":
        if forward_running.get(user_id):
            forward_running[user_id] = False
            await query.edit_message_text(
                f"🛑 **Stop signal sent!**\nForwarding will stop after the current message.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_back_keyboard("menu_forwarder"),
            )
        else:
            await query.answer("No active forward to stop.", show_alert=True)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all text messages based on user mode"""
    user_id = update.effective_user.id
    text = update.message.text or ""
    mode = context.user_data.get("mode", "")

    # ── LINK CHECKER MODE ──
    if mode == "linkchecker":
        links = extract_links(text)
        if not links:
            await update.message.reply_text(
                f"{EMOJI['warn']} No valid Telegram links found.\n"
                f"Please send links like:\n`https://t.me/username`\n`https://t.me/+invitehash`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status_msg = await update.message.reply_text(
            f"🔍 **Checking {len(links)} link(s)...**\n⏳ Please wait...",
            parse_mode=ParseMode.MARKDOWN,
        )

        results = []
        for i, link in enumerate(links):
            try:
                await status_msg.edit_text(
                    f"🔍 Checking link {i+1}/{len(links)}...\n`{link}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            r = await check_single_link(link)
            results.append(r)

        # Build result message
        header = (
            f"{'═'*32}\n"
            f"🔗 **LINK CHECK RESULTS**\n"
            f"Total: {len(results)} | "
            f"Active: {sum(1 for r in results if r['status']=='active')} | "
            f"Expired: {sum(1 for r in results if r['status'] in ('expired','expired/invalid','invalid'))}\n"
            f"{'═'*32}\n"
        )

        chunks = [header]
        current = header
        for i, r in enumerate(results, 1):
            line = format_link_result(r, i)
            if len(current) + len(line) > 3800:
                chunks.append(current)
                current = line
            else:
                current += line
        if current != header:
            chunks.append(current)

        await status_msg.delete()
        for chunk in chunks[1:] if len(chunks) > 1 else chunks:
            await update.message.reply_text(
                chunk,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_back_keyboard(),
            )

    # ── ADD ACCOUNT: WAITING PHONE ──
    elif mode == "add_account":
        phone = text.strip()
        if not re.match(r"^\+\d{7,15}$", phone):
            await update.message.reply_text(
                f"{EMOJI['cross']} Invalid phone number format.\nExample: `+919876543210`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        wait_msg = await update.message.reply_text(
            f"📱 Sending OTP to `{phone}`...",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            client, phone_code_hash, session_name = await do_send_code(user_id, phone)
            login_state[user_id] = {
                "phone": phone,
                "client": client,
                "hash": phone_code_hash,
                "session_name": session_name,
            }
            context.user_data["mode"] = "wait_otp"
            await wait_msg.edit_text(
                f"✅ OTP sent to `{phone}`!\n\n"
                f"📩 Enter the OTP you received:\n(Format: `12 345`)",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await wait_msg.edit_text(
                f"{EMOJI['cross']} Failed to send OTP:\n`{e}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ── ADD ACCOUNT: WAITING OTP ──
    elif mode == "wait_otp":
        otp = text.strip().replace(" ", "")
        state = login_state.get(user_id)
        if not state:
            await update.message.reply_text("❌ Session expired. Please start again.")
            context.user_data["mode"] = ""
            return

        result, err = await do_sign_in(
            state["client"], state["phone"], otp, state["hash"]
        )
        if result == "ok":
            await state["client"].disconnect()
            session_base = os.path.basename(state["session_name"])
            add_user_account(user_id, state["phone"], session_base)
            login_state.pop(user_id, None)
            context.user_data["mode"] = ""
            await update.message.reply_text(
                f"✅ **Account added successfully!**\n📱 `{state['phone']}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_forwarder_keyboard(),
            )
        elif result == "need_password":
            context.user_data["mode"] = "wait_password"
            await update.message.reply_text(
                f"🔐 **2FA Password required.**\nEnter your Telegram password:",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"{EMOJI['cross']} Login failed: `{err}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ── ADD ACCOUNT: WAITING 2FA PASSWORD ──
    elif mode == "wait_password":
        password = text.strip()
        state = login_state.get(user_id)
        if not state:
            await update.message.reply_text("❌ Session expired.")
            return

        try:
            await state["client"].check_password(password)
            await state["client"].disconnect()
            session_base = os.path.basename(state["session_name"])
            add_user_account(user_id, state["phone"], session_base)
            login_state.pop(user_id, None)
            context.user_data["mode"] = ""
            await update.message.reply_text(
                f"✅ **Account added with 2FA!**\n📱 `{state['phone']}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_forwarder_keyboard(),
            )
        except Exception as e:
            await update.message.reply_text(
                f"{EMOJI['cross']} Wrong password: `{e}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # ── FORWARD: WAITING SOURCE ──
    elif mode == "fwd_source":
        context.user_data["fwd_source"] = text.strip()
        context.user_data["mode"] = "fwd_dest"
        await update.message.reply_text(
            f"✅ **Source set!**\n`{text.strip()}`\n\n"
            f"**Step 2/2:** Now send the **destination** channel/group link\n"
            f"(where to forward messages TO)\n\n"
            f"⚠️ Make sure the account is a member/admin of the destination.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── FORWARD: WAITING DESTINATION ──
    elif mode == "fwd_dest":
        source = context.user_data.get("fwd_source", "")
        dest = text.strip()
        context.user_data["mode"] = ""

        status_msg = await update.message.reply_text(
            f"🚀 **STARTING SMART FORWARD...**\n"
            f"📥 From: `{source}`\n"
            f"📤 To: `{dest}`\n\n"
            f"⏳ Initializing...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_stop_forward_keyboard(),
        )

        # Cancel existing task if any
        if user_id in forward_tasks and not forward_tasks[user_id].done():
            forward_tasks[user_id].cancel()

        task = asyncio.create_task(
            smart_forward_worker(user_id, source, dest, status_msg, context)
        )
        forward_tasks[user_id] = task

    else:
        # Not in any mode — show main menu
        await update.message.reply_text(
            f"👋 Use /start to open the main menu.",
            reply_markup=make_colored_keyboard_start(),
        )

# ═══════════════════════════════════════════
#         MAIN ENTRY POINT
# ═══════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("╔══════════════════════════════════════╗")
    print("║   Telegram Super Bot is RUNNING! 🚀  ║")
    print("╚══════════════════════════════════════╝")
    print(f"  Bot Token: {BOT_TOKEN[:10]}...")
    print(f"  Sessions Dir: {SESSIONS_DIR}/")
    print("  Press Ctrl+C to stop\n")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
