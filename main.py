import os
import time
import random
import logging
from datetime import datetime, timezone
from typing import Optional

from curl_cffi import requests as curl_requests
from supabase import create_client, Client

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Конфигурация из переменных окружения ---
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PROXY_URL = os.environ.get("PROXY_URL")  # формат: http://user:pass@host:port

# Лимиты для контроля нагрузки и обхода ограничений Marktplaats
MAX_PAGES_PER_CATEGORY = 10      # ~300 объявлений на категорию за запуск
PAGE_LIMIT = 30                  # Размер страницы (стандарт для LRP API)
DELAY_BETWEEN_REQUESTS = (2, 5)  # Случайная задержка в секундах

# Ключевые слова для быстрого поиска (настройте под свои нужды)
KEYWORDS = ["gazelle", "iphone", "fiets", "bankstel", "laptop"]

# Инициализация клиента Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Сессия с имитацией TLS-отпечатка Chrome
session = curl_requests.Session(impersonate="chrome120")
session.headers.update({
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
})


def get_proxies() -> Optional[dict]:
    """Возвращает словарь для прокси или None, если прокси не задан."""
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


def polite_delay():
    """Случайная задержка между запросами для снижения риска блокировки."""
    time.sleep(random.uniform(*DELAY_BETWEEN_REQUESTS))


