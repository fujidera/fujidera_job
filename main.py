import asyncio
import re
import os
import random
import time
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
]

DEFAULT_LINKS = [
    "https://www.marktplaats.nl/l/antiek-en-kunst/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/audio-tv-en-foto/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/auto-onderdelen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/auto-diversen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/boeken/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/caravans-en-kamperen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/cd-s-en-dvd-s/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/computers-en-software/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/contacten-en-berichten/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/dieren-en-toebehoren/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/doe-het-zelf-en-verbouw/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/fietsen-en-brommers/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/hobby-en-vrije-tijd/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/huis-en-inrichting/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/kinderen-en-baby-s/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/kleding-dames/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/kleding-heren/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/muziek-en-instrumenten/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/sieraden-tassen-en-uiterlijk/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/spelcomputers-en-games/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/sport-en-fitness/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/telecommunicatie/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/tickets-en-kaartjes/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/tuin-en-terras/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/witgoed-en-apparatuur/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/zakelijke-goederen/#PriceCentsFrom:12000|PriceCentsTo:95000",
    "https://www.marktplaats.nl/l/diversen/#PriceCentsFrom:12000|PriceCentsTo:95000"
]
DEFAULT_PAGES = 3

NL_MONTHS = {'jan': 1, 'feb': 2, 'mrt': 3, 'apr': 4, 'mei': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12}

def parse_dutch_date(date_str):
    if not date_str or not isinstance(date_str, str) or date_str.strip().upper() == "EMPTY": return None
    date_str = date_str.strip().replace('\xa0', ' ').lower()
    
    # ИСПОЛЬЗУЕМ ВРЕМЯ НИДЕРЛАНДОВ
    nl_tz = ZoneInfo("Europe/Amsterdam")
    now = datetime.now(nl_tz)
    
    if "vandaag" in date_str: return now.strftime('%Y-%m-%d')
    if "gisteren" in date_str: return (now - timedelta(days=1)).strftime('%Y-%m-%d')
    if "eergisteren" in date_str: return (now - timedelta(days=2)).strftime('%Y-%m-%d')

    match = re.search(r'(\d{1,2})\s*([a-zA-Z]{3,4})\.?\s*\'?(\d{2,4})', date_str)
    if not match: return None
    day = int(match.group(1))
    month_str = match.group(2)[:3].lower()
    year_str = match.group(3)
    month = NL_MONTHS.get(month_str)
    if not month: return None
    year = int(year_str)
    if year < 100: year += 2000
    try: 
        return datetime(year, month, day, tzinfo=nl_tz).strftime('%Y-%m-%d')
    except ValueError: 
        return None

def send_tg(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                payload["text"] = text[i:i + 4000]
                tg_session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=10)
                time.sleep(1)
        else:
            tg_session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception: pass

