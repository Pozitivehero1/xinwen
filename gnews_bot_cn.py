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
    print("❌ Ошибка: не загружены переменные")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 5          # снизим до 2, чтобы не перегружать
MAX_AGE_HOURS = 168
SEND_INTERVAL_SEC = 30
RSS_TIMEOUT = 10
PAGE_TIMEOUT = 15

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов", "евро", "кубок",
    "гол", "матч", "тренер", "игрок", "стадион", "рфпл", "премьер-лига", "ла лига",
    "серия а", "бундеслига", "локомотив", "спартак", "зенит", "цска", "динамо",
    "краснодар", "ростов", "рубин", "реал", "барселона", "бавария", "псж",
    "трансфер", "контракт", "сборная", "товарищеский матч", "мукаса"
]

def is_football_article(title: str, desc: str) -> bool:
    text = (title + " " + (desc or "")).lower()
    return any(kw in text for kw in FOOTBALL_KEYWORDS)

def parse_rss_feed(url: str):
    try:
        resp = requests.get(url, timeout=RSS_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")
            # картинка
            image = None
            enclosure = item.find("enclosure")
            if enclosure is not None and enclosure.get("type", "").startswith("image"):
                image = enclosure.get("url")
            if not image:
                media = item.find("{http://search.yahoo.com/mrss/}content")
                if media is not None:
                    image = media.get("url")
            items.append((title, link, desc, pub_date, image))
        return items
    except Exception as e:
        print(f"   RSS ошибка: {e}")
        return []

def clean_html(html_text: str) -> str:
    """Удаляет все HTML-теги, лишние пробелы, мусорные фразы"""
    # Удаляем скрипты, стили, комментарии
    html_text = re.sub(r'<script.*?</script>', '', html_text, flags=re.DOTALL)
    html_text = re.sub(r'<style.*?</style>', '', html_text, flags=re.DOTALL)
    html_text = re.sub(r'<!--.*?-->', '', html_text, flags=re.DOTALL)
    # Удаляем все теги
    text = re.sub(r'<[^>]+>', ' ', html_text)
    # Убираем лишние пробелы и переносы
    text = re.sub(r'\s+', ' ', text)
    # Убираем типичные мусорные фразы
    trash = r'(Регистрация|Вход|Выйти|Подписаться|Поделиться|Источник|Фото:|Читайте также|Реклама|Обратная связь|Правила|Контакты|Пользовательское соглашение|СМИ|Свидетельство|18\+|Комментарии|Обсудить|Пожаловаться)'
    text = re.sub(trash, '', text, flags=re.IGNORECASE)
    # Убираем цифры + слово "комментарий"
    text = re.sub(r'\d+\s*(комментариев?|comment)', '', text, flags=re.IGNORECASE)
    # Убираем лишние пробелы и точки в начале
    text = re.sub(r'^\s*\.\s*', '', text)
    text = text.strip()
    return text

def fetch_article_data(url: str):
    """Возвращает (clean_text, image_url)"""
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
        })
        resp = session.get(url, timeout=PAGE_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            print(f"   HTTP {resp.status_code} для {url}")
            return "", None
        html_content = resp.text
        # Картинка og:image
        og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html_content)
        image_url = og_image.group(1) if og_image else None
        # Удаляем блоки с мусорными классами (примитивно)
        html_content = re.sub(r'<nav[^>]*>.*?</nav>', '', html_content, flags=re.DOTALL)
        html_content = re.sub(r'<footer[^>]*>.*?</footer>', '', html_content, flags=re.DOTALL)
        html_content = re.sub(r'<aside[^>]*>.*?</aside>', '', html_content, flags=re.DOTALL)
        html_content = re.sub(r'<div class="[^"]*?(?:menu|sidebar|comment|form|header)[^"]*?"[^>]*>.*?</div>', '', html_content, flags=re.DOTALL)
        # Ищем параграфы
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_content, re.DOTALL)
        clean_paragraphs = []
        for p in paragraphs:
            text = clean_html(p)
            if len(text) > 80:   # берём только осмысленные абзацы
                clean_paragraphs.append(text)
            if len(clean_paragraphs) >= 8:   # не больше 8 абзацев
                break
        full_text = "\n\n".join(clean_paragraphs)
        if len(full_text) > 3500:
            full_text = full_text[:3500] + "..."
        return full_text, image_url
    except Exception as e:
        print(f"   Ошибка парсинга {url}: {e}")
        return "", None

async def send_article(bot, title, url, description, rss_image):
    print(f"📰 Загружаем: {title[:60]}...")
    full_text, page_image = await asyncio.to_thread(fetch_article_data, url)
    if not full_text:
        # Если не удалось получить полный текст, используем описание из RSS (очистив его)
        full_text = clean_html(description) if description else "Нет текста."
    image = page_image or rss_image
    safe_title = html.escape(title)
    # Короткая версия для подписи к фото (первые 600 символов)
    short_text = full_text[:600] if full_text else ""
    caption = f"⚽ <b>{safe_title}</b>\n\n{short_text}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    try:
        if image:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image, caption=caption, parse_mode=ParseMode.HTML)
            if len(full_text) > 600:
                full_msg = f"<b>{safe_title}</b>\n\n{full_text}"
                if len(full_msg) > 4096:
                    full_msg = full_msg[:4093] + "..."
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=full_msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            message = f"⚽ <b>{safe_title}</b>\n\n{full_text}\n\n🔗 <a href='{html.escape(url)}'>Читать далее</a>"
            if len(message) > 4096:
                message = message[:4093] + "..."
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

async def main():
    print("🚀 Запуск бота (улучшенный парсинг)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 RSS: {feed_url}")
        items = await asyncio.to_thread(parse_rss_feed, feed_url)
        print(f"   Найдено {len(items)} записей")
        for title, link, desc, pub_date, rss_image in items:
            if not title or not link:
                continue
            if not is_football_article(title, desc):
                continue
            all_news.append((title, link, desc, rss_image, pub_date))
    if not all_news:
        print("Нет подходящих новостей.")
        return
    # Убираем дубли по URL (простая проверка)
    unique = {}
    for title, link, desc, img, pub in all_news:
        if link not in unique:
            unique[link] = (title, link, desc, img, pub)
    all_news = list(unique.values())
    all_news.sort(key=lambda x: x[4] or "", reverse=True)
    print(f"Отправляю {min(len(all_news), MAX_ARTICLES_PER_RUN)} новостей...")
    sent = 0
    for title, link, desc, rss_image, _ in all_news[:MAX_ARTICLES_PER_RUN]:
        ok = await send_article(bot, title, link, desc, rss_image)
        if ok:
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)
    print(f"✨ Готово. Отправлено {sent}.")

if __name__ == "__main__":
    asyncio.run(main())
