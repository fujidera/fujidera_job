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

def wait_for_text(last_id, timeout_minutes=15):
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

# --- ПАРСИНГ ДЕТАЛЕЙ ---
async def get_listing_details(page, item_data, retries=2):
    url = item_data["url"]
    cat_name = item_data["category"]
    
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)
            try: await page.wait_for_selector("h1", timeout=5000)
            except: return None

            await page.wait_for_timeout(1000)
            page_text = await page.inner_text("body")

            if "Checking your browser" in page_text or "We hebben overal gezocht" in page_text or "Cloudflare" in page_text:
                return None

            if "Website" in page_text or "Bezoek website" in page_text:
                return None 

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

            if title == "Без названия" or not title: return None

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
                "parsing_date": datetime.now(timezone.utc).isoformat(), "category": cat_name,
                "price_cents": price_cents, "views_count": views, "favorites_count": favorites,
                "url": url, "last_seen": datetime.now(timezone.utc).isoformat()
            }
        except Exception:
            if attempt == retries - 1: return None
            await asyncio.sleep(1)

async def process_batch(context, batch_data):
    tasks = [await context.new_page() for _ in range(len(batch_data))]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[get_listing_details(tasks[i], batch_data[i]) for i in range(len(batch_data))], return_exceptions=True),
            timeout=60.0
        )
    except asyncio.TimeoutError:
        results = []
    finally:
        for t in tasks: await t.close()
    return [r for r in results if isinstance(r, dict)]

# --- СБОР ССЫЛОК СО СТРАНИЦ ---
async def get_links_from_page(context, url):
    page = await context.new_page()
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
    finally:
        await page.close()

# --- ПОСТРОЕНИЕ URL С ПАГИНАЦИЕЙ ---
def build_page_url(base_url, page_num):
    if page_num == 1:
        return base_url
        
    if '#' in base_url:
        path, fragment = base_url.split('#', 1)
        fragment = '#' + fragment
    else:
        path, fragment = base_url, ''
        
    path = re.sub(r'/p/\d+/?', '', path)
    path = path.rstrip('/')
    
    if '/l/' in path:
        return f"{path}/p/{page_num}/{fragment}"
    elif '/q/' in path:
        return f"{path}/p/{page_num}/{fragment}"
    
    return base_url

# --- ЗАПИСЬ В БАЗУ ---
def process_and_save(parsed_items):
    if not parsed_items: return 0
    
    chunk_size = 50
    new_count = 0
    
    for i in range(0, len(parsed_items), chunk_size):
        chunk = parsed_items[i:i + chunk_size]
        ids_to_check = [item["id"] for item in chunk]
        
        existing_data = {}
        try:
            response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
            existing_data = {row["id"]: row["price_cents"] for row in response.data}
        except Exception as e: 
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
        except Exception as e:
            logger.error(f"Ошибка записи в Supabase: {e}")
            
    return new_count

# --- ГЛАВНЫЙ ПРОЦЕСС ---
async def run_playwright(links_list, pages_count):
    total_saved = 0
    batch_size = 5
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={'width': 1920, 'height': 1080}
        )
        
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['nl-NL', 'nl'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        for idx, base_url in enumerate(links_list, 1):
            logger.info(f"--- Обработка ссылки {idx}/{len(links_list)}: {base_url} ---")
            
            cat_slug = "Other"
            if "/l/" in base_url:
                match = re.search(r'/l/([^/]+)/', base_url)
                if match: cat_slug = match.group(1)
                
            send_tg(f"📂 Ссылка {idx}/{len(links_list)}: <b>{cat_slug}</b>")

            all_links = []
            for page_num in range(1, pages_count + 1):
                url = build_page_url(base_url, page_num)
                links = await get_links_from_page(context, url)
                all_links.extend(links)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            
            all_links = list(set(all_links))
            if not all_links:
                logger.info(f"Ссылок не найдено для {base_url}")
                continue
                
            send_tg(f"🔍 Найдено {len(all_links)} товаров в {cat_slug}. Начинаю парсинг...")
            
            for i in range(0, len(all_links), batch_size):
                batch_urls = all_links[i:i + batch_size]
                batch_data = [{"url": u, "category": cat_slug} for u in batch_urls]
                
                batch_results = await process_batch(context, batch_data)
                
                if batch_results:
                    saved = process_and_save(batch_results)
                    total_saved += saved
                
                await asyncio.sleep(random.uniform(1.0, 2.0))

            send_tg(f"✅ Категория <b>{cat_slug}</b> готова. Обработано {len(all_links)} товаров.")

        await browser.close()
    return total_saved

def main():
    logger.info("Запуск...")
    if not TG_TOKEN or not TG_CHAT_ID: return

    updates = get_updates(offset=0)
    last_update_id = updates[-1]["update_id"] if updates else 0

    send_tg("1️⃣ Скинь ссылки на категории, которые нужно спарсить (каждую с новой строки).")
    raw_text, last_update_id = wait_for_text(last_update_id, 15)
    
    if not raw_text:
        send_tg("❌ Отмена.")
        return

    links_list = [line.strip() for line in raw_text.splitlines() if line.strip().startswith("http")]
    
    if not links_list:
        send_tg("❌ Не найдено валидных ссылок. Отмена.")
        return

    send_tg(f"✅ Принято {len(links_list)} ссылок.")
    
    send_tg("2️⃣ Сколько страниц спарсить <b>с каждой ссылки</b>? Напиши число.")
    pages_str, last_update_id = wait_for_text(last_update_id, 10)
    if not pages_str: return
    try: pages = int(pages_str.strip())
    except ValueError: pages = 1

    send_tg(f"🚀 Поехали! Парсим по {pages} страниц с каждой из {len(links_list)} ссылок.")

    try:
        total_saved = asyncio.run(run_playwright(links_list, pages))
        if total_saved > 0:
            send_tg(f"🏁 Парсинг завершен! Успешно обработано и записано {total_saved} объявлений.")
        else:
            send_tg("⚠️ Ничего не найдено.")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        send_tg(f"💥 Ошибка: {e}")

if __name__ == "__main__":
    main()
