import os
import csv
import logging
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ========= НАСТРОЙКИ =========
# Твой Telegram ID для уведомлений (можно переопределить переменной окружения ADMIN_CHAT_ID)
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "143710784"))

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN is required"

# Настройки LLM (для перевода и NLU)
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

# ========= ЛОГИ =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip_taxi_bot")

# ========= СОСТОЯНИЯ DIALOG =========
PICKUP, DROP, CAR_CLASS, WHEN, CONTACT, CONFIRM = range(6)
ORDER_SLOTS = ["pickup", "drop", "car_class", "when", "passengers", "contact"]

# ========= УТИЛИТЫ =========
def main_menu():
    """Кнопки-команды (не callback): шлют текст /order и /translate."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🛎 Заказ /order"), KeyboardButton("🌐 Перевод /translate")]],
        resize_keyboard=True,
    )

def normalize_car_class(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower().strip()
    if t in {"business", "бизнес"}:
        return "Business"
    if t in {"s", "s-класс", "s class", "s-класc"}:
        return "S"
    if t in {"minivan", "минивэн", "минивен"}:
        return "Minivan"
    return None

def ensure_orders_csv(path="orders.csv"):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["ts", "user_id", "username", "pickup", "drop", "car_class", "when", "passengers", "contact"]
            )

# ========= КОМАНДЫ =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚖 *VIP Taxi Assistant*\n"
        "Привет! Помогу оформить заказ, перевести сообщение и ответить клиенту.\n\n"
        "Нажмите кнопки снизу или введите команды:\n"
        "• /order — оформить заказ\n"
        "• /translate — перевод RU/EN\n"
        "• /info — информация о сервисе\n"
        "• /help — помощь\n"
    )
    await (update.message or update.callback_query.message).reply_text(
        text, parse_mode="Markdown", reply_markup=main_menu()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Команды:*\n"
        "/start — приветствие и меню\n"
        "/order — оформить заказ VIP-такси\n"
        "/translate — перевод RU/EN (если нажали без текста, пришлите текст следом)\n"
        "/info — информация и контакты\n"
        "/cancel — отменить оформление заказа\n\n"
        "Любое сообщение могу распознать как заказ (адреса/время/класс/пассажиры) и довести оформление до подтверждения."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ VIP Taxi Assistant\nПремиальные поездки (Mercedes S, Business, Minivan). "
        "Вода, зарядка, Wi-Fi. Заказы 24/7 — начните с /order.",
    )

# ========= NLU (распознавание намерения и слотов) =========
async def nlu_extract(text: str) -> dict:
    """
    Возвращает dict:
    {
      "intent": "order|translate|chitchat",
      "pickup": "...", "drop": "...", "when": "...",
      "car_class": "Business|S|Minivan",
      "passengers": 1,
      "contact": "..."
    }
    """
    # Без LLM — простая эвристика
    if not LLM_ENABLED:
        low = text.lower()
        intent = "order" if any(k in low for k in
                                ["заказ", "такси", "машина", "s-класс", "микроавтобус", "минивэн", "аэропорт", "шереметьево"]) \
                 else ("translate" if "/translate" in low else "chitchat")
        return {"intent": intent}

    sys_prompt = (
        "Ты NLU-экстрактор для бота VIP-такси. Верни ЧИСТЫЙ JSON (без пояснений) "
        "с намерением пользователя и извлечёнными слотами заказа.\n"
        "intent: one of ['order','translate','chitchat'].\n"
        "Слоты (если можно извлечь): pickup, drop, when, car_class (Business/S/Minivan), passengers (int), contact.\n"
        "Если слота нет — не указывай или оставь пустым. Верни ТОЛЬКО JSON."
    )
    r = client.chat.completions.create(
        model=MODEL_NAME,
        response_format={"type": "json_object"},
        temperature=0.1,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
    )
    import json
    try:
        data = json.loads(r.choices[0].message.content)
    except Exception as e:
        log.warning(f"NLU parse failed: {e}")
        return {"intent": "chitchat"}

    # нормализация
    if "car_class" in data and data["car_class"]:
        data["car_class"] = normalize_car_class(str(data["car_class"])) or data["car_class"]
    if "passengers" in data and data["passengers"] not in (None, ""):
        try:
            data["passengers"] = int(data["passengers"])
        except Exception:
            data["passengers"] = None
    return data

async def ask_next_missing_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Спрашиваем следующий необходимый слот. Возвращаем состояние."""
    o = context.user_data.setdefault("order", {})
    if not o.get("pickup"):
        await update.message.reply_text("📍 Укажите *адрес подачи* (улица, дом):", parse_mode="Markdown")
        return PICKUP
    if not o.get("drop"):
        await update.message.reply_text("🎯 Укажите *адрес назначения*:", parse_mode="Markdown")
        return DROP
    if not o.get("car_class"):
        await update.message.reply_text("🚘 Выберите *класс авто*: Business / S / Minivan", parse_mode="Markdown")
        return CAR_CLASS
    if not o.get("when"):
        await update.message.reply_text("⏰ Когда подать автомобиль? (например: сейчас, 19:30, завтра 10:00)")
        return WHEN
    if not o.get("passengers"):
        await update.message.reply_text("👥 Сколько пассажиров будет ехать? (число)")
        return WHEN  # используем WHEN для простоты
    if not o.get("contact"):
        await update.message.reply_text("📞 Оставьте контакт (имя и телефон):")
        return CONTACT

    # Всё собрано — сводка и подтверждение
    summary = (
        "Проверьте заказ:\n"
        f"• Подача: {o['pickup']}\n"
        f"• Назначение: {o['drop']}\n"
        f"• Класс авто: {o['car_class']}\n"
        f"• Время: {o['when']}\n"
        f"• Пассажиров: {o['passengers']}\n"
        f"• Контакт: {o['contact']}\n\n"
        "Если всё верно, напишите *Подтверждаю*. Для отмены — /cancel"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")
    return CONFIRM

# ========= СЦЕНАРИЙ ЗАКАЗА =========
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = {}
    await update.message.reply_text("📍 Укажите *адрес подачи* (улица, дом):", parse_mode="Markdown")
    return PICKUP

async def order_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["pickup"] = update.message.text.strip()
    return await ask_next_missing_slot(update, context)

async def order_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["drop"] = update.message.text.strip()
    return await ask_next_missing_slot(update, context)

async def order_car_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cls = normalize_car_class(update.message.text.strip())
    if not cls:
        await update.message.reply_text("Пожалуйста, выберите из вариантов: Business / S / Minivan.")
        return CAR_CLASS
    context.user_data["order"]["car_class"] = cls
    return await ask_next_missing_slot(update, context)

async def order_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    o = context.user_data["order"]
    # если ждём число пассажиров
    if not o.get("passengers"):
        # попробуем извлечь число
        digits = "".join(ch for ch in txt if ch.isdigit())
        if digits:
            try:
                o["passengers"] = int(digits)
                return await ask_next_missing_slot(update, context)
            except Exception:
                pass
    # иначе — это время
    if not o.get("when"):
        o["when"] = txt
        return await ask_next_missing_slot(update, context)
    # если и время уже есть, а нас всё ещё сюда прислали — просто спросим следующее
    return await ask_next_missing_slot(update, context)

async def order_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["contact"] = update.message.text.strip()
    return await ask_next_missing_slot(update, context)

async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().lower().startswith("подтвержда"):
        await update.message.reply_text("Напишите *Подтверждаю* или используйте /cancel.", parse_mode="Markdown")
        return CONFIRM

    ensure_orders_csv()
    o = context.user_data["order"]
    user = update.effective_user

    with open("orders.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.utcnow().isoformat(), user.id, user.username,
            o["pickup"], o["drop"], o["car_class"], o["when"], o["passengers"], o["contact"]
        ])

    # уведомления
    admin_text = (
        "🆕 <b>Новый заказ</b>\n"
        f"👤 От: @{user.username or 'без_username'} (ID {user.id})\n"
        f"📍 Подача: {o['pickup']}\n"
        f"🏁 Назначение: {o['drop']}\n"
        f"🚘 Класс: {o['car_class']}\n"
        f"👥 Пассажиров: {o['passengers']}\n"
        f"⏰ Время: {o['when']}\n"
        f"☎️ Контакт: {o['contact']}"
    )
    if ADMIN_CHAT_ID and user.id != ADMIN_CHAT_ID:
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

