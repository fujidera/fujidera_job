import asyncio
import re
import os
import random
import time
import logging
from datetime import datetime, timezone

from playwright.async_api import async_playwright
from curl_cffi import requests as curl_requests
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
tg_session = curl_requests.Session()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15"
]

# --- TELEGRAM ЛОГИКА ---
def get_updates(offset):
    try:
        r = tg_session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json().get("result", [])
    except Exception: pass
    return []

def wait_for_text(last_id, timeout_minutes=10):
    start = time.time()
    while time.time() - start < timeout_minutes * 60:
        updates = get_updates(offset=last_id + 1)
        for up in updates:
            last_id = max(last_id, up["update_id"])
            if "message" in up and "text" in up["message"]:
                return up["message"]["text"], last_id
        time.sleep(2)
    return None, last_id

def send_tg(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        tg_session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

# --- ТВОЯ ТЕХНИЧЕСКАЯ ЧАСТЬ (ПАРСИНГ КАРТОЧКИ) ---
async def get_listing_details(page, url, retries=2):
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            try:
                await asyncio.wait_for(page.wait_for_selector("body", timeout=5000), timeout=6.0)
            except:
                return None

            await page.wait_for_timeout(500)
            page_text = await page.inner_text("body")

            # ФИЛЬТР: Магазин
            website_link = page.locator("a:has-text('Website'), a:has-text('Bezoek website')")
            if await website_link.count() > 0 and await website_link.is_visible():
                logger.info("   ⏭️ Пропущено (магазин)")
                return None

            # 1. Просмотры
            views = 0
            views_match = re.search(r'(\d+(?:\.\d+)*)\s*(?:x|keer)\s*bekeken', page_text, re.IGNORECASE)
            if views_match:
                views = int(views_match.group(1).replace('.', ''))

            # 2. Избранное
            favorites = 0
            fav_match = re.search(r'(\d+(?:\.\d+)*)\s*(?:x|keer)\s*bewaard', page_text, re.IGNORECASE)
            if fav_match:
                favorites = int(fav_match.group(1).replace('.', ''))

            # 3. Заголовок
            title = "Без названия"
            try:
                title_el = page.locator("h1").first
                if await title_el.count() > 0:
                    title = (await title_el.inner_text()).strip()
            except: pass

            # 4. Дата
            posted_date = ""
            try:
                date_title_el = page.locator(".Report-module-stat[title]").first
                if await date_title_el.count() > 0:
                    title_date = await date_title_el.get_attribute("title")
                    if title_date: posted_date = title_date
            except: pass

            # 5. Цена
            price_cents = 0
            try:
                price_el = page.locator("div.ListingHeader-module-price, div.ListingHeader-price, span.price").first
                if await price_el.count() > 0:
                    raw_price = (await price_el.inner_text()).strip()
                    # Переводим "€ 1.500,00" в 150000 центов
                    p_text = raw_price.replace('€', '').replace('.', '').replace(',', '.').strip()
                    m = re.search(r'(\d+(?:\.\d+)?)', p_text)
                    if m: price_cents = int(float(m.group(1)) * 100)
            except: pass

            item_id = url.split('/')[-1]

            return {
                "id": item_id,
                "title": title,
                "publication_date": posted_date,
                "parsing_date": datetime.now(timezone.utc).isoformat(),
                "category": "Custom",
                "price_cents": price_cents,
                "views_count": views,
                "favorites_count": favorites,
                "url": url,
                "last_seen": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"   ❌ Ошибка: {url} | {e}")
                return None
            await asyncio.sleep(2)

async def process_batch(batch_urls):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True) # ВАЖНО
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
        
        tasks = []
        for _ in range(len(batch_urls)):
            tasks.append(await context.new_page())

        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *[get_listing_details(tasks[i], batch_urls[i]) for i in range(len(batch_urls))],
                    return_exceptions=True
                ),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            results = []

        await browser.close()
        return [res for res in results if isinstance(res, dict)]

