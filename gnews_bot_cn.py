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

# ------------------- НАСТРОЙКИ -------------------
MAX_ARTICLES_PER_RUN = 1
MAX_AGE_HOURS = 72
SEND_INTERVAL_SEC = 20

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "http://fanat1k.ru/e107_plugins/rss_menu/rss.php?news.2",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

# Ключевые слова
FOOTBALL_KEYWORDS = [
    "футбол", "сборная", "чемпионат", "лига чемпионов", "евро",
    "кубок", "гол", "матч", "тренер", "игрок", "стадион", "рфпл",
    "премьер-лига", "ла лига", "серия а", "бундеслига"
]

BLACKLIST_WORDS = [
    "американский футбол", "nfl", "тейлор свифт", "баскетбол", "теннис"
]

def is_football(text):
    text = text.lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    for good in FOOTBALL_KEYWORDS:
        if good in text:
            return True
    return False

def parse_rss_date(date_str):
    # Парсим дату в формате RFC 2822 (пример: "Wed, 10 Jun 2026 12:00:00 +0300")
    try:
        # Удаляем смещение, оставляем +0000
        if date_str:
            # Простейший разбор: берём первые 5 слов
            parts = date_str.split()
            if len(parts) >= 5:
                # Собираем день, месяц, год, время
                day = parts[1]
                month = parts[2]
                year = parts[3]
                time = parts[4]
                dt_str = f"{day} {month} {year} {time} +0000"
                # Используем datetime.strptime с подстановкой английских названий месяцев
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(date_str)
    except:
        pass
    return None

def fetch_rss(url):
    print(f"📡 Загрузка RSS: {url}")
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"   Ошибка HTTP {resp.status_code}")
            return []
        root = ET.fromstring(resp.content)
        # Пространство имен может быть, ищем channel/item
        items = []
        for item in root.findall('.//item'):
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            description = item.find('description')
            # Картинка: ищем enclosure или media:content
            image = None
            enclosure = item.find('enclosure')
            if enclosure is not None and enclosure.get('type', '').startswith('image'):
                image = enclosure.get('url')
            if not image:
                media = item.find('{http://search.yahoo.com/mrss/}content')
                if media is not None:
                    image = media.get('url')
            items.append({
                'title': title.text if title is not None else '',
                'link': link.text if link is not None else '',
                'pubDate': pub_date.text if pub_date is not None else '',
                'description': description.text if description is not None else '',
                'image': image
            })
        print(f"   Найдено {len(items)} элементов")
        return items
    except Exception as e:
        print(f"   Ошибка: {e}")
        return []

def fetch_full_text_and_image(url):
    """Парсит страницу, возвращает (текст, картинка)"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return "", None
        html_content = resp.text
        # Ищем картинку: og:image
        img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html_content)
        image_url = img_match.group(1) if img_match else None
        if not image_url:
            # Первая картинка с src
            img_match = re.search(r'<img[^>]+src=["\']([^"\']+\.(jpg|jpeg|png|webp))["\']', html_content, re.I)
            if img_match:
                image_url = img_match.group(1)
        # Ищем текст: берём все параграфы внутри article или body
        # Упрощённо: находим теги <p> и собираем их, отбрасывая короткие и мусорные
        p_tags = re.findall(r'<p[^>]*>(.*?)</p>', html_content, re.DOTALL)
        text_parts = []
        for p in p_tags:
            # Очищаем от HTML-тегов
            clean = re.sub(r'<[^>]+>', '', p).strip()
            clean = re.sub(r'\s+', ' ', clean)
            if len(clean) > 40 and not re.search(r'(читайте|подпишись|источник|реклама)', clean, re.I):
                text_parts.append(clean)
        full_text = '\n\n'.join(text_parts[:15])  # не больше 15 абзацев
        if len(full_text) > 3500:
            full_text = full_text[:3500] + '...'
        return full_text, image_url
    except Exception as e:
        print(f"   Ошибка парсинга страницы: {e}")
        return "", None

async def send_article(bot, article):
    title = article['title']
    url = article['link']
    rss_desc = article['description']
    rss_image = article['image']
    print(f"📰 Обработка: {title[:60]}...")
    # Проверка на футбол
    if not is_football(title + ' ' + rss_desc):
        print(f"   Пропущено (не футбол)")
        return False
    full_text, page_image = fetch_full_text_and_image(url)
    if not full_text:
        full_text = rss_desc if rss_desc else "Нет текста."
    image_url = page_image or rss_image
    safe_title = html.escape(title)
    # Отправляем фото с краткой подписью
    caption = f"⚽ <b>{safe_title}</b>\n\n{full_text[:800]}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    try:
        if image_url:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=caption, parse_mode=ParseMode.HTML)
            print(f"   Отправлено фото")
            if len(full_text) > 800:
                text_msg = f"<b>{safe_title}</b>\n\n{full_text}"
                if len(text_msg) > 4096:
                    text_msg = text_msg[:4093] + "..."
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text_msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                print(f"   Отправлен полный текст")
        else:
            text_msg = f"⚽ <b>{safe_title}</b>\n\n{full_text}"
            if len(text_msg) > 4096:
                text_msg = text_msg[:4093] + "..."
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text_msg, parse_mode=ParseMode.HTML)
            print(f"   Отправлен текст без фото")
        return True
    except Exception as e:
        print(f"   Ошибка отправки: {e}")
        return False

async def main():
    print("🚀 Запуск футбольного бота (минимальная версия)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    all_news = []
    for feed_url in RSS_FEEDS:
        items = fetch_rss(feed_url)
        for item in items:
            # Фильтр по дате
            pub_date = item.get('pubDate')
            if pub_date:
                dt = parse_rss_date(pub_date)
                if dt:
                    age = datetime.now(timezone.utc) - dt
                    if age.total_seconds() > MAX_AGE_HOURS * 3600:
                        continue
            all_news.append(item)
    if not all_news:
        print("Нет новостей.")
        return
    # Сортируем по дате (чем новее, тем выше)
    all_news.sort(key=lambda x: x.get('pubDate', ''), reverse=True)
    print(f"Найдено {len(all_news)} новостей. Отправляю {min(len(all_news), MAX_ARTICLES_PER_RUN)}.")
    sent = 0
    for item in all_news[:MAX_ARTICLES_PER_RUN]:
        if await send_article(bot, item):
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)
    print(f"✨ Завершено. Отправлено {sent}.")

if __name__ == "__main__":
    asyncio.run(main())
