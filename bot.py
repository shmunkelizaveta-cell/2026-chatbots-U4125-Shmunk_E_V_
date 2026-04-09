import asyncio
import html
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from logging.handlers import RotatingFileHandler


def setup_logging(log_file: str = "bot.log") -> None:
    """
    Настройка логирования:
    - вывод в консоль
    - вывод в файл bot.log (с ротацией)
    Формат включает: дату, время, имя логгера, уровень, сообщение.
    """
    log_format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Чтобы не получать дубли при повторном запуске в одной сессии
    if root_logger.handlers:
        root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,  # ~2MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Чуть “приглушим” шумные логгеры зависимостей
    logging.getLogger("httpx").setLevel(logging.WARNING)


logger = logging.getLogger("telegram_news_bot")

# ============================================================
# Telegram News Bot + NewsAPI
# ============================================================
#
# Этот файл специально написан максимально простым:
# - новости берём из NewsAPI через requests
# - показываем 5 новостей по категориям
# - выводим: заголовок, описание, кликабельную ссылку
# - если у новости нет “реальной” ссылки (url не начинается с http) — пропускаем её
# - избранное сохраняем в JSON
#
# Переменные окружения (в .env):
# - BOT_TOKEN: токен Telegram-бота (BotFather)
# - NEWSAPI_KEY: ключ NewsAPI (https://newsapi.org)
# - FAVORITES_FILE: файл JSON для избранного (по умолчанию favorites.json)


# ----------------------------
# Настройки
# ----------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()
FAVORITES_FILE = os.getenv("FAVORITES_FILE", "favorites.json").strip()

NEWSAPI_TOP_URL = "https://newsapi.org/v2/top-headlines"
NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"

# Хотим русскоязычные новости:
# - top-headlines: берём Россию (country=ru)
# - everything: language=ru
NEWSAPI_COUNTRY = "ru"
NEWSAPI_LANGUAGE = "ru"

NEWS_LIMIT = 5  # сколько новостей показывать пользователю
HTTP_TIMEOUT_SECONDS = 8  # таймаут запросов к API


@dataclass(frozen=True)
class NewsItem:
    title: str
    description: str
    url: str
    image_url: str = ""


# Категории бота -> отображаемые названия
CATEGORY_TITLES: Dict[str, str] = {
    # базовые (как было)
    "politics": "Политика",
    "sport": "Спорт",
    "economy": "Экономика",
    "science": "Наука",

    # дополнительные
    "world": "Мировые новости",
    "society": "Общество",
    "russia": "Россия / локальные новости",
    "local": "Локальные новости",
    "tech": "Технологии",
    "ai": "Искусственный интеллект",
    "space": "Космос",
    "finance": "Финансы",
    "crypto": "Криптовалюты",
    "movies": "Кино и сериалы",
    "music": "Музыка",
    "games": "Игры",
    "health": "Здоровье",
    "eco": "Экология",
    "education": "Образование",
    "facts": "Интересные факты",
    "top": "Главное за день",
    "trends": "Тренды",
}

# Для некоторых категорий есть встроенные категории top-headlines,
# а для остальных используем endpoint /everything с запросом (q) на русском.
#
# Важно: "politics"/"economy" напрямую в NewsAPI нет, поэтому маппим:
TOP_HEADLINES_CATEGORY_MAP: Dict[str, str] = {
    # базовые
    "politics": "general",
    "sport": "sports",
    "economy": "business",
    "science": "science",
    # дополнительные
    "top": "general",
    "trends": "general",
    "health": "health",
    "tech": "technology",
}

EVERYTHING_QUERY_MAP: Dict[str, str] = {
    # общие / мир / общество
    "world": "мир OR международные OR международный OR зарубежные",
    "society": "общество OR происшествия OR события OR люди",
    "russia": "Россия OR РФ OR Москва OR Санкт-Петербург",
    "local": "местные новости OR регион OR город",

    # технологии
    "ai": "искусственный интеллект OR ИИ OR нейросеть OR нейросети",
    "space": "космос OR ракета OR спутник OR NASA OR SpaceX OR Роскосмос",

    # финансы
    "finance": "финансы OR банки OR инвестиции OR рынок OR акции",
    "crypto": "криптовалюта OR биткоин OR bitcoin OR ethereum OR блокчейн",

    # развлечения
    "movies": "кино OR фильм OR сериал OR премьера",
    "music": "музыка OR альбом OR концерт OR артист",
    "games": "игры OR видеоигры OR игровой OR релиз игры",

    # полезные темы
    "eco": "экология OR климат OR окружающая среда",
    "education": "образование OR школа OR университет OR ЕГЭ OR обучение",

    # вау-эффект
    "facts": "интересные факты OR ученые выяснили OR удивительный факт",
}


