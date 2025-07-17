import os
import logging
import time
import uuid
import sqlite3
import ccxt
import requests
import openai
import random
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)

# === НАСТРОЙКИ ===
TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
OPENAI_API_KEY = "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"
openai.api_key = OPENAI_API_KEY

BUDGET_FUTURES = 500  # для фьючерсов
BUDGET_SPOT = 3000    # для спота

FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "MATIC/USDT",
    "SHIB/USDT", "DOT/USDT", "OP/USDT", "TON/USDT", "ARBUSDT",
    "SEI/USDT", "SUI/USDT", "LTC/USDT", "BCH/USDT", "INJ/USDT"
]
SPOT_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "MATIC/USDT",
    "SHIB/USDT", "DOT/USDT", "OP/USDT", "TON/USDT", "ARBUSDT",
    "SEI/USDT", "SUI/USDT", "LTC/USDT", "BCH/USDT", "INJ/USDT"
]

ADMIN_ID = 1407143951

# === ЛОГИ ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === БД ДЛЯ ТОКЕНОВ ===
conn = sqlite3.connect("access_tokens.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER UNIQUE,
    username TEXT,
    activation_time TIMESTAMP
)
""")
conn.commit()

# === EXCHANGE INIT ===
binance = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# === ACCESS ===
def user_has_access(user_id):
    cursor.execute("SELECT user_id FROM tokens WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

async def generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    token = str(uuid.uuid4())
    cursor.execute("INSERT INTO tokens (token) VALUES (?)", (token,))
    conn.commit()
    await update.message.reply_text(f"✅ Новый токен: {token}")

async def activate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    username = update.message.chat.username
    if len(context.args) != 1:
        await update.message.reply_text("❌ Используй: /activate <токен>")
        return
    token = context.args[0]
    cursor.execute("SELECT token FROM tokens WHERE token = ? AND user_id IS NULL", (token,))
    if cursor.fetchone():
        activation_time = time.time()
        cursor.execute("UPDATE tokens SET user_id = ?, username = ?, activation_time = ? WHERE token = ?", (user_id, username, activation_time, token))
        conn.commit()
        await update.message.reply_text("✅ Токен активирован!")
        await main_menu(update, context)
    else:
        await update.message.reply_text("❌ Неверный или уже использованный токен.")

# === МЕНЮ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_has_access(user_id):
        await main_menu(update, context)
    else:
        await update.message.reply_text("❌ Нет доступа. Активируйте токен командой /activate <токен>.")

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🟢 Рекомендации для СПОТ", callback_data="spot_recommend")],
        [InlineKeyboardButton("💎 20 пар на ФЬЮЧЕРСАХ", callback_data="futures_manual")],
        [InlineKeyboardButton("⚡ Автопоиск (фьючерсы)", callback_data="futures_auto")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text("<b>Выбери режим:</b>", reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text("<b>Выбери режим:</b>", reply_markup=reply_markup, parse_mode="HTML")

# === CALLBACK ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.message.chat.id

    if data == "spot_recommend":
        await send_spot_signal(user_id, context)
    elif data == "futures_manual":
        await choose_futures_pair(user_id, query)
    elif data == "futures_auto":
        await start_auto_futures(user_id, context)
    elif data.startswith("futures_pair_"):
        pair = data.split("futures_pair_")[1]
        await send_futures_signal(user_id, pair, context)
    elif data == "main_menu":
        await main_menu(update, context)
    elif data == "stop_auto":
        context.user_data['auto'] = False
        await query.edit_message_text("Автопоиск остановлен.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

async def choose_futures_pair(user_id, query):
    keyboard = [
        [InlineKeyboardButton(pair, callback_data=f"futures_pair_{pair}")]
        for pair in FUTURES_PAIRS
    ]
    # По 2 кнопки в ряд
    buttons = [keyboard[i:i+2] for i in range(0, len(keyboard), 2)]
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("Выбери пару для сигнала:", reply_markup=markup)

# === SPOT SIGNAL ===
async def send_spot_signal(user_id, context):
    pair = random.choice(SPOT_PAIRS)
    signal = analyze_pair(pair, budget=BUDGET_SPOT, mode="spot")
    comment = get_ai_comment(pair, signal)
    msg = make_signal_message(signal, pair, "СПОТ", comment)
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

# === FUTURES SIGNAL ===
async def send_futures_signal(user_id, pair, context):
    signal = analyze_pair(pair, budget=BUDGET_FUTURES, mode="futures")
    comment = get_ai_comment(pair, signal)
    msg = make_signal_message(signal, pair, "ФЬЮЧЕРСЫ", comment)
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

# === AUTO FUTURES ===
async def start_auto_futures(user_id, context):
    context.user_data['auto'] = True
    await context.bot.send_message(chat_id=user_id, text="Автоматический подбор топовых сигналов включен!\nОстановить: /stop или кнопка ниже.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Остановить автопоиск", callback_data="stop_auto")], [InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
    await auto_futures_signals(user_id, context)

async def auto_futures_signals(user_id, context):
    # ищем лучшие входы по всем парам раз в час
    while context.user_data.get('auto', False):
        top_signals = []
        for pair in FUTURES_PAIRS:
            signal = analyze_pair(pair, budget=BUDGET_FUTURES, mode="futures")
            if signal["quality"] > 7.5:  # только хорошие сигналы
                top_signals.append((pair, signal))
        top_signals.sort(key=lambda x: x[1]["quality"], reverse=True)
        # отправляем топ-1 или топ-2
        for pair, signal in top_signals[:2]:
            comment = get_ai_comment(pair, signal)
            msg = make_signal_message(signal, pair, "ФЬЮЧЕРСЫ (Auto)", comment)
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
        # ждать час
        await asyncio.sleep(3600)

# === СИГНАЛ-ГЕНЕРАТОР ===
def analyze_pair(pair, budget, mode="futures"):
    # Получаем цену (упрощённо)
    try:
        ticker = binance.fetch_ticker(pair)
        price = float(ticker["last"])
        spread = abs(price - float(ticker["open"]))
        # Пример: расчёт плеча, стопа, тейка
        if mode == "futures":
            leverage = 5 if "BTC" in pair or "ETH" in pair else 10
            direction = "LONG" if spread > 0 else "SHORT"
            stop = round(price * (0.96 if direction == "LONG" else 1.04), 3)
            take = round(price * (1.03 if direction == "LONG" else 0.97), 3)
        else:
            leverage = 1
            direction = "BUY"
            stop = round(price * 0.98, 3)
            take = round(price * 1.01, 3)
        # Фиктивная оценка качества по волатильности
        quality = min(10, max(0, (spread / price) * 120))
        return {
            "price": price, "direction": direction, "stop": stop,
            "take": take, "leverage": leverage, "quality": quality,
            "budget": budget
        }
    except Exception as e:
        return {
            "price": 0, "direction": "NONE", "stop": 0, "take": 0, "leverage": 1, "quality": 0, "budget": budget
        }

# === AI-КОММЕНТАРИЙ ===
def get_ai_comment(pair, signal):
    prompt = f"""
