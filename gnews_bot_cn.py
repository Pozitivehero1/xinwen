import asyncio
import os
import html
import re
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("❌ Ошибка: не загружены TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 1
MAX_AGE_HOURS = 168           # 7 дней
SEND_INTERVAL_SEC = 20
REQUEST_TIMEOUT = 10

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

# Расширенный белый список – футбольные ключевые слова
FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов", "евро", "кубок",
    "гол", "матч", "тренер", "игрок", "стадион", "рфпл", "премьер-лига", "ла лига",
    "серия а", "бундеслига", "локомотив", "спартак", "зенит", "цска", "динамо",
    "краснодар", "ростов", "рубин", "анжи", "ахмат", "сочи", "реал", "барселона",
    "атлетико", "ювентус", "милан", "интер", "бавария", "боруссия", "псж", "манчестер",
    "ливерпуль", "арсенал", "челси", "тоттенхэм", "севилья", "порту", "бенфика",
    "шахтер", "динамо киев", "галатасарай", "фенербахче", "айакс", "псв", "фейеноорд",
    "трансфер", "контракт", "сборная", "отбор", "товарищеский матч", "квалификация",
    "турнир", "финал", "полуфинал", "пенальти", "офсайд", "фол", "желтая карточка",
    "красная карточка", "замена", "нападающий", "защитник", "полузащитник", "вратарь"
]

# Чёрный список (пока отключён)
BLACKLIST_WORDS = []   # можно добавить "баскетбол", "теннис" и т.п.

def is_football_article(title: str, description: str) -> tuple[bool, str]:
    """Возвращает (True/False, причина пропуска)"""
    text = (title + " " + (description or "")).lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False, f"чёрный список: {bad}"
    for good in FOOTBALL_KEYWORDS:
        if good in text:
            return True, ""
    return False, "нет ключевых слов"

def parse_rss_feed(url: str):
    """Загружает RSS и возвращает список записей (заголовок, ссылка, описание, дата)"""
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"   Ошибка HTTP {resp.status_code}")
            return []
        root = ET.fromstring(resp.content)
        ns = {'': 'http://www.w3.org/2005/Atom'}  # некоторые фиды используют Atom, но для RSS 2.0 просто ищем элементы
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            description = item.findtext("description", "")
            pub_date_str = item.findtext("pubDate", "")
            items.append((title, link, description, pub_date_str))
        # Если нет элементов item, пробуем Atom
        if not items:
            for entry in root.findall(".//entry"):
                title = entry.findtext("title", "")
                link = entry.findtext("link", "")
                if link and not link.startswith("http"):
                    link_attr = entry.find("link")
                    if link_attr is not None:
                        link = link_attr.get("href", "")
                description = entry.findtext("summary", "")
                pub_date_str = entry.findtext("published", "")
                items.append((title, link, description, pub_date_str))
        return items
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        return []

def parse_date(pub_date_str: str):
    if not pub_date_str:
        return None
    # Пробуем разные форматы
    formats = [
        '%a, %d %b %Y %H:%M:%S %z',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%SZ',
        '%d %b %Y %H:%M:%S %z',
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(pub_date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except:
            continue
    return None

def is_recent(pub_date_str: str) -> bool:
    dt = parse_date(pub_date_str)
    if not dt:
        return False
    now = datetime.now(timezone.utc)
    age = now - dt
    return age.total_seconds() <= MAX_AGE_HOURS * 3600

async def send_article(bot: Bot, title: str, url: str, description: str):
    """Отправляет новость (заголовок + описание)"""
    safe_title = html.escape(title)
    safe_desc = html.escape(description[:1000]) if description else "(нет описания)"
    message = f"⚽ <b>{safe_title}</b>\n\n{safe_desc}\n\n🔗 <a href='{html.escape(url)}'>Читать далее</a>"
    if len(message) > 4096:
        message = message[:4093] + "..."
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

async def main():
    print("🚀 Запуск футбольного бота (расширенные ключевые слова, отладка)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 Загрузка RSS: {feed_url}")
        items = parse_rss_feed(feed_url)
        print(f"   Найдено {len(items)} элементов")
        for title, link, description, pub_date in items:
            if not title or not link:
                continue
            if not is_recent(pub_date):
                continue
            is_football, reason = is_football_article(title, description)
            if not is_football:
                print(f"   Пропущено (не футбол: {reason}): {title[:50]}...")
                continue
            all_news.append((title, link, description, pub_date))
    if not all_news:
        print("Нет свежих футбольных новостей.")
        return
    # Сортируем по дате (новые сверху) – простейшая сортировка по строке, но можно и без
    all_news.sort(key=lambda x: x[3] or "", reverse=True)
    print(f"Найдено {len(all_news)} подходящих новостей. Отправлю {min(len(all_news), MAX_ARTICLES_PER_RUN)}.")
    sent = 0
    for title, link, description, _ in all_news[:MAX_ARTICLES_PER_RUN]:
        if await send_article(bot, title, link, description):
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)
    print(f"✨ Завершено. Отправлено {sent}.")

if __name__ == "__main__":
    asyncio.run(main())
