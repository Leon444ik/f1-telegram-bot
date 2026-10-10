import asyncio
import aiohttp
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from deep_translator import GoogleTranslator
from aiohttp import web

# Получаем токен из переменных окружения Render
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализируем онлайн-переводчик
translator = GoogleTranslator(source='en', target='ru')

# Множество для хранения ID отправленных инцидентов
sent_incidents = set()

# --- 1. БАЗА ДАННЫХ SQLite ---
def init_db():
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT
        )
    """)
    # Таблица личного зачёта пилотов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS driver_standings (
            position INTEGER PRIMARY KEY,
            name TEXT UNIQUE,
            team TEXT,
            points REAL
        )
    """)
    # Таблица Кубка конструкторов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS constructor_standings (
            position INTEGER PRIMARY KEY,
            team_name TEXT UNIQUE,
            points REAL
        )
    """)
    conn.commit()
    conn.close()

def add_user(user_id, username, first_name):
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username, first_name) 
        VALUES (?, ?, ?)
    """, (user_id, username, first_name))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()
    return [u[0] for u in users]

# --- 2. УПРАВЛЕНИЕ ЗАЧЁТОМ В БД ---
def update_driver_points(name_query: str, points: float):
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM driver_standings WHERE name LIKE ?", (f"%{name_query}%",))
    row = cursor.fetchone()
    if row:
        real_name = row[0]
        cursor.execute("UPDATE driver_standings SET points = ? WHERE name = ?", (points, real_name))
    else:
        cursor.execute("INSERT INTO driver_standings (name, team, points) VALUES (?, ?, ?)", (name_query, "F1 Team", points))
    
    # Пересчитываем позиции по очкам
    cursor.execute("SELECT id, points FROM driver_standings ORDER BY points DESC")
    conn.commit()
    conn.close()

def get_db_driver_standings():
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, team, points FROM driver_standings ORDER BY points DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_db_constructor_standings():
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT team_name, points FROM constructor_standings ORDER BY points DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- 3. МЕНЮ И КЛАВИАТУРЫ ---
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📊 Личный зачёт")
    builder.button(text="🏎️ Кубок конструкторов")
    builder.button(text="🏁 Ближайший уик-энд")
    builder.adjust(2, 1)
    return builder.as_markup(resize_keyboard=True)

