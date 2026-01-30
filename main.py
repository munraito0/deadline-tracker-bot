import calendar as cal_mod
import logging
import sqlite3
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from uuid import uuid4

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "8525186985:AAEAMZI158ay3EVBoi98N84Fv5hy2OlpgOI"

DB_PATH = Path(__file__).resolve().parent / "deadlines.db"


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS deadlines (
                id      TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name    TEXT NOT NULL,
                date    TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id     INTEGER PRIMARY KEY,
                remind_hour INTEGER NOT NULL DEFAULT 9,
                remind_min  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cols = [r[1] for r in con.execute("PRAGMA table_info(deadlines)").fetchall()]
        if "repeat" not in cols:
            con.execute("ALTER TABLE deadlines ADD COLUMN repeat TEXT")


def db_add(user_id: int, dl_id: str, name: str, date: str, repeat: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO deadlines (id, user_id, name, date, repeat) VALUES (?, ?, ?, ?, ?)",
            (dl_id, user_id, name, date, repeat),
        )


def db_get(user_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, name, date, repeat FROM deadlines WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [{"id": r[0], "name": r[1], "date": r[2], "repeat": r[3]} for r in rows]


def db_delete(dl_id: str, user_id: int) -> str | None:
    with _conn() as con:
        row = con.execute(
            "SELECT name FROM deadlines WHERE id = ? AND user_id = ?",
            (dl_id, user_id),
        ).fetchone()
        if row is None:
            return None
        con.execute("DELETE FROM deadlines WHERE id = ? AND user_id = ?", (dl_id, user_id))
        return row[0]


def db_update_date(dl_id: str, user_id: int, new_date: str) -> str | None:
    with _conn() as con:
        row = con.execute(
            "SELECT name FROM deadlines WHERE id = ? AND user_id = ?",
            (dl_id, user_id),
        ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE deadlines SET date = ? WHERE id = ? AND user_id = ?",
            (new_date, dl_id, user_id),
        )
        return row[0]


def db_update_name(dl_id: str, user_id: int, new_name: str) -> str | None:
    with _conn() as con:
        row = con.execute(
            "SELECT name FROM deadlines WHERE id = ? AND user_id = ?",
            (dl_id, user_id),
        ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE deadlines SET name = ? WHERE id = ? AND user_id = ?",
            (new_name, dl_id, user_id),
        )
        return row[0]


def db_all_deadlines() -> list[tuple]:
    with _conn() as con:
        return con.execute("SELECT id, user_id, name, date, repeat FROM deadlines").fetchall()


def _next_month(d):
    month = d.month + 1
    year = d.year
    if month > 12:
        month = 1
        year += 1
    day = min(d.day, cal_mod.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def db_advance_recurring(user_id: int) -> None:
    today = datetime.now().date()
    with _conn() as con:
        rows = con.execute(
            "SELECT id, date, repeat FROM deadlines WHERE user_id = ? AND repeat IS NOT NULL",
            (user_id,),
        ).fetchall()
        for dl_id, date_str, repeat in rows:
            try:
                d = datetime.strptime(date_str, "%d.%m.%Y").date()
            except ValueError:
                continue
            if d >= today:
                continue
            while d < today:
                if repeat == "weekly":
                    d += timedelta(days=7)
                elif repeat == "monthly":
                    d = _next_month(d)
                else:
                    break
            con.execute(
                "UPDATE deadlines SET date = ? WHERE id = ?",
                (d.strftime("%d.%m.%Y"), dl_id),
            )


def db_get_remind_time(user_id: int) -> tuple[int, int]:
    with _conn() as con:
        row = con.execute(
            "SELECT remind_hour, remind_min FROM settings WHERE user_id = ?", (user_id,),
        ).fetchone()
    return (row[0], row[1]) if row else (9, 0)


def db_set_remind_time(user_id: int, hour: int, minute: int) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT INTO settings (user_id, remind_hour, remind_min) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET remind_hour = ?, remind_min = ?
            """,
            (user_id, hour, minute, hour, minute),
        )


def db_all_remind_settings() -> list[tuple[int, int, int]]:
    with _conn() as con:
        return con.execute("SELECT user_id, remind_hour, remind_min FROM settings").fetchall()


ADD_NAME, ADD_DATE, ADD_REPEAT = range(3)
EDIT_DATE = 10
EDIT_NAME = 11
SET_TIME = 20

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

MONTHS_RU_NOM = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

REPEAT_LABELS = {"weekly": "еженед.", "monthly": "ежемес."}

BTN_ADD = "Добавить дедлайн"
BTN_LIST = "Мои дедлайны"
BTN_SETTINGS = "Время напоминания"
BTN_HELP = "Помощь"

DAYS_HEADER = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def calculate_days(date_str: str):
    try:
        deadline = datetime.strptime(date_str, "%d.%m.%Y").date()
        today = datetime.now().date()
        return (deadline - today).days, deadline
    except ValueError:
        return None, None


def format_date(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%d.%m.%Y")
        return f"{d.day} {MONTHS_RU[d.month]}"
    except Exception:
        return date_str


def _persistent_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ADD), KeyboardButton(BTN_LIST)],
            [KeyboardButton(BTN_SETTINGS), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Добавить дедлайн", callback_data="menu_add")],
        [InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")],
    ])


def _deadline_line(idx: int, dl: dict) -> str:
    days, _ = calculate_days(dl["date"])
    fd = format_date(dl["date"])
    if days is None:
        status = ""
    elif days < 0:
        status = f"просрочен на {abs(days)} дн."
    elif days == 0:
        status = "СЕГОДНЯ!"
    elif days == 1:
        status = "ЗАВТРА!"
    else:
        status = f"через {days} дн."
    repeat_tag = ""
    if dl.get("repeat") in REPEAT_LABELS:
        repeat_tag = f" [{REPEAT_LABELS[dl['repeat']]}]"
    return f"{idx}. {dl['name']}{repeat_tag} — {fd} ({status})"


def _job_name(user_id: int) -> str:
    return f"remind_{user_id}"


def _build_calendar(year: int, month: int, cancel_cb: str = "cancel_add") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    prev_m, prev_y = month - 1, year
    if prev_m < 1:
        prev_m, prev_y = 12, year - 1
    next_m, next_y = month + 1, year
    if next_m > 12:
        next_m, next_y = 1, year + 1

    rows.append([
        InlineKeyboardButton("◀", callback_data=f"cal_p_{prev_m:02d}.{prev_y}"),
        InlineKeyboardButton(f"{MONTHS_RU_NOM[month]} {year}", callback_data="cal_ignore"),
        InlineKeyboardButton("▶", callback_data=f"cal_n_{next_m:02d}.{next_y}"),
    ])

    rows.append([
        InlineKeyboardButton(d, callback_data="cal_ignore") for d in DAYS_HEADER
    ])

    for week in cal_mod.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
            else:
                date_str = f"{day:02d}.{month:02d}.{year}"
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal_d_{date_str}"))
        rows.append(row)

    rows.append([InlineKeyboardButton("Отмена", callback_data=cancel_cb)])

    return InlineKeyboardMarkup(rows)


def _parse_cal_nav(data: str) -> tuple[int, int]:
    raw = data.split("_", 2)[2]
    month_s, year_s = raw.split(".")
    return int(month_s), int(year_s)


async def _cal_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


def schedule_user_reminder(application, user_id: int, hour: int, minute: int) -> None:
    jq = application.job_queue
    for job in jq.get_jobs_by_name(_job_name(user_id)):
        job.schedule_removal()
    jq.run_daily(
        _send_user_reminder,
        time=dt_time(hour=hour, minute=minute, second=0),
        name=_job_name(user_id),
        data=user_id,
    )


def schedule_all_reminders(application) -> None:
    for user_id, hour, minute in db_all_remind_settings():
        schedule_user_reminder(application, user_id, hour, minute)


async def _send_user_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data

    db_advance_recurring(user_id)

    user_dls = db_get(user_id)
    if not user_dls:
        return

    overdue, today_list, tomorrow_list, week_list = [], [], [], []

    for dl in user_dls:
        days, _ = calculate_days(dl["date"])
        if days is None:
            continue
        fd = format_date(dl["date"])
        repeat_tag = ""
        if dl.get("repeat") in REPEAT_LABELS:
            repeat_tag = f" [{REPEAT_LABELS[dl['repeat']]}]"
        line = f"- {dl['name']}{repeat_tag} ({fd})"

        if days < 0:
            overdue.append(f"{line} — просрочен на {abs(days)} дн.")
        elif days == 0:
            today_list.append(line)
        elif days == 1:
            tomorrow_list.append(line)
        elif days <= 7:
            week_list.append(f"{line} — через {days} дн.")

    if not overdue and not today_list and not tomorrow_list and not week_list:
        return

    parts = []
    if overdue:
        parts.append("ПРОСРОЧЕНО:\n" + "\n".join(overdue))
    if today_list:
        parts.append("СЕГОДНЯ:\n" + "\n".join(today_list))
    if tomorrow_list:
        parts.append("ЗАВТРА:\n" + "\n".join(tomorrow_list))
    if week_list:
        parts.append("НА ЭТОЙ НЕДЕЛЕ:\n" + "\n".join(week_list))

    text = "Напоминание о дедлайнах:\n\n" + "\n\n".join(parts)
    keyboard = [[InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")]]
    try:
        await context.bot.send_message(
            chat_id=user_id, text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.warning("Не удалось отправить напоминание пользователю %s", user_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    hour, minute = db_get_remind_time(user_id)
    text = (
        "Deadline Tracker Bot\n\n"
        "Привет! Я помогу тебе не забывать о важных дедлайнах.\n\n"
        f"Напоминания: ежедневно в {hour:02d}:{minute:02d}\n"
        "Формат даты: ДД.ММ.ГГГГ или выбор через календарь."
    )
    await update.message.reply_text(text, reply_markup=_persistent_kb())


async def menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "Deadline Tracker Bot\n\nВыбери действие:",
        reply_markup=_main_menu_markup(),
    )


HELP_TEXT = (
    "Deadline Tracker Bot — v2.0\n\n"
    "Бот для отслеживания дедлайнов с напоминаниями.\n\n"
    "Возможности:\n"
    "- Добавление дедлайнов с датой через календарь\n"
    "- Повторяющиеся дедлайны (еженедельно, ежемесячно)\n"
    "- Редактирование названия и даты\n"
    "- Ежедневные напоминания о ближайших дедлайнах (7 дней)\n"
    "- Настройка времени напоминания\n\n"
    "Кнопки внизу экрана:\n"
    "- Добавить дедлайн — создать новый дедлайн\n"
    "- Мои дедлайны — список всех дедлайнов\n"
    "- Время напоминания — изменить время ежедневного напоминания\n"
    "- Помощь — эта справка\n\n"
    "Команды:\n"
    "/start — главное меню\n"
    "/add — добавить дедлайн\n"
    "/list — мои дедлайны\n"
    "/help — справка\n"
    "/cancel — отмена текущего действия\n\n"
    "Формат даты: ДД.ММ.ГГГГ или выбор через календарь."
)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=_persistent_kb())


async def list_deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id

    db_advance_recurring(user_id)
    user_dls = db_get(user_id)

    if not user_dls:
        keyboard = [
            [InlineKeyboardButton("Добавить дедлайн", callback_data="menu_add")],
            [InlineKeyboardButton("В меню", callback_data="menu_start")],
        ]
        text = "У тебя пока нет дедлайнов."
        if query:
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    sorted_dls = sorted(
        user_dls,
        key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"),
    )

    lines = [_deadline_line(i + 1, dl) for i, dl in enumerate(sorted_dls)]
    text = "Твои дедлайны:\n\n" + "\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    for i, dl in enumerate(sorted_dls):
        buttons.append([
            InlineKeyboardButton(f"{i+1} — Название", callback_data=f"editname_{dl['id']}"),
            InlineKeyboardButton(f"{i+1} — Дата", callback_data=f"editdate_{dl['id']}"),
            InlineKeyboardButton(f"{i+1} — Удалить", callback_data=f"delete_{dl['id']}"),
        ])
    buttons.append([InlineKeyboardButton("Добавить дедлайн", callback_data="menu_add")])
    buttons.append([InlineKeyboardButton("В меню", callback_data="menu_start")])

    if query:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Отмена", callback_data="cancel_add")]]
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "Введи название дедлайна:", reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            "Введи название дедлайна:", reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["deadline_name"] = update.message.text
    now = datetime.now()
    markup = _build_calendar(now.year, now.month, cancel_cb="cancel_add")
    await update.message.reply_text(
        "Выбери дату дедлайна или введи вручную (ДД.ММ.ГГГГ):",
        reply_markup=markup,
    )
    return ADD_DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()

    days_left, _ = calculate_days(date_str)
    if days_left is None:
        await update.message.reply_text(
            "Неверный формат даты!\nФормат: ДД.ММ.ГГГГ\nПример: 20.01.2026\n\nПопробуй ещё раз:"
        )
        return ADD_DATE

    context.user_data["deadline_date"] = date_str

    keyboard = [
        [InlineKeyboardButton("Нет", callback_data="repeat_none")],
        [InlineKeyboardButton("Еженедельно", callback_data="repeat_weekly")],
        [InlineKeyboardButton("Ежемесячно", callback_data="repeat_monthly")],
    ]
    await update.message.reply_text(
        "Повторяющийся дедлайн?", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_REPEAT


async def add_date_cal_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data.removeprefix("cal_d_")

    days_left, _ = calculate_days(date_str)
    if days_left is None:
        await query.message.edit_text("Ошибка даты. Попробуй ещё раз.")
        return ADD_DATE

    context.user_data["deadline_date"] = date_str

    keyboard = [
        [InlineKeyboardButton("Нет", callback_data="repeat_none")],
        [InlineKeyboardButton("Еженедельно", callback_data="repeat_weekly")],
        [InlineKeyboardButton("Ежемесячно", callback_data="repeat_monthly")],
    ]
    await query.message.edit_text(
        "Повторяющийся дедлайн?", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_REPEAT


async def add_date_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    month, year = _parse_cal_nav(query.data)
    markup = _build_calendar(year, month, cancel_cb="cancel_add")
    await query.message.edit_reply_markup(reply_markup=markup)
    return ADD_DATE


async def add_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    repeat_map = {
        "repeat_none": None,
        "repeat_weekly": "weekly",
        "repeat_monthly": "monthly",
    }
    repeat = repeat_map.get(query.data)

    user_id = query.from_user.id
    name = context.user_data["deadline_name"]
    date_str = context.user_data["deadline_date"]
    dl_id = uuid4().hex[:8]

    db_add(user_id, dl_id, name, date_str, repeat)

    days_left, _ = calculate_days(date_str)
    fd = format_date(date_str)
    if days_left < 0:
        status = f"просрочен на {abs(days_left)} дн."
    elif days_left == 0:
        status = "СЕГОДНЯ!"
    elif days_left == 1:
        status = "ЗАВТРА!"
    else:
        status = f"через {days_left} дн."

    repeat_info = ""
    if repeat in REPEAT_LABELS:
        repeat_info = f" [{REPEAT_LABELS[repeat]}]"

    text = f"Дедлайн добавлен!\n\n{name}{repeat_info} — {fd} ({status})"

    keyboard = [
        [InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")],
        [InlineKeyboardButton("Добавить ещё", callback_data="menu_add")],
        [InlineKeyboardButton("В меню", callback_data="menu_start")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def add_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await query.message.edit_text(
        "Добавление дедлайна отменено.", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def add_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await update.message.reply_text(
        "Добавление дедлайна отменено.", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def delete_deadline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    dl_id = query.data.removeprefix("delete_")

    deleted_name = db_delete(dl_id, user_id)
    if deleted_name:
        keyboard = [
            [InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")],
            [InlineKeyboardButton("В меню", callback_data="menu_start")],
        ]
        await query.message.edit_text(
            f"Дедлайн \"{deleted_name}\" удалён.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await query.message.edit_text("Дедлайн не найден.")


async def editdate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dl_id = query.data.removeprefix("editdate_")
    user_id = query.from_user.id

    user_dls = db_get(user_id)
    target = next((dl for dl in user_dls if dl["id"] == dl_id), None)
    if target is None:
        await query.message.edit_text("Дедлайн не найден.")
        return ConversationHandler.END

    context.user_data["edit_dl_id"] = dl_id

    now = datetime.now()
    markup = _build_calendar(now.year, now.month, cancel_cb="cancel_edit")
    await query.message.edit_text(
        f"Изменение даты для: {target['name']}\n"
        f"Текущая дата: {format_date(target['date'])}\n\n"
        "Выбери новую дату или введи вручную (ДД.ММ.ГГГГ):",
        reply_markup=markup,
    )
    return EDIT_DATE


async def editdate_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    date_str = update.message.text.strip()
    dl_id = context.user_data.get("edit_dl_id")

    days_left, _ = calculate_days(date_str)
    if days_left is None:
        await update.message.reply_text(
            "Неверный формат даты!\nФормат: ДД.ММ.ГГГГ\nПример: 20.01.2026\n\nПопробуй ещё раз:"
        )
        return EDIT_DATE

    name = db_update_date(dl_id, user_id, date_str)
    if name is None:
        await update.message.reply_text("Дедлайн не найден.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")],
        [InlineKeyboardButton("В меню", callback_data="menu_start")],
    ]
    await update.message.reply_text(
        f"Дата обновлена!\n\n{name} — {format_date(date_str)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def editdate_cal_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    dl_id = context.user_data.get("edit_dl_id")
    date_str = query.data.removeprefix("cal_d_")

    days_left, _ = calculate_days(date_str)
    if days_left is None:
        await query.message.edit_text("Ошибка даты.")
        return ConversationHandler.END

    name = db_update_date(dl_id, user_id, date_str)
    if name is None:
        await query.message.edit_text("Дедлайн не найден.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")],
        [InlineKeyboardButton("В меню", callback_data="menu_start")],
    ]
    await query.message.edit_text(
        f"Дата обновлена!\n\n{name} — {format_date(date_str)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def editdate_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    month, year = _parse_cal_nav(query.data)
    markup = _build_calendar(year, month, cancel_cb="cancel_edit")
    await query.message.edit_reply_markup(reply_markup=markup)
    return EDIT_DATE


async def editname_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dl_id = query.data.removeprefix("editname_")
    user_id = query.from_user.id

    user_dls = db_get(user_id)
    target = next((dl for dl in user_dls if dl["id"] == dl_id), None)
    if target is None:
        await query.message.edit_text("Дедлайн не найден.")
        return ConversationHandler.END

    context.user_data["edit_dl_id"] = dl_id
    keyboard = [[InlineKeyboardButton("Отмена", callback_data="cancel_edit")]]
    await query.message.edit_text(
        f"Текущее название: {target['name']}\n\nВведи новое название:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return EDIT_NAME


async def editname_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    new_name = update.message.text.strip()
    dl_id = context.user_data.get("edit_dl_id")

    old_name = db_update_name(dl_id, user_id, new_name)
    if old_name is None:
        await update.message.reply_text("Дедлайн не найден.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("Мои дедлайны", callback_data="menu_list")],
        [InlineKeyboardButton("В меню", callback_data="menu_start")],
    ]
    await update.message.reply_text(
        f"Название обновлено!\n\n\"{old_name}\" -> \"{new_name}\"",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def edit_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await query.message.edit_text(
        "Изменение отменено.", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def edit_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await update.message.reply_text(
        "Изменение отменено.", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def set_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    hour, minute = db_get_remind_time(user_id)
    keyboard = [[InlineKeyboardButton("Отмена", callback_data="cancel_settime")]]
    await update.message.reply_text(
        f"Текущее время напоминания: {hour:02d}:{minute:02d}\n\n"
        "Введи новое время в формате ЧЧ:ММ\nНапример: 09:00 или 21:30",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SET_TIME


async def set_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    try:
        parts = text.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Неверный формат!\nВведи время как ЧЧ:ММ (например: 09:00)\n\nПопробуй ещё раз:"
        )
        return SET_TIME

    db_set_remind_time(user_id, hour, minute)
    schedule_user_reminder(context.application, user_id, hour, minute)

    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await update.message.reply_text(
        f"Время напоминания установлено: {hour:02d}:{minute:02d}\n\n"
        "Каждый день в это время я напомню о ближайших дедлайнах.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def set_time_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await query.message.edit_text(
        "Настройка времени отменена.", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def set_time_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("В меню", callback_data="menu_start")]]
    await update.message.reply_text(
        "Настройка времени отменена.", reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


def main():
    init_db()

    application = Application.builder().token(TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CallbackQueryHandler(add_start, pattern="^menu_add$"),
            MessageHandler(filters.Text([BTN_ADD]), add_start),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_DATE: [
                CallbackQueryHandler(add_date_cal_pick, pattern=r"^cal_d_"),
                CallbackQueryHandler(add_date_cal_nav, pattern=r"^cal_[pn]_"),
                CallbackQueryHandler(_cal_ignore, pattern=r"^cal_ignore$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_date),
            ],
            ADD_REPEAT: [CallbackQueryHandler(add_repeat, pattern=r"^repeat_")],
        },
        fallbacks=[
            CommandHandler("cancel", add_cancel_command),
            CallbackQueryHandler(add_cancel_callback, pattern="^cancel_add$"),
        ],
        per_message=False,
    )

    editdate_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(editdate_start, pattern=r"^editdate_[a-f0-9]{8}$"),
        ],
        states={
            EDIT_DATE: [
                CallbackQueryHandler(editdate_cal_pick, pattern=r"^cal_d_"),
                CallbackQueryHandler(editdate_cal_nav, pattern=r"^cal_[pn]_"),
                CallbackQueryHandler(_cal_ignore, pattern=r"^cal_ignore$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editdate_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", edit_cancel_command),
            CallbackQueryHandler(edit_cancel_callback, pattern="^cancel_edit$"),
        ],
        per_message=False,
    )

    editname_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(editname_start, pattern=r"^editname_[a-f0-9]{8}$"),
        ],
        states={
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, editname_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", edit_cancel_command),
            CallbackQueryHandler(edit_cancel_callback, pattern="^cancel_edit$"),
        ],
        per_message=False,
    )

    time_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text([BTN_SETTINGS]), set_time_start),
        ],
        states={
            SET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_receive)],
        },
        fallbacks=[
            CommandHandler("cancel", set_time_cancel_command),
            CallbackQueryHandler(set_time_cancel_callback, pattern="^cancel_settime$"),
        ],
        per_message=False,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    application.add_handler(add_conv)
    application.add_handler(editdate_conv)
    application.add_handler(editname_conv)
    application.add_handler(time_conv)
    application.add_handler(CommandHandler("list", list_deadlines))
    application.add_handler(MessageHandler(filters.Text([BTN_LIST]), list_deadlines))
    application.add_handler(CallbackQueryHandler(list_deadlines, pattern="^menu_list$"))
    application.add_handler(CallbackQueryHandler(menu_start, pattern="^menu_start$"))
    application.add_handler(CallbackQueryHandler(delete_deadline_callback, pattern=r"^delete_[a-f0-9]{8}$"))

    schedule_all_reminders(application)

    print("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
