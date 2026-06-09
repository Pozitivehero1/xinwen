import asyncio
import os
import html
import re
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MISTRAL_API_KEY]):
    print("❌ Ошибка: не хватает переменных окружения")
    exit(1)

MAX_ARTICLES_PER_RUN = 1
MAX_AGE_HOURS = 72
RSS_TIMEOUT = 12
PAGE_TIMEOUT = 12
MISTRAL_MODEL = "mistral-tiny"

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов", "евро",
    "кубок", "гол", "матч", "тренер", "игрок", "стадион", "рфпл", "премьер-лига",
    "ла лига", "серия а", "бундеслига", "локомотив", "спартак", "зенит", "цска",
    "динамо", "краснодар", "ростов", "рубин", "реал", "барселона", "бавария", "псж"
]

BLACKLIST_WORDS = [
    "баскетбол", "нба", "теннис", "хоккей", "американский футбол", "nfl",
    "тейлор свифт", "свадьба", "тревис келси"
]

def is_football_article(title: str, desc: str) -> bool:
    text = (title + " " + (desc or "")).lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    return any(kw in text for kw in FOOTBALL_KEYWORDS)

def parse_rss_date(date_str: str):
    if not date_str:
        return None
    formats = [
        '%a, %d %b %Y %H:%M:%S %z',
        '%a, %d %b %Y %H:%M:%S %Z',
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%SZ',
        '%d %b %Y %H:%M:%S %z',
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except:
            continue
    return None

def is_recent(date_str: str) -> bool:
    dt = parse_rss_date(date_str)
    if not dt:
        return False
    now = datetime.now(timezone.utc)
    age = now - dt
    return age.total_seconds() <= MAX_AGE_HOURS * 3600

def fetch_rss_items(feed_url: str):
    try:
        resp = requests.get(feed_url, timeout=RSS_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")
            image = None
            enc = item.find("enclosure")
            if enc is not None and enc.get("type", "").startswith("image"):
                image = enc.get("url")
            if not image:
                media = item.find("{http://search.yahoo.com/mrss/}content")
                if media is not None:
                    image = media.get("url")
            items.append((title, link, desc, pub_date, image))
        if not items:
            for entry in root.findall(".//entry"):
                title = entry.findtext("title", "")
                link_el = entry.find("link")
                link = link_el.get("href") if link_el is not None else ""
                desc = entry.findtext("summary", "")
                pub_date = entry.findtext("published", "")
                items.append((title, link, desc, pub_date, None))
        return items
    except Exception as e:
        print(f"Ошибка RSS {feed_url}: {e}")
        return []

def fetch_page_image_and_text(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, timeout=PAGE_TIMEOUT, headers=headers)
        if resp.status_code != 200:
            print(f"   Страница ответила кодом {resp.status_code}")
            return None, None
        html_content = resp.text
        
        # Поиск картинки: og:image или первая подходящая
        image = None
        # og:image
        og_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html_content)
        if not og_match:
            og_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html_content)
        if og_match:
            image = og_match.group(1)
            if image.startswith('/'):
                image = urljoin(url, image)
        # Если нет og:image, ищем первый <img> с шириной > 200 (приблизительно)
        if not image:
            img_tags = re.findall(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html_content)
            for img_url in img_tags:
                if any(ext in img_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    if not img_url.startswith('http'):
                        img_url = urljoin(url, img_url)
                    # Пропускаем маленькие иконки
                    if 'logo' not in img_url.lower() and 'icon' not in img_url.lower():
                        image = img_url
                        break
        
        # Извлечение текста: очищаем от скриптов, стилей, берём параграфы
        clean = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL)
        clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.DOTALL)
        texts = []
        for p in paragraphs:
            text = re.sub(r'<[^>]+>', '', p).strip()
            if len(text) > 40 and not text.startswith("Читать") and not text.startswith("Источник"):
                text = re.sub(r'\d+\s*comment', '', text, flags=re.IGNORECASE)
                texts.append(text)
            if len(texts) >= 12:
                break
        full_text = "\n\n".join(texts)
        if len(full_text) > 3000:
            full_text = full_text[:3000] + "..."
        if image:
            print(f"   Найдена картинка: {image[:80]}")
        else:
            print("   Картинка не найдена")
        return image, full_text
    except Exception as e:
        print(f"   Ошибка загрузки страницы {url}: {e}")
        return None, None

