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

# ⚠️ ГЛАВНЫЙ ПЕРЕКЛЮЧАТЕЛЬ
# True: заходить в карточку каждого товара (обновляет просмотры, но медленно)
FETCH_DETAILS = True 

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
session = curl_requests.Session(impersonate="chrome120")
session.headers.update({"Accept-Language": "nl-NL,nl;q=0.9"})

# --- РАБОТА С БАЗОЙ ЗАДАЧ ---
def save_tasks_to_db(tasks):
    try:
        supabase.table("parser_configs").delete().neq("id", 0).execute()
        supabase.table("parser_configs").insert({"tasks": tasks}).execute()
        logger.info("Новый JSON сохранен в базу.")
    except Exception as e:
        logger.error(f"Ошибка сохранения JSON в базу: {e}")

def get_tasks_from_db():
    try:
        response = supabase.table("parser_configs").select("tasks").order("id", desc=True).limit(1).execute()
        if response.data:
            return response.data[0]["tasks"]
    except Exception as e:
        logger.error(f"Ошибка чтения JSON из базы: {e}")
    return []

def wait_for_tg_json(timeout_minutes=10):
    if not TG_TOKEN or not TG_CHAT_ID:
        return get_tasks_from_db()

    msg = f"⏳ Парсер запущен. Жду JSON ({timeout_minutes} мин). Если не пришлёшь — возьму последний из базы."
    session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": msg})

    last_update_id = 0
    try:
        r = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            updates = r.json().get("result", [])
            if updates: last_update_id = updates[-1]["update_id"]
    except Exception: pass

    logger.info(f"Ожидание файла в Telegram ({timeout_minutes} минут)...")
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
                    
                    if msg_obj and "document" in msg_obj:
                        doc = msg_obj["document"]
                        if doc.get("file_name", "").endswith(".json"):
                            logger.info(f"Получен файл: {doc['file_name']}")
                            file_info = session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile?file_id={doc['file_id']}", timeout=10).json()
                            file_url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_info['result']['file_path']}"
                            content = session.get(file_url, timeout=10).content.decode('utf-8')
                            
                            tasks = json.loads(content)
                            save_tasks_to_db(tasks)
                            
                            session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                                         json={"chat_id": TG_CHAT_ID, "text": "✅ Новый JSON получен! Начинаю."})
                            return tasks
        except Exception: pass
        time.sleep(2)

    return get_tasks_from_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
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
    except Exception: return None

def get_item_details(url):
    """Заходит по готовой ссылке и достает просмотры/избранное из window.__CONFIG__"""
    # Проверка, что ссылка полная (начинается с http)
    if not url.startswith("http"):
        url = f"https://www.marktplaats.nl{url}"
        
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200: 
            return 0, 0
        html = resp.text
        match = re.search(r'window\.__CONFIG__\s*=\s*(\{.+?\});', html, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            stats = data.get("listing", {}).get("stats", {})
            return stats.get("viewCount", 0), stats.get("favoritedCount", 0)
    except Exception as e:
        logger.error(f"Ошибка парсинга деталей: {e}")
    return 0, 0

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                     json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

def build_page_url(base_url, page_number):
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    limit = int(query.get('limit', [30])[0])
    query['offset'] = [str(page_number * limit)]
    query['limit'] = [str(limit)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

# --- СОХРАНЕНИЕ ---
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
                   f"💰 {price_str}\n📁 ID категории: {item['category']}\n"
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

# --- ГЛАВНАЯ ФУНКЦИЯ ---
def main():
    logger.info(f"🚀 Старт парсера (FETCH_DETAILS={FETCH_DETAILS})")
    tasks = wait_for_tg_json(timeout_minutes=10)
    if not tasks: return

    conn = init_db()
    
    for task in tasks:
        category_id = task.get("category_id")
        price_from = task.get("price_from")
        price_to = task.get("price_to")
        pages = task.get("pages", 1)
        
        base_url = f"https://www.marktplaats.nl/lrp/api/search?l1CategoryId={category_id}"
        if price_from is not None: base_url += f"&priceFrom={price_from}"
        if price_to is not None: base_url += f"&priceTo={price_to}"
        
        logger.info(f"Категория {category_id} (страниц: {pages})")
        
        for page in range(pages):
            url = build_page_url(base_url, page)
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
                item_id = item.get("itemId")
                if not item_id: continue
                
                # Формируем ссылку. Если API отдает относительную, она исправится в get_item_details
                raw_url = item.get("vipUrl")
                if not raw_url:
                    item_type = "a" if item_id.startswith("a") else "v"
                    raw_url = f"https://www.marktplaats.nl/{item_type}/{item_id}"
                elif not raw_url.startswith("http"):
                    raw_url = f"https://www.marktplaats.nl{raw_url}"
                
                views, favorites = 0, 0
                if FETCH_DETAILS or item_id not in existing:
                    views, favorites = get_item_details(raw_url)
                    time.sleep(random.uniform(1, 2)) # Пауза, чтобы не забанили
                
                parsed_items.append({
                    "id": item_id,
                    "title": item.get("title", "Без названия"),
                    "publication_date": item.get("date", ""),
                    "parsing_date": datetime.now(timezone.utc).isoformat(),
                    "category": str(category_id),
                    "price_cents": item.get("priceInfo", {}).get("priceCents"),
                    "views_count": views,
                    "favorites_count": favorites,
                    "url": raw_url,
                    "last_seen": datetime.now(timezone.utc).isoformat()
                })
            
            process_and_save(parsed_items, conn)
            time.sleep(random.uniform(1, 2))

    conn.close()
    logger.info("✅ Работа завершена.")

if __name__ == "__main__":
    main()
