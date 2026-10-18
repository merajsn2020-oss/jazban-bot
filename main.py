import os
import re
import asyncio
from contextlib import contextmanager
import html
import sqlite3
import logging
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# These values must be configured in your hosting provider's environment.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API_ID_RAW = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
DB = os.environ.get("JAZBAN_DB", "jazban.db")
TZ = ZoneInfo("Asia/Tehran")
BONUS_EVERY = 40
BONUS_TOMAN = 200_000
TARGET_TOTAL = 200
OWNER_ID = 0
client = None
awaiting_admin = set()
_notification_flush_lock = asyncio.Lock()


@contextmanager
def db():
    """Open a short-lived SQLite connection and always close it."""
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def setup_db():
    """Create missing tables and migrate existing DB without deleting data."""
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, code TEXT UNIQUE,
            count INTEGER DEFAULT 0, total INTEGER DEFAULT 0, activated_at TEXT,
            training_done INTEGER DEFAULT 0, notified_200 INTEGER DEFAULT 0
        )""")
        admin_cols = {r["name"] for r in c.execute("PRAGMA table_info(admins)")}
        admin_migrations = {
            "username": "ALTER TABLE admins ADD COLUMN username TEXT",
            "name": "ALTER TABLE admins ADD COLUMN name TEXT",
            "code": "ALTER TABLE admins ADD COLUMN code TEXT",
            "count": "ALTER TABLE admins ADD COLUMN count INTEGER DEFAULT 0",
            "total": "ALTER TABLE admins ADD COLUMN total INTEGER DEFAULT 0",
            "activated_at": "ALTER TABLE admins ADD COLUMN activated_at TEXT",
            "training_done": "ALTER TABLE admins ADD COLUMN training_done INTEGER DEFAULT 0",
            "notified_200": "ALTER TABLE admins ADD COLUMN notified_200 INTEGER DEFAULT 0",
        }
        for col, sql in admin_migrations.items():
            if col not in admin_cols:
                c.execute(sql)
        c.execute("""CREATE TABLE IF NOT EXISTS leads (
            user_id INTEGER PRIMARY KEY, admin_code TEXT, created_at TEXT,
            admin_user_id INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS notification_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
            dedupe_key TEXT
        )""")
        queue_cols = {r["name"] for r in c.execute("PRAGMA table_info(notification_queue)")}
        for col, sql in {
            "attempts": "ALTER TABLE notification_queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "ALTER TABLE notification_queue ADD COLUMN next_attempt_at TEXT",
            "dedupe_key": "ALTER TABLE notification_queue ADD COLUMN dedupe_key TEXT",
        }.items():
            if col not in queue_cols:
                c.execute(sql)
        lead_cols = {r["name"] for r in c.execute("PRAGMA table_info(leads)")}
        for col, sql in {
            "admin_code": "ALTER TABLE leads ADD COLUMN admin_code TEXT",
            "created_at": "ALTER TABLE leads ADD COLUMN created_at TEXT",
            "admin_user_id": "ALTER TABLE leads ADD COLUMN admin_user_id INTEGER",
        }.items():
            if col not in lead_cols:
                c.execute(sql)
        # Preserve historical rows while associating them with a stable admin ID.
        c.execute("""UPDATE leads SET admin_user_id=(
                    SELECT user_id FROM admins WHERE admins.code=leads.admin_code
                ) WHERE admin_user_id IS NULL""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_dedupe
                     ON notification_queue(dedupe_key) WHERE dedupe_key IS NOT NULL""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_leads_admin_code ON leads(admin_code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_leads_admin_user_id ON leads(admin_user_id)")


def new_code(conn=None):
    if conn is None:
        with db() as c:
            return new_code(c)
    used = {str(r["code"]) for r in conn.execute(
        "SELECT code FROM admins WHERE code IS NOT NULL"
    )}
    n = 10
    while str(n) in used:
        n += 1
    return str(n)


def escape(value):
    return html.escape(str(value or ""))


def enqueue_tx(c, chat_id, message, dedupe_key=None):
    now = datetime.now(TZ).isoformat()
    c.execute("""INSERT OR IGNORE INTO notification_queue
        (chat_id, message, created_at, attempts, next_attempt_at, dedupe_key)
        VALUES (?, ?, ?, 0, ?, ?)""",
        (int(chat_id), str(message), now, now, dedupe_key))


async def enqueue_notification(chat_id, message, dedupe_key=None):
    with db() as c:
        enqueue_tx(c, chat_id, message, dedupe_key)


async def flush_notification_queue(bot, limit=50):
    """Retry queued messages with backoff; serialize flushes to reduce duplicate sends."""
    async with _notification_flush_lock:
        try:
            now = datetime.now(TZ).isoformat()
            with db() as c:
                rows = c.execute("""SELECT id, chat_id, message, attempts
                    FROM notification_queue
                    WHERE next_attempt_at IS NULL OR next_attempt_at <= ?
                    ORDER BY id LIMIT ?""", (now, limit)).fetchall()
            for row in rows:
                try:
                    await bot.send_message(chat_id=row["chat_id"], text=row["message"])
                except Exception as exc:
                    attempts = int(row["attempts"] or 0) + 1
                    delay = min(60 * (2 ** min(attempts, 6)), 3600)
                    next_time = (datetime.now(TZ) + timedelta(seconds=delay)).isoformat()
                    logger.warning("Queued notification %s failed (attempt %s): %s", row["id"], attempts, exc)
                    with db() as c:
                        c.execute("UPDATE notification_queue SET attempts=?, next_attempt_at=? WHERE id=?",
                                  (attempts, next_time, row["id"]))
                else:
                    with db() as c:
                        c.execute("DELETE FROM notification_queue WHERE id=?", (row["id"],))
        except Exception:
            logger.exception("Notification queue processing failed")


async def safe_owner_message(bot, message, dedupe_key=None):
    await enqueue_notification(OWNER_ID, message, dedupe_key)
    await flush_notification_queue(bot)


def main_menu():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("ð Ø¢ÙØ§Ø± ÙÙ", callback_data="my_stats"),
        InlineKeyboardButton("ð¢ Ú©Ø¯ ÙÙ", callback_data="my_code"),
    ]])


