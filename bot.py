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
ERROR_SIGNATURE = "\n\n📩 <b>Перешлите это сообщение программисту Нате.</b>"

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

def get_kyiv_date():
    """Повертає об'єкт DATE за Києвом"""
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    return now.date()

# --- 1. Логіка AI (З АВТО-КОРЕКЦІЄЮ) ---
async def generate_ai_post(topic, context, platform, task_type="post", time_slot=None):
    # Встановлюємо жорсткі, але безпечні ліміти
    # 950 - це щоб із заголовком (TG | 2026-02-05) точно влізло в 1024
    MAX_CAPTION_LENGTH = 850 
    
    if platform == "tg":
        role_desc = "Ты опытный крипто-инвестор и ментор канала 'Хеш и Кэш'."
        if time_slot == "morning":
            greeting = "Начни с очень короткого приветствия."
        else:
            greeting = "БЕЗ ПРИВЕТСТВИЙ. Сразу к сути."
        
        reqs = (
            f"{greeting} Стиль: лаконичный, обучающий. "
            "Максимум 2 абзаца. 1-2 эмодзи. "
            "Пиши так, чтобы текст не нужно было сокращать."
        )
    else: # Instagram
        role_desc = "Ты SMM-менеджер крипто-блога."
        if task_type == "scenario":
            reqs = "Напиши подробный СЦЕНАРИЙ для карусели (5-7 слайдов). Детально распиши текст для каждого слайда."
        else:
            reqs = (
                "Напиши ОПИСАНИЕ (Caption) под пост. "
                "Текст должен быть СЖАТЫМ и емким. Самая суть + призыв сохранить."
            )

    # 1. Перша спроба генерації
    prompt = (
        f"{role_desc} Язык: {TARGET_LANGUAGE}.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Требования: {reqs}\n"
    )
    
    # Для сценарію ліміт не важливий, там окреме повідомлення
    if task_type == "scenario":
        prompt += "Лимит: до 2000 символов."
    else:
        prompt += f"СТРОГИЙ ЛИМИТ: Не более {MAX_CAPTION_LENGTH} символов."

    try:
        response = model.generate_content(prompt)
        text = clean_text(response.text)
        
        # 2. АВТО-КОРЕКЦІЯ (Якщо ШІ написав забагато)
        if task_type != "scenario" and len(text) > MAX_CAPTION_LENGTH:
            logging.info(f"⚠️ Текст задовгий ({len(text)}). Скорочую автоматично...")
            
            shorten_prompt = (
                f"Твой предыдущий текст получился слишком длинным ({len(text)} символов). "
                f"Сократи его до {MAX_CAPTION_LENGTH} символов, сохранив главный смысл и призыв к действию. "
                f"Текст для сокращения:\n{text}"
            )
            response_short = model.generate_content(shorten_prompt)
            text = clean_text(response_short.text)
            
        return text

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
async def prepare_draft(source_type, manual_date=None, from_command=False):
    date_now = manual_date if manual_date else get_kyiv_date()
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        # --- TELEGRAM ---
        if source_type in ['morning', 'day', 'evening']:
            table_name = "telegram_posts"
            cursor.execute(f"SELECT topic, content, photo_keywords FROM {table_name} WHERE publish_date = %s AND time_slot = %s", (date_now, source_type))
            result = cursor.fetchone()
            
            if result:
                topic, short_context, keywords = result
                photo_url = await get_random_photo(keywords)
                
                # Генеруємо текст (він вже буде нормальної довжини завдяки авто-корекції)
                final_text = await generate_ai_post(topic, short_context, "tg", task_type="post", time_slot=source_type)
                
                caption_header = f"✈️ TG ({source_type.upper()} | {date_now})\n\n"
                full_caption = caption_header + final_text
                
                # Страховка: якщо навіть після скорочення він > 1024 (малоймовірно, але можливо)
                if len(full_caption) > 1024:
                    full_caption = full_caption[:1020] + "..."

                builder = InlineKeyboardBuilder()
                builder.row(types.InlineKeyboardButton(text="✅ Опубликовать", callback_data="confirm_publish"))
                builder.row(
                    types.InlineKeyboardButton(text="🖼 Новое фото", callback_data=f"photo_{date_now}_{source_type}_tg"),
                    types.InlineKeyboardButton(text="📝 Новый текст", callback_data=f"text_{date_now}_{source_type}_tg_post")
                )
                
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=full_caption, reply_markup=builder.as_markup())
            elif from_command:
                await bot.send_message(ADMIN_ID, f"🤷‍♂️ TG: Пусто на {source_type} ({date_now})")

        # --- INSTAGRAM ---
        elif source_type == 'inst':
            table_name = "instagram_posts"
            cursor.execute(f"SELECT topic, content, post_type, photo_keywords FROM {table_name} WHERE publish_date = %s", (date_now,))
            result = cursor.fetchone()
            
            if result:
                topic, short_context, post_type, keywords = result
                
                if post_type == 'Карусель':
                    photo_url = "https://images.unsplash.com/photo-1611162617474-5b21e879e113?q=80&w=1000&auto=format&fit=crop"
                    prefix = "📸 INSTA CAROUSEL"
                else:
                    photo_url = await get_random_photo(keywords)
                    prefix = "📸 INSTA SINGLE"

                # Генеруємо опис (авто-корекція включена)
                final_caption = await generate_ai_post(topic, short_context, "inst", task_type="post")
                
                caption_header = f"{prefix} ({date_now})\n\n"
                full_caption = caption_header + final_caption
                
                if len(full_caption) > 1024:
                    full_caption = full_caption[:1020] + "..."

                builder_cap = InlineKeyboardBuilder()
                builder_cap.row(types.InlineKeyboardButton(text="📝 Переписать описание", callback_data=f"text_{date_now}_inst_inst_post"))
                if post_type == 'Single':
                     builder_cap.add(types.InlineKeyboardButton(text="🖼 Новое фото", callback_data=f"photo_{date_now}_inst_inst"))

                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=full_caption, reply_markup=builder_cap.as_markup())

                if post_type == 'Карусель':
                    scenario_text = await generate_ai_post(topic, short_context, "inst", task_type="scenario")
                    header = f"🛠 <b>СЦЕНАРИЙ ДЛЯ ДИЗАЙНЕРА ({date_now})</b>\n{'='*25}\n\n"
                    full_msg = header + scenario_text
                    
                    builder_scen = InlineKeyboardBuilder()
                    builder_scen.row(types.InlineKeyboardButton(text="🔄 Переписать сценарий", callback_data=f"text_{date_now}_inst_inst_scenario"))
                    
                    await bot.send_message(chat_id=ADMIN_ID, text=full_msg, parse_mode="HTML", reply_markup=builder_scen.as_markup())

            elif from_command:
                await bot.send_message(ADMIN_ID, f"🤷‍♂️ Insta: Пусто ({date_now})")

        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Ошибка ({source_type}): {e}{ERROR_SIGNATURE}", parse_mode="HTML")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        ua_date = get_kyiv_date()
        await message.answer(
            f"👋 Bot Updated (Auto-Correction)!\n📅 Сьогодні: {ua_date}\n"
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
    date_str, slot, plat = parts[1], parts[2], parts[3]
    await callback.answer("🔄...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        if plat == 'tg':
            cursor.execute("SELECT photo_keywords FROM telegram_posts WHERE publish_date=%s AND time_slot=%s", (date_str, slot))
        else:
            cursor.execute("SELECT photo_keywords FROM instagram_posts WHERE publish_date=%s", (date_str,))
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
    date_str, slot, plat, task_type = parts[1], parts[2], parts[3], parts[4]
    await callback.answer("📝 Думаю (Авто-корекція)...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        if plat == 'tg':
            cursor.execute("SELECT topic, content FROM telegram_posts WHERE publish_date=%s AND time_slot=%s", (date_str, slot))
            res = cursor.fetchone()
            if res:
                final_text = await generate_ai_post(res[0], res[1], "tg", task_type="post", time_slot=slot)
                new_cap = f"✈️ TG ({slot.upper()} | {date_str})\n\n{final_text}"
                await callback.message.edit_caption(caption=new_cap, reply_markup=callback.message.reply_markup)
        
        else: # INSTAGRAM
            cursor.execute("SELECT topic, content, post_type FROM instagram_posts WHERE publish_date=%s", (date_str,))
            res = cursor.fetchone()
            if res:
                final_text = await generate_ai_post(res[0], res[1], "inst", task_type=task_type)
                
                if task_type == "post":
                    prefix = "📸 INSTA SINGLE" if res[2] == 'Single' else "📸 INSTA CAROUSEL"
                    new_cap = f"{prefix} ({date_str})\n\n{final_text}"
                    await callback.message.edit_caption(caption=new_cap, reply_markup=callback.message.reply_markup)
                
                elif task_type == "scenario":
                    header = f"🛠 <b>СЦЕНАРИЙ ДЛЯ ДИЗАЙНЕРА ({date_str})</b>\n{'='*25}\n\n"
                    full_msg = header + final_text
                    await callback.message.edit_text(text=full_msg, parse_mode="HTML", reply_markup=callback.message.reply_markup)

        conn.close()
    except Exception as e: await callback.message.answer(f"Error: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish(callback: types.CallbackQuery):
    cap = callback.message.caption
    if "TG (" in cap:
        clean_cap = cap.split("\n\n", 1)[1] if "\n\n" in cap else cap
    else:
        clean_cap = cap
    await bot.send_photo(CHANNEL_ID, callback.message.photo[-1].file_id, caption=clean_cap)
    await callback.message.edit_caption(caption=f"✅ POSTED\n\n{clean_cap}")

# --- WEB SERVER ---
async def handle(request): return web.Response(text="I am alive")

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
    scheduler.add_job(prepare_draft, 'cron', hour=13, minute=50, args=['inst'])
    scheduler.start()
    
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())