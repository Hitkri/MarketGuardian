import os
import logging
import time
import uuid
import sqlite3
import asyncio
import requests
import matplotlib.pyplot as plt
import io

import pandas as pd
import numpy as np
import openai
from ccxt.async_support import binance as ccxt_binance
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === API KEYS ===
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'your_telegram_token')
ADMIN_ID = int(os.getenv('ADMIN_ID', '1407143951'))
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'your_binance_api_key')
BINANCE_SECRET = os.getenv('BINANCE_SECRET', 'your_binance_secret')
openai.api_key = os.getenv('OPENAI_API_KEY', 'your_openai_key')

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('trading_assistant')

# === DATABASE ===
conn = sqlite3.connect('trading_bot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, activation_time REAL)')
cursor.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, user_id INTEGER, mode TEXT, symbol TEXT, side TEXT, price REAL, size REAL, paper INTEGER, pnl REAL, take REAL, stop REAL)')
conn.commit()

# === EXCHANGE INIT ===
exchange = ccxt_binance({ 'enableRateLimit': True, 'options': {'defaultType': 'future'} })

# === TELEGRAM BOT CORE ===
scheduler = AsyncIOScheduler()
scheduler.start()

popular_pairs = ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'XRP/USDT', 'SOL/USDT', 'TON/USDT', 'DOGE/USDT', 'LINK/USDT', 'ADA/USDT', 'OP/USDT']

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    if cursor.execute('SELECT 1 FROM tokens WHERE user_id=?', (uid,)).fetchone():
        await update.message.reply_text('🟢 Готов! Напиши /new чтобы открыть сигнал по паре.')
    else:
        await update.message.reply_text('❌ Не активирован. Введите: /activate <token>')

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    if len(context.args) != 1:
        return await update.message.reply_text('Формат: /activate <token>')
    token = context.args[0]
    if not cursor.execute('SELECT token FROM tokens WHERE token=? AND user_id IS NULL', (token,)).fetchone():
        return await update.message.reply_text('❌ Токен недействителен')
    cursor.execute('UPDATE tokens SET user_id=?, username=?, activation_time=? WHERE token=?',
                   (uid, update.effective_user.username, time.time(), token))
    conn.commit()
    await update.message.reply_text('✅ Активировано. Теперь напиши /new')

async def new_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for pair in popular_pairs:
        keyboard.append([InlineKeyboardButton(pair, callback_data=f'signal_{pair.replace("/", "")}')])
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Выбери пару для сигнала:', reply_markup=markup)

async def handle_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sym_raw = query.data.split('_')[1]
    symbol = sym_raw.replace('USDT', '') + '/USDT'
    ohlcv = await exchange.fetch_ohlcv(symbol, '5m', limit=50)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    price = df['close'].iloc[-1]
    last_high = df['high'].max()
    last_low = df['low'].min()
    midpoint = round((last_high + last_low) / 2, 4)
    side = 'SHORT' if price > midpoint else 'LONG'
    stop = round(price * (1.01 if side == 'SHORT' else 0.99), 4)
    take = round(price * (0.99 if side == 'SHORT' else 1.01), 4)

    # Chart
    fig, ax = plt.subplots()
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    ax.plot(df['timestamp'], df['close'], label='Price')
    ax.axhline(price, color='blue', linestyle='--', label='Entry')
    ax.axhline(stop, color='red', linestyle='--', label='Stop')
    ax.axhline(take, color='green', linestyle='--', label='Take')
    ax.legend()
    ax.set_title(symbol)
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)

    text = f"📊 {symbol}\nРежим: {side}\nТекущая: {price}\nВход: {price}\nСтоп: {stop}\nТейк: {take}\n\nНажми ВХОД после входа в сделку."
    buttons = [[InlineKeyboardButton("📥 ВХОД", callback_data=f'enter_{symbol}_{side}_{price}_{stop}_{take}')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    await context.bot.send_photo(query.message.chat.id, photo=InputFile(buf, filename="chart.png"))

async def handle_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')
    uid = update.effective_chat.id
    symbol = parts[1] + '/' + parts[2]
    side = parts[3]
    entry = float(parts[4])
    stop = float(parts[5])
    take = float(parts[6])
    cursor.execute('INSERT INTO trades(timestamp, user_id, mode, symbol, side, price, size, paper, pnl, take, stop) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (time.time(), uid, 'futures', symbol, side, entry, 0, 1, 0, take, stop))
    conn.commit()
    await query.edit_message_text(f"📥 Позиция по {symbol} открыта по цене {entry}\nСледим... (ручной режим)")

async def pnl_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    rows = cursor.execute('SELECT side, price, pnl FROM trades WHERE user_id=?', (uid,)).fetchall()
    if not rows:
        await update.message.reply_text("Нет сделок")
        return
    total = sum(r[2] for r in rows)
    count = len(rows)
    avg = total / count
    await update.message.reply_text(f"📈 Сделок: {count}\n💰 Общий PNL: {round(total, 2)} USDT\n📊 Средний: {round(avg, 2)} USDT")

async def generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return await update.message.reply_text('Forbidden')
    token = str(uuid.uuid4())
    cursor.execute('INSERT INTO tokens(token) VALUES(?)', (token,))
    conn.commit()
    await update.message.reply_text(f'Token: {token}')

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('activate', activate))
    app.add_handler(CommandHandler('new', new_signal))
    app.add_handler(CommandHandler('stats', pnl_stats))
    app.add_handler(CommandHandler('generate_token', generate_token))
    app.add_handler(CallbackQueryHandler(handle_signal, pattern=r'^signal_'))
    app.add_handler(CallbackQueryHandler(handle_entry, pattern=r'^enter_'))
    app.run_polling()

if __name__ == '__main__':
    main()
