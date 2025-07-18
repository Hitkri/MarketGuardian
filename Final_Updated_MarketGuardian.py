import os
import logging
import time
import uuid
import sqlite3
import asyncio
import requests

import pandas as pd
import numpy as np
import openai
from ccxt.async_support import binance as ccxt_binance
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
# from sklearn.preprocessing import MinMaxScaler  # Removed dependency

# Dash dashboard removed - not installed

# === PHASE 6: BOT WITH CCXT POLLING & DASHBOARD ===

# === API KEYS & TOKENS ===
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0')
ADMIN_ID = int(os.getenv('ADMIN_ID', '1407143951'))
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'your_binance_api_key')
BINANCE_SECRET = os.getenv('BINANCE_SECRET', 'your_binance_secret')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'sk-...')
COVALENT_API_KEY = os.getenv('COVALENT_API_KEY', 'cqt_rQYF3wXMKqTkJGdWPBRy3B8vwrrh')

openai.api_key = OPENAI_API_KEY

# === CONFIGURATION ===
BUDGET_FUTURES = 500
BUDGET_SPOT = 3000
SPOT_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']
FUTURES_PAIRS = SPOT_PAIRS.copy()
TIMEFRAMES = ['5m', '15m', '1h']
QUALITY_THRESHOLD = 5.0
RSI_LOW_Q, RSI_HIGH_Q = 10, 90
ATR_LOW_Q, ATR_HIGH_Q = 10, 90
SENTIMENT_THRESHOLD = 0.0  # disabled sentiment

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('trading_bot')

# === DATABASE SETUP ===
conn = sqlite3.connect('trading_bot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, activation_time REAL)')
cursor.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, user_id INTEGER, mode TEXT, symbol TEXT, side TEXT, price REAL, size REAL, paper INTEGER)')
cursor.execute('CREATE TABLE IF NOT EXISTS last_signals (mode TEXT, symbol TEXT PRIMARY KEY, direction TEXT, timestamp REAL)')
conn.commit()

# === EXCHANGE INIT ===
exchange = ccxt_binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET,
    'enableRateLimit': True,
})

# === INDICATORS ===
def compute_rsi(df, period=14):
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(df, period=14):
    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_macd(df):
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return macd, signal, hist


def compute_bbands(df):
    mb = df['close'].rolling(20).mean()
    sd = df['close'].rolling(20).std()
    ub = mb + 2 * sd
    lb = mb - 2 * sd
    return mb, ub, lb


def compute_stoch(df):
    low = df['low'].rolling(14).min()
    high = df['high'].rolling(14).max()
    k = 100 * (df['close'] - low) / (high - low)
    d = k.rolling(3).mean()
    return k, d


def compute_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    return (tp * df['volume']).cumsum() / df['volume'].cumsum()

# === FORECASTING ===
def forecast_next(df):
    y = df['close'].values
    x = np.arange(len(y))
    if len(x) < 2:
        return y[-1] if len(y) else 0
    coeffs = np.polyfit(x, y, 1)
    return coeffs[0] * len(x) + coeffs[1]

# === FUNDAMENTALS VIA COVALENT ===
def fetch_onchain(symbol):
    asset = symbol.split('/')[0]
    url = f"https://api.covalenthq.com/v1/pricing/historical_v2/{asset}/USD/" \
          f"?quote-currency=USD&format=JSON&key={COVALENT_API_KEY}"
    r = requests.get(url)
    if r.ok:
        prices = r.json().get('data', {}).get('prices', [])
        return prices[-1].get('volume_24h_quote', 0) if prices else None
    return None

# === SIGNAL GENERATION ===
async def generate_signal(symbol, mode='spot'):
    # fetch candles
    frames = {}
    for tf in TIMEFRAMES:
        ohlcv = await exchange.fetch_ohlcv(symbol, tf, limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        frames[tf] = df
    # technical
    latest = frames[TIMEFRAMES[-1]]
    price = latest['close'].iloc[-1]
    # example simple trend
    entry = forecast_next(latest)
    atr = compute_atr(latest).iloc[-1]
    stop = entry - atr * 1.5
    take = entry + atr * 3
    direction = 'LONG' if take > entry else 'SHORT'
    quality = abs(entry - price)
    return {
        'symbol': symbol,
        'direction': direction,
        'price': round(entry, 4),
        'stop': round(stop, 4),
        'take': round(take, 4),
        'quality': round(quality, 4),
        'signal': True
    }

# === STATE & RECORD ===
def get_last_signal(mode, symbol):
    row = cursor.execute('SELECT direction FROM last_signals WHERE mode=? AND symbol=?', (mode, symbol)).fetchone()
    return row[0] if row else None

def update_last_signal(mode, symbol, direction):
    cursor.execute('INSERT OR REPLACE INTO last_signals(mode, symbol, direction, timestamp) VALUES(?,?,?,?)',
                   (mode, symbol, direction, time.time()))
    conn.commit()

def record_trade(user_id, mode, symbol, side, price, size, paper=True):
    cursor.execute('INSERT INTO trades(timestamp,user_id,mode,symbol,side,price,size,paper) VALUES(?,?,?,?,?,?,?,?)',
                   (time.time(), user_id, mode, symbol, side, price, size, int(paper)))
    conn.commit()

# === MESSAGE FORMATTING ===
def format_signal(sig, mode):
    typ = 'FUTURES' if mode=='futures' else 'SPOT'
    text = (f"⚡ {typ} {sig['symbol']}\nDir: {sig['direction']}\n"
            f"Entry: {sig['price']}\nStop: {sig['stop']} | Take: {sig['take']}\n"
            f"Quality: {sig['quality']}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Monitor", callback_data=f"monitor_{mode}_{sig['symbol']}")]])
    return text, kb