# ----------------------------
# Валидация ссылок
# ----------------------------

def is_valid_http_url(url: str) -> bool:
    """Минимальная проверка, чтобы ссылка была “реальной” и открывалась."""
    if not url:
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")


# ----------------------------
# Избранное (JSON)
# ----------------------------

def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_favorites(path: str) -> Dict[str, Any]:
    """Безопасная загрузка избранного из JSON."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        # Если файл повреждён/недоступен — бот продолжит работать
        return {}


def save_favorites(path: str, data: Dict[str, Any]) -> None:
    """Сохраняет избранное в JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_favorite(user_id: int, category: str, item: NewsItem, path: str) -> None:
    favorites = load_favorites(path)
    key = str(user_id)
    favorites.setdefault(key, [])

    record = {
        "title": item.title,
        "description": item.description,
        "url": item.url,
        "category": category,
        "saved_at": _utc_now_iso(),
    }

    # Не дублируем по URL
    existing_urls = {x.get("url") for x in favorites[key] if isinstance(x, dict)}
    if record["url"] in existing_urls:
        return

    favorites[key].append(record)
    save_favorites(path, favorites)


def list_favorites(user_id: int, path: str) -> List[Dict[str, Any]]:
    favorites = load_favorites(path)
    items = favorites.get(str(user_id), [])
    return items if isinstance(items, list) else []


# ----------------------------
# NewsAPI
# ----------------------------

class NewsApiError(RuntimeError):
    """Понятная ошибка для проблем с NewsAPI."""

