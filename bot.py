import os
import logging
import asyncio
import time
import telegram
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
_list_states = {}

def get_offset_from_db() -> int:
    try:
        res = supabase.table("bot_state").select("last_update_id").eq("id", 1).execute()
        if res.data: return int(res.data[0]["last_update_id"])
    except Exception as e: logger.error(f"DB error: {e}")
    return 0

def save_offset_to_db(offset: int):
    try: supabase.table("bot_state").upsert({"id": 1, "last_update_id": offset}).execute()
    except Exception as e: logger.error(f"DB error: {e}")

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Статистика"), KeyboardButton("📋 Список")],
        [KeyboardButton("👁 Топ-5"), KeyboardButton("🚀 Парсинг (3 стр)")],
        [KeyboardButton("⚙️ Фильтры"), KeyboardButton("❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def _authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == TG_CHAT_ID

async def _reject(update: Update):
    if update.effective_message:
        try: await update.effective_message.reply_text("⛔ Нет доступа.")
        except Exception: pass

ALLOWED_KEYS = {"cat", "q", "min_price", "max_price", "min_views", "days", "sort", "limit"}
SORT_MAP = {"views": ("views_count", True), "price": ("price_cents", False), "date": ("publication_date", True), "favs": ("favorites_count", True)}

def parse_filters(text: str) -> dict:
    result = {}
    for tok in text.split():
        if ":" not in tok: continue
        k, v = tok.split(":", 1)
        if k.strip().lower() in ALLOWED_KEYS and v.strip(): result[k.strip().lower()] = v.strip()
    return result

def _apply_filters(q, f: dict):
    if "cat" in f: q = q.ilike("category_l1", f"%{f['cat']}%")
    if "q" in f: q = q.ilike("title", f"%{f['q']}%")
    if "min_price" in f:
        try: q = q.gte("price_cents", int(float(f["min_price"]) * 100))
        except: pass
    if "max_price" in f:
        try: q = q.lte("price_cents", int(float(f["max_price"]) * 100))
        except: pass
    if "min_views" in f:
        try: q = q.gte("views_count", int(f["min_views"]))
        except: pass
    if "days" in f:
        try:
            dt = (datetime.now(timezone.utc) - timedelta(days=int(f["days"]))).strftime('%Y-%m-%d')
            q = q.gte("publication_date", dt)
        except: pass
    col, desc = SORT_MAP.get(f.get("sort", "views"), ("views_count", True))
    return q.order(col, desc=desc)

def format_item(i: int, item: dict) -> str:
    price = f"{item['price_cents']/100:.0f}€" if item.get("price_cents") else "—"
    title = (item.get("title") or "—")[:55]
    date_str = item.get("publication_date") or "—"
    return (f"{i}. <a href='{item.get('url','')}'>{title}</a>\n"
            f"   👁{item.get('views_count',0)} ❤️{item.get('favorites_count',0)} 💰{price} 📅{date_str}\n")

async def _send_list(reply_func, filters: dict, offset: int, state_key: str | None = None):
    limit = min(int(filters.get("limit", PAGE_SIZE)), MAX_LIMIT)
    try:
        cq = _apply_filters(supabase.table("marktplaats_listings").select("id", count="exact"), filters)
        total = cq.limit(1).execute().count or 0
        q = _apply_filters(supabase.table("marktplaats_listings").select(
            "id,title,url,views_count,favorites_count,price_cents,category_l1,publication_date"
        ), filters).range(offset, offset + limit - 1)
        items = q.execute().data or []

        if not items: return await reply_func("📭 Ничего не найдено.")

        body = f"🔍 <b>Результаты</b> (найдено {total})\n\n"
        for i, it in enumerate(items, 1): body += format_item(offset + i, it) + "\n"

        if state_key is None:
            state_key = os.urandom(3).hex()
            _list_states[state_key] = {"f": filters, "o": offset, "l": limit}
        
        nav = []
        if offset > 0: nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"pl:{state_key}:{max(0, offset - limit)}"))
        if total > offset + limit: nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"pl:{state_key}:{offset + limit}"))
        kb = InlineKeyboardMarkup([nav]) if nav else None
        
        try:
            await reply_func(body, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e): logger.error(f"TG Error: {e}")
    except Exception as e: await reply_func(f"⚠️ Ошибка: {e}")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return await _reject(update)
    text = (
        "👋 <b>Привет! Я бот для поиска объявлений.</b>\n\n"
        "Используй кнопки внизу или команды:\n"
        "/list q:iphone min_price:100\n\n"
        "🚀 <b>Запуск парсера:</b>\n"
        "• <code>/run</code> — стандартный (все категории по 3 стр)\n"
        "• <code>/run https://www.marktplaats.nl/l/boeken/ 20</code> — глубокий парсинг конкретной категории (20 страниц)"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return await _reject(update)
    try:
        total = supabase.table("marktplaats_listings").select("id", count="exact").limit(1).execute().count or 0
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        today_cnt = supabase.table("marktplaats_listings").select("id", count="exact").eq("publication_date", today).limit(1).execute().count or 0
        msg = f"📊 <b>Статистика</b>\n\n🗄 Всего в базе: <b>{total}</b>\n🆕 Добавлено сегодня: <b>{today_cnt}</b>"
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e: await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return await _reject(update)
    await _send_list(update.message.reply_text, parse_filters(" ".join(context.args or [])), 0)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return await _reject(update)
    await _send_list(update.message.reply_text, {"sort": "views", "limit": "5"}, 0)

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return await _reject(update)
    if not GH_TOKEN or not GH_REPO: return await update.message.reply_text("⚠️ GH не настроены.")
    
    pages = "3"
    links = ""
    
    if context.args:
        if len(context.args) >= 2 and context.args[0].startswith("http"):
            links = context.args[0]
            pages = context.args[1]
        elif context.args[0].isdigit():
            pages = context.args[0]

    try:
        payload = {
            "ref": "main", 
            "inputs": {
                "pages": pages, 
                "links": links
            }
        }
        
        r = http.post(
            f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches",
            headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json", "User-Agent": "MP-Bot"},
            json=payload, timeout=15,
        )
        
        if r.status_code in (200, 204):
            if links:
                await update.message.reply_text(f"🚀 Парсер запущен!\n🔗 Ссылка: {links}\n📄 Страниц: {pages}")
            else:
                await update.message.reply_text(f"🚀 Парсер запущен по всем категориям! (Страниц на категорию: {pages})")
        else:
            await update.message.reply_text(f"⚠️ GH {r.status_code}: {r.text[:100]}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return await _reject(update)
    text = update.message.text
    if text == "📊 Статистика": await cmd_stats(update, context)
    elif text == "📋 Список": await cmd_list(update, context)
    elif text == "👁 Топ-5": await cmd_top(update, context)
    elif text == "🚀 Парсинг (3 стр)": await cmd_run(update, context)
    elif text == "⚙️ Фильтры": await update.message.reply_text("⚙️ <b>Фильтры:</b>\n<code>cat:fietsen</code>\n<code>q:iphone</code>\n<code>min_price:100 max_price:500</code>\n<code>days:1</code>", parse_mode="HTML")
    elif text == "❓ Помощь": await cmd_start(update, context)
    else: await _send_list(update.message.reply_text, {"q": text}, 0)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _authorized(update): return await q.answer("⛔ Нет доступа")
    await q.answer()
    data = q.data or ""
    if data.startswith("pl:"):
        _, key, off = data.split(":", 2)
        state = _list_states.get(key)
        if state: await _send_list(q.edit_message_text, state["f"], int(off), state_key=key)

async def run_bot_limited_time():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler(["list", "find"], cmd_list))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_buttons))
    app.add_handler(CallbackQueryHandler(on_callback))

    await app.initialize()
    await app.start()
    offset = get_offset_from_db()
    
    start_time = time.time()
    MAX_RUNTIME = 20700 # 5ч 45м

    while time.time() - start_time < MAX_RUNTIME:
        try:
            updates = await app.bot.get_updates(offset=offset + 1, timeout=30)
            for u in updates:
                try: await app.process_update(u)
                except Exception as e: logger.error(f"Ошибка update: {e}")
                offset = u.update_id
                save_offset_to_db(offset)
        except Exception as e:
            logger.error(f"Ошибка polling: {e}")
            await asyncio.sleep(5)

    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(run_bot_limited_time())