Ты трейдер. Проанализируй и прокомментируй сигнал для {pair} на {signal['direction']}:
- Цена: {signal['price']}
- Плечо: {signal['leverage']}
- Стоп: {signal['stop']}
- Тейк: {signal['take']}
- Качество: {signal['quality']}
- Бюджет: {signal['budget']}
Напиши, почему этот сигнал может быть хорошим или плохим, укажи сильные и слабые стороны, оцени вероятность успеха.
"""
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Ты опытный криптотрейдер."}, {"role": "user", "content": prompt}],
            max_tokens=90,
            temperature=0.5
        )
        return response.choices[0].message["content"]
    except Exception:
        return "Комментарий временно недоступен."

# === ФОРМАТ СИГНАЛА ===
def make_signal_message(signal, pair, typ, comment):
    return (f"<b>⚡ {typ} ({pair})</b>\n"
            f"Плечо: <b>{signal['leverage']}</b>\n"
            f"Вход: <b>{signal['price']}</b>\n"
            f"Тейк: <b>{signal['take']}</b> | Стоп: <b>{signal['stop']}</b>\n"
            f"Направление: <b>{signal['direction']}</b>\n"
            f"Качество сигнала: <b>{signal['quality']:.1f}/10</b>\n"
            f"<i>{comment}</i>")

# === MAIN ===
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("generate_token", generate_token))
    app.add_handler(CommandHandler("activate", activate_token))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    import asyncio
    main()
