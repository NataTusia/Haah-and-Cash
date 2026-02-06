import os
import asyncio
import logging
import datetime
import time
import requests
import psycopg2
import re
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

TARGET_LANGUAGE = "russian"
ERROR_SIGNATURE = "\n\n📩 <b>Перешлите это сообщение программисту Нате, она знает что с этим делать и поможет вам исправить ошибку.</b>"

# --- Допоміжні функції ---
def clean_text(text):
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()

def connect_to_db_with_retry():
    for i in range(3):
        try:
            return psycopg2.connect(DATABASE_URL)
        except Exception as e:
            time.sleep(5)
            if i == 2: raise e

def get_kyiv_time():
    # UTC + 2 (зима)
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)

# --- 1. Логіка AI (ОНОВЛЕНІ ЛІМІТИ) ---
async def generate_ai_post(topic, context, platform, time_slot=None, post_type=None):
    # Безпечний ліміт для Телеграм-капшн (1024 - заголовок)
    SAFE_LIMIT = 850 
    
    if platform == "tg":
        role_desc = "Ты опытный крипто-инвестор и ментор канала 'Хеш и Кэш'. Объясняешь сложное просто."
        
        if time_slot == "morning":
            greeting_rule = "Начни пост с короткого, бодрого приветствия."
        else:
            greeting_rule = "СТРОГО ЗАПРЕЩЕНО использовать приветствия (Привет, Здравствуйте и т.д.). Сразу переходи к сути темы."

        reqs = (
            f"{greeting_rule} Стиль: обучающий, дружеский, но конкретный. "
            "Используй аналогии. Добавь 1-2 эмодзи. Без сложного форматирования."
        )
        max_len = SAFE_LIMIT

    else: # Instagram
        role_desc = "Ты SMM-менеджер популярного крипто-блога."
        
        if post_type in ["Reels", "Карусель"]:
            reqs = (
                "Это пост для Reels или Карусели. Основной контент уже на видео/картинках. "
                "Твоя задача: Написать ОЧЕНЬ КОРОТКОЕ и цепляющее описание (максимум 2-3 предложения), "
                "которое мотивирует досмотреть видео или полистать карусель. Обязательно добавь Call to Action."
            )
            max_len = 400 # Для Reels ліміт і так маленький, тут все ок
        else:
            reqs = (
                "Это одиночный пост с фото. Напиши полноценный, вовлекающий текст, который раскрывает тему. "
                "Стиль: экспертный, но доступный."
            )
            # Тут зменшили з 950 до 850, щоб точно влазило в Телеграм прев'ю
            max_len = SAFE_LIMIT

    prompt = (
        f"{role_desc} Напиши на языке: {TARGET_LANGUAGE}.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Требования: {reqs}\n"
        f"ВАЖНО: Строгий лимит — {max_len} символов. Не превышай его."
    )
    
    try:
        response = model.generate_content(prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото ---
async def get_random_photo(keywords):
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0: return data[0]['urls']['regular']
            elif isinstance(data, dict) and 'urls' in data: return data['urls']['regular']
        elif response.status_code == 404:
            backup_url = f"https://api.unsplash.com/photos/random?query=cryptocurrency&client_id={UNSPLASH_KEY}&count=1&t={int(time.time())}"
            r2 = requests.get(backup_url)
            if r2.status_code == 200: return r2.json()[0]['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    return "https://images.unsplash.com/photo-1518546305927-5a555bb7020d?q=80&w=1000&auto=format&fit=crop"

# --- 3. Основна функція ---
async def prepare_draft(source_type, manual_day=None, from_command=False):
    day_now = manual_day if manual_day else get_kyiv_time().day
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        # TELEGRAM
        if source_type in ['morning', 'day', 'evening']:
            table_name = "telegram_posts"
            platform = "tg"
            cursor.execute(
                f"SELECT topic, content, photo_keywords FROM {table_name} WHERE day_number = %s AND time_slot = %s", 
                (day_now, source_type)
            )
            result = cursor.fetchone()
            if result:
                topic, short_context, keywords = result
                photo_url = await get_random_photo(keywords)
                
                text = await generate_ai_post(topic, short_context, platform, time_slot=source_type)
                
                caption = f"✈️ TG ({source_type.upper()} | День {day_now})\n\n{text}"
                
                builder = InlineKeyboardBuilder()
                builder.row(types.InlineKeyboardButton(text="✅ Опубликовать", callback_data="confirm_publish"))
                builder.row(
                    types.InlineKeyboardButton(text="🖼 Новое фото", callback_data=f"photo_{day_now}_{source_type}_tg"),
                    types.InlineKeyboardButton(text="📝 Новый текст", callback_data=f"text_{day_now}_{source_type}_tg")
                )
                
                # Обрізка на випадок, якщо AI все ж таки трохи перевищив ліміт
                if len(caption) > 1024: caption = caption[:1020] + "..."
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
            else:
                if from_command: await bot.send_message(ADMIN_ID, f"🤷‍♂️ TG: Пусто на {source_type} (День {day_now})")

        # INSTAGRAM
        elif source_type == 'inst':
            table_name = "instagram_posts"
            platform = "inst"
            cursor.execute(
                f"SELECT topic, content, post_type, photo_keywords FROM {table_name} WHERE day_number = %s", 
                (day_now,)
            )
            result = cursor.fetchone()
            if result:
                topic, short_context, post_type, keywords = result
                
                if post_type in ['Reels', 'Карусель']:
                    photo_url = "https://images.unsplash.com/photo-1611162617474-5b21e879e113?q=80&w=1000&auto=format&fit=crop"
                    caption_prefix = f"📹 INSTA {post_type.upper()} (ЗАГЛУШКА)"
                else:
                    photo_url = await get_random_photo(keywords)
                    caption_prefix = f"📸 INSTA SINGLE"

                text = await generate_ai_post(topic, short_context, platform, post_type=post_type)
                
                caption = f"{caption_prefix} (День {day_now})\n\n{text}"
                
                builder = InlineKeyboardBuilder()
                builder.row(types.InlineKeyboardButton(text="📝 Новый текст", callback_data=f"text_{day_now}_inst_inst"))
                if post_type == 'Single':
                     builder.add(types.InlineKeyboardButton(text="🖼 Новое фото", callback_data=f"photo_{day_now}_inst_inst"))

                if len(caption) > 1024: caption = caption[:1020] + "..."
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
            else:
                if from_command: await bot.send_message(ADMIN_ID, f"🤷‍♂️ Insta: Пусто (День {day_now})")

        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Ошибка ({source_type}): {e}{ERROR_SIGNATURE}", parse_mode="HTML")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        ua_time = get_kyiv_time()
        await message.answer(
            f"👋 Bot Updated (Safe Limits)!\n📅 Час (UA): {ua_time.strftime('%d.%m %H:%M')}\n"
            "👇 Тест:\n/gen_morning\n/gen_day\n/gen_evening\n/gen_inst"
        )

@dp.message(Command("gen_morning"))
async def cmd_gm(message: types.Message): await prepare_draft("morning", from_command=True)

@dp.message(Command("gen_day"))
async def cmd_gd(message: types.Message): await prepare_draft("day", from_command=True)

@dp.message(Command("gen_evening"))
async def cmd_ge(message: types.Message): await prepare_draft("evening", from_command=True)

@dp.message(Command("gen_inst"))
async def cmd_gi(message: types.Message): await prepare_draft("inst", from_command=True)

# --- Callbacks ---
@dp.callback_query(F.data.startswith("photo_"))
async def regen_photo(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    day, slot, plat = int(parts[1]), parts[2], parts[3]
    await callback.answer("🔄...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        if plat == 'tg':
            cursor.execute("SELECT photo_keywords FROM telegram_posts WHERE day_number=%s AND time_slot=%s", (day, slot))
        else:
            cursor.execute("SELECT photo_keywords FROM instagram_posts WHERE day_number=%s", (day,))
        result = cursor.fetchone()
        if result:
            new_url = await get_random_photo(result[0])
            media = InputMediaPhoto(media=new_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
        conn.close()
    except Exception as e: await callback.message.answer(f"Error: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    day, slot, plat = int(parts[1]), parts[2], parts[3]
    await callback.answer("📝...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        if plat == 'tg':
            cursor.execute("SELECT topic, content FROM telegram_posts WHERE day_number=%s AND time_slot=%s", (day, slot))
            res = cursor.fetchone()
            if res:
                # Передаємо slot як time_slot
                new_text = await generate_ai_post(res[0], res[1], "tg", time_slot=slot)
                new_cap = f"✈️ TG ({slot.upper()} | День {day})\n\n{new_text}"
        else:
            cursor.execute("SELECT topic, content, post_type FROM instagram_posts WHERE day_number=%s", (day,))
            res = cursor.fetchone()
            if res:
                # Передаємо res[2] (post_type)
                new_text = await generate_ai_post(res[0], res[1], "inst", post_type=res[2])
                prefix = f"📹 INSTA {res[2]}" if res[2] in ['Reels', 'Карусель'] else "📸 INSTA SINGLE"
                new_cap = f"{prefix} (День {day})\n\n{new_text}"

        if len(new_cap) > 1024: new_cap = new_cap[:1020] + "..."
        await callback.message.edit_caption(caption=new_cap, reply_markup=callback.message.reply_markup)
        conn.close()
    except Exception as e: await callback.message.answer(f"Error: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish(callback: types.CallbackQuery):
    cap = callback.message.caption
    clean_cap = cap.split("\n\n", 1)[1] if "\n\n" in cap else cap
    await bot.send_photo(CHANNEL_ID, callback.message.photo[-1].file_id, caption=clean_cap)
    await callback.message.edit_caption(caption=f"✅ POSTED\n\n{clean_cap}")

# --- WEB SERVER ---
async def handle(request): return web.Response(text="Bot is ALIVE")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['morning'])
    scheduler.add_job(prepare_draft, 'cron', hour=14, minute=0, args=['day'])
    scheduler.add_job(prepare_draft, 'cron', hour=19, minute=0, args=['evening'])
    scheduler.add_job(prepare_draft, 'cron', hour=12, minute=0, args=['inst'])
    scheduler.start()
    
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())