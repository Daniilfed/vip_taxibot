# -*- coding: utf-8 -*-
# VIP Taxi Bot — Google Sheets, бронирование заказов, AI-диспетчер, регистрация водителей с фото

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vip_taxi_bot")

BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ID группы водителей
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

assert BOT_TOKEN, "BOT_TOKEN is required"

# --------- Тарифы ---------
# Базовая почасовая ставка
HOURLY_PRICES = {
    "Maybach W223": 7000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
    "Minivan": 3000,
}

DISCOUNT_HOURS_FROM = 3      # скидка от 3-х часов
DISCOUNT_KOEF = 0.9          # -10%

# кэш заказов для работы с водителями
ORDERS_CACHE: dict[str, dict] = {}

# --------- Google Sheets ---------
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
sh = gc.open("orders")
sheet = sh.sheet1

# Столбцы:
# A: order_id
# B: user_id
# C: username
# D: pickup
# E: destination
# F: car_class
# G: time_text
# H: hours
# I: contact
# J: approx_price
# K: created_at
# L: status
# M: driver_id
# N: driver_name
# O: duration_min

# Водители в отдельном листе "drivers"
from gspread.exceptions import WorksheetNotFound

try:
    drivers_sheet = sh.worksheet("drivers")
except WorksheetNotFound:
    drivers_sheet = sh.add_worksheet(title="drivers", rows=200, cols=10)
    # шапка
    drivers_sheet.append_row(
        [
            "driver_id",
            "username",
            "full_name",
            "car_class",
            "car_number",
            "photo1_file_id",
            "photo2_file_id",
            "photo3_file_id",
            "created_at",
        ]
    )


