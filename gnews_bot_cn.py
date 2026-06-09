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

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 1          # можно увеличить до 2-3, но учтите лимит токенов
MAX_AGE_HOURS = 72                # новости не старше 3 дней
RSS_TIMEOUT = 12
PAGE_TIMEOUT = 15
MISTRAL_MODEL = "mistral-tiny"    # бесплатная модель (или "mistral-small" для лучшего качества)

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
        resp = requests.get(feed_url, timeout=RSS_TIMEOUT)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        items = []
        # RSS 2.0
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")
            # картинка
            image = None
            enc = item.find("enclosure")
            if enc is not None and enc.get("type", "").startswith("image"):
                image = enc.get("url")
            if not image:
                media = item.find("{http://search.yahoo.com/mrss/}content")
                if media is not None:
                    image = media.get("url")
            items.append((title, link, desc, pub_date, image))
        # если нет item, пробуем Atom
        if not items:
            for entry in root.findall(".//entry"):
                title = entry.findtext("title", "")
                link_el = entry.find("link")
                link = link_el.get("href") if link_el is not None else ""
                desc = entry.findtext("summary", "")
                pub_date = entry.findtext("published", "")
                image = None
                items.append((title, link, desc, pub_date, image))
        return items
    except Exception as e:
        print(f"Ошибка RSS {feed_url}: {e}")
        return []

def fetch_page_image(url: str) -> str | None:
    """Пытается достать og:image со страницы"""
    try:
        resp = requests.get(url, timeout=PAGE_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', resp.text)
        if match:
            return match.group(1)
        return None
    except:
        return None

def summarize_with_mistral(text: str, max_chars: int = 900) -> str:
    """Отправляет текст в Mistral API и возвращает краткий пересказ"""
    if not text or len(text) < 50:
        return text
    prompt = f"Перескажи эту футбольную новость кратко, только самое важное, без рекламы и лишних деталей. Ограничься 500-700 символами. Новость:\n{text[:2000]}"
    try:
        response = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MISTRAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 300
            },
            timeout=25
        )
        if response.status_code == 200:
            data = response.json()
            summary = data["choices"][0]["message"]["content"].strip()
            if len(summary) > max_chars:
                summary = summary[:max_chars-3] + "..."
            return summary
        else:
            print(f"Mistral ошибка {response.status_code}: {response.text[:200]}")
            return text
    except Exception as e:
        print(f"Mistral исключение: {e}")
        return text

async def send_article(bot, title, url, description, rss_image):
    # Парсим картинку со страницы, если в RSS нет
    image = rss_image
    if not image:
        image = await asyncio.to_thread(fetch_page_image, url)
    # Пытаемся получить полный текст новости (первые абзацы)
    full_text = description
    if len(full_text) < 200:
        # если описание короткое, попробуем спарсить несколько абзацев со страницы (без заморочек)
        try:
            resp = requests.get(url, timeout=PAGE_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                # убираем скрипты
                clean = re.sub(r'<script.*?</script>', '', resp.text, flags=re.DOTALL)
                clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
                paras = re.findall(r'<p[^>]*>(.*?)</p>', clean, re.DOTALL)
                texts = []
                for p in paras:
                    txt = re.sub(r'<[^>]+>', '', p).strip()
                    if len(txt) > 40 and not txt.startswith("Читать"):
                        texts.append(txt)
                    if len(texts) >= 6:
                        break
                if texts:
                    full_text = "\n\n".join(texts)
        except:
            pass
    # Перефразирование через Mistral
    if full_text:
        summarized = await asyncio.to_thread(summarize_with_mistral, full_text, 900)
    else:
        summarized = "Нет текста новости."
    safe_title = html.escape(title)
    caption = f"⚽ <b>{safe_title}</b>\n\n{summarized}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    try:
        if image:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        print(f"✅ Отправлено: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        # пробуем отправить без HTML
        try:
            plain = f"⚽ {title}\n\n{summarized}"
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain)
            return True
        except:
            return False

async def main():
    print("🚀 Запуск футбольного бота с Mistral AI")
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
    # сортируем по дате (новые сверху)
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
