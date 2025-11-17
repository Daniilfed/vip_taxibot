# -*- coding: utf-8 -*-
"""
VIP Taxi Bot – версия с:
- Google Sheets (orders, drivers)
- бронированием заказов через группу водителей
- AI-диспетчером (/ai)
- регистрацией водителей (/setdriver)
- фото машины (/carphoto)
- простым чатом клиент ↔ водитель через бота
- примитивным парсером русских фраз про время (сегодня/завтра в 10 и т.п.)
"""

import os
import json
import logging
import re
from uuid import uuid4
from datetime import datetime, timedelta

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

import requests
from google.oauth2.service_account import Credentials
import gspread

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ---------- НАСТРОЙКИ ----------
BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ID ГРУППЫ водителей (например -1003446...)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

assert BOT_TOKEN, "BOT_TOKEN is required"

# Примерные почасовые тарифы
HOURLY_PRICES = {
    "Maybach W223": 5000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
    "Minivan": 3000,
}

# ---------- ГЛОБАЛЬНЫЕ СЛОВАРИ В ПАМЯТИ ----------
# order_id -> dict(...)
ORDERS_CACHE: dict[str, dict] = {}
# для чата клиент ↔ водитель
ACTIVE_CHAT_BY_CLIENT: dict[int, str] = {}  # user_id -> order_id
ACTIVE_CHAT_BY_DRIVER: dict[int, str] = {}  # driver_id -> order_id

