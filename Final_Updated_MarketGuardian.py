import os
import logging
import time
import uuid
import sqlite3
import ccxt
import random
import requests
import asyncio
import pandas as pd
from ta.volatility import AverageTrueRange
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

binance = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

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
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text("<b>Выбери режим:</b>", reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text("<b>Выбери режим:</b>", reply_markup=reply_markup, parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.message.chat.id

    if data == "spot_recommend":
        await choose_spot_pair(user_id, query)
    elif data == "futures_manual":
        await choose_futures_pair(user_id, query)
    elif data.startswith("futures_pair_"):
        pair = data.split("futures_pair_")[1]
        await send_futures_signal(user_id, pair, context)
    elif data.startswith("spot_pair_"):
        pair = data.split("spot_pair_")[1]
        await send_spot_signal(user_id, pair, context)
    elif data == "main_menu":
        await main_menu(update, context)
    elif data.startswith("monitor_"):
        pair = data.split("monitor_")[1]
        await monitor_signal(user_id, pair, context)

async def choose_futures_pair(user_id, query):
    # Кнопки по 2 в ряд для 20 пар
    pairs = FUTURES_PAIRS
    keyboard = []
    for i in range(0, len(pairs), 2):
        row = []
        row.append(InlineKeyboardButton(pairs[i], callback_data=f"futures_pair_{pairs[i]}"))
        if i + 1 < len(pairs):
            row.append(InlineKeyboardButton(pairs[i+1], callback_data=f"futures_pair_{pairs[i+1]}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выбери пару для сигнала:", reply_markup=markup)

async def choose_spot_pair(user_id, query):
    # Кнопки по 2 в ряд для 20 пар
    pairs = SPOT_PAIRS
    keyboard = []
    for i in range(0, len(pairs), 2):
        row = []
        row.append(InlineKeyboardButton(pairs[i], callback_data=f"spot_pair_{pairs[i]}"))
        if i + 1 < len(pairs):
            row.append(InlineKeyboardButton(pairs[i+1], callback_data=f"spot_pair_{pairs[i+1]}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выбери пару для сигнала:", reply_markup=markup)

# ЖИВОЙ анализ фьючерсов (плечо есть)
async def send_futures_signal(user_id, pair, context):
    signal = await analyze_pair(pair, budget=BUDGET_FUTURES, mode="futures")
    msg = make_signal_message(signal, pair, "ФЬЮЧЕРСЫ")
    keyboard = [[InlineKeyboardButton("👁 Мониторить сделку", callback_data=f"monitor_{pair}")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ЖИВОЙ анализ СПОТа (без плеча!)
async def send_spot_signal(user_id, pair, context):
    signal = await analyze_pair(pair, budget=BUDGET_SPOT, mode="spot")
    msg = make_signal_message(signal, pair, "СПОТ")
    keyboard = [[InlineKeyboardButton("👁 Мониторить сделку", callback_data=f"monitor_{pair}")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# Мониторинг открытой сделки как “живой трейдер”
async def monitor_signal(user_id, pair, context):
    await context.bot.send_message(chat_id=user_id, text=f"🟢 Мониторю {pair} для тебя!\nЕсли что-то резко изменится — пришлю совет как ассистент.", parse_mode="HTML")
    last_status = None
    for i in range(20):  # 20 раз, примерно 20 минут, раз в минуту (можешь увеличить!)
        signal = await analyze_pair(pair, budget=BUDGET_SPOT, mode="spot")  # можно добавить mode по типу
        status = ""
        # Эмулируем “живого трейдера”
        if not signal["signal"]:
            status = "💤 Пока лучше вне позиции."
        elif signal["direction"] == "LONG" and signal["rsi"] > 70:
            status = "⚠️ Перекупленность, можно зафиксировать часть прибыли."
        elif signal["direction"] == "SHORT" and signal["rsi"] < 30:
            status = "⚠️ Перепроданность, можно фиксировать."
        elif abs(signal["price"] - signal["take"]) < signal["atr"]:
            status = "🎯 Цель рядом! Можно частично фиксировать."
        elif abs(signal["price"] - signal["stop"]) < signal["atr"]:
            status = "❗ Близко к стопу! Будь внимателен."
        else:
            status = "👌 Всё нормально, держи позицию."
        # Если что-то новое — пишем
        if status != last_status:
            await context.bot.send_message(chat_id=user_id, text=f"{pair}: {status}\nЦена сейчас: <b>{signal['price']}</b>", parse_mode="HTML")
            last_status = status
        await asyncio.sleep(60)

async def analyze_pair(pair, budget, mode="futures"):
    try:
        timeframe = '1h'
        ohlcv = binance.fetch_ohlcv(pair, timeframe, limit=120)
        df = pd.DataFrame(ohlcv, columns=['ts','open','high','low','close','vol'])
        price = float(df.close.iloc[-1])

        atr = AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
        ema200 = EMAIndicator(df['close'], window=200, fillna=True).ema_indicator().iloc[-1]
        rsi = RSIIndicator(df['close'], window=14).rsi().iloc[-1]
        avg_vol = df['vol'].rolling(window=14).mean().iloc[-1]
        curr_vol = df['vol'].iloc[-1]

        signal = None
        direction = None
        leverage = 5 if mode == "futures" else 1
        quality = 0

        if mode == "spot":
            leverage = 1

        # Тренд
        if price > ema200 and rsi < 65:
            direction = "LONG"
            stop = price - 1.3 * atr
            take = price + 2.3 * atr
            signal = True
        elif price < ema200 and rsi > 35:
            direction = "SHORT"
            stop = price + 1.3 * atr
            take = price - 2.3 * atr
            signal = True
        else:
            signal = False

        if curr_vol < avg_vol * 0.7:
            signal = False

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

def make_signal_message(signal, pair, typ):
    if not signal['signal']:
        return f"<b>⛔ Нет хорошего сигнала по {pair} ({typ}) прямо сейчас.</b>\n<i>Проверь позже или выбери другую пару.</i>"
    text = f"<b>⚡ {typ} ({pair})</b>\n"
    if typ == "СПОТ":
        text += f"Вход: <b>{signal['price']}</b>\n"
    else:
        text += f"Плечо: <b>{signal['leverage']}</b>\nВход: <b>{signal['price']}</b>\n"
    text += (f"Тейк: <b>{signal['take']}</b> | Стоп: <b>{signal['stop']}</b>\n"
             f"Направление: <b>{signal['direction']}</b>\n"
             f"Качество: <b>{signal['quality']:.1f}/10</b>\n"
             f"ATR: <b>{signal['atr']}</b> | EMA200: <b>{signal['ema200']}</b> | RSI: <b>{signal['rsi']}</b>\n"
             f"Объем: <b>{signal['volume']}</b>\n"
             f"<i>Стоп и тейк динамические по волатильности, тренду, объёму и RSI.</i>")
    return text

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("generate_token", generate_token))
    app.add_handler(CommandHandler("activate", activate_token))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
