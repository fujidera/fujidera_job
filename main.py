import os
import time
import random
import logging
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from curl_cffi import requests as curl_requests
from supabase import create_client, Client

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

CATEGORIES_MAP = {
    "antiek-en-kunst": 1, "audio-tv-en-foto": 31, "auto-kopen": 91,
    "auto-onderdelen": 2600, "auto-diversen": 48, "boeken": 201,
    "caravans-en-kamperen": 289, "cd-s-en-dvd-s": 1744, "computers-en-software": 322,
    "contacten-en-berichten": 378, "diensten-en-vakmensen": 1098, "dieren-en-toebehoren": 395,
    "doe-het-zelf-en-verbouw": 239, "fietsen-en-brommers": 445, "hobby-en-vrije-tijd": 1099,
    "huis-en-inrichting": 504, "huizen-en-kamers": 1032, "kinderen-en-baby-s": 565,
    "kleding-dames": 621, "kleding-heren": 1776, "motoren": 678,
    "muziek-en-instrumenten": 728, "postzegels-en-munten": 1784, "sieraden-tassen-en-uiterlijk": 1826,
    "spelcomputers-en-games": 356, "sport-en-fitness": 784, "telecommunicatie": 820,
    "tickets-en-kaartjes": 1984, "tuin-en-terras": 1847, "vacatures": 167,
    "vakantie": 856, "verzamelen": 895, "watersport-en-boten": 976,
    "witgoed-en-apparatuur": 537, "zakelijke-goederen": 1085, "diversen": 428
}

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

def build_api_url(raw_url):
    """Превращает ссылку юзера в правильный запрос к API"""
    price_from, price_to = None, None
    if "#" in raw_url:
        hash_part = raw_url.split("#")[1]
        f_match = re.search(r'PriceCentsFrom:(\d+)', hash_part)
        t_match = re.search(r'PriceCentsTo:(\d+)', hash_part)
        if f_match: price_from = int(f_match.group(1)) // 100
        if t_match: price_to = int(t_match.group(1)) // 100
        raw_url = raw_url.split("#")[0]

    base_api = "https://www.marktplaats.nl/lrp/api/search?limit=30"
    
    if "/l/" in raw_url:
        match = re.search(r'/l/([^/]+)/', raw_url)
        if match:
            slug = match.group(1)
            cat_id = CATEGORIES_MAP.get(slug)
            if cat_id:
                base_api += f"&l1CategoryId={cat_id}"
            else:
                base_api += f"&query={slug}"
    elif "/q/" in raw_url:
        match = re.search(r'/q/([^/]+)/', raw_url)
        if match:
            query = match.group(1).replace("+", "%20")
            base_api += f"&query={query}"

    if price_from is not None: base_api += f"&priceFrom={price_from}"
    if price_to is not None: base_api += f"&priceTo={price_to}"
    
    return base_api

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

def fetch_json(url):
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code in [403, 429]: return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e: 
        logger.error(f"Ошибка API: {e}")
        return None

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

    send_tg(f"✅ Поехали! Парсим {pages} страниц.")
    
    api_url = build_api_url(raw_url)
    logger.info(f"API URL: {api_url}")

    conn = init_db()
    for page in range(pages):
        url = f"{api_url}&offset={page * 30}"
        logger.info(f"Загружаем страницу {page+1}")
        data = fetch_json(url)
        if not data: break
        
        items = data.get("listings", [])
        if not items: 
            logger.warning(f"На странице {page+1} нет объявлений.")
            break
        
        parsed_items = []
        for item in items:
            item_id = str(item.get("itemId")).strip()
            if not item_id: continue
            
            # Берем ссылку из API (vipUrl), если нет — склеиваем вручную
            raw_href = item.get("vipUrl") or item.get("url") or f"/v/{item_id}"
            if raw_href.startswith("/"):
                item_url = f"https://www.marktplaats.nl{raw_href}"
            else:
                item_url = raw_href
                
            views, favs = 0, 0
            if FETCH_DETAILS:
                views, favs = get_item_details(item_url)
                time.sleep(random.uniform(1, 2))
                
            parsed_items.append({
                "id": item_id,
                "title": item.get("title", "Без названия"),
                "publication_date": item.get("date", ""),
                "parsing_date": datetime.now(timezone.utc).isoformat(),
                "category": raw_url, 
                "price_cents": item.get("priceInfo", {}).get("priceCents"),
                "views_count": views,
                "favorites_count": favs,
                "url": item_url,
                "last_seen": datetime.now(timezone.utc).isoformat()
            })
            
        process_and_save(parsed_items, conn)
        time.sleep(random.uniform(1, 2))
        
    conn.close()
    send_tg("🏁 Парсинг завершен!")
    logger.info("Работа завершена.")

if __name__ == "__main__":
    main()
