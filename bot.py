# -*- coding: utf-8 -*-
"""
VIP Taxi Bot — версия с:
- заказом через /order и кнопку «🔔 Заказ»
- Google Sheets для заказов
- Google Sheets для водителей (фото, класс, номер авто)
- бронированием заказов водителями через группу
- AI-помощником диспетчера (/ai)
- нормализацией даты/времени через AI (ai_normalize_time)
- командой /carphoto для клиента (фото назначенного авто)
"""

import os
import json
import logging
from uuid import uuid4
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("vip_taxi_bot")

# ---------- НАСТРОЙКИ ----------
BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ID группы водителей (например -100...)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # для AI-функций (опционально)
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")

assert BOT_TOKEN, "BOT_TOKEN is required"
assert GOOGLE_CREDS_JSON, "GOOGLE_APPLICATION_CREDENTIALS_JSON is required"

# Тарифы (примерная цена/час, будем писать как «от … ₽/ч»)
PRICES_PER_HOUR = {
    "Maybach W223": 7000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
    "Minivan": 3000,
}

# Память бота для заказов
# order_id -> dict(order_data)
ORDERS_CACHE: dict[str, dict] = {}

# Память активного заказа по клиенту: user_id -> order_id
CLIENT_ACTIVE_ORDER: dict[int, str] = {}

# ---------- GOOGLE SHEETS ----------
from google.oauth2.service_account import Credentials
import gspread

credentials_info = json.loads(GOOGLE_CREDS_JSON)
credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
gc = gspread.authorize(credentials)

# Таблица заказов
ORDERS_SHEET = gc.open("orders").sheet1  # первая вкладка
# Таблица водителей (создай файл "drivers", первая строка: driver_id,driver_name,car_class,plate,car_photo_file_id,rating)
try:
    DRIVERS_SHEET = gc.open("drivers").sheet1
except Exception:
    DRIVERS_SHEET = None
    log.warning("Таблица drivers не найдена — /carphoto и рейтинг работать не будут")

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ GOOGLE SHEETS ----------

