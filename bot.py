# -*- coding: utf-8 -*-
"""
VIP taxi bot — базовая версия с регистрацией водителей
python-telegram-bot v20+
"""

import os
import logging
from typing import Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("vip_taxi_bot")


# ---------- НАСТРОЙКИ ----------

BRAND_NAME = "VIP taxi"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # можно не использовать
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # если понадобится
SHEET_ID = os.environ.get("SHEET_ID")  # если будешь подключать таблицу

# ID ГРУППЫ ДЛЯ РЕГИСТРАЦИИ ВОДИТЕЛЕЙ
DRIVER_REG_CHAT_ID = -5062249297

assert BOT_TOKEN, "BOT_TOKEN is required"

# Пример тарифов (пока просто константа, можно не использовать)
PRICES = {
    "Maybach W223": 7000,
    "Maybach W222": 4000,
    "S-Class W223": 5000,
    "S-Class W222": 3000,
    "Business": 2000,
}


# ---------- СТАРТ / МЕНЮ ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "друг"

    kb = ReplyKeyboardMarkup(
        [
            ["🚕 Заказать поездку"],
            ["👨‍✈️ Стать водителем"],
        ],
        resize_keyboard=True,
    )

    text = (
        f"Привет, {name}! Это бот {BRAND_NAME}.\n\n"
        "Выберите действие:\n"
        "• 🚕 Заказать поездку\n"
        "• 👨‍✈️ Стать водителем"
    )

    await update.message.reply_text(text, reply_markup=kb)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if "Стать водителем" in text:
        # Стартуем диалог регистрации (ConversationHandler перехватит)
        return await reg_driver_start(update, context)

    if "Заказать поездку" in text:
        await update.message.reply_text(
            "Функция заказа поездки пока не настроена.\n"
            "Но регистрация водителей уже работает ✅"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Пока я понимаю только:\n"
        "• 🚕 Заказать поездку\n"
        "• 👨‍✈️ Стать водителем"
    )
    return ConversationHandler.END


# ---------- РЕГИСТРАЦИЯ ВОДИТЕЛЕЙ ----------

(
    REG_NAME,
    REG_PHONE,
    REG_CAR,
    REG_DOCS,
    REG_CONFIRM,
) = range(5)


def _normalize_phone(text: str) -> Optional[str]:
    """Приводим номер к виду +7ХХХХХХХХХ."""
    import re

    digits = re.sub(r"\D", "", text or "")
    if len(digits) < 10:
        return None
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return "+" + digits


# /reg_driver или кнопка "Стать водителем"
async def reg_driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["driver_reg"] = {"photos": []}

    await update.message.reply_text(
        "👋 Добро пожаловать в регистрацию водителей VIP taxi.\n\n"
        "1️⃣ Напишите *ФИО полностью*:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_NAME


# Шаг 1 — ФИО
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


# Шаг 2 — телефон
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


# Шаг 3 — авто
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


# Шаг 4 — фото
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


# Шаг 5 — подтверждение и отправка в группу
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


# Решение админа в группе
async def driver_moderation_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Одобрение
    if data.startswith("drv_app_"):
        user_id = int(data.split("_")[-1])

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🎉 Ваша заявка на регистрацию в VIP taxi *одобрена*.\n"
                    "Мы свяжемся с вами для дальнейших шагов."
                ),
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
                text=(
                    "❌ Ваша заявка на регистрацию в VIP taxi *отклонена*.\n"
                    "Вы можете отправить новую заявку позже."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"Не удалось написать пользователю {user_id}: {e}")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(query.message.text + "\n\n❌ Водитель ОТКЛОНЁН.")
        return


# ---------- ЗАПУСК ПРИЛОЖЕНИЯ ----------

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # /start
    application.add_handler(CommandHandler("start", start))

    # регистрация водителей как отдельная команда / кнопка
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("reg_driver", reg_driver_start),
            MessageHandler(
                filters.Regex("Стать водителем") & filters.TEXT, reg_driver_start
            ),
        ],
        states={
            REG_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_driver_name)
            ],
            REG_PHONE: [
                MessageHandler(filters.CONTACT, reg_driver_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_driver_phone),
            ],
            REG_CAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_driver_car)
            ],
            REG_DOCS: [
                MessageHandler(filters.PHOTO, reg_driver_docs),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_driver_docs),
            ],
            REG_CONFIRM: [
                CallbackQueryHandler(reg_driver_confirm, pattern="^drv_reg_"),
            ],
        },
        fallbacks=[],
    )
    application.add_handler(reg_conv)

    # обработка решения в группе
    application.add_handler(
        CallbackQueryHandler(
            driver_moderation_action, pattern="^(drv_app_|drv_rej_)"
        )
    )

    # обработка кнопок меню (если не попали в диалог)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)
    )

    log.info("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()