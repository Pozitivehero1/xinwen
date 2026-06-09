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
MAX_ARTICLES_PER_RUN = 1          # сколько новостей за раз (можно увеличить до 2-3)
MAX_AGE_HOURS = 72
SEND_INTERVAL_SEC = 20

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

# ================== ПАРСИНГ RSS ==================
def fetch_rss_news():
    all_news = []
    for feed_url in RSS_FEEDS:
        print(f"📡 Парсим RSS: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
            if feed.bozo:
                print(f"   ⚠️ Ошибка: {feed.bozo_exception}")
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                link = entry.get('link', '')
                # Описание из RSS
                description = entry.get('summary', entry.get('description', ''))
                if description:
                    soup = BeautifulSoup(description, 'html.parser')
                    description = soup.get_text(separator=' ', strip=True)
                # Картинка из RSS (если есть)
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
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
    # Убираем дубли по URL
    unique = {}
    for item in all_news:
        if item['url'] not in unique:
            unique[item['url']] = item
    return list(unique.values())

# ================== ПАРСИНГ ПОЛНОГО ТЕКСТА И КАРТИНКИ СО СТРАНИЦЫ ==================
def fetch_full_article(url: str):
    """Возвращает (полный_текст, url_картинки)"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        resp = requests.get(url, timeout=20, headers=headers)
        if resp.status_code != 200:
            return "", None
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. Ищем картинку
        image_url = None
        # Мета-теги Open Graph
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
        else:
            # Первая крупная картинка в статье
            for img in soup.find_all('img'):
                src = img.get('src')
                if src and ('photo' in src or 'image' in src or 'news' in src):
                    if not src.startswith('http'):
                        src = requests.compat.urljoin(url, src)
                    image_url = src
                    break
        # 2. Ищем текст статьи
        # Пробуем разные контейнеры
        content = None
        for selector in ['article', '.article-content', '.post-content', '.entry-content', '.content', '#main-content', '.news-detail', '.story__body']:
            container = soup.select_one(selector)
            if container:
                content = container
                break
        if not content:
            content = soup
        
        # Удаляем скрипты, стили, комментарии
        for tag in content(['script', 'style', 'nav', 'footer', 'aside', 'form', 'button', 'meta', 'link']):
            tag.decompose()
        
        # Собираем абзацы
        paragraphs = content.find_all('p')
        text_parts = []
        for p in paragraphs:
            txt = p.get_text(strip=True)
            if len(txt) > 40 and not txt.startswith('Читать'):
                # Убираем мусорные фразы
                txt = re.sub(r'\d+\s*comment\s*', '', txt, flags=re.IGNORECASE)
                txt = re.sub(r'Подписаться|Источник|Ссылка|Фото:', '', txt)
                text_parts.append(txt)
        full_text = '\n\n'.join(text_parts)
        # Ограничим длину (Telegram лимит сообщения 4096)
        if len(full_text) > 3900:
            full_text = full_text[:3900] + '...'
        return full_text, image_url
    except Exception as e:
        print(f"⚠️ Ошибка парсинга {url}: {e}")
        return "", None

# ================== ОТПРАВКА В TELEGRAM ==================
async def send_article(bot: Bot, article: dict):
    title = article['title']
    url = article['url']
    rss_desc = article.get('description', '')
    rss_image = article.get('image')
    
    print(f"📰 Обработка: {title[:60]}...")
    # Парсим полный текст и картинку со страницы
    full_text, page_image = fetch_full_article(url)
    if not full_text:
        full_text = rss_desc if rss_desc else "Нет текста."
    # Берём картинку: сначала со страницы, потом из RSS
    image_url = page_image or rss_image
    
    safe_title = html.escape(title)
    # Для подписи к фото (максимум 1024 символа)
    caption = f"⚽ <b>{safe_title}</b>\n\n{full_text[:800]}"
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    
    try:
        if image_url:
            # Отправляем фото с подписью
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
            print(f"✅ Отправлено фото с подписью: {title[:60]}...")
            # Если полный текст не влез в подпись и он длиннее 800 символов – отправляем его отдельно
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
            # Нет фото – отправляем полный текст
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
        # Резерв – текст без HTML
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

# ================== MAIN ==================
async def main():
    print("🚀 Запуск футбольного бота (фото + полный текст)")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    news = fetch_rss_news()
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
