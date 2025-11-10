import os
import csv
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, ContextTypes, filters

# ========= НАСТРОЙКИ =========
# Твой Telegram ID для уведомлений (можно заменить через переменную окружения ADMIN_CHAT_ID)
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "143710784"))

# Модель для ИИ-ответов / перевода
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or None
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_ENABLED = False
try:
    if LLM_API_KEY:
        from openai import OpenAI
        client_kwargs = {}
        if OPENAI_BASE_URL:
            client_kwargs["base_url"] = OPENAI_BASE_URL
        client = OpenAI(api_key=LLM_API_KEY, **client_kwargs)
        LLM_ENABLED = True
except Exception:
    LLM_ENABLED = False

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN is required"

# ========= ЛОГИ =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ========= КЛАВИАТУРА =========
def main_menu():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛎 Заказ /order", callback_data="order"),
        InlineKeyboardButton("🌐 Перевод /translate", callback_data="translate"),
    ]])

# ========= КОМАНДЫ =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚖 *VIP Taxi Assistant*\n"
        "Привет! Помогу оформить заказ, перевести сообщение и ответить клиенту.\n\n"
        "Команды:\n"
        "/order — оформить заказ\n"
        "/translate — перевести текст RU/EN\n"
        "/info — информация о сервисе\n"
        "/help — помощь\n"
    )
    await (update.message or update.callback_query.message).reply_text(
        text, parse_mode="Markdown", reply_markup=main_menu()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Команды:*\n"
        "/start — приветствие и меню\n"
        "/order — оформить заказ VIP-такси\n"
        "/translate — перевод RU/EN\n"
        "/info — информация и контакты\n"
        "/cancel — отменить оформление заказа\n\n"
        "Напишите любое сообщение — отвечу как ИИ-ассистент (если задан LLM_API_KEY)."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ VIP Taxi Assistant\nПремиальные поездки (Mercedes S, Business, Minivan). "
        "Вода, зарядка, Wi-Fi. Заказы 24/7 — начните с /order.",
        parse_mode="Markdown",
    )

# ========= ОФОРМЛЕНИЕ ЗАКАЗА =========
PICKUP, DROP, CAR_CLASS, WHEN, CONTACT, CONFIRM = range(6)
CAR_CLASS_SET = {"business", "бизнес", "s", "s-класс", "minivan", "минивэн", "minivan/минивэн"}

def ensure_orders_csv(path="orders.csv"):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "user_id", "username", "pickup", "drop", "car_class", "when", "contact"])

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = {}
    await update.message.reply_text("📍 Укажите *адрес подачи* (улица, дом):", parse_mode="Markdown")
    return PICKUP

async def order_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text("🎯 Укажите *адрес назначения*:", parse_mode="Markdown")
    return DROP

async def order_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["drop"] = update.message.text.strip()
    await update.message.reply_text(
        "🚘 Выберите *класс авто*: Business / S / Minivan\n"
        "(можно написать: Бизнес, S-класс, Минивэн)",
        parse_mode="Markdown",
    )
    return CAR_CLASS

async def order_car_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    low = text.lower()
    # нормализуем
    if low in {"business", "бизнес"}:
        cls = "Business"
    elif low in {"s", "s-класс", "s-класc", "s class"}:
        cls = "S"
    elif low in {"minivan", "минивэн", "минивен"}:
        cls = "Minivan"
    else:
        await update.message.reply_text("Пожалуйста, выберите из вариантов: Business / S / Minivan.")
        return CAR_CLASS

    context.user_data["order"]["car_class"] = cls
    await update.message.reply_text("⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)")
    return WHEN

async def order_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["when"] = update.message.text.strip()
    await update.message.reply_text("📞 Оставьте контакт (имя и телефон):")
    return CONTACT

async def order_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["contact"] = update.message.text.strip()
    o = context.user_data["order"]
    summary = (
        "Проверьте заказ:\n"
        f"• Подача: {o['pickup']}\n"
        f"• Назначение: {o['drop']}\n"
        f"• Класс авто: {o['car_class']}\n"
        f"• Время: {o['when']}\n"
        f"• Контакт: {o['contact']}\n\n"
        "Если всё верно, напишите *Подтверждаю*. Для отмены — /cancel"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")
    return CONFIRM

async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().lower().startswith("подтвержда"):
        await update.message.reply_text("Напишите *Подтверждаю* или используйте /cancel.", parse_mode="Markdown")
        return CONFIRM

    ensure_orders_csv()
    o = context.user_data["order"]
    user = update.effective_user
    # Запись в CSV
    with open("orders.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            datetime.utcnow().isoformat(), user.id, user.username,
            o["pickup"], o["drop"], o["car_class"], o["when"], o["contact"]
        ])

    # Уведомление администратору
    admin_text = (
        "🆕 <b>Новый заказ</b>\n"
        f"👤 От: @{user.username or 'без_username'} (ID {user.id})\n"
        f"📍 Подача: {o['pickup']}\n"
        f"🏁 Назначение: {o['drop']}\n"
        f"🚘 Класс: {o['car_class']}\n"
        f"⏰ Время: {o['when']}\n"
        f"☎️ Контакт: {o['contact']}"
    )
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Failed to notify admin: {e}")

    await update.message.reply_text("✅ Заказ принят! Мы свяжемся с вами для подтверждения.")
    context.user_data.pop("order", None)
    return ConversationHandler.END

async def order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("order", None)
    await update.message.reply_text("Отменено. Могу помочь с новым заказом через /order.")
    return ConversationHandler.END

# ========= ПЕРЕВОД / ИИ-ЧАТ =========
async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LLM_ENABLED:
        await update.message.reply_text("Перевод недоступен: нет LLM_API_KEY.")
        return
    text = " ".join(context.args) if context.args else None
    if not text:
        await update.message.reply_text("Использование: `/translate ваш текст`", parse_mode="Markdown")
        return
    try:
        r = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Ты переводчик RU↔EN. Переводи кратко и точно."},
                {"role": "user", "content": text}
            ],
            temperature=0.2
        )
        await update.message.reply_text(r.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка перевода: {e}")

async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LLM_ENABLED:
        await update.message.reply_text("Бот запущен. Для ИИ-ответов добавьте LLM_API_KEY в переменные окружения.")
        return
    try:
        r = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Ты вежливый и лаконичный ассистент VIP-такси. Отвечай по делу."},
                {"role": "user", "content": update.message.text.strip()}
            ],
            temperature=0.4
        )
        await update.message.reply_text(r.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка LLM: {e}")

# ========= СБОРКА ПРИЛОЖЕНИЯ =========
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    async def set_commands(app_):
        cmds = [
            BotCommand("start", "запустить бота"),
            BotCommand("help", "помощь и описание функций"),
            BotCommand("order", "оформить заказ VIP-такси"),
            BotCommand("translate", "перевести текст ru/en"),
            BotCommand("info", "информация и контакты"),
            BotCommand("cancel", "отменить оформление заказа"),
        ]
        await app_.bot.set_my_commands(cmds)
    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))

    order_conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_pickup)],
            DROP: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_drop)],
            CAR_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_car_class)],
            WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_when)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_contact)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_confirm)],
        },
        fallbacks=[CommandHandler("cancel", order_cancel)],
        allow_reentry=True,
    )
    app.add_handler(order_conv)

    # общий ИИ-чат
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat))
    return app

def main():
    app = build_app()
    log.info("Starting VIP Taxi bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()