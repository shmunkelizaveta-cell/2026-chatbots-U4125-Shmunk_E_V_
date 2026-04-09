# Telegram News Bot (python-telegram-bot)

Простой Telegram-бот на Python, который показывает новости по категориям:
политика, спорт, экономика, наука. Есть меню с кнопками и сохранение новостей в избранное (JSON).

## Возможности

- Команды:
  - `/start` — приветствие
  - `/help` — помощь
  - `/news` — выбор категории
  - `/politics` — новости политики
  - `/sport` — новости спорта
  - `/economy` — новости экономики
  - `/science` — новости науки
- Дополнительно:
  - `/save <категория> <номер>` — сохранить новость в избранное (JSON)
  - `/favorites` — показать избранные новости

## Установка

```bash
cd telegram_news_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Настройка токена и NewsAPI

1) Скопируйте пример переменных окружения:

```bash
cp .env.example .env
```

2) Откройте `.env` и укажите:

- `BOT_TOKEN` — токен вашего бота из BotFather
- `NEWSAPI_KEY` — ключ NewsAPI (берётся на `https://newsapi.org`)
- `FAVORITES_FILE` — файл для избранного (по умолчанию `favorites.json`)

## Запуск

```bash
python bot.py
```

## Где лежат новости

Новости берутся из NewsAPI (`top-headlines`) через `requests` и показываются по категориям.
По требованию выводится только 5 новостей: заголовок, описание, ссылка.

Соответствие категорий бота категориям NewsAPI:

- `politics` → `general`
- `economy` → `business`
- `sport` → `sports`
- `science` → `science`

