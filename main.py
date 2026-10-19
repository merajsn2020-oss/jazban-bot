import os
import re
import html
import sqlite3
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto,
)
try:
    from telegram import CopyTextButton
except ImportError:
    CopyTextButton = None
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.error import TelegramError
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
SESSION_STRING = os.environ.get("SESSION_STRING", "")
DB = os.environ.get("DB_PATH", "jazban.db")
TZ = ZoneInfo("Asia/Tehran")
client = None
OWNER_MENU = [
    "📊 گزارش جذب 📊",
    "💰 مدیریت مالی 💰",
    "📢 پیام همگانی 📢",
    "🔎 جست‌وجوی ادمین 🔎",
    "⚙️ تنظیمات ⚙️",
]
ADMIN_MENU = [
    "💬 دریافت متن کامنت 💬",
    "👁️‍🗨️ دریافت متن پاسخگویی 👁️‍🗨️",
    "🎬 دریافت ویدیو آموزشی 🎬",
    "💰 سوابق پورسانت 💰",
    "💵 دریافت پورسانت 💵",
    "👨🏻‍💼 ارتباط با کارفرما 👨🏻‍💼",
    "🔄 راه‌اندازی مجدد ربات 🔄",
]

def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")

def date_fa(dt=None):
    dt = dt or datetime.now(TZ)
    months = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
    # Gregorian-to-Jalali conversion, without external dependencies.
    gy, gm, gd = dt.year, dt.month, dt.day
    g_d_m = [0,31,59,90,120,151,181,212,243,273,304,334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3)//4 - (gy2 + 99)//100 + (gy2 + 399)//400 + gd + g_d_m[gm-1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return f"{jd} {months[jm-1]} {jy}"

def digits_en(value):
    return str(value).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))

def escape(value):
    return html.escape(str(value or ""))

def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def table_columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