# === MONITORING ===
async def monitor_markets(app):
    tasks = [process_symbol(app, s, m) for m,pairs in [('spot', SPOT_PAIRS),('futures',FUTURES_PAIRS)] for s in pairs]
    await asyncio.gather(*tasks)

async def process_symbol(app, sym, mode):
    sig = await generate_signal(sym, mode)
    last = get_last_signal(mode, sym)
    if sig and sig['signal'] and sig['direction']!=last:
        update_last_signal(mode, sym, sig['direction'])
        msg,kb = format_signal(sig, mode)
        users = cursor.execute('SELECT user_id FROM tokens').fetchall()
        for (uid,) in users:
            await app.bot.send_message(uid, msg, parse_mode='HTML', reply_markup=kb)
            record_trade(uid, mode, sym, sig['direction'], sig['price'], 0)

# === DASHBOARD REMOVED ===
# Dashboard feature disabled

# === TELEGRAM HANDLERS ===

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    uid = update.effective_chat.id
    # User selects mode: show list of pairs
    if data in ('spot', 'futures'):
        mode = data
        # Build keyboard of pairs
        pairs = SPOT_PAIRS if mode=='spot' else FUTURES_PAIRS
        keyboard = []
        for i in range(0, len(pairs), 2):
            row = []
            for j in range(2):
                if i+j < len(pairs):
                    sym = pairs[i+j]
                    row.append(InlineKeyboardButton(sym, callback_data=f"pair_{mode}_{sym}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='back')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(
            f"<b>Select {mode.capitalize()} Pair:</b>", parse_mode='HTML', reply_markup=reply_markup)
        await update.callback_query.answer()
        return
    # Back to main menu
    if data == 'back':
        await main_menu(update, context)
        await update.callback_query.answer()
        return
    # User selects a specific pair
    if data.startswith('pair_'):
        _, mode, sym = data.split('_', 2)
        # Send initial analysis
        await context.bot.send_message(uid, f"Analyzing {sym} on {mode}...", parse_mode='HTML')
        sig = await generate_signal(sym, mode)
        if not sig or not sig.get('signal'):
            await context.bot.send_message(uid, f"No valid signal for {sym}.")
        else:
            msg, kb = format_signal(sig, mode)
            await context.bot.send_message(uid, msg, parse_mode='HTML', reply_markup=kb)
        # Schedule continuous monitoring every minute
        job_id = f"monitor_{uid}_{mode}_{sym}" 
        # Remove existing job if any
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        # Add new job
        scheduler.add_job(
            lambda job_uid=uid, job_mode=mode, job_sym=sym: asyncio.create_task(
                process_symbol(context.application, job_sym, job_mode)
            ),
            'interval', minutes=1, id=job_id
        )
        await context.bot.send_message(uid, f"Started monitoring {sym} every minute.")
        await update.callback_query.answer()
        return
    # Monitor action
    parts = data.split('_')
    if parts[0] == 'monitor':
        mode = parts[1]
        sym = '_'.join(parts[2:])
        sig = await generate_signal(sym, mode)
        status = 'No signal' if not sig else f"{sig['direction']} @ {sig['price']}"
        await context.bot.send_message(uid, f"<b>{sym}</b>: {status}", parse_mode='HTML')
        await update.callback_query.answer()

# === MAIN ===
# Initialize and start APScheduler
scheduler = AsyncIOScheduler()
scheduler.start()

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('generate_token', generate_token))
    app.add_handler(CommandHandler('activate', activate))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Full market-wide monitor (optional)
    scheduler.add_job(lambda: asyncio.create_task(monitor_markets(app)), 'interval', seconds=30, id='global_monitor', replace_existing=True)

    app.run_polling()

if __name__ == '__main__':
    main()
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('generate_token', generate_token))
    app.add_handler(CommandHandler('activate', activate))
    app.add_handler(CallbackQueryHandler(button_handler))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(monitor_markets(app)), 'interval', seconds=30)
    scheduler.start()

    app.run_polling()

if __name__ == '__main__':
    main()
