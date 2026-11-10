import os
import logging
import asyncio
import time
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)
from supabase import create_client
from curl_cffi import requests as curl_requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = int(os.environ["TG_CHAT_ID"])
GH_TOKEN = os.environ.get("GH_TOKEN")
GH_REPO = os.environ.get("GH_REPO")
GH_WORKFLOW = os.environ.get("GH_WORKFLOW", "parse.yml")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
http = curl_requests.Session()

PAGE_SIZE = 10
MAX_LIMIT = 30

_list_states: dict[str, dict] = {}

# ---------- Клавиатура ----------
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Статистика"), KeyboardButton("📋 Список")],
        [KeyboardButton("👁 Топ-5"), KeyboardButton("🚀 Парсинг (3 стр)")],
        [KeyboardButton("⚙️ Фильтры"), KeyboardButton("❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

# ---------- Работа с offset в БД ----------
def get_offset_from_db() -> int:
    try:
        res = supabase.table("bot_state").select("last_update_id").eq("id", 1).execute()
        if res.data:
            return int(res.data[0]["last_update_id"])
    except Exception as e:
        logger.error(f"DB error getting offset: {e}")
    return 0

def save_offset_to_db(offset: int):
    try:
        supabase.table("bot_state").upsert({"id": 1, "last_update_id": offset}).execute()
    except Exception as e:
        logger.error(f"DB error saving offset: {e}")

# ---------- auth ----------
def _authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == TG_CHAT_ID

async def _reject(update: Update):
    if update.effective_message:
        try:
            await update.effective_message.reply_text("⛔ Нет доступа.")
        except Exception:
            pass

# ---------- фильтры ----------
ALLOWED_KEYS = {
    "cat", "q", "min_price", "max_price", "min_views", "max_views",
    "min_favs", "max_favs", "days", "sort", "limit",
}

SORT_MAP = {
    "views": ("views_count", True),
    "price": ("price_cents", False),
    "date": ("parsing_date", True),
    "favs": ("favorites_count", True),
}

def parse_filters(text: str) -> dict:
    result = {}
    for tok in text.split():
        if ":" not in tok:
            continue
        k, v = tok.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k in ALLOWED_KEYS and v:
            result[k] = v
    return result

def filters_to_string(f: dict) -> str:
    return " ".join(f"{k}:{v}" for k, v in f.items()) or "—"

def _apply_filters(q, f: dict):
    if "cat" in f:
        q = q.ilike("category", f"%{f['cat']}%")
    if "q" in f:
        q = q.ilike("title", f"%{f['q']}%")
    for key, col, mul in (
        ("min_price", "price_cents", 100),
        ("max_price", "price_cents", 100),
        ("min_views", "views_count", 1),
        ("max_views", "views_count", 1),
        ("min_favs", "favorites_count", 1),
        ("max_favs", "favorites_count", 1),
    ):
        if key in f:
            try:
                val = int(float(f[key]) * mul)
                q = q.gte(col, val) if key.startswith("min") else q.lte(col, val)
            except Exception:
                pass
    if "days" in f:
        try:
            dt = (datetime.now(timezone.utc) - timedelta(days=int(f["days"]))).isoformat()
            q = q.gte("parsing_date", dt)
        except Exception:
            pass
    col, desc = SORT_MAP.get(f.get("sort", "views"), ("views_count", True))
    return q.order(col, desc=desc)

# ---------- форматирование ----------
def format_item(i: int, item: dict) -> str:
    price = f"{item['price_cents']/100:.0f}€" if item.get("price_cents") else "—"
    title = (item.get("title") or "—")[:55]
    return (
        f"{i}. <a href='{item.get('url','')}'>{title}</a>\n"
        f"   👁{item.get('views_count',0)} ❤️{item.get('favorites_count',0)} "
        f"💰{price} 📂{item.get('category','—')}\n"
    )

def _save_state(state: dict) -> str:
    key = os.urandom(3).hex()
    _list_states[key] = state
    if len(_list_states) > 500:
        for k in list(_list_states.keys())[:100]:
            _list_states.pop(k, None)
    return key

async def _send_list(reply_func, filters: dict, offset: int, state_key: str | None = None):
    limit = min(int(filters.get("limit", PAGE_SIZE)), MAX_LIMIT)
    try:
        try:
            cq = _apply_filters(
                supabase.table("marktplaats_listings").select("id", count="exact"),
                filters,
            )
            total = cq.limit(1).execute().count or 0
        except Exception:
            total = None

        q = _apply_filters(
            supabase.table("marktplaats_listings").select(
                "id,title,url,views_count,favorites_count,price_cents,category"
            ),
            filters,
        ).range(offset, offset + limit - 1)
        items = q.execute().data or []

        if not items:
            await reply_func("📭 Ничего не найдено по этим фильтрам.")
            return

        header = "🔍 <b>Результаты</b>"
        if total is not None:
            header += f" (найдено {total})"
        header += f"\n<i>{filters_to_string(filters)}</i>\n\n"
        body = header
        for i, it in enumerate(items, 1):
            body += format_item(offset + i, it) + "\n"

        if state_key is None:
            state_key = _save_state({"f": filters, "o": offset, "l": limit})
        nav = []
        if offset > 0:
            prev = max(0, offset - limit)
            nav.append(InlineKeyboardButton(
                "⬅️ Назад", callback_data=f"pl:{state_key}:{prev}"
            ))
        if total is not None and offset + limit < total:
            nxt = offset + limit
            nav.append(InlineKeyboardButton(
                "Вперёд ➡️", callback_data=f"pl:{state_key}:{nxt}"
            ))
        kb = InlineKeyboardMarkup([nav]) if nav else None

        await reply_func(
            body, parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=kb,
        )
    except Exception as e:
        logger.exception("send_list error")
        await reply_func(f"⚠️ Ошибка: {e}")

# ---------- команды ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _reject(update)
    text = (
        "👋 <b>Управление парсером Marktplaats</b>\n\n"
        "Используйте кнопки внизу экрана для быстрого доступа.\n"
        "Для сложных фильтров используйте команды:\n"
        "<code>/list cat:auto-s min_price:5000 limit:10</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _reject(update)
    try:
        total = supabase.table("marktplaats_listings")\
            .select("id", count="exact").limit(1).execute().count or 0
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        today_cnt = supabase.table("marktplaats_listings")\
            .select("id", count="exact")\
            .gte("parsing_date", today).limit(1).execute().count or 0
        top = supabase.table("marktplaats_listings").select(
            "title,url,views_count,favorites_count,price_cents,category"
        ).order("views_count", desc=True).limit(5).execute().data or []

        msg = (
            f"📊 <b>Статистика</b>\n\n"
            f"🗄 Всего в базе: <b>{total}</b>\n"
            f"🆕 Сегодня: <b>{today_cnt}</b>\n\n"
            f"👁 <b>Топ-5 за всё время:</b>\n\n"
        )
        for i, it in enumerate(top, 1):
            msg += format_item(i, it) + "\n"
        await update.message.reply_text(
            msg, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _reject(update)
    filters = parse_filters(" ".join(context.args or []))
    await _send_list(update.message.reply_text, filters, 0)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _reject(update)
    n, cat = PAGE_SIZE, None
    for a in (context.args or []):
        if a.isdigit():
            n = min(int(a), MAX_LIMIT)
        elif a.startswith("cat:"):
            cat = a.split(":", 1)[1]
        else:
            cat = a
    filters = {"sort": "views", "limit": str(n)}
    if cat:
        filters["cat"] = cat
    await _send_list(update.message.reply_text, filters, 0)

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _reject(update)
    if not GH_TOKEN or not GH_REPO:
        await update.message.reply_text("⚠️ GH_TOKEN/GH_REPO не настроены.")
        return

    pages, links = "3", ""
    for a in (context.args or []):
        if ":" in a:
            k, v = a.split(":", 1)
            if k == "pages":
                pages = v
            elif k == "links":
                links = v.replace(",", "\n")

    payload = {"ref": "main", "inputs": {"pages": str(pages), "links": links}}
    try:
        r = http.post(
            f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches",
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "MP-Bot-Trigger"
            },
            json=payload, timeout=15,
        )
        if r.status_code in (200, 204):
            n = len(links.splitlines()) if links else "дефолт"
            await update.message.reply_text(
                f"🚀 Запущено: {n} ссылок × {pages} стр.\n"
                f"Отчёт придёт отдельно от парсера."
            )
        else:
            await update.message.reply_text(f"⚠️ GH {r.status_code}: {r.text[:200]}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

# ---------- Обработка кнопок клавиатуры ----------
async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _reject(update)
    
    text = update.message.text

    if text == "📊 Статистика":
        await cmd_stats(update, context)
    elif text == "📋 Список":
        await cmd_list(update, context)
    elif text == "👁 Топ-5":
        await cmd_top(update, context)
    elif text == "🚀 Парсинг (3 стр)":
        context.args = ["pages:3"]
        await cmd_run(update, context)
    elif text == "⚙️ Фильтры":
        help_text = (
            "⚙️ <b>Как фильтровать объявления</b>\n\n"
            "Отправьте команду <code>/list</code> и добавьте к ней параметры:\n\n"
            "📁 <b>Категория:</b> <code>cat:fietsen</code>\n"
            "🔍 <b>Поиск по названию:</b> <code>q:iphone</code>\n"
            "💰 <b>Цена (в €):</b> <code>min_price:100 max_price:500</code>\n"
            "👁 <b>Просмотры:</b> <code>min_views:50</code>\n"
            "❤️ <b>Избранное:</b> <code>min_favs:5</code>\n"
            "📅 <b>За последние N дней:</b> <code>days:1</code>\n"
            "🔄 <b>Сортировка:</b> <code>sort:views|price|date|favs</code>\n"
            "📄 <b>Кол-во:</b> <code>limit:10</code>\n\n"
            "📝 <b>Пример:</b>\n"
            "<code>/list cat:auto-s min_price:5000 sort:price limit:10</code>"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")
    elif text == "❓ Помощь":
        await cmd_start(update, context)
    else:
        # Если написали просто текст, ищем это в названии
        filters = {"q": text}
        await _send_list(update.message.reply_text, filters, 0)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _authorized(update):
        try:
            await q.answer("⛔ Нет доступа.")
        except Exception:
            pass
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith("pl:"):
        return
    try:
        _, key, off = data.split(":", 2)
        state = _list_states.get(key)
        if not state:
            await q.edit_message_text("⌛ Сессия истекла. Повтори /list")
            return
        await _send_list(q.edit_message_text, state["f"], int(off), state_key=key)
    except Exception as e:
        logger.exception("callback error")
        await q.edit_message_text(f"⚠️ {e}")

# ---------- Главный цикл ----------
async def run_bot_limited_time():
    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler(["list", "find"], cmd_list))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("run", cmd_run))
    
    # Обработчик текста (для кнопок и быстрого поиска)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_buttons))
    
    app.add_handler(CallbackQueryHandler(on_callback))

    await app.initialize()
    await app.start()

    offset = get_offset_from_db()
    logger.info(f"Бот запущен. Начинаем с update_id: {offset}")

    start_time = time.time()
    MAX_RUNTIME = 20700  # 5 часов 45 минут

    while time.time() - start_time < MAX_RUNTIME:
        try:
            updates = await app.bot.get_updates(offset=offset + 1, timeout=30)

            for u in updates:
                try:
                    await app.process_update(u)
                except Exception as e:
                    logger.error(f"Ошибка обработки update {u.update_id}: {e}")

                offset = u.update_id
                save_offset_to_db(offset)

        except Exception as e:
            logger.error(f"Ошибка polling: {e}")
            await asyncio.sleep(5)

    logger.info("Время вышло (5ч 45м). Завершаем работу.")
    await app.stop()
    await app.shutdown()

def main():
    asyncio.run(run_bot_limited_time())

if __name__ == "__main__":
    main()
