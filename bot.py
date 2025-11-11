# -*- coding: utf-8 -*-
import os
import json
from google.oauth2.service_account import Credentials
import gspread

# Загружаем ключ из переменной Railway
credentials_info = json.loads(os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"])
credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

# Авторизация и подключение к Google Sheets
gc = gspread.authorize(credentials)
sheet = gc.open("orders").sheet1

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    Defaults,
    filters,
)

# ====================== НАСТРОЙКИ ==========================
BRAND_NAME = "VIP taxi"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip_taxi_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

# Google Sheets
SHEET_ID = os.getenv("SHEET_ID", "")
GOOGLE_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_JSON", "")

# Чат/канал водителей (группа/канал): пример -1001234567890
DRIVERS_CHANNEL_ID = os.getenv("DRIVERS_CHANNEL_ID", "")

# Тарифы (часовые для вывода)
PRICES_STR = {
    "Maybach W223": "7000 ₽/ч",
    "Maybach W222": "4000 ₽/ч",
    "S-Class W223": "5000 ₽/ч",
    "S-Class W222": "3000 ₽/ч",
    "Business": "2000 ₽/ч",
    "Minivan": "3000 ₽/ч",
}

# Фотки автопарка (нейтральные Unsplash)
CAR_PHOTOS = {
    "S-Class W222": "https://images.unsplash.com/photo-1615732045871-8db6d1dc8723",
    "Maybach W222": "https://images.unsplash.com/photo-1624784194858-4e1cb2e54c56",
    "S-Class W223": "https://images.unsplash.com/photo-1649254362283-5c9b83a3d31f",
    "Maybach W223": "https://images.unsplash.com/photo-1650659020204-3d8e60d2dcbb",
    "Business": "https://images.unsplash.com/photo-1606813902915-5c2b66f04e8e",
    "Minivan": "https://images.unsplash.com/photo-1618401471383-5e00764f9a72",
}
CAR_DESCR = {
    "S-Class W222": "Mercedes-Benz S-Class (W222). Салфетки, вода, зарядки.",
    "Maybach W222": "Mercedes-Maybach (W222). Индивидуальные кресла; вода, зарядки.",
    "S-Class W223": "Mercedes-Benz S-Class (W223). Новое поколение; вода, зарядки.",
    "Maybach W223": "Mercedes-Maybach (W223). Флагман; вода, зарядки.",
    "Business": "Mercedes E-Class / BMW 5. Комфорт, вода, зарядки.",
    "Minivan": "Mercedes V-Class. До 6 пассажиров; вода, зарядки.",
}

# Параметры оценки цены по расстоянию
BASE_PER_KM = int(os.getenv("BASE_PER_KM", "70"))
START_FEE   = int(os.getenv("START_FEE", "500"))

# Платежи
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "")

# ИИ
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# ====================== УТИЛИТЫ ===========================
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    to_rad = math.pi / 180
    dlat = (lat2 - lat1) * to_rad
    dlon = (lon2 - lon1) * to_rad
    a = (math.sin(dlat/2)**2 +
         math.cos(lat1*to_rad) * math.cos(lat2*to_rad) * math.sin(dlon/2)**2)
    return 2 * R * math.asin(math.sqrt(a))

def estimate_price_km(distance_km: float, car_class: str) -> int:
    mult = {
        "Maybach W223": 2.5,
        "Maybach W222": 2.0,
        "S-Class W223": 2.0,
        "S-Class W222": 1.6,
        "Business": 1.2,
        "Minivan": 1.6,
    }.get(car_class, 1.0)
    return int(START_FEE + max(1.0, distance_km) * BASE_PER_KM * mult)

def gsheet():
    if not (SHEET_ID and GOOGLE_SERVICE_JSON):
        raise RuntimeError("SHEET_ID/GOOGLE_SERVICE_JSON are required for Google Sheets")
    scopes = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
    info = json.loads(GOOGLE_SERVICE_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

def order_id():
    return hex(int(time.time()))[2:]

def class_caption(car_name: str) -> str:
    return f"{car_name}\n{CAR_DESCR.get(car_name,'')}\n{PRICES_STR.get(car_name,'')}"

def pay_keyboard(order_id_: str, amount: int):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Оплатить {amount} ₽", callback_data=f"pay:{order_id_}:{amount}")]]
    )

