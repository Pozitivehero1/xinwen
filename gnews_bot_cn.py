import asyncio
import os
import html
import re
import xml.etree.ElementTree as ET
import requests
import io
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from dotenv import load_dotenv
from telegram import Bot, InputFile
from telegram.constants import ParseMode

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MISTRAL_API_KEY]):
    print("❌ Ошибка: не хватает переменных окружения")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 1          # сколько новостей за раз (можно увеличить до 2-3)
MAX_AGE_HOURS = 72                # не старше 3 дней
RSS_TIMEOUT = 12
PAGE_TIMEOUT = 12
MISTRAL_MODEL = "mistral-tiny"    # бесплатно

# Список RSS-лент (упор на зарубежный футбол)
RSS_FEEDS = [
    "https://www.sports.ru/rss/",                                   # Sports.ru – много зарубежного
    "https://www.championat.com/rss/news/football.xml",             # Чемпионат – футбол
    "http://feeds.bbci.co.uk/sport/football/rss.xml",               # BBC World Football (англ.)
    "http://www.rusfootball.info/rss.xml",                          # запасной (будет меньше приоритет)
]

# Белый список – ключевые слова футбола (можно дополнять)
FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов", "евро",
    "кубок", "гол", "матч", "тренер", "игрок", "стадион", "рфпл", "премьер-лига",
    "ла лига", "серия а", "бундеслига", "лига 1", "апл", "уефа",
    "локомотив", "спартак", "зенит", "цска", "динамо", "краснодар", "ростов",
    "реал", "барселона", "бавария", "псж", "манчестер", "ливерпуль", "арсенал",
    "челси", "ювентус", "милан", "интер", "трансфер", "контракт", "слух"
]

# Чёрный список – что исключаем (баскетбол, теннис, российские соревнования)
BLACKLIST_WORDS = [
    "баскетбол", "нба", "теннис", "хоккей", "американский футбол", "nfl",
    "тейлор свифт", "свадьба", "тревис келси",
    "рфпл", "рпл", "фнл", "кубок россии", "россия", "российский",
    "зенит", "спартак", "цска", "локомотив", "краснодар", "динамо", "арсенал тула",
    "ахмат", "рубин", "ростов", "химки", "конкурс рфпл", "матч премьер-лиги россии"
]

# Приоритеты ключевых слов (чем выше число, тем важнее)
PRIORITY_KEYWORDS = {
    3: [
        "реал мадрид", "барселона", "бавария", "псж", "манчестер сити",
        "манчестер юнайтед", "ливерпуль", "арсенал", "челси", "ювентус",
        "милан", "интер", "наполи", "лига чемпионов", "уефа", "апл",
        "премьер-лига", "ла лига", "серия а", "бундеслига", "лига 1"
    ],
    2: [
        "трансфер", "слух", "контракт", "продлил", "подписал", "переход",
        "аренда", "агент", "зарплата", "бонус", "клаусула", "сумма сделки"
    ],
    1: [
        "мбappe", "холанд", "винисиус", "беллингем", "кейн", "салах", "де брюйне",
        "левандовски", "неймар", "месси", "роналду", "модрич", "ван дейк",
        "куртуа", "доннарумма", "нойер","педри", "ольмо", "ямаль", "винисиус", "вальверде", "моуриньо", "сборная франции", "сборная англии",
        "сборная бразилии", "сборная аргентины", "сборная испании",
        "сборная германии", "сборная португалии", "сборная нидерландов",
        "чемпионат мира", "кубок африки", "кубок америки", "евро"
    ],
}

# ================== ФУНКЦИИ ==================
def is_football_article(title: str, desc: str) -> bool:
    text = (title + " " + (desc or "")).lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    return any(kw in text for kw in FOOTBALL_KEYWORDS)

def compute_priority(title: str, desc: str) -> int:
    text = (title + " " + (desc or "")).lower()
    for weight, keywords in PRIORITY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return weight
    return 0

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

        # Поиск картинки
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

        # Парсинг текста
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