def _request_newsapi(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Общий запрос к NewsAPI с одинаковой обработкой ошибок."""
    try:
        logger.info("NewsAPI request %s params=%s", url, {k: v for k, v in params.items() if k != "apiKey"})
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logger.error("NewsAPI network error", exc_info=True)
        raise NewsApiError("NewsAPI не отвечает (ошибка сети).") from e

    if resp.status_code != 200:
        # NewsAPI обычно возвращает JSON с полем message
        try:
            payload = resp.json()
            message = str(payload.get("message", "")).strip()
            if message:
                logger.error("NewsAPI error response: %s", message)
                raise NewsApiError(message)
        except ValueError:
            pass
        logger.error("NewsAPI HTTP error: %s", resp.status_code)
        raise NewsApiError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        logger.error("NewsAPI response is not JSON", exc_info=True)
        raise NewsApiError("Не удалось прочитать ответ NewsAPI (не JSON).") from e

    return data if isinstance(data, dict) else {}


def _parse_articles(data: Dict[str, Any]) -> List[NewsItem]:
    articles = data.get("articles", [])
    if not isinstance(articles, list):
        return []

    items: List[NewsItem] = []
    for a in articles:
        if not isinstance(a, dict):
            continue

        title = str(a.get("title") or "").strip()
        description = str(a.get("description") or "").strip()
        url = str(a.get("url") or "").strip()
        image_url = str(a.get("urlToImage") or "").strip()

        if not title:
            continue
        if not is_valid_http_url(url):
            continue

        # Картинку используем только если это реальный URL
        if not is_valid_http_url(image_url):
            image_url = ""

        items.append(NewsItem(title=title, description=description, url=url, image_url=image_url))
        if len(items) >= NEWS_LIMIT:
            break

    return items


def fetch_news_from_newsapi(category_key: str) -> List[NewsItem]:
    """
    Получает новости из NewsAPI (top-headlines).

    Важно: функция синхронная (requests), поэтому вызываем её через asyncio.to_thread(),
    чтобы не блокировать event loop бота.
    """
    if not NEWSAPI_KEY:
        raise NewsApiError("Не задан NEWSAPI_KEY в .env")

    logger.info("Fetch news category=%s", category_key)

    # 1) Если категория поддерживается top-headlines — используем её
    if category_key in TOP_HEADLINES_CATEGORY_MAP:
        params = {
            "country": NEWSAPI_COUNTRY,
            "category": TOP_HEADLINES_CATEGORY_MAP[category_key],
            # Берём “с запасом”, чтобы после фильтрации по URL всё равно набрать 5
            "pageSize": max(NEWS_LIMIT * 3, 20),
            "apiKey": NEWSAPI_KEY,
        }
        data = _request_newsapi(NEWSAPI_TOP_URL, params)
        items = _parse_articles(data)
        logger.info("Fetched %d items (top-headlines) for %s", len(items), category_key)
        return items

    # 2) Иначе — everything с русским языком и запросом (q)
    q = EVERYTHING_QUERY_MAP.get(category_key)
    if not q:
        raise NewsApiError("Неизвестная категория")

    params = {
        "language": NEWSAPI_LANGUAGE,
        "sortBy": "publishedAt",
        "q": q,
        "pageSize": max(NEWS_LIMIT * 3, 20),
        "apiKey": NEWSAPI_KEY,
    }
    data = _request_newsapi(NEWSAPI_EVERYTHING_URL, params)
    items = _parse_articles(data)
    logger.info("Fetched %d items (everything) for %s", len(items), category_key)
    return items


def build_news_message_html(category: str, items: List[NewsItem]) -> str:
    """
    Формирует сообщение с новостями.
    Важно: выводим только новости с валидным URL (http/https).
    """
    category_name = CATEGORY_TITLES.get(category, category)
    valid_items = [it for it in items if is_valid_http_url(it.url)]

    if not valid_items:
        return f"Пока нет новостей с корректными ссылками в категории «{html.escape(category_name)}»."

    lines: List[str] = [f"<b>Новости: {html.escape(category_name)}</b>", ""]
    for i, it in enumerate(valid_items, start=1):
        lines.append(f"{i}. <b>{html.escape(it.title)}</b>")
        if it.description:
            lines.append(f"   {html.escape(it.description)}")
        # Ссылка реальная, берём из NewsAPI
        lines.append(f'   <a href="{html.escape(it.url)}">Открыть источник</a>')
        lines.append("")

    lines.append("Сохранить в избранное:")
    lines.append("<code>/save &lt;категория&gt; &lt;номер&gt;</code>")
    lines.append("Пример: <code>/save politics 2</code>")

    return "\n".join(lines).strip()

EMOJI_CATEGORY: Dict[str, str] = {
    "top": "🔥",
    "trends": "📊",
    "world": "🌐",
    "russia": "🏙",
    "local": "🏠",
    "society": "🏠",
    "politics": "🏛",
    "economy": "📈",
    "finance": "💰",
    "crypto": "🪙",
    "tech": "💻",
    "ai": "🤖",
    "space": "🚀",
    "sport": "🏅",
    "science": "🔬",
    "health": "🏥",
    "eco": "🌱",
    "education": "🧠",
    "movies": "🎬",
    "music": "🎵",
    "games": "🎮",
    "facts": "😂",
}


def build_categories_keyboard() -> InlineKeyboardMarkup:
    """Inline-меню категорий (удобнее, чем вводить команды)."""
    order = [
        "top",
        "trends",
        "world",
        "russia",
        "society",
        "politics",
        "economy",
        "finance",
        "crypto",
        "tech",
        "ai",
        "space",
        "sport",
        "science",
        "health",
        "eco",
        "education",
        "movies",
        "music",
        "games",
        "facts",
        "local",
    ]

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for key in order:
        title = CATEGORY_TITLES.get(key, key)
        emoji = EMOJI_CATEGORY.get(key, "📰")
        row.append(InlineKeyboardButton(f"{emoji} {title}", callback_data=f"cat:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Нижняя панель управления
    rows.append(
        [
            InlineKeyboardButton("🗂 Категории", callback_data="menu:cats"),
        ]
    )

    return InlineKeyboardMarkup(rows)


def build_article_keyboard(category: str, index: int, url: str) -> InlineKeyboardMarkup:
    """Кнопки под конкретной новостью: читать + сохранить."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Читать", url=url)],
            [InlineKeyboardButton("⭐️ В избранное", callback_data=f"fav:{category}:{index}")],
        ]
    )


