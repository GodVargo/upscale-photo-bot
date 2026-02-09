"""
Upscaler Photo Bot — Telegram-бот для апскейла фото с помощью AI
С HTTP API для WebApp и PostgreSQL для хранения пользователей
"""
import asyncio
import logging
import os
import csv
import io
import json
import base64
from datetime import datetime, timedelta
from dotenv import load_dotenv

import aiohttp
from aiohttp import web
import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, BufferedInputFile, WebAppData
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://godvargo.github.io/upscale-photo-webapp/")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
DEEPAI_API_KEY = os.getenv("DEEPAI_API_KEY", "463910db-7f7d-4bc2-9f3d-76dfbc8038d5")
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============ DATABASE ============

def get_db():
    """Подключение к PostgreSQL"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Создание таблицы пользователей"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            first_name VARCHAR(255),
            joined TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active BOOLEAN DEFAULT TRUE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("База данных инициализирована")


def add_user(user_id: int, username: str = None, first_name: str = None):
    """Добавление нового пользователя"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (id, username, first_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name,
            active = TRUE
    """, (user_id, username, first_name))
    conn.commit()
    cur.close()
    conn.close()


def get_all_user_ids():
    """Получение всех ID активных пользователей"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE active = TRUE")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [row['id'] for row in rows]


def mark_inactive(user_id: int):
    """Пометить пользователя как неактивного"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET active = FALSE WHERE id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()


def get_stats():
    """Получение статистики"""
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) as total FROM users")
    total = cur.fetchone()['total']
    
    cur.execute("SELECT COUNT(*) as active FROM users WHERE active = TRUE")
    active = cur.fetchone()['active']
    
    day_ago = datetime.now() - timedelta(hours=24)
    cur.execute("SELECT COUNT(*) as new_24h FROM users WHERE joined > %s", (day_ago,))
    new_24h = cur.fetchone()['new_24h']
    
    cur.close()
    conn.close()
    
    return {"total": total, "new_24h": new_24h, "active": active}


def export_users():
    """Экспорт всех пользователей"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, first_name, joined, active FROM users ORDER BY joined DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ============ HTTP API для WebApp ============

async def handle_upscale(request):
    """API endpoint для апскейла изображения через DeepAI"""
    logger.info("📥 Получен запрос на апскейл")
    
    try:
        # Читаем multipart данные
        reader = await request.multipart()
        image_data = None
        
        async for part in reader:
            if part.name == 'image':
                image_data = await part.read()
                logger.info(f"📁 Получено изображение: {len(image_data)} байт")
        
        if not image_data:
            return web.json_response({'error': 'No image provided'}, status=400)
        
        # Отправляем на DeepAI
        logger.info("🚀 Отправляем на DeepAI...")
        
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field('image', image_data, filename='image.jpg', content_type='image/jpeg')
            
            async with session.post(
                'https://api.deepai.org/api/waifu2x',
                data=form,
                headers={'api-key': DEEPAI_API_KEY}
            ) as resp:
                result = await resp.json()
                logger.info(f"📦 Ответ DeepAI: {result}")
                
                if 'output_url' in result:
                    # Скачиваем результат и возвращаем как base64
                    async with session.get(result['output_url']) as img_resp:
                        img_bytes = await img_resp.read()
                        img_base64 = base64.b64encode(img_bytes).decode('utf-8')
                        
                        return web.json_response({
                            'success': True,
                            'output_url': result['output_url'],
                            'image_base64': f"data:image/png;base64,{img_base64}"
                        })
                else:
                    return web.json_response({
                        'success': False,
                        'error': result.get('err', 'Unknown error')
                    }, status=500)
    
    except Exception as e:
        logger.error(f"❌ Ошибка апскейла: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def handle_health(request):
    """Health check endpoint"""
    return web.json_response({'status': 'ok'})


async def handle_cors_preflight(request):
    """Handle CORS preflight requests"""
    return web.Response(
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        }
    )