async def get_listing_details(page, item_data, retries=2):
    url = item_data["url"]
    cat_name = item_data["category"]
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)
            try: await page.wait_for_selector("h1", timeout=5000)
            except Exception: return None
            await page.wait_for_timeout(1000)
            page_text = await page.inner_text("body")
            page_html = await page.content()

            # Защита от капчи и удаленных объявлений
            if ("Checking your browser" in page_text or "We hebben overal gezocht" in page_text or "Cloudflare" in page_text): 
                return None
            
            # --- ЖЕСТКИЙ ФИЛЬТР КОММЕРЧЕСКИХ ПРОДАВЦОВ ---
            if '"adType":"Admarkt"' in page_html or '"isAdmarkt":true' in page_html: return None
            if '"sellerType":"TRADER"' in page_html or '"ad_seller_type":"TRADER"' in page_html: return None
            if '"sellerWebsiteUrl"' in page_html or "Website" in page_text or "Bezoek website" in page_text: return None
            if "Zakelijke verkoper" in page_text or "Bedrijfsprofiel" in page_text: return None
            # -----------------------------------------------

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
            except Exception: pass
            if title == "Без названия" or not title: return None

            raw_date = ""
            date_match = re.search(r'Sinds\s+(\d{1,2}\s+[a-zA-Z]{3,4}\.?\s*\'?\d{2,4}(?:[,\s]+\d{1,2}:\d{2})?)', page_text, re.IGNORECASE)
            if date_match: raw_date = date_match.group(1)
            else:
                try:
                    date_title_el = page.locator(".Report-module-stat[title]").first
                    if await date_title_el.count() > 0: raw_date = await date_title_el.get_attribute("title")
                except Exception: pass
            posted_date = parse_dutch_date(raw_date)

            price_cents = 0
            try:
                price_el = page.locator("div.ListingHeader-module-price, div.ListingHeader-price, span.price").first
                if await price_el.count() > 0:
                    raw_price = (await price_el.inner_text()).strip()
                    p_text = raw_price.replace('€', '').replace('.', '').replace(',', '.').strip()
                    m = re.search(r'(\d+(?:\.\d+)?)', p_text)
                    if m: price_cents = int(float(m.group(1)) * 100)
            except Exception: pass
            
            # --- СБОР ВСЕХ ФОТО ---
            image_urls = []
            try:
                raw_urls = re.findall(r'(https://(?:images\.marktplaats\.com|admarkt-cdn\.marktplaats\.com)/api/v1/(?:hz-mp-pro-listing|icas-mp-pro-admarkt)/images/[^"\\\s]+)', page_html)
                seen = set()
                for u in raw_urls:
                    clean = u.split('?')[0]
                    if '/profile/' not in clean and clean not in seen:
                        seen.add(clean)
                        image_urls.append(clean)
                
                if not image_urls:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1000)
                    images = await page.locator("img[src*='images.marktplaats.com'], img[src*='admarkt-cdn.marktplaats.com']").all()
                    seen_urls = set()
                    for img in images:
                        src = await img.get_attribute("src")
                        if src:
                            clean_url = src.split('?')[0]
                            if '/profile/' not in clean_url and clean_url not in seen_urls:
                                seen_urls.add(clean_url)
                                image_urls.append(clean_url)
                                
                    for _ in range(10): 
                        next_btn = page.locator("button[aria-label='Volgende afbeelding'], button[aria-label='Next image']").first
                        if await next_btn.count() > 0 and await next_btn.is_visible():
                            await next_btn.click()
                            await page.wait_for_timeout(500)
                            new_images = await page.locator("img[src*='images.marktplaats.com'], img[src*='admarkt-cdn.marktplaats.com']").all()
                            for img in new_images:
                                src = await img.get_attribute("src")
                                if src:
                                    clean_url = src.split('?')[0]
                                    if '/profile/' not in clean_url and clean_url not in seen_urls:
                                        seen_urls.add(clean_url)
                                        image_urls.append(clean_url)
                        else: break
            except Exception as e:
                logger.warning(f"Ошибка при сборе галереи фото: {e}")

            image_url = image_urls[0] if image_urls else ""
            # ----------------------

            # --- ОПИСАНИЕ И ПРОДАВЕЦ ---
            description = ""
            try:
                desc_el = page.locator("div[data-testid='description'], div.Description-module-description, div.Description").first
                if await desc_el.count() > 0:
                    description = (await desc_el.inner_text()).strip()
            except Exception: pass

            seller_name = ""
            try:
                seller_el = page.locator("a[href*='/u/'], .SellerInfoSmall-name a").first
                if await seller_el.count() > 0:
                    seller_name = (await seller_el.inner_text()).strip()
            except Exception: pass
            # ---------------------------

            # --- ДОПОЛНИТЕЛЬНЫЙ ФИЛЬТР B.V. и WWW ---
            if "B.V." in title or "N.V." in title or "B.V." in seller_name or "www." in description.lower():
                return None
            # ----------------------------------------

            # --- ИЗВЛЕЧЕНИЕ ПОДКАТЕГОРИЙ (L1 и L2) ---
            category_l1 = cat_name
            category_l2 = ""
            try:
                parts = url.split('/')
                if 'v' in parts:
                    v_idx = parts.index('v')
                    # Формат URL: .../v/l1-slug/l2-slug/id
                    if len(parts) > v_idx + 2:
                        category_l1 = parts[v_idx + 1]
                        category_l2 = parts[v_idx + 2]
            except Exception:
                pass
            # -----------------------------------------

            item_id = url.split('/')[-1]
            item_dict = {
                "id": item_id, "title": title,
                "parsing_date": datetime.now(timezone.utc).isoformat(),
                "category_l1": category_l1,
                "category_l2": category_l2,
                "price_cents": price_cents,
                "views_count": views, "favorites_count": favorites,
                "url": url, "last_seen": datetime.now(timezone.utc).isoformat(),
                "image_url": image_url,
                "image_urls": image_urls,
                "description": description,
                "seller_name": seller_name
            }
            if posted_date: item_dict["publication_date"] = posted_date
            return item_dict
        except Exception as e:
            logger.error(f"Ошибка парсинга {url}: {e}")
            if attempt == retries - 1: return None
            await asyncio.sleep(1)

async def process_batch(context, batch_data):
    tasks = [await context.new_page() for _ in range(len(batch_data))]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[get_listing_details(tasks[i], batch_data[i]) for i in range(len(batch_data))], return_exceptions=True),
            timeout=60.0,
        )
    except asyncio.TimeoutError: results = []
    finally:
        for t in tasks: await t.close()
    return [r for r in results if isinstance(r, dict)]

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
                clean = ('https://www.marktplaats.nl' + href.split('?')[0] if href.startswith('/') else href.split('?')[0])
                res.append(clean)
        return list(set(res))
    except Exception: return []
    finally: await page.close()