def build_article_caption_html(category: str, index: int, item: NewsItem) -> str:
    """Красивое форматирование одной новости."""
    emoji = EMOJI_CATEGORY.get(category, "📰")
    title = html.escape(item.title)
    desc = html.escape(item.description) if item.description else ""

    lines = [f"{emoji} <b>{index}. {title}</b>"]
    if desc:
        lines.append(f"🔹 {desc}")
    return "\n".join(lines).strip()


def is_paused(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Флаг паузы: если True — не отправляем ленту новостей."""
    return bool(context.user_data.get("paused", False))


def set_paused(context: ContextTypes.DEFAULT_TYPE, value: bool) -> None:
    context.user_data["paused"] = bool(value)


def get_feed_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Режим выдачи новостей:
    - "single": по одной новости + кнопка "Следующая"
    - "all": сразу все новости (как раньше)
    """
    mode = str(context.user_data.get("feed_mode", "single"))
    return mode if mode in {"single", "all"} else "single"


def set_feed_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    if mode in {"single", "all"}:
        context.user_data["feed_mode"] = mode


def _set_current_index(context: ContextTypes.DEFAULT_TYPE, category: str, index: int) -> None:
    cur = context.user_data.setdefault("current_index", {})
    cur[category] = int(index)


def _get_current_index(context: ContextTypes.DEFAULT_TYPE, category: str) -> int:
    cur = context.user_data.get("current_index", {})
    try:
        return int(cur.get(category, 0))
    except Exception:
        return 0


async def send_news_items(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category: str,
    items: List[NewsItem],
) -> None:
    """Отправляет 5 новостей: каждая — отдельным сообщением с кнопками."""
    assert update.effective_chat

    if is_paused(context):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏸ Сейчас пауза. Нажмите «▶️ Продолжить», чтобы снова получать новости.",
            reply_markup=MAIN_MENU,
        )
        return

    # Запоминаем последние новости для inline-сохранения и /save (на всякий случай)
    context.user_data.setdefault("last_news", {})[category] = items

    if not items:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Пока нет новостей в категории «{html.escape(CATEGORY_TITLES.get(category, category))}».",
            parse_mode=ParseMode.HTML,
            reply_markup=build_categories_keyboard(),
        )
        return

    header_emoji = EMOJI_CATEGORY.get(category, "📰")
    header = f"{header_emoji} <b>{html.escape(CATEGORY_TITLES.get(category, category))}</b>"

    mode = get_feed_mode(context)

    # Режим "по одной"
    if mode == "single":
        _set_current_index(context, category, 0)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{header}\nРежим: <b>по одной</b> (жмите «➡️ Следующая»)",
            parse_mode=ParseMode.HTML,
            reply_markup=build_categories_keyboard(),
            disable_web_page_preview=True,
        )
        await _send_single_article(update, context, category, start_from=0)
        return

    # Режим "все сразу"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{header}\nРежим: <b>все сразу</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_categories_keyboard(),
        disable_web_page_preview=True,
    )

    for i, item in enumerate(items, start=1):
        if is_paused(context):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⏸ Пауза включена. Нажмите «▶️ Продолжить», чтобы продолжить.",
                reply_markup=MAIN_MENU,
            )
            break

        caption = build_article_caption_html(category, i, item)
        keyboard = build_article_keyboard(category, i, item.url)

        # Если есть картинка — отправим фото с подписью (приятнее выглядит)
        if item.image_url:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=item.image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                # Если фото не отправилось (битый URL/Telegram не смог скачать) — просто текстом
                logging.exception("Failed to send article image, fallback to text")

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def _send_single_article(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    category: str,
    start_from: int,
) -> None:
    """Отправляет одну новость из сохранённого списка + кнопка «Следующая»."""
    assert update.effective_chat

    if is_paused(context):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏸ Сейчас пауза. Нажмите «▶️ Продолжить».",
            reply_markup=MAIN_MENU,
        )
        return

    last_news = context.user_data.get("last_news", {})
    items: List[NewsItem] = last_news.get(category, [])
    if not items:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Список новостей пуст. Откройте категорию заново.",
            reply_markup=build_categories_keyboard(),
        )
        return

    idx = start_from
    if idx < 0:
        idx = 0

    if idx >= len(items):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Новости закончились.\nНажмите «🔄 Обновить», чтобы загрузить свежие.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Обновить", callback_data=f"cat:{category}")],
                    [InlineKeyboardButton("🗂 Категории", callback_data="menu:cats")],
                ]
            ),
        )
        return

    item = items[idx]
    shown_number = idx + 1

    caption = build_article_caption_html(category, shown_number, item)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Читать", url=item.url)],
            [
                InlineKeyboardButton("⭐️ В избранное", callback_data=f"fav:{category}:{shown_number}"),
                InlineKeyboardButton("➡️ Следующая", callback_data=f"next:{category}"),
            ],
        ]
    )

    _set_current_index(context, category, idx)

    if item.image_url:
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=item.image_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception:
            logging.exception("Failed to send article image, fallback to text")

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ----------------------------
# UI: меню-кнопки
# ----------------------------

