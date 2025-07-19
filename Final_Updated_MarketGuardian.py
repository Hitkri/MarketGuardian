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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputMediaPhoto
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from io import BytesIO
import ccxt
import mplfinance as mpf
import matplotlib.pyplot as plt

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

exchange = ccxt.binance()

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
        entry_price = await fetch_price(symbol)
        active_positions[uid] = {'symbol': symbol, 'entry': entry_price, 'side': 'LONG'}
        cursor.execute('INSERT INTO trades (timestamp, user_id, symbol, side, entry) VALUES (?, ?, ?, ?, ?)',
                       (time.time(), uid, symbol, 'LONG', entry_price))
        conn.commit()

        chart = await generate_chart(symbol, entry_price)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Вход", callback_data='enter')],
            [InlineKeyboardButton("❌ Закрыть сделку", callback_data='close')]
        ])
        await context.bot.send_photo(chat_id=uid, photo=chart,
            caption=f'📈 Вход в позицию {symbol} (LONG) по {entry_price}\n🎯 Take Profit: {round(entry_price * 1.01, 4)}\n🛑 Stop Loss: {round(entry_price * 0.99, 4)}',
            reply_markup=kb)
        await update.callback_query.answer()

    elif data == 'enter':
        trigger = IntervalTrigger(seconds=30)
        scheduler.add_job(monitor_price, trigger, args=[context, uid], id=f'monitor_{uid}', replace_existing=True)
        await context.bot.send_message(uid, '🟢 Сделка активирована. Слежу за движением.')
        await update.callback_query.answer()

    elif data == 'close':
        try:
            scheduler.remove_job(f'monitor_{uid}')
        except:
            pass
        cursor.execute('SELECT id FROM trades WHERE user_id=? AND active=1 ORDER BY timestamp DESC LIMIT 1', (uid,))
        trade = cursor.fetchone()
        if trade:
            cursor.execute('UPDATE trades SET active=0 WHERE id=?', (trade[0],))
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
    now_price = await fetch_price(symbol)
    delta = now_price - entry
    danger_zone = round(entry * 0.985, 4)
    tp = round(entry * 1.01, 4)
    sl = round(entry * 0.99, 4)

    rsi, macd_signal = await fetch_indicators(symbol)

    status = f"📊 {symbol} ({pos['side']})\nВход: {entry} | Сейчас: {now_price}\n"
    if now_price <= sl:
        status += "❗ Цена достигла Stop Loss. Рекомендуется закрыть позицию."
    elif now_price >= tp:
        status += "✅ Достигнут Take Profit. Зафиксируйте прибыль."
    elif now_price < danger_zone:
        status += f"⚠️ Цена опустилась ниже {danger_zone}. Возможен пробой вниз — подумайте о выходе."
    elif abs(delta) < 0.002:
        status += "⏳ Рынок в боковике. Можно ждать подтверждения."
    else:
        status += f"🔄 Цена в пределах нормы.\nЕсли цена пробьёт {tp}, возможен рост. Если упадёт ниже {sl}, возможен разворот вниз."

    status += f"\n📈 RSI: {rsi:.2f} | MACD сигнал: {'↑' if macd_signal else '↓'}"
    await context.bot.send_message(uid, status)

async def fetch_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return round(ticker['last'], 4)
    except Exception as e:
        logger.error(f"Ошибка получения цены: {e}")
        return round(3.25 + np.random.uniform(-0.03, 0.03), 4)

async def fetch_indicators(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '5m')[-50:]
        df = pd.DataFrame(ohlcv, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['RSI'] = compute_rsi(df['Close'], 14)
        macd_line = df['Close'].ewm(span=12).mean() - df['Close'].ewm(span=26).mean()
        signal_line = macd_line.ewm(span=9).mean()
        macd_signal = macd_line.iloc[-1] > signal_line.iloc[-1]
        return df['RSI'].iloc[-1], macd_signal
    except:
        return 50, False

def compute_rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def generate_chart(symbol, entry_price):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '5m')[-30:]
        df = pd.DataFrame(ohlcv, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Date', inplace=True)

        support = df['Low'].rolling(window=10).min().iloc[-1]
        resistance = df['High'].rolling(window=10).max().iloc[-1]

        apdict = [
            mpf.make_addplot(df['Close'].rolling(5).mean(), color='orange'),
            mpf.make_addplot([entry_price] * len(df), color='green', width=0.75, linestyle='--'),
            mpf.make_addplot([support] * len(df), color='red', linestyle=':'),
            mpf.make_addplot([resistance] * len(df), color='blue', linestyle=':')
        ]
        fig, axlist = mpf.plot(df, type='candle', style='charles', returnfig=True, volume=False, addplot=apdict)
        buf = BytesIO()
        fig.savefig(buf, format='png')
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка генерации графика: {e}")
        return None

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
    msg = update.message.text.strip().replace(',', '.')
    if msg.startswith('+') or msg.startswith('-') or msg.startswith('0'):
        try:
            profit = float(msg)
            cursor.execute('SELECT id FROM trades WHERE user_id=? AND active=0 ORDER BY timestamp DESC LIMIT 1', (uid,))
            trade = cursor.fetchone()
            if trade:
                cursor.execute('UPDATE trades SET profit=? WHERE id=?', (profit, trade[0]))
                conn.commit()
                await update.message.reply_text(f'💾 Записано: {profit:+.2f} USD')
            else:
                await update.message.reply_text('❌ Сделка не найдена для обновления.')
        except Exception as e:
            logger.error(f"Ошибка записи прибыли: {e}")
            await update.message.reply_text('❌ Ошибка при вводе прибыли/убытка.')
    else:
        await update.message.reply_text('⚠️ Введи +10 или -5 для записи результата.')

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