def welcome_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("ðØ´Ø±ÙØ¹ Ø¢ÙÙØ²Ø´ð", callback_data="training_start")
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    uid = user.id
    if uid == OWNER_ID:
        await message.reply_text("ð¯ Ø¬Ø°Ø¨Ø§Ù Ø¢ÙØ§Ø¯ÙâØ³Øª.\n\n/addadmin - Ø«Ø¨Øª Ø§Ø¯ÙÛÙ Ø¬Ø¯ÛØ¯\n/stats - Ø¢ÙØ§Ø± Ø§Ø¯ÙÛÙâÙØ§\n/cancel - ÙØºÙ Ø¹ÙÙÛØ§Øª")
        return
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
        if not admin:
            await message.reply_text("Ø´ÙØ§ ÙÙÙØ² Ø¨ÙâØ¹ÙÙØ§Ù Ø§Ø¯ÙÛÙ Ø«Ø¨Øª ÙØ´Ø¯ÙâØ§ÛØ¯.")
            return
        code = admin["code"] or new_code(c)
        c.execute("UPDATE admins SET code=?, username=?, name=? WHERE user_id=?",
                  (code, user.username, user.first_name or str(uid), uid))
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
    if admin["training_done"]:
        await message.reply_text("Ø¢ÙÙØ²Ø´ Ø´ÙØ§ ÙØ¨ÙØ§Ù ØªÚ©ÙÛÙ Ø´Ø¯Ù. Ø§Ø² ÙÙÙÛ Ø²ÛØ± Ø§Ø³ØªÙØ§Ø¯Ù Ú©Ù.", reply_markup=main_menu())
    else:
        await message.reply_text(
            "Ø³ÙØ§Ù Ø¯ÙØ³Øª Ø¹Ø²ÛØ²Ù\nØ¨Ù Ø¬ÙØ¹ Ø§Ø¯ÙÛÙâÙØ§Û Ø¬Ø°Ø¨Ø§Ù Ø®ÙØ´ Ø§ÙÙØ¯Û â­ï¸â¤ï¸\n\n"
            "ÙÙ ÙØ±Ø§Ø±Ù ÙØ±Ø­ÙÙâØ¨ÙâÙØ±Ø­ÙÙ Ú©Ø§Ø± Ø±Ù Ø¨ÙØª Ø¢ÙÙØ²Ø´ Ø¨Ø¯Ù ØªØ§ Ø¨ØªÙÙÛ Ø´Ø±ÙØ¹ Ú©ÙÛ Ù Ø¯Ø±Ø¢ÙØ¯ Ø¯Ø§Ø´ØªÙ Ø¨Ø§Ø´Û.\n\n"
            "ÙØ± Ø³ÙØ§ÙÛ ÙÙ Ø¯Ø§Ø´ØªÛ ÙÛØªÙÙÛ Ø§Ø² Ú©Ø§Ø±ÙØ±ÙØ§Øª Ø¨Ù¾Ø±Ø³Û\n\nØ¢ÙØ§Ø¯ÙâØ§Û Ø´Ø±ÙØ¹ Ú©ÙÛÙØ",
            reply_markup=welcome_keyboard())


async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    awaiting_admin.add(OWNER_ID)
    await update.effective_message.reply_text("Ø¢ÛØ¯Û Ø¹Ø¯Ø¯Û Ø§Ø¯ÙÛÙ Ø±Ù Ø¨ÙØ±Ø³Øª ÛØ§ Ù¾ÛØ§Ù Ø®ÙØ¯Ø´ Ø±Ù ÙÙØ±ÙØ§Ø±Ø¯ Ú©Ù.")


