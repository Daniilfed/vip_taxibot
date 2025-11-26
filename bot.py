# -*- coding: utf-8 -*-
# VIP Taxi Bot — заказы, водители, Google Sheets, чат клиент-водитель + анкета /reg_driver

import os
import json
import logging
import re
from uuid import uuid4
from datetime import datetime, timedelta
from typing import Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

import requests
from google.oauth2.service_account import Credentials
import gspread

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ---------- НАСТРОЙКИ ----------
BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # группа водителей (заказы)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SHEET_ID = os.environ.get("SHEET_ID")

# ID секретной группы, куда будут прилетать анкеты водителей
DRIVER_REG_CHAT_ID = int(os.environ.get("DRIVER_REG_CHAT_ID", "-5062249297"))

assert BOT_TOKEN, "BOT_TOKEN is required"
assert SHEET_ID, "SHEET_ID is required"

# тарифы (почасовые, минимум 1 час)
PRICES = {
    "Maybach W223": 7000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
    "Minivan": 3000,
}

# аэропорты: фиксированно считаем как 2 часа аренды
AIRPORT_KEYWORDS = {
    "sheremetyevo": ["шереметьево", "svo"],
    "domodedovo": ["домодедово", "dme"],
    "vnukovo": ["внуково", "vko"],
}

# кэш заказов в памяти
ORDERS_CACHE: dict[str, dict] = {}  # order_id -> dict
ACTIVE_CHATS: dict[int, str] = {}   # user_id -> order_id (и для клиента, и для водителя)

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
spreadsheet = gc.open_by_key(SHEET_ID)
ORDERS_SHEET = spreadsheet.worksheet("Лист1")
DRIVERS_SHEET = spreadsheet.worksheet("drivers")


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДАТЫ/ВРЕМЕНИ ----------

def normalize_time_text(text: str) -> str:
    """
    Преобразует фразы типа «завтра в 10», «сегодня 19:30», «в пятницу в 8»
    в формат 'ДД.MM.ГГГГ ЧЧ:ММ'. Если не смогли распарсить — отдаём исходный текст.
    """
    try:
        t = text.lower().strip()
        now = datetime.now()
        target_date = None

        # относительные дни
        if "послезавтра" in t:
            target_date = now.date() + timedelta(days=2)
        elif "завтра" in t:
            target_date = now.date() + timedelta(days=1)
        elif "сегодня" in t:
            target_date = now.date()

        # дни недели
        if target_date is None:
            weekdays = {
                "понедельник": 0,
                "вторник": 1,
                "среду": 2,
                "среда": 2,
                "четверг": 3,
                "пятницу": 4,
                "пятница": 4,
                "субботу": 5,
                "суббота": 5,
                "воскресенье": 6,
            }
            for word, idx in weekdays.items():
                if word in t:
                    current_idx = now.weekday()
                    delta = (idx - current_idx) % 7
                    if delta == 0:
                        delta = 7
                    target_date = now.date() + timedelta(days=delta)
                    break

        # явная дата ДД.ММ(.ГГ)
        if target_date is None:
            m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", t)
            if m:
                day = int(m.group(1))
                month = int(m.group(2))
                year = now.year
                if m.group(3):
                    year = int(m.group(3))
                    if year < 100:
                        year += 2000
                try:
                    target_date = datetime(year, month, day).date()
                except ValueError:
                    target_date = now.date()

        if target_date is None:
            target_date = now.date()

        # время
        m = re.search(r"(\d{1,2})[:.](\d{2})", t)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
        else:
            m = re.search(r"\b(\d{1,2})\b", t)
            if m:
                hour = int(m.group(1))
                minute = 0
            else:
                hour = now.hour
                minute = now.minute

        dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception as e:
        log.error("Ошибка нормализации времени '%s': %s", text, e)
        return text


def detect_airport(text: str | None) -> str | None:
    """Определяем, есть ли в строке аэропорт."""
    if not text:
        return None
    t = text.lower()
    for code, words in AIRPORT_KEYWORDS.items():
        for w in words:
            if w in t:
                return code
    return None


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ GOOGLE SHEETS ----------

