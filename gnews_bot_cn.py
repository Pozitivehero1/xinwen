import asyncio
import os
import html
import re
import xml.etree.ElementTree as ET
import requests
import io
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from difflib import SequenceMatcher
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
MAX_ARTICLES_PER_POST = 3          # сколько публиковать за раз (топ-N)
MAX_NEWS_TO_COLLECT = 100          # максимум новостей за сбор
MIN_SCORE = 8                      # минимальная оценка для публикации
RSS_TIMEOUT = 12
PAGE_TIMEOUT = 12

RSS_FEEDS = [
    "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.espn.com/espn/rss/soccer/news",
    "https://www.goal.com/feeds/en/news",
    "https://www.skysports.com/rss/12040",
    "https://www.transfermarkt.com/rss/news",
    "https://www.footballinsider247.com/feed/",
    "https://www.sports.ru/rss/",
    "https://www.championat.com/rss/news/football.xml",
    "https://www.football365.com/feed",
    "https://talksport.com/football/feed/",
]

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

def is_duplicate(article1, article2):
    """Проверка на дубли по заголовку (нечёткое сравнение)"""
    title1 = article1[0].lower()
    title2 = article2[0].lower()
    return SequenceMatcher(None, title1, title2).ratio() > 0.8

def remove_duplicates(articles):
    unique = []
    for art in articles:
        is_dup = False
        for existing in unique:
            if is_duplicate(art, existing):
                is_dup = True
                break
        if not is_dup:
            unique.append(art)
    return unique

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
        if not image:
            img_tags = re.findall(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html_content)
            for img_url in img_tags:
                if any(ext in img_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    if not img_url.startswith('http'):
                        img_url = urljoin(url, img_url)
                    if 'logo' not in img_url.lower() and 'icon' not in img_url.lower():
                        image = img_url
                        break
        # текст
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
        else:
            return None
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
        return None

def call_llm(prompt: str, max_tokens: int = 300) -> str | None:
    """Вызов бесплатной модели OpenRouter (Nemotron 3 Ultra или Gemma 4)"""
    # Можно выбрать любую из бесплатных. Я ставлю Nemotron 3 Ultra — она мощная.
    model = "nvidia/nemotron-3-ultra:free"   # абсолютно бесплатно
    # Альтернативы: "google/gemma-4-31b:free", "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-next-80b-a3b:free"
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
            timeout=25
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            print(f"LLM ошибка {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"LLM исключение: {e}")
        return None

def rate_article(title: str, description: str) -> int:
    """Оценивает новость от 1 до 10"""
    prompt = f"""Оцени новость от 1 до 10 по интересности для болельщика.
10 - мировая сенсация, 8-9 - очень интересно, 6-7 - нормально, 1-5 - скучно.
Плюс за: трансферы, увольнения, травмы, конфликты, результаты матчей.
Минус за: чиновников, документы, заседания, юридические вопросы.
Верни только число (цифру) и ничего больше.

Заголовок: {title}
Описание: {description[:500]}"""
    result = call_llm(prompt, max_tokens=10)
    if result and result.isdigit():
        return int(result)
    return 5  # средняя оценка по умолчанию

def generate_post(title: str, content: str) -> str:
    """Генерирует короткий пост (до 800 символов) с эмодзи и вопросом"""
    prompt = f"""Ты — футбольный редактор. Сделай короткий пост для Telegram по этой новости.
Требования:
- Максимум 800 символов
- Используй эмодзи для акцента (но не перебарщивай)
- Разбей текст на короткие строки
- В конце добавь строку: "👇 Ваше мнение?"
- Не упоминай источники
- Пиши энергично, как для молодой аудитории

Заголовок: {title}
Текст новости: {content[:1500]}

Пост:"""
    post = call_llm(prompt, max_tokens=600)
    if not post:
        post = f"⚽ {title}\n\n{content[:500]}\n\n👇 Ваше мнение?"
    if len(post) > 800:
        post = post[:800]
    return post

async def send_article(bot, title, url, description, rss_image):
    print(f"📰 Обработка: {title[:50]}...")
    page_image, full_text = await asyncio.to_thread(fetch_page_image_and_text, url)
    if not full_text:
        full_text = description if description else ""
    candidate = page_image or rss_image
    image_bytes = None
    if candidate and candidate.startswith(('http://', 'https://')):
        image_bytes = await asyncio.to_thread(download_image, candidate)
    if not full_text:
        print("   Нет текста")
        return False
    post = await asyncio.to_thread(generate_post, title, full_text)
    try:
        if image_bytes:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=InputFile(image_bytes, filename="news.jpg"), caption=post, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=post, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        try:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=post)
            return True
        except:
            return False

async def main():
    print("🚀 Редакторский бот (OpenRouter, оценка + дайджест)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    # Сбор новостей
    all_news = []
    for feed in RSS_FEEDS:
        print(f"📡 RSS: {feed}")
        items = await asyncio.to_thread(fetch_rss_items, feed)
        print(f"   Найдено {len(items)}")
        for title, link, desc, pub_date, img in items:
            if title and link:
                all_news.append((title, link, desc, pub_date, img))
        if len(all_news) >= MAX_NEWS_TO_COLLECT:
            break
    print(f"Собрано {len(all_news)} новостей")
    # Дедупликация
    unique_news = remove_duplicates(all_news)
    print(f"Уникальных {len(unique_news)}")
    # Оценка LLM
    rated = []
    for idx, (title, link, desc, pub_date, img) in enumerate(unique_news):
        print(f"Оценка {idx+1}/{len(unique_news)}: {title[:50]}...")
        score = await asyncio.to_thread(rate_article, title, desc or "")
        rated.append((score, title, link, desc, img, pub_date))
    rated.sort(reverse=True)
    top_news = [n for n in rated if n[0] >= MIN_SCORE][:MAX_ARTICLES_PER_POST]
    if not top_news:
        print("Нет новостей с высоким рейтингом")
        return
    print(f"Публикую {len(top_news)} лучших новостей")
    for score, title, link, desc, img, pub_date in top_news:
        success = await send_article(bot, title, link, desc, img)
        if success and len(top_news) > 1:
            await asyncio.sleep(15)
    print("✨ Готово")

if __name__ == "__main__":
    asyncio.run(main())
