# -*- coding: utf-8 -*-
import os
import csv
import math
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
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ====================== НАСТРОЙКА ==========================
BRAND_NAME = "VIP taxi"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "143710784"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN is required"

# Фото автопарка (нейтральные Unsplash)
CAR_PHOTOS = {
    "S-Class W222": "https://images.unsplash.com/photo-1615732045871-8db6d1dc8723",
    "Maybach W222": "https://images.unsplash.com/photo-1624784194858-4e1cb2e54c56",
    "S-Class W223": "https://images.unsplash.com/photo-1649254362283-5c9b83a3d31f",
    "Maybach W223": "https://images.unsplash.com/photo-1650659020204-3d8e60d2dcbb",
    "Business": "https://images.unsplash.com/photo-1606813902915-5c2b66f04e8e",
    "Minivan": "https://images.unsplash.com/photo-1618401471383-5e00764f9a72",
}

CAR_DESCR = {
    "S-Class W222": "Mercedes-Benz S-Class (W222). Кожаный салон, салфетки, вода, зарядки.",
    "Maybach W222": "Mercedes-Maybach (W222). Индивидуальные кресла; салфетки, вода, зарядки.",
    "S-Class W223": "Mercedes-Benz S-Class (W223). Новое поколение; салфетки, вода, зарядки.",
    "Maybach W223": "Mercedes-Maybach (W223). Флагман люкса: массаж; вода и зарядки.",
    "Business": "Mercedes E-Class / BMW 5. Комфортный седан, вода и зарядки.",
    "Minivan": "Mercedes V-Class. До 6 пассажиров; салфетки, вода, зарядки.",
}

# 💰 АКТУАЛЬНЫЕ ТАРИФЫ (строки для вывода)
PRICES = {
    "Maybach W223": "7000 ₽/ч",
    "Maybach W222": "4000 ₽/ч",
    "S-Class W223": "5000 ₽/ч",
    "S-Class W222": "3000 ₽/ч",
    "Business": "2000 ₽/ч",
    "Minivan": "3000 ₽/ч",
}
# Числовые почасовые для расчёта
HOURLY_INT = {
    "Maybach W223": 7000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
    "Minivan": 3000,
}

# Оценка по расстоянию (руб/км) и базовая подача
RATE_PER_KM = {
    "Maybach W223": 120,
    "Maybach W222": 90,
    "S-Class W223": 100,
    "S-Class W222": 70,
    "Business": 50,
    "Minivan": 60,
}
BASE_FEE = 500

# ====================== ЛОГИ ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ====================== УТИЛИТЫ ===========================
def _try_coords(s: str):
    if not s or "," not in s:
        return None
    a, b = s.split(",", 1)
    try:
        return float(a), float(b)
    except Exception:
        return None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2*R*math.asin(math.sqrt(a))

def estimate_price(order: dict) -> int | None:
    car = order.get("car")
    if not car:
        return None
    rate = RATE_PER_KM.get(car)
    if not rate:
        return None
    c1 = _try_coords(order.get("pickup", ""))
    c2 = _try_coords(order.get("drop", ""))
    if not (c1 and c2):
        return None
    dist = haversine_km(c1[0], c1[1], c2[0], c2[1])
    rough = int(round(BASE_FEE + dist * rate, -1))
    return max(rough, BASE_FEE)

def calc_amount(order: dict) -> int:
    est = estimate_price(order)
    if est:
        return est
    return HOURLY_INT.get(order.get("car"), 3500)

def ensure_csv(path: str, header: list[str]):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)

def save_order(o: dict, user):
    ensure_csv("orders.csv", ["ts", "user_id", "username", "pickup", "drop", "car", "when", "passengers", "contact", "paid"])
    with open("orders.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), user.id, user.username,
            o.get("pickup"), o.get("drop"), o.get("car"),
            o.get("when"), o.get("passengers"), o.get("contact"),
            o.get("paid", 0)
        ])

