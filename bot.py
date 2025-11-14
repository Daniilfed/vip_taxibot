# -*- coding: utf-8 -*-
# VIP Taxi Bot — Google Sheets + бронирование заказов + AI-диспетчер

import os
import json
import logging
from uuid import uuid4
from datetime import datetime
from typing import Dict, Any

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
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ID группы водителей (например: -1003446...)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # ключ OpenAI для AI-диспетчера (опционально)
GOOGLE_JSON_ENV = "GOOGLE_APPLICATION_CREDENTIALS_JSON"
SHEET_ID = os.environ.get("SHEET_ID")  # можно не задавать, тогда ищем по имени "orders"

assert BOT_TOKEN, "BOT_TOKEN is required"
assert GOOGLE_JSON_ENV in os.environ, "GOOGLE_APPLICATION_CREDENTIALS_JSON is required"

# Тарифы (примерная цена/час, в тексте покажем как «от»)
PRICES = {
    "Maybach W223": "от 7000 ₽/ч",
    "Maybach W222": "от 4000 ₽/ч",
    "S-Class W223": "от 5000 ₽/ч",
    "S-Class W222": "от 3000 ₽/ч",
    "Business": "от 2000 ₽/ч",
    "Minivan": "от 3000 ₽/ч",
}

# Кэш заказов: order_id -> данные заказа (для действий водителей)
ORDERS_CACHE: Dict[str, Dict[str, Any]] = {}

# ---------- GOOGLE SHEETS ----------
from google.oauth2.service_account import Credentials
import gspread

credentials_info = json.loads(os.environ[GOOGLE_JSON_ENV])
credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
gc = gspread.authorize(credentials)

try:
    if SHEET_ID:
        sh = gc.open_by_key(SHEET_ID)
    else:
        sh = gc.open("orders")  # таблица по имени
    sheet = sh.sheet1          # первый лист
except Exception as e:
    log.error("Не удалось открыть Google Sheets: %s", e)
    raise

# Структура строк в таблице:
# A: order_id
# B: user_id
# C: username
# D: pickup
# E: destination
# F: car_class
# G: time
# H: passengers
# I: contact
# J: approx_price
# K: created_at
# L: status        (new / assigned / arrived)
# M: driver_id
# N: driver_name


