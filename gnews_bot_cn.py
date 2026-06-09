import asyncio
import os
import html
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
import feedparser
import requests
from bs4 import BeautifulSoup

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    print("❌ Ошибка: не загружены TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    exit(1)

# ================== НАСТРОЙКИ ==================
MAX_ARTICLES_PER_RUN = 1          # только 1 новость за раз для стабильности
MAX_AGE_HOURS = 72
SEND_INTERVAL_SEC = 20
TIMEOUT_RSS = 10                  # таймаут на загрузку RSS в секундах
TIMEOUT_PAGE = 15                 # таймаут на парсинг страницы

RSS_FEEDS = [
    "http://www.rusfootball.info/rss.xml",
    "http://www.euro-football.ru/news/news_xml_redtram.php3",
    "http://www.gazeta.ru/export/rss/sportnews.xml",
    "http://fanat1k.ru/e107_plugins/rss_menu/rss.php?news.2",
    "https://news.sportbox.ru/taxonomy/term/12216/0/feed"
]

FOOTBALL_KEYWORDS = [
    "футбол", "soccer", "football", "чемпионат", "лига чемпионов",
    "евро", "кубок", "гол", "матч", "тренер", "игрок", "стадион",
    "рфпл", "премьер-лига", "ла лига", "серия а", "бундеслига"
]

BLACKLIST_WORDS = [
    "американский футбол", "nfl", "super bowl", "тревис келси", "travis kelce",
    "тейлор свифт", "taylor swift", "свадьба", "баскетбол", "нба", "теннис"
]

# ================== ФИЛЬТРЫ ==================
def is_football_article(title: str, description: str) -> bool:
    text = (title + " " + (description or "")).lower()
    for bad in BLACKLIST_WORDS:
        if bad in text:
            return False
    for good in FOOTBALL_KEYWORDS:
        if good in text:
            return True
    return False

def is_recent(published_struct) -> bool:
    if not published_struct:
        return False
    try:
        if hasattr(published_struct, 'tm_year'):
            pub_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
        elif isinstance(published_struct, datetime):
            pub_dt = published_struct
        else:
            return False
        now = datetime.now(timezone.utc)
        age = now - pub_dt
        return age.total_seconds() <= MAX_AGE_HOURS * 3600
    except Exception:
        return False

# ================== ПАРСИНГ RSS (с таймаутом) ==================
def fetch_rss_with_timeout(feed_url):
    try:
        # feedparser.parse может зависнуть, поэтому запускаем в отдельном потоке с таймаутом
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(feedparser.parse, feed_url)
            return future.result(timeout=TIMEOUT_RSS)
    except concurrent.futures.TimeoutError:
        print(f"   ⏰ Таймаут RSS: {feed_url}")
        return None
    except Exception as e:
        print(f"   ❌ Ошибка RSS {feed_url}: {e}")
        return None

def fetch_rss_news():
    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 Парсим RSS: {feed_url}")
        feed = fetch_rss_with_timeout(feed_url)
        if not feed:
            continue
        if feed.bozo:
            print(f"   ⚠️ Ошибка парсинга: {feed.bozo_exception}")
        for entry in feed.entries[:10]:
            title = entry.get('title', '')
            link = entry.get('link', '')
            description = entry.get('summary', entry.get('description', ''))
            if description:
                soup = BeautifulSoup(description, 'html.parser')
                description = soup.get_text(separator=' ', strip=True)
            # Картинка из RSS
            image_url = None
            if 'media_content' in entry and entry.media_content:
                image_url = entry.media_content[0].get('url')
            elif 'enclosures' in entry and entry.enclosures:
                for enc in entry.enclosures:
                    if enc.get('type', '').startswith('image'):
                        image_url = enc.get('href')
                        break
            published = entry.get('published_parsed')
            if not title or not link:
                continue
            if not is_recent(published):
                continue
            if not is_football_article(title, description):
                continue
            all_news.append({
                'title': title,
                'url': link,
                'description': description,
                'image': image_url,
                'published': published,
            })
    # Убираем дубли по URL
    unique = {}
    for item in all_news:
        if item['url'] not in unique:
            unique[item['url']] = item
    return list(unique.values())

# ================== ПАРСИНГ СТРАНИЦЫ (с таймаутом) ==================
def fetch_full_article(url: str):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        resp = requests.get(url, timeout=TIMEOUT_PAGE, headers=headers)
        if resp.status_code != 200:
            return "", None
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Картинка
        image_url = None
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
        else:
            for img in soup.find_all('img'):
                src = img.get('src')
                if src and ('photo' in src or 'image' in src or 'news' in src):
                    if not src.startswith('http'):
                        src = requests.compat.urljoin(url, src)
                    image_url = src
                    break
        # Текст
        content = None
        for selector in ['article', '.article-content', '.post-content', '.entry-content', '.content', '#main-content', '.news-detail', '.story__body']:
            container = soup.select_one(selector)
            if container:
                content = container
                break
        if not content:
            content = soup
        for tag in content(['script', 'style', 'nav', 'footer', 'aside', 'form', 'button', 'meta', 'link']):
            tag.decompose()
        paragraphs = content.find_all('p')
        text_parts = []
        for p in paragraphs:
            txt = p.get_text(strip=True)
            if len(txt) > 40 and not txt.startswith('Читать'):
                txt = re.sub(r'\d+\s*comment\s*', '', txt, flags=re.IGNORECASE)
                txt = re.sub(r'Подписаться|Источник|Ссылка|Фото:', '', txt)
                text_parts.append(txt)
        full_text = '\n\n'.join(text_parts)
        if len(full_text) > 3900:
            full_text = full_text[:3900] + '...'
        return full_text, image_url
    except Exception as e:
        print(f"⚠️ Ошибка парсинга {url}: {e}")
        return "", None

# ================== ОТПРАВКА ==================
async def send_article(bot: Bot, article: dict):
    title = article['title']
    url = article['url']
    rss_desc = article.get('description', '')
    rss_image = article.get('image')
    
    print(f"📰 Обработка: {title[:60]}...")
    # Парсим полный текст и картинку (с ограничением по времени)
    try:
        full_text, page_image = await asyncio.wait_for(
            asyncio.to_thread(fetch_full_article, url),
            timeout=TIMEOUT_PAGE + 5
        )
    except asyncio.TimeoutError:
        print(f"   ⏰ Таймаут парсинга страницы: {url[:80]}...")
        full_text, page_image = rss_desc, rss_image
    if not full_text:
        full_text = rss_desc if rss_desc else "Нет текста."
    image_url = page_image or rss_image
    
    safe_title = html.escape(title)
    caption = f"⚽ <b>{safe_title}</b>\n\n{full_text[:800]}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    
    try:
        if image_url:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
            print(f"✅ Отправлено фото с подписью: {title[:60]}...")
            if len(full_text) > 800:
                text_message = f"<b>{safe_title}</b>\n\n{full_text}"
                if len(text_message) > 4096:
                    text_message = text_message[:4093] + "..."
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text_message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                print(f"   ➕ Отправлен полный текст")
        else:
            message = f"⚽ <b>{safe_title}</b>\n\n{full_text}"
            if len(message) > 4096:
                message = message[:4093] + "..."
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
            print(f"✅ Отправлен текст (без фото): {title[:60]}...")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        try:
            plain = f"⚽ {title}\n\n{full_text}"
            if len(plain) > 4096:
                plain = plain[:4093] + "..."
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain)
            print("   ✅ Отправлено в plain-режиме")
            return True
        except Exception as e2:
            print(f"   ❌ Не удалось: {e2}")
            return False

