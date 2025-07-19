import os
import logging
import time
import uuid
import sqlite3
import asyncio
import requests
import pandas as pd
import numpy as np
from datetime import datetime, date
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import matplotlib.pyplot as plt
from io import BytesIO

# === API KEYS ===
TELEGRAM_BOT_TOKEN = '7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0'  # жёстко зашит токен
ADMIN_ID = 1407143951

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('trading_assistant')

# === DATABASE ===
conn = sqlite3.connect('trading_bot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, activation_time REAL)')
cursor.execute('''
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL,
    user_id INTEGER,
    symbol TEXT,
    side TEXT,
    entry REAL,
    active INTEGER DEFAULT 1,
    closed INTEGER DEFAULT 0,
    pnl REAL DEFAULT 0
)
''')
conn.commit()

# === STATE ===
active_positions = {}
waiting_for_pnl = {}

# === TELEGRAM ===
scheduler = AsyncIOScheduler()
scheduler.start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Бот активен. Напиши /menu')

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
        entry_price = await fetch_mock_price(symbol)
        img = generate_fake_chart(symbol, entry_price)
        active_positions[uid] = {'symbol': symbol, 'entry': entry_price, 'side': 'LONG'}
        cursor.execute('INSERT INTO trades (timestamp, user_id, symbol, side, entry) VALUES (?, ?, ?, ?, ?)',
                       (time.time(), uid, symbol, 'LONG', entry_price))
        conn.commit()

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрыть сделку", callback_data='close')]])
        await context.bot.send_photo(chat_id=uid, photo=img,
            caption=f'📈 Вход в позицию {symbol} по {entry_price}\nСледим за движением...\nДержим!', reply_markup=kb)
        scheduler.add_job(lambda: asyncio.create_task(monitor_price(context, uid)), 'interval', seconds=30, id=f'monitor_{uid}', replace_existing=True)
        await update.callback_query.answer()

    elif data == 'close':
        scheduler.remove_job(f'monitor_{uid}')
        cursor.execute('UPDATE trades SET active=0 WHERE user_id=? AND active=1 ORDER BY timestamp DESC LIMIT 1', (uid,))
        conn.commit()
        await context.bot.send_message(uid, '💼 Сделка закрыта. Напиши, сколько заработал или потерял (например: +10 или -5)')
        waiting_for_pnl[uid] = True
        active_positions.pop(uid, None)
        await update.callback_query.answer()

    elif data == 'report':
        await generate_report(uid, context)
        await update.callback_query.answer()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    if uid in waiting_for_pnl:
        try:
            value = float(update.message.text.replace('+',''))
            cursor.execute('''UPDATE trades SET closed=1, pnl=? WHERE user_id=? AND closed=0 ORDER BY timestamp DESC LIMIT 1''',
                           (value, uid))
            conn.commit()
            await update.message.reply_text(f'✅ Записано: {value} USD')
        except:
            await update.message.reply_text('Ошибка ввода. Напиши пример: +10 или -5')
        waiting_for_pnl.pop(uid, None)

async def monitor_price(context, uid):
    if uid not in active_positions:
        return
    pos = active_positions[uid]
    symbol = pos['symbol']
    entry = pos['entry']
    side = pos['side']
    now_price = await fetch_mock_price(symbol)
    delta = now_price - entry
    status = f"📊 {symbol} {side}\nВход: {entry} | Сейчас: {now_price}\n"

    if abs(delta) < 0.002:
        status += "⏳ Движение слабое. Наблюдаем."
    elif delta > 0.01:
        status += "✅ Отличный рост. Держим!"
    elif delta < -0.01:
        status += "⚠️ Цена падает. Возможен выход."
    else:
        status += "📈 Позиция активна. Всё стабильно."

    await context.bot.send_message(uid, status)

async def fetch_mock_price(symbol):
    import random
    return round(3.25 + random.uniform(-0.015, 0.015), 4)

def generate_fake_chart(symbol, price):
    x = pd.date_range(end=datetime.now(), periods=30, freq='T')
    y = [price + np.sin(i / 5) * 0.01 for i in range(30)]
    plt.figure(figsize=(6,3))
    plt.plot(x, y, label=symbol)
    plt.axhline(price, color='green', linestyle='--', label=f'Entry {price}')
    plt.title(f'{symbol} Entry = {price}')
    plt.legend()
    plt.xticks(rotation=45)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

async def generate_report(uid, context):
    cursor.execute('SELECT timestamp, pnl FROM trades WHERE user_id=? AND closed=1', (uid,))
    rows = cursor.fetchall()
    if not rows:
        return await context.bot.send_message(uid, 'Нет завершённых сделок.')

    daily = {}
    for ts, pnl in rows:
        d = date.fromtimestamp(ts).day
        daily[d] = daily.get(d, 0) + pnl

    report_lines = ['📅 Отчёт по дням:']
    for day in range(1, 32):
        val = daily.get(day)
        if val is not None:
            report_lines.append(f'{day:02d}: {val:+.2f} USD')

    total = sum(daily.values())
    report_lines.append(f'\n📈 За месяц: {total:+.2f} USD')
    await context.bot.send_message(uid, '\n'.join(report_lines))

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == '__main__':
    main()