# Русские “кнопки” (reply keyboard)
BTN_CATEGORIES = "🗂 Категории"
BTN_FAVORITES = "⭐️ Избранное"
BTN_HELP = "❓ Помощь"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Продолжить"
BTN_STOP = "⏹ Стоп"
BTN_MODE_SINGLE = "📰 По одной"
BTN_MODE_ALL = "🗞 Все сразу"

# Быстрые категории (кнопки вместо команд)
BTN_TOP = "🔥 Главное"
BTN_TRENDS = "📊 Тренды"
BTN_WORLD = "🌐 Мир"
BTN_RUSSIA = "🏙 Россия"
BTN_TECH = "💻 Технологии"
BTN_AI = "🤖 ИИ"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(BTN_CATEGORIES), KeyboardButton(BTN_FAVORITES)],
        [KeyboardButton(BTN_TOP), KeyboardButton(BTN_TRENDS), KeyboardButton(BTN_WORLD)],
        [KeyboardButton(BTN_RUSSIA), KeyboardButton(BTN_TECH), KeyboardButton(BTN_AI)],
        [KeyboardButton(BTN_MODE_SINGLE), KeyboardButton(BTN_MODE_ALL)],
        [KeyboardButton(BTN_HELP), KeyboardButton(BTN_PAUSE), KeyboardButton(BTN_STOP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите категорию кнопкой…",
)


HELP_TEXT = (
    "Доступные команды:\n"
    "- /start — приветствие\n"
    "- /help — помощь\n"
    "- /news — выбор категории\n"
    "- /politics — новости политики\n"
    "- /sport — новости спорта\n"
    "- /economy — новости экономики\n"
    "- /science — новости науки\n"
    "- /world — мировые новости\n"
    "- /society — общество\n"
    "- /russia — Россия / локальные новости\n"
    "- /local — локальные новости\n"
    "- /tech — технологии\n"
    "- /ai — искусственный интеллект\n"
    "- /space — космос\n"
    "- /finance — финансы\n"
    "- /crypto — криптовалюты\n"
    "- /movies — кино и сериалы\n"
    "- /music — музыка\n"
    "- /games — игры\n"
    "- /health — здоровье\n"
    "- /eco — экология\n"
    "- /education — образование\n"
    "- /facts — интересные факты\n"
    "- /top — главное за день\n"
    "- /trends — тренды\n"
    "\n"
    "Избранное:\n"
    "- /save <категория> <номер> — сохранить новость в избранное\n"
    "- /favorites — показать избранные новости\n"
)


# ----------------------------
# Команды бота
# ----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    logger.info("Command /start user_id=%s chat_id=%s", getattr(update.effective_user, "id", None), getattr(update.effective_chat, "id", None))
    set_paused(context, False)
    # По умолчанию выдаём по одной новости — это удобнее.
    set_feed_mode(context, "single")
    await update.message.reply_text(
        "Привет! Я бот новостей.\n"
        "Показываю реальные новости из NewsAPI.\n"
        "Выберите категорию кнопкой ниже или используйте /news.",
        reply_markup=MAIN_MENU,
    )
    await update.message.reply_text(
        "🗂 Меню категорий:",
        reply_markup=build_categories_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    await update.message.reply_text(HELP_TEXT, reply_markup=MAIN_MENU)


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    await update.message.reply_text("🗂 Выберите категорию кнопками:", reply_markup=build_categories_keyboard())


async def send_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str) -> None:
    assert update.message

    try:
        items = await asyncio.to_thread(fetch_news_from_newsapi, category)
    except NewsApiError as e:
        logger.error("NewsApiError category=%s user_id=%s", category, getattr(update.effective_user, "id", None))
        await update.message.reply_text(
            f"Не удалось получить новости: {e}",
            reply_markup=MAIN_MENU,
        )
        return
    except Exception:
        logger.error("Unexpected error while fetching news category=%s", category, exc_info=True)
        await update.message.reply_text(
            "Не удалось получить новости (непредвиденная ошибка). Попробуйте позже.",
            reply_markup=MAIN_MENU,
        )
        return

    await send_news_items(update, context, category, items)


async def politics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "politics")