def build_page_url(base_url, page_num):
    if page_num == 1: return base_url
    if '#' in base_url:
        path, fragment = base_url.split('#', 1)
        fragment = '#' + fragment
    else: path, fragment = base_url, ''
    path = re.sub(r'/p/\d+/?', '', path).rstrip('/')
    if '/l/' in path: return f"{path}/p/{page_num}/{fragment}"
    elif '/q/' in path: return f"{path}/p/{page_num}/{fragment}"
    return base_url

def process_and_save(parsed_items):
    if not parsed_items: return []
    chunk_size = 50
    new_listings_found = []
    for i in range(0, len(parsed_items), chunk_size):
        chunk = parsed_items[i:i + chunk_size]
        ids_to_check = [item["id"] for item in chunk]
        existing_data = {}
        try:
            response = supabase.table("marktplaats_listings").select("id, price_cents, publication_date").in_("id", ids_to_check).execute()
            existing_data = {row["id"]: row for row in response.data}
        except Exception: continue

        snapshots_to_insert = []
        for item in chunk:
            item_id = item["id"]
            current_price = item["price_cents"]
            if "publication_date" not in item and item_id in existing_data:
                db_date = existing_data[item_id].get("publication_date")
                if db_date: item["publication_date"] = db_date
            if item_id in existing_data:
                if existing_data[item_id].get("price_cents") != current_price:
                    snapshots_to_insert.append({"listing_id": item_id, "price_cents": current_price})
            else:
                new_listings_found.append(item)
                snapshots_to_insert.append({"listing_id": item_id, "price_cents": current_price})

        try:
            supabase.table("marktplaats_listings").upsert(chunk, on_conflict="id").execute()
            if snapshots_to_insert:
                supabase.table("marktplaats_snapshots").insert(snapshots_to_insert).execute()
        except Exception as e: 
            logger.error(f"Ошибка записи в базу: {e}")
    return new_listings_found

async def run_playwright(links_list, pages_count):
    all_parsed_items = [] 
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
                    new_saved = process_and_save(batch_results)
                    all_new_listings.extend(new_saved)
                    all_parsed_items.extend(batch_results)
                await asyncio.sleep(random.uniform(1.0, 2.0))
        await browser.close()
    return all_parsed_items, all_new_listings

def get_total_db_count():
    try:
        res = supabase.table("marktplaats_listings").select("id", count="exact").limit(1).execute()
        return res.count if res.count is not None else 0
    except Exception: return 0

def main():
    pages = int(os.environ.get("INPUT_PAGES") or DEFAULT_PAGES)
    links_raw = (os.environ.get("INPUT_LINKS") or "").strip()

    if links_raw:
        links_list = [l.strip() for l in links_raw.splitlines() if l.strip().startswith("http")]
        if not links_list: links_list = DEFAULT_LINKS
    else: links_list = DEFAULT_LINKS

    send_tg(f"🚀 Запуск парсера: {len(links_list)} ссылок × {pages} стр.")
    try:
        parsed_items, new_items = asyncio.run(run_playwright(links_list, pages))
        if parsed_items:
            grouped = defaultdict(list)
            for item in parsed_items: 
                grouped[item.get('category_l1', 'Other')].append(item)

            report = "🏆 <b>ТОП-5 ОБЪЯВЛЕНИЙ ПО КАТЕГОРИЯМ</b>\n\n"
            for cat, items in grouped.items():
                items.sort(key=lambda x: x.get("views_count", 0), reverse=True)
                top_5 = items[:5]
                report += f"📂 <b>{cat}</b> (спарсено {len(items)})\n"
                for idx, item in enumerate(top_5, 1):
                    price_str = (f"{item['price_cents'] / 100:.2f} €" if item['price_cents'] else "См. описание")
                    report += f"  {idx}. <a href='{item['url']}'>{item['title'][:50]}</a>\n"
                    report += (f"     👁 {item['views_count']} | ❤️ {item['favorites_count']} | 💰 {price_str}\n")
                report += "\n"

            total_db = get_total_db_count()
            report += f"🆕 <b>Из них новых:</b> {len(new_items)}\n"
            report += f"🗄 <b>Всего в базе:</b> {total_db} объявлений."
            send_tg(report)
        else: send_tg("🏁 Парсинг завершен! Не удалось собрать данные (возможно, сработал фильтр магазинов).")
    except Exception as e: send_tg(f"💥 Ошибка: {e}")

if __name__ == "__main__":
    print("=== ПАРСЕР ЗАПУЩЕН ===")
    main()
