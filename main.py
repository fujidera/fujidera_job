import asyncio
import re
import os
import random
import time
import logging
from datetime import datetime, timezone
from collections import defaultdict

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

DEFAULT_LINKS = [
    "https://www.marktplaats.nl/l/antiek-en-kunst/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/audio-tv-en-foto/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/auto-s/#PriceCentsFrom:50000|PriceCentsTo:100000",
    "https://www.marktplaats.nl/l/auto-onderdelen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/auto-diversen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/boeken/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/caravans-en-kamperen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/cd-s-en-dvd-s/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/computers-en-software/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/contacten-en-berichten/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/diensten-en-vakmensen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/dieren-en-toebehoren/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/doe-het-zelf-en-verbouw/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/fietsen-en-brommers/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/hobby-en-vrije-tijd/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/huis-en-inrichting/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/huizen-en-kamers/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/kinderen-en-baby-s/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/kleding-dames/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/kleding-heren/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/motoren/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/muziek-en-instrumenten/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/sieraden-tassen-en-uiterlijk/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/spelcomputers-en-games/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/sport-en-fitness/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/telecommunicatie/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/tickets-en-kaartjes/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/tuin-en-terras/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/watersport-en-boten/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/witgoed-en-apparatuur/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/zakelijke-goederen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/diversen/#PriceCentsFrom:12000|PriceCentsTo:95000"
]
DEFAULT_PAGES = 3

# --- TELEGRAM ЛОГИКА ---
def get_updates(offset):
    try:
        r = tg_session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json().get("result", [])
    except Exception: pass
    return []

def wait_for_response(last_id, timeout_minutes=5):
    start = time.time()
    while time.time() - start < timeout_minutes * 60:
        updates = get_updates(offset=last_id + 1)
        for up in updates:
            last_id = max(last_id, up["update_id"])
            if "message" in up and "text" in up["message"]:
                text = up["message"]["text"].strip()
                if text.lower().startswith("/stats"):
                    send_stats()
                    return "__stats__", last_id
                return text, last_id
            if "callback_query" in up:
                data = up["callback_query"]["data"]
                cb_id = up["callback_query"]["id"]
                tg_session.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id})
                if data == "cmd_stats":
                    send_stats()
                    return "__stats__", last_id
                return data, last_id
        time.sleep(2)
    return None, last_id

def send_tg(text, reply_markup=None):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                payload["text"] = text[i:i+4000]
                tg_session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=10)
                time.sleep(1)
        else:
            tg_session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception: pass

# --- СТАТИСТИКА (/stats) ---
def _format_stat_item(i, item):
    price_str = f"{item['price_cents'] / 100:.2f} €" if item.get('price_cents') else "—"
    title = (item.get('title') or "Без названия")[:60]
    url = item.get('url') or ""
    cat = item.get('category') or "—"
    return (
        f"  {i}. <a href='{url}'>{title}</a>\n"
        f"     👁 {item.get('views_count', 0)} | ❤️ {item.get('favorites_count', 0)} "
        f"| 💰 {price_str} | 📂 {cat}\n"
    )

def send_stats():
    try:
        total_res = supabase.table("marktplaats_listings").select("id", count="exact").limit(1).execute()
        total = total_res.count or 0

        # Топ-5 за сегодня
        today_iso = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        try:
            today_res = supabase.table("marktplaats_listings").select(
                "title, url, views_count, favorites_count, price_cents, category"
            ).gte("parsing_date", today_iso).order("views_count", desc=True).limit(5).execute()
            today_items = today_res.data or []
        except Exception:
            today_items = []

        # Топ-5 за всё время (всегда)
        top_res = supabase.table("marktplaats_listings").select(
            "title, url, views_count, favorites_count, price_cents, category"
        ).order("views_count", desc=True).limit(5).execute()
        top_items = top_res.data or []

        msg = "📊 <b>Статистика базы данных</b>\n\n"
        msg += f"🗄 Всего объявлений: <b>{total}</b>\n\n"

        if today_items:
            msg += "🔥 <b>Топ-5 просматриваемых за сегодня:</b>\n"
            for i, item in enumerate(today_items, 1):
                msg += _format_stat_item(i, item)
            msg += "\n"

        msg += "👁 <b>Топ-5 за всё время:</b>\n"
        for i, item in enumerate(top_items, 1):
            msg += _format_stat_item(i, item)

        send_tg(msg)
    except Exception as e:
        send_tg(f"⚠️ Ошибка при получении статистики: {e}")

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