def save_order_to_sheet(order: Dict[str, Any]) -> None:
    """Запись подтверждённого заказа в Google Sheets."""
    try:
        status = order.get("status", "new")
        driver_id = order.get("driver_id", "")
        driver_name = order.get("driver_name", "")
        sheet.append_row(
            [
                order.get("order_id"),
                order.get("user_id"),
                order.get("username"),
                order.get("pickup"),
                order.get("destination"),
                order.get("car_class"),
                order.get("time"),
                order.get("passengers"),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status,
                str(driver_id) if driver_id else "",
                driver_name,
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка Google Sheets: %s", e)


def find_order_row(order_id: str):
    """Ищем номер строки по order_id в первой колонке. Возвращаем номер строки или None."""
    try:
        records = sheet.col_values(1)  # A колонка
        for idx, val in enumerate(records, start=1):
            if val == order_id:
                return idx
    except Exception as e:
        log.error("Ошибка поиска заказа в таблице: %s", e)
    return None


def update_order_status_in_sheet(order_id: str, status: str, driver_id=None, driver_name=None):
    """Обновить статус и данные водителя по order_id в таблице."""
    row = find_order_row(order_id)
    if not row:
        return
    try:
        sheet.update_cell(row, 12, status)  # L: status
        sheet.update_cell(row, 13, str(driver_id) if driver_id else "")  # M: driver_id
        sheet.update_cell(row, 14, driver_name or "")  # N: driver_name
    except Exception as e:
        log.error("Ошибка обновления статуса заказа в таблице: %s", e)


# ---------- КОНСТАНТЫ СОСТОЯНИЙ ----------
PICKUP, DEST, CAR, TIME, PAX, CONTACT, CONFIRM = range(7)

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔔 Заказ", "💰 Тарифы"],
            ["📌 Статус", "☎️ Контакт"],
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
    lines = ["<b>Тарифы (ориентировочно):</b>"]
    for k, v in PRICES.items():
        lines.append(f"• {k}: {v}")
    lines.append("\nМинимум 1 час. Точная стоимость зависит от маршрута, времени и загрузки.")
    return "\n".join(lines)


def to_maps_link(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat},{lon}"


def approx_for_class(car_class: str) -> str:
    return PRICES.get(car_class, "По запросу")


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
            BotCommand("ai", "AI-чат для диспетчера"),
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


# ---------- AI-ЧАТ ДЛЯ ДИСПЕТЧЕРА ----------
async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    AI-диспетчер.
    /ai <ситуация> -> бот возвращает ГОТОВЫЙ текст для клиента.
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
            "Добавьте переменную окружения OPENAI_API_KEY в Railway с ключом OpenAI."
        )
        return

    try:
        import requests
    except ImportError:
        await update.message.reply_text(
            "Модуль requests не установлен в окружении.\n"
            "Он уже есть в requirements.txt, просто пересоберите проект."
        )
        return

    system_prompt = (
        "Ты — живой диспетчер премиум-такси (VIP такси). "
        "Твоя задача — писать ГОТОВЫЕ сообщения для клиента от лица сервиса такси.\n\n"
        "Правила:\n"
        "1) Всегда обращайся к клиенту на ВЫ.\n"
        "2) Пиши максимально вежливо, спокойно и по делу.\n"
        "3) Не упоминай, что ты ИИ, бот, модель и т.п. Ты просто диспетчер.\n"
        "4) Не придумывай конкретные ЦЕНЫ и ТАРИФЫ, если в запросе они не указаны. "
        "   Можно писать общие фразы: «стоимость уточнит диспетчер», «ориентировочно» и т.п.\n"
        "5) Отвечай коротко: 1–3 предложения. Без длинных объяснений.\n"
        "6) Если ситуация конфликтная — сохраняй уважение, предлагай решение.\n"
        "7) Никаких смайликов, максимум 1–2 нейтральных (🙂, 🙏) при уместности.\n\n"
        "На вход ты получаешь ОПИСАНИЕ СИТУАЦИИ. "
        "Нужно вернуть только текст сообщения для клиента."
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
        import requests  # ещё раз для mypy/линтеров, но это ок
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
            "Не удалось получить ответ от ИИ. Проверьте ключ OPENAI_API_KEY и интернет на сервере."
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
    context.user_data["order"]["approx_price"] = approx_for_class(car)
    await update.message.reply_text(
        "⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True),
    )
    return TIME


async def time_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["time"] = update.message.text.strip()
    await update.message.reply_text(
        "Сколько пассажиров?",
        reply_markup=ReplyKeyboardMarkup(
            [["1", "2", "3", "4", "5", "6"], ["❌ Отмена"]], resize_keyboard=True
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
    text = (
        "<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('destination')}\n"
        f"• Класс авто: {o.get('car_class')}  ({o.get('approx_price')})\n"
        f"• Время: {o.get('time')}\n"
        f"• Пассажиров: {o.get('passengers')}\n"
        f"• Контакт: {o.get('contact')}\n\n"
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

    # подтверждение
    order = context.user_data["order"]

    # начальный статус
    order["status"] = "new"
    order["driver_id"] = None
    order["driver_name"] = None

    # сохраняем в таблицу
    save_order_to_sheet(order)

    # кладём в кэш
    ORDERS_CACHE[order["order_id"]] = dict(order)

    # пользователь
    await q.edit_message_text("Заказ принят. Водитель свяжется с вами.")

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
            f"🚘 Класс: {order.get('car_class')}  ({order.get('approx_price')})\n"
            f"⏰ Время: {order.get('time')}\n"
            f"👥 Пассажиров: {order.get('passengers')}\n\n"
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


# ---------- КНОПКИ ВОДИТЕЛЕЙ ----------
async def driver_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок в группе водителей и в ЛС водителя: взять/отменить/на месте."""
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

        if order.get("status") in ("assigned", "arrived"):
            await query.answer("Этот заказ уже забрал другой водитель.", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        # Обновляем статус и водителя
        order["status"] = "assigned"
        order["driver_id"] = driver.id
        order["driver_name"] = driver.username or driver.full_name
        ORDERS_CACHE[order_id] = order

        # Таблица
        update_order_status_in_sheet(
            order_id=order_id,
            status="assigned",
            driver_id=driver.id,
            driver_name=order["driver_name"],
        )

        # Удаляем сообщение из группы
        try:
            await query.message.delete()
        except Exception:
            pass

        # Личные сообщения водителю (важно: водитель должен сначала нажать /start бота в ЛС!)
        dm_text = (
            f"Вы приняли заказ #{order_id}\n\n"
            f"📍 Откуда: {order.get('pickup')}\n"
            f"🏁 Куда: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}  ({order.get('approx_price')})\n"
            f"⏰ Время: {order.get('time')}\n"
            f"👥 Пассажиров: {order.get('passengers')}\n\n"
            f"Личные данные клиента скрыты. Дальнейшие инструкции выдаст диспетчер."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚗 На месте", callback_data=f"drv_arrived:{order_id}")],
                [InlineKeyboardButton("🔴 Отменить заказ", callback_data=f"drv_cancel:{order_id}")],
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

        # Возвращаем статус
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

        # ЛС водителю
        try:
            await query.edit_message_text("Вы отменили заказ. Он возвращён в общий список.")
        except Exception:
            pass

        # Возвращаем заказ в группу
        try:
            admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
        except ValueError:
            admin_id = ADMIN_CHAT_ID

        if admin_id:
            text_for_drivers = (
                f"🆕 Заказ снова доступен #{order_id}\n"
                f"📍 Откуда: {order.get('pickup')}\n"
                f"🏁 Куда: {order.get('destination')}\n"
                f"🚘 Класс: {order.get('car_class')}  ({order.get('approx_price')})\n"
                f"⏰ Время: {order.get('time')}\n"
                f"👥 Пассажиров: {order.get('passengers')}\n\n"
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

    # Водитель на месте (демо-оплата)
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
            order_id=order_id,
            status="arrived",
            driver_id=order.get("driver_id"),
            driver_name=order.get("driver_name"),
        )

        # ДЕМО-ОПЛАТА: отправляем клиенту сообщение, без реальных платежей
        client_id = order.get("user_id")
        if client_id:
            try:
                await context.bot.send_message(
                    chat_id=int(client_id),
                    text=(
                        "🚗 Ваш водитель на месте.\n"
                        "В ближайшем будущем здесь появится кнопка для оплаты поездки 💳 (демо-версия)."
                    ),
                )
            except Exception as e:
                log.error("Не смог отправить сообщение клиенту: %s", e)

        try:
            await query.edit_message_text("Отметили: вы на месте. Ожидаем клиента.")
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
            TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, time_set),
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

    # кнопки водителей
    app.add_handler(CallbackQueryHandler(driver_orders_callback, pattern=r"^drv_"))

    # кнопки меню
    app.add_handler(MessageHandler(filters.Regex("^💰 Тарифы$"), price_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📌 Статус$"), status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^☎️ Контакт$"), contact_cmd))
    app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd))

    app.post_init = set_commands
    return app


if __name__ == "__main__":
    app = build_app()
    log.info("Bot is starting…")
    app.run_polling(close_loop=False)