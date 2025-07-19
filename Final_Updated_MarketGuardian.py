import os
import logging
import time
import uuid
import sqlite3
import asyncio
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from io import BytesIO
import ccxt

# === API KEYS ===
TELEGRAM_BOT_TOKEN = '7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0'
ADMIN_ID = int(os.getenv('ADMIN_ID', '1407143951'))

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('trading_assistant')

# === DATABASE ===
conn = sqlite3.connect('trading_bot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, activation_time REAL)')
cursor.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, user_id INTEGER, symbol TEXT, side TEXT, entry REAL, active INTEGER DEFAULT 1, profit REAL DEFAULT 0.0)')
conn.commit()

# === STATE ===
active_positions = {}

# === TELEGRAM ===
scheduler = AsyncIOScheduler()
scheduler.start()

# === BINANCE ===
binance = ccxt.binance()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Добро пожаловать! Напиши /menu')

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = ['TONUSDT', 'BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'BNBUSDT', 'SOLUSDT']
    buttons = [[InlineKeyboardButton(p, callback_data=f'select_{p}')] for p in pairs]
    buttons.append([InlineKeyboardButton('📅 Отчёт по сделкам', callback_data='report')])
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text('Выбери пару для отслеживания:', reply_markup=markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    uid = update.effective_chat.id

    if data.startswith('select_'):
        symbol = data.replace('select_', '')
        await context.bot.send_message(uid, f'🔍 Анализирую {symbol}...')
        entry_price = await fetch_binance_price(symbol)
        active_positions[uid] = {'symbol': symbol, 'entry': entry_price, 'side': 'LONG'}
        cursor.execute('INSERT INTO trades (timestamp, user_id, symbol, side, entry) VALUES (?, ?, ?, ?, ?)',
                       (time.time(), uid, symbol, 'LONG', entry_price))
        conn.commit()

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Вход", callback_data='enter')],
            [InlineKeyboardButton("❌ Закрыть сделку", callback_data='close')]
        ])
        await context.bot.send_message(chat_id=uid,
            text=f'📈 Вход в позицию {symbol} (LONG) по {entry_price}\n🎯 Take Profit: {round(entry_price * 1.01, 4)}\n🛑 Stop Loss: {round(entry_price * 0.99, 4)}',
            reply_markup=kb)
        await update.callback_query.answer()

    elif data == 'enter':
        scheduler.add_job(monitor_price, 'interval', seconds=30, id=f'monitor_{uid}', replace_existing=True, args=[context, uid])
        await context.bot.send_message(uid, '🟢 Сделка активирована. Слежу за движением.')
        await update.callback_query.answer()

    elif data == 'close':
        try:
            scheduler.remove_job(f'monitor_{uid}')
        except:
            pass
        cursor.execute('UPDATE trades SET active=0 WHERE user_id=?', (uid,))
        conn.commit()
        active_positions.pop(uid, None)
        await context.bot.send_message(uid, '💼 Сделка закрыта. Напиши +10 или -5 — сколько заработал/потерял?')
        await update.callback_query.answer()

    elif data == 'report':
        report = generate_report(uid)
        await context.bot.send_message(uid, report)
        await update.callback_query.answer()

async def monitor_price(context, uid):
    if uid not in active_positions:
        return
    pos = active_positions[uid]
    symbol = pos['symbol']
    entry = pos['entry']
    now_price = await fetch_binance_price(symbol)
    delta = now_price - entry
    status = f"📊 {symbol} ({pos['side']})\nВход: {entry} | Сейчас: {now_price}\n"

    if abs(delta) < 0.002:
        status += "⏳ Движение слабое. Наблюдаем."
    elif delta > 0.01:
        status += "✅ Отличный рост. Держим!"
    elif delta < -0.01:
        status += "⚠️ Цена падает. Возможен выход."
    else:
        status += "📈 Позиция активна. Всё стабильно."

    await context.bot.send_message(uid, status)

async def fetch_binance_price(symbol):
    try:
        price = binance.fetch_ticker(symbol)['last']
        return round(price, 4)
    except Exception as e:
        print(f"Ошибка получения цены Binance: {e}")
        return 0.0

def generate_report(uid):
    cursor.execute('SELECT timestamp, profit FROM trades WHERE user_id=? AND profit != 0', (uid,))
    rows = cursor.fetchall()
    if not rows:
        return '⛔️ Нет данных по сделкам.'

    from collections import defaultdict
    day_map = defaultdict(list)
    for t, p in rows:
        d = datetime.fromtimestamp(t).day
        day_map[d].append(p)

    text = '📅 Отчёт по сделкам:\n'
    monthly_total = 0
    for day in sorted(day_map):
        total = sum(day_map[day])
        monthly_total += total
        count = len(day_map[day])
        text += f'• {day} число: {total:+.2f} USD ({count} сделок)\n'
    text += f'📈 Всего за месяц: {monthly_total:+.2f} USD'
    return text

async def profit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    msg = update.message.text.strip()
    if msg.startswith('+') or msg.startswith('-'):
        try:
            profit = float(msg)
            cursor.execute('UPDATE trades SET profit=? WHERE user_id=? AND active=0 ORDER BY timestamp DESC LIMIT 1', (profit, uid))
            conn.commit()
            await update.message.reply_text(f'💾 Записано: {profit:+.2f} USD')
        except:
            await update.message.reply_text('❌ Ошибка при вводе прибыли/убытка.')

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
    app.add_handler(CommandHandler('menu', menu))
    app.add_handler(CommandHandler('generate_token', generate_token))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, profit_input))
    app.run_polling()

if __name__ == '__main__':
    main()