def save_feedback(rating: int, comment: str, user):
    ensure_csv("feedback.csv", ["ts", "user_id", "username", "rating", "comment"])
    with open("feedback.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([datetime.utcnow().isoformat(), user.id, user.username, rating, comment])

def save_user_stat(user):
    ensure_csv("users.csv", ["user_id", "username", "name", "orders", "last"])
    rows = {}
    if os.path.exists("users.csv"):
        with open("users.csv", "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[int(r["user_id"])] = r
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if user.id in rows:
        cnt = int(rows[user.id]["orders"]) + 1
    else:
        cnt = 1
    rows[user.id] = {
        "user_id": str(user.id),
        "username": user.username or "",
        "name": name,
        "orders": str(cnt),
        "last": datetime.utcnow().isoformat()
    }
    with open("users.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["user_id", "username", "name", "orders", "last"])
        w.writeheader()
        for r in rows.values():
            w.writerow(r)

# ====================== КЛАВИАТУРЫ ========================
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🛎 Заказ"), KeyboardButton("🚗 Автопарк")],
            [KeyboardButton("💳 Оплата"), KeyboardButton("📞 Контакты")],
            [KeyboardButton("⭐ Отзыв"), KeyboardButton("🪪 VIP-карта")],
            [KeyboardButton("📍 Геолокация", request_location=True)],
        ],
        resize_keyboard=True,
    )

def pickup_location_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Отправить мою геолокацию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def car_choice_kb():
    rows = [
        [InlineKeyboardButton("S-Class W222", callback_data="car:S-Class W222"),
         InlineKeyboardButton("Maybach W222", callback_data="car:Maybach W222")],
        [InlineKeyboardButton("S-Class W223", callback_data="car:S-Class W223"),
         InlineKeyboardButton("Maybach W223", callback_data="car:Maybach W223")],
        [InlineKeyboardButton("Business", callback_data="car:Business"),
         InlineKeyboardButton("Minivan", callback_data="car:Minivan")],
    ]
    return InlineKeyboardMarkup(rows)

def pay_keyboard(order_id: str, amount: int):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Оплатить {amount} ₽", callback_data=f"pay:{order_id}:{amount}")]]
    )

# ====================== СОСТОЯНИЯ =========================
PICKUP, DROP, CAR_CLASS, WHEN, PASSENGERS, CONTACT, CONFIRM = range(7)
FEEDBACK_RATING, FEEDBACK_TEXT = range(2)

# ====================== КОМАНДЫ ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        f"Добро пожаловать в {BRAND_NAME}.\n"
        "Ваш комфорт — наш приоритет.\n\n"
        "Выберите действие в меню ниже.\n"
        "Или отправьте геолокацию — подача по вашей точке."
    )
    await (update.message or update.callback_query.message).reply_text(txt, reply_markup=main_menu())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Тарифы:"]
    for k, v in PRICES.items():
        lines.append(f"• {k}: {v}")
    lines.append("\nОпции: ожидание, встреча с табличкой, детское кресло — по запросу.")
    await update.message.reply_text("\n".join(lines))

async def fleet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for cls in ["S-Class W222", "Maybach W222", "S-Class W223", "Maybach W223", "Business", "Minivan"]:
        url = CAR_PHOTOS[cls]
        descr = CAR_DESCR[cls]
        try:
            await update.message.reply_photo(photo=url, caption=f"{cls}\n{descr}\n{PRICES.get(cls, '')}")
        except Exception:
            await update.message.reply_text(f"{cls}\n{descr}\n{PRICES.get(cls, '')}")

async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "VIP-опции:\n"
        "• Салфетки, вода, зарядки\n"
        "• Встреча с табличкой\n"
        "• Ожидание и остановки по пути\n"
        "• Детское кресло по запросу"
    )
    await update.message.reply_text(txt)

async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Диспетчер: пишите здесь — ответим в чате.\nРезервный номер: +7 XXX XXX-XX-XX")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Укажите номер заказа или дату — проверим статус и вернёмся к вам.")

# ====================== ОТЗЫВЫ ============================
async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Оцените поездку от 1 до 5.")
    return FEEDBACK_RATING

async def feedback_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 5):
        await update.message.reply_text("Введите число от 1 до 5.")
        return FEEDBACK_RATING
    context.user_data["feedback_rating"] = int(txt)
    await update.message.reply_text("Оставьте короткий комментарий.")
    return FEEDBACK_TEXT

async def feedback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    rating = context.user_data.pop("feedback_rating", 5)
    save_feedback(rating, comment, update.effective_user)
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⭐ Отзыв от @{update.effective_user.username or 'user'} (ID {update.effective_user.id}):\n"
                f"Оценка: {rating}\nКомментарий: {comment}"
            )
        except Exception as e:
            log.warning(f"Admin notify failed: {e}")
    await update.message.reply_text("Спасибо. Мы ценим ваше мнение.")
    return ConversationHandler.END

async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отзыв отменён.")
    return ConversationHandler.END

