# -*- coding: utf-8 -*-
# VIP Taxi Bot — расширенная версия:
# - Google Sheets (заказы + статусы + время)
# - бронирование заказов водителями
# - Яндекс.Карты
# - часы аренды + коэффициент 2 для аэропортов
# - скидка от 3 часов
# - AI-диспетчер /ai
# - старт/стоп поездки (длительность)
# - профиль водителя (класс, номер, фото)
# - кнопка "Фото машины"
# - /orders — список последних заказов для диспетчера

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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ---------- НАСТРОЙКИ ----------
BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ID группы водителей (например -100...)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

assert BOT_TOKEN, "BOT_TOKEN is required"

# Тарифы (примерная цена/час, показываем как «от ... ₽/ч»)
PRICES = {
    "Maybach W223": "от 7000 ₽/ч",
    "Maybach W222": "от 4000 ₽/ч",
    "S-Class W223": "от 5000 ₽/ч",
    "S-Class W222": "от 3000 ₽/ч",
    "Business": "от 2000 ₽/ч",
    "Minivan": "от 3000 ₽/ч",
}

# Память бота:
# order_id -> dict(...)
ORDERS_CACHE: dict[str, dict] = {}
# driver_id -> {car_class, plate, photo_file_id}
DRIVER_PROFILES: dict[int, dict] = {}

# ---------- GOOGLE SHEETS ----------
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
sheet = gc.open("orders").sheet1

# Структура строк в таблице:
# 1: order_id
# 2: user_id
# 3: username
# 4: pickup
# 5: destination
# 6: car_class
# 7: time (время подачи — текстом)
# 8: hours (кол-во часов аренды)
# 9: passengers
# 10: contact
# 11: approx_price
# 12: created_at
# 13: status (new/assigned/started/finished)
# 14: driver_id
# 15: driver_name
# 16: arrived_at
# 17: finished_at