# ====================== ИИ ================================
def llm_chat(prompt: str, user_id: int) -> str:
    if not LLM_API_KEY:
        return ("❗️ ИИ недоступен: администратору нужно добавить переменную "
                "LLM_API_KEY в Railway.")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    system = (
        "Ты ассистент VIP-такси. Отвечай кратко и по делу. "
        "Если просят оформить поездку — предложи /order. Поддерживай RU/EN."
    )
    data = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=30)
        if r.status_code != 200:
            return f"Ошибка LLM: {r.status_code} — {r.text}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Ошибка LLM: {e}"

# ====================== КЛАВИАТУРЫ ========================
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🛎 Заказ"), KeyboardButton("🚗 Автопарк")],
            [KeyboardButton("💳 Оплата"), KeyboardButton("📞 Контакты")],
            [KeyboardButton("⭐ Отзыв"), KeyboardButton("🪪 VIP-карта")],
            [KeyboardButton("🤖 ИИ-помощник"), KeyboardButton("📍 Геолокация", request_location=True)],
        ],
        resize_keyboard=True,
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

# ====================== СОСТОЯНИЯ =========================
PICKUP, DROP, CAR_CLASS, WHEN, PASSENGERS, CONTACT, CONFIRM = range(7)
FEEDBACK_RATING, FEEDBACK_TEXT = range(2)
AI_CHAT = 99

# ====================== КОМАНДЫ ===========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        f"<b>Добро пожаловать в {BRAND_NAME}</b>\n"
        "Ваш комфорт — наш приоритет.\n\n"
        "Выберите действие в меню ниже.\n"
        "Или отправьте геолокацию — подача по вашей точке."
    )
    await (update.message or update.callback_query.message).reply_text(txt, reply_markup=main_menu())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["<b>Тарифы:</b>"]
    for k, v in PRICES_STR.items():
        lines.append(f"• {k}: {v}")
    lines.append("\nОпции: ожидание, встреча с табличкой, детское кресло — по запросу.")
    await update.message.reply_text("\n".join(lines))

async def fleet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for cls in ["S-Class W222","Maybach W222","S-Class W223","Maybach W223","Business","Minivan"]:
        url = CAR_PHOTOS[cls]
        caption = class_caption(cls)
        try:
            await update.message.reply_photo(photo=url, caption=caption)
        except Exception:
            await update.message.reply_text(caption)

async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Позвонить", url="tel:+7XXXXXXXXXX")]])
    await update.message.reply_text(
        "Диспетчер: пишите здесь — ответим в чате.\nРезервный номер: <code>+7 XXX XXX-XX-XX</code>",
        reply_markup=kb
    )

# ---------- ИИ ----------
async def ask_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ИИ-помощник включён. Напишите вопрос.\nВыход — /cancel.")
    return AI_CHAT

async def ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    await update.message.reply_text(llm_chat(q, update.effective_user.id))
    return AI_CHAT

