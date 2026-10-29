import os
import json
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.environ.get("TG_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Скинь мне JSON-файл с задачами для парсера.\n\n"
        "Формат файла:\n"
        "```json\n"
        "[\n"
        "  {\"url\": \"https://www.marktplaats.nl/lrp/api/search?query=iphone\", \"pages\": 3},\n"
        "  {\"url\": \"https://www.marktplaats.nl/lrp/api/search?l1CategoryId=91\", \"pages\": 5}\n"
        "]\n"
        "```\n"
        "Где `url` — ссылка на API поиска (или обычный поиск), а `pages` — сколько страниц спарсить."
    )

@dp.message(F.document)
async def handle_json_file(message: types.Message):
    document = message.document
    if not document.file_name.endswith('.json'):
        await message.answer("❌ Пожалуйста, отправь файл с расширением `.json`")
        return

    try:
        file_info = await bot.get_file(document.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        content = downloaded_file.read().decode('utf-8')
        tasks_data = json.loads(content)
        
        if not isinstance(tasks_data, list):
            raise ValueError("JSON должен быть массивом (списком)")

        # Перезаписываем конфиг в базе (удаляем старый, вставляем новый)
        supabase.table("parser_configs").delete().neq("id", 0).execute()
        supabase.table("parser_configs").insert({"tasks": tasks_data}).execute()
        
        await message.answer(f"✅ Успешно загружено {len(tasks_data)} задач! Парсер подхватит их при следующем запуске.")
        
    except json.JSONDecodeError:
        await message.answer("❌ Ошибка: Файл не является валидным JSON.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