@web.middleware
async def cors_middleware(request, handler):
    """Middleware для CORS"""
    if request.method == 'OPTIONS':
        return await handle_cors_preflight(request)
    
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ============ BOT HANDLERS ============

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🖼️ Улучшить фото",
        web_app=WebAppInfo(url=WEBAPP_URL)
    )
    
    await message.answer(
        "🖼️ <b>Upscaler Photo</b>\n\n"
        "Telegram-бот для апскейла и улучшения фотографий с помощью искусственного интеллекта.\n\n"
        "📌 <b>Возможности:</b>\n"
        "• Увеличение разрешения 2x / 4x\n"
        "• Улучшение чёткости и деталей\n"
        "• Удаление шумов\n"
        "• Работа с любыми фото\n\n"
        "Просто нажмите кнопку ниже — остальное сделает ИИ.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    await message.answer(
        "📖 <b>Помощь</b>\n\n"
        "/start — Открыть апскейлер\n"
        "/help — Справка\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите кнопку «Улучшить фото»\n"
        "2. Загрузите изображение\n"
        "3. Выберите масштаб (2x или 4x)\n"
        "4. Нажмите «Улучшить»\n"
        "5. Нажмите «Отправить в чат» — получите файл!",
        parse_mode="HTML"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика (только для админа)"""
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return
    
    stats = get_stats()
    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего в базе: <b>{stats['total']}</b>\n"
        f"📈 Новых за 24 часа: <b>{stats['new_24h']}</b>\n"
        f"✅ Активных: <b>{stats['active']}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("export"))
async def cmd_export(message: Message):
    """Экспорт базы пользователей (только для админа)"""
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return
    
    try:
        users = export_users()
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'Username', 'Name', 'Joined', 'Active'])
        for user in users:
            writer.writerow([
                user['id'],
                user['username'] or '',
                user['first_name'] or '',
                user['joined'],
                user['active']
            ])
        
        csv_bytes = output.getvalue().encode('utf-8')
        file = BufferedInputFile(csv_bytes, filename=f"users_{datetime.now().strftime('%Y%m%d')}.csv")
        await message.answer_document(file, caption=f"📁 База пользователей ({len(users)} записей)")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Рассылка сообщений (только для админа)"""
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return
    
    text = message.text.replace("/broadcast", "").strip()
    
    if not text:
        await message.answer(
            "📢 <b>Рассылка</b>\n\n"
            "Использование:\n"
            "<code>/broadcast Ваше сообщение</code>",
            parse_mode="HTML"
        )
        return
    
    user_ids = get_all_user_ids()
    sent = 0
    failed = 0
    
    status_msg = await message.answer(f"📤 Рассылка... 0/{len(user_ids)}")
    
    for i, user_id in enumerate(user_ids):
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                mark_inactive(user_id)
        
        if (i + 1) % 20 == 0:
            await status_msg.edit_text(f"📤 Рассылка... {i+1}/{len(user_ids)}")
        
        await asyncio.sleep(0.05)
    
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}",
        parse_mode="HTML"
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    """Обработчик фото — предлагает открыть WebApp"""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🖼️ Открыть апскейлер",
        web_app=WebAppInfo(url=WEBAPP_URL)
    )
    await message.answer(
        "📸 Для улучшения фото используйте наш апскейлер.\n"
        "Нажмите кнопку ниже:",
        reply_markup=builder.as_markup()
    )


@dp.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    """Обработчик данных от WebApp — отправляет результат пользователю"""
    logger.info(f"📥 Получены данные от WebApp: {message.web_app_data.data[:100]}...")
    
    try:
        data = json.loads(message.web_app_data.data)
        
        if data.get('action') == 'send_result':
            # Получаем base64 изображение
            image_base64 = data.get('image', '')
            
            if image_base64.startswith('data:image'):
                # Убираем префикс data:image/png;base64,
                image_base64 = image_base64.split(',')[1]
            
            image_bytes = base64.b64decode(image_base64)
            
            # Отправляем как документ
            file = BufferedInputFile(
                image_bytes, 
                filename=f"upscaled_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
            
            await message.answer_document(
                file,
                caption="✅ Вот ваше улучшенное изображение!"
            )
            logger.info("✅ Изображение отправлено пользователю")
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки WebApp данных: {e}")
        await message.answer("❌ Ошибка при обработке изображения. Попробуйте ещё раз.")


async def run_bot():
    """Запуск бота"""
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def run_server():
    """Запуск HTTP API сервера"""
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post('/upscale', handle_upscale)
    app.router.add_get('/health', handle_health)
    app.router.add_options('/upscale', handle_cors_preflight)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 HTTP API запущен на порту {PORT}")


async def main():
    """Запуск бота и HTTP сервера"""
    init_db()
    logger.info("🚀 Запуск Upscaler Photo Bot...")
    
    # Запускаем оба: бота и HTTP сервер
    await asyncio.gather(
        run_bot(),
        run_server()
    )


if __name__ == "__main__":
    asyncio.run(main())
