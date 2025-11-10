
import os
import csv
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, ContextTypes, filters

# ----- LLM (optional) -----
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN is required"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip_taxi_bot")

def menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛎 Заказ /order", callback_data="order"),
        InlineKeyboardButton("🌐 Перевод /translate", callback_data="translate"),
    ]])

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
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=menu_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Команды:*\n"
        "/start — приветствие и меню\n"
        "/order — оформить заказ VIP-такси\n"
        "/translate — перевод RU/EN\n"
        "/info — информация и контакты\n"
        "/cancel — отменить оформление заказа\n"
        "Напишите любое сообщение — отвечу как ИИ-ассистент."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ VIP Taxi Assistant\nПремиальные поездки на Mercedes S.\nЗаказы 24/7 — начните с /order.",
        parse_mode="Markdown"
    )

# ---- ORDER conversation ----
PICKUP, DROP, WHEN, CONTACT, CONFIRM = range(5)

def ensure_orders_csv(path="orders.csv"):
    if not os.path.exists(path):
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts","user_id","username","pickup","drop","when","contact"])

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = {}
    await update.message.reply_text("📍 Укажите *адрес подачи*:", parse_mode="Markdown")
    return PICKUP

async def order_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["pickup"] = update.message.text.strip()
    await update.message.reply_text("🎯 Укажите *адрес назначения*:", parse_mode="Markdown")
    return DROP

async def order_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["drop"] = update.message.text.strip()
    await update.message.reply_text("⏰ Когда подать авто? (например: сейчас, 19:30, завтра 10:00)")
    return WHEN

async def order_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["when"] = update.message.text.strip()
    await update.message.reply_text("📞 Контакт (имя и телефон):")
    return CONTACT

async def order_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["contact"] = update.message.text.strip()
    o = context.user_data["order"]
    await update.message.reply_text(
        f"Проверьте заказ:\n• Подача: {o['pickup']}\n• Назначение: {o['drop']}\n• Время: {o['when']}\n• Контакт: {o['contact']}\n\nНапишите *Подтверждаю* или /cancel",
        parse_mode="Markdown"
    )
    return CONFIRM

async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower().startswith("подтвержда"):
        ensure_orders_csv()
        o = context.user_data["order"]
        from datetime import datetime
        with open("orders.csv","a",newline="",encoding="utf-8") as f:
            w = csv.writer(f)
            u = update.effective_user
            w.writerow([datetime.utcnow().isoformat(), u.id, u.username, o["pickup"], o["drop"], o["when"], o["contact"]])
        await update.message.reply_text("✅ Заказ принят! Мы свяжемся для подтверждения.")
        context.user_data.pop("order", None)
        return ConversationHandler.END
    else:
        await update.message.reply_text("Напишите *Подтверждаю* или /cancel", parse_mode="Markdown")
        return CONFIRM

async def order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("order", None)
    await update.message.reply_text("Отменено. Готов помочь с новым заказом через /order.")
    return ConversationHandler.END

# ---- Translate & chat ----
async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LLM_ENABLED:
        await update.message.reply_text("Перевод недоступен: нет ключа LLM. Добавьте LLM_API_KEY.")
        return
    text = " ".join(context.args) if context.args else None
    if not text:
        await update.message.reply_text("Использование: `/translate ваш текст`", parse_mode="Markdown")
        return
    try:
        r = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role":"system","content":"Ты переводчик RU↔EN. Переводи кратко и точно."},
                {"role":"user","content":text}
            ],
            temperature=0.2
        )
        await update.message.reply_text(r.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка перевода: {e}")

async def chat_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LLM_ENABLED:
        await update.message.reply_text("Бот запущен. Для ИИ-ответов добавьте LLM_API_KEY.")
        return
    try:
        r = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role":"system","content":"Ты вежливый ассистент VIP-такси. Отвечай по делу."},
                {"role":"user","content":update.message.text.strip()}
            ],
            temperature=0.4
        )
        await update.message.reply_text(r.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка LLM: {e}")

def build_app():
    app = Application.builder().token(BOT_TOKEN).build()

    async def set_cmds(app_):
        cmds = [
            BotCommand("start","запустить бота"),
            BotCommand("help","помощь и описание функций"),
            BotCommand("order","оформить заказ VIP-такси"),
            BotCommand("translate","перевести текст ru/en"),
            BotCommand("info","информация и контакты"),
            BotCommand("cancel","отменить оформление заказа")
        ]
        await app_.bot.set_my_commands(cmds)
    app.post_init = set_cmds

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))

    conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_pickup)],
            DROP: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_drop)],
            WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_when)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_contact)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_confirm)],
        },
        fallbacks=[CommandHandler("cancel", order_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_fallback))
    return app

def main():
    app = build_app()
    log.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