async def receive_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message:
        return
    if update.effective_user.id != OWNER_ID or OWNER_ID not in awaiting_admin:
        return
    msg = update.effective_message
    origin = getattr(msg, "forward_origin", None)
    target = getattr(origin, "sender_user", None) if origin else None
    if not target and msg.text and msg.text.strip().isdigit():
        target_id = int(msg.text.strip())
        if target_id <= 0:
            await msg.reply_text("Ø¢ÛØ¯Û Ø¹Ø¯Ø¯Û ÙØ¹ØªØ¨Ø± Ø¨ÙØ±Ø³Øª.")
            return
        with db() as c:
            c.execute("INSERT OR IGNORE INTO admins (user_id, name) VALUES (?, ?)", (target_id, str(target_id)))
        awaiting_admin.discard(OWNER_ID)
        await msg.reply_text("Ø§Ø¯ÙÛÙ Ø«Ø¨Øª Ø´Ø¯. Ø¨ÙØ´ Ø¨Ú¯Ù Ø±Ø¨Ø§Øª Ø±Ù Ø§Ø³ØªØ§Ø±Øª Ú©ÙÙ.")
        try:
            await context.bot.send_message(target_id, "Ø¨Ø±Ø§Û Ø´Ø±ÙØ¹ Ø¢ÙÙØ²Ø´ Ù ÙØ¹Ø§ÙâØ³Ø§Ø²ÛØ /start Ø±Ù Ø¨Ø²Ù.")
        except Exception as exc:
            logger.warning("Could not notify new admin %s: %s", target_id, exc)
            await enqueue_notification(target_id, "Ø¨Ø±Ø§Û Ø´Ø±ÙØ¹ Ø¢ÙÙØ²Ø´ Ù ÙØ¹Ø§ÙâØ³Ø§Ø²ÛØ /start Ø±Ù Ø¨Ø²Ù.", f"new-admin:{target_id}")
        return
    if not target:
        await msg.reply_text("Ø¢ÛØ¯Û Ø¹Ø¯Ø¯Û ÛØ§ Ù¾ÛØ§Ù ÙÙØ±ÙØ§Ø±Ø¯Ø´Ø¯Ù Ø±Ù Ø¨ÙØ±Ø³Øª.")
        return
    with db() as c:
        c.execute("INSERT OR IGNORE INTO admins (user_id, username, name) VALUES (?, ?, ?)",
                  (target.id, target.username, target.first_name or str(target.id)))
    awaiting_admin.discard(OWNER_ID)
    await msg.reply_text(f"Ø§Ø¯ÙÛÙ {target.first_name or target.id} Ø«Ø¨Øª Ø´Ø¯. Ø¨ÙØ´ Ø¨Ú¯Ù Ø±Ø¨Ø§Øª Ø±Ù Ø§Ø³ØªØ§Ø±Øª Ú©ÙÙ.")
    try:
        await context.bot.send_message(target.id, "Ø¨Ø±Ø§Û Ø´Ø±ÙØ¹ Ø¢ÙÙØ²Ø´ Ù ÙØ¹Ø§ÙâØ³Ø§Ø²ÛØ /start Ø±Ù Ø¨Ø²Ù.")
    except Exception as exc:
        logger.warning("Could not notify new admin %s: %s", target.id, exc)
        await enqueue_notification(target.id, "Ø¨Ø±Ø§Û Ø´Ø±ÙØ¹ Ø¢ÙÙØ²Ø´ Ù ÙØ¹Ø§ÙâØ³Ø§Ø²ÛØ /start Ø±Ù Ø¨Ø²Ù.", f"new-admin:{target.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == OWNER_ID:
        awaiting_admin.discard(OWNER_ID)
        await update.effective_message.reply_text("Ø¹ÙÙÛØ§Øª ÙØºÙ Ø´Ø¯.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    with db() as c:
        rows = c.execute("SELECT * FROM admins ORDER BY total DESC").fetchall()
    if not rows:
        await update.effective_message.reply_text("ÙÙÙØ² Ø§Ø¯ÙÛÙÛ Ø«Ø¨Øª ÙØ´Ø¯Ù.")
        return
    lines = ["ð Ø¢ÙØ§Ø± Ø¬Ø°Ø¨Ø§Ù"]
    for a in rows:
        lines.append(f"{a['name'] or a['user_id']} (@{a['username'] or 'ÙØ¯Ø§Ø±Ø¯'})\n"
                     f"Ú©Ø¯: {a['code'] or 'ÙÙÙØ² ÙØ¹Ø§Ù ÙØ´Ø¯Ù'} | Ø§ÛÙ Ø¯ÙØ±Ù: {a['count']} | Ú©Ù: {a['total']}")
    await update.effective_message.reply_text("\n\n".join(lines))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
    if not admin:
        await query.message.reply_text("Ø´ÙØ§ ÙÙÙØ² Ø¨ÙâØ¹ÙÙØ§Ù Ø§Ø¯ÙÛÙ Ø«Ø¨Øª ÙØ´Ø¯ÙâØ§ÛØ¯.")
        return
    action = query.data

    if action == "training_start":
        await query.message.reply_text(
            "<b>ÙØ±Ø­ÙÙ Ø§ÙÙ</b>\n\nØ¯ÙØ³Øª Ø®ÙØ¨ÙØ ÙØ±Ø­ÙÙ Ø§ÙÙ Ú©Ø§Ø± Ø´ÙØ§ Ø§ÛÙÙ Ú©Ù ÙØªÙÛ Ú©Ù Ø¯Ø± Ø§Ø¯Ø§ÙÙ Ø¨ÙØªÙÙ ÙÛØ¯Ù Ø±Ù Ú©Ù¾Û Ú©ÙÛØ¯ Ù ÙØ§Ø±Ø¯ Ø§ÛÙØ³ØªØ§Ú¯Ø±Ø§Ù Ø¨Ø´ÛØ¯.\n\n"
            "Ø¨Ø§ Ù¾ÛØ¬ Ø´Ø®ØµÛ ÛØ§ ÙØ± Ù¾ÛØ¬Û Ú©Ù ÙØ§ÛÙ Ø¨ÙØ¯ÛØ¯Ø Ø²ÛØ± Ù¾Ø³Øª Ø§ÙØ±Ø§Ø¯ ÙØ¹Ø±ÙÙ ÙØ«Ù Ø¢Ø±ØªÛØ³ØªâÙØ§ Ù Ø¨ÙØ§Ú¯Ø±ÙØ§Ø ÙØªÙ Ø±Ù Ú©Ø§ÙÙØª Ú©ÙÛØ¯\nð <b>ÙØªÙ Ú©Ø§ÙÙØª</b> ð",
            parse_mode="HTML")
        await query.message.reply_text(
            "ÙÛØ§Ø²ÙÙØ¯ ÚÙØ¯ Ø§Ù¾Ø±Ø§ØªÙØ± Ù¾Ø§Ø³Ø®Ú¯ÙÛÛ\nØ¯Ø±Ø¢ÙØ¯ Ø±ÙØ²Ø§ÙÙ Ø¨ÛÙ Û²Û°Û° ØªØ§ Û±.Û²Û°Û° ØªÙÙØ§Ù\nÙØ²ÛÙÙ Ø§Ø³ØªØ®Ø¯Ø§Ù Ú©Ø§ÙÙØ§ Ø±Ø§ÛÚ¯Ø§Ù!\n\n(Ø¯Ø§ÛØ±Ú©Øª Ø¯Ø±Ø®Ø¯ÙØªÙ)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ÙØªÙØ¬Ù Ø´Ø¯ÙØ Ø¨Ø±ÛÙ ÙØ±Ø­ÙÙ Ø¨Ø¹Ø¯... â¡ï¸", callback_data="training_stage2")]]))
        return

    if action == "training_stage2":
        await query.message.reply_text(
            "<b>ÙØ±Ø­ÙÙ Ø¯ÙÙ: Ù¾Ø§Ø³Ø®Ú¯ÙÛÛ Ø¨Ù Ø§ÙØ±Ø§Ø¯</b> ð¬\n\nØ¨Ø¹Ø¯ Ø§Ø² Ø§ÛÙÚ©Ù Ú©Ø§ÙÙØª Ú¯Ø°Ø§Ø´ØªÛØ¯Ø ÙØ±Ø§Ø¬Ø¹ÛÙ Ø¨Ù Ø¯Ø§ÛØ±Ú©ØªØªÙÙ Ù¾ÛØ§Ù ÙÛØ¯Ù ØªØ§ Ø¯Ø±Ø¨Ø§Ø±Ù Ú©Ø§Ø± Ø³ÙØ§Ù Ø¨Ù¾Ø±Ø³Ù\n\nØ´ÙØ§ Ø¯ÙØªØ§ ÙØªÙ Ø²ÛØ± Ø±Ù Ø¨Ù Ø¹ÙÙØ§Ù ØªÙØ¶ÛØ­ Ú©Ø§Ø± Ø¨Ø±Ø§Ø´ÙÙ Ø§Ø±Ø³Ø§Ù ÙÛÚ©ÙÛØ¯ð",
            parse_mode="HTML")
        await query.message.reply_text(
            "Ø³ÙØ§Ù Ø¯ÙØ³Øª Ø®ÙØ¨Ù â¤ï¸\n\nØ§ÛÙ Ú©Ø§Ø± Ø´Ø±Ø§ÛØ· Ø®Ø§ØµÛ ÙØ¯Ø§Ø±Ù Ù ÙÙØ· Ø¨Ù ÚÙØ¯ Ø³Ø§Ø¹Øª Ø²ÙØ§Ù Ø¢Ø²Ø§Ø¯ Ø¯Ø± Ø±ÙØ² Ø¨Ø±Ø§Û Ù¾Ø§Ø³Ø®Ú¯ÙÛÛ Ø¨Ù ÙØ±Ø§Ø¬Ø¹ÛÙ Ø¯Ø§Ø®Ù Ù¾ÙØªÙØ±Ù Ú©Ø§Ø±Û ÙÛØ§Ø² Ø¯Ø§Ø±Ù.\n\n"
            "Ø­ÙÙÙ Ø±ÙØ²Ø§ÙÙ Ø´ÙØ§ Ø¨ÛÙ Û²Û°Û° ÙØ²Ø§Ø± ØªØ§ Û± ÙÛÙÛÙÙ Ù Û²Û°Û° ÙØ²Ø§Ø± ØªÙÙØ§Ù ÙØ³Øª Ù Ù¾Ø±Ø¯Ø§Ø®Øª Ø¨ÙâØµÙØ±Øª ÙÙØ¸Ù Ø§ÙØ¬Ø§Ù ÙÛâØ´Ù.\n\nð¥ Ø¸Ø±ÙÛØª Ø§Ø³ØªØ®Ø¯Ø§Ù: Û²Û° ÙÙØ±")
        code = escape(admin["code"])
        await query.message.reply_text(
            "Ø¬ÙØª Ø§Ø³ØªØ®Ø¯Ø§Ù Ù Ø¯Ø±ÛØ§ÙØª Ø§Ø·ÙØ§Ø¹Ø§Øª Ø¨ÛØ´ØªØ± ÙØ·ÙØ§ Ø¹Ø¨Ø§Ø±Øª: "
            f"(Ø§Ø³ØªØ®Ø¯Ø§Ù Ú©Ø¯{code})\nØ±Ù Ø¨Ù Ø§ÛØ¯Û Ø²ÛØ± Ø¯Ø§Ø®Ù ØªÙÚ¯Ø±Ø§Ù Ø§Ø±Ø³Ø§Ù ÙÙØ§ÛÛØ¯ð\n@jazban_work\n\n"
            "â ï¸ ØªÙØ¬Ù: Ø§Ú¯Ø± Ø¹Ø¨Ø§Ø±Øª Ø¨Ø§ÙØ§Ø±Ù Ø§Ø±Ø³Ø§Ù ÙÚ©ÙÛØ¯ ÛØ§ ØªØºÛÛØ±Ø´ Ø¨Ø¯ÛØ¯Ø Ù¾ÛØ§Ù Ø´ÙØ§ Ù¾Ø§Ø³Ø® Ø¯Ø§Ø¯Ù ÙÙÛØ´ÙØ¯.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ÙØªÙØ¬Ù Ø´Ø¯ÙØ Ø§Ø¯Ø§ÙÙ Ø¨Ø¯Ù", callback_data="training_stage3")]]))
        return

    if action == "training_stage3":
        code = escape(admin["code"])
        await query.message.reply_text(
            "<b>Ú©Ø¯ Ø§Ø®ØªØµØ§ØµÛ Ø´ÙØ§ Ø¨Ù Ø¹ÙÙØ§Ù Ø§Ø¯ÙÛÙ Ø¬Ø°Ø¨Ø§Ù</b>\n"
            f"Â«Ú©Ø¯ {code} ÙØ³ØªÂ»\n\nÙØ± ÙÙØª ÙØ±Ø§Ø¬Ø¹ÙâÚ©ÙÙØ¯Ù Ø¯Ø§Ø®Ù ØªÙÚ¯Ø±Ø§Ù Ø¹Ø¨Ø§Ø±Øª Â«Ø§Ø³ØªØ®Ø¯Ø§Ù Ú©Ø¯{code}Â» Ø±Ù Ø§Ø±Ø³Ø§Ù Ú©ÙÙØ "
            "ÙØªÙØ¬Ù ÙÛâØ´Ù Ú©Ù Ø§Ø² Ø·Ø±Ù Ø´ÙØ§ ÙØ¹Ø±ÙÛ Ø´Ø¯Ù Ù ÙÙ Ø¨Ù Ø´ÙØ§ Ù ÙÙ Ø¨Ù Ú©Ø§Ø±ÙØ±ÙØ§ Ø§Ø·ÙØ§Ø¹ ÙÛØ¯Ù Ú©Ù ÛÚ© Ø¬Ø°Ø¨ Ø¬Ø¯ÛØ¯ Ø§Ø² Ø·Ø±ÛÙ Ø´ÙØ§ Ø§ÙØ¬Ø§Ù Ø´Ø¯Ù!\n\n"
            "ØªÙØ§Ù Ø¬Ø°Ø¨âÙØ§Û Ø´ÙØ§ Ø«Ø¨Øª ÙÛâØ´Ù Ù Ø¨Ø§ ÙØ± Ø¬Ø°Ø¨ Ø¬Ø¯ÛØ¯Ø Ø®ÙØ¯Ù Ø¨ÙØªÙÙ Ø§Ø·ÙØ§Ø¹ ÙÛØ¯Ù.\n\nØ¨Ù Ø§Ø²Ø§Û ÙØ± Û´Û° Ø¬Ø°Ø¨Ø <b>Û²Û°Û° ÙØ²Ø§Ø± ØªÙÙØ§Ù</b> Ù¾ÙØ±Ø³Ø§ÙØª Ø¯Ø±ÛØ§ÙØª ÙÛâÚ©ÙÛØ¯. "
            "Ø¨Ù ÙØ­Ø¶ Ø§ÛÙÚ©Ù Ø¨Ù Û´Û° Ø¬Ø°Ø¨ Ø¨Ø±Ø³ÛØ¯Ø Ø­ÙÙÙØªÙÙ ÙØ§Ø±ÛØ² ÙÛâØ´Ù Ù Ø´ÙØ§Ø±ÙØ¯Ù Ø¬Ø°Ø¨âÙØ§ØªÙÙ Ø¨Ø±Ø§Û Ø´Ø±ÙØ¹ Ø¯ÙØ±Ù Ø¨Ø¹Ø¯Û Ø§Ø² ÙÙ ÙØ­Ø§Ø³Ø¨Ù ÙÛâØ´Ù.\n\n"
            "<b>ÙÚ¯Ø±Ø§Ù ØªØ¹Ø¯Ø§Ø¯ Ø¬Ø°Ø¨âÙØ§ØªÙÙ ÙØ¨Ø§Ø´ÛØ¯Ø</b> Ø§Ú¯Ù ØªØ§ Ù¾Ø§ÛØ§Ù Ø±ÙØ² Ø¨Ù Û´Û° Ø¬Ø°Ø¨ ÙØ±Ø³ÛØ¯ÛØ¯Ø Ø¬Ø°Ø¨âÙØ§Û Ø¨Ø§ÙÛâÙÙÙØ¯Ù Ø¨Ù Ø±ÙØ² Ø¨Ø¹Ø¯ ÙÙØªÙÙ ÙÛâØ´Ù Ù Ø§Ø² Ø¨ÛÙ ÙÙÛâØ±Ù. Ù¾Ø³ Ø¨Ø±Ø§Û Ø±Ø³ÛØ¯Ù Ø¨Ù Ø­Ø¯ÙØµØ§Ø¨ ÙÙØª Ø¯Ø§Ø±ÛØ¯ Ù ÙÛâØªÙÙÛØ¯ Ø¨Ø§ ØªÙØ§Ø´ Ø¨ÛØ´ØªØ±Ø Ø¬Ø°Ø¨âÙØ§ØªÙÙ Ø±Ù ØªÚ©ÙÛÙ Ú©ÙÛØ¯.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ÙØªÙØ¬Ù Ø´Ø¯ÙØ Ø§Ø¯Ø§ÙÙ Ø¨Ø¯Ù", callback_data="training_stage4")]]))
        return

    if action == "training_stage4":
        await query.message.reply_text(
            "ØªØ¹Ø¯Ø§Ø¯ Ø§ÙØ±Ø§Ø¯Û Ú©Ù Ø¬Ø°Ø¨ ÙÛâÚ©ÙÛØ¯Ø Ø¨Ù Ø§ÛÙ Ø¨Ø³ØªÚ¯Û Ø¯Ø§Ø±Ù Ú©Ù ÚÙØ¯Ø± Ø²ÙØ§Ù ÙÛâØ°Ø§Ø±ÛØ¯ Ù ÚÙØ¯Ø± Ú©Ø§ÙÙØª Ø²ÛØ± Ù¾Ø³ØªâÙØ§ ÙÛâØ°Ø§Ø±ÛØ¯.\n\nÙØ±ÚÛ Ø²ÛØ± Ù¾Ø³ØªâÙØ§Û Ø¨ÛØ´ØªØ±Û Ú©Ø§ÙÙØª Ø¨Ø°Ø§Ø±ÛØ¯Ø Ø·Ø¨ÛØ¹Ø§ØªØ§ Ø´Ø§ÙØ³ Ø¬Ø°Ø¨ Ø§ÙØ±Ø§Ø¯ Ø¨ÛØ´ØªØ±Û Ø±Ù Ø¯Ø§Ø±ÛØ¯.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Ù¾Ø§ÛÙâØ§ÙØ Ø´Ø±ÙØ¹ Ú©ÙÛÙðª", callback_data="training_finish")]]))
        return

    if action == "training_finish":
        activated_at = datetime.now(TZ).strftime("%Y/%m/%d")
        with db() as c:
            c.execute("UPDATE admins SET training_done=1, activated_at=COALESCE(activated_at, ?) WHERE user_id=?", (activated_at, uid))
            admin = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
            enqueue_tx(c, OWNER_ID,
                       f"Ø§Ø¯ÙÛÙ Ø¢ÙÙØ²Ø´ Ø±Ø§ ØªÚ©ÙÛÙ Ú©Ø±Ø¯.\nÙØ§Ù: {admin['name']}\nØ¢ÛØ¯Û: {uid}\nÚ©Ø¯: {admin['code']}\nØªØ§Ø±ÛØ® Ø´Ø±ÙØ¹: {admin['activated_at']}",
                       f"training-finished:{uid}")
        await query.message.reply_text(
            "ð <b>ØªØ¨Ø±ÛÚ©!</b>\nØ§Ø² Ø§ÛÙ ÙØ­Ø¸Ù Ø´ÙØ§ ÛÚ©Û Ø§Ø² Ø§Ø¯ÙÛÙâÙØ§Û ÙØ¬ÙÙØ¹Ù Ø¬Ø°Ø¨Ø§Ù ÙØ³ØªÛØ¯.\n\nØ§Ø² ÙÙÛÙ Ø§ÙØ§Ù ÙÛâØªÙÙÛ Ú©Ø§Ø±Øª Ø±Ù Ø´Ø±ÙØ¹ Ú©ÙÛ Ù Ø¬Ø°Ø¨âÙØ§Øª Ø±Ù Ø«Ø¨Øª Ú©ÙÛ.\n\n"
            f"ð <b>Ø¢ÙØ§Ø± Ø´ÙØ§</b>\nð¥ ØªØ¹Ø¯Ø§Ø¯ Ø¬Ø°Ø¨âÙØ§: {admin['total']} ÙÙØ±\nð¯ ÙØ¯Ù Ø¯ÙØ±Ù: Û²Û°Û° Ø¬Ø°Ø¨\nð° Ù¾ÙØ±Ø³Ø§ÙØª ÙØ± Û´Û° Ø¬Ø°Ø¨: Û²Û°Û° ÙØ²Ø§Ø± ØªÙÙØ§Ù\nð ØªØ§Ø±ÛØ® Ø´Ø±ÙØ¹ ÙÙÚ©Ø§Ø±Û: {escape(admin['activated_at'])}\n\nÙÙÙÙ Ø¨Ø§Ø´Û!",
            parse_mode="HTML", reply_markup=main_menu())
        await flush_notification_queue(context.bot)
        return

    if action == "my_stats":
        await query.message.reply_text(
            f"ð Ø¢ÙØ§Ø± Ø´ÙØ§\nð¥ Ø¬Ø°Ø¨ Ø§ÛÙ Ø¯ÙØ±Ù: {admin['count']}\nð¯ Ú©Ù Ø¬Ø°Ø¨âÙØ§: {admin['total']}\nð ØªØ§Ø±ÛØ® Ø´Ø±ÙØ¹ ÙÙÚ©Ø§Ø±Û: {admin['activated_at'] or 'ÙÙÙØ² ÙØ¹Ø§Ù ÙØ´Ø¯Ù'}",
            reply_markup=main_menu())
        return

    if action == "my_code":
        await query.message.reply_text(f"Ú©Ø¯ Ø§Ø®ØªØµØ§ØµÛ Ø´ÙØ§: Ø§Ø³ØªØ®Ø¯Ø§Ù Ú©Ø¯{admin['code']}\nØ§ÛÙ Ø¹Ø¨Ø§Ø±Øª Ø±Ù Ø¨Ø¯ÙÙ ØªØºÛÛØ± Ø¨Ø±Ø§Û ÙØªÙØ§Ø¶ÛØ§Ù Ø¨ÙØ±Ø³Øª.", reply_markup=main_menu())