# ---------- GOOGLE SHEETS ----------
credentials_info = json.loads(os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"])
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

# Таблица водителей (файл "drivers", 1-я вкладка):
# A: driver_id
# B: driver_name
# C: car_class
# D: plate
# E: car_photo_file_id
# F: rating (опционально)
try:
    DRIVERS_SHEET = gc.open("drivers").sheet1
except Exception:
    DRIVERS_SHEET = None
    log.warning("Таблица drivers не найдена — регистрация водителей будет недоступна.")

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
    G: time_text (как ввёл клиент / нормализованная дата)
    H: hours_text (например '2 ч.')
    I: contact
    J: approx_price_text (например '≈ 10 000 ₽ за 2 ч.')
    K: created_at
    L: status (new / assigned / arrived / finished)
    M: driver_id
    N: driver_name
    """
    try:
        ORDERS_SHEET.append_row(
            [
                order.get("order_id"),
                order.get("user_id"),
                order.get("username"),
                order.get("pickup"),
                order.get("destination"),
                order.get("car_class"),
                order.get("time"),
                order.get("hours_text"),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order.get("status", "new"),
                str(order.get("driver_id") or ""),
                order.get("driver_name") or "",
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка записи заказа в таблицу: %s", e)


def find_order_row(order_id: str) -> int | None:
    try:
        col = ORDERS_SHEET.col_values(1)  # A
        for idx, val in enumerate(col, start=1):
            if val == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа в таблице: %s", e)
    return None


def update_order_status_in_sheet(order_id: str, status: str, driver_id=None, driver_name=None):
    row = find_order_row(order_id)
    if not row:
        return
    try:
        ORDERS_SHEET.update_cell(row, 12, status)  # L
        ORDERS_SHEET.update_cell(row, 13, str(driver_id) if driver_id else "")  # M
        ORDERS_SHEET.update_cell(row, 14, driver_name or "")  # N
    except Exception as e:
        log.error("Ошибка обновления статуса заказа: %s", e)


def get_driver_info(driver_id: int) -> dict | None:
    """Ищем водителя в таблице drivers по driver_id."""
    if DRIVERS_SHEET is None:
        return None
    try:
        records = DRIVERS_SHEET.get_all_records()
        for row in records:
            if str(row.get("driver_id")) == str(driver_id):
                return row
    except Exception as e:
        log.error("Ошибка чтения таблицы drivers: %s", e)
    return None


def save_driver(driver_id: int, name: str, car_class: str, plate: str, car_photo_file_id: str | None):
    """Добавление/обновление водителя в таблицу drivers."""
    if DRIVERS_SHEET is None:
        return
    try:
        records = DRIVERS_SHEET.get_all_records()
        # ищем существующую строку
        row_index = None
        for idx, row in enumerate(records, start=2):  # данные с 2 строки
            if str(row.get("driver_id")) == str(driver_id):
                row_index = idx
                break
        if row_index:
            # обновляем
            DRIVERS_SHEET.update(
                f"A{row_index}:E{row_index}",
                [[str(driver_id), name, car_class, plate, car_photo_file_id or ""]],
            )
        else:
            # добавляем
            DRIVERS_SHEET.append_row(
                [str(driver_id), name, car_class, plate, car_photo_file_id or ""],
                value_input_option="USER_ENTERED",
            )
    except Exception as e:
        log.error("Ошибка записи в таблицу drivers: %s", e)


# ---------- ПАРСЕР ВРЕМЕНИ ----------

def normalize_time_text(text: str) -> str:
    """
    Примитивно понимаем фразы:
    - 'сейчас'
    - 'через 10 минут'
    - 'завтра в 10', 'завтра в 10:30'
    - 'сегодня в 19:30'
    Если не смогли распарсить — возвращаем исходную строку.
    """
    t = text.strip().lower()
    now = datetime.now()

    try:
        if t == "сейчас":
            return now.strftime("%d.%m.%Y %H:%M")

        m = re.match(r"через\s+(\d+)\s*мин", t)
        if m:
            minutes = int(m.group(1))
            dt = now + timedelta(minutes=minutes)
            return dt.strftime("%d.%m.%Y %H:%M")

        if t.startswith("завтра"):
            base = now + timedelta(days=1)
            m = re.search(r"(\d{1,2})(?::(\d{2}))?", t)
            if m:
                h = int(m.group(1))
                mi = int(m.group(2) or 0)
                dt = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            else:
                dt = base.replace(hour=12, minute=0, second=0, microsecond=0)
            return dt.strftime("%d.%m.%Y %H:%M")

        if t.startswith("сегодня"):
            base = now
            m = re.search(r"(\d{1,2})(?::(\d{2}))?", t)
            if m:
                h = int(m.group(1))
                mi = int(m.group(2) or 0)
                dt = base.replace(hour=h, minute=mi, second=0, microsecond=0)
                return dt.strftime("%d.%m.%Y %H:%M")

    except Exception as e:
        log.error("Ошибка парсинга времени '%s': %s", text, e)

    return text  # не распознали


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔔 Заказ", "💰 Тарифы"],
            ["📌 Статус", "☎️ Контакт"],
            ["📷 Фото машины", "❌ Отмена"],
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
    rows = [
        ["1 час", "2 часа"],
        ["3 часа", "4 часа"],
        ["5 часов и более"],
        ["❌ Отмена"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def price_text() -> str:
    lines = ["<b>Тарифы (ориентировочно, за 1 час):</b>"]
    for car, price in HOURLY_PRICES.items():
        lines.append(f"• {car}: от {price:,} ₽/ч".replace(",", " "))
    lines.append(
        "\nМинимум 1 час. От 3 часов действует скидка, аэропорты считаются фиксированно (обычно как 2 часа)."
    )
    return "\n".join(lines)


def to_yandex_maps_link(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"


def calc_price_text(car_class: str, hours: int, pickup: str, destination: str) -> str:
    """Возвращаем человеко-читаемую строку с примерной ценой."""
    base = HOURLY_PRICES.get(car_class)
    if not base:
        return "по запросу"

    total_hours = max(1, hours)

    # скидка 10% от 3 часов
    if total_hours >= 3:
        total = int(base * total_hours * 0.9)
    else:
        total = base * total_hours

    # простое правило для аэропортов: не больше цены за 2 часа
    aero_words = ("шереметьево", "внуково", "домодедово", "жуковск")
    dest_text = f"{pickup} {destination}".lower()
    if any(w in dest_text for w in aero_words):
        total = min(total, base * 2)

    return f"≈ {total:,} ₽ за {total_hours} ч.".replace(",", " ")


# ---------- КОДЫ СОСТОЯНИЙ ДЛЯ ЗАКАЗА ----------
PICKUP, DEST, CAR, TIME_STATE, HOURS, CONTACT, CONFIRM = range(7)

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
            BotCommand("carphoto", "Фото назначенной машины"),
            BotCommand("ai", "AI-ответ для клиента"),
            BotCommand("cancel", "Отмена"),
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Добро пожаловать в <b>{BRAND_NAME}</b>.\n"
        "Ваш комфорт — наш приоритет.\n\n"
        "Чтобы сделать заказ, выберите «🔔 Заказ» или команду /order.\n"
        "Адрес подачи/назначения можно отправить текстом или точкой на карте (через скрепку).",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(price_text(), parse_mode=ParseMode.HTML)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Укажите номер заказа или дату — проверим статус и вернёмся к вам.",
        reply_markup=main_menu_kb(),
    )


async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Диспетчер: пишите здесь — ответим в чате.\nРезервный номер: +7 XXX XXX-XX-XX",
        reply_markup=main_menu_kb(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отмена. Чем ещё помочь?", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ---------- AI-ДИСПЕТЧЕР (/ai) ----------

async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_text(
            "Напишите так:\n"
            "/ai ситуация для клиента.\n\n"
            "Например:\n"
            "/ai машина задерживается на 10 минут\n"
            "/ai клиент просит скидку, но мы не можем дать\n"
            "/ai клиент спрашивает, можно ли детское кресло"
        )
        return

    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "AI-чат пока не настроен. Добавьте переменную окружения OPENAI_API_KEY в Railway."
        )
        return

    system_prompt = (
        "Ты — живой диспетчер премиум-такси VIP taxi. "
        "Пиши готовые сообщения для клиентов.\n\n"
        "Правила:\n"
        "1) Обращайся на ВЫ.\n"
        "2) Кратко: 1–3 предложения.\n"
        "3) Не придумывай конкретные цены, если их нет в вопросе.\n"
        "4) Не говори, что ты бот или ИИ.\n"
        "5) Максимум уважения, без конфликтов.\n"
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4.1-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
                "max_tokens": 250,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.error("Ошибка AI-чата: %s", e)
        await update.message.reply_text(
            "Не удалось получить ответ от ИИ. Проверьте ключ OPENAI_API_KEY и интернет на сервере."
        )


# ---------- РЕГИСТРАЦИЯ ВОДИТЕЛЕЙ /setdriver ----------

async def setdriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Простейшая регистрация водителя.
    Использование: /setdriver <класс_авто> <номер_авто>
    Пример: /setdriver "S-Class W223" A777AA77
    Фото машины можно потом загрузить: просто отправить фото с подписью /setcarphoto
    """
    if DRIVERS_SHEET is None:
        await update.message.reply_text("Таблица водителей не настроена. Скажите об этом разработчику.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/setdriver <класс_авто> <номер_авто>\n\n"
            "Например:\n"
            "/setdriver \"S-Class W223\" A777AA77"
        )
        return

    # всё, кроме последнего слова — класс авто, последнее — номер
    plate = args[-1]
    car_class = " ".join(args[:-1])

    if car_class not in HOURLY_PRICES:
        await update.message.reply_text(
            "Неизвестный класс авто. Используйте один из:\n" + ", ".join(HOURLY_PRICES.keys())
        )
        return

    driver_id = update.effective_user.id
    driver_name = update.effective_user.full_name

    # пока без фото
    save_driver(driver_id, driver_name, car_class, plate, car_photo_file_id=None)

    await update.message.reply_text(
        f"Вы зарегистрированы как водитель:\n"
        f"👤 {driver_name}\n"
        f"🚘 {car_class}\n"
        f"🔢 Номер: {plate}\n\n"
        "Чтобы привязать фото машины, отправьте боту фото с подписью /setcarphoto."
    )


async def setcarphoto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Водитель отправляет фото с подписью /setcarphoto — мы сохраняем file_id в таблицу."""
    if DRIVERS_SHEET is None:
        await update.message.reply_text("Таблица водителей не настроена.")
        return

    if not update.message.photo:
        await update.message.reply_text("Пришлите именно фото машины с подписью /setcarphoto.")
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    driver_id = update.effective_user.id
    info = get_driver_info(driver_id)
    if not info:
        await update.message.reply_text("Сначала зарегистрируйтесь командой /setdriver.")
        return

    save_driver(
        driver_id=driver_id,
        name=info.get("driver_name") or update.effective_user.full_name,
        car_class=info.get("car_class") or "",
        plate=info.get("plate") or "",
        car_photo_file_id=file_id,
    )

    await update.message.reply_text("Фото машины сохранено и привязано к вашему профилю.")


# ---------- КОМАНДА КЛИЕНТА /carphoto ----------

async def carphoto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клиент запрашивает фото машины назначенного водителя."""
    user_id = update.effective_user.id

    order_id = ACTIVE_CHAT_BY_CLIENT.get(user_id)
    if not order_id:
        await update.message.reply_text(
            "Сейчас у вас нет активной поездки с назначенным водителем."
        )
        return

    order = ORDERS_CACHE.get(order_id)
    if not order or not order.get("driver_id"):
        await update.message.reply_text("Информация о водителе временно недоступна.")
        return

    info = get_driver_info(order["driver_id"])
    if not info:
        await update.message.reply_text("Информация о водителе временно недоступна. Попробуйте позже.")
        return

    file_id = info.get("car_photo_file_id") or info.get("car_photo") or ""
    if not file_id:
        await update.message.reply_text("Фото машины ещё не загружено. Напишите диспетчеру.")
        return

    caption = (
        f"Ваш водитель:\n"
        f"👨‍✈️ {info.get('driver_name')}\n"
        f"🚘 {info.get('car_class')}\n"
        f"🔢 Номер авто: {info.get('plate')}"
    )
    await update.message.bot.send_photo(
        chat_id=user_id,
        photo=file_id,
        caption=caption,
    )


# ---------- ЗАКАЗ (CONVERSATION) ----------

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"] = {
        "order_id": uuid4().hex[:8],
        "user_id": update.effective_user.id,
        "username": f"@{update.effective_user.username}"
        if update.effective_user.username
        else update.effective_user.full_name,
    }
    await update.message.reply_text(
        "Укажите адрес подачи или отправьте точку на карте (через скрепку).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return PICKUP


async def pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    link = to_yandex_maps_link(loc.latitude, loc.longitude)
    context.user_data["order"]["pickup"] = link
    await update.message.reply_text(
        "Укажите адрес назначения или отправьте точку на карте.",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def text_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text(
        "Укажите адрес назначения или отправьте точку на карте.",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def dest_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    context.user_data["order"]["destination"] = to_yandex_maps_link(
        loc.latitude, loc.longitude
    )
    await update.message.reply_text("Выберите класс авто.", reply_markup=cars_kb())
    return CAR


async def text_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["destination"] = update.message.text.strip()
    await update.message.reply_text("Выберите класс авто.", reply_markup=cars_kb())
    return CAR


async def car_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car = update.message.text.strip()
    if car not in HOURLY_PRICES:
        await update.message.reply_text("Пожалуйста, выберите тариф кнопкой.", reply_markup=cars_kb())
        return CAR
    context.user_data["order"]["car_class"] = car
    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, сегодня в 19:30, завтра в 10)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME_STATE


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    norm = normalize_time_text(raw)
    context.user_data["order"]["time"] = norm
    await update.message.reply_text(
        "На сколько часов нужна машина?\nМинимум 1 час. От 3 часов действует скидка.",
        reply_markup=hours_kb(),
    )
    return HOURS


async def hours_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    m = re.search(r"(\d+)", text)
    hours = int(m.group(1)) if m else 1
    if hours < 1:
        hours = 1
    context.user_data["order"]["hours"] = hours
    context.user_data["order"]["hours_text"] = f"{hours} ч."
    await update.message.reply_text(
        "Оставьте контакт (имя и телефон), или поделитесь номером кнопкой ниже.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
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
    hours = o.get("hours", 1)
    o["approx_price"] = calc_price_text(
        car_class=o.get("car_class"),
        hours=hours,
        pickup=o.get("pickup", ""),
        destination=o.get("destination", ""),
    )

    text = (
        "<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('destination')}\n"
        f"• Класс авто: {o.get('car_class')}\n"
        f"• Время подачи: {o.get('time')}\n"
        f"• Аренда: {o.get('hours_text')}\n"
        f"• Контакт: {o.get('contact')}\n"
        f"• Ориентировочно: {o.get('approx_price')}\n\n"
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

    order = context.user_data["order"]
    order["status"] = "new"
    order["driver_id"] = None
    order["driver_name"] = None

    save_order_to_sheet(order)

    global ORDERS_CACHE
    ORDERS_CACHE[order["order_id"]] = dict(order)

    await q.edit_message_text(
        "Заказ принят. Как только назначим водителя — бот пришлёт уведомление."
    )

    # отправляем в группу водителей
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID

    if admin_id:
        text_for_drivers = (
            f"🆕 Новый заказ #{order.get('order_id')}\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours_text')}\n"
            f"💰 {order.get('approx_price')}\n\n"
            f"Личные данные клиента скрыты."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 Взять заказ", callback_data=f"drv_take:{order.get('order_id')}"
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
            log.error("Не удалось отправить заказ в группу: %s", e)

    context.user_data.clear()
    return ConversationHandler.END


# ---------- ОБРАБОТКА КНОПОК ВОДИТЕЛЕЙ + ЧАТ ----------

async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    driver = query.from_user

    global ORDERS_CACHE, ACTIVE_CHAT_BY_CLIENT, ACTIVE_CHAT_BY_DRIVER

    # Взять заказ
    if data.startswith("drv_take:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ уже не активен.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # проверка класса авто по таблице drivers
        info = get_driver_info(driver.id)
        if not info:
            await query.answer("Сначала зарегистрируйтесь командой /setdriver.", show_alert=True)
            return
        driver_car_class = info.get("car_class")
        if driver_car_class != order.get("car_class"):
            await query.answer(
                f"Этот заказ только для класса {order.get('car_class')}. У вас: {driver_car_class}",
                show_alert=True,
            )
            return

        if order.get("status") in ("assigned", "arrived", "finished"):
            await query.answer("Заказ уже забрал другой водитель.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = driver.username or driver.full_name
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(
            order_id, status="assigned", driver_id=driver.id, driver_name=order["driver_name"]
        )

        try:
            await query.message.delete()
        except Exception:
            pass

        # чат: сохраняем связь
        ACTIVE_CHAT_BY_CLIENT[order["user_id"]] = order_id
        ACTIVE_CHAT_BY_DRIVER[driver.id] = order_id

        # уведомляем клиента
        client_id = order.get("user_id")
        if client_id:
            plate = info.get("plate") or "—"
            msg = (
                "Ваш водитель:\n"
                f"👨‍✈️ {info.get('driver_name')}\n"
                f"🚘 {info.get('car_class')}\n"
                f"🔢 Номер авто: {plate}\n\n"
                "Водитель назначен. Как только будет на месте — вы получите уведомление.\n"
                "Фото машины можно запросить командой /carphoto или кнопкой «📷 Фото машины»."
            )
            try:
                await context.bot.send_message(chat_id=int(client_id), text=msg)
            except Exception as e:
                log.error("Не удалось отправить сообщение клиенту: %s", e)

        # сообщение водителю
        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours_text')}\n"
            f"💰 {order.get('approx_price')}\n\n"
            "Чат с клиентом уже активен: просто пишите сюда — сообщения будут уходить клиенту."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🚗 На месте", callback_data=f"drv_arrived:{order_id}"),
                ],
            ]
        )
        try:
            await context.bot.send_message(
                chat_id=driver.id,
                text=dm_text,
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error("Не удалось отправить заказ в ЛС водителю: %s", e)

    # Водитель на месте
    elif data.startswith("drv_arrived:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return

        if order.get("driver_id") != driver.id:
            await query.answer("Отметить может только водитель, принявший заказ.", show_alert=True)
            return

        order["status"] = "arrived"
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(
            order_id, status="arrived", driver_id=order.get("driver_id"), driver_name=order.get("driver_name")
        )

        client_id = order.get("user_id")
        if client_id:
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Завершить поездку", callback_data=f"trip_finish:{order_id}"
                        )
                    ]
                ]
            )
            try:
                await context.bot.send_message(
                    chat_id=int(client_id),
                    text="🚗 Ваш водитель на месте.\nПосле окончания поездки можно нажать «Завершить поездку».",
                    reply_markup=kb,
                )
            except Exception as e:
                log.error("Не смог отправить сообщение клиенту: %s", e)

        try:
            await query.edit_message_text("Отметили: вы на месте.")
        except Exception:
            pass

    # Завершение поездки
    elif data.startswith("trip_finish:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return

        order["status"] = "finished"
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(
            order_id, status="finished",
            driver_id=order.get("driver_id"),
            driver_name=order.get("driver_name"),
        )

        client_id = order.get("user_id")
        driver_id = order.get("driver_id")

        # чистим чат-карты
        if client_id in ACTIVE_CHAT_BY_CLIENT:
            del ACTIVE_CHAT_BY_CLIENT[client_id]
        if driver_id in ACTIVE_CHAT_BY_DRIVER:
            del ACTIVE_CHAT_BY_DRIVER[driver_id]

        try:
            await query.edit_message_text("Поездка завершена. Спасибо, что выбрали VIP taxi.")
        except Exception:
            pass

        if driver_id:
            try:
                await context.bot.send_message(
                    chat_id=int(driver_id),
                    text="Поездка завершена. Спасибо за работу!",
                )
            except Exception:
                pass


# ---------- РЕЛЕЙ ЧАТА КЛИЕНТ ↔ ВОДИТЕЛЬ ----------

async def relay_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если у пользователя есть активный заказ — пересылаем сообщения второй стороне."""
    if not update.message or not update.message.text:
        return

    uid = update.effective_user.id
    text = update.message.text

    order_id = None
    role = None
    if uid in ACTIVE_CHAT_BY_CLIENT:
        order_id = ACTIVE_CHAT_BY_CLIENT[uid]
        role = "client"
    elif uid in ACTIVE_CHAT_BY_DRIVER:
        order_id = ACTIVE_CHAT_BY_DRIVER[uid]
        role = "driver"

    if not order_id:
        return

    order = ORDERS_CACHE.get(order_id)
    if not order:
        return

    if role == "client":
        peer_id = order.get("driver_id")
        prefix = "Сообщение от клиента:"
    else:
        peer_id = order.get("user_id")
        prefix = "Сообщение от водителя:"

    if not peer_id:
        return

    try:
        await context.bot.send_message(
            chat_id=int(peer_id),
            text=f"{prefix}\n{text}",
        )
    except Exception as e:
        log.error("Ошибка пересылки сообщения в чате: %s", e)


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
    app.add_handler(CommandHandler("setdriver", setdriver_cmd))
    app.add_handler(CommandHandler("setcarphoto", setcarphoto_cmd))
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_pickup),
            ],
            DEST: [
                MessageHandler(filters.LOCATION, dest_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_dest),
            ],
            CAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, car_choose),
            ],
            TIME_STATE: [
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

    # кнопки водителей / завершение поездки
    app.add_handler(CallbackQueryHandler(driver_orders_callback, pattern=r"^(drv_|trip_)"))

    # Кнопки меню
    app.add_handler(MessageHandler(filters.Regex("^💰 Тарифы$"), price_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📌 Статус$"), status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^☎️ Контакт$"), contact_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📷 Фото машины$"), carphoto_cmd))
    app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd))

    # Релей чата — в самом конце
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_chat))

    app.post_init = set_commands
    return app


if __name__ == "__main__":
    app = build_app()
    log.info("Bot is starting…")
    app.run_polling(close_loop=False)