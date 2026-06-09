import requests
import telegram
import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram.constants import ParseMode
from playwright.async_api import async_playwright

# ================== ЗАГРУЗКА КОНФИГУРАЦИИ ==================
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GNEWS_API_KEY]):
    print("❌ Ошибка: не загружены переменные окружения. Проверьте Secrets в GitHub.")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 3          # Сколько новостей отправлять за один запуск
MAX_AGE_HOURS = 24                # Не отправлять новости старше X часов
SEND_INTERVAL_SEC = 20            # Пауза между отправками
CHANNEL_HEADER = "⚽ Футбольные новости"
CONTACT_TEXT = "📩 Связаться"
CONTACT_URL = "https://t.me/tl33054"
GROUP_TEXT = "💬 Обсудить"
GROUP_URL = "https://t.me/DONG8NY"

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def format_time(time_str: str) -> str:
    """Приводит ISO-время к читаемому формату (МСК = UTC+3)."""
    if not time_str:
        return "неизвестно"
    try:
        # Убираем 'Z' и добавляем +00:00
        if time_str.endswith('Z'):
            time_str = time_str[:-1] + '+00:00'
        dt = datetime.fromisoformat(time_str)
        # Переводим в Московское время (UTC+3)
        msk_tz = timezone(timedelta(hours=3))
        dt_msk = dt.astimezone(msk_tz)
        return dt_msk.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return time_str.split('T')[0]

def is_recent(published_at: str) -> bool:
    """Проверяет, что новость не старше MAX_AGE_HOURS."""
    if not published_at:
        return False
    try:
        if published_at.endswith('Z'):
            published_at = published_at[:-1] + '+00:00'
        pub_dt = datetime.fromisoformat(published_at)
        now = datetime.now(timezone.utc)
        age = now - pub_dt
        return age.total_seconds() <= MAX_AGE_HOURS * 3600
    except Exception:
        return False

def extract_hashtags(title: str) -> str:
    """Примитивное выделение ключевых слов для хэштегов (без jieba)."""
    words = re.findall(r'\b\w{4,}\b', title.lower())
    # Убираем стоп-слова
    stop = {'это', 'все', 'после', 'перед', 'который', 'также', 'еще', 'уже'}
    tags = [w for w in words if w not in stop][:3]
    return " ".join(f"#{tag}" for tag in tags) if tags else "#футбол"

