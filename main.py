import os
import time
import random
import logging
import re
import json
from datetime import datetime, timezone
from bs4 import BeautifulSoup

from curl_cffi import requests as curl_requests
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PROXY_URL = os.environ.get("PROXY_URL")

# Настройки парсинга
FETCH_DETAILS = True  # Включи, если нужны просмотры и избранное (медленнее)
KEYWORDS = ["gazelle", "iphone", "fiets"]
DELAY = (2, 4)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
session = curl_requests.Session(impersonate="chrome120")
session.headers.update({"Accept-Language": "nl-NL,nl;q=0.9"})

def get_proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

def fetch_json(url):
    try:
        resp = session.get(url, timeout=15, proxies=get_proxies())
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Ошибка API: {e}")
        return None

def fetch_html(url):
    try:
        resp = session.get(url, timeout=15, proxies=get_proxies())
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"Ошибка HTML: {e}")
        return None

def get_item_details(item_id):
    """Заходит в карточку товара и вытаскивает просмотры и избранное из JSON внутри HTML."""
    url = f"https://www.marktplaats.nl/v/{item_id}"
    html = fetch_html(url)
    if not html:
        return 0, 0

    views, favorites = 0, 0
    try:
        # Ищем встроенный JSON Marktplaats
        match = re.search(r'window\.__PRELOADED_STATE__\s*=\s*({.+?});', html)
        if match:
            data = json.loads(match.group(1))
            # Пытаемся найти статистику в JSON
            stats = data.get("listing", {}).get("stats", {})
            if not stats:
                stats = data.get("vip", {}).get("stats", {})
                
            views = stats.get("viewCount", 0)
            favorites = stats.get("favoriteCount", 0)
    except Exception as e:
        logger.error(f"Не удалось распарсить детали для {item_id}: {e}")
        
    return views, favorites

def parse_item(item):
    """Берет только запрошенные поля из списка поиска."""
    try:
        item_id = item.get("itemId")
        if not item_id:
            return None

        # Цена
        price_cents = item.get("priceInfo", {}).get("priceCents")

        # Дата публикации (в API она приходит как строка "Vandaag", "Gisteren" или timestamp)
        pub_date = item.get("date", "")

        # Категория (пытаемся достать название)
        cat_name = item.get("category", {}).get("name", "Unknown")

        # Ссылка
        url = f"https://www.marktplaats.nl/v/{item_id}"

        # Просмотры и избранное (если включено)
        views, favorites = 0, 0
        if FETCH_DETAILS:
            views, favorites = get_item_details(item_id)
            time.sleep(random.uniform(1, 3)) # Задержка, чтобы не забанили при заходе в карточки

        return {
            "id": item_id,
            "publication_date": pub_date,
            "parsing_date": datetime.now(timezone.utc).isoformat(),
            "category": cat_name,
            "price_cents": price_cents,
            "views_count": views,
            "favorites_count": favorites,
            "url": url
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга элемента: {e}")
        return None

def save_to_db(listings):
    if not listings:
        return
    try:
        supabase.table("marktplaats_listings").upsert(listings, on_conflict="id").execute()
        logger.info(f"Успешно сохранено {len(listings)} шт.")
    except Exception as e:
        logger.error(f"Ошибка Supabase: {e}")

def main():
    logger.info("🚀 Старт парсера (только нужные поля)")
    
    for kw in KEYWORDS:
        logger.info(f"Ищем: {kw}")
        offset = 0
        for _ in range(2):  # Максимум 2 страницы на слово
            url = f"https://www.marktplaats.nl/lrp/api/search?query={kw}&limit=30&offset={offset}"
            data = fetch_json(url)
            if not data: break
            
            items = data.get("listings", [])
            if not items: break
            
            parsed = [parse_item(i) for i in items]
            parsed = [p for p in parsed if p]
            
            save_to_db(parsed)
            
            offset += 30
            time.sleep(random.uniform(*DELAY))

if __name__ == "__main__":
    main()
