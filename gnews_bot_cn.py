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
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MISTRAL_API_KEY]):
    print("❌ Ошибка: не хватает переменных окружения")
    exit(1)

MAX_ARTICLES_PER_RUN = 1
MAX_AGE_HOURS = 72
RSS_TIMEOUT = 12
PAGE_TIMEOUT = 10
MISTRAL_MODEL = "mistral-tiny"  # или "mistral-small" для лучшего качества, но tiny бесплатно

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
    """Возвращает (image_url, full_text)"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, timeout=PAGE_TIMEOUT, headers=headers)
        if resp.status_code != 200:
            return None, None
        html_content = resp.text
        # og:image
        image = None
        match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html_content)
        if not match:
            match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html_content)
        if match:
            image = match.group(1)
        # извлечение текста (удаляем скрипты, стили, мусор)
        clean = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL)
        clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
        # ищем параграфы
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.DOTALL)
        texts = []
        for p in paragraphs:
            text = re.sub(r'<[^>]+>', '', p).strip()
            if len(text) > 40 and not text.startswith("Читать") and not text.startswith("Источник"):
                text = re.sub(r'\d+\s*comment', '', text, flags=re.IGNORECASE)
                texts.append(text)
            if len(texts) >= 15:
                break
        full_text = "\n\n".join(texts)
        if len(full_text) > 3000:
            full_text = full_text[:3000] + "..."
        return image, full_text
    except Exception as e:
        print(f"   Ошибка загрузки страницы {url}: {e}")
        return None, None

def summarize_with_mistral(text: str, max_tokens: int = 350) -> str:
    """Пересказ новости, достаточно подробный, но укладывающийся в лимит подписи к фото"""
    if not text or len(text) < 50:
        return text
    prompt = f"""Ты спортивный журналист. Перескажи эту футбольную новость кратко, но содержательно, До 1024 символов. Выдели главные события, имена, счёт, интригу. Не добавляй рекламу, не упоминай сайт. Новость:

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
                "temperature": 0.4,
                "max_tokens": max_tokens
            },
            timeout=20
        )
        if resp.status_code == 200:
            data = resp.json()
            summary = data["choices"][0]["message"]["content"].strip()
            # обрезаем, если слишком длинное (лимит подписи 1024, но мы потом ещё заголовок добавим)
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
    # Пытаемся получить картинку и полный текст со страницы
    print(f"📰 Загружаем страницу: {title[:50]}...")
    page_image, full_text = await asyncio.to_thread(fetch_page_image_and_text, url)
    if not full_text:
        full_text = description if description else ""
    if not page_image:
        page_image = rss_image
    # Если нет текста, ничего не отправляем
    if not full_text:
        print("   Нет текста новости")
        return False
    # Пересказываем через Mistral
    summary = await asyncio.to_thread(summarize_with_mistral, full_text, 400)
    safe_title = html.escape(title)
    # Формируем подпись: заголовок + пересказ
    caption = f"⚽ <b>{safe_title}</b>\n\n{summary}"
    if len(caption) > 1024:
        # обрезаем пересказ, если нужно
        excess = len(caption) - 1020
        summary = summary[:-excess] + "..."
        caption = f"⚽ <b>{safe_title}</b>\n\n{summary}"
    try:
        if page_image:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=page_image, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

async def main():
    print("🚀 Запуск футбольного бота с Mistral AI (картинки + пересказ)")
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
