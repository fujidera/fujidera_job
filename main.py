import os
import time
import random
import logging
import re
import json
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

# Словарь для перевода слаг-категорий в ID
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

def wait_for_tg_input(timeout_minutes=10):
    """Ждет текст от юзера в формате: URL | PAGES"""
    msg = f"⏳ Парсер запущен. Скинь ссылку и кол-во страниц в формате:\n`URL | 10`\nЖду {timeout_minutes} минут..."
    session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

    last_update_id = 0
    try:
        r = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            updates = r.json().get("result", [])
            if updates: last_update_id = updates[-1]["update_id"]
    except Exception: pass

    logger.info(f"Ожидание сообщения в Telegram ({timeout_minutes} минут)...")
    start_time = time.time()
    
    while time.time() - start_time < timeout_minutes * 60:
        try:
            r = session.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", 
                params={"offset": last_update_id + 1, "timeout": 30}, timeout=40
            )
            if r.status_code == 200 and r.json().get("ok"):
                updates = r.json().get("result", [])
                for update in updates:
                    last_update_id = update["update_id"]
                    msg_obj = update.get("message")
                    
                    if msg_obj and "text" in msg_obj:
                        text = msg_obj["text"]
                        if "|" in text:
                            parts = text.split("|")
                            url = parts[0].strip()
                            try:
                                pages = int(parts[1].strip())
                            except:
                                pages = 1
                            
                            session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                                         json={"chat_id": TG_CHAT_ID, "text": f"✅ Принято! Парсим {pages} страниц."})
                            return url, pages
        except Exception: pass
        time.sleep(2)

    return None, 0

def build_api_url(raw_url):
    """Превращает любую ссылку Marktplaats в запрос к API"""
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
    
    return base_api, price_from, price_to

def init_db():
    conn = sqlite3.connect('marktplaats_parser.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS snapshots 
                    (id INTEGER PRIMARY KEY, listing_id TEXT, price_cents INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    return conn

def fetch_json(url):
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code in [403, 429]: return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e: 
        logger.error(f"Ошибка API: {e}")
        return None

def get_item_details(item_id):
    """Строим ссылку ТОЛЬКО из ID и достаем просмотры НАПРЯМУЮ ИЗ HTML."""
    item_id = str(item_id).strip()
    item_type = "a" if item_id.startswith("a") else "v"
    url = f"https://www.marktplaats.nl/{item_type}/{item_id}"
    
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200: 
            return 0, 0
        html = resp.text
        
        views, favorites = 0, 0
        
        # Ищем просмотры (например, <b>84x</b> bekeken)
        v_match = re.search(r'<b>(\d+)x</b>\s*bekeken', html)
        if v_match: 
            views = int(v_match.group(1))
        
        # Ищем избранное (например, <b>0x</b> bewaard)
        f_match = re.search(r'<b>(\d+)x</b>\s*bewaard', html)
        if f_match: 
            favorites = int(f_match.group(1))
            
        return views, favorites
    except Exception as e:
        logger.error(f"Ошибка получения просмотров для {item_id}: {e}")
        return 0, 0

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                     json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

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
                   f"💰 {price_str}\n📁 Категория: {item['category']}\n"
                   f"👁 {item['views_count']} | ❤️ {item['favorites_count']}\n"
                   f"🔗 <a href='{item['url']}'>Открыть</a>")
            send_telegram(msg)
        else:
            updated_listings.append(item)
            old_price = existing_data[item_id]
            if current_price != old_price:
                cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
                old_str = f"{old_price / 100:.2f} €" if old_price else "?"
                new_str = f"{current_price / 100:.2f} €" if current_price else "?"
                send_telegram(f"🔄 <b>Цена изменилась!</b>\n📦 <b>{item['title']}</b>\nБыло: <s>{old_str}</s>\nСтало: <b>{new_str}</b>\n🔗 <a href='{item['url']}'>Открыть</a>")

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
    logger.info(f"🚀 Старт парсера")
    raw_url, pages = wait_for_tg_input(timeout_minutes=10)
    
    if not raw_url or pages == 0:
        logger.warning("Ссылка не получена. Завершаем.")
        return

    api_base, p_from, p_to = build_api_url(raw_url)
    logger.info(f"Сформирован URL API: {api_base}")

    conn = init_db()
    
    for page in range(pages):
        url = f"{api_base}&offset={page * 30}"
        data = fetch_json(url)
        if not data: break
        
        items = data.get("listings", [])
        if not items: break
        
        page_ids = [item.get("itemId") for item in items if item.get("itemId")]
        
        existing = set()
        try:
            resp = supabase.table("marktplaats_listings").select("id").in_("id", page_ids).execute()
            existing = {row["id"] for row in resp.data}
        except Exception: pass
            
        parsed_items = []
        for item in items:
            item_id = str(item.get("itemId")).strip()
            if not item_id: continue
            
            item_type = "a" if item_id.startswith("a") else "v"
            full_url = f"https://www.marktplaats.nl/{item_type}/{item_id}"
            
            views, favorites = 0, 0
            if FETCH_DETAILS or item_id not in existing:
                views, favorites = get_item_details(item_id)
                time.sleep(random.uniform(1, 2))
            
            cat_id = "Custom"
            if "/l/" in raw_url:
                slug = raw_url.split("/l/")[1].split("/")[0]
                cat_id = str(CATEGORIES_MAP.get(slug, "Unknown"))

            parsed_items.append({
                "id": item_id,
                "title": item.get("title", "Без названия"),
                "publication_date": item.get("date", ""),
                "parsing_date": datetime.now(timezone.utc).isoformat(),
                "category": cat_id, 
                "price_cents": item.get("priceInfo", {}).get("priceCents"),
                "views_count": views,
                "favorites_count": favorites,
                "url": full_url,
                "last_seen": datetime.now(timezone.utc).isoformat()
            })
        
        process_and_save(parsed_items, conn)
        time.sleep(random.uniform(1, 2))

    conn.close()
    send_telegram("✅ Парсинг завершен!")
    logger.info("✅ Работа завершена.")

if __name__ == "__main__":
    main()
