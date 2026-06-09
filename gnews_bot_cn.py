import asyncio
import os
import html
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
import feedparser
import requests
from bs4 import BeautifulSoup

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("❌ Ошибка: не загружены TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    exit(1)

# ------------------- НАСТРОЙКИ -------------------
MAX_ARTICLES_PER_RUN = 2      # максимум новостей за один запуск
MAX_AGE_HOURS = 72            # не старше 3 дней
SEND_INTERVAL_SEC = 20

# Список RSS-лент (футбольные новости)
RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "http://fanat1k.ru/e107_plugins/rss_menu/rss.php?news.2",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

# Ключевые слова для фильтра (футбол)
FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов",
    "евро", "кубок", "гол", "матч", "тренер", "игрок", "стадион",
    "рфпл", "премьер-лига", "ла лига", "серия а", "бундеслига"
]

# Чёрный список (нежелательные темы)
BLACKLIST_WORDS = [
    "американский футбол", "nfl", "super bowl", "тревис келси", "travis kelce",
    "тейлор свифт", "taylor swift", "свадьба", "баскетбол", "нба", "теннис"
]

def is_football_article(title: str, description: str) -> bool:
    """Проверяет, относится ли новость к футболу"""
    text = (title + " " + (description or "")).lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    for good in FOOTBALL_KEYWORDS:
        if good in text:
            return True
    return False

def parse_rss_date(date_str: str):
    """Пытается преобразовать дату из RSS в datetime (UTC)"""
    if not date_str:
        return None
    # feedparser уже отдаёт структурированную дату, но на всякий случай
    try:
        # Попробуем стандартные форматы
        dt = feedparser._parse_date(date_str)
        if dt:
            return dt
    except:
        pass
    return None

def is_recent(published_struct) -> bool:
    """Проверяет, не старше ли новость MAX_AGE_HOURS"""
    if not published_struct:
        return False
    # published_struct может быть кортежем time.struct_time или datetime
    if hasattr(published_struct, 'tm_year'):
        pub_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
    elif isinstance(published_struct, datetime):
        pub_dt = published_struct
    else:
        return False
    now = datetime.now(timezone.utc)
    age = now - pub_dt
    return age.total_seconds() <= MAX_AGE_HOURS * 3600

def fetch_rss_news():
    """Собирает новости из всех RSS-лент, фильтрует по свежести и теме"""
    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 Парсим RSS: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
            if feed.bozo:  # ошибка парсинга
                print(f"   ⚠️ Ошибка парсинга: {feed.bozo_exception}")
            for entry in feed.entries[:10]:  # не больше 10 с каждой ленты
                title = entry.get('title', '')
                link = entry.get('link', '')
                # Берём описание (может быть кратким)
                description = entry.get('summary', entry.get('description', ''))
                # Очищаем HTML из описания
                if description:
                    soup = BeautifulSoup(description, 'html.parser')
                    description = soup.get_text(separator=' ', strip=True)
                published = entry.get('published_parsed')
                if not title or not link:
                    continue
                if not is_recent(published):
                    continue
                if not is_football_article(title, description):
                    continue
                all_news.append({
                    'title': title,
                    'url': link,
                    'description': description,
                    'published': published,
                })
        except Exception as e:
            print(f"   ❌ Ошибка при загрузке {feed_url}: {e}")
    # Убираем дубликаты по URL
    unique = {}
    for item in all_news:
        if item['url'] not in unique:
            unique[item['url']] = item
    return list(unique.values())

async def get_full_text(url: str) -> str:
    """Пытается получить полный текст статьи со страницы (если description слишком короткий)"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(url, timeout=15, headers=headers)
        if resp.status_code != 200:
            return ''
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Ищем основной контент
        for selector in ['article', '.article-content', '.post-content', '.entry-content', '.content', '#main-content']:
            container = soup.select_one(selector)
            if container:
                paragraphs = container.find_all('p')
                text = '\n\n'.join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40)
                if len(text) > 200:
                    return text
        # fallback: все параграфы
        all_paras = soup.find_all('p')
        text = '\n\n'.join(p.get_text(strip=True) for p in all_paras if len(p.get_text(strip=True)) > 60)
        return text[:3000]  # ограничим
    except Exception as e:
        print(f"⚠️ Ошибка парсинга {url}: {e}")
        return ''

async def send_article(bot: Bot, article: dict):
    title = article['title']
    url = article['url']
    description = article.get('description', '')

    # Если описание слишком короткое (< 300 символов), пытаемся распарсить страницу
    full_text = description
    if len(full_text) < 300:
        print(f"📰 Описание короткое, парсим страницу: {title[:60]}...")
        page_text = await get_full_text(url)
        if page_text:
            full_text = page_text
        else:
            full_text = description

    # Экранируем HTML-символы
    safe_title = html.escape(title)
    safe_text = html.escape(full_text)

    # Формируем сообщение: только заголовок (жирный) и текст. БЕЗ ссылки на источник.
    message = f"⚽ <b>{safe_title}</b>\n\n{safe_text}"

    if len(message) > 4096:
        message = message[:4093] + "..."

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        print(f"✅ Отправлено: {title[:60]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        # Пробуем без HTML
        try:
            plain_message = re.sub(r'<[^>]+>', '', message)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain_message, disable_web_page_preview=True)
            print("   ✅ Отправлено в plain-режиме")
            return True
        except Exception as e2:
            print(f"   ❌ Не удалось: {e2}")
            return False

async def main():
    print("🚀 Запуск RSS-футбольного бота (полный текст, без ссылок и дат)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    news = fetch_rss_news()
    if not news:
        print("Нет свежих футбольных новостей.")
        return

    # Сортируем по дате (новые сверху) если есть published
    news.sort(key=lambda x: x.get('published'), reverse=True)

    print(f"Найдено {len(news)} новостей. Отправлю не более {MAX_ARTICLES_PER_RUN}.")
    sent = 0
    for item in news[:MAX_ARTICLES_PER_RUN]:
        if await send_article(bot, item):
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)

    print(f"✨ Завершено. Отправлено {sent} новостей.")

if __name__ == "__main__":
    asyncio.run(main())
