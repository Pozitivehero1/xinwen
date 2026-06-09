import requests
import telegram
import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram.constants import ParseMode
from playwright.async_api import async_playwright

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GNEWS_API_KEY]):
    print("❌ Ошибка: не загружены переменные окружения.")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 2
MAX_AGE_HOURS = 72                     # теперь 3 дня
SEND_INTERVAL_SEC = 20

# Белый список – достаточно одного слова
FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов",
    "евро", "кубок", "гол", "матч", "тренер", "игрок", "стадион",
    "рфпл", "премьер-лига", "ла лига", "серия а", "бундеслига"
]
# Чёрный список временно отключён (просто закомментирован)
# BLACKLIST_WORDS = [...]
BLACKLIST_WORDS = []   # пустой список = ничего не блокируем

def is_football_article(title: str, description: str) -> bool:
    text = (title + " " + (description or "")).lower()
    # Проверка чёрного списка
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    # Проверка белого списка
    for good in FOOTBALL_KEYWORDS:
        if good in text:
            return True
    return False

def is_recent(published_at: str) -> bool:
    if not published_at:
        print("    ⚠️ Нет поля publishedAt")
        return False
    try:
        if published_at.endswith('Z'):
            published_at = published_at[:-1] + '+00:00'
        pub_dt = datetime.fromisoformat(published_at)
        now = datetime.now(timezone.utc)
        age = now - pub_dt
        result = age.total_seconds() <= MAX_AGE_HOURS * 3600
        if not result:
            print(f"    📅 Слишком старая: {pub_dt} (возраст {age.days} дней)")
        return result
    except Exception as e:
        print(f"    ❌ Ошибка парсинга даты: {e}")
        return False

def fetch_gnews():
    print("📡 Запрос к GNews API...")
    # Для русских новостей лучше использовать "sport football" или просто "футбол"
    query = "футбол"
    url = f"https://gnews.io/api/v4/search?q={query}&lang=ru&country=ru&max=15&apikey={GNEWS_API_KEY}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"⚠️ GNews вернул код {resp.status_code}")
            return []
        data = resp.json()
        articles = data.get("articles", [])
        print(f"✅ Получено {len(articles)} статей")
        # Отладка: выведем первые 3 заголовка
        for i, art in enumerate(articles[:3]):
            print(f"   {i+1}. {art.get('title')} ({art.get('publishedAt')})")
        return articles
    except Exception as e:
        print(f"❌ Ошибка при запросе к GNews: {e}")
        return []

async def get_full_text(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        full_text = ""
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            selectors = ['article', '.article-content', '.post-content', '.entry-content', '.content', '#main-content', '.news-detail']
            for sel in selectors:
                container = await page.query_selector(sel)
                if container:
                    paragraphs = await container.query_selector_all('p')
                    texts = [await p.inner_text() for p in paragraphs[:6] if len(await p.inner_text()) > 40]
                    if texts:
                        full_text = "\n\n".join(texts)
                        break
            if not full_text:
                all_paragraphs = await page.query_selector_all('p')
                for p in all_paragraphs[:6]:
                    txt = await p.inner_text()
                    if len(txt) > 40:
                        full_text += txt.strip() + "\n\n"
        except Exception as e:
            print(f"⚠️ Ошибка парсинга {url}: {e}")
        finally:
            await browser.close()
        return full_text.strip()

async def send_article(bot, article):
    title = article.get("title")
    url = article.get("url")
    image = article.get("image")
    description = article.get("description", "")

    if not title or not url:
        return False

    if not is_football_article(title, description):
        print(f"⏭️ Пропускаем (не футбол по ключевым словам): {title[:60]}")
        return False

    print(f"📰 Парсим полный текст: {title[:60]}...")
    full_text = await get_full_text(url)
    if not full_text:
        full_text = description

    caption_parts = [
        f"⚽ <b>{title}</b>\n",
        full_text[:900],
        "",
        f"🔗 <a href='{url}'>Читать полностью на сайте</a>"
    ]
    caption = "\n".join(p for p in caption_parts if p)
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    try:
        if image:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

async def main():
    print("🚀 Запуск футбольного бота (полный текст, без дат и контактов)")
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

    articles = fetch_gnews()
    if not articles:
        print("Нет новостей от GNews.")
        return

    fresh_football = []
    seen_urls = set()
    for art in articles:
        url = art.get("url")
        pub = art.get("publishedAt")
        title = art.get("title", "")
        desc = art.get("description", "")
        if not url or url in seen_urls:
            continue
        if not is_recent(pub):
            continue
        if not is_football_article(title, desc):
            continue
        seen_urls.add(url)
        fresh_football.append(art)

    if not fresh_football:
        print("Нет свежих футбольных новостей за последние", MAX_AGE_HOURS, "часов.")
        return

    print(f"Найдено {len(fresh_football)} подходящих новостей. Отправлю не более {MAX_ARTICLES_PER_RUN}.")
    sent = 0
    for art in fresh_football[:MAX_ARTICLES_PER_RUN]:
        success = await send_article(bot, art)
        if success:
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)

    print(f"✨ Завершено. Отправлено {sent} новостей.")

if __name__ == "__main__":
    asyncio.run(main())
