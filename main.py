
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

    user = update.effective_user

    with db() as c:
        admin = c.execute(
            "SELECT * FROM admins WHERE user_id=?", (uid,)
        ).fetchone()

        if not admin or not admin["code"]:
            used = {
                row["code"]
                for row in c.execute(
                    "SELECT code FROM admins WHERE code IS NOT NULL"
                )
            }
            number = 10
            while str(number) in used:
                number += 1
            code = str(number)

            if not admin:
                c.execute(
                    "INSERT INTO admins "
                    "(user_id, username, name, code) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        uid,
                        user.username,
                        user.first_name or str(uid),
                        code,
                    ),
                )
            else:
                c.execute(
                    "UPDATE admins SET code=? WHERE user_id=?",
                    (code, uid),
                )

        c.execute(
            "UPDATE admins SET username=?, name=? WHERE user_id=?",
            (user.username, user.first_name or str(uid), uid),
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