def normalize_digits(value):
    return value.translate(str.maketrans("Û°Û±Û²Û³Û´ÛµÛ¶Û·Û¸Û¹Ù Ù¡Ù¢Ù£Ù¤Ù¥Ù¦Ù§Ù¨Ù©", "01234567890123456789"))


async def process_lead(app, sender, code):
    """Atomically save a unique lead, update counters and queue related notices."""
    now = datetime.now(TZ).isoformat()
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()
        if not admin or not admin["training_done"]:
            return False
        try:
            c.execute("INSERT INTO leads (user_id, admin_code, created_at, admin_user_id) VALUES (?, ?, ?, ?)",
                      (sender.id, code, now, admin["user_id"]))
        except sqlite3.IntegrityError:
            # A Telegram user can only count once globally.
            return False

        c.execute("UPDATE admins SET count=COALESCE(count,0)+1, total=COALESCE(total,0)+1 WHERE code=?", (code,))
        updated = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()
        current_count = int(updated["count"] or 0)
        periods, remainder = counter_transition(current_count - 1, 1)
        if periods:
            c.execute("UPDATE admins SET count=? WHERE code=?", (remainder, code))
            updated = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()

        reached_200 = int(updated["total"] or 0) >= TARGET_TOTAL and not int(updated["notified_200"] or 0)
        if reached_200:
            c.execute("UPDATE admins SET notified_200=1 WHERE code=?", (code,))

        username = getattr(sender, "username", None)
        link = f"https://t.me/{username}" if username else f"tg://user?id={sender.id}"
        lead_name = getattr(sender, "first_name", None) or "Ú©Ø§Ø±Ø¨Ø±"
        enqueue_tx(c, OWNER_ID,
                   f"Ø¬Ø°Ø¨ Ø¬Ø¯ÛØ¯ Ø«Ø¨Øª Ø´Ø¯.\nØ§Ø¯ÙÛÙ: {admin['name']}\nØ¢ÛØ¯Û Ø§Ø¯ÙÛÙ: {admin['user_id']}\nÚ©Ø¯ Ø§Ø¯ÙÛÙ: {code}\n"
                   f"Ú©Ø§Ø±Ø¨Ø±: {lead_name}\nØ¢ÛØ¯Û Ú©Ø§Ø±Ø¨Ø±: {sender.id}\nÙÛÙÚ©: {link}\nØ¬Ø°Ø¨ Ø¨Ø§ÙÛâÙØ§ÙØ¯Ù Ø§ÛÙ Ø¯ÙØ±Ù: {remainder}\n"
                   f"Ú©Ù Ø¬Ø°Ø¨âÙØ§: {updated['total']}\nØ²ÙØ§Ù: {now}", f"lead-owner:{sender.id}")
        enqueue_tx(c, admin["user_id"],
                   f"ÛÚ© Ø¬Ø°Ø¨ Ø¬Ø¯ÛØ¯ Ø«Ø¨Øª Ø´Ø¯.\nØ¬Ø°Ø¨ Ø§ÛÙ Ø¯ÙØ±Ù: {remainder}\nÚ©Ù Ø¬Ø°Ø¨âÙØ§: {updated['total']}",
                   f"lead-admin:{sender.id}")

        for i in range(periods):
            enqueue_tx(c, admin["user_id"],
                       "Ø¨Ù Û´Û° Ø¬Ø°Ø¨ Ø±Ø³ÛØ¯ÛØ Ù¾Ø§Ø¯Ø§Ø´ Ø§ÛÙ Ø¯ÙØ±Ù Û²Û°Û° ÙØ²Ø§Ø± ØªÙÙØ§Ù Ø§Ø³Øª Ù Ø´ÙØ§Ø±ÙØ¯Ù Ø¯ÙØ±Ù Ø¬Ø¯ÛØ¯ Ø´Ø±ÙØ¹ Ø´Ø¯.",
                       f"bonus-admin:{sender.id}:{i}")
        if periods:
            enqueue_tx(c, OWNER_ID,
                       f"Ø§Ø¯ÙÛÙ {admin['name']} (Ú©Ø¯ {code}) Ø§ÛÙ Ø¨Ø§Ø± {periods} Ø¯ÙØ±Ù Û´Û°ØªØ§ÛÛ ØªÚ©ÙÛÙ Ú©Ø±Ø¯Ø "
                       f"ÙØ¬ÙÙØ¹ Ù¾ÙØ±Ø³Ø§ÙØª ÙØ§Ø¨Ù Ø¨Ø±Ø±Ø³Û: {periods * BONUS_TOMAN:,} ØªÙÙØ§Ù.",
                       f"bonus-owner:{sender.id}")
        if reached_200:
            enqueue_tx(c, admin["user_id"], "ð ØªØ¨Ø±ÛÚ©! Ø¨Ù ÙØ¯Ù Û²Û°Û° Ø¬Ø°Ø¨ Ø±Ø³ÛØ¯Û. Ú©Ø§Ø±ÙØ±ÙØ§ Ø§Ø² ØªÚ©ÙÛÙ ÙØ¯ÙØª ÙØ·ÙØ¹ Ø´Ø¯.",
                       f"target200-admin:{admin['user_id']}")
            enqueue_tx(c, OWNER_ID,
                       f"ð Ø§Ø¯ÙÛÙ Ø¨Ù ÙØ¯Ù Û²Û°Û° Ø¬Ø°Ø¨ Ø±Ø³ÛØ¯!\nÙØ§Ù: {admin['name']}\nØ¢ÛØ¯Û: {admin['user_id']}\nÚ©Ø¯: {code}\nÚ©Ù Ø¬Ø°Ø¨âÙØ§: {updated['total']}",
                       f"target200-owner:{admin['user_id']}")
    await flush_notification_queue(app.bot)
    return True


