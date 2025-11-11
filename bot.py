# -*- coding: utf-8 -*-
# VIP Taxi Bot — минимальный, без картинок, с Google Sheets

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
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # строка или число — не важно
assert BOT_TOKEN, "BOT_TOKEN is required"

# Тарифы (примерная цена/час)
PRICES = {
    "Maybach W223": "7000 ₽/ч",
    "Maybach W222": "4000 ₽/ч",
    "S-Class W223": "5000 ₽/ч",
    "S-Class W222": "3000 ₽/ч",
    "Business": "2000 ₽/ч",
    "Minivan": "3000 ₽/ч",
}

# ---------- GOOGLE SHEETS ----------
# Требуется Railway variable GOOGLE_APPLICATION_CREDENTIALS_JSON со всем JSON ключом
from google.oauth2.service_account import Credentials
import gspread

credentials_info = json.loads(os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"])
credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"  # <-- добавили Drive
    ],
)
gc = gspread.authorize(credentials)
sheet = gc.open("orders").sheet1  # таблица: orders -> Лист1
def save_order_to_sheet(order: dict) -> None:
    """Запись подтверждённого заказа в Google Sheets."""
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
                order.get("passengers"),
                order.get("contact"),
                order.get("approx_price"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ],
            value_input_option="USER_ENTERED",
        )
        log.info("Заказ записан в Google Sheets")
    except Exception as e:
        log.error("Ошибка Google Sheets: %s", e)

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
    lines = ["<b>Тарифы (примерно):</b>"]
    for k, v in PRICES.items():
        lines.append(f"• {k}: {v}")
    lines.append("\nМинимум 1 час. Точная стоимость зависит от маршрута и времени.")
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

# ---------- ЗАКАЗ (CONVERSATION) ----------
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
    link = to_maps_link(loc.latitude, loc.longitude)
    context.user_data["order"]["pickup"] = link
    await update.message.reply_text("Точка подачи получена.\n📍 Отправьте адрес назначения.", reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True))
    return DEST

async def text_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text("Укажите адрес назначения.", reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True))
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
        reply_markup=ReplyKeyboardMarkup([["1", "2", "3", "4", "5", "6"], ["❌ Отмена"]], resize_keyboard=True),
    )
    return PAX

async def pax_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["order"]["passengers"] = update.message.text.strip()
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Поделиться телефоном", request_contact=True)], ["❌ Отмена"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text("Оставьте контакт (имя и телефон), или поделитесь номером:", reply_markup=kb)
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
        f"• Класс авто: {o.get('car_class')}  (≈ {o.get('approx_price')})\n"
        f"• Время: {o.get('time')}\n"
        f"• Пассажиров: {o.get('passengers')}\n"
        f"• Контакт: {o.get('contact')}\n\n"
        "Если всё верно — нажмите «Подтверждаю». Для отмены — «Отмена»."
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Подтверждаю", callback_data="confirm"),
          InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
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
    save_order_to_sheet(order)

    # Сообщение пользователю
    await q.edit_message_text("Заказ принят. Водитель свяжется с вами.")
    # Дублируем диспетчеру
    try:
        admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
    except ValueError:
        admin_id = ADMIN_CHAT_ID

    if admin_id:
        txt = (
            f"🆕 <b>Новый заказ</b>\n"
            f"От: {order.get('username')} (ID {order.get('user_id')})\n"
            f"📍 Подача: {order.get('pickup')}\n"
            f"🏁 Назначение: {order.get('destination')}\n"
            f"🚘 Класс: {order.get('car_class')}  (≈ {order.get('approx_price')})\n"
            f"⏰ Время: {order.get('time')}\n"
            f"👥 Пассажиров: {order.get('passengers')}\n"
            f"☎️ Контакт: {order.get('contact')}\n"
            f"№ {order.get('order_id')}"
        )
        try:
            await context.bot.send_message(admin_id, txt, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.error("Не удалось отправить админу: %s", e)

    context.user_data.clear()
    return ConversationHandler.END

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
        fallbacks=[CommandHandler("cancel", cancel_cmd), MessageHandler(filters.Regex("^❌ Отмена$"), cancel_cmd)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Кнопки меню
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