def save_order_to_sheet(order: dict) -> None:
    """Сохранить заказ в таблицу (новая строка)."""
    try:
        sheet.append_row(
            [
                order.get("order_id"),
                order.get("user_id"),
                order.get("username"),
                order.get("pickup"),
                order.get("destination"),
                order.get("car_class"),
                order.get("time"),
                order.get("hours"),
                order.get("passengers"),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                order.get("status", "new"),
                str(order.get("driver_id") or ""),
                order.get("driver_name") or "",
                order.get("arrived_at") or "",
                order.get("finished_at") or "",
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка Google Sheets: %s", e)


def find_order_row(order_id: str):
    """Найти номер строки по order_id в первой колонке."""
    try:
        col = sheet.col_values(1)
        for idx, val in enumerate(col, start=1):
            if val == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа: %s", e)
    return None


def update_order_in_sheet(order: dict):
    """Обновить статус/водителя/время в таблице для уже существующего заказа."""
    row = find_order_row(order.get("order_id"))
    if not row:
        return
    try:
        sheet.update_cell(row, 13, order.get("status", ""))               # status
        sheet.update_cell(row, 14, str(order.get("driver_id") or ""))     # driver_id
        sheet.update_cell(row, 15, order.get("driver_name") or "")        # driver_name
        sheet.update_cell(row, 16, order.get("arrived_at") or "")         # arrived_at
        sheet.update_cell(row, 17, order.get("finished_at") or "")        # finished_at
    except Exception as e:
        log.error("Ошибка обновления заказа в таблице: %s", e)


# ---------- СОСТОЯНИЯ ДИАЛОГА ----------
PICKUP, DEST, CAR, TIME, HOURS, PAX, CONTACT, CONFIRM = range(8)

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
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


def price_text() -> str:
    lines = ["<b>Тарифы (ориентировочно):</b>"]
    for k, v in PRICES.items():
        lines.append(f"• {k}: {v}")
    lines.append(
        "\nМинимум 2 часа. От 3 часов действует скидка. "
        "Аэропорты считаются с коэффициентом 2 для всех классов."
    )
    return "\n".join(lines)


def to_maps_link(lat: float, lon: float) -> str:
    """Ссылка на Яндекс.Карты по координатам."""
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"


def _parse_hours(text: str) -> int:
    """Парсим текст выбора часов в число."""
    t = text.lower().strip()
    if t.startswith("2"):
        return 2
    if t.startswith("3"):
        return 3
    if t.startswith("4"):
        return 4
    return 5  # «5 часов и более»


def _is_airport(order: dict) -> bool:
    """Проверка, что заказ связан с аэропортом (по тексту адреса)."""
    pickup = (order.get("pickup") or "").lower()
    dest = (order.get("destination") or "").lower()
    s = pickup + " " + dest
    for kw in ["шереметьево", "домодедово", "внуково", "жуковский", "аэропорт", "airport"]:
        if kw in s:
            return True
    return False


def _calc_price_for_order(order: dict) -> str:
    """Примерный расчёт общей стоимости (учёт часов, аэропорта, скидки)."""
    car = order.get("car_class")
    base_text = PRICES.get(car)
    if not base_text:
        return "По запросу"

    digits = "".join(ch for ch in base_text if ch.isdigit())
    try:
        per_hour = int(digits)
    except ValueError:
        return base_text

    hours = int(order.get("hours") or 2)
    total = per_hour * hours

    # Аэропорты — коэффициент 2 для всех классов
    if _is_airport(order):
        total *= 2
    # От 3 часов — скидка (если не аэропорт)
    elif hours >= 3:
        total = int(total * 0.9)

    return f"≈ {total:,} ₽ за {hours} ч.".replace(",", " ")


# ---------- КОМАНДЫ БОТА ----------
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
            BotCommand("setdriver", "Настроить профиль водителя"),
            BotCommand("setcarphoto", "Задать фото машины"),
            BotCommand("orders", "Последние заказы (для диспетчера)"),
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


# ---------- AI-ДИСПЕТЧЕР /ai ----------
async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ai <ситуация>
    AI возвращает готовый текст для клиента (с датами «сегодня/завтра» в явном формате).
    """
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
            "Добавьте переменную окружения OPENAI_API_KEY в Railway."
        )
        return

    import requests

    today_str = datetime.now().strftime("%Y-%m-%d")
    system_prompt = (
        "Ты — живой диспетчер премиум-такси (VIP taxi).\n"
        "Отвечаешь клиентам от лица сервиса.\n\n"
        "Сегодняшняя дата: " + today_str + "\n\n"
        "Если в запросе встречаются слова типа «сегодня в 19:00», "
        "«завтра в 10», «послезавтра в 8 утра» — обязательно "
        "переведи это в явную дату и время в формате:\n"
        "«завтра (2025-11-15) в 10:00».\n\n"
        "Правила:\n"
        "1) Всегда обращайся к клиенту на ВЫ.\n"
        "2) Пиши 1–3 предложения, по делу.\n"
        "3) Не упоминай, что ты ИИ или модель.\n"
        "4) Цены не выдумывай. Можно писать: «точную стоимость рассчитает диспетчер».\n"
        "5) В конфликте — извиниться и предложить решение.\n"
        "6) Максимум 1–2 нейтральных смайлика (🙂, 🙏) при необходимости.\n"
        "7) Верни только текст сообщения для клиента."
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
        await update.message.reply_text(
            "Не удалось получить ответ от ИИ. Проверьте OPENAI_API_KEY или интернет на сервере."
        )


# ---------- ПРОФИЛЬ ВОДИТЕЛЯ ----------
async def setdriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setdriver <класс> <номер>
    Примеры:
    /setdriver S-Class W223 А123АА777
    """
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "Используйте так:\n"
            "/setdriver <класс> <номер>\n\n"
            "Например:\n"
            "/setdriver S-Class W223 А123АА777"
        )
        return

    car_class = " ".join(context.args[:-1]).strip()
    plate = context.args[-1].strip()

    if car_class not in PRICES:
        await update.message.reply_text(
            "Неизвестный класс. Допустимые варианты:\n" + "\n".join(PRICES.keys())
        )
        return

    DRIVER_PROFILES[user.id] = {
        "car_class": car_class,
        "plate": plate,
        "photo_file_id": DRIVER_PROFILES.get(user.id, {}).get("photo_file_id"),
    }

    await update.message.reply_text(
        f"Профиль водителя обновлён.\n"
        f"Класс: {car_class}\n"
        f"Номер: {plate}\n"
        f"Фото можно добавить: отправьте фото и подпишите сообщение командой /setcarphoto."
    )


async def setcarphoto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Водитель отправляет фото машины и команду /setcarphoto в подписи.
    """
    user = update.effective_user
    profile = DRIVER_PROFILES.get(user.id)

    if not profile:
        await update.message.reply_text(
            "Сначала задайте класс и номер:\n/setdriver <класс> <номер>"
        )
        return

    if not update.message.photo:
        await update.message.reply_text("Отправьте именно фото машины (из галереи/камеры).")
        return

    file_id = update.message.photo[-1].file_id
    profile["photo_file_id"] = file_id
    DRIVER_PROFILES[user.id] = profile

    await update.message.reply_text("Фото машины сохранено.")


# ---------- ЗАКАЗ ОТ КЛИЕНТА (CONVERSATION) ----------
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"] = {
        "order_id": uuid4().hex[:8],
        "user_id": update.effective_user.id,
        "username": f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name,
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


async def pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    context.user_data["order"]["pickup"] = to_maps_link(loc.latitude, loc.longitude)
    await update.message.reply_text(
        "Точка подачи получена.\n📍 Отправьте адрес назначения.",
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
    context.user_data["order"]["destination"] = to_maps_link(loc.latitude, loc.longitude)
    await update.message.reply_text("Выберите класс авто.", reply_markup=cars_kb())
    return CAR


async def text_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["destination"] = update.message.text.strip()
    await update.message.reply_text("Выберите класс авто.", reply_markup=cars_kb())
    return CAR


async def car_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    car = update.message.text.strip()
    if car not in PRICES:
        await update.message.reply_text("Пожалуйста, выберите тариф кнопкой.", reply_markup=cars_kb())
        return CAR

    context.user_data["order"]["car_class"] = car
    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["time"] = update.message.text.strip()
    kb = ReplyKeyboardMarkup(
        [
            ["2 часа", "3 часа"],
            ["4 часа", "5 часов и более"],
            ["❌ Отмена"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "На сколько часов нужна машина?\nМинимум 2 часа. От 3 часов действует скидка.",
        reply_markup=kb,
    )
    return HOURS


async def hours_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    hours = _parse_hours(update.message.text)
    context.user_data["order"]["hours"] = hours
    context.user_data["order"]["approx_price"] = _calc_price_for_order(context.user_data["order"])

    await update.message.reply_text(
        "Сколько пассажиров?",
        reply_markup=ReplyKeyboardMarkup(
            [["1", "2", "3", "4", "5", "6"], ["❌ Отмена"]],
            resize_keyboard=True,
        ),
    )
    return PAX


async def pax_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["passengers"] = update.message.text.strip()
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
    text = (
        "<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('destination')}\n"
        f"• Класс авто: {o.get('car_class')}\n"
        f"• Время подачи: {o.get('time')}\n"
        f"• Аренда: {o.get('hours', 2)} ч.\n"
        f"• Пассажиров: {o.get('passengers')}\n"
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
    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("Отменено. Чем ещё помочь?")
        return ConversationHandler.END

    order = context.user_data["order"]

    if "hours" not in order:
        order["hours"] = 2
    if "approx_price" not in order:
        order["approx_price"] = _calc_price_for_order(order)

    order["status"] = "new"
    order["driver_id"] = None
    order["driver_name"] = None
    order["arrived_at"] = None
    order["finished_at"] = None

    save_order_to_sheet(order)
    ORDERS_CACHE[order["order_id"]] = dict(order)

    await q.edit_message_text("Заказ принят. Водитель свяжется с вами.")

    # Отправляем заказ в группу водителей
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
            f"⏳ Аренда: {order.get('hours')} ч.\n"
            f"👥 Пассажиров: {order.get('passengers')}\n"
            f"💰 Ориентировочно: {order.get('approx_price')}\n\n"
            f"Личные данные клиента скрыты."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"drv_take:{order.get('order_id')}")]]
        )
        try:
            await context.bot.send_message(chat_id=admin_id, text=text_for_drivers, reply_markup=keyboard)
        except Exception as e:
            log.error("Не удалось отправить заказ в группу водителей: %s", e)

    context.user_data.clear()
    return ConversationHandler.END


# ---------- КНОПКИ ВОДИТЕЛЕЙ (взять / отменить / на месте / завершить) ----------
async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    driver = query.from_user
    global ORDERS_CACHE

    # Взять заказ
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

        if order.get("status") in ("assigned", "started", "finished"):
            await query.answer("Этот заказ уже забрал другой водитель.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        profile = DRIVER_PROFILES.get(driver.id)
        if not profile:
            await query.answer("Сначала настройте профиль: /setdriver", show_alert=True)
            return

        # Ограничение по классу машины
        if profile.get("car_class") != order.get("car_class"):
            await query.answer("Этот заказ не подходит вашему классу авто.", show_alert=True)
            return

        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = driver.username or driver.full_name
        ORDERS_CACHE[order_id] = order
        update_order_in_sheet(order)

        # Удаляем сообщение из группы
        try:
            await query.message.delete()
        except Exception:
            pass

        # Отправляем детали в личку водителю
        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}\n"
            f"⏰ Время подачи: {order.get('time')}\n"
            f"⏳ Аренда: {order.get('hours')} ч.\n"
            f"👥 Пассажиров: {order.get('passengers')}\n"
            f"💰 Ориентировочно: {order.get('approx_price')}\n\n"
            f"Личные данные клиента скрыты. Дальнейшие детали сообщит диспетчер."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚗 На месте", callback_data=f"drv_arrived:{order_id}")],
                [InlineKeyboardButton("🔴 Отменить заказ", callback_data=f"drv_cancel:{order_id}")],
            ]
        )
        try:
            await context.bot.send_message(chat_id=driver.id, text=dm_text, reply_markup=keyboard)
        except Exception as e:
            log.error("Не удалось отправить заказ в ЛС водителю: %s", e)

    # Отменить заказ водителем
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
        update_order_in_sheet(order)

        try:
            await query.edit_message_text("Вы отменили заказ. Он возвращён в общий список.")
        except Exception:
            pass

        # Вернуть заказ в группу
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
                f"⏳ Аренда: {order.get('hours')} ч.\n"
                f"👥 Пассажиров: {order.get('passengers')}\n"
                f"💰 Ориентировочно: {order.get('approx_price')}\n\n"
                f"Личные данные клиента скрыты."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"drv_take:{order_id}")]]
            )
            try:
                await context.bot.send_message(chat_id=admin_id, text=text_for_drivers, reply_markup=keyboard)
            except Exception as e:
                log.error("Не удалось вернуть заказ в группу водителей: %s", e)

    # Водитель на месте
    elif data.startswith("drv_arrived:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)

        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return

        if order.get("driver_id") != driver.id:
            await query.answer("Отметить «на месте» может только водитель, принявший заказ.", show_alert=True)
            return

        now = datetime.now()
        order["status"] = "started"
        order["arrived_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        ORDERS_CACHE[order_id] = order
        update_order_in_sheet(order)

        client_id = order.get("user_id")
        if client_id:
            keyboard_client = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Завершить поездку", callback_data=f"finish:{order_id}")]]
            )
            try:
                await context.bot.send_message(
                    chat_id=int(client_id),
                    text=(
                        "🚗 Ваш водитель на месте.\n"
                        "После окончания поездки нажмите «Завершить поездку»."
                    ),
                    reply_markup=keyboard_client,
                )
            except Exception as e:
                log.error("Не смог отправить сообщение клиенту: %s", e)

        keyboard_driver = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Завершить поездку", callback_data=f"finish:{order_id}")]]
        )
        try:
            await query.edit_message_text(
                "Отметили: вы на месте. Когда поездка закончится — нажмите «Завершить поездку».",
                reply_markup=keyboard_driver,
            )
        except Exception:
            pass

    # Завершить поездку (нажимает либо водитель, либо клиент)
    elif data.startswith("finish:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS_CACHE.get(order_id)

        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return

        if order.get("status") == "finished":
            await query.answer("Поездка уже завершена.", show_alert=True)
            return

        now = datetime.now()
        order["status"] = "finished"
        order["finished_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        ORDERS_CACHE[order_id] = order

        minutes = 0
        if order.get("arrived_at"):
            try:
                started = datetime.strptime(order["arrived_at"], "%Y-%m-%d %H:%M:%S")
                minutes = int((now - started).total_seconds() // 60)
            except Exception:
                pass

        update_order_in_sheet(order)
        txt_done = (
            f"✅ Поездка по заказу #{order_id} завершена.\n"
            f"Продолжительность (с момента подачи): ~{minutes} мин."
        )

        try:
            await query.edit_message_text(txt_done)
        except Exception:
            pass

        other_chat = None
        if query.from_user.id == order.get("driver_id"):
            other_chat = order.get("user_id")
        elif query.from_user.id == order.get("user_id"):
            other_chat = order.get("driver_id")

        if other_chat:
            try:
                await context.bot.send_message(chat_id=int(other_chat), text=txt_done)
            except Exception as e:
                log.error("Не смог отправить финальное сообщение второй стороне: %s", e)


# ---------- КНОПКА «ФОТО МАШИНЫ» ДЛЯ КЛИЕНТА ----------
async def car_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Клиент нажимает «📷 Фото машины» — пытаемся найти его последний активный заказ
    и отправить фото машины прикреплённого водителя (если есть).
    """
    user_id = update.effective_user.id

    # Ищем заказ этого пользователя в кэше, где уже есть водитель
    active_order = None
    for o in ORDERS_CACHE.values():
        if o.get("user_id") == user_id and o.get("driver_id"):
            if o.get("status") in ("assigned", "started"):
                active_order = o
                break

    if not active_order:
        await update.message.reply_text(
            "Фото машины будет доступно после назначения водителя на ваш заказ."
        )
        return

    driver_id = active_order.get("driver_id")
    profile = DRIVER_PROFILES.get(driver_id)

    if not profile:
        await update.message.reply_text(
            "Профиль водителя ещё не заполнен. Пожалуйста, уточните у диспетчера."
        )
        return

    photo_id = profile.get("photo_file_id")
    plate = profile.get("plate")
    car_class = profile.get("car_class")

    if not photo_id:
        await update.message.reply_text(
            f"Водитель пока не загрузил фото машины.\n"
            f"Класс: {car_class}\n"
            f"Номер: {plate or 'не указан'}"
        )
        return

    caption = f"Ваш автомобиль:\nКласс: {car_class}\nНомер: {plate or 'не указан'}"

    try:
        await update.message.reply_photo(photo=photo_id, caption=caption)
    except Exception as e:
        log.error("Ошибка при отправке фото машины: %s", e)
        await update.message.reply_text(
            "Не удалось отправить фото машины. Попробуйте позже или уточните у диспетчера."
        )


# ---------- ПРОСТАЯ CRM: /orders (последние заказы) ----------
async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /orders — показать последние заказы (для диспетчера).
    Можно добавить фильтр по статусу: /orders new, /orders assigned, /orders finished
    """
    status_filter = None
    if context.args:
        status_filter = context.args[0].strip().lower()

    try:
        values = sheet.get_all_values()
    except Exception as e:
        log.error("Не удалось прочитать таблицу: %s", e)
        await update.message.reply_text("Не удалось прочитать таблицу заказов.")
        return

    if len(values) <= 1:
        await update.message.reply_text("Заказов пока нет.")
        return

    # предполагаем, что первая строка — заголовки (как у тебя)
    rows = values[1:]
    if not rows:
        await update.message.reply_text("Заказов пока нет.")
        return

    # последние 10 заказов (с конца)
    rows = rows[-10:]

    lines = []
    for row in rows:
        # подстраховка по длине строки
        try:
            order_id = row[0]
            pickup = row[3] if len(row) > 3 else ""
            dest = row[4] if len(row) > 4 else ""
            car_class = row[5] if len(row) > 5 else ""
            time_str = row[6] if len(row) > 6 else ""
            hours = row[7] if len(row) > 7 else ""
            approx_price = row[10] if len(row) > 10 else ""
            status = (row[12] if len(row) > 12 else "").lower()
        except Exception:
            continue

        if status_filter and status != status_filter:
            continue

        line = (
            f"#{order_id} | {status or '—'}\n"
            f"📍 {pickup}\n"
            f"🏁 {dest}\n"
            f"🚘 {car_class}, {hours} ч.\n"
            f"⏰ {time_str}\n"
            f"💰 {approx_price}\n"
            "------------------------"
        )
        lines.append(line)

    if not lines:
        await update.message.reply_text("Нет заказов с таким фильтром.")
        return

    text = "<b>Последние заказы:</b>\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------- РОУТИНГ / СБОРКА ПРИЛОЖЕНИЯ ----------
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
    app.add_handler(CommandHandler("orders", orders_cmd))

    # разговор заказа
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
            TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, time_set),
            ],
            HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hours_set),
            ],
            PAX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pax_set),
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

    # обработка кнопок водителей и завершения
    app.add_handler(CallbackQueryHandler(driver_orders_callback, pattern=r"^drv_|^finish:"))

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