def save_order_to_sheet(order: dict) -> None:
    """Запись заказа в Google Sheets."""
    try:
        sheet.append_row(
            [
                order.get("order_id"),
                order.get("user_id"),
                order.get("username"),
                order.get("pickup"),
                order.get("destination"),
                order.get("car_class"),
                order.get("time_text"),
                str(order.get("hours")),
                order.get("contact"),
                order.get("approx_price_text"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order.get("status", "new"),
                str(order.get("driver_id") or ""),
                order.get("driver_name") or "",
                str(order.get("duration_min") or ""),
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка Google Sheets (save_order_to_sheet): %s", e)


def find_order_row(order_id: str):
    """Найти строку заказа по order_id (в колонке A)."""
    try:
        values = sheet.col_values(1)
        for idx, v in enumerate(values, start=1):
            if v == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа: %s", e)
    return None


def update_order_status_in_sheet(order_id: str, status: str | None = None,
                                 driver_id=None, driver_name=None,
                                 duration_min: int | None = None):
    """Обновить статус / водителя / длительность заказа."""
    row = find_order_row(order_id)
    if not row:
        return
    try:
        if status is not None:
            sheet.update_cell(row, 12, status)
        # driver_id
        if driver_id is not None or driver_id == "":
            sheet.update_cell(row, 13, str(driver_id) if driver_id else "")
        # driver_name
        if driver_name is not None:
            sheet.update_cell(row, 14, driver_name or "")
        # duration
        if duration_min is not None:
            sheet.update_cell(row, 15, str(duration_min))
    except Exception as e:
        log.error("Ошибка обновления статуса заказа: %s", e)


# --------- работа с листом drivers ---------
def save_driver_profile(profile: dict) -> None:
    """Создать или обновить профиль водителя в листе drivers."""
    driver_id_str = str(profile.get("driver_id"))
    try:
        values = drivers_sheet.col_values(1)
        row_idx = None
        for idx, v in enumerate(values, start=1):
            if v == driver_id_str:
                row_idx = idx
                break

        row = [
            driver_id_str,
            profile.get("username") or "",
            profile.get("full_name") or "",
            profile.get("car_class") or "",
            profile.get("car_number") or "",
            profile.get("photo1_file_id") or "",
            profile.get("photo2_file_id") or "",
            profile.get("photo3_file_id") or "",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ]

        if row_idx:
            # Обновляем строку
            drivers_sheet.update(f"A{row_idx}:I{row_idx}", [row])
        else:
            drivers_sheet.append_row(row, value_input_option="USER_ENTERED")

        log.info("Профиль водителя сохранён/обновлён в Google Sheets")

    except Exception as e:
        log.error("Ошибка сохранения профиля водителя: %s", e)


def get_driver_profile(driver_id: int):
    """Вернуть профиль водителя по его Telegram ID из листа drivers."""
    driver_id_str = str(driver_id)
    try:
        values = drivers_sheet.get_all_values()
        # первая строка — заголовки
        for row in values[1:]:
            if not row:
                continue
            if row[0] == driver_id_str:
                return {
                    "driver_id": row[0],
                    "username": row[1],
                    "full_name": row[2],
                    "car_class": row[3],
                    "car_number": row[4],
                    "photo1_file_id": row[5],
                    "photo2_file_id": row[6],
                    "photo3_file_id": row[7],
                }
    except Exception as e:
        log.error("Ошибка чтения профиля водителя: %s", e)
    return None


# --------- состояния разговоров ---------
PICKUP, DEST, CAR, TIME, HOURS, CONTACT, CONFIRM = range(7)

DR_NAME, DR_CAR_NUM, DR_CAR_CLASS, DR_PHOTO1, DR_PHOTO2, DR_PHOTO3 = range(100, 106)


# --------- вспомогательные функции ---------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔔 Заказ", "💰 Тарифы"],
            ["📌 Статус", "☎️ Контакт"],
            ["📷 Фото машины"],
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


def driver_car_class_kb() -> ReplyKeyboardMarkup:
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
        ["5 часов", "5 часов и более"],
        ["❌ Отмена"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def price_overview_text() -> str:
    lines = ["<b>Тарифы (ориентировочно, почасовые):</b>"]
    for car, price in HOURLY_PRICES.items():
        lines.append(f"• {car}: от {price:,} ₽/ч".replace(",", " "))
    lines.append(
        "\nМинимальный заказ — 1 час.\n"
        "От 3 часов действует скидка.\n"
        "По аэропортам — фиксированная цена, не более стоимости 2-х часов аренды."
    )
    return "\n".join(lines)


def to_yandex_maps_link(lat: float, lon: float) -> str:
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"


def detect_airport(dest_text: str) -> str | None:
    if not dest_text:
        return None
    t = dest_text.lower()
    if "домодедово" in t:
        return "Домодедово"
    if "шереметьево" in t:
        return "Шереметьево"
    if "внуково" in t:
        return "Внуково"
    if "жуковский" in t:
        return "Жуковский"
    if "аэропорт" in t:
        return "Аэропорт"
    return None


def calc_price(car_class: str, hours: int, destination: str) -> tuple[int, str]:
    """Возвращает (сумма, человекочитаемый текст)."""
    hours = max(int(hours or 1), 1)
    rate = HOURLY_PRICES.get(car_class, 0)
    airport_name = detect_airport(destination)

    billable_hours = hours

    if airport_name:
        # аэропорт — не больше 2-х часов тарифа
        billable_hours = min(hours, 2)

    total = rate * billable_hours

    # скидка от 3 часов, только для обычных поездок
    if not airport_name and hours >= DISCOUNT_HOURS_FROM:
        total = int(total * DISCOUNT_KOEF)

    if airport_name:
        txt = f"≈ {total:,} ₽ (аэропорт {airport_name}, не более 2-х часов тарифа)".replace(",", " ")
    else:
        txt = f"≈ {total:,} ₽ за {hours} ч.".replace(",", " ")

    return total, txt


def parse_hours(text: str) -> int:
    text = text.lower().strip()
    for h in [1, 2, 3, 4, 5]:
        if str(h) in text:
            return h
    return 1


# --------- команды ---------
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
            BotCommand("ai", "AI-чат для диспетчера"),
            BotCommand("setdriver", "Регистрация/редактирование профиля водителя"),
            BotCommand("carphoto", "Показать фото машины (при назначенном водителе)"),
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Добро пожаловать в <b>{BRAND_NAME}</b>.\n"
        "Ваш комфорт — наш приоритет.\n\n"
        "Выберите действие в меню ниже или отправьте точку на карте — подача по вашей точке.",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(price_overview_text(), parse_mode=ParseMode.HTML)


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


# --------- AI-диспетчер ---------
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
            "AI-чат пока не настроен.\n"
            "Добавьте переменную окружения OPENAI_API_KEY в Railway с ключом OpenAI."
        )
        return

    try:
        import json as _json
        import urllib.request as _urlreq

        system_prompt = (
            "Ты — живой диспетчер премиум-такси (VIP taxi).\n"
            "Пиши ГОТОВЫЕ сообщения для клиента от лица сервиса.\n\n"
            "Правила:\n"
            "1) Обращайся к клиенту на ВЫ.\n"
            "2) Пиши вежливо, коротко: 1–3 предложения.\n"
            "3) Не упоминай, что ты бот или ИИ.\n"
            "4) Не придумывай точные цены, если их нет в запросе.\n"
            "5) В сложных ситуациях предлагай решение и сохраняй спокойный тон.\n"
            "6) Только текст для клиента, без технических подробностей."
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

        req = _urlreq.Request(
            "https://api.openai.com/v1/chat/completions",
            data=_json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        answer = data["choices"][0]["message"]["content"].strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.error("Ошибка AI-чата: %s", e)
        await update.message.reply_text(
            "Не удалось получить ответ от ИИ. Проверьте ключ OPENAI_API_KEY и интернет на сервере."
        )


# --------- оформление заказа ---------
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"] = {
        "order_id": uuid4().hex[:8],
        "user_id": update.effective_user.id,
        "username": f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name,
    }
    kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Отправить мою геолокацию", request_location=True)],
            ["❌ Отмена"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Укажите адрес подачи.\n\n"
        "Можно:\n"
        "• Нажать кнопку «📍 Отправить мою геолокацию» (если вы на точке подачи).\n"
        "• Или просто отправить адрес текстом.\n"
        "Также можно через скрепку 📎 → «Геопозиция» и выбрать точку на карте.",
        reply_markup=kb,
    )
    return PICKUP


async def pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    link = to_yandex_maps_link(loc.latitude, loc.longitude)
    context.user_data["order"]["pickup"] = link
    await update.message.reply_text(
        "Точка подачи сохранена.\nТеперь укажите адрес назначения (текстом или точкой на карте).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DEST


async def text_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text(
        "Укажите адрес назначения (можно текстом или точкой на карте).",
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
    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["time_text"] = update.message.text.strip()
    await update.message.reply_text(
        "На сколько часов нужна машина?\nМинимум 1 час. От 3 часов действует скидка.",
        reply_markup=hours_kb(),
    )
    return HOURS


async def hours_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    hours = parse_hours(update.message.text)
    context.user_data["order"]["hours"] = hours
    kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("Поделиться телефоном", request_contact=True)],
            ["❌ Отмена"],
        ],
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
    hours = int(o.get("hours", 1))
    _, price_text = calc_price(o.get("car_class"), hours, o.get("destination", ""))
    o["approx_price_text"] = price_text
    o["duration_min"] = None  # пока поездка не началась

    text = (
        "<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('destination')}\n"
        f"• Класс авто: {o.get('car_class')}\n"
        f"• Время подачи: {o.get('time_text')}\n"
        f"• Аренда: {hours} ч.\n"
        f"• Контакт: {o.get('contact')}\n"
        f"• Ориентировочно: {o.get('approx_price_text')}\n\n"
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


# --------- подтверждение заказа клиентом ---------
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

    await q.edit_message_text("Заказ принят. Водитель свяжется с вами.")

    # отправка в группу водителей
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID

    if admin_id:
        hours = order.get("hours")
        text_for_drivers = (
            f"🆕 Новый заказ #{order.get('order_id')}\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time_text')}\n"
            f"⏱ Аренда: {hours} ч.\n"
            f"💰 Ориентировочно: {order.get('approx_price_text')}\n\n"
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
            log.error("Не удалось отправить заказ в группу водителей: %s", e)

    context.user_data.clear()
    return ConversationHandler.END


# --------- кнопки водителей ---------
async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    driver = query.from_user

    global ORDERS_CACHE

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

        if order.get("status") in ("assigned", "ongoing", "arrived", "finished"):
            await query.answer("Этот заказ уже забрал другой водитель.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # проверяем профиль водителя и соответствие класса
        profile = get_driver_profile(driver.id)
        if not profile:
            await query.answer(
                "Вы ещё не зарегистрированы как водитель.\n"
                "Отправьте команду /setdriver в личку боту и заполните профиль.",
                show_alert=True,
            )
            return

        driver_class = profile.get("car_class")
        order_class = order.get("car_class")
        if driver_class != order_class:
            await query.answer(
                f"Этот заказ только для класса: {order_class}.\n"
                f"Ваш класс в профиле: {driver_class or 'не указан'}.",
                show_alert=True,
            )
            return

        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = driver.username or driver.full_name
        ORDERS_CACHE[order_id] = order

        update_order_status_in_sheet(
            order_id=order_id,
            status="assigned",
            driver_id=driver.id,
            driver_name=order["driver_name"],
        )

        try:
            await query.message.delete()
        except Exception:
            pass

        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time_text')}\n"
            f"⏱ Аренда: {order.get('hours')} ч.\n"
            f"💰 Ориентировочно: {order.get('approx_price_text')}\n\n"
            f"Личные данные клиента скрыты. Дальнейшие инструкции выдаст диспетчер."
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

        update_order_status_in_sheet(
            order_id=order_id,
            status="new",
            driver_id=None,
            driver_name=None,
        )

        try:
            await query.edit_message_text("Вы отменили заказ. Он возвращён в общий список.")
        except Exception:
            pass

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
                f"⏰ Время подачи: {order.get('time_text')}\n"
                f"⏱ Аренда: {order.get('hours')} ч.\n"
                f"💰 Ориентировочно: {order.get('approx_price_text')}\n\n"
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
                log.error("Не удалось вернуть заказ в группу водителей: %s", e)

    elif data.startswith("drv_arrived:"):
        from time import time as now_ts
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

        order["status"] = "ongoing"
        order["ride_start_ts"] = now_ts()
        ORDERS_CACHE[order_id] = order

        update_order_status_in_sheet(
            order_id=order_id,
            status="ongoing",
            driver_id=order.get("driver_id"),
            driver_name=order.get("driver_name"),
        )

        client_id = order.get("user_id")
        if client_id:
            keyboard_client = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Завершить поездку", callback_data=f"finish_order:{order_id}"
                        )
                    ]
                ]
            )
            try:
                await context.bot.send_message(
                    chat_id=int(client_id),
                    text=(
                        "🚗 Ваш водитель на месте.\n"
                        "После окончания поездки можно нажать «Завершить поездку»."
                    ),
                    reply_markup=keyboard_client,
                )
            except Exception as e:
                log.error("Не смог отправить сообщение клиенту: %s", e)

        keyboard_driver = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Завершить поездку", callback_data=f"finish_order:{order_id}"
                    )
                ]
            ]
        )
        try:
            await query.edit_message_text(
                "Отметили: вы на месте. Таймер запущен.\n"
                "После окончания поездки нажмите «Завершить поездку».",
                reply_markup=keyboard_driver,
            )
        except Exception:
            pass