def download_image(image_url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(image_url, timeout=10, headers=headers)
        if resp.status_code == 200 and resp.headers.get('content-type', '').startswith('image/'):
            return io.BytesIO(resp.content)
        else:
            print(f"   Не удалось скачать картинку, статус {resp.status_code}")
            return None
    except Exception as e:
        print(f"   Ошибка скачивания картинки: {e}")
        return None

def summarize_with_mistral(text: str) -> str:
    if not text or len(text) < 50:
        return text
    prompt = f"""Ты спортивный журналист. Перескажи эту футбольную новость кратко, но содержательно, примерно 300-500 символов. Выдели главные события, имена, счёт, интригу. Не добавляй рекламу, не упоминай сайт. Новость:

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
                "max_tokens": 400
            },
            timeout=20
        )
        if resp.status_code == 200:
            data = resp.json()
            summary = data["choices"][0]["message"]["content"].strip()
            if any(bad in summary for bad in ["если известен", "уточнить", "X:X", "ключевые моменты"]):
                print("   Пересказ содержит шаблонный мусор, используем оригинальный текст")
                return text[:800]
            if len(summary) > 800:
                summary = summary[:800] + "..."
            return summary
        else:
            print(f"Mistral ошибка {resp.status_code}")
            return text
    except Exception as e:
        print(f"Mistral исключение: {e}")
        return text

def load_sent_urls():
    if not os.path.exists('sent_urls.txt'):
        return set()
    with open('sent_urls.txt', 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_urls(urls_set):
    with open('sent_urls.txt', 'w', encoding='utf-8') as f:
        for url in urls_set:
            f.write(url + '\n')

async def send_article(bot, title, url, description, rss_image):
    print(f"📰 Загружаем страницу: {title[:50]}...")
    page_image, full_text = await asyncio.to_thread(fetch_page_image_and_text, url)
    if not full_text:
        full_text = description if description else ""

    candidate = page_image or rss_image
    image_bytes = None
    if candidate and candidate.startswith(('http://', 'https://')):
        print(f"   Скачиваем картинку: {candidate[:80]}")
        image_bytes = await asyncio.to_thread(download_image, candidate)
        if image_bytes:
            print("   Картинка успешно скачана")
        else:
            print("   Не удалось скачать картинку, отправляем без фото")

    if not full_text:
        print("   Нет текста новости")
        return False

    summary = await asyncio.to_thread(summarize_with_mistral, full_text)
    safe_title = html.escape(title)
    caption = f"⚽ <b>{safe_title}</b>\n\n{summary}"
    if len(caption) > 1024:
        max_summary_len = 1024 - len(f"⚽ <b>{safe_title}</b>\n\n") - 3
        summary = summary[:max_summary_len] + "..."
        caption = f"⚽ <b>{safe_title}</b>\n\n{summary}"

    try:
        if image_bytes:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=InputFile(image_bytes, filename="news.jpg"), caption=caption, parse_mode=ParseMode.HTML)
            print(f"✅ Отправлено с фото: {title[:60]}")
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
            print(f"✅ Отправлено текстом: {title[:60]}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        if image_bytes:
            try:
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                print("   ✅ Отправлено текстом после ошибки с фото")
                return True
            except Exception as e2:
                print(f"   ❌ Не удалось: {e2}")
        return False

async def main():
    print("🚀 Запуск футбольного бота (приоритет топ-клубов и трансферов)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    sent_urls = load_sent_urls()
    print(f"Загружено {len(sent_urls)} ранее отправленных URL")

    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 RSS: {feed_url}")
        items = await asyncio.to_thread(fetch_rss_items, feed_url)
        print(f"   Найдено {len(items)} записей")
        for title, link, desc, pub_date, rss_image in items:
            if not title or not link:
                continue
            if link in sent_urls:
                continue
            if not is_football_article(title, desc):
                continue
            if not is_recent(pub_date):
                continue
            priority = compute_priority(title, desc)
            all_news.append({
                'title': title,
                'url': link,
                'description': desc,
                'image': rss_image,
                'pub_date': pub_date,
                'priority': priority
            })

    if not all_news:
        print("Нет новых футбольных новостей.")
        return

    # Сортировка: сначала высокий приоритет, затем по дате (новые сверху)
    all_news.sort(key=lambda x: (-x['priority'], parse_rss_date(x['pub_date']) or datetime.min), reverse=False)

    to_send = all_news[:MAX_ARTICLES_PER_RUN]
    print(f"Отправляю {len(to_send)} новостей...")
    for item in to_send:
        success = await send_article(bot, item['title'], item['url'], item['description'], item['image'])
        if success:
            sent_urls.add(item['url'])
    save_sent_urls(sent_urls)
    print(f"✨ Готово. Отправлено {len(to_send)}. Всего сохранено URL: {len(sent_urls)}")

if __name__ == "__main__":
    asyncio.run(main())