def summarize_with_mistral(text: str) -> str:
    if not text or len(text) < 50:
        return text
    # Жёсткий промпт без шаблонов
    prompt = f"""Перескажи футбольную новость коротко (3-5 предложений), только факты: кто, что, где, когда, какой счёт (если есть). Не используй фразы "если известно", "уточнить", "ключевые моменты" и не пиши шаблонные заглушки. Просто напиши связный текст.

Новость:
{text[:2000]}"""
    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MISTRAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 350
            },
            timeout=20
        )
        if resp.status_code == 200:
            data = resp.json()
            summary = data["choices"][0]["message"]["content"].strip()
            # Проверка на мусорные фразы
            if any(bad in summary for bad in ["если известен", "уточнить", "X:X", "ключевые моменты"]):
                print("   Пересказ содержит шаблонный мусор, используем оригинальный текст")
                return text[:800]
            if len(summary) > 800:
                summary = summary[:800] + "..."
            return summary
        else:
            print(f"Mistral ошибка {resp.status_code}: {resp.text[:200]}")
            return text
    except Exception as e:
        print(f"Mistral исключение: {e}")
        return text

async def send_article(bot, title, url, description, rss_image):
    print(f"📰 Загружаем страницу: {title[:50]}...")
    page_image, full_text = await asyncio.to_thread(fetch_page_image_and_text, url)
    if not full_text:
        full_text = description if description else ""
    
    # Выбираем картинку: сначала со страницы, потом из RSS
    image = None
    if page_image and page_image.startswith(('http://', 'https://')):
        image = page_image
    elif rss_image and rss_image.startswith(('http://', 'https://')):
        image = rss_image
    
    if image:
        print(f"   Используем картинку: {image[:80]}")
    else:
        print("   Картинка отсутствует, отправляем только текст")
    
    if not full_text:
        print("   Нет текста новости")
        return False
    
    # Пересказ от Mistral
    summary = await asyncio.to_thread(summarize_with_mistral, full_text)
    safe_title = html.escape(title)
    caption = f"⚽ <b>{safe_title}</b>\n\n{summary}"
    if len(caption) > 1024:
        # Обрезаем пересказ, оставляя заголовок
        max_summary_len = 1024 - len(f"⚽ <b>{safe_title}</b>\n\n") - 3
        summary = summary[:max_summary_len] + "..."
        caption = f"⚽ <b>{safe_title}</b>\n\n{summary}"
    
    try:
        if image:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        # Пробуем отправить без фото
        if "Wrong type" in str(e) or "Failed to get http url content" in str(e):
            print("   Пробуем отправить без фото")
            try:
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                print("   ✅ Отправлено текстом")
                return True
            except Exception as e2:
                print(f"   ❌ Не удалось и текстом: {e2}")
        return False

async def main():
    print("🚀 Запуск футбольного бота с Mistral (исправленный промпт)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 RSS: {feed_url}")
        items = await asyncio.to_thread(fetch_rss_items, feed_url)
        print(f"   Найдено {len(items)} записей")
        for title, link, desc, pub_date, rss_image in items:
            if not title or not link:
                continue
            if not is_football_article(title, desc):
                continue
            if not is_recent(pub_date):
                continue
            all_news.append((title, link, desc, rss_image, pub_date))
    if not all_news:
        print("Нет свежих футбольных новостей.")
        return
    all_news.sort(key=lambda x: parse_rss_date(x[4]) or datetime.min, reverse=True)
    to_send = all_news[:MAX_ARTICLES_PER_RUN]
    print(f"Отправляю {len(to_send)} новостей...")
    for idx, (title, link, desc, rss_image, _) in enumerate(to_send):
        ok = await send_article(bot, title, link, desc, rss_image)
        if ok and idx < len(to_send)-1:
            await asyncio.sleep(10)
    print("✨ Готово.")

if __name__ == "__main__":
    asyncio.run(main())
