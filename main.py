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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
session = curl_requests.Session(impersonate="chrome120")
session.headers.update({"Accept-Language": "nl-NL,nl;q=0.9"})

# --- SQLite (История цен) ---
def init_db():
    conn = sqlite3.connect('marktplaats_parser.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS snapshots 
                    (id INTEGER PRIMARY KEY, listing_id TEXT, price_cents INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    return conn

# --- Вспомогательные функции ---
def fetch_json(url):
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code in [403, 429]:
            logger.error(f"Блокировка! Код {resp.status_code}.")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Ошибка API: {e}")
        return None

def get_item_details(item_id):
    url = f"https://www.marktplaats.nl/v/{item_id}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200: return 0, 0
        html = resp.text
        views, favorites = 0, 0
        match = re.search(r'window\.__PRELOADED_STATE__\s*=\s*({.+?});', html)
        if match:
            data = json.loads(match.group(1))
            stats = data.get("listing", {}).get("stats", {}) or data.get("vip", {}).get("stats", {})
            views = stats.get("viewCount", 0)
            favorites = stats.get("favoriteCount", 0)
        return views, favorites
    except Exception:
        return 0, 0

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        session.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

def build_page_url(base_url, page_number):
    """Добавляет параметр offset для пагинации к API URL."""
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    limit = int(query.get('limit', [30])[0])
    query['offset'] = [str(page_number * limit)]
    query['limit'] = [str(limit)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

# --- Главная логика (Идентична Frinity-job) ---
def process_and_save(parsed_items, conn):
    if not parsed_items: return
    cursor = conn.cursor()
    ids_to_check = [item["id"] for item in parsed_items]
    
    existing_data = {}
    try:
        response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
        existing_data = {row["id"]: row["price_cents"] for row in response.data}
    except Exception as e:
        logger.error(f"Ошибка чтения Supabase: {e}")
        return

    new_listings, updated_listings = [], []
    
    for item in parsed_items:
        item_id = item["id"]
        current_price = item["price_cents"]
        
        if item_id not in existing_data:
            new_listings.append(item)
            cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
            
            price_str = f"{current_price / 100:.2f} €" if current_price else "См. описание"
            msg = (f"🆕 <b>Новое объявление!</b>\n\n📦 <b>{item['title']}</b>\n"
                   f"💰 Цена: {price_str}\n📁 {item['category']}\n"
                   f"👁 {item['views_count']} | ❤️ {item['favorites_count']}\n"
                   f"🔗 <a href='{item['url']}'>Открыть</a>")
            send_telegram(msg)
            
        else:
            updated_listings.append(item)
            old_price = existing_data[item_id]
            
            if current_price != old_price:
                logger.info(f"Изменение цены у {item_id}: {old_price} -> {current_price}")
                cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
                
                old_str = f"{old_price / 100:.2f} €" if old_price else "См. описание"
                new_str = f"{current_price / 100:.2f} €" if current_price else "См. описание"
                msg = (f"🔄 <b>Изменение цены!</b>\n\n📦 <b>{item['title']}</b>\n"
                       f"Было: <s>{old_str}</s>\nСтало: <b>{new_str}</b>\n"
                       f"🔗 <a href='{item['url']}'>Открыть</a>")
                send_telegram(msg)

    conn.commit()

    try:
        if new_listings:
            supabase.table("marktplaats_listings").insert(new_listings).execute()
            logger.info(f"Добавлено новых: {len(new_listings)}")
            
        if updated_listings:
            for item in updated_listings:
                supabase.table("marktplaats_listings").update({
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "views_count": item["views_count"],
                    "favorites_count": item["favorites_count"],
                    "price_cents": item["price_cents"]
                }).eq("id", item["id"]).execute()
            logger.info(f"Обновлено: {len(updated_listings)}")
    except Exception as e:
        logger.error(f"Ошибка записи в Supabase: {e}")

def main():
    logger.info("🚀 Старт парсера")
    conn = init_db()
    
    # Загружаем конфиг из Supabase
    try:
        response = supabase.table("parser_configs").select("tasks").order("id", desc=True).limit(1).execute()
        if not response.data:
            logger.warning("Конфиг не найден. Скиньте JSON боту.")
            return
        tasks = response.data[0]["tasks"]
    except Exception as e:
        logger.error(f"Ошибка загрузки конфига: {e}")
        return

    # Итерируемся по задачам
    for task in tasks:
        base_url = task.get("url")
        pages = task.get("pages", 1)
        if not base_url: continue
        
        logger.info(f"Задача: {base_url} (страниц: {pages})")
        for page in range(pages):
            url = build_page_url(base_url, page)
            data = fetch_json(url)
            if not data: break
            
            items = data.get("listings", [])
            if not items: break
            
            parsed_items = []
            for item in items:
                item_id = item.get("itemId")
                if not item_id: continue
                
                views, favorites = get_item_details(item_id)
                time.sleep(random.uniform(1, 3)) # Задержка
                
                parsed_items.append({
                    "id": item_id,
                    "title": item.get("title", "Без названия"),
                    "publication_date": item.get("date", ""),
                    "parsing_date": datetime.now(timezone.utc).isoformat(),
                    "category": item.get("category", {}).get("name", "Unknown"),
                    "price_cents": item.get("priceInfo", {}).get("priceCents"),
                    "views_count": views,
                    "favorites_count": favorites,
                    "url": f"https://www.marktplaats.nl/v/{item_id}",
                    "last_seen": datetime.now(timezone.utc).isoformat()
                })
            
            process_and_save(parsed_items, conn)
            time.sleep(random.uniform(3, 6))

    conn.close()
    logger.info("✅ Работа завершена.")

if __name__ == "__main__":
    main()