def save_order_to_sheet(order: dict) -> None:
    """
    Структура строк в orders:
    A: order_id
    B: user_id
    C: username
    D: pickup
    E: destination
    F: car_class
    G: time_text (то, что ввёл клиент, или нормализованное)
    H: hours
    I: contact
    J: approx_price_text
    K: created_at
    L: status        (new / assigned / arrived / finished)
    M: driver_id
    N: driver_name
    O: car_plate
    """
    try:
        ORDERS_SHEET.append_row(
            [
                order.get("order_id"),
                str(order.get("user_id")),
                order.get("username"),
                order.get("pickup"),
                order.get("destination"),
                order.get("car_class"),
                order.get("time"),
                str(order.get("hours")),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order.get("status", "new"),
                str(order.get("driver_id") or ""),
                order.get("driver_name") or "",
                order.get("car_plate") or "",
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ %s записан в Google Sheets", order.get("order_id"))
    except Exception as e:
        log.error("Ошибка Google Sheets при записи заказа: %s", e)


def find_order_row(order_id: str):
    """Найти номер строки в orders по order_id (колонка A)."""
    try:
        values = ORDERS_SHEET.col_values(1)
        for idx, val in enumerate(values, start=1):
            if val == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа в таблице: %s", e)
    return None


def update_order_status_in_sheet(order_id: str, **fields) -> None:
    """
    Обновить статус/водителя/номер авто и т.п.
    Поддерживаем поля: status, driver_id, driver_name, car_plate
    """
    row = find_order_row(order_id)
    if not row:
        return
    updates = {}
    if "status" in fields:
        updates[12] = fields["status"]         # L
    if "driver_id" in fields:
        updates[13] = str(fields["driver_id"] or "")  # M
    if "driver_name" in fields:
        updates[14] = fields["driver_name"] or ""     # N
    if "car_plate" in fields:
        updates[15] = fields["car_plate"] or ""       # O

    try:
        for col, val in updates.items():
            ORDERS_SHEET.update_cell(row, col, val)
    except Exception as e:
        log.error("Ошибка обновления заказа в таблице: %s", e)


def get_driver_row(driver_id: int):
    """Поиск строки водителя по driver_id в таблице drivers."""
    if not DRIVERS_SHEET:
        return None, None
    try:
        ids = DRIVERS_SHEET.col_values(1)
        for idx, val in enumerate(ids, start=1):
            if val == str(driver_id):
                row = DRIVERS_SHEET.row_values(idx)
                return idx, row
    except Exception as e:
        log.error("Ошибка чтения drivers: %s", e)
    return None, None


def get_driver_info(driver_id: int) -> dict | None:
    """Вернуть словарь с информацией о водителе или None."""
    _, row = get_driver_row(driver_id)
    if not row:
        return None
    # driver_id, driver_name, car_class, plate, car_photo_file_id, rating
    data = {
        "driver_id": row[0] if len(row) > 0 else "",
        "driver_name": row[1] if len(row) > 1 else "",
        "car_class": row[2] if len(row) > 2 else "",
        "plate": row[3] if len(row) > 3 else "",
        "car_photo_file_id": row[4] if len(row) > 4 else "",
        "rating": row[5] if len(row) > 5 else "",
    }
    return data


# ---------- AI ВСПОМОГАТЕЛЬНОЕ ----------

import requests

def ai_chat(system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str | None:
    """Общий помощник для запросов в OpenAI."""
    if not OPENAI_API_KEY:
        return None
    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-4.1-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("Ошибка запроса к OpenAI: %s", e)
        return None


def ai_normalize_time(user_text: str) -> str:
    """
    Нормализуем дату/время из текста клиента.
    Пример: «завтра в 10» -> «завтра 10:00» или «15.11 10:00».
    Возвращаем короткую строку, пригодную для показа клиенту.
    """
    if not OPENAI_API_KEY:
        return user_text

    system_prompt = (
        "Ты помощник диспетчера такси. "
        "Твоя задача — ПРЕОБРАЗОВАТЬ неформальное описание даты/времени клиента "
        "в короткую, понятную запись.\n\n"
        "Правила формата:\n"
        "1) Если дата сегодня — пиши только «сегодня HH:MM» (24-часовой формат).\n"
        "2) Если дата завтра — «завтра HH:MM».\n"
        "3) Если другая дата — «DD.MM HH:MM».\n"
        "4) Если клиент не указал время — используй «в ближайшее время».\n"
        "5) Никаких объяснений, только итоговая строка.\n"
    )

    result = ai_chat(system_prompt, user_text, max_tokens=30)
    if not result:
        return user_text
    return result.replace("\n", " ").strip()


# ---------- КОНСТАНТЫ СОСТОЯНИЙ ----------
PICKUP, DEST, CAR, TIME, HOURS, CONTACT, CONFIRM = range(7)

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БОТА ----------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔔 Заказ", "💰 Тарифы"],
            ["📌 Статус", "☎️ Контакт"],
            ["📸 Фото машины", "❌ Отмена"],
        ],
        resize_keyboard=True,
    )


def cars_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["Maybach W223", "Maybach W222"],
        ["S-Class W223", "S-Class W222"],
        ["Business", "Minivan"],
        ["❌ Отмена"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def hours_kb() -> ReplyKeyboardMarkup:
    # минимум 1 час, скидки от 3 часов можно описать текстом
    rows = [
        ["1 час", "2 часа"],
        ["3 часа", "4 часа"],
        ["5 часов и более"],
        ["❌ Отмена"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def yandex_maps_link(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"


def approx_price_text(car_class: str, hours: int | None) -> str:
    price_per_hour = PRICES_PER_HOUR.get(car_class)
    if not price_per_hour:
        return "по запросу"
    if not hours:
        return f"от {price_per_hour:,} ₽/ч".replace(",", " ")
    total = price_per_hour * hours
    # грубая скидка 10% от 3 часов
    if hours >= 3:
        total = int(total * 0.9)
    return f"≈ {total:,} ₽ за {hours} ч.".replace(",", " ")


def format_driver_short(driver_info: dict) -> str:
    """
    Текст вида:
    Ваш водитель:
    👨‍✈️ Имя
    🚘 Класс
    🔢 Номер авто
    ⭐ 4.9
    """
    parts = ["Ваш водитель:"]
    name = driver_info.get("driver_name") or "Водитель"
    car_class = driver_info.get("car_class") or "класс не указан"
    plate = driver_info.get("plate") or "—"
    rating = driver_info.get("rating")
    parts.append(f"👨‍✈️ {name}")
    parts.append(f"🚘 {car_class}")
    parts.append(f"🔢 Номер авто: {plate}")
    if rating:
        parts.append(f"⭐ Рейтинг: {rating}")
    return "\n".join(parts)


def find_active_order_for_client(user_id: int) -> dict | None:
    order_id = CLIENT_ACTIVE_ORDER.get(user_id)
    if not order_id:
        return None
    return ORDERS_CACHE.get(order_id)


# ---------- КОМАНДЫ ----------

async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Запустить бота"),
            BotCommand("menu", "Показать меню"),
            BotCommand("order", "Сделать заказ"),
            BotCommand("price", "Тарифы"),
            BotCommand("status", "Статус заказа"),
            BotCommand("contact", "Связаться с диспетчером"),
            BotCommand("cancel", "Отмена"),
            BotCommand("ai", "AI-помощник диспетчера"),
            BotCommand("carphoto", "Фото назначенной машины"),
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Добро пожаловать в <b>{BRAND_NAME}</b>.\n"
        "Ваш комфорт — наш приоритет.\n\n"
        "Выберите действие в меню ниже или отправьте геолокацию — подача по вашей точке.",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["<b>Тарифы (ориентировочно):</b>"]
    for name, price in PRICES_PER_HOUR.items():
        lines.append(f"• {name}: от {price:,} ₽/ч".replace(",", " "))
    lines.append("\nМинимум 1 час. Скидки действуют от 3 часов аренды.")
    lines.append("Точная стоимость зависит от маршрута, времени и загрузки.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Укажите номер заказа или дату — проверим статус и вернёмся к вам.",
        reply_markup=main_menu_kb(),
    )


async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Диспетчер: пишите здесь — ответим в чате.\n"
        "Резервный номер: +7 XXX XXX-XX-XX",
        reply_markup=main_menu_kb(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Отмена. Чем могу помочь ещё?",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


# ---------- AI-ЧАТ ДЛЯ ДИСПЕТЧЕРА ----------

async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_text(
            "Напишите так:\n"
            "/ai ситуация для клиента.\n\n"
            "Например:\n"
            "/ai машина задерживается на 10 минут\n"
            "/ai клиент просит скидку, но мы не можем дать\n"
            "/ai клиент спрашивает, можно ли детское кресло",
        )
        return

    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "AI пока не настроен. Добавьте переменную OPENAI_API_KEY в Railway."
        )
        return

    system_prompt = (
        "Ты — живой диспетчер премиум-такси (VIP taxi).\n"
        "Пиши готовые сообщения для клиента.\n\n"
        "Правила:\n"
        "1) Обращайся к клиенту на ВЫ.\n"
        "2) Пиши вежливо, кратко, 1–3 предложения.\n"
        "3) Не упоминай, что ты ИИ или модель.\n"
        "4) Не придумывай конкретные цены, если их нет в запросе.\n"
        "5) Можно использовать 1–2 нейтральных смайла 🙂🙏 при уместности.\n"
    )

    answer = ai_chat(system_prompt, question, max_tokens=120)
    if not answer:
        await update.message.reply_text(
            "Не удалось получить ответ от ИИ. Попробуйте позже."
        )
        return

    await update.message.reply_text(answer)


# ---------- /CARPHOTO ДЛЯ КЛИЕНТА ----------

async def carphoto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    order = find_active_order_for_client(user.id)
    if not order or not order.get("driver_id"):
        await update.message.reply_text(
            "Сейчас к вам не назначен водитель.\n"
            "Фото машины можно запросить после назначения водителя на заказ.",
            reply_markup=main_menu_kb(),
        )
        return

    driver_info = get_driver_info(order["driver_id"])
    if not driver_info:
        await update.message.reply_text(
            "Информация о водителе временно недоступна. "
            "Попробуйте позже или напишите диспетчеру.",
            reply_markup=main_menu_kb(),
        )
        return

    text = format_driver_short(driver_info)

    car_photo_id = driver_info.get("car_photo_file_id")
    if car_photo_id:
        await update.message.reply_photo(
            photo=car_photo_id,
            caption=text,
        )
    else:
        await update.message.reply_text(text)


# ---------- ЗАКАЗ (CONVERSATION) ----------

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    order_id = uuid4().hex[:8]

    context.user_data["order"] = {
        "order_id": order_id,
        "user_id": user.id,
        "username": f"@{user.username}" if user.username else user.full_name,
    }

    kb = ReplyKeyboardMarkup(
        [
            ["🗺 Ввести адрес вручную"],
            ["❌ Отмена"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Укажите адрес подачи.\n\n"
        "Напишите адрес или отправьте геолокацию отдельным сообщением.\n"
        "Если хотите, можете нажать «🗺 Ввести адрес вручную».",
        reply_markup=kb,
    )
    return PICKUP


async def pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # здесь клиент отправил геолокацию
    loc = update.message.location
    link = yandex_maps_link(loc.latitude, loc.longitude)
    context.user_data["order"]["pickup"] = link
    await update.message.reply_text(
        "Точка подачи сохранена.\nУкажите адрес назначения (или просто район/аэропорт).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def pickup_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "🗺 Ввести адрес вручную":
        await update.message.reply_text(
            "Напишите адрес подачи текстом:",
            reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
        )
        return PICKUP
    context.user_data["order"]["pickup"] = text
    await update.message.reply_text(
        "Укажите адрес назначения (или просто район/аэропорт).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def dest_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    context.user_data["order"]["destination"] = yandex_maps_link(
        loc.latitude, loc.longitude
    )
    await update.message.reply_text("Выберите класс авто:", reply_markup=cars_kb())
    return CAR


async def dest_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["destination"] = update.message.text.strip()
    await update.message.reply_text("Выберите класс авто:", reply_markup=cars_kb())
    return CAR


async def car_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car = update.message.text.strip()
    if car not in PRICES_PER_HOUR:
        await update.message.reply_text(
            "Пожалуйста, выберите класс авто кнопкой ниже.",
            reply_markup=cars_kb(),
        )
        return CAR
    context.user_data["order"]["car_class"] = car
    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра в 10)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    normalized = ai_normalize_time(raw)
    context.user_data["order"]["time"] = normalized

    await update.message.reply_text(
        "На сколько часов нужна машина?\nМинимум 1 час. От 3 часов действует скидка.",
        reply_markup=hours_kb(),
    )
    return HOURS


def parse_hours(text: str) -> int | None:
    # «1 час», «2 часа», «5 часов и более»
    for num in ["1", "2", "3", "4", "5"]:
        if text.startswith(num):
            try:
                return int(num)
            except ValueError:
                return None
    return None


async def hours_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = update.message.text.strip()
    hours = parse_hours(txt)
    if not hours:
        await update.message.reply_text(
            "Пожалуйста, выберите количество часов кнопкой ниже.",
            reply_markup=hours_kb(),
        )
        return HOURS
    context.user_data["order"]["hours"] = hours

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Оставьте контакт (имя и телефон) или поделитесь номером.",
        reply_markup=kb,
    )
    return CONTACT


async def contact_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    c = update.message.contact
    phone = c.phone_number
    name = f"{c.first_name or ''} {c.last_name or ''}".strip()
    context.user_data["order"]["contact"] = f"{name} {phone}".strip()
    return await confirm_order(update, context)


async def contact_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["contact"] = update.message.text.strip()
    return await confirm_order(update, context)


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    o = context.user_data["order"]
    car = o.get("car_class")
    hours = o.get("hours")
    approx = approx_price_text(car, hours)
    o["approx_price"] = approx

    text = (
        "<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('destination')}\n"
        f"• Класс авто: {car}\n"
        f"• Время подачи: {o.get('time')}\n"
        f"• Аренда: {hours} ч.\n"
        f"• Контакт: {o.get('contact')}\n"
        f"• Ориентировочно: {approx}\n\n"
        "Если всё верно — нажмите «Подтверждаю». Для отмены — «Отмена»."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтверждаю", callback_data="confirm"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
            ]
        ]
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return CONFIRM


async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("Отменено. Чем ещё помочь?")
        return ConversationHandler.END

    order = context.user_data.get("order", {})
    order_id = order.get("order_id")
    if not order_id:
        await q.edit_message_text("Произошла ошибка. Попробуйте оформить заказ заново.")
        context.user_data.clear()
        return ConversationHandler.END

    # начальные статусы
    order["status"] = "new"
    order["driver_id"] = None
    order["driver_name"] = None
    order["car_plate"] = None

    # сохраняем в таблицу и кэш
    save_order_to_sheet(order)
    ORDERS_CACHE[order_id] = order
    CLIENT_ACTIVE_ORDER[order["user_id"]] = order_id

    await q.edit_message_text("Заказ принят. Как только назначим водителя — бот пришлёт уведомление.")

    # отправляем заказ в группу водителей
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID

    if admin_id:
        text_for_drivers = (
            f"🆕 Новый заказ #{order_id}\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⌛ Аренда: {order.get('hours')} ч.\n"
            f"💰 {order.get('approx_price')}\n\n"
            f"Личные данные клиента скрыты."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 Взять заказ", callback_data=f"drv_take:{order_id}"
                    )
                ]
            ]
        )
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text_for_drivers,
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error("Не удалось отправить заказ в группу водителей: %s", e)

    context.user_data.clear()
    return ConversationHandler.END


# ---------- КНОПКИ ВОДИТЕЛЕЙ ----------

async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    driver = query.from_user

    if data.startswith("drv_take:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден или уже закрыт.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if order.get("status") != "new":
            await query.answer("Кто-то уже взял этот заказ.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # записываем водителя и проверяем его класс по таблице drivers
        driver_info = get_driver_info(driver.id)
        if driver_info and driver_info.get("car_class"):
            order_car = order.get("car_class")
            if driver_info["car_class"] != order_car:
                await query.answer(
                    f"Этот заказ только для класса: {order_car}", show_alert=True
                )
                return

        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = driver.username or driver.full_name
        order["car_plate"] = (
            driver_info["plate"] if driver_info and driver_info.get("plate") else None
        )
        ORDERS_CACHE[order_id] = order

        update_order_status_in_sheet(
            order_id,
            status="assigned",
            driver_id=order["driver_id"],
            driver_name=order["driver_name"],
            car_plate=order["car_plate"],
        )

        # сообщение в группе удаляем
        try:
            await query.message.delete()
        except Exception:
            pass

        # уведомляем клиента
        client_id = order.get("user_id")
        if client_id:
            try:
                text_client = format_driver_short(
                    driver_info
                    or {
                        "driver_name": order["driver_name"],
                        "car_class": order.get("car_class"),
                        "plate": order.get("car_plate"),
                    }
                )
                text_client += (
                    "\n\nВодитель назначен. "
                    "Как только будет на месте — вы получите уведомление."
                    "\nФото машины можно запросить командой /carphoto."
                )
                await context.bot.send_message(chat_id=int(client_id), text=text_client)
            except Exception as e:
                log.error("Не смог отправить клиенту информацию о водителе: %s", e)

        # личное сообщение водителю
        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⌛ Аренда: {order.get('hours')} ч.\n"
            f"💰 {order.get('approx_price')}\n\n"
            "Когда будете на месте — нажмите «На месте»."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🚗 На месте", callback_data=f"drv_arrived:{order_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔴 Отменить заказ", callback_data=f"drv_cancel:{order_id}"
                    )
                ],
            ]
        )
        try:
            await context.bot.send_message(
                chat_id=driver.id, text=dm_text, reply_markup=keyboard
            )
        except Exception as e:
            log.error("Не удалось отправить заказ в ЛС водителю: %s", e)

    elif data.startswith("drv_cancel:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        if order.get("driver_id") != driver.id:
            await query.answer(
                "Отменить может только водитель, который принял заказ.",
                show_alert=True,
            )
            return

        order["status"] = "new"
        order["driver_id"] = None
        order["driver_name"] = None
        order["car_plate"] = None
        ORDERS_CACHE[order_id] = order

        update_order_status_in_sheet(
            order_id,
            status="new",
            driver_id="",
            driver_name="",
            car_plate="",
        )

        # сообщение водителю
        try:
            await query.edit_message_text("Вы отменили заказ. Он возвращён в общий список.")
        except Exception:
            pass

        # вернуть заказ в группу
        try:
            admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
        except ValueError:
            admin_id = ADMIN_CHAT_ID

        if admin_id:
            text_for_drivers = (
                f"🆕 Заказ снова доступен #{order_id}\n"
                f"📍 Откуда: {order.get('pickup')}\n"
                f"🏁 Куда: {order.get('destination')}\n"
                f"🚘 Класс: {order.get('car_class')}\n"
                f"⏰ Время подачи: {order.get('time')}\n"
                f"⌛ Аренда: {order.get('hours')} ч.\n"
                f"💰 {order.get('approx_price')}\n\n"
                "Личные данные клиента скрыты."
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🟢 Взять заказ", callback_data=f"drv_take:{order_id}"
                        )
                    ]
                ]
            )
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=text_for_drivers,
                    reply_markup=keyboard,
                )
            except Exception as e:
                log.error(
                    "Не удалось вернуть заказ в группу водителей: %s",
                    e,
                )

    elif data.startswith("drv_arrived:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        if order.get("driver_id") != driver.id:
            await query.answer(
                "Отметить «на месте» может только водитель, принявший заказ.",
                show_alert=True,
            )
            return

        order["status"] = "arrived"
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(order_id, status="arrived")

        # сообщение клиенту
        client_id = order.get("user_id")
        if client_id:
            try:
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Завершить поездку",
                                callback_data=f"cli_finish:{order_id}",
                            )
                        ]
                    ]
                )
                await context.bot.send_message(
                    chat_id=int(client_id),
                    text=(
                        "🚗 Ваш водитель на месте.\n"
                        "После окончания поездки можно нажать «Завершить поездку»."
                    ),
                    reply_markup=keyboard,
                )
            except Exception as e:
                log.error("Не смог отправить клиенту уведомление «на месте»: %s", e)

        try:
            await query.edit_message_text("Отметили: вы на месте. Ожидаем клиента.")
        except Exception:
            pass

    elif data.startswith("cli_finish:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return

        order["status"] = "finished"
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(order_id, status="finished")

        # убираем активный заказ у клиента
        user_id = order.get("user_id")
        if user_id and CLIENT_ACTIVE_ORDER.get(user_id) == order_id:
            CLIENT_ACTIVE_ORDER.pop(user_id, None)

        try:
            await query.edit_message_text(
                "Спасибо! Поездка завершена. Будем рады видеть вас снова."
            )
        except Exception:
            pass


# ---------- РОУТИНГ ----------

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("carphoto", carphoto_cmd))

    # разговор заказов
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("order", order_start),
            MessageHandler(filters.Regex("^🔔 Заказ$"), order_start),
        ],
        states={
            PICKUP: [
                MessageHandler(filters.LOCATION, pickup_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pickup_text),
            ],
            DEST: [
                MessageHandler(filters.LOCATION, dest_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dest_text),
            ],
            CAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, car_choose),
            ],
            TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, time_set),
            ],
            HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hours_set),
            ],
            CONTACT: [
                MessageHandler(filters.CONTACT, contact_from_button),
                MessageHandler(filters.TEXT & ~filters.COMMAND, contact_text),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_cb, pattern="^(confirm|cancel)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # хендлер для всех callback с префиксом drv_ и cli_
    app.add_handler(CallbackQueryHandler(driver_orders_callback, pattern=r"^(drv_|cli_)"))

    # доп. кнопки меню
    app.add_handler(MessageHandler(filters.Regex("^💰 Тарифы$"), price_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📌 Статус$"), status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^☎️ Контакт$"), contact_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📸 Фото машины$"), carphoto_cmd))
    app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd))

    app.post_init = set_commands
    return app


if __name__ == "__main__":
    application = build_app()
    log.info("Bot is starting…")
    application.run_polling(close_loop=False)