# ================== ПОЛУЧЕНИЕ НОВОСТЕЙ ИЗ GNews ==================
def fetch_gnews():
    """Запрашивает свежие футбольные новости на русском языке."""
    print("📡 Запрос к GNews API...")
    # Ключевые слова: футбол, возможны варианты
    query = "(football OR soccer OR футбол)"
    url = (
        f"https://gnews.io/api/v4/search?q={query}"
        f"&lang=ru&country=ru&max=10&apikey={GNEWS_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"⚠️ GNews вернул код {resp.status_code}")
            return []
        data = resp.json()
        articles = data.get("articles", [])
        print(f"✅ Получено {len(articles)} статей")
        return articles
    except Exception as e:
        print(f"❌ Ошибка при запросе к GNews: {e}")
        return []

# ================== ПАРСИНГ ПОЛНОЙ СТАТЬИ (ПЕРВЫЕ АБЗАЦЫ) ==================
async def scrape_details(url: str):
    """Возвращает (полное время публикации, краткое содержание) со страницы новости."""
    pub_time = ""
    summary = ""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            # Пытаемся найти время публикации
            time_selectors = [
                'meta[property="article:published_time"]',
                'meta[name="publish-date"]',
                'time',
                '.publish-date',
                '.date'
            ]
            for sel in time_selectors:
                el = await page.query_selector(sel)
                if el:
                    attr = await el.get_attribute("content") or await el.get_attribute("datetime")
                    if attr:
                        pub_time = attr.strip()
                        break
                    txt = await el.inner_text()
                    if txt:
                        pub_time = txt.strip()
                        break
            # Берём первые 2 абзаца из тела статьи
            content_selectors = [
                'article', '.article-body', '.post-content',
                '.entry-content', '.content', '#main-content'
            ]
            for sel in content_selectors:
                container = await page.query_selector(sel)
                if container:
                    paragraphs = await container.query_selector_all('p')
                    texts = []
                    for p in paragraphs[:3]:
                        txt = await p.inner_text()
                        if txt and len(txt) > 40:
                            texts.append(txt.strip())
                    if texts:
                        summary = "\n\n".join(texts[:2])
                        break
        except Exception as e:
            print(f"⚠️ Ошибка при парсинге {url}: {e}")
        finally:
            await browser.close()
    return pub_time, summary

# ================== ОТПРАВКА В TELEGRAM ==================
async def send_article(bot, article, custom_summary=""):
    """Формирует и отправляет один пост в канал."""
    title = article.get("title")
    url = article.get("url")
    image = article.get("image")
    source = article.get("source", {}).get("name", "источник")
    published = article.get("publishedAt")
    description = article.get("description", "")

    if not title or not url:
        return False

    # Если не удалось распарсить summary, используем description из API
    final_summary = custom_summary if custom_summary else description
    if not final_summary:
        final_summary = "👉 Нажмите «читать далее», чтобы открыть полную новость."

    # Время публикации
    time_str = format_time(published)
    hashtags = extract_hashtags(title)

    # Построение текста
    caption_parts = [
        f"{CHANNEL_HEADER} {hashtags}\n",
        f"<b>{title}</b>\n",
        final_summary[:800],  # ограничим, чтобы не превысить лимит Telegram
        "",
        f"📅 {time_str} | 📰 {source}",
        f"🔗 <a href='{url}'>Читать полностью</a>",
        f"{CONTACT_TEXT}: <a href='{CONTACT_URL}'>{CONTACT_TEXT}</a> | {GROUP_TEXT}: <a href='{GROUP_URL}'>{GROUP_TEXT}</a>"
    ]
    caption = "\n".join(p for p in caption_parts if p)

    # Обрезаем, если больше 1024 символов (лимит caption)
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    try:
        if image:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=image,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
        print(f"✅ Отправлено: {title[:60]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        # Пробуем без картинки и HTML
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=caption,
                disable_web_page_preview=True
            )
            return True
        except Exception as e2:
            print(f"❌ И резервная отправка провалилась: {e2}")
            return False

# ================== ГЛАВНАЯ ФУНКЦИЯ (ОДИН ЗАПУСК ДЛЯ GITHUB ACTIONS) ==================
async def main():
    print("🚀 Запуск футбольного новостного бота (serverless mode)")
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

    # Получаем свежие новости
    articles = fetch_gnews()
    if not articles:
        print("Нет новостей от GNews.")
        return

    # Фильтруем: только недавние и уникальные в пределах этого запуска
    recent_articles = []
    seen_urls = set()
    for art in articles:
        url = art.get("url")
        pub = art.get("publishedAt")
        if url and url not in seen_urls and is_recent(pub):
            seen_urls.add(url)
            recent_articles.append(art)

    if not recent_articles:
        print("Нет свежих новостей (старше 24 часов или дубликаты).")
        return

    print(f"Найдено {len(recent_articles)} свежих новостей. Отправлю не более {MAX_ARTICLES_PER_RUN}.")
    sent = 0
    for art in recent_articles[:MAX_ARTICLES_PER_RUN]:
        # Пытаемся получить расширенное описание со страницы (не обязательно)
        pub_time, detail_summary = await scrape_details(art["url"])
        # Если удалось достать текст – используем его, иначе description
        final_text = detail_summary if detail_summary else art.get("description", "")
        success = await send_article(bot, art, final_text)
        if success:
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)
        else:
            print(f"⚠️ Пропущена новость: {art['title'][:50]}")

    print(f"✨ Завершено. Отправлено {sent} новостей.")

if __name__ == "__main__":
    asyncio.run(main())