def get_refresh_inline_keyboard(action: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить данные", callback_data=f"refresh_{action}")
    return builder.as_markup()

# --- 4. УМНЫЙ ПЕРЕВОДЧИК СООБЩЕНИЙ FIA ---
def translate_f1_message(text: str) -> str:
    if not text:
        return ""
    t = text.upper().strip()
    if "DOUBLE YELLOW IN" in t:
        sector = t.split("IN")[-1].replace("TRACK", "").strip()
        return f"Двойные жёлтые флаги ({sector})"
    if "YELLOW IN" in t:
        sector = t.split("IN")[-1].replace("TRACK", "").strip()
        return f"Жёлтые флаги ({sector})"
    if "GREEN IN" in t or "GREEN FLAG" in t:
        return "Зелёные флаги — трасса свободна"
    if "SAFETY CAR DEPLOYED" in t:
        return "Выведена машина безопасности (Safety Car)"
    if "VIRTUAL SAFETY CAR DEPLOYED" in t:
        return "Введён режим виртуальной машины безопасности (VSC)"
    if "SAFETY CAR IN THIS LAP" in t:
        return "Машина безопасности уходит на этом круге"
    if "RED FLAG" in t:
        return "Красные флаги! Сессия остановлена"
    if "DRS ENABLED" in t:
        return "Система DRS разрешена"
    if "DRS DISABLED" in t:
        return "Система DRS запрещена"
    if "INCIDENT INVOLVING CAR" in t and "NOTED" in t:
        car = t.split("CAR")[-1].split("NOTED")[0].strip()
        return f"Зафиксирован инцидент с машиной #{car}"
    if "INCIDENT INVOLVING CAR" in t and "UNDER INVESTIGATION" in t:
        car = t.split("CAR")[-1].split("UNDER INVESTIGATION")[0].strip()
        return f"Инцидент с машиной #{car} расследуется стюардами"
    return text

# --- 5. ПОЛУЧЕНИЕ ДАННЫХ (БД + API) ---
async def get_driver_standings():
    db_rows = get_db_driver_standings()
    if db_rows:
        text = "📊 **Личный зачёт пилотов:**\n\n"
        for idx, (name, team, points) in enumerate(db_rows[:15], 1):
            pts_str = int(points) if points == int(points) else points
            text += f"{idx}. **{name}** ({team}) — {pts_str} очк.\n"
        return text

    # Фоллбэк на API, если БД пустая
    url = "https://api.jolpi.ca/ergast/f1/current/driverstandings.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    standings_list = data['MRData']['StandingsTable']['StandingsLists']
                    if not standings_list:
                        return "⚠️ Данные о личном зачёте недоступны."
                    
                    standings = standings_list[0]['DriverStandings']
                    text = "📊 **Личный зачёт пилотов (API):**\n\n"
                    for driver in standings[:10]:
                        pos = driver['position']
                        name = f"{driver['Driver']['givenName']} {driver['Driver']['familyName']}"
                        points = driver['points']
                        team = driver['Constructors'][0]['name'] if driver.get('Constructors') else ""
                        text += f"{pos}. **{name}** ({team}) — {points} очк.\n"
                    return text
                return "⚠️ Не удалось получить данные о зачёте пилотов."
    except Exception as e:
        print(f"Ошибка API (Drivers): {e}")
        return "⚠️ Произошла ошибка при загрузке данных."

async def get_constructor_standings():
    db_rows = get_db_constructor_standings()
    if db_rows:
        text = "🏎️ **Кубок конструкторов:**\n\n"
        for idx, (team, points) in enumerate(db_rows, 1):
            pts_str = int(points) if points == int(points) else points
            text += f"{idx}. **{team}** — {pts_str} очк.\n"
        return text

    url = "https://api.jolpi.ca/ergast/f1/current/constructorstandings.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    standings_list = data['MRData']['StandingsTable']['StandingsLists']
                    if not standings_list:
                        return "⚠️ Данные о Кубке конструкторов недоступны."
                    
                    standings = standings_list[0]['ConstructorStandings']
                    text = "🏎️ **Кубок конструкторов (API):**\n\n"
                    for team in standings:
                        pos = team['position']
                        name = team['Constructor']['name']
                        points = team['points']
                        text += f"{pos}. **{name}** — {points} очк.\n"
                    return text
                return "⚠️ Не удалось получить данные от сервера."
    except Exception as e:
        print(f"Ошибка API (Constructors): {e}")
        return "⚠️ Произошла ошибка при загрузке данных."

async def get_full_weekend_schedule():
    url = "https://api.jolpi.ca/ergast/f1/current/next.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    races = data['MRData']['RaceTable']['Races']
                    if not races:
                        return "🏁 Сезон завершен или нет информации о ближайшем Гран-при."
                    
                    race = races[0]
                    race_name = race['raceName']
                    circuit = race['Circuit']['circuitName']
                    locality = race['Circuit']['Location']['locality']
                    country = race['Circuit']['Location']['country']
                    
                    def to_msk(date_str, time_str):
                        if not date_str or not time_str:
                            return "Не указано"
                        t_str = time_str.replace('Z', '')
                        utc_dt = datetime.strptime(f"{date_str} {t_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        msk_dt = utc_dt.astimezone(timezone(timedelta(hours=3)))
                        return msk_dt.strftime("%d.%m в %H:%M")

                    text = f"🏁 **{race_name}**\n📍 Трасса: {circuit} ({locality}, {country})\n\n"
                    text += "⏱️ **Расписание сессий (Время МСК):**\n"
                    if 'FirstPractice' in race:
                        text += f"🏎️ Практика 1: **{to_msk(race['FirstPractice']['date'], race['FirstPractice'].get('time'))}**\n"
                    if 'SecondPractice' in race:
                        text += f"🏎️ Практика 2: **{to_msk(race['SecondPractice']['date'], race['SecondPractice'].get('time'))}**\n"
                    if 'ThirdPractice' in race:
                        text += f"🏎️ Практика 3: **{to_msk(race['ThirdPractice']['date'], race['ThirdPractice'].get('time'))}**\n"
                    if 'Qualifying' in race:
                        text += f"⏱️ Квалификация: **{to_msk(race['Qualifying']['date'], race['Qualifying'].get('time'))}**\n"
                    text += f"🏆 **Гонка:** **{to_msk(race['date'], race.get('time'))}**\n"
                    return text
                return "⚠️ Не удалось получить расписание."
    except Exception as e:
        print(f"Ошибка API (Schedule): {e}")
        return "⚠️ Произошла ошибка при загрузке расписания."

# --- 6. ТРЕКЕР ПРОИСШЕСТВИЙ С АВТОПЕРЕВОДОМ ---
async def track_live_incidents():
    url = "https://api.openf1.org/v1/race_control?session_key=latest"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        messages = await response.json()
                        for msg in messages[-5:]:
                            msg_id = f"{msg.get('date')}_{msg.get('message')}"
                            if msg_id not in sent_incidents:
                                category = msg.get('category', '').upper()
                                flag = msg.get('flag', '')
                                raw_message = msg.get('message', '')
                                
                                if category in ['FLAG', 'SAFETY CAR', 'DRS', 'INCIDENT'] or flag in ['YELLOW', 'RED', 'SAFETY CAR', 'VIRTUAL SAFETY CAR']:
                                    icon = "🚨"
                                    if flag == "YELLOW":
                                        icon = "🟨"
                                    elif flag == "RED":
                                        icon = "🟥"
                                    elif "SAFETY CAR" in raw_message.upper():
                                        icon = "🏎️💨"

                                    translated_msg = translate_f1_message(raw_message)

                                    alert_text = (
                                        f"{icon} **ОПОВЕЩЕНИЕ ДИРЕКЦИИ ГОНКИ**\n\n"
                                        f"🇷🇺 **Событие:** {translated_msg}\n"
                                        f"🇬🇧 _Оригинал: {raw_message}_"
                                    )
                                    
                                    users = get_all_users()
                                    for user_id in users:
                                        try:
                                            await bot.send_message(user_id, alert_text, parse_mode="Markdown")
                                        except Exception:
                                            pass
                                    sent_incidents.add(msg_id)
        except Exception as e:
            print(f"Ошибка фонового трекера: {e}")
            
        await asyncio.sleep(20)

# --- 7. ОБРАБОТЧИКИ TELEGRAM И АДМИН-КОМАНДЫ ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"Привет, {message.from_user.first_name}! 🏎️\n"
        "Добро пожаловать в бот Формулы-1.\n"
        "Бот автоматически отслеживает происшествия на трассе и показывает результаты!",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("set_driver"))
async def cmd_set_driver(message: types.Message):
    # Команда формата: /set_driver Имя Очки
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("⚠️ Формат: `/set_driver Имя Очки`\nПример: `/set_driver Antonelli 325`", parse_mode="Markdown")
        return
    name = args[1]
    try:
        pts = float(args[2])
        update_driver_points(name, pts)
        await message.answer(f"✅ Очки пилота **{name}** успешно обновлены до **{pts}**!", parse_mode="Markdown")
    except ValueError:
        await message.answer("⚠️ Очки должны быть числом!")

@dp.message(lambda message: message.text == "📊 Личный зачёт")
async def btn_driver_standings(message: types.Message):
    await message.answer("🔄 Загружаю зачёт пилотов...")
    text = await get_driver_standings()
    await message.answer(text, parse_mode="Markdown", reply_markup=get_refresh_inline_keyboard("drivers"))

@dp.message(lambda message: message.text == "🏎️ Кубок конструкторов")
async def btn_constructor_standings(message: types.Message):
    await message.answer("🔄 Загружаю Кубок конструкторов...")
    text = await get_constructor_standings()
    await message.answer(text, parse_mode="Markdown", reply_markup=get_refresh_inline_keyboard("constructors"))

@dp.message(lambda message: message.text == "🏁 Ближайший уик-энд")
async def btn_schedule(message: types.Message):
    await message.answer("🔄 Загружаю расписание уик-энда...")
    text = await get_full_weekend_schedule()
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data.startswith("refresh_"))
async def process_refresh(callback: types.CallbackQuery):
    await callback.answer("Обновляю данные...")
    action = callback.data.split("_")[1]
    
    if action == "drivers":
        new_text = await get_driver_standings()
    elif action == "constructors":
        new_text = await get_constructor_standings()
    else:
        return

    try:
        await callback.message.edit_text(
            new_text, 
            parse_mode="Markdown", 
            reply_markup=get_refresh_inline_keyboard(action)
        )
    except Exception:
        pass

# --- 8. ВЕБ-СЕРВЕР ДЛЯ РЕНДЕРА (СЛУШАЕТ ПОРТ) ---
async def handle_ping(request):
    return web.Response(text="F1 Telegram Bot is alive and running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}")

# --- 9. ГЛАВНЫЙ ЗАПУСК ---
async def main():
    init_db()
    await start_web_server()
    asyncio.create_task(track_live_incidents())
    print("Бот и веб-сервер успешно запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())