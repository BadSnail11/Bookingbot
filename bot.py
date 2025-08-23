
import os
import re
import logging
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    ConversationHandler, CallbackQueryHandler
)

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Load env ----
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in your .env")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}
VENUE_NAME = os.getenv("VENUE_NAME", "Your Venue")
VENUE_PHONE = os.getenv("VENUE_PHONE", "+00 000 000 000")
VENUE_ADDRESS = os.getenv("VENUE_ADDRESS", "City, Street 1")
VENUE_HOURS = os.getenv("VENUE_HOURS", "Daily 10:00–22:00")
RESERVATION_DURATION_MIN = int(os.getenv("RESERVATION_DURATION_MIN", "120"))
TIME_SLOT_STEP_MIN = int(os.getenv("TIME_SLOT_STEP_MIN", "30"))
DB_PATH = os.getenv("DB_PATH", "reservations.db")

# Booking window config
MIN_ADVANCE_DAYS = int(os.getenv("MIN_ADVANCE_DAYS", "1"))  # 1 => no same‑day
ONLY_TOMORROW = os.getenv("ONLY_TOMORROW", "false").lower() in ("1", "true", "yes", "y")

# Default timezone for user input
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Europe/Moscow"))

# ---- Weekly schedule ----
# Monday=0 ... Sunday=6
WEEKLY_RULES = {
    0: (time(16,0), time(22,30)),  # Mon
    1: (time(16,0), time(22,30)),  # Tue
    2: (time(16,0), time(22,30)),  # Wed
    3: (time(16,0), time(22,30)),  # Thu
    4: (time(16,0), time(23,30)),  # Fri
    5: (time(14,0), time(23,30)),  # Sat
    6: (time(14,0), time(22,30)),  # Sun
}

# ---- DB helpers ----
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def ensure_user(con, update: Update):
    chat = update.effective_chat
    user = update.effective_user
    con.execute(
        "INSERT OR IGNORE INTO users (chat_id, first_name, last_name, username) VALUES (?,?,?,?)",
        (chat.id, user.first_name, user.last_name, user.username),
    )
    con.commit()
    cur = con.execute("SELECT id FROM users WHERE chat_id=?", (chat.id,))
    return cur.fetchone()["id"]

# ---- Conversation states ----
DATE, TIME, PARTY, NAME, PHONE, CONFIRM = range(6)

# Simple validators
PHONE_RE = re.compile(r"^[+\d][\d\-()\s]{5,}$")

def _human_contacts() -> str:
    return (f"*{VENUE_NAME}*\n"
            f"📍 {VENUE_ADDRESS}\n"
            f"📞 {VENUE_PHONE}\n"
            f"🕒 {VENUE_HOURS}")

def _slots_for_date(d):
    open_t, last_t = WEEKLY_RULES.get(d.weekday(), (None, None))
    if not open_t or not last_t:
        return []
    slots = []
    cur_dt = datetime.combine(d, open_t)
    last_dt = datetime.combine(d, last_t)
    step = timedelta(minutes=TIME_SLOT_STEP_MIN)
    while cur_dt <= last_dt:
        slots.append(cur_dt.time())
        cur_dt += step
    return slots