# --- СБОР ССЫЛОК И ГЛАВНАЯ ЛОГИКА ---
async def parse_marktplaats(raw_url, max_pages):
    all_links = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
        page = await context.new_page()

        if '#' in raw_url:
            path_with_query, fragment = raw_url.split('#', 1)
            fragment = '#' + fragment
        else:
            path_with_query, fragment = raw_url, ''
        
        if '?' in path_with_query:
            path, query = path_with_query.split('?', 1)
            query = '?' + query
        else:
            path, query = path_with_query, ''
        
        path = re.sub(r'/p/\d+', '', path)
        query = re.sub(r'&?page=\d+', '', query)

        for page_num in range(1, max_pages + 1):
            if page_num == 1:
                url = f"{path}{query}{fragment}"
            else:
                if '/l/' in path:
                    url = f"{path}p/{page_num}/{query}{fragment}"
                else:
                    url = f"{path}{query}&page={page_num}{fragment}" if query else f"{path}?page={page_num}{fragment}"
            
            logger.info(f"🔍 Загружаю страницу {page_num}...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_selector("a[href*='/v/'], a[href*='/a/']", timeout=10000)
                await page.wait_for_timeout(2000)
                
                links = page.locator("a[href*='/v/'], a[href*='/a/']")
                link_count = await links.count()
                
                for i in range(link_count):
                    href = await links.nth(i).get_attribute("href")
                    if href:
                        if not href.startswith('http'):
                            href = 'https://www.marktplaats.nl' + href
                        clean_href = href.split('?')[0]
                        if clean_href not in all_links:
                            all_links.append(clean_href)
            except Exception as e:
                logger.warning(f"⚠️ Ошибка загрузки страницы {page_num}: {e}")

        await browser.close()

    if not all_links:
        return []

    logger.info(f"✅ Найдено {len(all_links)} объявлений. Парсю детали...")
    
    batch_size = 5
    found_items = []
    
    for i in range(0, len(all_links), batch_size):
        batch = all_links[i:i + batch_size]
        logger.info(f"   📦 Обработка батча #{i//batch_size + 1} ({len(batch)} шт.)...")
        
        batch_results = await process_batch(batch)
        found_items.extend(batch_results)
        
        await asyncio.sleep(random.uniform(1.5, 4.0))

    return found_items

# --- ЗАПИСЬ В БАЗУ И УВЕДОМЛЕНИЯ ---
def process_and_save(parsed_items):
    if not parsed_items: return
    ids_to_check = [item["id"] for item in parsed_items]
    
    existing_data = {}
    try:
        response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
        existing_data = {row["id"]: row["price_cents"] for row in response.data}
    except Exception as e: 
        logger.error(f"Ошибка чтения из Supabase: {e}")
        return

    new_listings, updated_listings = [], []
    
    for item in parsed_items:
        item_id = item["id"]
        current_price = item["price_cents"]
        
        if item_id not in existing_data:
            new_listings.append(item)
            price_str = f"{current_price / 100:.2f} €" if current_price else "См. описание"
            msg = (f"🆕 <b>Новое!</b>\n📦 <b>{item['title']}</b>\n"
                   f"💰 {price_str}\n"
                   f"👁 {item['views_count']} | ❤️ {item['favorites_count']}\n"
                   f"🔗 <a href='{item['url']}'>Открыть</a>")
            send_tg(msg)
        else:
            updated_listings.append(item)
            old_price = existing_data[item_id]
            if current_price != old_price:
                old_str = f"{old_price / 100:.2f} €" if old_price else "?"
                new_str = f"{current_price / 100:.2f} €" if current_price else "?"
                send_tg(f"🔄 <b>Цена изменилась!</b>\n📦 <b>{item['title']}</b>\nБыло: <s>{old_str}</s>\nСтало: <b>{new_str}</b>\n🔗 <a href='{item['url']}'>Открыть</a>")

    try:
        if new_listings:
            supabase.table("marktplaats_listings").insert(new_listings).execute()
            logger.info(f"Добавлено новых: {len(new_listings)}")
            
        if updated_listings:
            for item in updated_listings:
                supabase.table("marktplaats_listings").update({
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "price_cents": item["price_cents"],
                    "views_count": item["views_count"],
                    "favorites_count": item["favorites_count"]
                }).eq("id", item["id"]).execute()
            logger.info(f"Обновлено: {len(updated_listings)}")
    except Exception as e:
        logger.error(f"Ошибка Supabase: {e}")

def main():
    logger.info("Запуск...")
    if not TG_TOKEN or not TG_CHAT_ID:
        return

    updates = get_updates(offset=0)
    last_update_id = updates[-1]["update_id"] if updates else 0

    send_tg("1️⃣ Скинь ссылку на категорию или поиск\n(например: <code>https://www.marktplaats.nl/l/audio-tv-en-foto/#PriceCentsFrom:12000|PriceCentsTo:95000</code>)")
    raw_url, last_update_id = wait_for_text(last_update_id, 10)
    if not raw_url:
        send_tg("❌ Ссылка не получена. Отмена.")
        return

    send_tg("2️⃣ Сколько страниц спарсить? Напиши число.")
    pages_str, last_update_id = wait_for_text(last_update_id, 10)
    if not pages_str:
        send_tg("❌ Количество не получено. Отмена.")
        return

    try: pages = int(pages_str.strip())
    except ValueError: pages = 1

    send_tg(f"✅ Поехали! Запускаю невидимый браузер для парсинга {pages} страниц.")
    
    try:
        results = asyncio.run(parse_marktplaats(raw_url, pages))
        if results:
            process_and_save(results)
            send_tg(f"🏁 Парсинг завершен! Собрано и записано {len(results)} объявлений (без магазинов).")
        else:
            send_tg("⚠️ Парсинг завершен, но ничего не найдено.")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        send_tg(f"💥 Ошибка при выполнении: {e}")

if __name__ == "__main__":
    main()