# --------- завершение поездки (кнопка клиента/водителя) ---------
async def finish_order_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from time import time as now_ts

    query = update.callback_query
    await query.answer()
    data = query.data
    order_id = data.split(":", 1)[1]

    order = ORDERS_CACHE.get(order_id)
    if not order:
        await query.answer("Заказ не найден.", show_alert=True)
        return

    if order.get("status") == "finished":
        await query.edit_message_text(
            f"Поездка по заказу #{order_id} уже завершена."
        )
        return

    start_ts = order.get("ride_start_ts")
    if start_ts:
        duration_min = int((now_ts() - start_ts) / 60) or 1
    else:
        duration_min = 0

    order["status"] = "finished"
    order["duration_min"] = duration_min
    ORDERS_CACHE[order_id] = order

    update_order_status_in_sheet(
        order_id=order_id,
        status="finished",
        driver_id=order.get("driver_id"),
        driver_name=order.get("driver_name"),
        duration_min=duration_min,
    )

    msg = (
        f"Поездка по заказу #{order_id} завершена.\n"
        f"Время в пути: {duration_min} мин."
    )

    client_id = order.get("user_id")
    driver_id = order.get("driver_id")

    # уведомляем обоих
    if client_id:
        try:
            await context.bot.send_message(chat_id=int(client_id), text=msg)
        except Exception as e:
            log.error("Не смог отправить сообщение клиенту при завершении: %s", e)

    if driver_id:
        try:
            await context.bot.send_message(chat_id=int(driver_id), text=msg)
        except Exception as e:
            log.error("Не смог отправить сообщение водителю при завершении: %s", e)

    try:
        await query.edit_message_text(msg)
    except Exception:
        pass