async def on_startup(app: Application):
    global client
    run_tests()
    await flush_notification_queue(app.bot)
    if not SESSION_STRING:
        await safe_owner_message(app.bot,
            "Ø±Ø¨Ø§Øª ÙØ¯ÛØ±ÛØªÛ Ø§Ø¬Ø±Ø§ Ø´Ø¯Ø Ø§ØªØµØ§Ù Ø§Ú©Ø§ÙØª Ø§Ø³ØªØ®Ø¯Ø§Ù ÙÙÙØ² ØªÙØ¸ÛÙ ÙØ´Ø¯Ù (SESSION_STRING Ø®Ø§ÙÛ Ø§Ø³Øª).",
            "startup-no-session")
        return
    if not API_ID_RAW or not API_HASH:
        await safe_owner_message(app.bot, "Ø§ØªØµØ§Ù Ø§Ú©Ø§ÙØª Ø§Ø³ØªØ®Ø¯Ø§Ù ØªÙØ¸ÛÙ ÙØ´Ø¯Ù: API_ID ÛØ§ API_HASH Ø®Ø§ÙÛ Ø§Ø³Øª.", "startup-no-api")
        return
    try:
        client = TelegramClient(StringSession(SESSION_STRING), int(API_ID_RAW), API_HASH)
        await client.start()
    except Exception:
        logger.exception("Failed to start Telethon client")
        await safe_owner_message(app.bot, "Ø®Ø·Ø§ Ø¯Ø± Ø±Ø§ÙâØ§ÙØ¯Ø§Ø²Û Ø§Ú©Ø§ÙØª Ø§Ø³ØªØ®Ø¯Ø§Ù ØªÙÚ¯Ø±Ø§Ù (Telethon). Ø¬Ø²Ø¦ÛØ§Øª Ø¯Ø± ÙØ§Ú¯ ÙØ§Ø³Øª Ø«Ø¨Øª Ø´Ø¯.", "telethon-start-failed")
        return

    @client.on(events.NewMessage(incoming=True))
    async def incoming(event):
        try:
            if not event.is_private or event.out:
                return
            text = normalize_digits(event.raw_text or "")
            match = re.search(r"Ø§Ø³ØªØ®Ø¯Ø§Ù\s+Ú©Ø¯\s*(\d+)", text, re.IGNORECASE)
            if not match:
                return
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False):
                return
            await process_lead(app, sender, match.group(1))
        except Exception:
            logger.exception("Error processing incoming Telethon message")
    await safe_owner_message(app.bot, "Ø§ØªØµØ§Ù Ø§Ú©Ø§ÙØª Ø§Ø³ØªØ®Ø¯Ø§Ù Ø¨Ø±ÙØ±Ø§Ø± Ø´Ø¯.", "telethon-connected")


