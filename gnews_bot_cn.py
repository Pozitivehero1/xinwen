import asyncio
import os
import re
import xml.etree.ElementTree as ET
import requests
import io
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from dotenv import load_dotenv
from telegram import Bot, InputFile
from telegram.constants import ParseMode

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENROUTER_API_KEY]):
    print("❌ Не хватает переменных окружения")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_NEWS_TO_COLLECT = 60
MAX_NEWS_TO_EVALUATE = 12          # не более 12 для оценки (лимит 20 в минуту)
FINAL_POSTS_COUNT = 3
RSS_TIMEOUT = 12
PAGE_TIMEOUT = 12
REQUEST_DELAY = 3                   # пауза между LLM-запросами (сек)

# RSS-источники (только футбольные, без другого спорта)
RSS_FEEDS = [
    "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.espn.com/espn/rss/soccer/news",
    "https://www.goal.com/feeds/en/news",
    "https://www.skysports.com/rss/12040",
    "https://www.transfermarkt.com/rss/news",
    "https://www.sports.ru/rss/",
]

# Белый список ключевых слов (футбол)
FOOTBALL_KEYWORDS = [
    "football", "soccer", "transfer", "contract", "injury", "manager", "fifa",
    "real madrid", "barcelona", "bayern", "psg", "man city", "liverpool",
    "champions league", "premier league", "la liga", "serie a", "bundesliga",
    "world cup", "euro", "copa america"
]

def is_football_news(title: str, desc: str) -> bool:
    text = (title + " " + (desc or "")).lower()
    return any(kw in text for kw in FOOTBALL_KEYWORDS)

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
        return items
    except Exception as e:
        print(f"Ошибка RSS {feed_url}: {e}")
        return []

def fetch_page_image_and_text(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, timeout=PAGE_TIMEOUT, headers=headers)
        if resp.status_code != 200:
            return None, None
        html_content = resp.text
        # og:image
        image = None
        og_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html_content)
        if not og_match:
            og_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html_content)
        if og_match:
            image = og_match.group(1)
            if image.startswith('/'):
                image = urljoin(url, image)
        # текст
        clean = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL)
        clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.DOTALL)
        texts = []
        for p in paragraphs:
            text = re.sub(r'<[^>]+>', '', p).strip()
            if len(text) > 40 and not text.startswith("Читать"):
                text = re.sub(r'\d+\s*comment', '', text, flags=re.IGNORECASE)
                texts.append(text)
            if len(texts) >= 8:
                break
        full_text = " ".join(texts[:4])  # первые 4 абзаца
        if len(full_text) > 1500:
            full_text = full_text[:1500]
        return image, full_text
    except Exception as e:
        print(f"Ошибка страницы {url}: {e}")
        return None, None

def download_image(image_url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(image_url, timeout=10, headers=headers)
        if resp.status_code == 200 and resp.headers.get('content-type', '').startswith('image/'):
            return io.BytesIO(resp.content)
        return None
    except Exception:
        return None

def call_llm(prompt: str, max_tokens: int = 250) -> str | None:
    """Вызов LLM через OpenRouter с обработкой лимитов"""
    model = "nvidia/nemotron-3-ultra-550b-a55b:free"   # стабильная бесплатная модель
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": max_tokens
            },
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            print(f"LLM ошибка {resp.status_code}: {resp.text[:100]}")
            return None
    except Exception as e:
        print(f"LLM исключение: {e}")
        return None

def rate_article(title: str, description: str) -> int:
    prompt = f"""Rate this football news headline on a scale of 1 to 10.
10 = world sensation
8-9 = very interesting
6-7 = normal
1-5 = boring
Return only a single number, nothing else.

Headline: {title}
Description: {description[:300]}"""
    result = call_llm(prompt, max_tokens=10)
    if result and result.isdigit():
        return int(result)
    return 5

def generate_post(title: str, content: str) -> str:
    prompt = f"""Write a short, engaging Telegram post (max 800 characters) based on this football news. 
Use emojis moderately. The post should be punchy, easy to read, and end with a question: "👇 Your opinion?"
Do not mention sources like BBC, ESPN, etc. Write in English.

Headline: {title}
Content: {content[:800]}

Telegram post:"""
    post = call_llm(prompt, max_tokens=500)
    if not post or len(post) < 20:
        post = f"⚽ {title}\n\n👇 Your opinion?"
    if len(post) > 800:
        post = post[:800]
    return post

async def send_post(bot, title, url, description, rss_image):
    print(f"📰 Обработка: {title[:60]}...")
    page_image, full_text = await asyncio.to_thread(fetch_page_image_and_text, url)
    if not full_text:
        full_text = description if description else title
    image_bytes = None
    if page_image or rss_image:
        img_url = page_image or rss_image
        if img_url.startswith(('http://', 'https://')):
            image_bytes = await asyncio.to_thread(download_image, img_url)
    post = await asyncio.to_thread(generate_post, title, full_text)
    try:
        if image_bytes:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=InputFile(image_bytes, filename="news.jpg"), caption=post, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=post, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Опубликовано: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=post)
            return True
        except:
            return False

async def main():
    print("🚀 Редакторский бот (фильтр по ключевым словам + LLM оценка без rate limit)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Сбор новостей
    raw_news = []
    for feed in RSS_FEEDS:
        print(f"📡 RSS: {feed}")
        items = await asyncio.to_thread(fetch_rss_items, feed)
        print(f"   Найдено {len(items)}")
        for title, link, desc, pub_date, img in items:
            if title and link and is_football_news(title, desc or ""):
                raw_news.append((title, link, desc, pub_date, img))
        if len(raw_news) >= MAX_NEWS_TO_COLLECT:
            break
    print(f"Собрано футбольных новостей: {len(raw_news)}")
    if not raw_news:
        return

    # Убираем дубли по заголовкам (простейший способ)
    unique = []
    seen = set()
    for title, link, desc, pub_date, img in raw_news:
        key = title.lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append((title, link, desc, pub_date, img))
    print(f"Уникальных: {len(unique)}")

    # Берём только первые MAX_NEWS_TO_EVALUATE (чтобы не превысить лимит)
    candidates = unique[:MAX_NEWS_TO_EVALUATE]
    print(f"Оцениваем {len(candidates)} новостей (с паузой {REQUEST_DELAY} сек между запросами)")

    rated = []
    for idx, (title, link, desc, pub_date, img) in enumerate(candidates):
        print(f"Оценка {idx+1}/{len(candidates)}: {title[:50]}...")
        score = await asyncio.to_thread(rate_article, title, desc or "")
        rated.append((score, title, link, desc, img))
        if idx < len(candidates) - 1:
            await asyncio.sleep(REQUEST_DELAY)

    # Сортируем по убыванию оценки
    rated.sort(key=lambda x: x[0], reverse=True)
    top = [item for item in rated if item[0] >= 7][:FINAL_POSTS_COUNT]
    if not top:
        print("Нет новостей с рейтингом >= 7")
        return

    print(f"Публикуем {len(top)} лучших новостей")
    for score, title, link, desc, img in top:
        success = await send_post(bot, title, link, desc, img)
        if success and len(top) > 1:
            await asyncio.sleep(10)

    print("✨ Готово")

if __name__ == "__main__":
    asyncio.run(main())