def fetch_json(url: str, max_retries: int = 3) -> Optional[dict]:
    """
    Выполняет GET-запрос и возвращает JSON.
    При ошибках (429, 403, 5xx) делает экспоненциальную выдержку.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(
                url,
                timeout=(10, 30),
                proxies=get_proxies(),
            )
            if resp.status_code == 429:
                wait = 10 * attempt
                logger.warning("Rate limit (429). Ждём %s секунд...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Ошибка запроса (попытка %s/%s): %s", attempt, max_retries, e)
            if attempt == max_retries:
                return None
            time.sleep(2 ** attempt)  # экспоненциальная выдержка
    return None


def build_search_url(l1_id: Optional[str] = None,
                     l2_id: Optional[str] = None,
                     query: Optional[str] = None,
                     offset: int = 0) -> str:
    """
    Формирует URL для внутреннего LRP API Marktplaats.
    Документация: https://www.marktplaats.nl/lrp/api/search
    """
    base = "https://www.marktplaats.nl/lrp/api/search"
    params = [f"limit={PAGE_LIMIT}", f"offset={offset}"]
    if l1_id:
        params.append(f"l1CategoryId={l1_id}")
    if l2_id:
        params.append(f"l2CategoryId={l2_id}")
    if query:
        params.append(f"query={query}")
    return f"{base}?{'&'.join(params)}"


def parse_listing(item: dict) -> Optional[dict]:
    """Преобразует один элемент из ответа LRP API в строку для Supabase."""
    try:
        item_id = item.get("itemId")
        if not item_id:
            return None

        # Извлечение цены (Marktplaats работает в центах)
        price_info = item.get("priceInfo", {})
        price_cents = price_info.get("priceCents")
        price_type = price_info.get("priceType")

        # Извлечение локации
        location = item.get("location", {})
        city = location.get("cityName")
        postcode = location.get("postcode")

        # Извлечение продавца
        seller = item.get("seller", {})
        seller_id = seller.get("sellerId")
        seller_name = seller.get("sellerName")
        seller_type = seller.get("sellerType")

        # Извлечение изображений
        images = []
        for img in item.get("images", []):
            if isinstance(img, dict) and "url" in img:
                images.append(img["url"])

        return {
            "id": item_id,
            "title": item.get("title", "Без названия"),
            "description": item.get("description", ""),
            "price_cents": price_cents,
            "price_type": price_type,
            "link": f"https://www.marktplaats.nl/v/{item_id}",
            "category_id": item.get("categoryId"),
            "location_city": city,
            "location_postcode": postcode,
            "seller_id": seller_id,
            "seller_name": seller_name,
            "seller_type": seller_type,
            "date_text": item.get("date"),
            "images": images,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error("Ошибка парсинга объявления %s: %s", item.get("itemId"), e)
        return None


def upsert_listings(listings: list[dict]):
    """Пакетная запись объявлений в Supabase с разрешением конфликтов по id."""
    if not listings:
        return
    try:
        response = (
            supabase.table("marktplaats_listings")
            .upsert(listings, on_conflict="id")  # Ключевой момент для идемпотентности
            .execute()
        )
        logger.info("Записано/обновлено %s объявлений.", len(listings))
        return response
    except Exception as e:
        logger.error("Ошибка записи в Supabase: %s", e)
        return None


def crawl_category(l1_id: str, l2_id: Optional[str] = None):
    """
    Обходит одну категорию (или подкатегорию) с пагинацией.
    """
    name = f"L1={l1_id}" + (f", L2={l2_id}" if l2_id else "")
    logger.info("--- Начинаем обход категории: %s ---", name)

    offset = 0
    for page in range(MAX_PAGES_PER_CATEGORY):
        url = build_search_url(l1_id=l1_id, l2_id=l2_id, offset=offset)
        logger.info("Запрос: %s", url)

        data = fetch_json(url)
        if not data:
            logger.warning("Нет данных для %s, offset=%s. Прерываем категорию.", name, offset)
            break

        # В ответе LRP API объявления обычно находятся в ключе 'listings' или 'items'
        items = data.get("listings") or data.get("items") or []
        if not items:
            logger.info("Пустая страница для %s. Конец категории.", name)
            break

        listings = [parse_listing(item) for item in items]
        listings = [l for l in listings if l]  # убираем None
        upsert_listings(listings)

        offset += PAGE_LIMIT
        polite_delay()

        # Проверка, есть ли следующая страница
        next_offset = data.get("meta", {}).get("next_offset")
        if next_offset is None and len(items) < PAGE_LIMIT:
            logger.info("Достигнут конец категории %s.", name)
            break

    logger.info("--- Завершён обход категории: %s ---", name)


def crawl_keywords():
    """
    Быстрый поиск по ключевым словам без указания категории.
    """
    for kw in KEYWORDS:
        logger.info("--- Поиск по ключевому слову: %s ---", kw)
        offset = 0
        for page in range(3):  # Ограничимся 3 страницами на слово
            url = build_search_url(query=kw, offset=offset)
            logger.info("Запрос: %s", url)

            data = fetch_json(url)
            if not data:
                break

            items = data.get("listings") or data.get("items") or []
            if not items:
                break

            listings = [parse_listing(item) for item in items]
            listings = [l for l in listings if l]
            upsert_listings(listings)

            offset += PAGE_LIMIT
            polite_delay()


def get_l1_categories() -> list[str]:
    """
    Возвращает список ID основных (L1) категорий.
    В реальном проекте лучше получать динамически, но для старта можно
    использовать известные ID или захардкодить несколько основных.
    Примеры L1 ID: 91 (Авто), 1744 (Велосипеды), 2 (Антиквариат) и т.д.
    """
    # Для примера — несколько популярных L1 категорий.
    # В продакшене замените на динамическое получение через /v1/categories
    return ["91", "1744", "2", "3", "4", "5"]


def main():
    logger.info("🚀 Запуск парсера Marktplaats")

    # 1. Гибридная стратегия: сначала обход категорий
    l1_categories = get_l1_categories()
    for l1_id in l1_categories:
        # Для простоты обходим только на уровне L1.
        # Для более полного охвата можно получать L2-подкатегории
        # через GET /lrp/api/search?l1CategoryId={l1_id} и обходить каждую.
        crawl_category(l1_id=l1_id)

    # 2. Затем — быстрый поиск по ключевым словам
    crawl_keywords()

    logger.info("✅ Парсер Marktplaats завершил работу.")


if __name__ == "__main__":
    main()