# --------- регистрация водителя с фото ---------
async def setdriver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запуск регистрации/редактирования профиля водителя."""
    user = update.effective_user
    context.user_data["driver_reg"] = {
        "driver_id": user.id,
        "username": f"@{user.username}" if user.username else "",
        "full_name": user.full_name,
    }
    await update.message.reply_text(
        "Регистрация водителя.\n\n"
        "1️⃣ Напишите, как вас показывать клиенту (имя/имя и фамилия).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DR_NAME


async def dr_name_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    context.user_data["driver_reg"]["full_name"] = name
    await update.message.reply_text(
        "2️⃣ Укажите госномер автомобиля (например: А777АА777).",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DR_CAR_NUM


async def dr_car_num_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car_number = update.message.text.strip()
    context.user_data["driver_reg"]["car_number"] = car_number
    await update.message.reply_text(
        "3️⃣ Выберите класс автомобиля.",
        reply_markup=driver_car_class_kb(),
    )
    return DR_CAR_CLASS


async def dr_car_class_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car_class = update.message.text.strip()
    if car_class not in HOURLY_PRICES:
        await update.message.reply_text(
            "Пожалуйста, выберите класс кнопкой снизу.",
            reply_markup=driver_car_class_kb(),
        )
        return DR_CAR_CLASS
    context.user_data["driver_reg"]["car_class"] = car_class
    await update.message.reply_text(
        "4️⃣ Отправьте фото автомобиля спереди.",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return DR_PHOTO1


async def dr_photo1_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно отправить именно фото, а не файл. Попробуйте ещё раз.")
        return DR_PHOTO1
    file_id = update.message.photo[-1].file_id
    context.user_data["driver_reg"]["photo1_file_id"] = file_id
    await update.message.reply_text("5️⃣ Теперь отправьте фото автомобиля сбоку.")
    return DR_PHOTO2


async def dr_photo2_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно отправить именно фото. Попробуйте ещё раз.")
        return DR_PHOTO2
    file_id = update.message.photo[-1].file_id
    context.user_data["driver_reg"]["photo2_file_id"] = file_id
    await update.message.reply_text("6️⃣ И последнее — фото салона.")
    return DR_PHOTO3


async def dr_photo3_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно отправить именно фото. Попробуйте ещё раз.")
        return DR_PHOTO3
    file_id = update.message.photo[-1].file_id
    reg = context.user_data.get("driver_reg", {})
    reg["photo3_file_id"] = file_id

    save_driver_profile(reg)

    await update.message.reply_text(
        "Профиль водителя сохранён.\n"
        "Теперь при назначении вас на заказ клиент сможет увидеть фото автомобиля.",
        reply_markup=main_menu_kb(),
    )
    context.user_data.pop("driver_reg", None)
    return ConversationHandler.END


# --------- фото машины для клиента ---------
async def car_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать клиенту фото машины по его активному заказу (если есть водитель с фото)."""
    user_id = update.effective_user.id

    # ищем заказ этого пользователя с назначенным водителем
    current_order = None
    for o in ORDERS_CACHE.values():
        if o.get("user_id") == user_id and o.get("driver_id"):
            current_order = o

    if not current_order:
        await update.message.reply_text(
            "Фото машины будет доступно после назначения водителя на ваш заказ."
        )
        return

    driver_id = current_order.get("driver_id")
    profile = get_driver_profile(driver_id)
    if not profile:
        await update.message.reply_text(
            "Водитель ещё не загрузил фото автомобиля. Попросите диспетчера уточнить."
        )
        return

    text_header = (
        "Ваш водитель:\n"
        f"🧑‍✈️ {profile.get('full_name') or profile.get('username')}\n"
        f"🚘 {profile.get('car_class')}\n"
        f"🔢 Номер авто: {profile.get('car_number')}"
    )
    await update.message.reply_text(text_header)

    photos_ids = [
        profile.get("photo1_file_id"),
        profile.get("photo2_file_id"),
        profile.get("photo3_file_id"),
    ]
    for pid in photos_ids:
        if pid:
            try:
                await update.message.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=pid,
                )
            except Exception as e:
                log.error("Не удалось отправить фото машины: %s", e)


