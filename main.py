import os
import re
import html
import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# تنظیم لاگینگ برای ثبت خطاها بدون توقف ربات
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
SESSION_STRING = os.environ.get("SESSION_STRING", "")

DB = "jazban.db"
TZ = ZoneInfo("Asia/Tehran")
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
                total INTEGER DEFAULT 0,
                activated_at TEXT,
                training_done INTEGER DEFAULT 0,
                notified_200 INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                user_id INTEGER PRIMARY KEY,
                admin_code TEXT,
                created_at TEXT
            )
        """)

        columns = {
            row["name"] for row in c.execute("PRAGMA table_info(admins)")
        }
        migrations = {
            "activated_at": "ALTER TABLE admins ADD COLUMN activated_at TEXT",
            "training_done": "ALTER TABLE admins ADD COLUMN training_done INTEGER DEFAULT 0",
            "notified_200": "ALTER TABLE admins ADD COLUMN notified_200 INTEGER DEFAULT 0",
        }
        for column, sql in migrations.items():
            if column not in columns:
                c.execute(sql)

def new_code():
    with db() as c:
        used = {
            row["code"]
            for row in c.execute(
                "SELECT code FROM admins WHERE code IS NOT NULL"
            )
        }

    number = 10
    while str(number) in used:
        number += 1
    return str(number)

def escape(value):
    return html.escape(str(value or ""))

async def safe_owner_message(bot, text, **kwargs):
    """تابع امن برای ارسال پیام به مالک ربات جهت جلوگیری از کرش در صورت بروز خطا"""
    try:
        await bot.send_message(OWNER_ID, text, **kwargs)
    except Exception as e:
        logger.error(f"Failed to send message to owner: {e}")

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار من", callback_data="my_stats"),
            InlineKeyboardButton("🔢 کد من", callback_data="my_code"),
        ]
    ])

def welcome_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔎شروع آموزش🔎", callback_data="training_start"
        )
    ]])

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

        if not admin:
            await update.message.reply_text(
                "شما هنوز به‌عنوان ادمین ثبت نشده‌اید."
            )
            return

        if not admin["code"]:
            code = new_code()
            c.execute(
                "UPDATE admins SET code=? WHERE user_id=?", (code, uid)
            )
        else:
            code = admin["code"]

        c.execute(
            "UPDATE admins SET username=?, name=? WHERE user_id=?",
            (
                update.effective_user.username,
                update.effective_user.first_name or str(uid),
                uid,
            ),
        )
        admin = c.execute(
            "SELECT * FROM admins WHERE user_id=?", (uid,)
        ).fetchone()

    if admin["training_done"]:
        await update.message.reply_text(
            "آموزش شما قبلاً تکمیل شده. از منوی زیر استفاده کن.",
            reply_markup=main_menu(),
        )
        return

    await update.message.reply_text(
        "سلام دوست عزیزم\n"
        "به جمع ادمین‌های جذبان خوش اومدی ⭐️❤️\n\n"
        "من قراره مرحله‌به‌مرحله کار رو بهت آموزش بدم تا بتونی شروع کنی و درآمد داشته باشی.\n\n"
        "هر سوالی هم داشتی میتونی از کارفرمات بپرسی\n\n"
        "آماده‌ای شروع کنیم؟",
        reply_markup=welcome_keyboard(),
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
                    "INSERT OR IGNORE INTO admins (user_id, name) VALUES (?, ?)",
                    (target_id, str(target_id)),
                )

            awaiting_admin.discard(OWNER_ID)
            await msg.reply_text(
                "ادمین ثبت شد. بهش بگو ربات رو استارت کنه."
            )

            try:
                await context.bot.send_message(
                    target_id,
                    "برای شروع آموزش و فعال‌سازی، /start رو بزن.",
                )
            except Exception as e:
                logger.error(f"Failed to notify new admin: {e}")
            return

    if not target:
        await msg.reply_text(
            "آیدی عددی یا پیام فورواردشده رو بفرست."
        )
        return

    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO admins (user_id, username, name) VALUES (?, ?, ?)",
            (
                target.id,
                target.username,
                target.first_name or str(target.id),
            ),
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

async def admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    with db() as c:
        admin = c.execute(
            "SELECT * FROM admins WHERE user_id=?", (uid,)
        ).fetchone()

    if not admin:
        await query.message.reply_text(
            "شما هنوز به‌عنوان ادمین ثبت نشده‌اید."
        )
        return

    action = query.data

    if action == "training_start":
        await query.message.reply_text(
            "<b>مرحله اول</b>\n\n"
            "دوست خوبم، مرحله اول کار شما اینه که متنی که در ادامه بهتون میدم رو کپی کنید و وارد اینستاگرام بشید.\n\n"
            "با پیج شخصی یا هر پیجی که مایل بودید، زیر پست افراد معروف مثل آرتیست‌ها و بلاگرها، متن رو کامنت کنید\n"
            "👇 <b>متن کامنت</b> 👇",
            parse_mode="HTML",
        )

        await query.message.reply_text(
            "نیازمند چند اپراتور پاسخگویی\n"
            "درآمد روزانه بین ۲۰۰ تا ۱.۲۰۰ تومان\n"
            "هزینه استخدام کاملا رایگان!\n\n"
            "(دایرکت درخدمتم)",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "متوجه شدم، بریم مرحله بعد... ➡️",
                    callback_data="training_stage2",
                )
            ]]),
        )
        return

    if action == "training_stage2":
        await query.message.reply_text(
            "<b>مرحله دوم: پاسخگویی به افراد</b> 💬\n\n"
            "بعد از اینکه کامنت گذاشتید، مراجعین به دایرکتتون پیام میدن تا درباره کار سوال بپرسن\n\n"
            "شما دوتا متن زیر رو به عنوان توضیح کار براشون ارسال میکنید👇",
            parse_mode="HTML",
        )

        await query.message.reply_text(
            "سلام دوست خوبم ❤️\n\n"
            "این کار شرایط خاصی نداره و فقط به چند ساعت زمان آزاد در روز برای پاسخگویی به مراجعین داخل پلتفرم کاری نیاز داره.\n\n"
            "حقوق روزانه شما بین ۲۰۰ هزار تا ۱ میلیون و ۲۰۰ هزار تومان هست و پرداخت به‌صورت منظم انجام می‌شه.\n\n"
            "👥 ظرفیت استخدام: ۲۰ نفر"
        )

        code = escape(admin["code"])
        await query.message.reply_text(
            "جهت استخدام و دریافت اطلاعات بیشتر لطفا عبارت: "
            f"(استخدام کد{code})\n"
            "رو به ایدی زیر داخل تلگرام ارسال نمایید👇\n"
            "@jazban_work\n\n"
            "⚠️ توجه: اگر عبارت بالارو ارسال نکنید یا تغییرش بدید، پیام شما پاسخ داده نمیشود.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "متوجه شدم، ادامه بده",
                    callback_data="training_stage3",
                )
            ]]),
        )
        return

    if action == "training_stage3":
        code = escape(admin["code"])
        await query.message.reply_text(
            "<b>کد اختصاصی شما به عنوان ادمین جذبان</b>\n"
            f"«کد {code} هست»\n\n"
            f"هر وقت مراجعه‌کننده داخل تلگرام عبارت «استخدام کد{code}» رو ارسال کنه، "
            "متوجه می‌شم که از طرف شما معرفی شده و هم به شما و هم به کارفرما اطلاع میدم "
            "که یک جذب جدید از طریق شما انجام شده!\n\n"
            "تمام جذب‌های شما ثبت می‌شن و با هر جذب جدید، خودم بهتون اطلاع میدم.\n\n"
            "به ازای هر ۴۰ جذب، <b>۲۰۰ هزار تومان</b> پورسانت دریافت می‌کنید. "
            "به محض اینکه به ۴۰ جذب برسید، حقوقتون واریز می‌شه و شمارنده جذب‌هاتون "
            "برای شروع دوره بعدی از نو محاسبه می‌شه.\n\n"
            "<b>نگران تعداد جذب‌هاتون نباشید؛</b> اگه تا پایان روز به ۴۰ جذب نرسیدید، "
            "جذب‌های باقی‌مونده به روز بعد منتقل می‌شن و از بین نمی‌رن. پس برای رسیدن "
            "به حدنصاب وقت دارید و می‌تونید با تلاش بیشتر، جذب‌هاتون رو تکمیل کنید.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "متوجه شدم، ادامه بده",
                    callback_data="training_stage4",
                )
            ]]),
        )
        return

    if action == "training_stage4":
        await query.message.reply_text(
            "تعداد افرادی که جذب می‌کنید، به این بستگی داره که چقدر زمان می‌ذارید و چقدر کامنت زیر پست‌ها می‌ذارید.\n\n"
            "هرچی زیر پست‌های بیشتری کامنت بذارید، طبیعاتا شانس جذب افراد بیشتری رو دارید.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "پایه‌ام، شروع کنیم💪",
                    callback_data="training_finish",
                )
            ]]),
        )
        return

    if action == "training_finish":
        now = datetime.now(TZ)
        activated_at = now.strftime("%Y/%m/%d")

        with db() as c:
            c.execute(
                "UPDATE admins SET training_done=1, activated_at=? WHERE user_id=?",
                (activated_at, uid),
            )
            admin = c.execute(
                "SELECT * FROM admins WHERE user_id=?", (uid,)
            ).fetchone()

        await query.message.reply_text(
            "🎉 <b>تبریک!</b>\n"
            "از این لحظه شما یکی از ادمین‌های مجموعه جذبان هستید.\n\n"
            "از همین الان می‌تونی کارت رو شروع کنی و جذب‌هات رو ثبت کنی.\n\n"
            "📊 <b>آمار شما</b>\n"
            f"👥 تعداد جذب‌ها: {admin['total']} نفر\n"
            "🎯 هدف دوره: ۲۰۰ جذب\n"
            "💰 پورسانت هر ۴۰ جذب: ۲۰۰ هزار تومان\n"
            f"📅 تاریخ شروع همکاری: {escape(activated_at)}\n\n"
            "موفق باشی!",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

        await safe_owner_message(
            context.bot,
            "ادمین آموزش را تکمیل کرد.\n"
            f"نام: {admin['name']}\n"
            f"آیدی: {uid}\n"
            f"کد: {admin['code']}\n"
            f"تاریخ شروع: {activated_at}",
        )
        return

    if action == "my_stats":
        await query.message.reply_text(
            f"📊 آمار شما\n"
            f"👥 جذب این دوره: {admin['count']}\n"
            f"🎯 کل جذب‌ها: {admin['total']}\n"
            f"📅 تاریخ شروع همکاری: {admin['activated_at'] or 'هنوز فعال نشده'}",
            reply_markup=main_menu(),
        )
        return

    if action == "my_code":
        await query.message.reply_text(
            f"کد اختصاصی شما: استخدام کد{admin['code']}\n"
            "این عبارت رو بدون تغییر برای متقاضیان بفرست.",
            reply_markup=main_menu(),
        )

def normalize_digits(value):
    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    return value.translate(translation)

async def on_startup(app: Application):
    global client

    if not SESSION_STRING:
        await safe_owner_message(
            app.bot,
            "ربات مدیریتی اجرا شد؛ اتصال اکانت استخدام هنوز تنظیم نشده (SESSION_STRING خالی است).",
        )
        return

    try:
        client = TelegramClient(
            StringSession(SESSION_STRING), API_ID, API_HASH
        )
        await client.start()
    except Exception as e:
        logger.error(f"Failed to start Telethon client: {e}")
        await safe_owner_message(
            app.bot,
            f"خطا در راه‌اندازی اکانت استخدام تلگرام (Telethon): {e}"
        )
        return

    @client.on(events.NewMessage(incoming=True))
    async def incoming(event):
        if not event.is_private or event.out:
            return

        incoming_text = normalize_digits(event.raw_text or "")
        match = re.search(
            r"استخدام\s+کد\s*(\d+)", incoming_text, re.IGNORECASE
        )
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

            if not admin or not admin["training_done"]:
                return

            exists = c.execute(
                "SELECT 1 FROM leads WHERE user_id=?", (sender.id,)
            ).fetchone()

            if exists:
                return

            now = datetime.now(TZ).isoformat()

            c.execute(
                "INSERT INTO leads (user_id, admin_code, created_at) VALUES (?, ?, ?)",
                (sender.id, code, now),
            )
            c.execute(
                "UPDATE admins SET count=count+1, total=total+1 WHERE code=?",
                (code,),
            )

            updated = c.execute(
                "SELECT * FROM admins WHERE code=?", (code,)
            ).fetchone()

            reached_40 = updated["count"] >= 40
            reached_200 = (
                updated["total"] >= 200 and not updated["notified_200"]
            )
            period_count = updated["count"]

            if reached_40:
                c.execute(
                    "UPDATE admins SET count=0 WHERE code=?", (code,)
                )

            if reached_200:
                c.execute(
                    "UPDATE admins SET notified_200=1 WHERE code=?", (code,)
                )

        username = getattr(sender, "username", None)
        user_link = (
            f"https://t.me/{username}"
            if username else f"tg://user?id={sender.id}"
        )
        lead_name = getattr(sender, "first_name", None) or "کاربر"
        count_after = 0 if reached_40 else period_count

        await safe_owner_message(
            app.bot,
            "جذب جدید ثبت شد.\n"
            f"ادمین: {admin['name']}\n"
            f"آیدی ادمین: {admin['user_id']}\n"
            f"کد ادمین: {code}\n"
            f"کاربر: {lead_name}\n"
            f"آیدی کاربر: {sender.id}\n"
            f"لینک: {user_link}\n"
            f"جذب این دوره: {count_after}\n"
            f"کل جذب‌ها: {updated['total']}\n"
            f"زمان: {now}",
        )

        try:
            await app.bot.send_message(
                admin["user_id"],
                "یک جذب جدید ثبت شد.\n"
                f"جذب این دوره: {count_after}\n"
                f"کل جذب‌ها: {updated['total']}",
            )

            if reached_40:
                await app.bot.send_message(
                    admin["user_id"],
                    "به ۴۰ جذب رسیدی؛ پاداش این دوره ۲۰۰ هزار تومان است و شمارنده دوره جدید شروع شد.",
                )

            if reached_200:
                await app.bot.send_message(
                    admin["user_id"],
                    "🎉 تبریک! به هدف ۲۰۰ جذب رسیدی. کارفرما از تکمیل هدفت مطلع شد.",
                )

        except Exception as e:
            logger.error(f"Failed to send notification to admin {admin['user_id']}: {e}")

        if reached_40:
            await safe_owner_message(
                app.bot,
                f"ادمین {admin['name']} (کد {code}) به ۴۰ جذب رسید؛ "
                "پرداخت ۲۰۰ هزار تومان باید بررسی شود.",
            )

        if reached_200:
            await safe_owner_message(
                app.bot,
                "🎉 ادمین به هدف ۲۰۰ جذب رسید!\n"
                f"نام: {admin['name']}\n"
                f"آیدی: {admin['user_id']}\n"
                f"کد: {code}\n"
                f"کل جذب‌ها: {updated['total']}",
            )

    await safe_owner_message(app.bot, "اتصال اکانت استخدام برقرار شد.")

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM admins").fetchall()

    lines = ["📊 گزارش ساعت ۱۰ شب جذبان"]
    for a in rows:
        lines.append(
            f"{a['name']}: این دوره {a['count']} جذب | کل {a['total']}"
        )

    await safe_owner_message(context.bot, "\n".join(lines))

def main():
    setup_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, receive_admin)
    )

    if app.job_queue:
        app.job_queue.run_daily(
            daily_report,
            time=datetime.strptime("22:00", "%H:%M").time().replace(
                tzinfo=TZ
            ),
        )

    app.run_polling()

if __name__ == "__main__":
    main()