# ====================== VIP-КАРТА =========================
async def vipcard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = (update.effective_user.first_name or "").strip()
    trips = 0
    if os.path.exists("users.csv"):
        with open("users.csv", "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if int(r["user_id"]) == uid:
                    trips = int(r.get("orders", 0))
                    name = r.get("name") or name
                    break
    await update.message.reply_text(
        f"🪪 VIP Card\nИмя: {name}\nID: {uid}\nПоездок: {trips}\nСтатус: Premium"
    )

# ====================== ОПЛАТА (ДЕМО) =====================
async def on_pay_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, order_id, amount = q.data.split(":")
    await q.edit_message_text(f"✅ Оплата заказа #{order_id} на {amount} ₽ выполнена (демо).")
    if ADMIN_CHAT_ID:
        user = update.effective_user
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"💰 Оплата (демо): заказ #{order_id} на {amount} ₽ от @{user.username or 'user'} (ID {user.id})"
            )
        except Exception as e:
            log.warning(f"Admin notify failed: {e}")

async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    o = context.user_data.get("order", {})
    order_id = str(uuid4())[:8]
    amount = calc_amount(o) if o else 3500
    await update.message.reply_text(
        f"💳 Оплата заказа #{order_id}\nСумма: {amount} ₽\nУслуга: Подача {BRAND_NAME}",
        reply_markup=pay_keyboard(order_id, amount)
    )

# ====================== ОФОРМЛЕНИЕ ЗАКАЗА =================
def class_caption(car_name: str) -> str:
    return f"{car_name}\n{CAR_DESCR.get(car_name, '')}\n{PRICES.get(car_name, '')}"

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = {"paid": 0}
    await update.message.reply_text(
        "Укажите адрес подачи или отправьте свою геолокацию кнопкой ниже.",
        reply_markup=pickup_location_kb()
    )
    return PICKUP

async def order_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text("Укажите адрес назначения.")
    return DROP

async def order_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["drop"] = update.message.text.strip()
    await update.message.reply_text("Выберите класс автомобиля:", reply_markup=car_choice_kb())
    return CAR_CLASS

async def on_car_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, car = q.data.split(":", 1)
    context.user_data["order"]["car"] = car
    url = CAR_PHOTOS.get(car)
    caption = class_caption(car)
    try:
        await q.message.reply_photo(photo=url, caption=caption)
    except Exception:
        await q.message.reply_text(caption)
    await q.message.reply_text("Когда подать автомобиль? (например: 10:00 сегодня / завтра 19:30)")
    return WHEN

async def order_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    o = context.user_data["order"]
    if ":" in txt or any(k in txt.lower() for k in ["сегодня", "завтра", "вечер", "утро", "ночь"]):
        o["when"] = txt
        await update.message.reply_text("Сколько пассажиров?")
        return PASSENGERS
    digits = "".join(ch for ch in txt if ch.isdigit())
    if digits:
        try:
            o["passengers"] = int(digits)
            await update.message.reply_text("Когда подать автомобиль?")
            return WHEN
        except Exception:
            pass
    await update.message.reply_text("Уточните: это время или количество пассажиров?")
    return WHEN

async def order_passengers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = "".join(ch for ch in update.message.text if ch.isdigit())
    if not digits:
        await update.message.reply_text("Введите число пассажиров (например, 2).")
        return PASSENGERS
    context.user_data["order"]["passengers"] = int(digits)
    await update.message.reply_text("Оставьте контакт (имя и телефон).")
    return CONTACT

async def order_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["contact"] = update.message.text.strip()
    o = context.user_data["order"]

    price_hint = estimate_price(o)
    price_line = f"\n💸 Ориентировочная стоимость: ~{price_hint} ₽" if price_hint else ""

    summary = (
        "Проверьте заказ:\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('drop')}\n"
        f"• Класс авто: {o.get('car')}\n"
        f"• Время: {o.get('when')}\n"
        f"• Пассажиров: {o.get('passengers')}\n"
        f"• Контакт: {o.get('contact')}"
        f"{price_line}\n\n"
        "Если всё верно — напишите «Подтверждаю». Для отмены — /cancel."
    )
    await update.message.reply_text(summary)
    return CONFIRM