async def retry_notifications_job(context: ContextTypes.DEFAULT_TYPE):
    await flush_notification_queue(context.bot)


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    start = datetime.combine(today, time.min, tzinfo=TZ).isoformat()
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=TZ).isoformat()
    with db() as c:
        admins = c.execute("SELECT * FROM admins ORDER BY total DESC").fetchall()
        lines = [f"ð Ú¯Ø²Ø§Ø±Ø´ Ø³Ø§Ø¹Øª Û±Û° Ø´Ø¨ Ø¬Ø°Ø¨Ø§Ù ({today.isoformat()})"]
        for a in admins:
            daily = c.execute("""SELECT COUNT(*) AS cnt FROM leads
                              WHERE (admin_user_id=? OR (admin_user_id IS NULL AND admin_code=?))
                              AND created_at>=? AND created_at<?""",
                              (a["user_id"], a["code"], start, end)).fetchone()["cnt"]
            lines.append(f"{a['name'] or a['user_id']}: Ø§ÙØ±ÙØ² {daily} Ø¬Ø°Ø¨ | Ú©Ù {a['total']}")
    await safe_owner_message(context.bot, "\n".join(lines), f"daily-report:{today.isoformat()}")


def counter_transition(current_count, added=1):
    """Return (completed bonus periods, remainder) for a period counter."""
    if current_count < 0 or added < 0:
        raise ValueError("counts cannot be negative")
    value = current_count + added
    return value // BONUS_EVERY, value % BONUS_EVERY


