import os
import time
import random
import logging
import re
import sqlite3
from datetime import datetime, timezone

from curl_cffi import requests as curl_requests
from supabase import create_client, Client
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

FETCH_DETAILS = True

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
session = curl_requests.Session(impersonate="chrome120")
session.headers.update({"Accept-Language": "nl-NL,nl;q=0.9"})

def get_updates(offset):
    try:
        r = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json().get("result", [])
    except Exception: pass
    return []

def wait_for_text(last_update_id, timeout_minutes=10):
    start_time = time.time()
    while time.time() - start_time < timeout_minutes * 60:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = max(last_update_id, update["update_id"])
            msg_obj = update.get("message")
            if msg_obj and "text" in msg_obj:
                return msg_obj["text"], last_update_id
        time.sleep(2)
    return None, last_update_id

def send_tg(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

def get_page_url(base_url, page):
    if page == 1:
        return base_url
    if '#' in base_url:
        url, hash_part = base_url.split('#', 1)
        hash_part = '#' + hash_part
    else:
        url, hash_part = base_url, ''
    
    url = url.rstrip('/')
    return f"{url}/p/{page}/{hash_part}"

def get_item_details(item_url):
    try:
        resp = session.get(item_url, timeout=15, allow_redirects=True)
        if resp.status_code != 200: 
            return 0, 0
        html = resp.text
        
        if "We hebben overal gezocht" in html:
            logger.warning(f"404: {item_url}")
            return 0, 0

        views, favs = 0, 0
        v_match = re.search(r'(\d+)\s*x\s*bekeken', html, re.IGNORECASE)
        if v_match: views = int(v_match.group(1))
        
        f_match = re.search(r'(\d+)\s*x\s*bewaard', html, re.IGNORECASE)
        if f_match: favs = int(f_match.group(1))
        
        return views, favs
    except Exception as e:
        logger.error(f"Ошибка парсинга {item_url}: {e}")
        return 0, 0

def init_db():
    conn = sqlite3.connect('marktplaats_parser.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS snapshots 
                    (id INTEGER PRIMARY KEY, listing_id TEXT, price_cents INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    return conn

def process_and_save(parsed_items, conn):
    if not parsed_items: return
    cursor = conn.cursor()
    ids_to_check = [item["id"] for item in parsed_items]
    
    existing_data = {}
    try:
        response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
        existing_data = {row["id"]: row["price_cents"] for row in response.data}
    except Exception: return

    new_listings, updated_listings = [], []
    
    for item in parsed_items:
        item_id = item["id"]
        current_price = item["price_cents"]
        
        if item_id not in existing_data:
            new_listings.append(item)
            cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
            
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
                cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
                old_str = f"{old_price / 100:.2f} €" if old_price else "?"
                new_str = f"{current_price / 100:.2f} €" if current_price else "?"
                send_tg(f"🔄 <b>Цена изменилась!</b>\n📦 <b>{item['title']}</b>\nБыло: <s>{old_str}</s>\nСтало: <b>{new_str}</b>\n🔗 <a href='{item['url']}'>Открыть</a>")

    conn.commit()

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
        logger.error("Нет токена/чата ТГ.")
        return

    updates = get_updates(offset=0)
    last_update_id = updates[-1]["update_id"] if updates else 0

    send_tg("1️⃣ Скинь ссылку на категорию или поиск\n(например: <code>https://www.marktplaats.nl/l/computers-en-software/#PriceCentsFrom:12000|PriceCentsTo:95000</code>)")
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

    send_tg(f"✅ Поехали! Парсим {pages} страниц.")
    
    conn = init_db()
    for page in range(1, pages + 1):
        page_url = get_page_url(raw_url, page)
        logger.info(f"Загружаем страницу {page}: {page_url}")
        
        try:
            resp = session.get(page_url, timeout=15)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error(f"Ошибка загрузки страницы {page_url}: {e}")
            break
            
        soup = BeautifulSoup(html, 'html.parser')
        articles = soup.find_all('article')
        
        if not articles:
            logger.warning(f"На странице {page} не найдено объявлений.")
            break
            
        parsed_items = []
        for art in articles:
            a = art.find('a', href=True)
            if not a: continue
            
            href = a['href'].split('?')[0] 
            if not (href.startswith('/v/') or href.startswith('/a/')): 
                continue
                
            item_id = href.split('/')[-1]
            full_url = f"https://www.marktplaats.nl{href}"
            
            title = "Без названия"
            title_tag = art.find(class_='hz-StructuredListing-title')
            if title_tag: 
                title = title_tag.text.strip()
            
            price_cents = 0
            price_tag = art.find(class_='hz-Title')
            if price_tag:
                price_text = price_tag.text.strip().replace('.', '').replace(',', '.')
                m = re.search(r'(\d+(?:\.\d+)?)', price_text)
                if m: 
                    price_cents = int(float(m.group(1)) * 100)
            
            views, favs = 0, 0
            if FETCH_DETAILS:
                views, favs = get_item_details(full_url)
                time.sleep(random.uniform(1, 2))
                
            parsed_items.append({
                "id": item_id,
                "title": title,
                "publication_date": "", 
                "parsing_date": datetime.now(timezone.utc).isoformat(),
                "category": raw_url, 
                "price_cents": price_cents,
                "views_count": views,
                "favorites_count": favs,
                "url": full_url,
                "last_seen": datetime.now(timezone.utc).isoformat()
            })
            
        process_and_save(parsed_items, conn)
        time.sleep(random.uniform(1, 2))
        
    conn.close()
    send_tg("🏁 Парсинг завершен!")
    logger.info("Работа завершена.")

if __name__ == "__main__":
    main()
