import os
import time
import random
import logging
import re
import json
import sqlite3
from datetime import datetime, timezone

from curl_cffi import requests as curl_requests
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Конфигурация
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

KEYWORDS = ["gazelle", "iphone", "fiets"] # Твои ключевые слова
PAGES_PER_KEYWORD = 2

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
session = curl_requests.Session(impersonate="chrome120")
session.headers.update({"Accept-Language": "nl-NL,nl;q=0.9"})

# --- Работа с SQLite (как в оригинале) ---
def init_db():
    conn = sqlite3.connect('marktplaats_parser.db')
    cursor = conn.cursor()
    # Таблица снимков цен
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT,
            price_cents INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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
        if resp.status_code != 200:
            return 0, 0
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
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        session.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка TG: {e}")

# --- Основная логика (Идентична Frinity-job) ---
def process_and_save(parsed_items, conn):
    if not parsed_items:
        return
        
    cursor = conn.cursor()
    ids_to_check = [item["id"] for item in parsed_items]
    
    # 1. Получаем текущие данные из Supabase
    existing_data = {}
    try:
        # Supabase имеет лимит на размер IN, разбиваем если нужно (для простоты шлем сразу)
        response = supabase.table("marktplaats_listings").select("id, price_cents").in_("id", ids_to_check).execute()
        existing_data = {row["id"]: row["price_cents"] for row in response.data}
    except Exception as e:
        logger.error(f"Ошибка чтения Supabase: {e}")
        return

    new_listings = []
    updated_listings = []
    
    # 2. Сравниваем и разделяем на новые и измененные
    for item in parsed_items:
        item_id = item["id"]
        current_price = item["price_cents"]
        
        if item_id not in existing_data:
            # НОВЫЙ ТОВАР
            new_listings.append(item)
            
            # Пишем снимок в SQLite
            cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
            
            # Отправляем в ТГ
            price_str = f"{current_price / 100:.2f} €" if current_price else "См. описание"
            msg = (
                f"🆕 <b>Новое объявление!</b>\n\n"
                f"📦 <b>{item['title']}</b>\n"
                f"💰 Цена: {price_str}\n"
                f"📁 {item['category']}\n"
                f"👁 {item['views_count']} | ❤️ {item['favorites_count']}\n"
                f"🔗 <a href='{item['url']}'>Открыть на Marktplaats</a>"
            )
            send_telegram(msg)
            
        else:
            old_price = existing_data[item_id]
            # ОБНОВЛЯЕМ ДАТУ И ПРОСМОТРЫ
            updated_listings.append(item)
            
            # ЕСЛИ ЦЕНА ИЗМЕНИЛАСЬ
            if current_price != old_price:
                logger.info(f"Изменение цены у {item_id}: {old_price} -> {current_price}")
                
                # Пишем снимок в SQLite
                cursor.execute("INSERT INTO snapshots (listing_id, price_cents) VALUES (?, ?)", (item_id, current_price))
                
                # Отправляем в ТГ
                old_str = f"{old_price / 100:.2f} €" if old_price else "См. описание"
                new_str = f"{current_price / 100:.2f} €" if current_price else "См. описание"
                msg = (
                    f"🔄 <b>Изменение цены!</b>\n\n"
                    f"📦 <b>{item['title']}</b>\n"
                    f"Было: <s>{old_str}</s>\n"
                    f"Стало: <b>{new_str}</b>\n"
                    f"🔗 <a href='{item['url']}'>Открыть на Marktplaats</a>"
                )
                send_telegram(msg)

    conn.commit()

    # 3. Запись в Supabase
    try:
        if new_listings:
            supabase.table("marktplaats_listings").insert(new_listings).execute()
            logger.info(f"Добавлено новых: {len(new_listings)}")
            
        if updated_listings:
            # Обновляем last_seen и просмотры
            for item in updated_listings:
                supabase.table("marktplaats_listings").update({
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "views_count": item["views_count"],
                    "favorites_count": item["favorites_count"],
                    "price_cents": item["price_cents"] # На случай если цена изменилась
                }).eq("id", item["id"]).execute()
            logger.info(f"Обновлено: {len(updated_listings)}")
            
    except Exception as e:
        logger.error(f"Ошибка записи в Supabase: {e}")

def main():
    logger.info("🚀 Старт парсера (Архитектура Frinity-job)")
    conn = init_db()
    
    for kw in KEYWORDS:
        logger.info(f"Ищем: {kw}")
        offset = 0
        for _ in range(PAGES_PER_KEYWORD):
            url = f"https://www.marktplaats.nl/lrp/api/search?query={kw}&limit=30&offset={offset}"
            data = fetch_json(url)
            if not data: break
            
            items = data.get("listings", [])
            if not items: break
            
            parsed_items = []
            for item in items:
                item_id = item.get("itemId")
                if not item_id: continue
                
                # Достаем просмотры (медленно, но нужно)
                views, favorites = get_item_details(item_id)
                time.sleep(random.uniform(1, 3)) # Задержка
                
                parsed_items.append({
                    "id": item_id,
                    "title": item.get("title", "Без названия"),
                    "price_cents": item.get("priceInfo", {}).get("priceCents"),
                    "category": item.get("category", {}).get("name", "Unknown"),
                    "views_count": views,
                    "favorites_count": favorites,
                    "url": f"https://www.marktplaats.nl/v/{item_id}",
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                    "last_seen": datetime.now(timezone.utc).isoformat()
                })
            
            process_and_save(parsed_items, conn)
            
            offset += 30
            time.sleep(random.uniform(3, 6))

    conn.close()
    logger.info("✅ Работа завершена.")

if __name__ == "__main__":
    main()