# ================== MAIN С ГЛОБАЛЬНЫМ ТАЙМАУТОМ ==================
async def main():
    print("🚀 Запуск футбольного бота (фото + полный текст, с таймаутами)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        # Устанавливаем общий таймаут на выполнение всего скрипта (2 минуты)
        await asyncio.wait_for(inner_main(bot), timeout=120)
    except asyncio.TimeoutError:
        print("⏰ Глобальный таймаут: скрипт выполнялся слишком долго, прерываем.")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
    print("✨ Скрипт завершён.")

async def inner_main(bot: Bot):
    # Получаем новости из RSS (эта функция синхронная, запустим в потоке)
    news = await asyncio.to_thread(fetch_rss_news)
    if not news:
        print("Нет свежих футбольных новостей.")
        return
    news.sort(key=lambda x: x.get('published'), reverse=True)
    print(f"Найдено {len(news)} новостей. Отправлю {min(len(news), MAX_ARTICLES_PER_RUN)}.")
    sent = 0
    for item in news[:MAX_ARTICLES_PER_RUN]:
        if await send_article(bot, item):
            sent += 1
            if sent < MAX_ARTICLES_PER_RUN:
                await asyncio.sleep(SEND_INTERVAL_SEC)
    print(f"✨ Готово. Отправлено {sent}.")

if __name__ == "__main__":
    asyncio.run(main())
