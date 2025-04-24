import os
import logging
import asyncio
import base64
from datetime import datetime, timedelta

import pytz
import tzlocal
from dotenv import load_dotenv

# ─── Load environment variables ─────────────────────────────────────────────────
load_dotenv()

# ─── Timezone setup ────────────────────────────────────────────────────────────
os.environ["TZLOCAL_FORCE_PYTZ"] = "1"
tzlocal.get_localzone = lambda: pytz.UTC
ist = pytz.timezone("Asia/Kolkata")

# ─── Monkey-patch APScheduler’s astimezone ───────────────────────────────────────
import apscheduler.util as aps_util
import apscheduler.schedulers.base as aps_base

def patched_astimezone(tz):
    if tz is None:
        return pytz.UTC
    if hasattr(tz, "zone"):
        return tz
    if hasattr(tz, "key"):
        try:
            return pytz.timezone(tz.key)
        except Exception:
            return pytz.UTC
    return pytz.UTC

aps_util.astimezone = patched_astimezone
aps_base.astimezone = patched_astimezone

# ─── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
for noisy in ("httpx", "telethon", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─── Load Google Sheets credentials from Base64 ─────────────────────────────────
key_b64 = os.getenv("GSA_KEY_B64")
if not key_b64:
    raise RuntimeError("Missing GSA_KEY_B64 environment variable!")

creds_bytes = base64.b64decode(key_b64)
creds_path = "/tmp/credentials.json"
with open(creds_path, "wb") as f:
    f.write(creds_bytes)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path

# ─── Load other config ──────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
GROUP_USERNAME = os.getenv("GROUP_USERNAME")
OWNER_USERNAME = os.getenv("OWNER_USERNAME")
SHEET_ID = os.getenv("SHEET_ID")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "session")
SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")

# Validate essential configs
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable!")
if not API_ID or not API_HASH:
    raise RuntimeError("Missing API_ID or API_HASH environment variable!")
if not SESSION_STRING:
    raise RuntimeError(
        "Missing TELETHON_SESSION_STRING. Generate a StringSession for your user account using Telethon locally and set this env var to fetch channel history."
    )
API_ID = int(API_ID)

# ─── Google Sheets auth ─────────────────────────────────────────────────────────
import gspread
try:
    gc = gspread.service_account(filename=creds_path)
    sh = gc.open_by_key(SHEET_ID)
except Exception as e:
    logger.error(f"Could not open Google Sheet {SHEET_ID}: {e}")
    raise

worksheet_title = "Apply Links"
try:
    sheet = sh.worksheet(worksheet_title)
except gspread.exceptions.WorksheetNotFound:
    sheet = sh.add_worksheet(title=worksheet_title, rows="1000", cols="5")

# Ensure header row
try:
    sheet.update(
        values=[["Name", "Username", "Batch", "Date", "Time"]],
        range_name="A1:E1"
    )
except Exception as e:
    logger.warning(f"Could not set header row: {e}")

# ─── Telethon client setup (user session required) ───────────────────────────────
from telethon import TelegramClient
from telethon.sessions import StringSession

tele_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
tele_client.start()

# ─── python-telegram-bot setup ───────────────────────────────────────────────────
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    ApplicationBuilder,
    Defaults,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# Conversation state
BATCH = 1

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_USERNAME}"),
            InlineKeyboardButton("Join Group",   url=f"https://t.me/{GROUP_USERNAME}")
        ],
        [InlineKeyboardButton("✅ Check", callback_data="check")]
    ])
    await update.message.reply_text(
        "Welcome! To proceed, please join both our channel and group, then tap ✅ Check.",
        reply_markup=kb
    )

async def check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    chan_member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
    grp_member = await context.bot.get_chat_member(f"@{GROUP_USERNAME}", user_id)
    allowed = {ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR}

    if chan_member.status in allowed and grp_member.status in allowed:
        await query.message.reply_text("✅ Thanks! Now please enter your Batch Year/Graduation Year:")
        return BATCH
    else:
        await query.message.reply_text("❌ You must join both the channel and group to proceed.")
        return ConversationHandler.END

async def batch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batch = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id
    full_name = user.full_name
    username = user.username or ""

    await update.message.reply_text(
        "Thanks! Your batch is noted. I’m fetching the Apply Links now — you’ll get them shortly."
    )

    asyncio.create_task(
        fetch_and_send_apply_links(
            context.bot, chat_id, full_name, username, batch
        )
    )
    return ConversationHandler.END

async def fetch_and_send_apply_links(bot, chat_id, full_name, username, batch):
    now = datetime.now(ist)
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%I:%M:%S %p")

    # 1) Local log
    try:
        await asyncio.to_thread(
            lambda: open("data.txt", "a", encoding="utf-8").write(
                f"{full_name},{username},{batch},{date_str},{time_str}\n"
            )
        )
    except Exception as e:
        logger.error(f"Failed to write data.txt: {e}")

    # 2) Google Sheets
    try:
        await asyncio.to_thread(
            sheet.append_row,
            [full_name, username, batch, date_str, time_str],
            'RAW'
        )
    except Exception as e:
        logger.error(f"Failed to append to Google Sheet: {e}")

    # 3) Telethon fetch
    now_utc = datetime.now(pytz.UTC)
    min_date = now_utc - timedelta(days=30)
    found_any = False

    try:
        with open("groups.txt", encoding="utf-8") as gf:
            group_usernames = [ln.strip().split("/")[-1] for ln in gf if ln.strip()]
    except FileNotFoundError:
        group_usernames = []

    for entity_username in group_usernames:
        try:
            entity = await tele_client.get_entity(entity_username)
        except Exception as e:
            logger.warning(f"Could not load Telethon entity @{entity_username}: {e}")
            continue

        async for msg in tele_client.iter_messages(entity, limit=200):
            if not msg.text:
                continue
            post_date = msg.date.astimezone(pytz.UTC)
            if post_date < min_date:
                continue
            if batch.lower() not in msg.text.lower():
                continue
            post_date_ist = post_date.astimezone(ist)
            prefix = post_date_ist.strftime(
                f"This message was posted on @{entity_username} at %d/%m/%Y at %I:%M:%S %p IST.\n\n"
            )
            await bot.send_message(chat_id, prefix + msg.text)
            found_any = True

    if not found_any:
        await bot.send_message(
            chat_id,
            f"No recent posts (within 1 month) found for batch {batch}. "
            f"If you have questions, please DM the owner: @{OWNER_USERNAME}"
        )

# ─── Bot setup ────────────────────────────────────────────────────────────────
def main():
    defaults = Defaults(tzinfo=pytz.UTC)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(10)
        .defaults(defaults)
        .concurrent_updates(1000)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_handler),
            CallbackQueryHandler(check_handler, pattern="^check$"),
        ],
        states={BATCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, batch_handler)]},
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv)

    logger.info("Bot is running…")
    app.run_polling()

if __name__ == "__main__":
    main()