# ========= ПЕРЕВОД =========
async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LLM_ENABLED:
        await update.message.reply_text("Перевод недоступен: нет LLM_API_KEY.")
        return
    text = " ".join(context.args) if context.args else None
    if not text:
        context.user_data["await_translate"] = True
        await update.message.reply_text("Отправьте текст, и я переведу RU↔EN.")
        return
    try:
        r = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Ты переводчик RU↔EN. Переводи кратко и точно."},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
        )
        await update.message.reply_text(r.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Ошибка перевода: {e}")

# ========= ОБЩИЙ ХЭНДЛЕР СООБЩЕНИЙ (ИИ+NLU) =========
async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # режим «жду текст для перевода»
    if context.user_data.get("await_translate"):
        context.user_data.pop("await_translate", None)
        if not LLM_ENABLED:
            await update.message.reply_text("Перевод недоступен: нет LLM_API_KEY.")
            return
        try:
            r = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": "Ты переводчик RU↔EN. Переводи кратко и точно."},
                    {"role": "user", "content": text},
                ],
                temperature=0.2,
            )
            await update.message.reply_text(r.choices[0].message.content)
        except Exception as e:
            await update.message.reply_text(f"Ошибка перевода: {e}")
        return

    # NLU: распознать намерение и извлечь слоты
    nlu = await nlu_extract(text)
    intent = nlu.get("intent") or "chitchat"

    if intent == "order":
        o = context.user_data.setdefault("order", {})
        for k in ("pickup", "drop", "when", "contact"):
            if nlu.get(k):
                o[k] = nlu[k]
        if nlu.get("car_class"):
            o["car_class"] = normalize_car_class(nlu["car_class"]) or nlu["car_class"]
        if nlu.get("passengers"):
            try:
                o["passengers"] = int(nlu["passengers"])
            except Exception:
                pass
        await ask_next_missing_slot(update, context)
        return

    if intent == "translate":
        await translate_cmd(update, context)
        return

    # короткий ответ ИИ, не сбивая сценарий
    if not LLM_ENABLED:
        await update.message.reply_text("Готов помочь с заказом. Нажмите «🛎 Заказ /order» или введите /order.")
        return
    r = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system",
             "content": "Ты вежливый ассистент VIP-такси. Отвечай очень кратко и предлагай оформить заказ командой /order."},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
    )
    await update.message.reply_text(r.choices[0].message.content)

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

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))

    # Диалог заказа
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

    # ИИ / NLU для всех прочих текстов
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat))

    return app

def main():
    app = build_app()
    log.info("Starting VIP Taxi bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()