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
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)

# --- 1. Логіка AI ---
async def generate_ai_post(topic, context, platform, task_type="post", time_slot=None):
    CAPTION_LIMIT = 850
    SCENARIO_LIMIT = 2000 
    
    if platform == "tg":
        role_desc = "Ты опытный крипто-инвестор и ментор канала 'Хеш и Кэш'."
        if time_slot == "morning":
            greeting = "Начни пост с короткого, бодрого приветствия."
        else:
            greeting = "СТРОГО ЗАПРЕЩЕНО использовать приветствия. Сразу переходи к сути."
        
        reqs = f"{greeting} Стиль: обучающий, дружеский. Используй аналогии. Добавь 1-2 эмодзи."
        max_len = CAPTION_LIMIT

    else: # Instagram
        role_desc = "Ты SMM-менеджер и контент-мейкер популярного крипто-блога."
        
        if task_type == "scenario":
            reqs = (
                "Твоя задача — написать подробный СЦЕНАРИЙ для карусели (5-8 слайдов). "
                "Распиши контент для каждого слайда отдельно (Слайд 1: Заголовок + Визуал, Слайд 2: Тезис и т.д.). "
                "Пиши детально, чтобы дизайнер понял задачу. "
                "В конце добавь идею для обложки."
            )
            max_len = SCENARIO_LIMIT
        else:
            reqs = (
                "Твоя задача — написать вовлекающий ОПИСАНИЕ (Caption) под этот пост. "
                "Это текст, который люди будут читать под картинками. "
                "Он должен дополнять слайды, но не дублировать их слово в слово. "
                "Обязательно добавь призыв к действию (сохранить, подписаться) и хештеги."
            )
            max_len = CAPTION_LIMIT

    prompt = (
        f"{role_desc} Напиши на языке: {TARGET_LANGUAGE}.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Требования: {reqs}\n"
        f"ВАЖНО: Лимит символов — {max_len}."
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
        
        # --- TELEGRAM ---
        if source_type in ['morning', 'day', 'evening']:
            table_name = "telegram_posts"
            cursor.execute(f"SELECT topic, content, photo_keywords FROM {table_name} WHERE day_number = %s AND time_slot = %s", (day_now, source_type))
            result = cursor.fetchone()
            
            if result:
                topic, short_context, keywords = result
                photo_url = await get_random_photo(keywords)
                text = await generate_ai_post(topic, short_context, "tg", task_type="post", time_slot=source_type)
                
                caption = f"✈️ TG ({source_type.upper()} | День {day_now})\n\n{text}"
                
                builder = InlineKeyboardBuilder()
                builder.row(types.InlineKeyboardButton(text="✅ Опубликовать", callback_data="confirm_publish"))
                builder.row(
                    types.InlineKeyboardButton(text="🖼 Новое фото", callback_data=f"photo_{day_now}_{source_type}_tg"),
                    types.InlineKeyboardButton(text="📝 Новый текст", callback_data=f"text_{day_now}_{source_type}_tg_post")
                )
                
                if len(caption) > 1024: caption = caption[:1020] + "..."
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
            elif from_command:
                await bot.send_message(ADMIN_ID, f"🤷‍♂️ TG: Пусто на {source_type} (День {day_now})")

        # --- INSTAGRAM ---
        elif source_type == 'inst':
            table_name = "instagram_posts"
            # Reels ігноруємо (але ми їх вже видалили з БД, це про всяк випадок)
            cursor.execute(f"SELECT topic, content, post_type, photo_keywords FROM {table_name} WHERE day_number = %s AND post_type != 'Reels'", (day_now,))
            result = cursor.fetchone()
            
            if result:
                topic, short_context, post_type, keywords = result
                
                if post_type == 'Карусель':
                    photo_url = "https://images.unsplash.com/photo-1611162617474-5b21e879e113?q=80&w=1000&auto=format&fit=crop"
                    prefix = "📸 INSTA CAROUSEL"
                else:
                    photo_url = await get_random_photo(keywords)
                    prefix = "📸 INSTA SINGLE"

                # ОПИС (Caption)
                caption_text = await generate_ai_post(topic, short_context, "inst", task_type="post")
                full_caption = f"{prefix} (День {day_now})\n\n{caption_text}"
                
                builder_cap = InlineKeyboardBuilder()
                builder_cap.row(types.InlineKeyboardButton(text="📝 Переписать описание", callback_data=f"text_{day_now}_inst_inst_post"))
                if post_type == 'Single':
                     builder_cap.add(types.InlineKeyboardButton(text="🖼 Новое фото", callback_data=f"photo_{day_now}_inst_inst"))

                if len(full_caption) > 1024: full_caption = full_caption[:1020] + "..."
                
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=full_caption, reply_markup=builder_cap.as_markup())

                # СЦЕНАРІЙ (Тільки для Каруселі)
                if post_type == 'Карусель':
                    scenario_text = await generate_ai_post(topic, short_context, "inst", task_type="scenario")
                    header = f"🛠 <b>СЦЕНАРИЙ ДЛЯ ДИЗАЙНЕРА (День {day_now})</b>\n{'='*25}\n\n"
                    full_msg = header + scenario_text
                    
                    builder_scen = InlineKeyboardBuilder()
                    builder_scen.row(types.InlineKeyboardButton(text="🔄 Переписать сценарий", callback_data=f"text_{day_now}_inst_inst_scenario"))
                    
                    await bot.send_message(chat_id=ADMIN_ID, text=full_msg, parse_mode="HTML", reply_markup=builder_scen.as_markup())

            elif from_command:
                await bot.send_message(ADMIN_ID, f"🤷‍♂️ Insta: Пусто (День {day_now})")

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
            f"👋 Bot Online!\n📅 Час (UA): {ua_time.strftime('%d.%m %H:%M')}\n"
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
    day = int(parts[1])
    slot = parts[2]
    plat = parts[3]
    task_type = parts[4]
    
    await callback.answer("📝 Думаю...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        if plat == 'tg':
            cursor.execute("SELECT topic, content FROM telegram_posts WHERE day_number=%s AND time_slot=%s", (day, slot))
            res = cursor.fetchone()
            if res:
                new_text = await generate_ai_post(res[0], res[1], "tg", task_type="post", time_slot=slot)
                new_cap = f"✈️ TG ({slot.upper()} | День {day})\n\n{new_text}"
                if len(new_cap) > 1024: new_cap = new_cap[:1020] + "..."
                await callback.message.edit_caption(caption=new_cap, reply_markup=callback.message.reply_markup)
        
        else: # INSTAGRAM
            cursor.execute("SELECT topic, content, post_type FROM instagram_posts WHERE day_number=%s", (day,))
            res = cursor.fetchone()
            if res:
                new_text = await generate_ai_post(res[0], res[1], "inst", task_type=task_type)
                
                if task_type == "post":
                    prefix = "📸 INSTA SINGLE" if res[2] == 'Single' else "📸 INSTA CAROUSEL"
                    new_cap = f"{prefix} (День {day})\n\n{new_text}"
                    if len(new_cap) > 1024: new_cap = new_cap[:1020] + "..."
                    await callback.message.edit_caption(caption=new_cap, reply_markup=callback.message.reply_markup)
                
                elif task_type == "scenario":
                    header = f"🛠 <b>СЦЕНАРИЙ ДЛЯ ДИЗАЙНЕРА (День {day})</b>\n{'='*25}\n\n"
                    full_msg = header + new_text
                    await callback.message.edit_text(text=full_msg, parse_mode="HTML", reply_markup=callback.message.reply_markup)

        conn.close()
    except Exception as e: await callback.message.answer(f"Error: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish(callback: types.CallbackQuery):
    cap = callback.message.caption
    clean_cap = cap.split("\n\n", 1)[1] if "\n\n" in cap else cap
    await bot.send_photo(CHANNEL_ID, callback.message.photo[-1].file_id, caption=clean_cap)
    await callback.message.edit_caption(caption=f"✅ POSTED\n\n{clean_cap}")

# --- WEB SERVER (FIXED) ---
async def handle(request):
    return web.Response(text="I am alive")

async def main():
    logging.basicConfig(level=logging.INFO)

    # 1. ЗАПУСК ВЕБ-СЕРВЕРА (Це має бути першим!)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Отримуємо порт з оточення (для Render) або ставимо 10000 для локального тесту
    port = int(os.environ.get("PORT", 10000))
    
    # Запускаємо сервер на 0.0.0.0 (це критично важливо!)
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    logging.info(f"✅ Web Server started on port {port}")

    # 2. ЗАПУСК ПЛАНУВАЛЬНИКА
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['morning'])
    scheduler.add_job(prepare_draft, 'cron', hour=14, minute=0, args=['day'])
    scheduler.add_job(prepare_draft, 'cron', hour=19, minute=0, args=['evening'])
    scheduler.add_job(prepare_draft, 'cron', hour=12, minute=0, args=['inst'])
    scheduler.start()

    # 3. ЗАПУСК БОТА
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except:
        pass
        
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())