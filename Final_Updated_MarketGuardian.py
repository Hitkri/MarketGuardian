import os
import logging
import time
import uuid
import sqlite3
import ccxt
import random
import requests
import openai
import asyncio
import pandas as pd
import numpy as np
from ta.volatility import AverageTrueRange
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)

# === НАСТРОЙКИ ===
TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
OPENAI_API_KEY = "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"
openai.api_key = OPENAI_API_KEY

BUDGET_FUTURES = 500
BUDGET_SPOT = 3000

FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "MATIC/USDT",
    "SHIB/USDT", "DOT/USDT", "OP/USDT", "TON/USDT", "ARB/USDT",
    "SEI/USDT", "SUI/USDT", "LTC/USDT", "BCH/USDT", "INJ/USDT"
]
SPOT_PAIRS = FUTURES_PAIRS

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
        [InlineKeyboardButton(pair, callback_data=f"futures_pair_{pair}")] for pair in FUTURES_PAIRS
    ]
    # По 2 кнопки в ряд
    buttons = [keyboard[i:i+2] for i in range(0, len(keyboard), 2)]
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("Выбери пару для сигнала:", reply_markup=markup)

# === SPOT SIGNAL ===
async def send_spot_signal(user_id, context):
    pair = random.choice(SPOT_PAIRS)
    signal = await analyze_pair(pair, budget=BUDGET_SPOT, mode="spot")
    msg = make_signal_message(signal, pair, "СПОТ")
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

# === FUTURES SIGNAL ===
async def send_futures_signal(user_id, pair, context):
    signal = await analyze_pair(pair, budget=BUDGET_FUTURES, mode="futures")
    msg = make_signal_message(signal, pair, "ФЬЮЧЕРСЫ")
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

# === AUTO FUTURES ===
async def start_auto_futures(user_id, context):
    context.user_data['auto'] = True
    await context.bot.send_message(chat_id=user_id, text="Автоматический подбор топовых сигналов включен!\nОстановить: /stop или кнопка ниже.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Остановить автопоиск", callback_data="stop_auto")], [InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
    asyncio.create_task(auto_futures_signals(user_id, context))

async def auto_futures_signals(user_id, context):
    while context.user_data.get('auto', False):
        best_signals = []
        for pair in FUTURES_PAIRS:
            signal = await analyze_pair(pair, budget=BUDGET_FUTURES, mode="futures")
            if signal["signal"]:
                best_signals.append((pair, signal))
        best_signals.sort(key=lambda x: x[1]["quality"], reverse=True)
        for pair, signal in best_signals[:2]:
            msg = make_signal_message(signal, pair, "ФЬЮЧЕРСЫ (Auto)")
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
        await asyncio.sleep(3600)  # раз в час

# === ANALYZE (REAL TA) ===
async def analyze_pair(pair, budget, mode="futures"):
    try:
        ohlcv = binance.fetch_ohlcv(pair, '1h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
        price = float(df.close.iloc[-1])

        # Индикаторы
        atr = AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        ema200 = EMAIndicator(df['close'], window=200, fillna=True).ema_indicator().iloc[-1]
        rsi = RSIIndicator(df['close'], window=14).rsi().iloc[-1]
        avg_vol = df['vol'].rolling(window=14).mean().iloc[-1]
        curr_vol = df['vol'].iloc[-1]

        # Логика
        signal = None
        direction = None
        leverage = 5
        quality = 0

        # Тренд
        if price > ema200 and rsi < 65:
            direction = "LONG"
            stop = price - 1.2 * atr
            take = price + 2.2 * atr
            signal = True
        elif price < ema200 and rsi > 35:
            direction = "SHORT"
            stop = price + 1.2 * atr
            take = price - 2.2 * atr
            signal = True
        else:
            signal = False

        # Объемы и фильтры
        if curr_vol < avg_vol * 0.7:
            signal = False

        # Качество
        volatility = (atr / price) * 100
        quality = min(10, max(0, volatility + (1 if signal else 0)))

        return {
            "price": round(price, 4),
            "direction": direction if signal else "NO ENTRY",
            "take": round(take, 4) if signal else 0,
            "stop": round(stop, 4) if signal else 0,
            "leverage": leverage,
            "quality": quality,
            "atr": round(atr, 4),
            "ema200": round(ema200, 4),
            "rsi": round(rsi, 2),
            "volume": int(curr_vol),
            "signal": signal,
            "budget": budget
        }
    except Exception as e:
        logger.error(f"Ошибка анализа {pair}: {e}")
        return {
            "price": 0, "direction": "ERROR", "take": 0, "stop": 0, "leverage": 1, "quality": 0, "atr": 0, "ema200": 0, "rsi": 0, "volume": 0, "signal": False, "budget": budget
        }

# === ФОРМАТ СИГНАЛА ===
def make_signal_message(signal, pair, typ):
    if not signal['signal']:
        return f"<b>⛔ Нет хорошего сигнала по {pair} ({typ}) прямо сейчас.</b>\n<i>Проверь позже или выбери другую пару.</i>"
    msg = (f"<b>⚡ {typ} ({pair})</b>\n"
            f"Плечо: <b>{signal['leverage']}</b>\n"
            f"Вход: <b>{signal['price']}</b>\n"
            f"Тейк: <b>{signal['take']}</b> | Стоп: <b>{signal['stop']}</b>\n"
            f"Направление: <b>{signal['direction']}</b>\n"
            f"Качество: <b>{signal['quality']:.1f}/10</b>\n"
            f"ATR: <b>{signal['atr']}</b> | EMA200: <b>{signal['ema200']}</b> | RSI: <b>{signal['rsi']}</b>\n"
            f"Объем: <b>{signal['volume']}</b>\n"
            f"<i>Стоп и тейк рассчитаны по волатильности, тренду и объёму.</i>")
    return msg

# === MAIN ===
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("generate_token", generate_token))
    app.add_handler(CommandHandler("activate", activate_token))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
