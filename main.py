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

# --- ПАРСИНГ ДЕТАЛЕЙ КАРТОЧКИ (PLAYWRIGHT) ---
async def get_listing_details(page, url, retries=2):
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            try: await asyncio.wait_for(page.wait_for_selector("body", timeout=5000), timeout=6.0)
            except: return None

            await page.wait_for_timeout(500)
            page_text = await page.inner_text("body")

            if "Website" in page_text or "Bezoek website" in page_text:
                return None # Пропускаем магазины

            views = 0
            v_match = re.search(r'(\d+(?:\.\d+)*)\s*(?:x|keer)\s*bekeken', page_text, re.IGNORECASE)
            if v_match: views = int(v_match.group(1).replace('.', ''))

            favorites = 0
            f_match = re.search(r'(\d+(?:\.\d+)*)\s*(?:x|keer)\s*bewaard', page_text, re.IGNORECASE)
            if f_match: favorites = int(f_match.group(1).replace('.', ''))

            title = "Без названия"
            try:
                title_el = page.locator("h1").first
                if await title_el.count() > 0: title = (await title_el.inner_text()).strip()
            except: pass

            posted_date = ""
            try:
                date_title_el = page.locator(".Report-module-stat[title]").first
                if await date_title_el.count() > 0:
                    title_date = await date_title_el.get_attribute("title")
                    if title_date: posted_date = title_date
            except: pass

            price_cents = 0
            try:
                price_el = page.locator("div.ListingHeader-module-price, div.ListingHeader-price, span.price").first
                if await price_el.count() > 0:
                    raw_price = (await price_el.inner_text()).strip()
                    p_text = raw_price.replace('€', '').replace('.', '').replace(',', '.').strip()
                    m = re.search(r'(\d+(?:\.\d+)?)', p_text)
                    if m: price_cents = int(float(m.group(1)) * 100)
            except: pass

            item_id = url.split('/')[-1]
            return {
                "id": item_id, "title": title, "publication_date": posted_date,
                "parsing_date": datetime.now(timezone.utc).isoformat(), "category": "Custom",
                "price_cents": price_cents, "views_count": views, "favorites_count": favorites,
                "url": url, "last_seen": datetime.now(timezone.utc).isoformat()
            }
        except Exception:
            if attempt == retries - 1: return None
            await asyncio.sleep(1)

async def process_batch(batch_urls):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
        tasks = [await context.new_page() for _ in range(len(batch_urls))]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[get_listing_details(tasks[i], batch_urls[i]) for i in range(len(batch_urls))], return_exceptions=True),
                timeout=45.0
            )
        except asyncio.TimeoutError:
            results = []
        await browser.close()
        return [r for r in results if isinstance(r, dict)]

