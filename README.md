
# Telegram Booking Bot (Python, Supabase)

Бот для бронирования столов в Telegram с хранением данных в **Supabase** (PostgreSQL через REST). Поддерживает:
- Пошаговую бронь `/book` (дата/время/гости/имя/телефон)
- Проверку доступных столов по вместимости и пересечениям
- Заявка со статусом `pending` → подтверждение/отмена админом в боте
- **Напоминание пользователю за 2 часа до начала** (JobQueue)
- **Оповещение админов** о новой заявке через **отдельного бота**
- Расписание приёма брони (Mon–Sun) и запрет на брони в день обращения

## Установка
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install python-telegram-bot==21.4 python-dotenv requests
```

## Настройка
1) Скопируйте `.env.example` → `.env` и заполните:
```
TELEGRAM_BOT_TOKEN=...
ADMIN_IDS=11111111,22222222

# Админ-оповещения через другой бот (опц.)
ADMIN_ALERT_BOT_TOKEN=...
ADMIN_ALERT_CHAT_IDS=11111111,22222222

# Контакты заведения
VENUE_NAME=...
VENUE_PHONE=...
VENUE_ADDRESS=...
VENUE_HOURS=...

# Правила брони
LOCAL_TZ=Europe/Berlin
RESERVATION_DURATION_MIN=120
TIME_SLOT_STEP_MIN=30
MIN_ADVANCE_DAYS=1
ONLY_TOMORROW=false
REMINDER_HOURS_BEFORE=2

# Supabase
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_API_KEY=YOUR_ANON_OR_SERVICE_KEY
```
2) Создайте таблицы в Supabase (SQL):
```sql
CREATE TABLE IF NOT EXISTS tables (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0)
);
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    chat_id INTEGER NOT NULL UNIQUE,
    first_name TEXT,
    last_name TEXT,
    username TEXT
);
CREATE TABLE IF NOT EXISTS reservations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    table_id INTEGER REFERENCES tables(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    starts_at TIMESTAMP NOT NULL,
    ends_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','canceled', 'stopped')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_res_start_end ON reservations(starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_res_status ON reservations(status);
```
3) Засидьте столы (пример):
```sql
INSERT INTO tables (name, capacity) VALUES
('T1',2),('T2',2),('T3',4),('T4',4),('T5',6),('VIP1',8);
```

## Запуск
```bash
python bot.py
```

## Как это работает
- Все операции идут через REST Supabase `GET/POST/PATCH /rest/v1/<table>` с заголовками `apikey` и `Authorization`.
- Поиск занятых столов: `reservations?status=in.(pending,confirmed)&table_id=not.is.null&starts_at=lt.<end>&ends_at=gt.<start>`;
  затем подбираем минимально подходящий стол из `tables?capacity=gte.<party>&order=capacity.asc`.
- Напоминания: при подтверждении брони планируется job на `starts_at - REMINDER_HOURS_BEFORE`. При старте бота все будущие `confirmed` проверкиваются и планируются заново.
- Админ-оповещения: при создании заявки (status=`pending`) отправляем сообщение в `ADMIN_ALERT_CHAT_IDS` через `ADMIN_ALERT_BOT_TOKEN`.
  Основные админ-команды остаются в **основном** боте.

## Команды
- Пользователь: `/book`, `/my`, `/contacts`, `/help`
- Админ (в основном боте): `/pending`, `/confirm <id>`, `/cancel_res <id>`

## Время
Колонки `starts_at`/`ends_at` — тип `TIMESTAMP` (без TZ). Сохраняем время в **UTC** (`YYYY-MM-DDTHH:MM:SS`) и трактуем как UTC при чтении. Отображение пользователю — в `LOCAL_TZ`.

## Идеи развития
- Добавить флаг `reminder_sent` (миграция) для устойчивости к внешним повторам.
- Ограничить, чтобы `ends_at` не превышало время закрытия.
- Веб-админка (FastAPI) и RLS в Supabase.