def setup_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, code TEXT UNIQUE,
            count INTEGER DEFAULT 0, total INTEGER DEFAULT 0, activated_at TEXT,
            training_done INTEGER DEFAULT 0, notified_200 INTEGER DEFAULT 0,
            cycle_threshold INTEGER DEFAULT 40, cycle_amount INTEGER DEFAULT 200000
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS leads (
            user_id INTEGER PRIMARY KEY, admin_code TEXT, created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS notification_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, message TEXT, created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            card_number TEXT NOT NULL, owner_name TEXT NOT NULL, created_at TEXT,
            active INTEGER DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS commissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, lead_count INTEGER NOT NULL DEFAULT 40,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT,
            request_at TEXT, paid_at TEXT, card_id INTEGER, receipt_file_id TEXT,
            receipt_type TEXT, cycle_no INTEGER DEFAULT 1
        )""")
        # Migrations for existing installations.
        cols = table_columns(c, "admins")
        for name, sql in {
            "activated_at": "ALTER TABLE admins ADD COLUMN activated_at TEXT",
            "training_done": "ALTER TABLE admins ADD COLUMN training_done INTEGER DEFAULT 0",
            "notified_200": "ALTER TABLE admins ADD COLUMN notified_200 INTEGER DEFAULT 0",
            "cycle_threshold": "ALTER TABLE admins ADD COLUMN cycle_threshold INTEGER DEFAULT 40",
            "cycle_amount": "ALTER TABLE admins ADD COLUMN cycle_amount INTEGER DEFAULT 200000",
        }.items():
            if name not in cols:
                c.execute(sql)
        defaults = {
            "commission_threshold": "40",
            "commission_amount": "200000",
            "new_lead_notifications": "1",
            "training_video_enabled": "0",
            "channel_enabled": "0",
            "channel_id": "",
            "channel_text": "برای ادامه، ابتدا عضو کانال ما شو.",
            "channel_button": "عضویت در کانال",
            "channel_url": "",
            "video_file_id": "",
            "comment_text": "نیازمند چند اپراتور پاسخگویی\nدرآمد روزانه بین ۲۰۰ تا ۱.۲۰۰ تومان\nهزینه استخدام کاملا رایگان!\n\n(دایرکت درخدمتم)",
            "reply_text": "جهت استخدام و دریافت اطلاعات بیشتر لطفا عبارت: (استخدام کد{code})\nرو به ایدی زیر داخل تلگرام ارسال نمایید👇\n@jazban_work\n\n⚠️ توجه: اگر عبارت بالارو ارسال نکنید یا تغییرش بدید، پیام شما پاسخ داده نمیشود.",
            "training_video_caption": "این ویدیو آموزشی\nصفر تا صد کار ما رو بهت آموزش میده!\nپس حتما ببینش دوست خوبم\n\nاگه ام قبلا این ویدیو رو از کارفرما دریافت کردی\nیا الان کامل تماشاش کردی:\nروی دکمه «ویدیو رو دیدم» بزن🔻",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        threshold = int(c.execute("SELECT value FROM settings WHERE key='commission_threshold'").fetchone()[0])
        amount = int(c.execute("SELECT value FROM settings WHERE key='commission_amount'").fetchone()[0])
        c.execute("UPDATE admins SET cycle_threshold=? WHERE cycle_threshold IS NULL OR cycle_threshold<1", (threshold,))
        c.execute("UPDATE admins SET cycle_amount=? WHERE cycle_amount IS NULL OR cycle_amount<1", (amount,))

def get_setting(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def new_code(conn=None):
    own = conn is None
    conn = conn or db()
    used = {str(r["code"]) for r in conn.execute("SELECT code FROM admins WHERE code IS NOT NULL")}
    available = [str(n) for n in range(10, 100) if str(n) not in used]
    if available:
        code = random.choice(available)
    else:
        n = 100
        while str(n) in used:
            n += 1
        code = str(n)
    if own:
        conn.close()
    return code

def owner_menu():
    return ReplyKeyboardMarkup([[x] for x in OWNER_MENU], resize_keyboard=True)

def admin_menu():
    return ReplyKeyboardMarkup([[x] for x in ADMIN_MENU], resize_keyboard=True)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")]])

def owner_home_text():
    return "سلام معراج عزیز🤖👋🏻\n\nدستور بده چیکار کنم برات؟"

async def safe_owner_message(bot, text, **kwargs):
    try:
        await bot.send_message(OWNER_ID, text, **kwargs)
    except Exception as e:
        logger.error("Failed to send owner message: %s", e)
        if "parse_mode" not in kwargs:
            with db() as c:
                c.execute("INSERT INTO notification_queue(chat_id,message,created_at) VALUES(?,?,?)", (OWNER_ID, text, now_iso()))

async def flush_notification_queue(bot):
    with db() as c:
        rows = c.execute("SELECT id,chat_id,message FROM notification_queue ORDER BY id").fetchall()
        for r in rows:
            try:
                await bot.send_message(r["chat_id"], r["message"])
                c.execute("DELETE FROM notification_queue WHERE id=?", (r["id"],))
            except Exception as e:
                logger.warning("Queued message %s not sent: %s", r["id"], e)

def profile_link(admin):
    name = escape(admin["name"] or "ادمین")
    if admin["username"]:
        return f'<a href="https://t.me/{escape(admin["username"])}">{name}</a>'
    return f'<a href="tg://user?id={admin["user_id"]}">{name}</a>'

def money(value):
    return f"{int(value):,}"

def copy_card_button(label, card_number, fallback_callback):
    if CopyTextButton is not None and card_number:
        return InlineKeyboardButton(label, copy_text=CopyTextButton(text=str(card_number)))
    return InlineKeyboardButton(label, callback_data=fallback_callback)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not user or not msg:
        return
    if user.id == OWNER_ID:
        context.user_data.clear()
        await msg.reply_text(owner_home_text(), reply_markup=owner_menu())
        return
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (user.id,)).fetchone()
        if not admin:
            code = new_code(c)
            c.execute("INSERT INTO admins(user_id,username,name,code,cycle_threshold,cycle_amount) VALUES(?,?,?,?,?,?)", (user.id, user.username, user.first_name or str(user.id), code, int(get_setting("commission_threshold", "40")), int(get_setting("commission_amount", "200000"))))
        else:
            code = admin["code"] or new_code(c)
            c.execute("UPDATE admins SET username=?,name=?,code=? WHERE user_id=?", (user.username, user.first_name or str(user.id), code, user.id))
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (user.id,)).fetchone()
    if not await channel_gate(update, context):
        return
    if admin["training_done"]:
        await msg.reply_text("آموزش شما قبلا تکمیل شده. از منوی زیر استفاده کن.", reply_markup=admin_menu())
        return
    await msg.reply_text(
        "سلام دوست خوبم ⭐️💟\n\nبه جمع ادمین‌های مجموعه «جذبان»\nخیلی خیلی خوش اومدی!\n\nامیدوارم همکاری خوبی کنار هم داشته باشیم🤝\n\nبرای شروع همکاری🔻روی دکمه زیر بزن🔻",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔷 شروع همکاری 🔷", callback_data="training_start")]])
    )

async def channel_gate(update, context):
    if get_setting("channel_enabled", "0") != "1":
        return True
    user = update.effective_user
    channel_id = get_setting("channel_id")
    channel_url = get_setting("channel_url")
    text = get_setting("channel_text", "برای ادامه، ابتدا عضو کانال ما شو.")
    button = get_setting("channel_button", "عضویت در کانال")
    try:
        member = await context.bot.get_chat_member(channel_id, user.id)
        if member.status in ("member", "administrator", "creator") or (member.status == "restricted" and getattr(member, "is_member", False)):
            return True
    except Exception as e:
        logger.warning("Membership check failed for %s: %s", channel_id, e)
    rows = []
    if channel_url:
        rows.append([InlineKeyboardButton(f"📢 {button} 📢", url=channel_url)])
    rows.append([InlineKeyboardButton("🔄 بررسی عضویت 🔄", callback_data="check_channel")])
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
    return False

async def ensure_owner(update):
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

async def owner_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    text = msg.text or ""
    if user.id == OWNER_ID:
        state = context.user_data.get("state")
        if state:
            await handle_owner_input(update, context, state)
            return
        if text == "📊 گزارش جذب 📊":
            await show_lead_report(update, context, "latest")
        elif text == "💰 مدیریت مالی 💰":
            await msg.reply_text("بخش مدیریت مالی", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ واریزی های انجام شده ✅", callback_data="finance_paid")],
                [InlineKeyboardButton("☑️ واریزی های انجام نشده ☑️", callback_data="finance_unpaid")],
                [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")],
            ]))
        elif text == "📢 پیام همگانی 📢":
            context.user_data["state"] = "broadcast_message"
            await msg.reply_text("معراج عزیز، لطفا پیامی که میخوای برای تمام ادمین‌های ربات ارسال بشه رو بفرست 📩", reply_markup=back_kb())
        elif text == "🔎 جست‌وجوی ادمین 🔎":
            context.user_data["state"] = "search_admin"
            await msg.reply_text("لطفا یوزرنیم ادمین رو ارسال کن یا یک پیام ازش برام فوروارد کن", reply_markup=back_kb())
        elif text == "⚙️ تنظیمات ⚙️":
            await show_settings(msg)
        else:
            await msg.reply_text(owner_home_text(), reply_markup=owner_menu())
        return

    # Admin reply keyboard commands
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (user.id,)).fetchone()
    if not admin:
        return
    if text == "💬 دریافت متن کامنت 💬":
        await msg.reply_text(get_setting("comment_text"), reply_markup=admin_menu())
    elif text == "👁️‍🗨️ دریافت متن پاسخگویی 👁️‍🗨️":
        await msg.reply_text(get_setting("reply_text").replace("{code}", admin["code"] or ""), reply_markup=admin_menu())
    elif text == "🎬 دریافت ویدیو آموزشی 🎬":
        vid = get_setting("video_file_id")
        if vid:
            await context.bot.send_video(user.id, vid, caption=get_setting("training_video_caption"))
        else:
            await msg.reply_text("ویدیوی آموزشی هنوز بارگذاری نشده.")
    elif text == "💰 سوابق پورسانت 💰":
        await show_admin_commissions(msg, user.id)
    elif text == "💵 دریافت پورسانت 💵":
        await begin_payout_request(msg, context, user.id)
    elif text == "👨🏻‍💼 ارتباط با کارفرما 👨🏻‍💼":
        await msg.reply_text("برای ارتباط با کارفرما روی لینک زیر بزن:\nhttps://t.me/jazban_work", reply_markup=admin_menu())
    elif text == "🔄 راه‌اندازی مجدد ربات 🔄":
        with db() as c:
            c.execute("UPDATE admins SET training_done=0 WHERE user_id=?", (user.id,))
        await msg.reply_text("آموزش از مرحله شروع همکاری دوباره فعال شد. کد و سوابق شما محفوظ است.", reply_markup=admin_menu())

async def show_settings(msg):
    await msg.reply_text("⚙️ تنظیمات ⚙️", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 روشن کردن اعلان جذب جدید 🔔" if get_setting("new_lead_notifications", "1") != "1" else "🔕 خاموش کردن اعلان جذب جدید 🔕", callback_data="toggle_notifications")],
        [InlineKeyboardButton("📢 اضافه کردن کانال 📢", callback_data="channel_setup")],
        [InlineKeyboardButton("🔴 خاموش کردن عضویت اجباری 🔴" if get_setting("channel_enabled", "0") == "1" else "🟢 روشن کردن عضویت اجباری 🟢", callback_data="toggle_channel")],
        [InlineKeyboardButton("🟢 روشن کردن ویدیوی آموزشی 🟢" if get_setting("training_video_enabled", "0") != "1" else "🔴 خاموش کردن ویدیوی آموزشی 🔴", callback_data="toggle_video")],
        [InlineKeyboardButton("🎞️ بارگذاری/تعویض ویدیو 🎞️", callback_data="upload_video")],
        [InlineKeyboardButton("💵 تنظیمات پورسانت 💵", callback_data="commission_settings")],
        [InlineKeyboardButton("📝 ویرایش متن‌های ربات 📝", callback_data="edit_texts")],
        [InlineKeyboardButton("📊 گزارش وضعیت ربات 📊", callback_data="bot_status")],
        [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")],
    ]))

async def handle_owner_input(update, context, state):
    msg = update.effective_message
    text = msg.text or ""
    if state == "search_admin":
        if msg.forward_origin and getattr(msg.forward_origin, "sender_user", None):
            target_id = msg.forward_origin.sender_user.id
            await show_admin_search_result(msg, target_id)
            context.user_data.pop("state", None)
            return
        username = text.strip().lstrip("@").split("/")[-1]
        if not username:
            await msg.reply_text("یوزرنیم یا پیام فورواردشده رو بفرست.")
            return
        with db() as c:
            row = c.execute("SELECT user_id FROM admins WHERE lower(username)=lower(?)", (username,)).fetchone()
        if not row:
            await msg.reply_text("ادمینی با این یوزرنیم پیدا نشد. یوزرنیم ثبت‌شده ادمین رو بفرست یا پیامش رو فوروارد کن.", reply_markup=back_kb())
            return
        await show_admin_search_result(msg, row["user_id"])
        context.user_data.pop("state", None)
    elif state == "broadcast_message":
        context.user_data["broadcast_message_id"] = msg.message_id
        context.user_data["broadcast_chat_id"] = msg.chat_id
        context.user_data["state"] = "broadcast_confirm"
        await msg.reply_text("پیش‌نمایش پیام بالا نمایش داده شد. برای ارسال به تمام ادمین‌ها تأیید کن.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 بفرست 📤", callback_data="broadcast_send")],
            [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")],
        ]))
    elif state == "channel_id":
        channel = text.strip()
        if not channel:
            await msg.reply_text("آیدی کانال یا یوزرنیم کانال رو بفرست.")
            return
        context.user_data["channel_id_input"] = channel
        context.user_data["state"] = "channel_url"
        await msg.reply_text("لینک عضویت کانال رو بفرست (مثلا https://t.me/channel).")
    elif state == "channel_url":
        url = text.strip()
        if not (url.startswith("https://t.me/") or url.startswith("http://t.me/")):
            await msg.reply_text("لطفا لینک معتبر کانال مثل https://t.me/channel رو بفرست.")
            return
        context.user_data["channel_url_input"] = url
        context.user_data["state"] = "channel_text"
        await msg.reply_text("متن پیام عضویت اجباری چی باشه؟")
    elif state == "channel_text":
        context.user_data["channel_text_input"] = text
        context.user_data["state"] = "channel_button"
        await msg.reply_text("اسم دکمه عضویت چی باشه؟")
    elif state == "channel_button":
        channel_id = context.user_data.get("channel_id_input", "")
        url = context.user_data.get("channel_url_input", "")
        channel_text = context.user_data.get("channel_text_input", "")
        set_setting("channel_id", channel_id)
        set_setting("channel_url", url)
        set_setting("channel_text", channel_text)
        set_setting("channel_button", text.strip() or "عضویت در کانال")
        set_setting("channel_enabled", "1")
        context.user_data.pop("state", None)
        await msg.reply_text("کانال ثبت شد و عضویت اجباری روشن شد. مطمئن شو ربات به کانال دسترسی ادمین دارد.")
        await show_settings(msg)
    elif state == "upload_video":
        if msg.video:
            set_setting("video_file_id", msg.video.file_id)
            context.user_data.pop("state", None)
            await msg.reply_text("ویدیوی آموزشی ذخیره شد.")
            await show_settings(msg)
        else:
            await msg.reply_text("لطفا خود ویدیو رو به صورت فایل ویدیویی ارسال کن.")
    elif state == "commission_threshold":
        n = digits_en(text.strip())
        if not n.isdigit() or int(n) < 1:
            await msg.reply_text("یک عدد معتبر بزرگ‌تر از صفر بفرست.")
            return
        context.user_data["new_threshold"] = int(n)
        context.user_data["state"] = "commission_amount"
        await msg.reply_text("مبلغ پورسانت هر دوره رو به تومان بفرست.")
    elif state == "commission_amount":
        n = digits_en(text.replace(",", "").strip())
        if not n.isdigit() or int(n) < 1:
            await msg.reply_text("مبلغ معتبر به تومان بفرست.")
            return
        # New settings apply to new cycles; current commissions keep their stored amount.
        new_threshold = int(context.user_data.pop("new_threshold"))
        new_amount = int(n)
        set_setting("commission_threshold", new_threshold)
        set_setting("commission_amount", new_amount)
        with db() as c:
            c.execute("UPDATE admins SET cycle_threshold=?,cycle_amount=? WHERE count=0", (new_threshold, new_amount))
        context.user_data.pop("state", None)
        await msg.reply_text("تنظیمات پورسانت ذخیره شد و برای دوره‌های جدید اعمال می‌شه.")
        await show_settings(msg)
    elif state == "edit_comment":
        set_setting("comment_text", text)
        context.user_data.pop("state", None)
        await msg.reply_text("متن کامنت ذخیره شد.")
        await show_settings(msg)
    elif state == "edit_reply":
        set_setting("reply_text", text)
        context.user_data.pop("state", None)
        await msg.reply_text("متن پاسخگویی ذخیره شد.")
        await show_settings(msg)
    elif state == "payout_card_number":
        card = digits_en(re.sub(r"[\s-]", "", text.strip()))
        if not card.isdigit() or len(card) < 16 or len(card) > 19:
            await msg.reply_text("شماره کارت معتبر رو بدون فاصله یا با فاصله ارسال کن.")
            return
        context.user_data["card_number_input"] = card
        context.user_data["state"] = "payout_owner_name"
        await msg.reply_text("نام و نام خانوادگی صاحب حساب رو ارسال کن.")
    elif state == "payout_owner_name":
        name = text.strip()
        if len(name) < 3:
            await msg.reply_text("نام و نام خانوادگی رو کامل ارسال کن.")
            return
        card = context.user_data.pop("card_number_input")
        with db() as c:
            cur = c.execute("INSERT INTO cards(admin_id,card_number,owner_name,created_at) VALUES(?,?,?,?)", (update.effective_user.id, card, name, now_iso()))
            card_id = cur.lastrowid
        context.user_data.pop("state", None)
        await show_card_confirmation(msg, update.effective_user.id, card_id)
    elif state == "payout_edit_card":
        card = digits_en(re.sub(r"[\s-]", "", text.strip()))
        if not card.isdigit() or len(card) < 16 or len(card) > 19:
            await msg.reply_text("شماره کارت معتبر رو ارسال کن.")
            return
        context.user_data["edit_card_number"] = card
        context.user_data["state"] = "payout_edit_name"
        await msg.reply_text("نام و نام خانوادگی صاحب حساب رو ارسال کن.")
    elif state == "payout_edit_name":
        card_id = context.user_data.get("selected_card_id")
        with db() as c:
            c.execute("UPDATE cards SET card_number=?,owner_name=? WHERE id=? AND admin_id=?", (context.user_data.pop("edit_card_number"), text.strip(), card_id, update.effective_user.id))
        context.user_data.pop("state", None)
        await show_card_confirmation(msg, update.effective_user.id, card_id)

async def show_admin_search_result(msg, target_id):
    with db() as c:
        a = c.execute("SELECT * FROM admins WHERE user_id=?", (target_id,)).fetchone()
        if not a:
            await msg.reply_text("این کاربر در فهرست ادمین‌های ربات پیدا نشد.", reply_markup=back_kb())
            return
        commissions = c.execute("SELECT * FROM commissions WHERE admin_id=? ORDER BY id DESC", (target_id,)).fetchall()
        cards = c.execute("SELECT * FROM cards WHERE admin_id=? AND active=1", (target_id,)).fetchall()
        latest = c.execute("SELECT MAX(created_at) AS d FROM leads WHERE admin_code=?", (a["code"],)).fetchone()["d"]
    paid = sum(1 for x in commissions if x["status"] == "paid")
    unpaid = sum(1 for x in commissions if x["status"] != "paid")
    text = (
        "🔎 <b>گزارش ادمین</b>\n\n"
        f"👤 نام: {profile_link(a)}\n"
        f"🔗 یوزرنیم: @{escape(a['username'] or 'ندارد')}\n"
        f"🆔 آیدی: <code>{a['user_id']}</code>\n"
        f"🔢 کد اختصاصی: ({escape(a['code'] or 'ندارد')})\n"
        f"🔹 این دوره: ({a['count']}) جذب\n"
        f"🔷 کل جذب‌ها: ({a['total']}) جذب\n"
        f"🎓 وضعیت آموزش: {'تکمیل شده' if a['training_done'] else 'تکمیل نشده'}\n"
        f"📅 شروع همکاری: {escape(a['activated_at'] or 'ثبت نشده')}\n"
        f"💳 کارت‌های ثبت‌شده: ({len(cards)})\n"
        f"✅ پورسانت پرداخت‌شده: ({paid})\n"
        f"⏳ پورسانت پرداخت‌نشده/در انتظار: ({unpaid})\n"
        f"🕒 آخرین جذب: {escape(latest or 'ثبت نشده')}"
    )
    await msg.reply_text(text, parse_mode="HTML", reply_markup=back_kb(), disable_web_page_preview=True)

async def show_lead_report(update, context, sort="latest"):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with db() as c:
        if sort == "most":
            rows = c.execute("SELECT * FROM admins ORDER BY count DESC,total DESC").fetchall()
        else:
            rows = c.execute("SELECT * FROM admins ORDER BY COALESCE((SELECT MAX(created_at) FROM leads WHERE leads.admin_code=admins.code), '') DESC").fetchall()
        lines = ["📊 <b>گزارش جذب</b>"]
        for a in rows:
            lines.append(
                f"👤 {profile_link(a)} | کد ({digits_en(a['code'] or '-')})\n"
                f"🔹 این دوره: ({a['count']}) جذب\n"
                f"🔷 کلا: ({a['total']}) جذب\n"
                "━━━━━━━━━━━━━━"
            )
    if len(lines) == 1:
        lines.append("هنوز ادمینی ثبت نشده.")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 بر اساس بیشترین جذب 📈", callback_data="report_most" if sort != "most" else "report_latest")],
        [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")],
    ])
    await update.effective_message.reply_text("\n\n".join(lines), parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)

async def show_finance_list(msg, status):
    with db() as c:
        if status == "paid":
            rows = c.execute("""SELECT co.*, a.name, a.username, a.code, ca.card_number, ca.owner_name
                FROM commissions co JOIN admins a ON a.user_id=co.admin_id
                LEFT JOIN cards ca ON ca.id=co.card_id
                WHERE co.status='paid' ORDER BY co.id DESC""").fetchall()
        else:
            rows = c.execute("""SELECT co.*, a.name, a.username, a.code, ca.card_number, ca.owner_name
                FROM commissions co JOIN admins a ON a.user_id=co.admin_id
                LEFT JOIN cards ca ON ca.id=co.card_id
                WHERE co.status IN ('pending','requested') ORDER BY co.id DESC""").fetchall()
    if not rows:
        await msg.reply_text("موردی برای نمایش وجود نداره.", reply_markup=back_kb())
        return
    buttons = []
    for r in rows:
        if status == "paid":
            dt = datetime.fromisoformat(r["paid_at"]) if r["paid_at"] else datetime.now(TZ)
            label = f"💰 واریز {money(r['amount'])} تومان - {date_fa(dt)} 💰"
        else:
            card = r["card_number"] or "کارت ثبت نشده"
            label = f"💳 {card} 💳"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"commission_{r['id']}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")])
    await msg.reply_text("فهرست واریزی‌ها:", reply_markup=InlineKeyboardMarkup(buttons))

async def show_admin_commissions(msg, admin_id):
    with db() as c:
        rows = c.execute("SELECT * FROM commissions WHERE admin_id=? ORDER BY id DESC", (admin_id,)).fetchall()
    if not rows:
        await msg.reply_text("هنوز سابقه پورسانتی برای شما ثبت نشده.", reply_markup=admin_menu())
        return
    lines = ["💰 سوابق پورسانت شما"]
    for r in rows:
        status = "پرداخت‌شده" if r["status"] == "paid" else ("در انتظار پرداخت" if r["status"] == "requested" else "در انتظار درخواست")
        dt = r["paid_at"] or r["request_at"] or r["created_at"]
        lines.append(f"━━━━━━━━━━━━━━\nمبلغ: {money(r['amount'])} تومان\nوضعیت: {status}\nتاریخ: {escape(dt)}")
    await msg.reply_text("\n".join(lines), reply_markup=admin_menu())

async def begin_payout_request(msg, context, admin_id):
    with db() as c:
        pending = c.execute("SELECT * FROM commissions WHERE admin_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (admin_id,)).fetchone()
        already_requested = c.execute("SELECT id FROM commissions WHERE admin_id=? AND status='requested' ORDER BY id DESC LIMIT 1", (admin_id,)).fetchone()
        cards = c.execute("SELECT * FROM cards WHERE admin_id=? AND active=1 ORDER BY id DESC", (admin_id,)).fetchall()
    if not pending:
        if already_requested:
            await msg.reply_text("درخواست پورسانت شما قبلا ثبت شده و در انتظار واریزه.", reply_markup=admin_menu())
        else:
            await msg.reply_text("در حال حاضر پورسانت قابل درخواست برای پرداخت نداری.", reply_markup=admin_menu())
        return
    context.user_data["payout_commission_id"] = pending["id"]
    if not cards:
        context.user_data["state"] = "payout_card_number"
        await msg.reply_text("لطفا شماره کارت بانکی به همراه:\nنام و نام خانوادگی صاحب حساب رو ارسال نمایید\n\n💢توجه کنید💢\nدر ارسال شماره کارت لطفا دقت کامل رو داشته باشید تا در واریز پورسانت مشکلی پیش نیاد.", reply_markup=back_kb())
        return
    buttons = [[InlineKeyboardButton(f"💳 {mask_card(c['card_number'])} 💳", callback_data=f"select_card_{c['id']}")] for c in cards]
    buttons.append([InlineKeyboardButton("➕ ثبت کارت جدید ➕", callback_data="add_card")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")])
    await msg.reply_text("لطفا کارت بانکی خود را از لیست زیر انتخاب کنید", reply_markup=InlineKeyboardMarkup(buttons))

def mask_card(card):
    card = str(card)
    if len(card) < 8:
        return card
    return card[:4] + "-****-****-" + card[-4:]

async def show_card_confirmation(msg, admin_id, card_id):
    with db() as c:
        card = c.execute("SELECT * FROM cards WHERE id=? AND admin_id=?", (card_id, admin_id)).fetchone()
    if not card:
        await msg.reply_text("کارت پیدا نشد.")
        return
    buttons = [[InlineKeyboardButton("✅ تأیید ✅", callback_data=f"confirm_card_{card_id}")], [InlineKeyboardButton("✏️ ویرایش ✏️", callback_data=f"edit_card_{card_id}")], [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")]]
    await msg.reply_text(f"اطلاعات ثبت‌شده شما\n\n💳 شماره کارت: {card['card_number']}\n👤 نام صاحب حساب: {escape(card['owner_name'])}\n\nلطفا اطلاعات رو بررسی و تأیید کن.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def create_commission(admin_id, code, lead_count):
    amount = int(get_setting("commission_amount", "200000"))
    with db() as c:
        existing = c.execute("SELECT id FROM commissions WHERE admin_id=? AND cycle_no=?", (admin_id, code)).fetchone() if False else None
        c.execute("INSERT INTO commissions(admin_id,amount,lead_count,status,created_at,cycle_no) VALUES(?,?,?,'pending',?,?)", (admin_id, amount, lead_count, now_iso(), int(code)))
        return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

async def owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, action):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        return False
    if action == "back":
        context.user_data.pop("state", None)
        await q.message.reply_text(owner_home_text(), reply_markup=owner_menu())
    elif action == "toggle_notifications":
        new = "0" if get_setting("new_lead_notifications", "1") == "1" else "1"
        set_setting("new_lead_notifications", new)
        await q.answer("اعلان جذب جدید روشن شد" if new == "1" else "اعلان جذب جدید خاموش شد", show_alert=False)
        await show_settings(q.message)
    elif action == "toggle_video":
        new = "0" if get_setting("training_video_enabled", "0") == "1" else "1"
        set_setting("training_video_enabled", new)
        await q.answer("ویدیوی آموزشی روشن شد" if new == "1" else "ویدیوی آموزشی خاموش شد", show_alert=False)
        await show_settings(q.message)
    elif action == "toggle_channel":
        new = "0" if get_setting("channel_enabled", "0") == "1" else "1"
        if new == "1" and (not get_setting("channel_id") or not get_setting("channel_url")):
            await q.answer("اول کانال رو از گزینه اضافه کردن کانال ثبت کن", show_alert=True)
            return True
        set_setting("channel_enabled", new)
        await q.answer("عضویت اجباری روشن شد" if new == "1" else "عضویت اجباری خاموش شد", show_alert=False)
        await show_settings(q.message)
    elif action == "channel_setup":
        context.user_data["state"] = "channel_id"
        await q.message.reply_text("آیدی کانال یا یوزرنیم کانال رو ارسال کن (ربات باید دسترسی بررسی عضویت داشته باشه).", reply_markup=back_kb())
    elif action == "upload_video":
        context.user_data["state"] = "upload_video"
        await q.message.reply_text("لطفا ویدیوی آموزشی رو همین‌جا ارسال کن.", reply_markup=back_kb())
    elif action == "commission_settings":
        context.user_data["state"] = "commission_threshold"
        await q.message.reply_text(f"حدنصاب فعلی: {get_setting('commission_threshold','40')} جذب\nمبلغ فعلی: {money(get_setting('commission_amount','200000'))} تومان\n\nحدنصاب جدید رو بفرست. تغییرات از دوره جدید اعمال می‌شن.", reply_markup=back_kb())
    elif action == "edit_texts":
        await q.message.reply_text("کدوم متن رو می‌خوای ویرایش کنی؟", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 متن کامنت 💬", callback_data="edit_comment")],
            [InlineKeyboardButton("👁️‍🗨️ متن پاسخگویی 👁️‍🗨️", callback_data="edit_reply")],
            [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="back")],
        ]))
    elif action == "edit_comment":
        context.user_data["state"] = "edit_comment"
        await q.message.reply_text("متن جدید کامنت رو ارسال کن.", reply_markup=back_kb())
    elif action == "edit_reply":
        context.user_data["state"] = "edit_reply"
        await q.message.reply_text("متن جدید پاسخگویی رو ارسال کن. برای درج کد از {code} استفاده کن.", reply_markup=back_kb())
    elif action == "bot_status":
        with db() as c:
            admins = c.execute("SELECT COUNT(*) n FROM admins").fetchone()["n"]
            leads = c.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
            pending = c.execute("SELECT COUNT(*) n FROM commissions WHERE status!='paid'").fetchone()["n"]
        await q.message.reply_text(f"📊 وضعیت ربات\n👥 ادمین‌ها: ({admins})\n🎯 جذب‌های ثبت‌شده: ({leads})\n💰 پورسانت‌های پرداخت‌نشده: ({pending})\n📢 عضویت اجباری: {'روشن' if get_setting('channel_enabled') == '1' else 'خاموش'}\n🎬 ویدیوی آموزشی: {'روشن' if get_setting('training_video_enabled') == '1' else 'خاموش'}", reply_markup=back_kb())
    elif action == "commission_pending_list":
        await show_finance_list(q.message, "requested")
    elif action.startswith("copy_card_"):
        cid = int(action.rsplit("_", 1)[1])
        with db() as c:
            r = c.execute("SELECT co.status,ca.card_number FROM commissions co LEFT JOIN cards ca ON ca.id=co.card_id WHERE co.id=?", (cid,)).fetchone()
        if r and r["card_number"]:
            await q.answer("شماره کارت برای کپی ارسال شد", show_alert=False)
            await q.message.reply_text(f"شماره کارت برای کپی:\n<code>{escape(r['card_number'])}</code>", parse_mode="HTML")
        else:
            await q.answer("شماره کارت این ادمین هنوز ثبت نشده", show_alert=True)
    elif action.startswith("send_receipt_"):
        cid = int(action.rsplit("_", 1)[1])
        context.user_data["receipt_commission_id"] = cid
        context.user_data["state"] = "receipt_upload"
        await q.message.reply_text("معراج عزیز، لطفا فیش واریزی رو ارسال کن", reply_markup=back_kb())
    elif action == "finance_paid":
        await show_finance_list(q.message, "paid")
    elif action == "finance_unpaid":
        await show_finance_list(q.message, "requested")
    elif action.startswith("commission_"):
        cid = int(action.split("_", 1)[1])
        await show_commission_detail(q.message, cid)
    elif action == "report_most":
        await show_lead_report(update, context, "most")
    elif action == "report_latest":
        await show_lead_report(update, context, "latest")
    elif action == "broadcast_send":
        chat_id = context.user_data.get("broadcast_chat_id")
        message_id = context.user_data.get("broadcast_message_id")
        if not chat_id or not message_id:
            await q.message.reply_text("پیامی برای ارسال پیدا نشد.", reply_markup=owner_menu())
            return True
        with db() as c:
            admins = c.execute("SELECT user_id FROM admins").fetchall()
        ok = 0
        fail = 0
        for a in admins:
            try:
                await context.bot.send_message(a["user_id"], "📩 از طرف کارفرما یک پیام جدید داری 📩\n\n🔻متن پیام🔻")
                await context.bot.copy_message(a["user_id"], chat_id, message_id)
                ok += 1
            except Exception as e:
                logger.warning("Broadcast failed to %s: %s", a["user_id"], e)
                fail += 1
        context.user_data.pop("state", None)
        context.user_data.pop("broadcast_chat_id", None)
        context.user_data.pop("broadcast_message_id", None)
        await q.message.reply_text(f"ارسال پیام همگانی تمام شد.\nموفق: ({ok})\nناموفق: ({fail})", reply_markup=owner_menu())
    elif action == "check_channel":
        await channel_gate_callback(q, context)
    elif action.startswith("select_card_"):
        card_id = int(action.rsplit("_", 1)[1])
        context.user_data["selected_card_id"] = card_id
        await show_card_confirmation(q.message, uid, card_id)
    elif action == "add_card":
        context.user_data["state"] = "payout_card_number"
        await q.message.reply_text("لطفا شماره کارت بانکی به همراه:\nنام و نام خانوادگی صاحب حساب رو ارسال نمایید\n\n💢توجه کنید💢\nدر ارسال شماره کارت لطفا دقت کامل رو داشته باشید تا در واریز پورسانت مشکلی پیش نیاد.", reply_markup=back_kb())
    elif action.startswith("confirm_card_"):
        card_id = int(action.rsplit("_", 1)[1])
        commission_id = context.user_data.get("payout_commission_id")
        with db() as c:
            card = c.execute("SELECT * FROM cards WHERE id=? AND admin_id=?", (card_id, uid)).fetchone()
            commission = c.execute("SELECT * FROM commissions WHERE id=? AND admin_id=?", (commission_id, uid)).fetchone() if commission_id else None
            if not card or not commission:
                await q.message.reply_text("درخواست پورسانت یا کارت پیدا نشد.")
                return True
            c.execute("UPDATE commissions SET status='requested',request_at=?,card_id=? WHERE id=?", (now_iso(), card_id, commission_id))
            a = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
        await q.message.reply_text("✅ درخواست شما با موفقیت ثبت شد ✅\n\nپورسانتتون حداکثر تا یک ساعت آینده براتون واریز می‌شه و فیش واریزی همین‌جا براتون ارسال میشه\n\nاز همکاری و اعتماد شما متشکریم 🫶", reply_markup=admin_menu())
        await safe_owner_message(context.bot, f"درخواست واریز پورسانت ثبت شد\n👤 ادمین: {a['name']}\n🔢 کد اختصاصی: ({a['code']})\n💰 مبلغ: ({money(commission['amount'])}) تومان\n💳 شماره کارت: {card['card_number']}\n👤 نام صاحب حساب: {card['owner_name']}\n📅 تاریخ درخواست: {date_fa()}\n\nدر پنل مدیریت مالی، واریزی‌های انجام‌نشده رو باز کن.")
    elif action.startswith("edit_card_"):
        card_id = int(action.rsplit("_", 1)[1])
        context.user_data["selected_card_id"] = card_id
        context.user_data["state"] = "payout_edit_card"
        await q.message.reply_text("شماره کارت جدید رو ارسال کن.", reply_markup=back_kb())
    elif action == "admin_video_seen":
        await send_training_stage1(q.message, context)
    return True

async def channel_gate_callback(q, context):
    user_id = q.from_user.id
    try:
        member = await context.bot.get_chat_member(get_setting("channel_id"), user_id)
        ok = member.status in ("member", "administrator", "creator") or (member.status == "restricted" and getattr(member, "is_member", False))
    except Exception as e:
        logger.warning("Channel membership recheck failed: %s", e)
        ok = False
    await q.answer("عضویت تأیید شد" if ok else "هنوز عضویتت تأیید نشده", show_alert=not ok)
    if ok:
        await q.message.reply_text("عضویت تأیید شد. برای ادامه /start رو بزن.")
    return ok

async def show_commission_detail(msg, commission_id):
    with db() as c:
        r = c.execute("""SELECT co.*, a.name, a.username, a.user_id, a.code, ca.card_number, ca.owner_name
            FROM commissions co JOIN admins a ON a.user_id=co.admin_id LEFT JOIN cards ca ON ca.id=co.card_id WHERE co.id=?""", (commission_id,)).fetchone()
    if not r:
        await msg.reply_text("درخواست پیدا نشد.", reply_markup=back_kb())
        return
    if r["status"] == "paid" and r["receipt_file_id"]:
        caption = f"واریز پورسانت برای ادمین کد {r['code']}\n📅 تاریخ: {date_fa(datetime.fromisoformat(r['paid_at']))}\n👤 اکانت: {profile_link(r)}"
        try:
            if r["receipt_type"] == "photo":
                await msg.reply_photo(r["receipt_file_id"], caption=caption, parse_mode="HTML")
            else:
                await msg.reply_document(r["receipt_file_id"], caption=caption, parse_mode="HTML")
        except Exception:
            await msg.reply_text(caption, parse_mode="HTML")
        return
    if r["status"] in ("requested", "pending"):
        card_number = r["card_number"]
        if not card_number:
            with db() as c:
                card = c.execute("SELECT card_number,owner_name FROM cards WHERE admin_id=? AND active=1 ORDER BY id DESC LIMIT 1", (r["admin_id"],)).fetchone()
            if card:
                card_number = card["card_number"]
                card_name = card["owner_name"]
            else:
                card_name = r["owner_name"]
        else:
            card_name = r["owner_name"]
        status_text = "در انتظار درخواست واریز" if r["status"] == "pending" else "در انتظار پرداخت"
        text = (f"💳 شماره کارت: {escape(card_number or 'هنوز ثبت نشده')}\n"
                f"👤 نام صاحب حساب: {escape(card_name or 'ثبت نشده')}\n"
                f"🔗 اکانت: {profile_link(r)}\n"
                f"📅 تاریخ درخواست: {date_fa(datetime.fromisoformat(r['request_at'])) if r['request_at'] else 'ثبت نشده'}\n"
                f"💰 مبلغ پورسانت: {money(r['amount'])} تومان\n"
                f"📌 وضعیت: {status_text}")
        pay_button = copy_card_button("💳 پرداخت 💳", card_number, f"copy_card_{r['id']}") if card_number else InlineKeyboardButton("💳 پرداخت 💳", callback_data=f"copy_card_{r['id']}")
        kb = InlineKeyboardMarkup([
            [pay_button],
            [InlineKeyboardButton("📩 ارسال فیش واریز 📩", callback_data=f"send_receipt_{r['id']}")],
            [InlineKeyboardButton("🔙 بازگشت 🔙", callback_data="finance_unpaid")],
        ])
        await msg.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        await msg.reply_text(f"درخواست پورسانت برای {r['name']}\nمبلغ: {money(r['amount'])} تومان\nوضعیت: در انتظار درخواست واریز", reply_markup=back_kb())

async def training_callback(update, context, action, admin):
    q = update.callback_query
    if action == "training_start":
        if get_setting("training_video_enabled", "0") == "1" and get_setting("video_file_id"):
            await q.message.reply_text("ویدیوی آموزشی")
            await context.bot.send_video(q.from_user.id, get_setting("video_file_id"), caption=get_setting("training_video_caption"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 ویدیو رو دیدم 🎬", callback_data="admin_video_seen")]]))
        else:
            await send_training_stage1(q.message, context)
    elif action == "training_stage2":
        await q.message.reply_text("2️⃣ <b>مرحله دوم: پاسخگویی به افراد 💬</b>\n\nبعد از اینکه کامنت گذاشتی، مراجعین به دایرکت شما پیام میدن و درباره کار سوال می‌پرسن.\n\nو شما فقط متن آماده زیر رو براشون می‌فرستی 👇", parse_mode="HTML")
        await q.message.reply_text("<blockquote>سلام دوست خوبم ❤️\n\nاین کار شرایط خاصی نداره و فقط به چند ساعت زمان آزاد در روز برای پاسخگویی به مراجعین داخل پلتفرم کاری نیاز داره.\n\nحقوق روزانه شما بین ۲۰۰ هزار تا ۱ میلیون و ۲۰۰ هزار تومان هست و پرداخت به‌صورت منظم انجام می‌شه.\n\n👥 ظرفیت استخدام: ۲۰ نفر</blockquote>", parse_mode="HTML")
        await q.message.reply_text("<blockquote>" + escape(get_setting("reply_text").replace("{code}", admin["code"] or "")) + "</blockquote>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔷 متوجه شدم، ادامه بده 🔷", callback_data="training_stage3")]]))
    elif action == "training_stage3":
        code = escape(admin["code"])
        await q.message.reply_text("<b>کد اختصاصی شما به عنوان ادمین جذبان</b>\n«کد " + code + " هست»\n\nهر وقت مراجعه‌کننده داخل تلگرام عبارت «استخدام کد" + code + "» رو ارسال کنه، متوجه می‌شم که از طرف شما معرفی شده و هم به شما و هم به کارفرما اطلاع میدم که یک جذب جدید از طریق شما انجام شده!\n\nتمام جذب‌های شما ثبت می‌شن و با هر جذب جدید، خودم بهتون اطلاع میدم.\n\nبه ازای هر " + get_setting("commission_threshold", "40") + " جذب، <b>" + money(get_setting("commission_amount", "200000")) + " تومان</b> پورسانت دریافت می‌کنید. بعد از رسیدن به حدنصاب، درخواست واریز رو از بخش «دریافت پورسانت» ثبت کن.\n\n<b>نگران تعداد جذب‌هاتون نباشید؛</b> جذب‌های باقی‌مونده به روز بعد منتقل می‌شن و از بین نمی‌رن.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔷 متوجه شدم، ادامه بده 🔷", callback_data="training_stage4")]]))
    elif action == "training_stage4":
        await q.message.reply_text("تعداد افرادی که جذب می‌کنید، به این بستگی داره که چقدر زمان می‌ذارید و چقدر کامنت زیر پست‌ها می‌ذارید.\n\nهرچی زیر پست‌های بیشتری کامنت بذارید، طبیعاتا شانس جذب افراد بیشتری رو دارید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💪 پایه‌ام، شروع کنیم 💪", callback_data="training_finish")]]))
    elif action == "training_finish":
        activated_at = datetime.now(TZ).strftime("%Y/%m/%d")
        with db() as c:
            c.execute("UPDATE admins SET training_done=1,activated_at=? WHERE user_id=?", (activated_at, q.from_user.id))
            a = c.execute("SELECT * FROM admins WHERE user_id=?", (q.from_user.id,)).fetchone()
        await q.message.reply_text(f"🎉 تبریک!\nاز این لحظه شما یکی از ادمین‌های مجموعه جذبان هستید.\n\nاز همین الان می‌تونی کارت رو شروع کنی و جذب‌هات رو ثبت کنی.\n\n📊 آمار شما\n👥 تعداد جذب‌ها: {a['total']} نفر\n💰 پورسانت هر {get_setting('commission_threshold','40')} جذب: {money(get_setting('commission_amount','200000'))} تومان\n📅 تاریخ شروع همکاری: {activated_at}\n\nموفق باشی!", reply_markup=admin_menu())
        await safe_owner_message(context.bot, f"ادمین آموزش را تکمیل کرد.\nنام: {a['name']}\nآیدی: {q.from_user.id}\nکد: {a['code']}\nتاریخ شروع: {activated_at}")

async def send_training_stage1(msg, context):
    await msg.reply_text("1️⃣ <b>مرحله اول: کامنت گذاری 💬</b>\n\nدوست خوبم، متن زیر رو کپی کن و زیر پست افراد معروف مثل بلاگرها و آرتیست‌ها کامنت بذار.\n\n👇 <b>متن کامنت</b> 👇", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁️‍🗨️ دریافت متن پاسخگویی 👁️‍🗨️", callback_data="training_stage2")]]))
    await msg.reply_text("<blockquote>" + escape(get_setting("comment_text")) + "</blockquote>", parse_mode="HTML")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    action = q.data
    if action not in {"toggle_notifications", "toggle_video", "toggle_channel", "check_channel"} and not action.startswith("copy_card_"):
        await q.answer()
    if uid == OWNER_ID:
        handled = await owner_callback(update, context, action)
        if handled:
            return
    if action == "check_channel":
        await channel_gate_callback(q, context)
        return
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
    if not admin:
        await q.message.reply_text("برای شروع، ابتدا ربات رو با /start فعال کن.")
        return
    if action == "back":
        context.user_data.pop("state", None)
        await q.message.reply_text("از منوی زیر استفاده کن.", reply_markup=admin_menu())
        return
    if action.startswith("training_") or action == "admin_video_seen":
        await training_callback(update, context, action, admin)
        return
    if action.startswith("select_card_"):
        card_id = int(action.rsplit("_", 1)[1])
        context.user_data["selected_card_id"] = card_id
        await show_card_confirmation(q.message, uid, card_id)
        return
    if action == "add_card":
        context.user_data["state"] = "payout_card_number"
        await q.message.reply_text("لطفا شماره کارت بانکی به همراه:\nنام و نام خانوادگی صاحب حساب رو ارسال نمایید\n\n💢توجه کنید💢\nدر ارسال شماره کارت لطفا دقت کامل رو داشته باشید تا در واریز پورسانت مشکلی پیش نیاد.", reply_markup=back_kb())
        return
    if action.startswith("confirm_card_"):
        card_id = int(action.rsplit("_", 1)[1])
        commission_id = context.user_data.get("payout_commission_id")
        with db() as c:
            card = c.execute("SELECT * FROM cards WHERE id=? AND admin_id=?", (card_id, uid)).fetchone()
            commission = c.execute("SELECT * FROM commissions WHERE id=? AND admin_id=?", (commission_id, uid)).fetchone() if commission_id else None
            if not card or not commission or commission["status"] == "paid":
                await q.message.reply_text("درخواست پورسانت یا کارت پیدا نشد.")
                return
            c.execute("UPDATE commissions SET status='requested',request_at=?,card_id=? WHERE id=?", (now_iso(), card_id, commission_id))
            a = c.execute("SELECT * FROM admins WHERE user_id=?", (uid,)).fetchone()
        await q.message.reply_text("✅ درخواست شما با موفقیت ثبت شد ✅\n\nپورسانتتون حداکثر تا یک ساعت آینده براتون واریز می‌شه و فیش واریزی همین‌جا براتون ارسال میشه\n\nاز همکاری و اعتماد شما متشکریم 🫶", reply_markup=admin_menu())
        await safe_owner_message(context.bot, f"درخواست واریز پورسانت ثبت شد\n👤 ادمین: {a['name']}\n🔢 کد اختصاصی: ({a['code']})\n💰 مبلغ: ({money(commission['amount'])}) تومان\n💳 شماره کارت: {card['card_number']}\n👤 نام صاحب حساب: {card['owner_name']}\n📅 تاریخ درخواست: {date_fa()}\n\nدر پنل مدیریت مالی، واریزی‌های انجام‌نشده رو باز کن.")
        return
    if action.startswith("edit_card_"):
        card_id = int(action.rsplit("_", 1)[1])
        context.user_data["selected_card_id"] = card_id
        context.user_data["state"] = "payout_edit_card"
        await q.message.reply_text("شماره کارت جدید رو ارسال کن.", reply_markup=back_kb())
        return
    if action == "my_stats":
        await q.message.reply_text(f"📊 آمار شما\n👥 جذب این دوره: {admin['count']}\n🎯 کل جذب‌ها: {admin['total']}\n📅 تاریخ شروع همکاری: {admin['activated_at'] or 'هنوز فعال نشده'}", reply_markup=admin_menu())
    elif action == "my_code":
        await q.message.reply_text(f"کد اختصاصی شما: استخدام کد{admin['code']}\nاین عبارت رو بدون تغییر برای متقاضیان بفرست.", reply_markup=admin_menu())
    elif action.startswith("copy_card_") and uid == OWNER_ID:
        cid = int(action.rsplit("_", 1)[1])
        with db() as c:
            r = c.execute("SELECT ca.card_number FROM commissions co LEFT JOIN cards ca ON ca.id=co.card_id WHERE co.id=?", (cid,)).fetchone()
        if r and r["card_number"]:
            # Standard Bot API cannot write to the owner's device clipboard.
            await q.answer("شماره کارت برای کپی ارسال شد", show_alert=False)
            await q.message.reply_text(f"شماره کارت برای کپی:\n<code>{escape(r['card_number'])}</code>", parse_mode="HTML")
        else:
            await q.answer("شماره کارت ثبت نشده", show_alert=True)
    elif action.startswith("send_receipt_") and uid == OWNER_ID:
        cid = int(action.rsplit("_", 1)[1])
        context.user_data["receipt_commission_id"] = cid
        context.user_data["state"] = "receipt_upload"
        await q.message.reply_text("معراج عزیز، لطفا فیش واریزی رو ارسال کن", reply_markup=back_kb())

async def receive_owner_media(update, context):
    if update.effective_user.id != OWNER_ID:
        return False
    state = context.user_data.get("state")
    msg = update.effective_message
    if state == "receipt_upload":
        if msg.photo:
            file_id, typ = msg.photo[-1].file_id, "photo"
        elif msg.document:
            file_id, typ = msg.document.file_id, "document"
        else:
            await msg.reply_text("لطفا تصویر فیش یا فایل فیش واریزی رو ارسال کن.")
            return True
        cid = context.user_data.get("receipt_commission_id")
        paid_at = now_iso()
        with db() as c:
            r = c.execute("SELECT co.*,a.name,a.username,a.user_id,a.code FROM commissions co JOIN admins a ON a.user_id=co.admin_id WHERE co.id=?", (cid,)).fetchone()
            if not r:
                await msg.reply_text("درخواست پورسانت پیدا نشد.")
                return True
            c.execute("UPDATE commissions SET status='paid',paid_at=?,receipt_file_id=?,receipt_type=? WHERE id=?", (paid_at, file_id, typ, cid))
        context.user_data.pop("state", None)
        context.user_data.pop("receipt_commission_id", None)
        await msg.reply_text(f"پرداخت پورسانت {r['name']} با موفقیت انجام شد معراج", reply_markup=owner_menu())
        dt = datetime.fromisoformat(paid_at)
        caption = f"فیش واریزی\n\nپورسانت شما با موفقیت واریز شد\n\n💰 مبلغ: {money(r['amount'])} تومان\n📅 تاریخ: {date_fa(dt)}\n🕒 ساعت: {dt.strftime('%H:%M')}"
        try:
            if typ == "photo":
                await context.bot.send_photo(r["admin_id"], file_id, caption=caption)
            else:
                await context.bot.send_document(r["admin_id"], file_id, caption=caption)
        except Exception as e:
            logger.error("Receipt delivery failed: %s", e)
            await safe_owner_message(context.bot, "پرداخت در دیتابیس ثبت شد، اما ارسال رسید به ادمین ناموفق بود. لطفا وضعیت ارسال را بررسی کن.")
        return True
    if state == "upload_video":
        if msg.video:
            set_setting("video_file_id", msg.video.file_id)
            context.user_data.pop("state", None)
            await msg.reply_text("ویدیوی آموزشی ذخیره شد.")
            await show_settings(msg)
            return True
        await msg.reply_text("لطفا ویدیو رو به صورت ویدیو ارسال کن.")
        return True
    return False

async def all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await receive_owner_media(update, context):
        return
    # Route owner and admin text inputs, and support forwarded messages for search.
    if update.effective_user and update.effective_message:
        if update.effective_user.id == OWNER_ID and context.user_data.get("state") in ("search_admin", "broadcast_message"):
            await owner_text_handler(update, context)
            return
        if update.effective_user.id != OWNER_ID and context.user_data.get("state"):
            await handle_owner_input(update, context, context.user_data["state"])
            return
        if update.effective_message.text:
            await owner_text_handler(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.effective_user.id == OWNER_ID:
        await update.effective_message.reply_text("عملیات لغو شد.", reply_markup=owner_menu())
    else:
        await update.effective_message.reply_text("عملیات لغو شد.", reply_markup=admin_menu())

async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Kept for compatibility with the existing command; owner panel uses search instead.
    if update.effective_user.id != OWNER_ID:
        return
    context.user_data["state"] = "search_admin"
    await update.effective_message.reply_text("لطفا یوزرنیم ادمین رو ارسال کن یا یک پیام ازش برام فوروارد کن", reply_markup=back_kb())

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await show_lead_report(update, context, "latest")

async def telethon_start(app):
    global client
    if not SESSION_STRING:
        await safe_owner_message(app.bot, "ربات مدیریتی اجرا شد؛ اتصال اکانت استخدام هنوز تنظیم نشده (SESSION_STRING خالی است).")
        return
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.start()
    except Exception as e:
        logger.exception("Failed to start Telethon client")
        await safe_owner_message(app.bot, f"خطا در راه‌اندازی اکانت استخدام تلگرام: {e}")
        return

    @client.on(events.NewMessage(incoming=True))
    async def incoming(event):
        try:
            if not event.is_private or event.out:
                return
            incoming_text = normalize_digits(event.raw_text or "")
            # Accept: استخدام کد10, استخدام کد 10, استخدام 10, استخدام۱۰.
            match = re.search(r"استخدام\s*(?:کد\s*)?(\d+)", incoming_text, re.IGNORECASE)
            if not match:
                return
            code = match.group(1)
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False):
                return
            with db() as c:
                admin = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()
                if not admin or not admin["training_done"]:
                    return
                # A person is only counted once, regardless of message formatting or repetition.
                exists = c.execute("SELECT 1 FROM leads WHERE user_id=?", (sender.id,)).fetchone()
                if exists:
                    return
                now = now_iso()
                c.execute("INSERT INTO leads(user_id,admin_code,created_at) VALUES(?,?,?)", (sender.id, code, now))
                threshold = int(admin["cycle_threshold"] or get_setting("commission_threshold", "40"))
                amount = int(admin["cycle_amount"] or get_setting("commission_amount", "200000"))
                c.execute("UPDATE admins SET count=count+1,total=total+1 WHERE code=?", (code,))
                updated = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()
                periods_reached = updated["count"] // threshold
                latest_commission_id = None
                if periods_reached:
                    latest_card = c.execute("SELECT id FROM cards WHERE admin_id=? AND active=1 ORDER BY id DESC LIMIT 1", (admin["user_id"],)).fetchone()
                    for i in range(periods_reached):
                        cycle_no = updated["total"] - (updated["count"] - threshold) + i
                        cur = c.execute("INSERT INTO commissions(admin_id,amount,lead_count,status,created_at,card_id,cycle_no) VALUES(?,?,?,'pending',?,?,?)", (admin["user_id"], amount, threshold, now, latest_card["id"] if latest_card else None, cycle_no))
                        latest_commission_id = cur.lastrowid
                    c.execute("UPDATE admins SET count=count-? WHERE code=?", (periods_reached * threshold, code))
                    # Apply current global settings to the next cycle, not the cycle just completed.
                    c.execute("UPDATE admins SET cycle_threshold=?,cycle_amount=? WHERE code=? AND count=0", (int(get_setting("commission_threshold", "40")), int(get_setting("commission_amount", "200000")), code))
                    updated = c.execute("SELECT * FROM admins WHERE code=?", (code,)).fetchone()
                reached_200 = updated["total"] >= 200 and not updated["notified_200"]
                if reached_200:
                    c.execute("UPDATE admins SET notified_200=1 WHERE code=?", (code,))
            lead_name = getattr(sender, "first_name", None) or "کاربر"
            username = getattr(sender, "username", None)
            user_link = f"https://t.me/{username}" if username else f"tg://user?id={sender.id}"
            if get_setting("new_lead_notifications", "1") == "1":
                await safe_owner_message(app.bot, f"🔔 جذب جدید ثبت شد 🔔\n\n👤 توسط: {admin['name']} | کد ({code})\n\n🔹 این دوره: ({updated['count']}) جذب\n🔷 کلا: ({updated['total']}) جذب", parse_mode="HTML")
                try:
                    await app.bot.send_message(admin["user_id"], f"یک جذب جدید ثبت شد.\nجذب این دوره: ({updated['count']})\nکل جذب‌ها: ({updated['total']})")
                except Exception as e:
                    logger.warning("Admin lead notice failed: %s", e)
                    await enqueue_notification(admin["user_id"], f"یک جذب جدید ثبت شد.\nجذب این دوره: ({updated['count']})\nکل جذب‌ها: ({updated['total']})")
            if periods_reached:
                threshold = int(get_setting("commission_threshold", "40"))
                for _ in range(periods_reached):
                    amount = int(get_setting("commission_amount", "200000"))
                    await context_send_admin(app.bot, admin["user_id"], f"🎉 تبریک دوست خوبم!\n\n{threshold} جذب شما با موفقیت تکمیل شد و پورسانت این دوره‌ات به مبلغ {money(amount)} تومان ثبت شد💰\n\n📩 بعد از واریز پورسانت، فیش واریزی برات همین‌جا ارسال می‌شه و می‌تونی سوابق پورسانتت رو هم از بخش «سوابق پورسانت» مشاهده کنی.\n\nبرای ثبت درخواست واریز، از منوی «دریافت پورسانت» اقدام کن.")
                    with db() as c:
                        comm = c.execute("SELECT co.id,ca.card_number FROM commissions co LEFT JOIN cards ca ON ca.id=co.card_id WHERE co.admin_id=? ORDER BY co.id DESC LIMIT 1", (admin["user_id"],)).fetchone()
                    commission_id = comm["id"] if comm else 0
                    card_number = comm["card_number"] if comm else None
                    pay_btn = copy_card_button("💳 پرداخت 💳", card_number, f"copy_card_{commission_id}") if card_number else InlineKeyboardButton("💳 پرداخت 💳", callback_data=f"copy_card_{commission_id}")
                    await safe_owner_message(app.bot, f"⚡️{admin['name']} {threshold} جذبش رو تکمیل کرد⚡️\n\n👤 ادمین: {admin['name']}\n🔢 کد اختصاصی: ({code})\n📊 جذب‌های تکمیل‌شده: ({threshold})\n💰 مبلغ پورسانت: ({money(amount)}) تومان\n📌 وضعیت: در انتظار درخواست واریز", reply_markup=InlineKeyboardMarkup([
                        [pay_btn],
                        [InlineKeyboardButton("📩 ارسال فیش واریز 📩", callback_data=f"send_receipt_{commission_id}")],
                    ]))
            if reached_200:
                await context_send_admin(app.bot, admin["user_id"], "🎉 تبریک! به هدف ۲۰۰ جذب رسیدی.")
            # Include the new lead details in the daily report source; database remains authoritative.
        except Exception:
            logger.exception("Error processing incoming message")

    await safe_owner_message(app.bot, "اتصال اکانت استخدام برقرار شد.")

async def context_send_admin(bot, admin_id, text):
    try:
        await bot.send_message(admin_id, text)
    except Exception as e:
        logger.warning("Could not send admin message %s: %s", admin_id, e)
        await enqueue_notification(admin_id, text)

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with db() as c:
        admins = c.execute("SELECT * FROM admins ORDER BY total DESC").fetchall()
        lines = [f"📊 گزارش ساعت ۲۲ جذبان ({today})"]
        for a in admins:
            count = c.execute("SELECT COUNT(*) cnt FROM leads WHERE admin_code=? AND created_at LIKE ?", (a["code"], f"{today}%")).fetchone()["cnt"]
            lines.append(f"{a['name']}: امروز ({count}) جذب | کل ({a['total']})")
    await safe_owner_message(context.bot, "\n".join(lines))

async def post_init(app):
    await flush_notification_queue(app.bot)
    await telethon_start(app)

def normalize_digits(value):
    return str(value).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))

def main():
    setup_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, all_messages))
    if app.job_queue:
        app.job_queue.run_daily(daily_report, time=datetime.strptime("22:00", "%H:%M").time().replace(tzinfo=TZ))
    else:
        logger.warning("JobQueue is not available. Daily report schedule could not be set.")
    app.run_polling()

if __name__ == "__main__":
    main()
