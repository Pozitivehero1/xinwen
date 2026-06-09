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
MAX_ARTICLES_PER_RUN = 1
MAX_AGE_HOURS = 168          # 7 дней
SEND_INTERVAL_SEC = 10
RSS_TIMEOUT = 10
PAGE_TIMEOUT = 15

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

# Расширенный белый список
FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов", "евро", "кубок",
    "гол", "матч", "тренер", "игрок", "стадион", "рфпл", "премьер-лига", "ла лига",
    "серия а", "бундеслига", "локомотив", "спартак", "зенит", "цска", "динамо",
    "краснодар", "ростов", "рубин", "реал", "барселона", "бавария", "псж",
    "трансфер", "контракт", "сборная", "товарищеский матч"
]

def is_football_article(title: str, desc: str) -> bool:
    text = (title + " " + (desc or "")).lower()
    return any(kw in text for kw in FOOTBALL_KEYWORDS)

def parse_rss_feed(url: str):
    """Парсит RSS, возвращает список (title, link, description, pub_date, image_url)"""
    try:
        resp = requests.get(url, timeout=RSS_TIMEOUT)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")
            # Извлекаем картинку из enclosure или media:content
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

def fetch_article_data(url: str):
    """Загружает страницу, возвращает (полный_текст, картинка_og)"""
    try:
        resp = requests.get(url, timeout=PAGE_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return "", None
        html_content = resp.text
        # Ищем картинку og:image
        og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html_content)
        image_url = og_image.group(1) if og_image else None
        # Ищем текст статьи (первые 10 абзацев)
        # Удаляем скрипты и стили
        clean = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL)
        clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
        # Ищем параграфы
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.DOTALL)
        text_paragraphs = []
        for p in paragraphs:
            text = re.sub(r'<[^>]+>', '', p).strip()
            # Отфильтровываем короткие и мусорные
            if len(text) > 40 and not text.startswith("Читать"):
                text = re.sub(r'\d+\s*comment', '', text, flags=re.IGNORECASE)
                text_paragraphs.append(text)
            if len(text_paragraphs) >= 10:
                break
        full_text = "\n\n".join(text_paragraphs)
        # Обрезаем длинный текст
        if len(full_text) > 3500:
            full_text = full_text[:3500] + "..."
        return full_text, image_url
    except Exception as e:
        print(f"   Ошибка парсинга страницы: {e}")
        return "", None

async def send_article(bot, title, url, description, rss_image):
    # Парсим полный текст и картинку со страницы
    print(f"📰 Загружаем статью: {title[:60]}...")
    full_text, page_image = await asyncio.to_thread(fetch_article_data, url)
    if not full_text:
        full_text = description if description else "Нет текста."
    image = page_image or rss_image

    safe_title = html.escape(title)
    # Короткая подпись для фото (первые 700 символов текста)
    short_text = full_text[:700] if full_text else description[:700]
    caption = f"⚽ <b>{safe_title}</b>\n\n{short_text}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    try:
        if image:
            # Отправляем фото с подписью
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image, caption=caption, parse_mode=ParseMode.HTML)
            # Если полный текст длиннее 700 символов, отправляем отдельно
            if len(full_text) > 700:
                full_message = f"<b>{safe_title}</b>\n\n{full_text}"
                if len(full_message) > 4096:
                    full_message = full_message[:4093] + "..."
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=full_message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            # Без фото – отправляем полный текст
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
    print("🚀 Запуск бота (фото + полный текст)")
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
            # Проверка даты (упрощённо – если нет даты, пропускаем)
            # Для простоты пропустим проверку даты, т.к. многие RSS дают кривую дату
            all_news.append((title, link, desc, rss_image, pub_date))

    if not all_news:
        print("Нет подходящих новостей.")
        return

    # Сортируем по дате (чем новее – тем выше) – используем строку даты как есть
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