# --- ПОСТРОЕНИЕ URL ---
def build_page_url(base_url, page_num):
    if page_num == 1: return base_url
    if '#' in base_url:
        path, fragment = base_url.split('#', 1)
        fragment = '#' + fragment
    else:
        path, fragment = base_url, ''
    path = re.sub(r'/p/\d+/?', '', path).rstrip('/')
    if '/l/' in path: return f"{path}/p/{page_num}/{fragment}"
    elif '/q/' in path: return f"{path}/p/{page_num}/{fragment}"
    return base_url

# --- ЗАПИСЬ В БАЗУ (UPSERT) ---
def process_and_save(parsed_items):
    if not parsed_items: return []
    chunk_size = 50
    new_listings_found = []
    for i in range(0, len(parsed_items), chunk_size):
        chunk = parsed_items[i:i + chunk_size]
        ids_to_check = [item["id"] for item in chunk]
        existing_data = {}
        try:
            response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
            existing_data = {row["id"]: row["price_cents"] for row in response.data}
        except Exception as e: 
            continue

        snapshots_to_insert = []
        for item in chunk:
            item_id = item["id"]
            current_price = item["price_cents"]
            if item_id in existing_data:
                if existing_data[item_id] != current_price:
                    snapshots_to_insert.append({"listing_id": item_id, "price_cents": current_price})
            else:
                new_listings_found.append(item)
                snapshots_to_insert.append({"listing_id": item_id, "price_cents": current_price})

        try:
            supabase.table("marktplaats_listings").upsert(chunk, on_conflict="id").execute()
            if snapshots_to_insert:
                supabase.table("marktplaats_snapshots").insert(snapshots_to_insert).execute()
        except Exception as e:
            pass
    return new_listings_found