# ---------- Отзывы ----------
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
    try:
        with open("feedback.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f); 
            if f.tell() == 0:
                w.writerow(["ts","user_id","username","rating","comment"])
            w.writerow([datetime.utcnow().isoformat(), update.effective_user.id, update.effective_user.username, rating, comment])
    except Exception as e:
        log.warning(f"feedback save error: {e}")
    await update.message.reply_text("Спасибо. Мы ценим ваше мнение.")
    return ConversationHandler.END

async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отзыв отменён.")
    return ConversationHandler.END

# ====================== ОФОРМЛЕНИЕ ЗАКАЗА =================
def _set_order(o, key, val): o[key] = val; return o

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = {"order_id": order_id()}
    await update.message.reply_text(
        "Укажите <b>адрес подачи</b> или отправьте геолокацию кнопкой «📍 Геолокация».",
    )
    return PICKUP

async def order_pickup_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    o = _set_order(context.user_data.setdefault("order", {}), "pickup", update.message.text.strip())
    await update.message.reply_text("Укажите <b>адрес назначения</b> или отправьте геолокацию.")
    return DROP

async def order_pickup_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    o = context.user_data.setdefault("order", {})
    o["pickup"] = f"{loc.latitude:.6f},{loc.longitude:.6f}"
    o["pickup_lat"], o["pickup_lon"] = loc.latitude, loc.longitude
    await update.message.reply_text("✅ Локация подачи принята.\nТеперь укажите <b>адрес назначения</b> или отправьте геолокацию.")
    return DROP

async def order_drop_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_order(context.user_data["order"], "drop", update.message.text.strip())
    await update.message.reply_text("🚗 Выберите класс авто:", reply_markup=ReplyKeyboardMarkup(
        [["Maybach W223","Maybach W222"],["S-Class W223","S-Class W222"],["Business","Minivan"]],
        resize_keyboard=True
    ))
    return CAR_CLASS

async def order_drop_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    o = context.user_data["order"]
    o["drop"] = f"{loc.latitude:.6f},{loc.longitude:.6f}"
    o["drop_lat"], o["drop_lon"] = loc.latitude, loc.longitude
    await update.message.reply_text("🚗 Выберите класс авто:", reply_markup=ReplyKeyboardMarkup(
        [["Maybach W223","Maybach W222"],["S-Class W223","S-Class W222"],["Business","Minivan"]],
        resize_keyboard=True
    ))
    return CAR_CLASS

async def on_car_choice_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, car = q.data.split(":", 1)
    context.user_data["order"]["car"] = car
    caption = class_caption(car)
    url = CAR_PHOTOS.get(car)
    try:
        await q.message.reply_photo(photo=url, caption=caption)
    except Exception:
        await q.message.reply_text(caption)
    await q.message.reply_text("⏰ Когда подать автомобиль? (например: сейчас / 19:30 / завтра 10:00)")
    return WHEN

async def on_car_choice_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cls = update.message.text.strip()
    context.user_data["order"]["car"] = cls
    await update.message.reply_text("⏰ Когда подать автомобиль? (например: сейчас / 19:30 / завтра 10:00)")
    return WHEN

async def order_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["when"] = update.message.text.strip()
    await update.message.reply_text("👥 Сколько пассажиров?")
    return PASSENGERS

async def order_passengers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["passengers"] = update.message.text.strip()
    await update.message.reply_text("☎️ Оставьте контакт (имя и телефон):")
    return CONTACT

async def order_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    o = context.user_data["order"]
    o["contact"] = update.message.text.strip()

    # Расстояние (если есть обе геоточки)
    dist = 8.0
    if all(k in o for k in ("pickup_lat","pickup_lon","drop_lat","drop_lon")):
        dist = round(haversine_km(o["pickup_lat"], o["pickup_lon"], o["drop_lat"], o["drop_lon"]), 1)
    o["distance_km"] = dist

    # Оценка
    o["est_price"] = estimate_price_km(dist, o.get("car", "Business"))

    summary = (
        f"<b>Проверьте заказ:</b>\n"
        f"• Подача: {o.get('pickup')}\n"
        f"• Назначение: {o.get('drop')}\n"
        f"• Класс: {o.get('car')}\n"
        f"• Расстояние: ~{o['distance_km']} км\n"
        f"• Оценка: ~{o['est_price']} ₽\n"
        f"• Время: {o.get('when')}\n"
        f"• Пассажиров: {o.get('passengers')}\n"
        f"• Контакт: {o.get('contact')}\n\n"
        f"Если всё верно — напишите «Подтверждаю». Для отмены — /cancel."
    )
    await update.message.reply_text(summary)
    return CONFIRM

async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.lower().startswith("подтвержда"):
        await update.message.reply_text("Напишите «Подтверждаю» или используйте /cancel.")
        return CONFIRM

    o = context.user_data["order"]
    # Запись в Google Sheets
    try:
        sh = gsheet()
        w  = sh.worksheet("Orders")
        w.append_row([
            o.get("order_id",""), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            str(update.effective_chat.id), o.get("contact",""),
            o.get("pickup",""), o.get("drop",""),
            o.get("car",""), o.get("distance_km",0),
            o.get("est_price",0), "new", "", "",
            o.get("when",""), o.get("passengers","")
        ])
    except Exception as e:
        log.warning(f"Sheets error: {e}")
        if ADMIN_CHAT_ID:
            try: await context.bot.send_message(ADMIN_CHAT_ID, f"Sheets error: {e}")
            except: pass

    # Сообщение в служебный чат водителей
    if DRIVERS_CHANNEL_ID:
        txt = (f"🆕 <b>Новый заказ</b> #{o.get('order_id','')}\n"
               f"• Подача: {o.get('pickup','')}\n"
               f"• Назначение: {o.get('drop','')}\n"
               f"• Класс: {o.get('car','')}\n"
               f"• Расстояние: ~{o.get('distance_km',0)} км\n"
               f"• Оценка: ~{o.get('est_price',0)} ₽\n"
               f"• Время: {o.get('when','')}\n"
               f"• Пассажиров: {o.get('passengers','')}")
        try:
            await context.bot.send_message(int(DRIVERS_CHANNEL_ID), txt, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Driver alert error: {e}")
            if ADMIN_CHAT_ID:
                try: await context.bot.send_message(ADMIN_CHAT_ID, f"Driver alert error: {e}")
                except: pass

    await update.message.reply_text("✅ Заказ принят. Водитель свяжется с вами.")

    # Оплата: если провайдер не задан — демо-кнопка. Иначе — Telegram Payments.
    amount = int(o.get("est_price", 3500))
    oid = str(uuid4())[:8]
    if not PAYMENTS_PROVIDER_TOKEN:
        await update.message.reply_text(
            f"Сумма к оплате — {amount} ₽.", reply_markup=pay_keyboard(oid, amount)
        )
    else:
        title = f"Поездка {o.get('car','')}"
        desc  = f"Оценка: ~{amount} ₽. Итог зависит от фактического маршрута."
        await update.message.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=title, description=desc, payload=json.dumps({"order_id": o.get("order_id","")}),
            provider_token=PAYMENTS_PROVIDER_TOKEN, currency="RUB",
            prices=[LabeledPrice("Поездка", amount*100)]
        )

    context.user_data["order"] = {}
    return ConversationHandler.END

async def order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = {}
    await update.message.reply_text("Оформление отменено ✅", reply_markup=main_menu())
    return ConversationHandler.END

# ---------- Оплата (демо-кнопка callback) ----------
async def on_pay_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, order_id_, amount = q.data.split(":")
    await q.edit_message_text(f"✅ Оплата заказа #{order_id_} на {amount} ₽ выполнена (демо).")

# ---------- Telegram Payments ----------
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Оплата получена. Спасибо!", reply_markup=main_menu())

# ====================== ТЕКСТЫ МЕНЮ =======================
async def on_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").lower()
    if "заказ" in txt:
        return await order_start(update, context)
    if "автопарк" in txt:
        return await fleet_cmd(update, context)
    if "оплата" in txt:
        # запустим оплату по текущему заказу, если он есть
        o = context.user_data.get("order")
        if o and "est_price" in o:
            amount = int(o["est_price"])
            await update.message.reply_text(
                f"Сумма к оплате — {amount} ₽.",
                reply_markup=pay_keyboard(str(uuid4())[:8], amount)
            )
        else:
            await update.message.reply_text("Оформите заказ — и я посчитаю сумму к оплате.")
        return
    if "контакт" in txt:
        return await contact_cmd(update, context)
    if "отзыв" in txt:
        return await feedback_start(update, context)
    if "vip" in txt or "карта" in txt:
        uid = update.effective_user.id
        await update.message.reply_text(f"🪪 VIP Card\nID: {uid}\nСтатус: Premium")
        return
    if "ии" in txt or "помощник" in txt:
        return await ask_start(update, context)
    return await start(update, context)

# ====================== РЕГИСТРАЦИЯ =======================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).defaults(
        Defaults(parse_mode=ParseMode.HTML)
    ).build()

    async def set_commands(app_):
        cmds = [
            BotCommand("start", "начать и меню"),
            BotCommand("order", "оформить заказ"),
            BotCommand("price", "тарифы"),
            BotCommand("fleet", "автопарк"),
            BotCommand("ask", "включить ИИ-помощника"),
            BotCommand("contact", "контакты"),
            BotCommand("feedback", "оставить отзыв"),
            BotCommand("cancel", "отменить оформление"),
        ]
        await app_.bot.set_my_commands(cmds)
    app.post_init = set_commands

    # Заказ (Conversation)
    order_conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start),
                      MessageHandler(filters.Regex("^🛎 Заказ$"), order_start)],
        states={
            PICKUP: [
                MessageHandler(filters.LOCATION, order_pickup_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_pickup_text),
            ],
            DROP: [
                MessageHandler(filters.LOCATION, order_drop_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_drop_text),
            ],
            CAR_CLASS: [
                CallbackQueryHandler(on_car_choice_inline, pattern=r"^car:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_car_choice_text),
            ],
            WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_when)],
            PASSENGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_passengers)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_contact)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_confirm)],
        },
        fallbacks=[CommandHandler("cancel", order_cancel)],
        allow_reentry=True,
    )
    app.add_handler(order_conv)

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("fleet", fleet_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("feedback", feedback_start))
    app.add_handler(CommandHandler("ask", ask_start))

    # ИИ диалог
    ai_conv = ConversationHandler(
        entry_points=[CommandHandler("ask", ask_start)],
        states={ AI_CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_message)] },
        fallbacks=[CommandHandler("cancel", order_cancel)],
        allow_reentry=True,
    )
    app.add_handler(ai_conv)

    # Оплата (демо-кнопка)
    app.add_handler(CallbackQueryHandler(on_pay_click, pattern=r"^pay:"))

    # Telegram Payments
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Тексты из меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_menu))

    return app

def main():
    app = build_app()
    log.info("Starting VIP taxi bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()