def save_order_to_sheet(order: dict) -> None:
    """Записать новый заказ в Лист1."""
    try:
        ORDERS_SHEET.append_row(
            [
                order.get("order_id"),
                order.get("user_id"),
                order.get("username"),
                order.get("pickup"),
                order.get("destination", ""),
                order.get("car_class"),
                order.get("time"),
                order.get("hours_text"),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order.get("status", "new"),
                order.get("driver_id") or "",
                order.get("driver_name") or "",
                "",  # arrived_at
                "",  # finished_at
                "",  # duration_min
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка записи заказа в таблицу: %s", e)


def find_order_row(order_id: str):
    """Найти номер строки заказа по order_id."""
    try:
        col = ORDERS_SHEET.col_values(1)
        for idx, v in enumerate(col, start=1):
            if v == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа: %s", e)
    return None


def update_order_driver_and_status(order_id: str, status: str, driver_id=None, driver_name=None):
    """Обновить статус и водителя у заказа."""
    row = find_order_row(order_id)
    if not row:
        return
    try:
        ORDERS_SHEET.update_cell(row, 12, status)  # status
        ORDERS_SHEET.update_cell(row, 13, str(driver_id) if driver_id else "")
        ORDERS_SHEET.update_cell(row, 14, driver_name or "")
    except Exception as e:
        log.error("Ошибка обновления статуса заказа: %s", e)


def update_order_arrived(order_id: str, arrived_at: datetime):
    row = find_order_row(order_id)
    if not row:
        return
    try:
        ORDERS_SHEET.update_cell(row, 15, arrived_at.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        log.error("Ошибка записи arrived_at: %s", e)


def update_order_finished(order_id: str, arrived_at: datetime | None, finished_at: datetime):
    row = find_order_row(order_id)
    if not row:
        return
    try:
        ORDERS_SHEET.update_cell(row, 16, finished_at.strftime("%Y-%m-%d %H:%M:%S"))
        if arrived_at:
            duration_min = int((finished_at - arrived_at).total_seconds() // 60)
            ORDERS_SHEET.update_cell(row, 17, duration_min)
    except Exception as e:
        log.error("Ошибка записи finished_at/duration: %s", e)


def find_driver_row(driver_id: int):
    """Найти строку водителя по driver_id в листе drivers."""
    try:
        col = DRIVERS_SHEET.col_values(1)
        for idx, v in enumerate(col, start=1):
            if v and str(v) == str(driver_id):
                return idx
    except Exception as e:
        log.error("Ошибка поиска водителя: %s", e)
    return None


def get_driver_info(driver_id: int) -> dict | None:
    """Считать данные водителя из листа drivers.

    Формат строк:
    A: driver_id
    B: driver_name
    C: car_class
    D: plate
    E: car_photo_file_ids (через |, до 3 штук)
    F: rating
    G: last_lat
    H: last_lon
    I: last_update
    """
    row = find_driver_row(driver_id)
    if not row:
        return None
    try:
        values = DRIVERS_SHEET.row_values(row)
        while len(values) < 9:
            values.append("")
        photos_raw = values[4] or ""
        car_photos = [p for p in photos_raw.split("|") if p.strip()]
        return {
            "driver_id": values[0],
            "driver_name": values[1],
            "car_class": values[2],
            "plate": values[3],
            "car_photos": car_photos,
            "rating": values[5],
            "last_lat": values[6],
            "last_lon": values[7],
            "last_update": values[8],
        }
    except Exception as e:
        log.error("Ошибка чтения данных водителя: %s", e)
        return None


def upsert_driver(driver_id: int, driver_name: str, car_class: str, plate: str, photo_file_ids: list[str]):
    """Создать/обновить запись водителя. Фото храним как 'id1|id2|id3' в одной ячейке."""
    photos_str = "|".join(photo_file_ids) if photo_file_ids else ""
    row = find_driver_row(driver_id)
    try:
        if row:
            DRIVERS_SHEET.update(
                f"A{row}:E{row}",
                [[str(driver_id), driver_name, car_class, plate, photos_str]],
            )
        else:
            DRIVERS_SHEET.append_row(
                [str(driver_id), driver_name, car_class, plate, photos_str, "", "", "", ""],
                value_input_option="USER_ENTERED",
            )
        log.info("Водитель %s обновлён/добавлен", driver_id)
    except Exception as e:
        log.error("Ошибка записи водителя: %s", e)


# ---------- КОНСТАНТЫ СОСТОЯНИЙ ----------

PICKUP, DEST, CAR, TIME, HOURS, CONTACT, CONFIRM = range(7)
DRV_CLASS, DRV_PLATE, DRV_PHOTO = range(10, 13)

# состояния анкеты /reg_driver (отдельно, чтобы не пересекались)
REG_NAME, REG_PHONE, REG_CAR, REG_DOCS, REG_CONFIRM = range(20, 25)


# ---------- КНОПКИ ----------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔔 Заказ", "⚡ Срочный заказ"],
            ["💰 Тарифы", "📌 Статус"],
            ["☎️ Контакт", "📸 Фото машины"],
            ["👨‍✈️ Стать водителем"],
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


def hours_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["1 час", "2 часа"],
            ["3 часа", "4 часа"],
            ["5 часов и более"],
            ["❌ Отмена"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def to_ymaps_link(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"


def format_price(car_class: str, hours: int) -> str:
    base = PRICES.get(car_class, 0)
    total = base * max(1, hours)
    return f"≈ {total:,.0f} ₽ за {hours} ч.".replace(",", " ")


# ---------- ОБЩИЕ КОМАНДЫ ----------

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
            BotCommand("carphoto", "Фото назначенной машины"),
            BotCommand("cancel", "Отмена"),
            BotCommand("ai", "AI-чат для диспетчера"),
            BotCommand("setdriver", "Тех.регистрация авто водителя"),
            BotCommand("reg_driver", "Анкета на подключение водителя"),
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
    text = (update.message.text or "").strip()
    if "Стать водителем" in text:
        return await reg_driver_start(update, context)
    await start(update, context)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["<b>Тарифы (ориентировочно, почасовые):</b>"]
    for k, v in PRICES.items():
        lines.append(f"• {k}: от {v:,.0f} ₽/ч".replace(",", " "))
    lines.append("\nМинимум 1 час. Точная стоимость зависит от маршрута, времени и загрузки.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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


# ---------- AI /ai ----------

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
            "AI-чат пока не настроен. Добавьте OPENAI_API_KEY в переменные Railway."
        )
        return

    system_prompt = (
        "Ты — живой диспетчер премиум-такси. "
        "На вход получаешь описание ситуации, на выход даёшь ГОТОВОЕ письмо клиенту.\n"
        "Правила:\n"
        "1) Всегда обращайся на ВЫ.\n"
        "2) Пиши 1–3 коротких предложения.\n"
        "3) Не придумывай точные цены, если их нет в запросе.\n"
        "4) Не упоминай, что ты ИИ.\n"
        "5) Будь вежлив, спокойный, уверенный.\n"
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
        await update.message.reply_text("Не удалось получить ответ от AI-диспетчера.")


# ---------- ТЕХ. РЕГИСТРАЦИЯ ВОДИТЕЛЯ /setdriver (авто и фото для заказов) ----------

async def setdriver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    context.user_data["driver"] = {
        "driver_id": user.id,
        "driver_name": user.username or user.full_name,
        "photos": [],
    }
    await update.message.reply_text(
        "Регистрация/обновление водителя.\n\n"
        "Выберите класс авто, на котором вы работаете:",
        reply_markup=cars_kb(),
    )
    return DRV_CLASS


async def setdriver_class(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car_class = update.message.text.strip()
    if car_class not in PRICES:
        await update.message.reply_text("Пожалуйста, выберите класс кнопкой.", reply_markup=cars_kb())
        return DRV_CLASS
    context.user_data["driver"]["car_class"] = car_class
    await update.message.reply_text(
        "Укажите номер авто (например: A777AA77):",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DRV_PLATE


async def setdriver_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["driver"]["plate"] = update.message.text.strip()
    await update.message.reply_text(
        "Отправьте <b>1–3 фото вашей машины</b> (можно по очереди).\n"
        "После последнего фото напишите «Готово».",
        reply_markup=ReplyKeyboardMarkup([["Готово", "❌ Отмена"]], resize_keyboard=True),
        parse_mode=ParseMode.HTML,
    )
    return DRV_PHOTO


async def finish_driver_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data["driver"]
    photos = d.get("photos") or []
    if not photos:
        await update.message.reply_text("Сначала отправьте хотя бы одно фото машины.")
        return DRV_PHOTO

    upsert_driver(
        driver_id=d["driver_id"],
        driver_name=d["driver_name"],
        car_class=d["car_class"],
        plate=d["plate"],
        photo_file_ids=photos,
    )

    await update.message.reply_text(
        "Данные водителя сохранены.\n"
        f"Класс: {d['car_class']}\n"
        f"Номер авто: {d['plate']}\n"
        f"Фото: {len(photos)} шт.\n"
        "Теперь вы сможете брать заказы только по своему классу.",
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop("driver", None)
    return ConversationHandler.END


async def setdriver_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data.get("driver") or {}
    photos: list[str] = d.setdefault("photos", [])

    # Фото
    if update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        if file_id not in photos:
            photos.append(file_id)

        if len(photos) < 3:
            await update.message.reply_text(
                f"Фото сохранено ({len(photos)}/3).\n"
                "Можете отправить ещё фото или написать «Готово».",
            )
            return DRV_PHOTO
        else:
            # Уже 3 фото — завершаем автоматически
            return await finish_driver_registration(update, context)

    # Текст
    text = (update.message.text or "").lower().strip()
    if text.startswith("готов"):
        return await finish_driver_registration(update, context)

    await update.message.reply_text(
        "Отправьте фото машины или напишите «Готово», когда закончите."
    )
    return DRV_PHOTO


# ---------- АНКЕТА /reg_driver (ФИО, телефон, авто, документы) ----------

def _normalize_phone(text: str) -> Optional[str]:
    """Приводим номер к виду +7ХХХХХХХХХ."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) < 10:
        return None
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return "+" + digits


async def reg_driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["driver_reg"] = {"photos": []}

    await update.message.reply_text(
        "👋 Добро пожаловать в регистрацию водителей VIP taxi.\n\n"
        "1️⃣ Напишите *ФИО полностью*:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_NAME


async def reg_driver_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_name = (update.message.text or "").strip()
    if not full_name:
        await update.message.reply_text("Пожалуйста, введите ваше ФИО текстом.")
        return REG_NAME

    context.user_data["driver_reg"]["full_name"] = full_name

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "2️⃣ Теперь отправьте номер телефона в формате +7...\n\n"
        "Можно нажать кнопку ниже, чтобы отправить номер автоматически.",
        reply_markup=kb,
    )
    return REG_PHONE


async def reg_driver_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone_raw = None

    if update.message.contact:
        phone_raw = update.message.contact.phone_number
    elif update.message.text:
        phone_raw = update.message.text.strip()

    phone_norm = _normalize_phone(phone_raw or "")
    if not phone_norm:
        await update.message.reply_text(
            "Похоже, номер некорректный. Отправьте номер ещё раз в формате +7…"
        )
        return REG_PHONE

    context.user_data["driver_reg"]["phone"] = phone_norm

    await update.message.reply_text(
        "3️⃣ Напишите данные вашего авто:\n"
        "Марка, модель, год, цвет, госномер.\n\n"
        "Например:\n"
        "Mercedes-Benz S 350d, 2021, чёрный, А123ВС777",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_CAR


async def reg_driver_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car_info = (update.message.text or "").strip()
    if not car_info:
        await update.message.reply_text("Пожалуйста, опишите автомобиль текстом.")
        return REG_CAR

    context.user_data["driver_reg"]["car"] = car_info
    context.user_data["driver_reg"]["photos"] = []

    kb = ReplyKeyboardMarkup(
        [["Готово"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    await update.message.reply_text(
        "4️⃣ Отправьте фото документов и авто:\n"
        "• водительское удостоверение (обе стороны)\n"
        "• автомобиль (вид спереди/сбоку)\n\n"
        "Можно отправить несколько фото подряд.\n"
        "Когда закончите — нажмите «Готово».",
        reply_markup=kb,
    )
    return REG_DOCS


async def reg_driver_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Нажали "Готово"
    if update.message.text and update.message.text.lower() == "готово":
        reg = context.user_data.get("driver_reg", {})
        if not reg.get("photos"):
            await update.message.reply_text(
                "Вы ещё не отправили ни одной фотографии.\n"
                "Пожалуйста, отправьте хотя бы одно фото."
            )
            return REG_DOCS

        summary = (
            "Проверьте, пожалуйста, данные:\n\n"
            f"👤 ФИО: *{reg.get('full_name', '-') }*\n"
            f"📱 Телефон: *{reg.get('phone', '-') }*\n"
            f"🚘 Авто: *{reg.get('car', '-') }*\n"
            f"📸 Фото: *{len(reg.get('photos', []))}* шт.\n\n"
            "Отправить заявку на проверку?"
        )

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Отправить", callback_data="drv_reg_send"),
                    InlineKeyboardButton("❌ Отмена", callback_data="drv_reg_cancel"),
                ]
            ]
        )

        await update.message.reply_text(
            summary,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return REG_CONFIRM

    # Пришло фото
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        context.user_data["driver_reg"]["photos"].append(file_id)

        await update.message.reply_text(
            f"Фото добавлено ✅ (всего: {len(context.user_data['driver_reg']['photos'])}).\n"
            "Можете отправить ещё или нажмите «Готово».",
        )
        return REG_DOCS

    await update.message.reply_text(
        "Отправьте фото или нажмите «Готово», когда закончите."
    )
    return REG_DOCS


async def reg_driver_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "drv_reg_cancel":
        await query.edit_message_text("❌ Регистрация отменена.")
        context.user_data.pop("driver_reg", None)
        return ConversationHandler.END

    if data != "drv_reg_send":
        return REG_CONFIRM

    reg = context.user_data.get("driver_reg", {})
    user = query.from_user

    text = (
        "🆕 <b>Новая заявка водителя</b>\n\n"
        f"👤 ФИО: <b>{reg.get('full_name', '-') }</b>\n"
        f"📱 Телефон: <b>{reg.get('phone', '-') }</b>\n"
        f"🚘 Авто: <b>{reg.get('car', '-') }</b>\n\n"
        f"👤 Telegram: {user.mention_html()}\n"
        f"🆔 ID: <code>{user.id}</code>"
    )

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"drv_app_{user.id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"drv_rej_{user.id}"),
            ]
        ]
    )

    # Текст в секретную группу
    await context.bot.send_message(
        chat_id=DRIVER_REG_CHAT_ID,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
    )

    # Фото
    for file_id in reg.get("photos", []):
        await context.bot.send_photo(
            chat_id=DRIVER_REG_CHAT_ID,
            photo=file_id,
            caption=f"Документы/авто водителя ID {user.id}",
        )

    await query.edit_message_text(
        "✅ Ваша заявка отправлена на проверку.\n"
        "Мы свяжемся с вами после рассмотрения."
    )

    context.user_data.pop("driver_reg", None)
    return ConversationHandler.END


async def driver_moderation_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Одобрение/отклонение заявки в группе DRIVER_REG_CHAT_ID."""
    query = update.callback_query
    await query.answer()
    data = query.data

    # Одобрение
    if data.startswith("drv_app_"):
        user_id = int(data.split("_")[-1])

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🎉 Ваша заявка на регистрацию в VIP taxi *одобрена*.\n"
                     "Мы свяжемся с вами для дальнейших шагов.",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"Не удалось написать пользователю {user_id}: {e}")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(query.message.text + "\n\n✅ Водитель ОДОБРЕН.")
        return

    # Отказ
    if data.startswith("drv_rej_"):
        user_id = int(data.split("_")[-1])

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Ваша заявка на регистрацию в VIP taxi *отклонена*.\n"
                     "Вы можете отправить новую заявку позже.",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"Не удалось написать пользователю {user_id}: {e}")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(query.message.text + "\n\n❌ Водитель ОТКЛОНЁН.")
        return


# ---------- ЗАКАЗ (обычный) ----------

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    o = {
        "order_id": uuid4().hex[:8],
        "user_id": update.effective_user.id,
        "username": f"@{update.effective_user.username}"
        if update.effective_user.username
        else update.effective_user.full_name,
        "urgent": False,
    }
    context.user_data["order"] = o

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить мою геолокацию", request_location=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Укажите адрес подачи или отправьте геолокацию кнопкой ниже.",
        reply_markup=kb,
    )
    return PICKUP


async def pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    link = to_ymaps_link(loc.latitude, loc.longitude)
    context.user_data["order"]["pickup"] = link
    await update.message.reply_text(
        "Укажите адрес назначения.",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def text_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text(
        "Укажите адрес назначения.",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def dest_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    context.user_data["order"]["destination"] = to_ymaps_link(loc.latitude, loc.longitude)
    await update.message.reply_text("Выберите класс авто.", reply_markup=cars_kb())
    return CAR


async def text_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["destination"] = update.message.text.strip()
    await update.message.reply_text("Выберите класс авто.", reply_markup=cars_kb())
    return CAR


async def car_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car = update.message.text.strip()
    if car not in PRICES:
        await update.message.reply_text("Пожалуйста, выберите класс кнопкой.", reply_markup=cars_kb())
        return CAR
    order = context.user_data["order"]
    order["car_class"] = car

    # если срочный заказ — не спрашиваем время и часы
    if order.get("urgent"):
        order["time"] = "Срочно (как можно быстрее)"
        order["hours"] = 1
        order["hours_text"] = "1 ч. (срочный)"
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Оставьте контакт (имя и телефон), или поделитесь номером:",
            reply_markup=kb,
        )
        return CONTACT

    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    norm = normalize_time_text(raw)
    context.user_data["order"]["time"] = norm
    context.user_data["order"]["time_raw"] = raw

    await update.message.reply_text(
        "На сколько часов нужна машина? Минимум 1 час. От 3 часов действует скидка.",
        reply_markup=hours_kb(),
    )
    return HOURS


async def hours_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "час" not in text:
        await update.message.reply_text("Выберите вариант кнопкой.", reply_markup=hours_kb())
        return HOURS
    if text.startswith("5"):
        hours = 5
    else:
        try:
            hours = int(text.split()[0])
        except Exception:
            hours = 1
    context.user_data["order"]["hours"] = hours
    context.user_data["order"]["hours_text"] = f"{hours} ч."
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Оставьте контакт (имя и телефон), или поделитесь номером:",
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

    # Аэропорт?
    airport_from = detect_airport(o.get("pickup"))
    airport_to = detect_airport(o.get("destination"))
    airport = airport_from or airport_to

    if airport:
        # фикс как 2 часа аренды
        o["hours"] = 2
        o["hours_text"] = "2 ч. (аэропорт)"
        base = PRICES.get(o["car_class"], 0)
        total = base * 2
        approx = f"≈ {total:,.0f} ₽ за поездку (аэропорт, до 2 ч.)".replace(",", " ")
    else:
        hours = o.get("hours", 1)
        approx = format_price(o["car_class"], hours)

    o["approx_price"] = approx

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

    ORDERS_CACHE[order["order_id"]] = {
        **order,
        "status": "new",
        "driver_id": None,
        "driver_name": None,
        "arrived_at": None,
    }

    await q.edit_message_text("Заказ принят. Как только назначим водителя — бот пришлёт уведомление.")

    # отправляем в группу водителей
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID

    if admin_id:
        text_for_drivers = (
            f"🆕 Новый заказ #{order['order_id']}\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours_text')}\n"
            f"💰 {order.get('approx_price')}\n\n"
            "Личные данные клиента скрыты."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 Взять заказ", callback_data=f"drv_take:{order['order_id']}"
                    )
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=admin_id,
            text=text_for_drivers,
            reply_markup=keyboard,
        )

    context.user_data.clear()
    return ConversationHandler.END


# ---------- СРОЧНЫЙ ЗАКАЗ ----------

async def urgent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    o = {
        "order_id": uuid4().hex[:8],
        "user_id": update.effective_user.id,
        "username": f"@{update.effective_user.username}"
        if update.effective_user.username
        else update.effective_user.full_name,
        "urgent": True,
    }
    context.user_data["order"] = o
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить мою геолокацию", request_location=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Срочный заказ.\nОтправьте геолокацию точки подачи или введите адрес.",
        reply_markup=kb,
    )
    return PICKUP


# ---------- КНОПКИ ВОДИТЕЛЕЙ ПО ЗАКАЗАМ ----------

async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    driver = query.from_user

    # Взять заказ
    if data.startswith("drv_take:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)

        if not order:
            await query.answer("Этот заказ уже не активен или не найден.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if order.get("status") in ("assigned", "on_place", "finished"):
            await query.answer("Этот заказ уже забрал другой водитель.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # проверяем, зарегистрирован ли водитель
        info = get_driver_info(driver.id)
        if not info:
            await query.answer(
                "Вы ещё не зарегистрированы как водитель.\n"
                "Откройте личный чат с ботом и выполните /setdriver.",
                show_alert=True,
            )
            return

        # проверяем класс авто
        required_class = order.get("car_class")
        if info["car_class"] != required_class:
            await query.answer(
                f"Этот заказ только для класса {required_class}. "
                f"У вас в профиле указан {info['car_class']}.",
                show_alert=True,
            )
            return

        # обновляем статус
        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = info["driver_name"] or driver.username or driver.full_name
        ORDERS_CACHE[order_id] = order
        update_order_driver_and_status(
            order_id=order_id,
            status="assigned",
            driver_id=driver.id,
            driver_name=order["driver_name"],
        )

        # удаляем сообщение из группы
        try:
            await query.message.delete()
        except Exception:
            pass

        # DM водителю
        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination') or 'Не указано (срочный)'}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours_text')}\n\n"
            "После прибытия нажмите «На месте», затем по окончании — «Завершить поездку»."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🅿 На месте", callback_data=f"drv_arrived:{order_id}"),
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

        # уведомление клиенту, что водитель назначен
        client_id = order.get("user_id")
        if client_id:
            text_client = (
                "Ваш заказ принят в работу.\n\n"
                f"Ваш водитель:\n"
                f"👨‍✈️ {order['driver_name']}\n"
                f"🚘 {info['car_class']}\n"
                f"🧾 Номер авто: {info['plate'] or '—'}\n\n"
                "Как только водитель будет на месте — вы получите уведомление.\n"
                "Фото машины можно запросить командой /carphoto или кнопкой «Фото машины»."
            )
            try:
                await context.bot.send_message(chat_id=int(client_id), text=text_client)
            except Exception as e:
                log.error("Не удалось отправить уведомление клиенту: %s", e)

        # активируем чат
        ACTIVE_CHATS[driver.id] = order_id
        if client_id:
            ACTIVE_CHATS[int(client_id)] = order_id

    # Отмена заказа водителем
    elif data.startswith("drv_cancel:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        if order.get("driver_id") != driver.id:
            await query.answer("Отменить может только водитель, принявший заказ.", show_alert=True)
            return

        order["status"] = "new"
        order["driver_id"] = None
        order["driver_name"] = None
        ORDERS_CACHE[order_id] = order

        update_order_driver_and_status(order_id, "new", None, None)

        try:
            await query.edit_message_text("Вы отменили заказ. Он возвращён в общий список.")
        except Exception:
            pass
                # возвращаем заказ в группу
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID
            



       