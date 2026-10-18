
import os
import re
import sqlite3
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
SESSION_STRING = os.environ.get("SESSION_STRING", "")

DB = "jazban.db"
client = None
awaiting_admin = set()


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                code TEXT UNIQUE,
                count INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                user_id INTEGER PRIMARY KEY,
                admin_code TEXT,
                created_at TEXT
            )
        """)


def new_code():
    with db() as c:
        while True:
            code = f"{secrets.randbelow(9000) + 1000}"
            if not c.execute(
                "SELECT 1 FROM admins WHERE code=?", (code,)
            ).fetchone():
                return code


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid == OWNER_ID:
        await update.message.reply_text(
            "🎯 جذبان آماده‌ست.\n\n"
            "/addadmin - ثبت ادمین جدید\n"
            "/stats - آمار ادمین‌ها\n"
            "/cancel - لغو عملیات"
        )
        return

    with db() as c:
        admin = c.execute(
            "SELECT * FROM admins WHERE user_id=?", (uid,)
        ).fetchone()

        if admin:
            if not admin["code"]:
                code = new_code()
                c.execute(
                    "UPDATE admins SET code=? WHERE user_id=?",
                    (code, uid)
                )
                await update.message.reply_text(
                    f"ادمین عزیز، کد اختصاصی شما:\n\n"
                    f"`{code}`\n\n"
                    "متقاضی باید این کد رو همراه کلمه استخدام "
                    "به اکانت استخدام بفرسته.",
                    parse_mode="Markdown"
                )
                await context.bot.send_message(
                    OWNER_ID,
                    f"ادمین {admin['name']} فعال شد؛ کد: {code}"
                )
            else:
                await update.message.reply_text(
                    f"کد اختصاصی شما: {admin['code']}\n"
                    f"جذب این دوره: {admin['count']}\n"
                    f"کل جذب‌ها: {admin['total']}"
                )
        else:
            await update.message.reply_text(
                "شما هنوز به‌عنوان ادمین ثبت نشده‌اید."
            )


async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    awaiting_admin.add(OWNER_ID)
    await update.message.reply_text(
        "آیدی عددی ادمین رو بفرست یا پیام خودش رو فوروارد کن."
    )


async def receive_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID or OWNER_ID not in awaiting_admin:
        return

    msg = update.effective_message
    target = None

    if msg.forward_origin and hasattr(msg.forward_origin, "sender_user"):
        target = msg.forward_origin.sender_user
    elif msg.text and msg.text.strip().isdigit():
        target_id = int(msg.text.strip())
        if target_id > 0:
            with db() as c:
                c.execute(
                    """INSERT OR IGNORE INTO admins
                    (user_id, name) VALUES (?, ?)""",
                    (target_id, str(target_id))
                )
            awaiting_admin.discard(OWNER_ID)
            await msg.reply_text(
                "ادمین ثبت شد. بهش بگو ربات رو استارت کنه."
            )
            try:
                await context.bot.send_message(
                    target_id, "برای فعال‌سازی کد اختصاصی، /start رو بزن."
                )
            except Exception:
                pass
            return

    if not target:
        await msg.reply_text(
            "آیدی عددی یا پیام فورواردشده رو بفرست."
        )
        return

    with db() as c:
        c.execute(
            """INSERT OR IGNORE INTO admins
            (user_id, username, name) VALUES (?, ?, ?)""",
            (target.id, target.username, target.first_name or str(target.id))
        )

    awaiting_admin.discard(OWNER_ID)
    await msg.reply_text(
        f"ادمین {target.first_name} ثبت شد. بهش بگو ربات رو استارت کنه."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting_admin.discard(OWNER_ID)
    await update.message.reply_text("عملیات لغو شد.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    with db() as c:
        rows = c.execute(
            "SELECT * FROM admins ORDER BY total DESC"
        ).fetchall()

    if not rows:
        await update.message.reply_text("هنوز ادمینی ثبت نشده.")
        return

    lines = ["📊 آمار جذبان"]
    for a in rows:
        lines.append(
            f"{a['name']} (@{a['username'] or 'ندارد'})\n"
            f"کد: {a['code'] or 'هنوز فعال نشده'} | "
            f"این دوره: {a['count']} | کل: {a['total']}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM admins").fetchall()

    lines = ["📊 گزارش ساعت ۱۰ شب جذبان"]
    for a in rows:
        lines.append(
            f"{a['name']}: این دوره {a['count']} جذب | "
            f"کل {a['total']}"
        )

    await context.bot.send_message(OWNER_ID, "\n".join(lines))


async def on_startup(app: Application):
    global client

    if not SESSION_STRING:
        await app.bot.send_message(
            OWNER_ID,
            "ربات مدیریتی اجرا شد؛ اتصال اکانت استخدام هنوز تنظیم نشده."
        )
        return

    client = TelegramClient(
        StringSession(SESSION_STRING), API_ID, API_HASH
    )
    await client.start()

    @client.on(events.NewMessage(incoming=True))
    async def incoming(event):
        if not event.is_private or event.out:
            return

        text = event.raw_text or ""
        match = re.search(r"استخدام\s+(\d{4})", text)
        if not match:
            return

        code = match.group(1)
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        with db() as c:
            admin = c.execute(
                "SELECT * FROM admins WHERE code=?", (code,)
            ).fetchone()

            if not admin:
                return

            # هر حساب تلگرام فقط یک بار شمرده می‌شود.
            exists = c.execute(
                "SELECT 1 FROM leads WHERE user_id=?", (sender.id,)
            ).fetchone()
            if exists:
                return

            now = datetime.now(ZoneInfo("Asia/Tehran")).isoformat()
            c.execute(
                "INSERT INTO leads (user_id, admin_code, created_at) "
                "VALUES (?, ?, ?)", (sender.id, code, now)
            )
            c.execute(
                "UPDATE admins SET count=count+1, total=total+1 "
                "WHERE code=?", (code,)
            )
            updated = c.execute(
                "SELECT * FROM admins WHERE code=?", (code,)
            ).fetchone()

            # هر ۵۰ جذب، شمارنده دوره از نو شروع می‌شود.
            reached = updated["count"] >= 50
            if reached:
                c.execute(
                    "UPDATE admins SET count=0 WHERE code=?", (code,)
                )

        username = getattr(sender, "username", None)
        user_link = (
            f"https://t.me/{username}" if username
            else f"tg://user?id={sender.id}"
        )
        name = getattr(sender, "first_name", "کاربر")

        await app.bot.send_message(
            OWNER_ID,
            f"جذب جدید ثبت شد.\n"
            f"ادمین: {admin['name']}\n"
            f"کاربر: {name}\n"
            f"لینک: {user_link}\n"
            f"آمار این دوره: {0 if reached else updated['count']}\n"
            f"کل جذب‌ها: {updated['total']}"
        )

        try:
            await app.bot.send_message(
                admin["user_id"],
                f"یک جذب جدید ثبت شد.\n"
                f"آمار این دوره: {0 if reached else updated['count']}\n"
                f"کل جذب‌ها: {updated['total']}"
            )
            if reached:
                await app.bot.send_message(
                    admin["user_id"],
                    "به ۵۰ جذب رسیدی؛ شمارنده دوره جدید شروع شد."
                )
        except Exception:
            pass

    await app.bot.send_message(
        OWNER_ID, "اتصال اکانت استخدام برقرار شد."
    )


def main():
    setup_db()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, receive_admin)
    )

    if app.job_queue:
        app.job_queue.run_daily(
            daily_report,
            time=datetime.strptime("22:00", "%H:%M").time().replace(
                tzinfo=ZoneInfo("Asia/Tehran")
            )
        )

    app.run_polling()


if __name__ == "__main__":
    main()