async def sport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "sport")


async def economy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "economy")


async def science(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "science")


# Дополнительные категории
async def world(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "world")


async def society(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "society")


async def russia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "russia")


async def local(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "local")


async def tech(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "tech")


async def ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "ai")


async def space(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "space")


async def finance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "finance")


async def crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "crypto")


async def movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "movies")


async def music(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "music")


async def games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "games")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "health")


async def eco(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "eco")


async def education(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "education")


async def facts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "facts")


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "top")


async def trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_category(update, context, "trends")


async def on_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка выбора категории через inline-кнопки."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith("cat:"):
        return

    category = data.split(":", 1)[1].strip()
    logger.info(
        "UI category click category=%s user_id=%s chat_id=%s",
        category,
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
    )
    if category not in CATEGORY_TITLES:
        await query.edit_message_text("Неизвестная категория.")
        return

    try:
        items = await asyncio.to_thread(fetch_news_from_newsapi, category)
    except NewsApiError as e:
        await query.edit_message_text(f"Не удалось получить новости: {e}")
        return
    except Exception:
        logging.exception("Unexpected error while fetching news")
        await query.edit_message_text("Не удалось получить новости. Попробуйте позже.")
        return

    # Отправляем новости в чат (не редактируем старое меню — так удобнее)
    await send_news_items(update, context, category, items)


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопка для возврата к меню категорий."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    logger.info(
        "UI menu categories click user_id=%s chat_id=%s",
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
    )
    await query.edit_message_reply_markup(reply_markup=build_categories_keyboard())


async def on_next_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Следующая» для режима «по одной новости»."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("next:"):
        return

    category = data.split(":", 1)[1].strip()
    logger.info(
        "UI next click category=%s user_id=%s chat_id=%s",
        category,
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
    )
    if category not in CATEGORY_TITLES:
        await query.answer("Неизвестная категория.", show_alert=True)
        return

    current = _get_current_index(context, category)
    await _send_single_article(update, context, category, start_from=current + 1)