# --- СБОР ССЫЛОК СО СТРАНИЦ ---
async def get_links_from_page(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_selector("a[href*='/v/'], a[href*='/a/']", timeout=10000)
        await page.wait_for_timeout(2000)
        
        links = await page.locator("a[href*='/v/'], a[href*='/a/']").all()
        res = []
        for l in links:
            href = await l.get_attribute("href")
            if href:
                clean = 'https://www.marktplaats.nl' + href.split('?')[0] if href.startswith('/') else href.split('?')[0]
                res.append(clean)
        return list(set(res))
    except Exception:
        return []

# --- ЗАПИСЬ В БАЗУ (БЕЗОПАСНЫМИ ЧАНКАМИ ПО 50 ШТУК) ---
def process_and_save(parsed_items):
    if not parsed_items: return
    
    chunk_size = 50
    new_count = 0
    updated_count = 0
    
    for i in range(0, len(parsed_items), chunk_size):
        chunk = parsed_items[i:i + chunk_size]
        ids_to_check = [item["id"] for item in chunk]
        
        existing_data = {}
        try:
            response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
            existing_data = {row["id"]: row["price_cents"] for row in response.data}
        except Exception as e: 
            logger.error(f"Ошибка чтения из Supabase: {e}")
            continue

        new_listings, updated_listings = [], []
        
        for item in chunk:
            item_id = item["id"]
            current_price = item["price_cents"]
            
            if item_id not in existing_data:
                new_listings.append(item)
                price_str = f"{current_price / 100:.2f} €" if current_price else "См. описание"
                send_tg(f"🆕 <b>Новое!</b>\n📦 <b>{item['title']}</b>\n💰 {price_str}\n👁 {item['views_count']} | ❤️ {item['favorites_count']}\n🔗 <a href='{item['url']}'>Открыть</a>")
            else:
                updated_listings.append(item)
                if existing_data[item_id] != current_price:
                    old_str = f"{existing_data[item_id] / 100:.2f} €" if existing_data[item_id] else "?"
                    new_str = f"{current_price / 100:.2f} €" if current_price else "?"
                    send_tg(f"🔄 <b>Цена изменилась!</b>\n📦 <b>{item['title']}</b>\nБыло: <s>{old_str}</s>\nСтало: <b>{new_str}</b>\n🔗 <a href='{item['url']}'>Открыть</a>")

        try:
            if new_listings:
                supabase.table("marktplaats_listings").insert(new_listings).execute()
                new_count += len(new_listings)
            if updated_listings:
                for item in updated_listings:
                    supabase.table("marktplaats_listings").update({
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                        "price_cents": item["price_cents"],
                        "views_count": item["views_count"],
                        "favorites_count": item["favorites_count"]
                    }).eq("id", item["id"]).execute()
                updated_count += len(updated_listings)
        except Exception as e:
            logger.error(f"Ошибка записи в Supabase: {e}")
            
    logger.info(f"Сохранено в базу: {new_count} новых, {updated_count} обновленных.")

# --- ГЛАВНЫЙ ПРОЦЕСС ---
async def run_playwright(raw_url, pages_count, is_auto):
    all_links = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
        page = await context.new_page()

        if is_auto:
            logger.info("Режим АВТО: собираем список категорий с главной страницы...")
            send_tg("⏳ Собираю список всех категорий с главной страницы...")
            await page.goto("https://www.marktplaats.nl/", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
            
            try:
                categories = await page.evaluate("() => window.__CONFIG__.categoryLinks")
            except:
                categories = []
                
            if not categories: return 0

            for cat in categories:
                cat_id = cat["id"]
                for page_num in range(pages_count):
                    offset = page_num * 30
                    url = f"https://www.marktplaats.nl/lrp/api/search?l1CategoryId={cat_id}&limit=30&offset={offset}"
                    r = tg_session.get(url, timeout=10)
                    if r.status_code == 200:
                        items = r.json().get("listings", [])
                        if not items: break
                        for item in items:
                            item_id = item.get("itemId")
                            if item_id:
                                t = "a" if item_id.startswith("a") else "v"
                                all_links.append(f"https://www.marktplaats.nl/{t}/{item_id}")
                    await asyncio.sleep(0.5)
        else:
            logger.info(f"Режим ССЫЛКИ: {raw_url}")
            if '#' in raw_url:
                path_query, fragment = raw_url.split('#', 1)
                fragment = '#' + fragment
            else:
                path_query, fragment = raw_url, ''
                
            path_query = re.sub(r'/p/\d+', '', path_query)

            for page_num in range(1, pages_count + 1):
                if '/l/' in path_query:
                    url = f"{path_query}p/{page_num}/{fragment}" if page_num > 1 else f"{path_query}{fragment}"
                else:
                    url = f"{path_query}&page={page_num}{fragment}" if page_num > 1 else f"{path_query}{fragment}"
                
                logger.info(f"Загружаю страницу {page_num}...")
                links = await get_links_from_page(page, url)
                all_links.extend(links)

        await browser.close()

    all_links = list(set(all_links))
    logger.info(f"Всего найдено {len(all_links)} ссылок. Начинаем парсинг просмотров...")
    send_tg(f"🔍 Найдено {len(all_links)} товаров. Начинаю сбор данных...")

    total_saved = 0
    batch_size = 5
    
    # ПАРСИМ И СОХРАНЯЕМ ПО ХОДУ ДЕЛА
    for i in range(0, len(all_links), batch_size):
        batch = all_links[i:i + batch_size]
        batch_results = await process_batch(batch)
        
        if batch_results:
            process_and_save(batch_results) # Сохраняем сразу после каждого батча!
            total_saved += len(batch_results)
        
        if (i + len(batch)) % 100 == 0:
            send_tg(f"⏳ Обработано {i + len(batch)} из {len(all_links)}...")
            
        logger.info(f"Обработано {i + len(batch)}/{len(all_links)}")
        await asyncio.sleep(random.uniform(1.0, 2.0))

    return total_saved

def main():
    logger.info("Запуск...")
    if not TG_TOKEN or not TG_CHAT_ID: return

    updates = get_updates(offset=0)
    last_update_id = updates[-1]["update_id"] if updates else 0

    send_tg("1️⃣ Скинь ссылку (например: <code>https://www.marktplaats.nl/l/computers-en-software/</code>)\nИЛИ напиши <b>ALL</b>, чтобы спарсить все категории автоматом.")
    raw_url, last_update_id = wait_for_text(last_update_id, 10)
    
    if not raw_url:
        send_tg("❌ Отмена.")
        return

    is_auto = raw_url.strip().upper() == "ALL" or raw_url.strip().upper() == "ВСЕ"

    if is_auto:
        send_tg("2️⃣ Сколько страниц спарсить <b>с каждой категории</b>? (Напиши число, например 3).")
    else:
        send_tg("2️⃣ Сколько страниц спарсить? Напиши число.")
        
    pages_str, last_update_id = wait_for_text(last_update_id, 10)
    if not pages_str: return
    try: pages = int(pages_str.strip())
    except ValueError: pages = 1

    if is_auto:
        send_tg(f"✅ Режим АВТО включен. Парсим по {pages} страниц с каждой категории.")
    else:
        send_tg(f"✅ Поехали! Парсим {pages} страниц.")

    try:
        total_saved = asyncio.run(run_playwright(raw_url, pages, is_auto))
        if total_saved > 0:
            send_tg(f"🏁 Парсинг завершен! Успешно обработано и записано {total_saved} объявлений.")
        else:
            send_tg("⚠️ Ничего не найдено.")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        send_tg(f"💥 Ошибка: {e}")

if __name__ == "__main__":
    main()
