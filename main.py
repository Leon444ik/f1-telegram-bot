import asyncio
import aiohttp
import sqlite3
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from deep_translator import GoogleTranslator

import os

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализируем онлайн-переводчик
translator = GoogleTranslator(source='en', target='ru')

# Множество для хранения ID отправленных инцидентов
sent_incidents = set()

# Кэш переводов, чтобы не отправлять повторные запросы в Google
translation_cache = {}

# Быстрый локальный словарь основных флагов и команд FIA
F1_DICTIONARY = {
    "SAFETY CAR DEPLOYED": "Выведена машина безопасности (Safety Car)",
    "VIRTUAL SAFETY CAR DEPLOYED": "Введён режим виртуальной машины безопасности (VSC)",
    "SAFETY CAR IN THIS LAP": "Машина безопасности уходит на этом круге",
    "VIRTUAL SAFETY CAR ENDING": "Режим VSC завершается",
    "YELLOW FLAG": "Жёлтые флаги на трассе",
    "RED FLAG": "Красные флаги (Сессия остановлена)",
    "GREEN FLAG": "Зелёные флаги (Трасса чиста)",
    "DRS ENABLED": "Система DRS разрешена",
    "DRS DISABLED": "Система DRS запрещена",
    "CHEQUERED FLAG": "Клетчатый флаг (Финиш)",
}

# --- 1. БАЗА ДАННЫХ SQLite ---
def init_db():
    conn = sqlite3.connect("f1_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT
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

# --- 2. МЕНЮ И КЛАВИАТУРЫ ---
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

# --- 3. УМНЫЙ ПЕРЕВОДЧИК СООБЩЕНИЙ FIA ---
def translate_f1_message(text: str) -> str:
    """Мгновенный и надежный переводчик сообщений FIA без зависимости от внешних серверов"""
    if not text:
        return ""
    
    t = text.upper().strip()
    
    # 1. Желтые и двойные желтые флаги по секторам
    if "DOUBLE YELLOW IN" in t:
        sector = t.split("IN")[-1].replace("TRACK", "").strip()
        return f"Двойные жёлтые флаги ({sector})"
    if "YELLOW IN" in t:
        sector = t.split("IN")[-1].replace("TRACK", "").strip()
        return f"Жёлтые флаги ({sector})"
        
    # 2. Зеленые флаги
    if "GREEN IN" in t or "GREEN FLAG" in t:
        return "Зелёные флаги — трасса свободна"
        
    # 3. Базовые события
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
        
    # 4. Расследования и инциденты
    if "INCIDENT INVOLVING CAR" in t and "NOTED" in t:
        car = t.split("CAR")[-1].split("NOTED")[0].strip()
        return f"Зафиксирован инцидент с машиной #{car}"
    if "INCIDENT INVOLVING CAR" in t and "UNDER INVESTIGATION" in t:
        car = t.split("CAR")[-1].split("UNDER INVESTIGATION")[0].strip()
        return f"Инцидент с машиной #{car} расследуется стюардами"
        
    # Если попалось редкое сообщение, выводим понятную структурированную форму
    return text

async def get_constructor_standings():
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
                    text = "🏎️ **Кубок конструкторов:**\n\n"
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

# --- 5. ТРЕКЕР ПРОИСШЕСТВИЙ С АВТОПЕРЕВОДОМ (OpenF1 API) ---
async def track_live_incidents():
    """Отслеживает происшествия и переводит сообщения FIA на русский язык"""
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

# --- 6. ОБРАБОТЧИКИ TELEGRAM ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"Привет, {message.from_user.first_name}! 🏎️\n"
        "Добро пожаловать в бот Формулы-1.\n"
        "Бот автоматически отслеживает происшествия на трассе и переводит их на русский язык!",
        reply_markup=get_main_keyboard()
    )

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

async def main():
    init_db()
    asyncio.create_task(track_live_incidents())
    print("Бот успешно запущен (с защитой перевода от ошибок)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main()) 