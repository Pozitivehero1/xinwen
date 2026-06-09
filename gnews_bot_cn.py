import sys
import os
import html
import time
import asyncio
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

# Принудительный сброс буфера
def debug_print(*args, **kwargs):
    print(*args, **kwargs, flush=True)

debug_print("=== НАЧАЛО ЗАГРУЗКИ СКРИПТА ===")

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

debug_print(f"TOKEN loaded: {'OK' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
debug_print(f"CHAT_ID loaded: {'OK' if TELEGRAM_CHAT_ID else 'MISSING'}")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    debug_print("❌ Ошибка: не загружены переменные окружения")
    sys.exit(1)

# ------------------- НАСТРОЙКИ -------------------
MAX_ARTICLES_PER_RUN = 1
MAX_AGE_HOURS = 72
SEND_INTERVAL_SEC = 10

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
]

FOOTBALL_KEYWORDS = [
    "футбол", "football", "soccer", "чемпионат", "лига чемпионов",
    "кубок", "гол", "матч", "тренер", "игрок", "стадион", "рфпл"
]

BLACKLIST_WORDS = [
    "американский футбол", "nfl", "тейлор свифт", "taylor swift",
    "баскетбол", "нба", "теннис", "свадьба"
]

def is_football_article(title, desc):
    text = (title + " " + (desc or "")).lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    for good in FOOTBALL_KEYWORDS:
        if good in text:
            return True
    return False

def parse_rss_date(date_str):
    # Пытаемся разобрать дату в формате RFC 822
    if not date_str:
        return None
    try:
        # удаляем смещение часового пояса для простоты
        import email.utils
        tt = email.utils.parsedate_tz(date_str)
        if tt:
            ts = email.utils.mktime_tz(tt)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except:
        pass
    return None

def fetch_rss_news():
    all_news = []
    for feed_url in RSS_FEEDS:
        debug_print(f"📡 Парсим RSS: {feed_url}")
        try:
            resp = requests.get(feed_url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                debug_print(f"   Ошибка HTTP {resp.status_code}")
                continue
            root = ET.fromstring(resp.content)
            # Пространства имён игнорируем, ищем item
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ''
                link = item.find('link').text if item.find('link') is not None else ''
                desc_elem = item.find('description')
                description = desc_elem.text if desc_elem is not None else ''
                pub_elem = item.find('pubDate')
                pub_date = pub_elem.text if pub_elem is not None else ''
                if not title or not link:
                    continue
                pub_dt = parse_rss_date(pub_date)
                if pub_dt:
                    age = datetime.now(timezone.utc) - pub_dt
                    if age.total_seconds() > MAX_AGE_HOURS * 3600:
                        continue
                if not is_football_article(title, description):
                    continue
                all_news.append({
                    'title': title,
                    'url': link,
                    'description': description,
                    'published': pub_dt
                })
        except Exception as e:
            debug_print(f"   ❌ Ошибка: {e}")
    # Удаляем дубли по URL
    seen = set()
    unique = []
    for item in all_news:
        if item['url'] not in seen:
            seen.add(item['url'])
            unique.append(item)
    return unique

async def send_article(bot, article):
    title = article['title']
    description = article['description']
    safe_title = html.escape(title)
    safe_text = html.escape(description[:3000])  # ограничим
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
        debug_print(f"✅ Отправлено: {title[:60]}...")
        return True
    except Exception as e:
        debug_print(f"❌ Ошибка отправки: {e}")
        return False

async def main():
    debug_print("🚀 Запуск RSS-футбольного бота (упрощённая версия)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    news = fetch_rss_news()
    if not news:
        debug_print("Нет свежих футбольных новостей.")
        return
    debug_print(f"Найдено {len(news)} новостей. Отправлю не более {MAX_ARTICLES_PER_RUN}.")
    sent = 0
    for item in news[:MAX_ARTICLES_PER_RUN]:
        if await send_article(bot, item):
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)
    debug_print(f"✨ Завершено. Отправлено {sent} новостей.")

if __name__ == "__main__":
    debug_print("=== СКРИПТ ВХОДИТ В MAIN ===")
    asyncio.run(main())
    debug_print("=== СКРИПТ ЗАВЕРШЁН ===")