# --- ГЛАВНЫЙ ПРОЦЕСС ---
async def run_playwright(links_list, pages_count):
    all_new_listings = []
    batch_size = 5
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS), viewport={'width': 1920, 'height': 1080})
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['nl-NL', 'nl'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        for idx, base_url in enumerate(links_list, 1):
            cat_slug = "Other"
            if "/l/" in base_url:
                match = re.search(r'/l/([^/]+)/', base_url)
                if match: cat_slug = match.group(1)
            send_tg(f"📂 Начинаю: <b>{cat_slug}</b> ({idx}/{len(links_list)})")

            all_links = []
            for page_num in range(1, pages_count + 1):
                url = build_page_url(base_url, page_num)
                links = await get_links_from_page(context, url)
                all_links.extend(links)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            
            all_links = list(set(all_links))
            if not all_links: continue
                
            for i in range(0, len(all_links), batch_size):
                batch_urls = all_links[i:i + batch_size]
                batch_data = [{"url": u, "category": cat_slug} for u in batch_urls]
                batch_results = await process_batch(context, batch_data)
                if batch_results:
                    saved = process_and_save(batch_results)
                    all_new_listings.extend(saved)
                await asyncio.sleep(random.uniform(1.0, 2.0))
        await browser.close()
    return all_new_listings

def get_total_db_count():
    try:
        res = supabase.table("marktplaats_listings").select("id", count="exact").limit(1).execute()
        return res.count if res.count is not None else 0
    except: return 0

def main():
    if not TG_TOKEN or not TG_CHAT_ID: return
    updates = get_updates(offset=0)
    last_update_id = updates[-1]["update_id"] if updates else 0

    # Если последнее входящее сообщение между запусками — /stats, отвечаем и не парсим
    if updates:
        last_up = updates[-1]
        if "message" in last_up and "text" in last_up["message"]:
            if last_up["message"]["text"].strip().lower().startswith("/stats"):
                send_stats()
                return

    buttons = {
        "inline_keyboard": [
            [{"text": "🚀 1 страница", "callback_data": "pages_1"},
             {"text": "🔥 3 страницы", "callback_data": "pages_3"}],
            [{"text": "📦 5 страниц", "callback_data": "pages_5"}],
            [{"text": "📊 Статистика", "callback_data": "cmd_stats"}],
        ]
    }
    send_tg("1️⃣ Скинь ссылки (каждую с новой строки) ИЛИ выбери количество страниц кнопками ниже (для дефолтного списка):", reply_markup=buttons)
    
    raw_text, last_update_id = wait_for_response(last_update_id, 5)

    # Пользователь выбрал «Статистика» — отчёт отправлен, парсер не запускаем
    if raw_text == "__stats__":
        return
    
    if not raw_text or not raw_text.strip():
        send_tg("⏰ Время вышло. Запускаю дефолтный список: 32 категории по 3 страницы.")
        links_list, pages = DEFAULT_LINKS, DEFAULT_PAGES
    else:
        if raw_text.startswith("pages_"):
            pages = int(raw_text.split("_")[1])
            links_list = DEFAULT_LINKS
            send_tg(f"✅ Выбрано: {pages} стр. Запускаю дефолтный список из 32 категорий.")
        else:
            links_list = [line.strip() for line in raw_text.splitlines() if line.strip().startswith("http")]
            if not links_list:
                links_list, pages = DEFAULT_LINKS, DEFAULT_PAGES
                send_tg("⚠️ Не найдено ссылок. Запускаю дефолтный список.")
            else:
                send_tg(f"✅ Принято {len(links_list)} ссылок. Сколько страниц спарсить? (Если не ответишь за 5 мин, будет 3).")
                pages_str, last_update_id = wait_for_response(last_update_id, 5)

                if pages_str == "__stats__":
                    return
                if not pages_str:
                    pages = DEFAULT_PAGES
                    send_tg("⏰ Время вышло. Ставлю 3 страницы по умолчанию.")
                else:
                    try: pages = int(pages_str.strip())
                    except ValueError: 
                        pages = DEFAULT_PAGES
                        send_tg("⚠️ Не распознал число. Ставлю 3 страницы по умолчанию.")

    send_tg(f"🚀 Поехали! Парсим по {pages} страниц с каждой из {len(links_list)} ссылок.")
    try:
        new_items = asyncio.run(run_playwright(links_list, pages))
        if new_items:
            grouped = defaultdict(list)
            for item in new_items:
                grouped[item['category']].append(item)
                
            report = "🏆 <b>ТОП-5 НОВЫХ ОБЪЯВЛЕНИЙ ПО КАТЕГОРИЯМ</b>\n\n"
            for cat, items in grouped.items():
                items.sort(key=lambda x: x.get("views_count", 0) + (x.get("favorites_count", 0) * 20), reverse=True)
                top_5 = items[:5]
                report += f"📂 <b>{cat}</b> (найдено {len(items)})\n"
                for idx, item in enumerate(top_5, 1):
                    price_str = f"{item['price_cents'] / 100:.2f} €" if item['price_cents'] else "См. описание"
                    report += f"  {idx}. <a href='{item['url']}'>{item['title'][:50]}</a>\n"
                    report += f"     👁 {item['views_count']} | ❤️ {item['favorites_count']} | 💰 {price_str}\n"
                report += "\n"
                
            total_db = get_total_db_count()
            report += f"📊 <b>Всего новых за запуск:</b> {len(new_items)}\n"
            report += f"🗄 <b>Всего в базе:</b> {total_db} объявлений."
            send_tg(report)
        else:
            send_tg("🏁 Парсинг завершен! Новых объявлений не найдено.")
    except Exception as e:
        send_tg(f"💥 Ошибка: {e}")

if __name__ == "__main__":
    main()