async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.lower().startswith("подтвержда"):
        await update.message.reply_text("Напишите «Подтверждаю» или используйте /cancel.")
        return CONFIRM

    o = context.user_data["order"]
    user = update.effective_user
    save_order(o, user)
    save_user_stat(user)

    if ADMIN_CHAT_ID and user.id != ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                "🆕 Новый заказ\n"
                f"Подача: {o.get('pickup')}\nНазначение: {o.get('drop')}\n"
                f"Класс: {o.get('car')}\nВремя: {o.get('when')}\n"
                f"Пассажиров: {o.get('passengers')}\nКонтакт: {o.get('contact')}\n"
                f"От: @{user.username or 'user'} (ID {user.id})"
            )
        except Exception as e:
            log.warning(f"Admin notify failed: {e}")

    await update.message.reply_text("Заказ принят. Водитель свяжется с вами.")
    order_id = str(uuid4())[:8]
    amount = calc_amount(o)
    await update.message.reply_text(
        f"Сумма к оплате — {amount} ₽.",
        reply_markup=pay_keyboard(order_id, amount)
    )
    context.user_data.pop("order", None)
    return ConversationHandler.END

async def order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("order", None)
    await update.message.reply_text("Оформление отменено.")
    return ConversationHandler.END

# ========= ГЕОЛОКАЦИЯ ВНУТРИ КОНВЕРСАЦИИ (исправление) ====
async def order_pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    context.user_data["order"]["pickup"] = f"{lat:.6f},{lon:.6f}"
    await update.message.reply_text(
        "📍 Точка подачи сохранена.\nТеперь укажите адрес назначения "
        "или отправьте геолокацию места назначения."
    )
    return DROP

async def order_drop_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    lat, lon = loc.latitude, lon = loc.latitude, loc.longitude
    context.user_data["order"]["drop"] = f"{lat:.6f},{lon:.6f}"
    await update.message.reply_text("🎯 Точка назначения сохранена.\nВыберите класс автомобиля:",
                                    reply_markup=car_choice_kb())
    return CAR_CLASS

# ====================== ТЕКСТЫ ИЗ МЕНЮ ====================
async def on_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").lower()
    if "заказ" in txt:
        return await order_start(update, context)
    if "автопарк" in txt:
        return await fleet_cmd(update, context)
    if "оплата" in txt:
        return await pay_cmd(update, context)
    if "контакт" in txt:
        return await contact_cmd(update, context)
    if "отзыв" in txt:
        return await feedback_start(update, context)
    if "vip" in txt or "карта" in txt:
        return await vipcard_cmd(update, context)
    return await start(update, context)

# ====================== РЕГИСТРАЦИЯ =======================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    async def set_commands(app_):
        cmds = [
            BotCommand("start", "начать и меню"),
            BotCommand("order", "оформить заказ"),
            BotCommand("price", "тарифы"),
            BotCommand("fleet", "автопарк"),
            BotCommand("vip", "vip-опции"),
            BotCommand("status", "статус заказа"),
            BotCommand("contact", "контакты"),
            BotCommand("feedback", "оставить отзыв"),
            BotCommand("vipcard", "моя vip-карта"),
            BotCommand("pay", "оплата (демо)"),
            BotCommand("menu", "показать меню"),
            BotCommand("cancel", "отменить оформление"),
        ]
        await app_.bot.set_my_commands(cmds)
    app.post_init = set_commands

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("fleet", fleet_cmd))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("vipcard", vipcard_cmd))
    app.add_handler(CommandHandler("pay", pay_cmd))

    # Отзывы
    feedback_conv = ConversationHandler(
        entry_points=[CommandHandler("feedback", feedback_start)],
        states={
            FEEDBACK_RATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_rating)],
            FEEDBACK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_text)],
        },
        fallbacks=[CommandHandler("cancel", feedback_cancel)],
    )
    app.add_handler(feedback_conv)

    # Заказ
    order_conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            PICKUP: [
                MessageHandler(filters.LOCATION, order_pickup_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_pickup),
            ],
            DROP: [
                MessageHandler(filters.LOCATION, order_drop_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_drop),
            ],
            CAR_CLASS: [CallbackQueryHandler(on_car_choice, pattern=r"^car:")],
            WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_when)],
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_passengers)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_contact)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_confirm)],
        },
        fallbacks=[CommandHandler("cancel", order_cancel)],
        allow_reentry=True,
    )
    app.add_handler(order_conv)

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_car_choice, pattern=r"^car:"))
    app.add_handler(CallbackQueryHandler(on_pay_click, pattern=r"^pay:"))

    # Тексты из меню-кнопок
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_menu))

    return app

def main():
    app = build_app()
    log.info("Starting VIP taxi bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()