# --------- роутинг ---------
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
    app.add_handler(CommandHandler("carphoto", car_photo_cmd))

    # разговор по заказу
    conv_order = ConversationHandler(
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
        ],
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv_order)

    # разговор по регистрации водителя
    conv_driver = ConversationHandler(
        entry_points=[CommandHandler("setdriver", setdriver_start)],
        states={
            DR_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dr_name_set),
            ],
            DR_CAR_NUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dr_car_num_set),
            ],
            DR_CAR_CLASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dr_car_class_set),
            ],
            DR_PHOTO1: [
                MessageHandler(filters.PHOTO, dr_photo1_set),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dr_photo1_set),
            ],
            DR_PHOTO2: [
                MessageHandler(filters.PHOTO, dr_photo2_set),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dr_photo2_set),
            ],
            DR_PHOTO3: [
                MessageHandler(filters.PHOTO, dr_photo3_set),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dr_photo3_set),
            ],
        ],
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv_driver)

    # колбэки для водителей и завершения заказа
    app.add_handler(CallbackQueryHandler(driver_orders_callback, pattern=r"^drv_"))
    app.add_handler(CallbackQueryHandler(finish_order_cb, pattern=r"^finish_order:"))

    # Кнопки меню
    app.add_handler(MessageHandler(filters.Regex("^💰 Тарифы$"), price_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📌 Статус$"), status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^☎️ Контакт$"), contact_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📷 Фото машины$"), car_photo_cmd))
    app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd))

    app.post_init = set_commands
    return app


if __name__ == "__main__":
    app = build_app()
    log.info("Bot is starting…")
    app.run_polling(close_loop=False)