async def start(update: Update, context: ContextTypes.context):
    text = (
        "Привет! Я бот для бронирования столов.\n\n"
        "Команды:\n"
        "/book — забронировать стол\n"
        "/my — мои бронирования\n"
        "/contacts — контакты заведения\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.context):
    await update.message.reply_text(
        "Чтобы забронировать столик, используйте /book и следуйте шагам. "
        "Мы принимаем брони только не в день обращения "
        f"(минимум за {MIN_ADVANCE_DAYS} дн.)."
    )

async def contacts_cmd(update: Update, context: ContextTypes.context):
    await update.message.reply_markdown(_human_contacts())

def _date_choices():
    today = datetime.now(LOCAL_TZ).date()
    first = today + timedelta(days=MIN_ADVANCE_DAYS)
    dates = []
    if ONLY_TOMORROW:
        dates = [first]
    else:
        for i in range(0, 6):
            dates.append(first + timedelta(days=i))
    return dates

async def book(update: Update, context: ContextTypes.context):
    choices = _date_choices()
    keyboard, row = [], []
    for i, d in enumerate(choices, start=1):
        row.append(d.strftime("%Y-%m-%d"))
        if i % 3 == 0:
            keyboard.append(row); row = []
    if row: keyboard.append(row)

    await update.message.reply_text(
        "Выберите дату (YYYY-MM-DD) или введите вручную:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return DATE

def parse_date(text: str):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None

def parse_time(text: str):
    for fmt in ("%H:%M", "%H.%M", "%H%M"):
        try:
            return datetime.strptime(text.strip(), fmt).time()
        except ValueError:
            continue
    return None

async def book_date(update: Update, context: ContextTypes.context):
    d = parse_date(update.message.text)
    today = datetime.now(LOCAL_TZ).date()
    min_date = today + timedelta(days=MIN_ADVANCE_DAYS)
    if not d:
        await update.message.reply_text("Пожалуйста, укажите корректную дату, формат YYYY-MM-DD.")
        return DATE
    if d < min_date:
        if ONLY_TOMORROW:
            await update.message.reply_text("Бронь в день обращения не доступна. Можно бронировать только на завтра.")
        else:
            await update.message.reply_text(f"Бронь в день обращения не доступна. Выберите дату начиная с {min_date.isoformat()}.")
        return DATE
    if ONLY_TOMORROW and d != min_date:
        await update.message.reply_text("Сейчас принимаем брони только на завтра. Выберите завтрашнюю дату.")
        return DATE

    slots = _slots_for_date(d)
    if not slots:
        await update.message.reply_text("В этот день бронирования не принимаются. Выберите другой день.")
        return DATE
    context.user_data["date"] = d

    labels = [t.strftime("%H:%M") for t in slots]
    keyboard = [labels[i:i+4] for i in range(0, len(labels), 4)]
    await update.message.reply_text(
        "Во сколько? (например, 19:30)",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return TIME

async def book_time(update: Update, context: ContextTypes.context):
    t = parse_time(update.message.text)
    d = context.user_data.get("date")
    if not t or not d:
        await update.message.reply_text("Пожалуйста, укажите время в формате HH:MM, например 19:30.")
        return TIME
    allowed = _slots_for_date(d)
    if t not in allowed:
        open_t, last_t = WEEKLY_RULES[d.weekday()]
        await update.message.reply_text(
            f"В этот день принимаем с {open_t.strftime('%H:%M')} до {last_t.strftime('%H:%M')} (последняя запись). "
            "Выберите время из предложенных."
        )
        return TIME

    context.user_data["time"] = t
    keyboard = [["2","3","4"],["5","6","7"],["8"]]
    await update.message.reply_text(
        "Сколько человек?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return PARTY

async def book_party(update: Update, context: ContextTypes.context):
    try:
        party = int(update.message.text.strip())
        if party <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Укажите число гостей (целое положительное).")
        return PARTY
    context.user_data["party"] = party
    await update.message.reply_text("Ваше имя и фамилия:", reply_markup=ReplyKeyboardRemove())
    return NAME

async def book_name(update: Update, context: ContextTypes.context):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Имя слишком короткое. Повторите.")
        return NAME
    context.user_data["name"] = name
    await update.message.reply_text("Ваш номер телефона (с кодом страны, например +375...):")
    return PHONE

async def book_phone(update: Update, context: ContextTypes.context):
    phone = update.message.text.strip()
    if not PHONE_RE.match(phone):
        await update.message.reply_text("Похоже, номер некорректен. Введите еще раз (например +375123456789).")
        return PHONE
    context.user_data["phone"] = phone

    d = context.user_data["date"]
    t = context.user_data["time"]
    party = context.user_data["party"]
    starts_local = datetime.combine(d, t).replace(tzinfo=LOCAL_TZ)
    ends_local = starts_local + timedelta(minutes=RESERVATION_DURATION_MIN)

    context.user_data["starts_local"] = starts_local
    context.user_data["ends_local"] = ends_local

    text = (
        f"Проверьте данные:\n"
        f"📅 Дата: {d.isoformat()}\n"
        f"⏰ Время: {t.strftime('%H:%M')}\n"
        f"👥 Гостей: {party}\n"
        f"🧾 Имя: {context.user_data['name']}\n"
        f"📞 Телефон: {phone}\n\n"
        f"Подтверждаете?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Отправить заявку", callback_data="confirm_yes"),
         InlineKeyboardButton("Изменить", callback_data="confirm_no")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard)
    return CONFIRM

def find_available_table(con, party_size: int, starts_utc: str, ends_utc: str):
    sql = """
    SELECT id, name, capacity FROM tables
    WHERE capacity >= ?
    AND id NOT IN (
        SELECT table_id FROM reservations
        WHERE status IN ('pending','confirmed')
        AND table_id IS NOT NULL
        AND starts_at < ? AND ends_at > ?
    )
    ORDER BY capacity ASC
    """
    cur = con.execute(sql, (party_size, ends_utc, starts_utc))
    return cur.fetchone()

async def confirm_callback(update: Update, context: ContextTypes.context):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_no":
        await query.edit_message_text("Окей, начнем заново: /book")
        return ConversationHandler.END

    with db() as con:
        user_id = ensure_user(con, update)
        starts_local = context.user_data["starts_local"]
        ends_local = context.user_data["ends_local"]
        starts_utc = starts_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        ends_utc = ends_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

        table = find_available_table(
            con,
            party_size=context.user_data["party"],
            starts_utc=starts_utc,
            ends_utc=ends_utc
        )

        table_id = table["id"] if table else None
        con.execute(
            """INSERT INTO reservations (user_id, table_id, name, phone, party_size, starts_at, ends_at, status)
               VALUES (?,?,?,?,?,?,?, 'pending')""",
            (
                user_id,
                table_id,
                context.user_data["name"],
                context.user_data["phone"],
                context.user_data["party"],
                starts_utc,
                ends_utc,
            )
        )
        res_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()

    if table:
        msg = (f"Заявка №{res_id} отправлена ✅\n"
               f"Пока бронь в ожидании подтверждения администратора.\n"
               f"Предварительно доступен стол {table['name']} (вмещает до {table['capacity']} гостей).")
    else:
        msg = (f"Заявка №{res_id} отправлена ✅\n"
               f"Пока бронь в ожидании подтверждения администратора.\n"
               f"Сейчас нет подходящих свободных столов на это время — "
               f"администратор свяжется для альтернативы.")
    msg += "\n\nЕсли появятся вопросы или захотите отменить, свяжитесь с заведением:\n" + _human_contacts()
    await query.edit_message_text(msg)
    return ConversationHandler.END

async def my_reservations(update: Update, context: ContextTypes.context):
    with db() as con:
        cur = con.execute("SELECT id FROM users WHERE chat_id=?", (update.effective_chat.id,))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("У вас пока нет бронирований. Нажмите /book чтобы создать.")
            return
        user_id = row["id"]
        res = con.execute(
            """SELECT r.id, r.status, r.starts_at, r.ends_at, r.party_size, r.table_id, t.name as table_name
               FROM reservations r
               LEFT JOIN tables t ON t.id = r.table_id
               WHERE r.user_id=? AND r.ends_at >= datetime('now')
               ORDER BY r.starts_at ASC
            """,
            (user_id,)
        ).fetchall()
    if not res:
        await update.message.reply_text("Активных бронирований нет. Нажмите /book чтобы создать.")
        return
    lines = []
    for r in res:
        starts = datetime.fromisoformat(r["starts_at"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        status_map = {"pending":"⏳ ожидание","confirmed":"✅ подтверждено","canceled":"❌ отменено"}
        table_label = r["table_name"] or "—"
        lines.append(f"№{r['id']} • {starts} • {status_map.get(r['status'], r['status'])} • Стол: {table_label} • Гостей: {r['party_size']}")
    lines.append("\nХотите отменить или есть вопросы?\n" + _human_contacts())
    await update.message.reply_markdown("\n".join(lines))

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS if ADMIN_IDS else False

async def admin_pending(update: Update, context: ContextTypes.context):
    if not is_admin(update.effective_user.id):
        return
    with db() as con:
        rows = con.execute(
            """SELECT r.id, r.name, r.phone, r.party_size, r.starts_at, r.ends_at, r.table_id, t.name as table_name, u.chat_id
               FROM reservations r
               LEFT JOIN tables t ON t.id = r.table_id
               JOIN users u ON u.id = r.user_id
               WHERE r.status='pending' ORDER BY r.starts_at ASC"""
        ).fetchall()
    if not rows:
        await update.message.reply_text("Нет ожидающих заявок.")
        return
    lines = ["Ожидают подтверждения:"]
    for r in rows:
        starts = datetime.fromisoformat(r["starts_at"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        table_label = r["table_name"] or "—"
        lines.append(f"#{r['id']} • {starts} • {r['party_size']} чел • {r['name']} {r['phone']} • стол: {table_label}")
    await update.message.reply_text("\n".join(lines))

async def admin_confirm(update: Update, context: ContextTypes.context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /confirm <reservation_id>")
        return
    res_id = context.args[0]
    with db() as con:
        row = con.execute(
            """SELECT r.id, r.status, r.table_id, t.name as table_name, u.chat_id, r.starts_at
               FROM reservations r
               LEFT JOIN tables t ON t.id = r.table_id
               JOIN users u ON u.id = r.user_id
               WHERE r.id=?""",
            (res_id,)
        ).fetchone()
        if not row:
            await update.message.reply_text("Бронь не найдена.")
            return
        if row["status"] == "confirmed":
            await update.message.reply_text("Бронь уже подтверждена.")
            return
        if row["table_id"] is None:
            starts_utc = row["starts_at"]
            ends_utc = (datetime.fromisoformat(starts_utc).replace(tzinfo=ZoneInfo("UTC"))
                        + timedelta(minutes=RESERVATION_DURATION_MIN)).strftime("%Y-%m-%d %H:%M:%S")
            res2 = con.execute("SELECT party_size FROM reservations WHERE id=?", (res_id,)).fetchone()
            table = find_available_table(con, res2["party_size"], starts_utc, ends_utc)
            if not table:
                await update.message.reply_text("Подходящих столов нет для подтверждения.")
                return
            con.execute("UPDATE reservations SET table_id=? WHERE id=?", (table["id"], res_id))
            table_name = table["name"]
        else:
            table_name = row["table_name"] or "—"

        con.execute("UPDATE reservations SET status='confirmed' WHERE id=?", (res_id,))
        con.commit()
        chat_id = row["chat_id"]

    await update.message.reply_text(f"Бронь #{res_id} подтверждена, стол {table_name}. Уведомляю пользователя.")
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Ваша бронь №{res_id} подтверждена! До встречи в {VENUE_NAME}. Стол: {table_name}."
        )
    except Exception as e:
        logger.exception("Failed to notify user: %s", e)

async def admin_cancel(update: Update, context: ContextTypes.context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /cancel_res <reservation_id>")
        return
    res_id = context.args[0]
    with db() as con:
        row = con.execute(
            """SELECT r.id, r.status, u.chat_id FROM reservations r
               JOIN users u ON u.id = r.user_id
               WHERE r.id=?""",
            (res_id,)
        ).fetchone()
        if not row:
            await update.message.reply_text("Бронь не найдена.")
            return
        if row["status"] == "canceled":
            await update.message.reply_text("Бронь уже отменена.")
            return
        con.execute("UPDATE reservations SET status='canceled' WHERE id=?", (res_id,))
        con.commit()
        chat_id = row["chat_id"]
    await update.message.reply_text(f"Бронь #{res_id} отменена. Оповещаю пользователя.")
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ К сожалению, бронь №{res_id} была отменена. Если есть вопросы, свяжитесь с нами:\n{_human_contacts()}"
        )
    except Exception as e:
        logger.exception("Failed to notify user: %s", e)

async def cancel(update: Update, context: ContextTypes.context):
    await update.message.reply_text("Окей, бронирование отменено. Нажмите /book чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def build_app():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_time)],
            PARTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_party)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_name)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
            CONFIRM: [CallbackQueryHandler(confirm_callback, pattern="^confirm_(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("contacts", contacts_cmd))
    app.add_handler(conv)
    app.add_handler(CommandHandler("my", my_reservations))

    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("confirm", admin_confirm))
    app.add_handler(CommandHandler("cancel_res", admin_cancel))

    return app

def main():
    app = build_app()
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