async def on_favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохранение в избранное по inline-кнопке."""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if not data.startswith("fav:"):
        return

    await query.answer()

    if not update.effective_user:
        await query.answer("Не удалось определить пользователя.", show_alert=True)
        return

    # Формат: fav:<category>:<index>
    parts = data.split(":")
    if len(parts) != 3:
        await query.answer("Некорректные данные кнопки.", show_alert=True)
        return

    category = parts[1].strip()
    try:
        idx = int(parts[2])
    except ValueError:
        await query.answer("Некорректный номер.", show_alert=True)
        return

    logger.info(
        "UI favorite click category=%s index=%s user_id=%s chat_id=%s",
        category,
        idx,
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
    )

    last_news = context.user_data.get("last_news", {})
    items: List[NewsItem] = last_news.get(category, [])

    if not items or idx < 1 or idx > len(items):
        await query.answer("Список новостей устарел. Откройте категорию заново.", show_alert=True)
        return

    try:
        add_favorite(update.effective_user.id, category, items[idx - 1], FAVORITES_FILE)
    except OSError:
        logger.error(
            "Failed to save favorite (file error) user_id=%s category=%s index=%s",
            update.effective_user.id,
            category,
            idx,
            exc_info=True,
        )
        await query.answer("Не удалось сохранить (ошибка файла).", show_alert=True)
        return

    logger.info("Saved favorite user_id=%s category=%s index=%s", update.effective_user.id, category, idx)
    await query.answer("Сохранено в избранное.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """⏹ Стоп: убираем клавиатуру и ставим паузу."""
    assert update.message
    logger.info(
        "UI stop user_id=%s chat_id=%s",
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
    )
    set_paused(context, True)
    await update.message.reply_text(
        "⏹ Остановлено.\nЧтобы снова начать — напишите /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка русских кнопок (reply keyboard)."""
    assert update.message
    text = (update.message.text or "").strip()
    logger.info(
        "UI text button=%r user_id=%s chat_id=%s",
        text,
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
    )

    if text == BTN_HELP:
        await help_command(update, context)
        return

    if text == BTN_CATEGORIES:
        await update.message.reply_text("🗂 Категории:", reply_markup=build_categories_keyboard())
        return

    if text == BTN_FAVORITES:
        await favorites_command(update, context)
        return

    if text == BTN_PAUSE:
        set_paused(context, True)
        await update.message.reply_text("⏸ Пауза включена. Нажмите «▶️ Продолжить».", reply_markup=MAIN_MENU)
        return

    if text == BTN_RESUME:
        set_paused(context, False)
        await update.message.reply_text("▶️ Продолжаем. Выберите категорию.", reply_markup=MAIN_MENU)
        return

    if text == BTN_STOP:
        await stop_command(update, context)
        return

    if text == BTN_MODE_SINGLE:
        set_feed_mode(context, "single")
        await update.message.reply_text(
            "✅ Режим: показываю <b>по одной</b> новости (кнопка «➡️ Следующая»).",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if text == BTN_MODE_ALL:
        set_feed_mode(context, "all")
        await update.message.reply_text(
            "✅ Режим: показываю новости <b>все сразу</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    quick_map = {
        BTN_TOP: "top",
        BTN_TRENDS: "trends",
        BTN_WORLD: "world",
        BTN_RUSSIA: "russia",
        BTN_TECH: "tech",
        BTN_AI: "ai",
    }
    if text in quick_map:
        set_paused(context, False)
        category = quick_map[text]
        try:
            items = await asyncio.to_thread(fetch_news_from_newsapi, category)
        except NewsApiError as e:
            await update.message.reply_text(f"Не удалось получить новости: {e}", reply_markup=MAIN_MENU)
            return
        except Exception:
            logging.exception("Unexpected error while fetching news")
            await update.message.reply_text("Не удалось получить новости. Попробуйте позже.", reply_markup=MAIN_MENU)
            return
        await send_news_items(update, context, category, items)
        return

    await update.message.reply_text(
        "Не понял сообщение. Нажмите «🗂 Категории» или используйте /help.",
        reply_markup=MAIN_MENU,
    )


def _parse_save_args(args: List[str]) -> Optional[Tuple[str, int]]:
    if len(args) != 2:
        return None
    category = args[0].strip().lower()
    if category not in CATEGORY_TITLES:
        return None
    try:
        index = int(args[1])
    except ValueError:
        return None
    return category, index


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    if not update.effective_user:
        await update.message.reply_text("Не удалось определить пользователя.", reply_markup=MAIN_MENU)
        return

    parsed = _parse_save_args(context.args)
    if not parsed:
        await update.message.reply_text(
            "Неверный формат.\n"
            "Используйте: /save <категория> <номер>\n"
            "Пример: /save science 1",
            reply_markup=MAIN_MENU,
        )
        return

    category, idx = parsed

    last_news = context.user_data.get("last_news", {})
    items: List[NewsItem] = last_news.get(category, [])

    if not items:
        await update.message.reply_text(
            "Сначала откройте новости нужной категории (например, /science), "
            "а потом сохраните нужный номер через /save.",
            reply_markup=MAIN_MENU,
        )
        return

    if idx < 1 or idx > len(items):
        await update.message.reply_text(
            f"Номер новости должен быть от 1 до {len(items)}.",
            reply_markup=MAIN_MENU,
        )
        return

    try:
        add_favorite(update.effective_user.id, category, items[idx - 1], FAVORITES_FILE)
    except OSError:
        logger.error(
            "Failed to save favorite via /save user_id=%s category=%s index=%s",
            update.effective_user.id,
            category,
            idx,
            exc_info=True,
        )
        await update.message.reply_text(
            "Не получилось сохранить избранное (ошибка файла). Попробуйте позже.",
            reply_markup=MAIN_MENU,
        )
        return

    logger.info("Saved favorite via /save user_id=%s category=%s index=%s", update.effective_user.id, category, idx)
    await update.message.reply_text("Сохранено в избранное.", reply_markup=MAIN_MENU)


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    if not update.effective_user:
        await update.message.reply_text("Не удалось определить пользователя.", reply_markup=MAIN_MENU)
        return

    items = list_favorites(update.effective_user.id, FAVORITES_FILE)
    if not items:
        await update.message.reply_text("У вас пока нет избранных новостей.", reply_markup=MAIN_MENU)
        return

    # Тут оставим Markdown: просто печатаем строки (заголовок/описание/ссылка).
    lines = ["**Ваше избранное**", ""]
    for i, rec in enumerate(items, start=1):
        if not isinstance(rec, dict):
            continue
        cat = CATEGORY_TITLES.get(str(rec.get("category", "")), str(rec.get("category", "")))
        title = str(rec.get("title", ""))
        desc = str(rec.get("description", "") or "")
        url = str(rec.get("url", ""))

        lines.append(f"{i}. [{cat}] {title}")
        if desc:
            lines.append(f"   {desc}")
        if url:
            lines.append(f"   {url}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=MAIN_MENU,
        parse_mode=ParseMode.MARKDOWN,
    )


# ----------------------------
# Ошибки и запуск
# ----------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Произошла ошибка. Попробуйте позже.",
            reply_markup=MAIN_MENU,
        )


