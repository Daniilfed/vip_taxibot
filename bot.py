# -*- coding: utf-8 -*-
# VIP Taxi Bot — заказы, срочные заказы, Google Sheets, регистрация водителей, чат и фото авто

import os
import json
import logging
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
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- ЛОГИ ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ---------------- НАСТРОЙКИ ----------------
BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ID группы водителей
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

assert BOT_TOKEN, "BOT_TOKEN is required"

# Тарифы (за 1 час)
HOURLY_PRICES = {
    "Maybach W223": 5000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
    "Minivan": 3000,
}

# Фиксированные аэропорты (не дороже 2х часов аренды)
AIRPORT_PRICES = {
    "Шереметьево": 2,   # множитель от часа
    "Домодедово": 2,
    "Внуково": 2,
}

# Кэш заказов и чатов
ORDERS_CACHE: dict[str, dict] = {}           # order_id -> данные заказа
CURRENT_ORDER_BY_USER: dict[int, str] = {}   # user_id -> order_id (для чата)
CHAT_LINKS: dict[int, int] = {}              # user_id -> другой участник

# ---------------- GOOGLE SHEETS ----------------
from google.oauth2.service_account import Credentials
import gspread

credentials_info = json.loads(os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"])
credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
gc = gspread.authorize(credentials)

DOC = gc.open("orders")
ORDERS_SHEET = DOC.sheet1
try:
    DRIVERS_SHEET = DOC.worksheet("drivers")
except Exception:
    DRIVERS_SHEET = None
    log.warning("Лист 'drivers' не найден в таблице orders")

# ---------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ SHEETS ----------------
def save_order_to_sheet(order: dict) -> None:
    """Записать заказ в orders (Лист1)."""
    try:
        ORDERS_SHEET.append_row(
            [
                order.get("order_id"),
                order.get("user_id"),
                order.get("username"),
                order.get("pickup"),
                order.get("destination"),
                order.get("car_class"),
                order.get("time"),        # уже нормализованная дата/время
                order.get("hours_text"),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                order.get("status", "new"),
                order.get("driver_id") or "",
                order.get("driver_name") or "",
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка Google Sheets (save_order_to_sheet): %s", e)


def find_order_row(order_id: str):
    try:
        col = ORDERS_SHEET.col_values(1)
        for idx, val in enumerate(col, start=1):
            if val == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа: %s", e)
    return None


def update_order_status_in_sheet(order_id: str, status: str, driver_id=None, driver_name=None):
    row = find_order_row(order_id)
    if not row:
        return
    try:
        ORDERS_SHEET.update_cell(row, 12, status)  # L: status
        ORDERS_SHEET.update_cell(row, 13, str(driver_id) if driver_id else "")
        ORDERS_SHEET.update_cell(row, 14, driver_name or "")
    except Exception as e:
        log.error("Ошибка обновления статуса заказа: %s", e)


def get_driver_row(driver_id: int):
    if not DRIVERS_SHEET:
        return None, None
    try:
        col = DRIVERS_SHEET.col_values(1)
        for idx, val in enumerate(col, start=1):
            if str(val) == str(driver_id):
                values = DRIVERS_SHEET.row_values(idx)
                # A..I
                data = {
                    "driver_id": values[0],
                    "driver_name": values[1],
                    "car_class": values[2],
                    "plate": values[3],
                    "car_photo_file_id": values[4],
                    "rating": values[5] if len(values) > 5 else "",
                    "last_lat": values[6] if len(values) > 6 else "",
                    "last_lon": values[7] if len(values) > 7 else "",
                    "last_update": values[8] if len(values) > 8 else "",
                }
                return idx, data
    except Exception as e:
        log.error("Ошибка чтения drivers: %s", e)
    return None, None


def upsert_driver(driver_id: int, driver_name: str, car_class: str, plate: str, photo_id: str):
    """Создать или обновить запись водителя в листе drivers."""
    global DRIVERS_SHEET
    if not DRIVERS_SHEET:
        # пробуем создать лист
        try:
            DRIVERS_SHEET = DOC.add_worksheet("drivers", rows=100, cols=9)
            DRIVERS_SHEET.append_row(
                ["driver_id", "driver_name", "car_class", "plate",
                 "car_photo_file_id", "rating", "last_lat", "last_lon", "last_update"]
            )
        except Exception as e:
            log.error("Не удалось создать лист drivers: %s", e)
            return

    row_idx, existing = get_driver_row(driver_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if row_idx:
        DRIVERS_SHEET.update_row(
            row_idx,
            [
                str(driver_id),
                driver_name,
                car_class,
                plate,
                photo_id,
                existing.get("rating", ""),
                existing.get("last_lat", ""),
                existing.get("last_lon", ""),
                now,
            ],
        )
    else:
        DRIVERS_SHEET.append_row(
            [str(driver_id), driver_name, car_class, plate, photo_id, "5.0", "", "", now]
        )


def set_driver_location(driver_id: int, lat: float, lon: float):
    row_idx, existing = get_driver_row(driver_id)
    if not row_idx:
        return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        DRIVERS_SHEET.update_row(
            row_idx,
            [
                existing["driver_id"],
                existing["driver_name"],
                existing["car_class"],
                existing["plate"],
                existing["car_photo_file_id"],
                existing.get("rating", ""),
                str(lat),
                str(lon),
                now,
            ],
        )
    except Exception as e:
        log.error("Ошибка обновления координат: %s", e)


# ---------------- ВСПОМОГАТЕЛЬНОЕ ----------------
PICKUP, DEST, CAR, TIME, HOURS, CONTACT, CONFIRM = range(7)
D_CLASS, D_PLATE, D_PHOTO = range(100, 103)

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔔 Заказ", "⚡ Срочный заказ"],
            ["💰 Тарифы", "📌 Статус"],
            ["☎️ Контакт", "📸 Фото машины"],
            ["❌ Отмена"],
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


def price_text() -> str:
    lines = ["<b>Тарифы (за 1 час, ориентировочно):</b>"]
    for k, v in HOURLY_PRICES.items():
        lines.append(f"• {k}: от {v:,} ₽/ч".replace(",", " "))
    lines.append(
        "\nМинимальный заказ: 1 час. От 3 часов действует скидка, точная стоимость зависит от маршрута и времени."
    )
    lines.append(
        "\n<b>Аэропорты (фикс):</b>\n"
        "• Шереметьево: не дороже 2 часов выбранного класса\n"
        "• Домодедово: не дороже 2 часов\n"
        "• Внуково: не дороже 2 часов"
    )
    return "\n".join(lines)


def to_yandex_maps_link(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"


def approx_price(car_class: str, hours: int | None, destination: str | None) -> str:
    base = HOURLY_PRICES.get(car_class)
    if not base:
        return "По запросу"

    # аэропорты
    if destination:
        for airport, mult in AIRPORT_PRICES.items():
            if airport.lower() in destination.lower():
                price = base * mult
                return f"{price:,} ₽ фикс".replace(",", " ")

    if not hours:
        return f"от {base:,} ₽/ч".replace(",", " ")

    # скидка от 3 часов (-10%)
    total = base * hours
    if hours >= 3:
        total = int(total * 0.9)
    return f"≈ {total:,} ₽ за {hours} ч.".replace(",", " ")


def normalize_datetime(text: str) -> str:
    """
    Примитивный парсер: 'сейчас', 'через 30 мин', 'завтра в 10', '16.11 19:30'.
    Возвращает строку 'YYYY-MM-DD HH:MM' или исходный текст, если не получилось.
    """
    s = text.strip().lower()
    now = datetime.now()

    if s in ("сейчас", "как можно скорее", "как можно скорее!", "как можно быстрей"):
        return now.strftime("%Y-%m-%d %H:%M")

    if s.startswith("через "):
        try:
            part = s.replace("через", "").strip()
            mins = 0
            if "мин" in part:
                num = "".join(ch for ch in part if ch.isdigit())
                mins = int(num or "0")
            elif "час" in part:
                num = "".join(ch for ch in part if ch.isdigit())
                mins = int(num or "0") * 60
            dt = now + timedelta(minutes=mins)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return text

    day_offset = 0
    if "послезавтра" in s:
        day_offset = 2
        s = s.replace("послезавтра", "").strip()
    elif "завтра" in s:
        day_offset = 1
        s = s.replace("завтра", "").strip()
    elif "сегодня" in s:
        day_offset = 0
        s = s.replace("сегодня", "").strip()

    date_obj = now.date() + timedelta(days=day_offset)

    # время вида 10:30 или 10.30 или просто 10
    hour = 0
    minute = 0
    import re

    m = re.search(r"(\d{1,2})[:\.](\d{2})", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
    else:
        m2 = re.search(r"\b(\d{1,2})\b", s)
        if m2:
            hour = int(m2.group(1))
            minute = 0
        else:
            # дата в формате 16.11 10:00
            m3 = re.search(r"(\d{1,2})\.(\d{1,2})\s+(\d{1,2})[:\.](\d{2})", s)
            if m3:
                day = int(m3.group(1))
                month = int(m3.group(2))
                hour = int(m3.group(3))
                minute = int(m3.group(4))
                year = now.year
                try:
                    dt = datetime(year, month, day, hour, minute)
                    return dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return text
            else:
                return text

    try:
        dt = datetime.combine(date_obj, datetime.min.time()).replace(hour=hour, minute=minute)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text


# ---------------- КОМАНДЫ ----------------
async def set_commands(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Запустить бота"),
            BotCommand("menu", "Показать меню"),
            BotCommand("order", "Сделать заказ"),
            BotCommand("urgent", "Срочный заказ"),
            BotCommand("price", "Тарифы"),
            BotCommand("status", "Статус заказа"),
            BotCommand("contact", "Связаться с диспетчером"),
            BotCommand("carphoto", "Фото вашей машины"),
            BotCommand("setdriver", "Регистрация водителя"),
            BotCommand("ai", "AI-подсказка для диспетчера"),
            BotCommand("cancel", "Отмена"),
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
    await update.message.reply_text("Отмена. Чем могу помочь ещё?", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ---------------- AI-ДИСПЕТЧЕР ----------------
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
            "AI-чат пока не настроен. Добавьте переменную OPENAI_API_KEY в Railway."
        )
        return

    import requests

    system_prompt = (
        "Ты — живой диспетчер премиум-такси (VIP такси). "
        "Пиши готовые сообщения для клиента от лица сервиса.\n"
        "Всегда «Вы», вежливо, коротко (1–3 предложения), без цен если их не дали.\n"
        "Не упоминай, что ты бот или ИИ."
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "max_tokens": 250,
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.error("Ошибка AI-чата: %s", e)
        await update.message.reply_text("Не удалось получить ответ от AI, попробуйте позже.")


# ---------------- ЗАКАЗ (обычный и срочный) ----------------
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE, urgent: bool = False) -> int:
    context.user_data["order"] = {
        "order_id": uuid4().hex[:8],
        "user_id": update.effective_user.id,
        "username": f"@{update.effective_user.username}"
        if update.effective_user.username
        else update.effective_user.full_name,
        "urgent": urgent,
    }
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить мою геолокацию", request_location=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Укажите адрес подачи или отправьте свою геолокацию кнопкой ниже.",
        reply_markup=kb,
    )
    return PICKUP


async def order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await order_start(update, context, urgent=False)


async def urgent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await order_start(update, context, urgent=True)


async def pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    link = to_yandex_maps_link(loc.latitude, loc.longitude)
    context.user_data["order"]["pickup"] = link
    await update.message.reply_text(
        "Точка подачи получена.\n📍 Укажите адрес назначения (или напишите 'по городу').",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def text_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text(
        "Укажите адрес назначения (или напишите 'по городу').",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def dest_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    context.user_data["order"]["destination"] = to_yandex_maps_link(loc.latitude, loc.longitude)
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

    if context.user_data["order"].get("urgent"):
        # срочный заказ: без часов
        context.user_data["order"]["hours_text"] = ""
        context.user_data["order"]["time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        await update.message.reply_text(
            "Оставьте контакт (имя и телефон), или поделитесь номером:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return CONTACT

    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    normalized = normalize_datetime(raw)
    context.user_data["order"]["time"] = normalized
    await update.message.reply_text(
        "На сколько часов нужна машина? (минимум 1 час). От 3 часов действует скидка.",
        reply_markup=ReplyKeyboardMarkup(
            [["1 час", "2 часа"], ["3 часа", "4 часа"], ["5 часов и более"], ["❌ Отмена"]],
            resize_keyboard=True,
        ),
    )
    return HOURS


async def hours_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = update.message.text.strip()
    hours = 1
    if "5" in txt or "более" in txt:
        hours = 5
    elif txt.startswith("4"):
        hours = 4
    elif txt.startswith("3"):
        hours = 3
    elif txt.startswith("2"):
        hours = 2

    context.user_data["order"]["hours"] = hours
    context.user_data["order"]["hours_text"] = f"{hours} ч."

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Оставьте контакт (имя и телефон), или поделитесь номером:", reply_markup=kb
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
    destination = o.get("destination")

    price = approx_price(car, hours, destination)
    o["approx_price"] = price

    text = (
        "<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {destination or 'по городу'}\n"
        f"• Класс авто: {car}\n"
        f"• Время подачи: {o.get('time')}\n"
        f"• Аренда: {o.get('hours_text') or 'по факту'}\n"
        f"• Контакт: {o.get('contact')}\n"
        f"• Ориентировочно: {price}\n\n"
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

    # кэш
    order_id = order["order_id"]
    ORDERS_CACHE[order_id] = order

    await q.edit_message_text(
        "Заказ принят. Как только назначим водителя — бот пришлёт уведомление."
    )

    # отправка в группу водителей
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID

    if admin_id:
        text_for_drivers = (
            f"🆕 <b>Новый заказ</b> #{order_id}\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination') or 'по городу'}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours_text') or 'по факту'}\n"
            f"💰 Ориентировочно: {order.get('approx_price')}\n\n"
            f"Личные данные клиента скрыты."
        )
        if order.get("urgent"):
            text_for_drivers = "⚡ <b>Срочный заказ</b>\n" + text_for_drivers

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
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.error("Не удалось отправить заказ в группу: %s", e)

    context.user_data.clear()
    return ConversationHandler.END


# ---------------- РЕГИСТРАЦИЯ ВОДИТЕЛЯ ----------------
async def setdriver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Регистрация водителя.\nВыберите класс авто:", reply_markup=cars_kb()
    )
    return D_CLASS


async def setdriver_carclass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car = update.message.text.strip()
    if car not in HOURLY_PRICES:
        await update.message.reply_text("Выберите класс кнопкой.", reply_markup=cars_kb())
        return D_CLASS
    context.user_data["driver_reg"] = {"car_class": car}
    await update.message.reply_text(
        "Введите номер авто (как на госномере, например A777AA77).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return D_PLATE


async def setdriver_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plate = update.message.text.strip()
    context.user_data["driver_reg"]["plate"] = plate
    await update.message.reply_text(
        "Пришлите <b>одно фото</b> вашей машины (вид сбоку/3⁄4).",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return D_PHOTO


async def setdriver_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно прислать именно фото, попробуйте ещё раз.")
        return D_PHOTO

    file_id = update.message.photo[-1].file_id
    d = context.user_data["driver_reg"]
    driver = update.effective_user
    upsert_driver(driver.id, driver.full_name, d["car_class"], d["plate"], file_id)

    await update.message.reply_text(
        "Данные водителя сохранены.\n"
        f"Класс: {d['car_class']}\n"
        f"Номер авто: {d['plate']}\n"
        "Теперь вы можете брать заказы в группе водителей.",
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop("driver_reg", None)
    return ConversationHandler.END


# ---------------- ЛОГИКА ВОДИТЕЛЕЙ (бронь, отмена, на месте) ----------------
async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    driver = query.from_user

    global ORDERS_CACHE, CURRENT_ORDER_BY_USER, CHAT_LINKS

    # взять заказ
    if data.startswith("drv_take:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Этот заказ уже не активен.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # проверяем водителя в таблице
        _, driver_info = get_driver_row(driver.id)
        if not driver_info:
            await query.answer(
                "Вы не зарегистрированы как водитель. Напишите боту в личку /setdriver.",
                show_alert=True,
            )
            return

        if driver_info["car_class"] != order.get("car_class"):
            await query.answer(
                f"Заказ только для класса {order.get('car_class')}. "
                f"У вас: {driver_info['car_class']}.",
                show_alert=True,
            )
            return

        if order.get("status") in ("assigned", "arrived", "finished"):
            await query.answer("Заказ уже взял другой водитель.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # назначаем
        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = driver.full_name
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(order_id, "assigned", driver.id, driver.full_name)

        try:
            await query.message.delete()
        except Exception:
            pass

        # обновляем мапы для чата
        client_id = int(order["user_id"])
        CURRENT_ORDER_BY_USER[client_id] = order_id
        CURRENT_ORDER_BY_USER[driver.id] = order_id
        CHAT_LINKS[client_id] = driver.id
        CHAT_LINKS[driver.id] = client_id

        # сообщение водителю
        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination') or 'по городу'}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours_text') or 'по факту'}\n"
            f"💰 Ориентировочно: {order.get('approx_price')}\n\n"
            "Когда будете на месте — нажмите «На месте»."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🚗 На месте", callback_data=f"drv_arrived:{order_id}"),
                ],
                [
                    InlineKeyboardButton("🔴 Отменить заказ", callback_data=f"drv_cancel:{order_id}"),
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

        # сообщение клиенту
        plate = driver_info.get("plate") or "—"
        await context.bot.send_message(
            chat_id=client_id,
            text=(
                "Ваш водитель назначен.\n\n"
                f"👨‍✈️ Имя: {driver.full_name}\n"
                f"🚘 Класс: {driver_info.get('car_class')}\n"
                f"🔢 Номер авто: {plate}\n\n"
                "Как только водитель будет на месте — вы получите уведомление.\n"
                "Фото машины можно запросить командой /carphoto или кнопкой «📸 Фото машины».\n\n"
                "С этого момента вы можете переписываться через бота — просто пишите сообщение, "
                "оно будет доставлено водителю."
            ),
            reply_markup=main_menu_kb(),
        )

    # отмена водителем
    elif data.startswith("drv_cancel:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        if order.get("driver_id") != driver.id:
            await query.answer("Отменить может только водитель, принявший заказ.", show_alert=True)
            return

        client_id = int(order["user_id"])
        order["status"] = "new"
        order["driver_id"] = None
        order["driver_name"] = None
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(order_id, "new", None, None)

        CURRENT_ORDER_BY_USER.pop(driver.id, None)
        CURRENT_ORDER_BY_USER.pop(client_id, None)
        CHAT_LINKS.pop(driver.id, None)
        CHAT_LINKS.pop(client_id, None)

        try:
            await query.edit_message_text("Вы отменили заказ. Он возвращён в общий список.")
        except Exception:
            pass

        # возвращаем в группу
        try:
            admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
        except ValueError:
            admin_id = ADMIN_CHAT_ID

        if admin_id:
            text_for_drivers = (
                f"🆕 Заказ снова доступен #{order_id}\n"
                f"📍 Откуда: {order.get('pickup')}\n"
                f"🏁 Куда: {order.get('destination') or 'по городу'}\n"
                f"🚘 Класс: {order.get('car_class')}\n"
                f"⏰ Время подачи: {order.get('time')}\n"
                f"⏳ Аренда: {order.get('hours_text') or 'по факту'}\n"
                f"💰 Ориентировочно: {order.get('approx_price')}\n\n"
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
                log.error("Не удалось вернуть заказ в группу: %s", e)

        # уведомляем клиента
        await context.bot.send_message(
            chat_id=client_id,
            text="Водитель отменил заказ. Мы подберём другого водителя.",
        )

    # водитель на месте
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
        update_order_status_in_sheet(
            order_id, "arrived", order.get("driver_id"), order.get("driver_name")
        )

        client_id = int(order["user_id"])

        # клиенту
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Завершить поездку", callback_data=f"finish:{order_id}"
                    )
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=client_id,
            text=(
                "🚗 Ваш водитель на месте.\n"
                "После окончания поездки можно нажать «Завершить поездку».\n"
                "Пишите сюда, чтобы связаться с водителем."
            ),
            reply_markup=keyboard,
        )

        try:
            await query.edit_message_text("Отметили: вы на месте. Ожидаем клиента.")
        except Exception:
            pass

    # завершение поездки клиентом
    elif data.startswith("finish:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        client_id = int(order["user_id"])
        driver_id = int(order.get("driver_id") or 0)

        order["status"] = "finished"
        ORDERS_CACHE[order_id] = order
        update_order_status_in_sheet(
            order_id, "finished", order.get("driver_id"), order.get("driver_name")
        )

        CURRENT_ORDER_BY_USER.pop(client_id, None)
        CURRENT_ORDER_BY_USER.pop(driver_id, None)
        CHAT_LINKS.pop(client_id, None)
        CHAT_LINKS.pop(driver_id, None)

        try:
            await query.edit_message_text("Поездка завершена. Спасибо!")
        except Exception:
            pass

        if driver_id:
            await context.bot.send_message(
                chat_id=driver_id,
                text=f"Клиент завершил поездку по заказу #{order_id}.",
            )


# ---------------- ЧАТ КЛИЕНТ ↔ ВОДИТЕЛЬ ----------------
async def relay_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересылка обычных сообщений между клиентом и водителем через бота."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    if user_id not in CURRENT_ORDER_BY_USER:
        return
    if update.message.text.startswith("/"):
        # команды не пересылаем
        return

    order_id = CURRENT_ORDER_BY_USER[user_id]
    order = ORDERS_CACHE.get(order_id)
    if not order:
        return

    if user_id == int(order["user_id"]):
        peer_id = int(order.get("driver_id") or 0)
        prefix = "Сообщение от клиента:\n"
    else:
        peer_id = int(order["user_id"])
        prefix = "Сообщение от водителя:\n"

    if not peer_id:
        return

    try:
        await context.bot.send_message(
            chat_id=peer_id,
            text=prefix + update.message.text,
        )
    except Exception as e:
        log.error("Ошибка пересылки чата: %s", e)


# ---------------- ФОТО МАШИНЫ ----------------
async def carphoto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправить клиенту фото машины по текущему активному заказу."""
    user_id = update.effective_user.id
    order_id = CURRENT_ORDER_BY_USER.get(user_id)
    if not order_id:
        await update.message.reply_text(
            "Сейчас нет активного заказа, к которому можно показать авто."
        )
        return

    order = ORDERS_CACHE.get(order_id)
    if not order or not order.get("driver_id"):
        await update.message.reply_text("Информация о водителе временно недоступна.")
        return

    _, driver_info = get_driver_row(int(order["driver_id"]))
    if not driver_info or not driver_info.get("car_photo_file_id"):
        await update.message.reply_text(
            "Фото машины пока не загружено. Попробуйте позже или напишите диспетчеру."
        )
        return

    caption = (
        f"Ваш водитель:\n"
        f"👨‍✈️ {order.get('driver_name')}\n"
        f"🚘 {driver_info.get('car_class')}\n"
        f"🔢 Номер авто: {driver_info.get('plate') or '—'}"
    )
    await update.message.reply_photo(
        photo=driver_info["car_photo_file_id"],
        caption=caption,
    )


# ---------------- РОУТИНГ ----------------
def build_app() -> Application:
    # ---------- УМНЫЙ ОТВЕТ НА СВОБОДНЫЙ ТЕКСТ КЛИЕНТА ----------
async def smart_client_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает обычные текстовые сообщения от клиента БЕЗ /ai.
    Если человек пишет что-то вроде «нужна машина завтра в 10 из Шереметьево»,
    бот поймёт, что это запрос поездки, переформулирует и подскажет, что делать дальше.
    """

    # работаем только в личке с ботом, в группе водителей ничего не делаем
    chat = update.effective_chat
    if chat.type != "private":
        return

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Это всё равно обработают другие хендлеры, тут не мешаемся
    menu_phrases = {"🔔 Заказ", "💰 Тарифы", "📌 Статус", "☎️ Контакт", "📸 Фото машины", "❌ Отмена"}
    if text in menu_phrases or text.startswith("/"):
        return

    # Если нет ключа OpenAI — просто молча выходим, чтобы не спамить ошибками
    if not OPENAI_API_KEY:
        return

    import requests

    system_prompt = (
        "Ты — вежливый живой диспетчер премиум-такси «VIP taxi».\n"
        "К тебе в ЛИЧНЫЙ чат пишет КЛИЕНТ в свободной форме.\n"
        "Твоя задача — написать готовое сообщение для клиента.\n\n"
        "Три возможные ситуации:\n"
        "1) Клиент ОПИСЫВАЕТ ПОЕЗДКУ (нужна машина, хочу заказать, завтра в 10, из аэропорта и т.п.).\n"
        "   Тогда:\n"
        "   - коротко и понятно переформулируй, что ты понял (дата/время, откуда, куда, что за поездка),\n"
        "   - вежливо напиши, что для оформления заказа нужно нажать кнопку «Заказ» внизу чата "
        "     или отправить команду /order,\n"
        "   - перечисли, каких данных может не хватать (класс авто, время аренды, количество часов и т.п.).\n"
        "   НИКОГДА не пиши фразы вида «заказ подтверждён», «мы оформили заказ» и т.п. — ты только помогаешь.\n\n"
        "2) Клиент задаёт ВОПРОС про такси (цены, детское кресло, оплата, встреча в аэропорту и т.д.).\n"
        "   - Просто ответь как живой диспетчер, кратко и по делу (1–3 предложения).\n"
        "   - Если вопрос касается цены, говори аккуратно и общими формулировками "
        "     (ориентировочно, точную цену назовёт диспетчер).\n\n"
        "3) Сообщение вообще НЕ про такси.\n"
        "   - Вежливо скажи, что ты бот сервиса премиум-такси и можешь помочь только с поездками.\n\n"
        "Всегда обращайся на ВЫ. Пиши по-русски. Не упоминай, что ты ИИ или бот.\n"
        "Верни только текст ответа клиенту."
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": 250,
        "temperature": 0.4,
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip()
        if answer:
            await update.message.reply_text(answer)
    except Exception as e:
        log.error("Ошибка smart_client_text: %s", e)
        # В случае ошибки просто молчим, чтобы не пугать клиента
        return
        
    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("urgent", urgent_cmd))
    app.add_handler(CommandHandler("carphoto", carphoto_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # регистрация водителя
    driver_conv = ConversationHandler(
        entry_points=[CommandHandler("setdriver", setdriver_start)],
        states={
            D_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setdriver_carclass)],
            D_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setdriver_plate)],
            D_PHOTO: [MessageHandler(filters.PHOTO, setdriver_photo)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd),
        ],
    )
    app.add_handler(driver_conv)

    # разговор заказов
    order_conv = ConversationHandler(
        entry_points=[
            CommandHandler("order", order_cmd),
            MessageHandler(filters.Regex("^🔔 Заказ$"), order_cmd),
            MessageHandler(filters.Regex("^⚡ Срочный заказ$"), urgent_cmd),
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
    app.add_handler(order_conv)

    # кнопки водителей
    app.add_handler(CallbackQueryHandler(driver_orders_callback, pattern=r"^(drv_|finish:)"))

    # меню-кнопки
    app.add_handler(MessageHandler(filters.Regex("^💰 Тарифы$"), price_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📌 Статус$"), status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^☎️ Контакт$"), contact_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📸 Фото машины$"), carphoto_cmd))
    app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd))

    # чат клиент ↔ водитель (всегда в конце, чтобы не мешать остальным хендлерам)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_chat))

    app.post_init = set_commands
        # умный ответ на обычные текстовые сообщения клиента (без /ai)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_client_text))
    return app


if __name__ == "__main__":
    app = build_app()
    log.info("Bot is starting…")
    app.run_polling(close_loop=False)