def run_tests():
    cases = {
        39: (0, 39), 40: (1, 0), 41: (1, 1), 79: (1, 39),
        80: (2, 0), 81: (2, 1), 85: (2, 5), 199: (4, 39),
        200: (5, 0), 201: (5, 1),
    }
    for total_count, expected in cases.items():
        actual = counter_transition(0, total_count)
        assert actual == expected, (total_count, actual, expected)
    assert counter_transition(39, 1) == (1, 0)
    assert counter_transition(39, 2) == (1, 1)
    assert TARGET_TOTAL == 200 and BONUS_EVERY == 40 and BONUS_TOMAN == 200_000
    logger.info("Offline counter boundary tests passed.")


def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not OWNER_ID_RAW:
        missing.append("OWNER_ID")
    if missing:
        raise RuntimeError("Environment variables missing: " + ", ".join(missing))
    if SESSION_STRING and (not API_ID_RAW or not API_HASH):
        raise RuntimeError("SESSION_STRING requires both API_ID and API_HASH")
    try:
        return int(OWNER_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("OWNER_ID must be an integer Telegram user ID") from exc


async def on_shutdown(app: Application):
    global client
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            logger.exception("Error while disconnecting Telethon client")
        client = None


def main():
    global OWNER_ID
    OWNER_ID = validate_config()
    setup_db()
    app = (Application.builder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, receive_admin))
    if app.job_queue:
        app.job_queue.run_daily(daily_report, time=time(22, 0, tzinfo=TZ), name="daily-report")
        app.job_queue.run_repeating(retry_notifications_job, interval=60, first=10, name="retry-notifications")
    else:
        logger.warning("JobQueue unavailable. Install python-telegram-bot[job-queue] to enable scheduled jobs.")
    app.run_polling()


if __name__ == "__main__":
    main()