def main() -> None:
    setup_logging("bot.log")
    logger.info("Starting bot…")

    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN. Укажите токен в .env.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды (сохранены)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("politics", politics))
    app.add_handler(CommandHandler("sport", sport))
    app.add_handler(CommandHandler("economy", economy))
    app.add_handler(CommandHandler("science", science))

    # дополнительные категории
    app.add_handler(CommandHandler("world", world))
    app.add_handler(CommandHandler("society", society))
    app.add_handler(CommandHandler("russia", russia))
    app.add_handler(CommandHandler("local", local))
    app.add_handler(CommandHandler("tech", tech))
    app.add_handler(CommandHandler("ai", ai))
    app.add_handler(CommandHandler("space", space))
    app.add_handler(CommandHandler("finance", finance))
    app.add_handler(CommandHandler("crypto", crypto))
    app.add_handler(CommandHandler("movies", movies))
    app.add_handler(CommandHandler("music", music))
    app.add_handler(CommandHandler("games", games))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("eco", eco))
    app.add_handler(CommandHandler("education", education))
    app.add_handler(CommandHandler("facts", facts))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("trends", trends))

    # Избранное (сохранено)
    app.add_handler(CommandHandler("save", save_command))
    app.add_handler(CommandHandler("favorites", favorites_command))

    # Inline UI
    app.add_handler(CallbackQueryHandler(on_category_callback, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(on_favorite_callback, pattern=r"^fav:"))
    app.add_handler(CallbackQueryHandler(on_next_callback, pattern=r"^next:"))
    app.add_handler(CallbackQueryHandler(on_menu_callback, pattern=r"^menu:cats$"))

    # Reply-keyboard UI (русские кнопки)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Ошибки
    app.add_error_handler(on_error)

    # В разных версиях python-telegram-bot доступность Update.ALL_TYPES может отличаться.
    # Эта конструкция сохраняет поведение (получать все типы апдейтов), но не ломает запуск.
    try:
        allowed_updates = Update.ALL_TYPES  # type: ignore[attr-defined]
    except Exception:
        allowed_updates = None

    app.run_polling(allowed_updates=allowed_updates)


if __name__ == "__main__":
    main()