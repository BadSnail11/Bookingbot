
import os
import re
import logging
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Tuple

import requests
from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, Bot
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    ConversationHandler, CallbackQueryHandler
)

import asyncio

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in your .env")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}
ADMIN_ALERT_BOT_TOKEN = os.getenv("ADMIN_ALERT_BOT_TOKEN", "")
ADMIN_ALERT_CHAT_IDS = [int(x) for x in os.getenv("ADMIN_ALERT_CHAT_IDS","").replace(" ","").split(",") if x.strip().isdigit()]

VENUE_NAME = os.getenv("VENUE_NAME", "Your Venue")
VENUE_PHONE = os.getenv("VENUE_PHONE", "+00 000 000 000")
VENUE_ADDRESS = os.getenv("VENUE_ADDRESS", "City, Street 1")
VENUE_HOURS = os.getenv("VENUE_HOURS", "Daily 10:00–22:00")

RESERVATION_DURATION_MIN = int(os.getenv("RESERVATION_DURATION_MIN", "120"))
TIME_SLOT_STEP_MIN = int(os.getenv("TIME_SLOT_STEP_MIN", "30"))
MIN_ADVANCE_DAYS = int(os.getenv("MIN_ADVANCE_DAYS", "1"))
ONLY_TOMORROW = os.getenv("ONLY_TOMORROW", "false").lower() in ("1", "true", "yes", "y")
REMINDER_HOURS_BEFORE = int(os.getenv("REMINDER_HOURS_BEFORE", "2"))
DAILY_RESERVATION_LIMIT = int(os.getenv("DAILY_RESERVATION_LIMIT", "2"))
RES_LIMIT_SCOPE = os.getenv("RES_LIMIT_SCOPE", "global").lower()
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Europe/Moscow"))
AUTO_CONFIRM_MAX_PARTY = int(os.getenv("AUTO_CONFIRM_MAX_PARTY", "4"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
if not SUPABASE_URL or not SUPABASE_API_KEY:
    raise RuntimeError("Please set SUPABASE_URL and SUPABASE_API_KEY in your .env")

REST_BASE = SUPABASE_URL.rstrip("/") + "/rest/v1"
SB_HEADERS = {
    "apikey": SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

BLOCKED_DATES = [str(x) for x in os.getenv("BLOCKED_DATES","").replace(" ","").split(",") if x.strip()]

def sb_get(table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    r = requests.get(f"{REST_BASE}/{table}", headers=SB_HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def sb_post(table: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(f"{REST_BASE}/{table}", headers={**SB_HEADERS, "Prefer":"return=representation"}, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) else data

def sb_patch(table: str, params: Dict[str, str], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = requests.patch(f"{REST_BASE}/{table}", headers={**SB_HEADERS, "Prefer":"return=representation"}, params=params, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else [data]

WEEKLY_RULES = {
    0: (time(16,0), time(22,30)),
    1: (time(16,0), time(22,30)),
    2: (time(16,0), time(22,30)),
    3: (time(16,0), time(22,30)),
    4: (time(16,0), time(23,30)),
    5: (time(14,0), time(23,30)),
    6: (time(14,0), time(22,30)),
}

DATE, TIME_STATE, PARTY, SETS, NAME, PHONE, COMMENT, CONFIRM = range(8)
PHONE_RE = re.compile(r"^[+\d][\d\-()\s]{5,}$")

def _human_contacts() -> str:
    return (f"*{VENUE_NAME}*\n"
            f"📍 {VENUE_ADDRESS}\n"
            f"📞 {VENUE_PHONE}\n"
            f"🕒 {VENUE_HOURS}")

def _slots_for_date(d) -> List[time]:
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

def _utc_iso(dt_local: datetime) -> str:
    return dt_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")

def _parse_utc_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt

def _date_choices():
    today = datetime.now(LOCAL_TZ).date()
    first = today + timedelta(days=MIN_ADVANCE_DAYS)
    if ONLY_TOMORROW:
        return [first]
    return [first + timedelta(days=i) for i in range(0,10)]

def _utc_bounds_for_local_date(d) -> Tuple[str, str]:
    """Вернёт UTC-границы для локальной календарной даты d: [start, end)."""
    start_local = datetime.combine(d, time(0, 0)).replace(tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return _utc_iso(start_local), _utc_iso(end_local)

def sb_count_reservations_in_day(day_start_utc_iso: str, day_end_utc_iso: str, user_id: int | None = None) -> int:
    """
    Считает брони на дату (исключая canceled). Учитываем pending/confirmed/stopped.
    Если передан user_id — считаем только для этого пользователя (для режима per_user).
    """
    params = {
        "select": "id",
        "status": "in.(confirmed,pending)",
        # PostgREST: комбинированный фильтр через and=
        "and": f"(created_at.gte.{day_start_utc_iso},created_at.lt.{day_end_utc_iso})",
    }
    if user_id is not None:
        params["user_id"] = f"eq.{user_id}"
    rows = sb_get("reservations", params)
    return len(rows)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS if ADMIN_IDS else False

def sb_ensure_user(chat_id: int, first_name: str, last_name: str, username: str) -> int:
    rows = sb_get("users", {"select":"id", "chat_id": f"eq.{chat_id}"})
    if rows:
        return rows[0]["id"]
    row = sb_post("users", {
        "chat_id": chat_id,
        "first_name": first_name,
        "last_name": last_name,
        "username": username
    })
    return row["id"]

def sb_find_available_table(party_size: int, starts_utc_iso: str, ends_utc_iso: str) -> Optional[Dict[str, Any]]:
    # reserved = sb_get("reservations", {
    #     "select":"table_id",
    #     "status":"in.(pending,confirmed)",
    #     "table_id":"not.is.null",
    #     "starts_at":f"lt.{ends_utc_iso}",
    #     "ends_at":f"gt.{starts_utc_iso}",
    # })
    # reserved_ids = {r["table_id"] for r in reserved if r.get("table_id") is not None}
    # tables = sb_get("tables", {
    #     "select":"id,name,capacity",
    #     "capacity":f"gte.{party_size}",
    #     "order":"capacity.asc"
    # })
    # for t in tables:
    #     if t["id"] not in reserved_ids:
    #         return t
    # return None
    reserved_ids = sb_reserved_table_ids(starts_utc_iso, ends_utc_iso)

    # кандидаты по вместимости — отсортированы от меньшей к большей
    tables = sb_get("tables", {
        "select": "id,name,capacity",
        "capacity": f"gte.{party_size}",
        "order": "capacity.asc"
    })
    for t in tables:
        if t["id"] not in reserved_ids:
            return t
    return None

def sb_insert_reservation(user_id: int, table_id: Optional[int], name: str, phone: str,
                          party_size: int, starts_utc_iso: str, ends_utc_iso: str, set_count: Optional[int], comment: Optional[str] = "", status: str = "pending") -> Dict[str, Any]:
    payload = {
        "user_id": user_id,
        "table_id": table_id,
        "name": name,
        "phone": phone,
        "party_size": party_size,
        "set_count": set_count,
        "starts_at": starts_utc_iso,
        "ends_at": ends_utc_iso,
        "status": status,
    }
    if comment:
        payload["comment"] = comment
    row = sb_post("reservations", payload)
    return row

def sb_get_user_by_chat(chat_id: int):
    rows = sb_get("users", {"select":"id,chat_id,first_name,last_name,username", "chat_id":f"eq.{chat_id}"})
    return rows[0] if rows else None

def sb_get_reservations_for_user_future(user_id: int):
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    rows = sb_get("reservations", {
        "select":"id,status,starts_at,ends_at,party_size,table_id",
        "user_id":f"eq.{user_id}",
        "ends_at":f"gte.{now_iso}",
        "order":"starts_at.asc"
    })
    return rows

def sb_get_table_names(table_ids: List[int]) -> Dict[int, str]:
    if not table_ids:
        return {}
    uniq = ",".join(str(i) for i in sorted(set(table_ids)))
    rows = sb_get("tables", {"select":"id,name", "id":f"in.({uniq})"})
    return {r["id"]: r["name"] for r in rows}

def sb_get_pending():
    return sb_get("reservations", {
        "select":"id,name,phone,party_size,starts_at,ends_at,table_id,user_id,status",
        "status":"eq.pending",
        "order":"starts_at.asc"
    })

def sb_get_reservation(res_id: int):
    rows = sb_get("reservations", {"select":"id,status,table_id,starts_at,party_size,user_id", "id":f"eq.{res_id}"})
    return rows[0] if rows else None

def sb_update_reservation(res_id: int, payload: Dict[str, Any]):
    rows = sb_patch("reservations", {"id":f"eq.{res_id}"}, payload)
    return rows[0] if rows else None

def sb_get_user(user_id: int):
    rows = sb_get("users", {"select":"id,chat_id,first_name,last_name,username", "id":f"eq.{user_id}"})
    return rows[0] if rows else None

def sb_get_confirmed_future():
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    return sb_get("reservations", {
        "select":"id,user_id,starts_at,status",
        "status":"eq.confirmed",
        "starts_at":f"gt.{now_iso}",
        "order":"starts_at.asc"
    })

def sb_insert_reservation(user_id: int, table_id: Optional[int], name: str, phone: str,
                          party_size: int, starts_utc_iso: str, ends_utc_iso: str,
                          set_count: Optional[int] = None,
                          comment: Optional[str] = "",
                          status: str = "pending") -> Dict[str, Any]:
    payload = {
        "user_id": user_id,
        "table_id": table_id,
        "name": name,
        "phone": phone,
        "party_size": party_size,
        "starts_at": starts_utc_iso,
        "ends_at": ends_utc_iso,
        "status": status,
    }
    if set_count is not None:
        payload["set_count"] = set_count
    if comment:
        payload["comment"] = comment

    row = sb_post("reservations", payload)
    return row


def sb_reserved_table_ids(starts_utc_iso: str, ends_utc_iso: str) -> set[int]:
    """
    Возвращает множество id столов, занятых в интервале [starts, ends) бронированиями
    со статусами pending/confirmed. Учитывает как table_id, так и joined_table_id.
    """
    rows = sb_get("reservations", {
        "select": "table_id,joined_table_id",
        "status": "in.(pending,confirmed)",
        "starts_at": f"lt.{ends_utc_iso}",
        "ends_at": f"gt.{starts_utc_iso}",
    })
    taken: set[int] = set()
    for r in rows:
        tid = r.get("table_id")
        jtid = r.get("joined_table_id")
        if tid is not None:
            taken.add(tid)
        if jtid is not None:
            taken.add(jtid)
    return taken


def _format_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

async def start(update: Update, context: ContextTypes.context):
    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        if chat.pinned_message:
            await context.bot.unpin_chat_message(
                chat_id=update.effective_chat.id,
                message_id=chat.pinned_message.message_id
            )
    except:
        pass

    faq_text = (
        "Всем мяу! "
        "Делюсь ответами на частые вопросы:"
        "1. Если забронирован стол по телефону, то не нужно бронировать его заново через бот. Брони, сделаные по номеру телефона-актуальны"
        "2. Отмена бронирования связана с загрузкой в определенные даты и время. Позже загруженные временные слоты не будут доступны, но пока что, мы отменяем Ваши брони вручную по этой причине "
        "3. С 28.08-01.09 все доступное время забронировано, поэтому эти даты неактивны. "
        "4. Мы не бронируем барную стойку, она только по приходу. "
        "Вроде все) "
        "Буду рада ответить на Ваши вопросы тут (https://t.me/ayilesa). Ну и всех приглашаем на гастрофест!)"
    )

    message = await update.message.chat.send_message(faq_text)

    await message.pin()
    
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

def parse_date(text: str):
    try:
        return datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None

def parse_time(text: str):
    for fmt in ("%H:%M", "%H.%M", "%H%M"):
        try:
            return datetime.strptime(text.strip(), fmt).time()
        except ValueError:
            continue
    return None

def _date_keyboard():
    choices = _date_choices()
    keyboard, row = [], []
    for i, d in enumerate(choices, start=1):
        row.append(d.strftime("%d.%m.%Y"))
        if i % 3 == 0:
            keyboard.append(row); row=[]
    if row: keyboard.append(row)
    return keyboard

async def book(update: Update, context: ContextTypes.context):
    await update.message.reply_text(
        "Выберите дату (YYYY-MM-DD) или введите вручную:",
        reply_markup=ReplyKeyboardMarkup(_date_keyboard(), one_time_keyboard=True, resize_keyboard=True),
    )
    return DATE

async def book_date(update: Update, context: ContextTypes.context):
    d = parse_date(update.message.text)
    today = datetime.now(LOCAL_TZ).date()
    min_date = today + timedelta(days=MIN_ADVANCE_DAYS)
    # print(d, [parse_date(el) for el in BLOCKED_DATES], BLOCKED_DATES)
    if d in [parse_date(el) for el in BLOCKED_DATES]:
        await update.message.reply_text("К сожаленю, бронирование в эту дату недоступно.")
        return DATE
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

    
    keyboard = [["2","3","4"],["5","6","7"],["8"]]
    await update.message.reply_text(
        "Сколько человек?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    
    return PARTY

async def book_time(update: Update, context: ContextTypes.context):
    t = parse_time(update.message.text)
    d = context.user_data.get("date")
    if not t or not d:
        await update.message.reply_text("Пожалуйста, укажите время в формате HH:MM, например 19:30.")
        return TIME_STATE
    allowed = _slots_for_date(d)
    if t not in allowed:
        open_t, last_t = WEEKLY_RULES[d.weekday()]
        await update.message.reply_text(
            f"В этот день принимаем с {open_t.strftime('%H:%M')} до {last_t.strftime('%H:%M')} (последняя запись). "
            "Выберите время из предложенных."
        )
        return TIME_STATE

    context.user_data["time"] = t

    starts_local = datetime.combine(d, t).replace(tzinfo=LOCAL_TZ)
    ends_local = starts_local + timedelta(minutes=RESERVATION_DURATION_MIN)
    starts_utc_iso = _utc_iso(starts_local)
    ends_utc_iso = _utc_iso(ends_local)

    table = sb_find_available_table(
        party_size=context.user_data["party"],
        starts_utc_iso=starts_utc_iso,
        ends_utc_iso=ends_utc_iso
    )
    table_id = table["id"] if table else None
    if table_id is None:
        await update.message.reply_text(
            f"К сожалению, на выбранное время невозможно найти стол. "
            f"Пожалуйста, выберите другое время:"
        )
        return TIME_STATE
        # return ConversationHandler.END

    keyboard = [["0","1","2","3","4"], ["5","6","7","8","9"]]
    await update.message.reply_text(
        "Сколько сетов хотите предзаказать? (0 — решим на месте)",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return SETS

async def book_party(update: Update, context: ContextTypes.context):
    try:
        party = int(update.message.text.strip())
        if party <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Укажите число гостей (целое положительное).")
        return PARTY
    context.user_data["party"] = party

    slots = _slots_for_date(context.user_data["date"])
    labels = [t.strftime("%H:%M") for t in slots]
    keyboard = [labels[i:i+4] for i in range(0, len(labels), 4)]
    await update.message.reply_text(
        "Во сколько? (например, 19:30)",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return TIME_STATE

async def book_sets(update: Update, context: ContextTypes.context):
    try:
        sets = int(update.message.text.strip())
        # легкая валидация, при желании скорректируй
        if sets < 0 or sets > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Укажите количество сетов числом (0 или больше).")
        return SETS

    context.user_data["set_count"] = sets
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

    # d = context.user_data["date"]
    # t = context.user_data["time"]
    # party = context.user_data["party"]
    # starts_local = datetime.combine(d, t).replace(tzinfo=LOCAL_TZ)
    # ends_local = starts_local + timedelta(minutes=RESERVATION_DURATION_MIN)

    # context.user_data["starts_local"] = starts_local
    # context.user_data["ends_local"] = ends_local

    # text = (
    #     f"Проверьте данные:\n"
    #     f"📅 Дата: {d.isoformat()}\n"
    #     f"⏰ Время: {t.strftime('%H:%M')}\n"
    #     f"👥 Гостей: {party}\n"
    #     f"🍣 Сеты: {context.user_data.get('set_count', 0)}\n"
    #     f"🧾 Имя: {context.user_data['name']}\n"
    #     f"📞 Телефон: {phone}\n\n"
    #     f"Подтверждаете?"
    # )
    # keyboard = InlineKeyboardMarkup([
    #     [InlineKeyboardButton("Отправить заявку", callback_data="confirm_yes"),
    #      InlineKeyboardButton("Изменить", callback_data="confirm_no")]
    # ])
    # await update.message.reply_text(text, reply_markup=keyboard)
    # return CONFIRM

    await update.message.reply_text(
        "Оставите комментарий к бронированию? (например, пожелание по столу, детское кресло и т. п.)\n"
        "Если нет — нажмите «Пропустить».",
        reply_markup=ReplyKeyboardMarkup([["Пропустить"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return COMMENT

async def book_comment(update: Update, context: ContextTypes.context):
    txt = (update.message.text or "").strip()
    if txt.lower() == "пропустить":
        txt = ""
    if len(txt) > 500:
        await update.message.reply_text("Комментарий слишком длинный (до 500 символов). Попробуйте короче.")
        return COMMENT

    context.user_data["comment"] = txt

    # сформируем локальные даты и превью перед подтверждением
    d = context.user_data["date"]
    t = context.user_data["time"]
    party = context.user_data["party"]
    name = context.user_data["name"]
    phone = context.user_data["phone"]

    starts_local = datetime.combine(d, t).replace(tzinfo=LOCAL_TZ)
    ends_local = starts_local + timedelta(minutes=RESERVATION_DURATION_MIN)
    context.user_data["starts_local"] = starts_local
    context.user_data["ends_local"] = ends_local

    text = (
        f"Проверьте данные:\n"
        f"📅 Дата: {d.isoformat()}\n"
        f"⏰ Время: {t.strftime('%H:%M')}\n"
        f"👥 Гостей: {party}\n"
        f"🍣 Сеты: {context.user_data.get('set_count', 0)}\n"
        f"💬 Комментарий: {txt or '—'}\n"
        f"🧾 Имя: {name}\n"
        f"📞 Телефон: {phone}\n\n"
        f"Подтверждаете?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Отправить заявку", callback_data="confirm_yes"),
         InlineKeyboardButton("Изменить", callback_data="confirm_no")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard)
    return CONFIRM

async def confirm_callback(update: Update, context: ContextTypes.context):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_no":
        await query.edit_message_text("Окей, начнем заново: /book")
        return ConversationHandler.END

    user = update.effective_user
    starts_local = context.user_data["starts_local"]
    ends_local = context.user_data["ends_local"]
    starts_utc_iso = _utc_iso(starts_local)
    ends_utc_iso = _utc_iso(ends_local)

    

    user_id = sb_ensure_user(
        chat_id=update.effective_chat.id,
        first_name=user.first_name, last_name=user.last_name, username=user.username or ""
    )

    today = datetime.now(LOCAL_TZ).date()
    # day_start_utc_iso, day_end_utc_iso = _utc_bounds_for_local_date(context.user_data["date"])
    day_start_utc_iso, day_end_utc_iso = _utc_bounds_for_local_date(today)
    user_id_for_limit = user_id if RES_LIMIT_SCOPE == "per_user" else None
    current_count = sb_count_reservations_in_day(day_start_utc_iso, day_end_utc_iso, user_id_for_limit)

    if current_count >= DAILY_RESERVATION_LIMIT:
        scope_text = "на пользователя" if RES_LIMIT_SCOPE == "per_user" else "на заведение"
        await query.edit_message_text(
            f"К сожалению, на выбранную дату достигнут лимит бронирований "
            f"({DAILY_RESERVATION_LIMIT} {scope_text} в день). Пожалуйста, выберите другую дату."
        )
        return ConversationHandler.END

    table = sb_find_available_table(
        party_size=context.user_data["party"],
        starts_utc_iso=starts_utc_iso,
        ends_utc_iso=ends_utc_iso
    )
    table_id = table["id"] if table else None

    should_auto_confirm = (
    context.user_data["party"] <= AUTO_CONFIRM_MAX_PARTY
    and table is not None)

    status_for_insert = "confirmed" if should_auto_confirm else "pending"

    row = sb_insert_reservation(
        user_id=user_id,
        table_id=table_id,
        name=context.user_data["name"],
        phone=context.user_data["phone"],
        party_size=context.user_data["party"],
        set_count=context.user_data.get("set_count"),
        starts_utc_iso=starts_utc_iso,
        ends_utc_iso=ends_utc_iso,
        comment=context.user_data.get("comment"),
        status=status_for_insert,
    )
    res_id = row["id"]

    if ADMIN_ALERT_BOT_TOKEN and (ADMIN_ALERT_CHAT_IDS or ADMIN_IDS):
        alert_bot = Bot(token=ADMIN_ALERT_BOT_TOKEN)
        dt_local_str = _format_local(_parse_utc_iso(starts_utc_iso))
        table_text = f"{table['name']} (до {table['capacity']})" if table else "—"
        alert = (
            f"🆕 Запрос на бронь #{res_id}\n"
            f"Дата/время: {dt_local_str}\n"
            f"Гостей: {context.user_data['party']}\n"
            f"Сеты: {context.user_data.get('set_count', 0)}\n"
            f"Имя: {context.user_data['name']}\n"
            f"Телефон: {context.user_data['phone']}\n"
            f"Предварительный стол: {table_text}\n"
            f"Комментарий: {context.user_data.get('comment','—')}\n"
            f"Статус: {status_for_insert}"
        )
        targets = ADMIN_ALERT_CHAT_IDS or list(ADMIN_IDS)
        for chat_id in targets:
            try:
                await alert_bot.send_message(chat_id=chat_id, text=alert)
            except Exception as e:
                logger.warning("Failed to send admin alert to %s: %s", chat_id, e)

    if should_auto_confirm:
    # отвечаем пользователю — сразу подтверждение
        await query.edit_message_text(
            f"✅ Ваша бронь №{res_id} подтверждена! Встречаемся "
            f"{_format_local(_parse_utc_iso(starts_utc_iso))} в {VENUE_NAME}. "
            f"Стол: {table['name']}."
        )
    else:
        if table:
            msg = (f"Пока бронь в ожидании подтверждения администратора.\n"
                f"Предварительно доступен стол {table['name']} (вмещает до {table['capacity']} гостей).")
        else:
            msg = (f"Пока бронь в ожидании подтверждения администратора.\n"
                f"Сейчас нет подходящих свободных столов на это время — "
                f"администратор свяжется для альтернативы.")
        msg += "\n\nЕсли появятся вопросы или захотите отменить, свяжитесь с заведением:\n" + _human_contacts()
        await query.edit_message_text(msg)
    return ConversationHandler.END

async def my_reservations(update: Update, context: ContextTypes.context):
    u = sb_get_user_by_chat(update.effective_chat.id)
    if not u:
        await update.message.reply_text("У вас пока нет бронирований. Нажмите /book чтобы создать.")
        return
    rows = sb_get_reservations_for_user_future(u["id"])
    if not rows:
        await update.message.reply_text("Активных бронирований нет. Нажмите /book чтобы создать.")
        return
    table_names = sb_get_table_names([r["table_id"] for r in rows if r.get("table_id")])
    lines = []
    for r in rows:
        starts = _format_local(_parse_utc_iso(r["starts_at"]))
        status_map = {"pending":"⏳ ожидание","confirmed":"✅ подтверждено","canceled":"❌ отменено","stopped":"⛔️ остановлено"}
        table_label = table_names.get(r.get("table_id"), "—")
        lines.append(f"№{r['id']} • {starts} • {status_map.get(r['status'], r['status'])} • Стол: {table_label} • Гостей: {r['party_size']}")
    lines.append("\nХотите отменить или есть вопросы?\n" + _human_contacts())
    await update.message.reply_markdown("\n".join(lines))

async def admin_pending(update: Update, context: ContextTypes.context):
    if not is_admin(update.effective_user.id):
        return
    rows = sb_get_pending()
    if not rows:
        await update.message.reply_text("Нет ожидающих заявок.")
        return
    table_names = sb_get_table_names([r["table_id"] for r in rows if r.get("table_id")])
    lines = ["Ожидают подтверждения:"]
    for r in rows:
        starts = _format_local(_parse_utc_iso(r["starts_at"]))
        table_label = table_names.get(r.get("table_id"), "—")
        lines.append(f"#{r['id']} • {starts} • {r['party_size']} чел • {r['name']} {r['phone']} • стол: {table_label}")
    await update.message.reply_text("\n".join(lines))

async def _schedule_or_send_reminder(app: Application, res_id: int, user_chat_id: int, starts_utc: datetime):
    when = starts_utc - timedelta(hours=REMINDER_HOURS_BEFORE)
    now = datetime.now(ZoneInfo("UTC"))
    job_name = f"reminder_{res_id}"
    for j in app.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()
    if when <= now and starts_utc > now:
        try:
            await app.bot.send_message(
                chat_id=user_chat_id,
                text=f"🔔 Напоминание: через {REMINDER_HOURS_BEFORE} ч у вас бронь №{res_id} в {VENUE_NAME}."
            )
        except Exception as e:
            logger.warning("Failed to send immediate reminder: %s", e)
        return
    if when > now:
        app.job_queue.run_once(
            reminder_job,
            when=when,
            name=job_name,
            data={"res_id": res_id, "user_chat_id": user_chat_id},
        )

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    res_id = data["res_id"]
    user_chat_id = data["user_chat_id"]
    r = sb_get_reservation(res_id)
    if not r or r.get("status") != "confirmed":
        return
    starts_utc = _parse_utc_iso(r["starts_at"])
    now = datetime.now(ZoneInfo("UTC"))
    if starts_utc <= now:
        return
    try:
        await context.bot.send_message(
            chat_id=user_chat_id,
            text=f"🔔 Напоминание: через {REMINDER_HOURS_BEFORE} ч у вас бронь №{res_id} в {VENUE_NAME}."
        )
    except Exception as e:
        logger.warning("Failed to send reminder: %s", e)

async def admin_confirm(update: Update, context: ContextTypes.context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /confirm <reservation_id>")
        return
    res_id = int(context.args[0])
    row = sb_get_reservation(res_id)
    if not row:
        await update.message.reply_text("Бронь не найдена.")
        return
    if row["status"] == "confirmed":
        await update.message.reply_text("Бронь уже подтверждена.")
        return

    starts_utc = _parse_utc_iso(row["starts_at"])
    ends_utc = starts_utc + timedelta(minutes=RESERVATION_DURATION_MIN)

    assigned_table_name = None
    if row.get("table_id") is None:
        table = sb_find_available_table(row["party_size"], starts_utc.isoformat(timespec="seconds"), ends_utc.isoformat(timespec="seconds"))
        if not table:
            await update.message.reply_text("Подходящих столов нет для подтверждения.")
            return
        upd = sb_update_reservation(res_id, {"table_id": table["id"], "status":"confirmed"})
        assigned_table_name = table["name"]
    else:
        upd = sb_update_reservation(res_id, {"status":"confirmed"})

    u = sb_get_user(row["user_id"])
    if not u:
        await update.message.reply_text("Подтвердил, но не нашёл пользователя для уведомления.")
        return

    await _schedule_or_send_reminder(context.application, res_id, u["chat_id"], starts_utc)

    await update.message.reply_text(f"Бронь #{res_id} подтверждена, стол {assigned_table_name or '—'}. Уведомляю пользователя.")
    try:
        await context.bot.send_message(
            chat_id=u["chat_id"],
            text=f"✅ Ваша бронь №{res_id} подтверждена! До встречи в {VENUE_NAME}. "
                 f"Встречаемся {starts_utc.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}."
        )
    except Exception as e:
        logger.exception("Failed to notify user: %s", e)

async def admin_cancel(update: Update, context: ContextTypes.context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /cancel_res <reservation_id>")
        return
    res_id = int(context.args[0])
    row = sb_get_reservation(res_id)
    if not row:
        await update.message.reply_text("Бронь не найдена.")
        return
    if row["status"] == "canceled":
        await update.message.reply_text("Бронь уже отменена.")
        return
    upd = sb_update_reservation(res_id, {"status":"canceled"})
    u = sb_get_user(row["user_id"])
    await update.message.reply_text(f"Бронь #{res_id} отменена. Оповещаю пользователя.")
    if u:
        try:
            await context.bot.send_message(
                chat_id=u["chat_id"],
                text=f"❌ К сожалению, бронь №{res_id} была отменена. Если есть вопросы, свяжитесь с нами:\n{_human_contacts()}"
            )
        except Exception as e:
            logger.exception("Failed to notify user: %s", e)

async def cancel(update: Update, context: ContextTypes.context):
    await update.message.reply_text("Окей, бронирование отменено. Нажмите /book чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def on_startup(app: Application):
    try:
        rows = sb_get_confirmed_future()
        for r in rows:
            res_id = r["id"]
            starts_utc = _parse_utc_iso(r["starts_at"])
            u = sb_get_user(r["user_id"])
            if not u:
                continue
            await _schedule_or_send_reminder(app, res_id, u["chat_id"], starts_utc)
        logger.info("Startup reminders scheduled: %d", len(rows))
    except Exception as e:
        logger.exception("Failed to schedule reminders on startup: %s", e)

def build_app():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_date)],
            TIME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_time)],
            PARTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_party)],
            SETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_sets)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_name)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_comment)],
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
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
