
# VIP Taxi Bot (Telegram) — Ready for Railway

Готовый Telegram-бот для VIP-такси: команды `/start`, `/help`, `/info`, `/order`, `/translate` и обычный чат с ИИ.
Работает с `python-telegram-bot 21.x` и любым OpenAI-совместимым API.

## Переменные окружения
- `BOT_TOKEN` — токен от @BotFather
- `LLM_API_KEY` — API ключ модели (OpenAI/Groq/Together и т.п.)
- `OPENAI_BASE_URL` — (опц.) базовый URL совместимого API
- `MODEL_NAME` — по умолчанию `gpt-4o-mini`

## Запуск локально
```bash
pip install -r requirements.txt
python bot.py
```

## Деплой на Railway
1. Загрузите файлы репозитория в GitHub.
2. Railway → New Project → Deploy from GitHub → выбрать репозиторий.
3. Project → Variables: `BOT_TOKEN`, `LLM_API_KEY`, (опц.) `OPENAI_BASE_URL`, `MODEL_NAME`.
4. Нажмите Deploy. `Procfile` уже настроен (`worker: python bot.py`).

## Команды
/start, /help, /info, /order, /translate, /cancel
