import os
import logging
import time
import uuid
import sqlite3
import asyncio
import requests
from collections import deque
from threading import Thread

import pandas as pd
import numpy as np
import openai
import praw
import tensorflow as tf
from binance import AsyncClient, BinanceSocketManager
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense
from sklearn.preprocessing import MinMaxScaler
import dash
import dash_table
import dash_core_components as dcc
import dash_html_components as html

# === PHASE 6: FULLY INTEGRATED BOT WITH DASHBOARD & FREE APIs ===
# GPU memory growth
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)

# === API KEYS & TOKENS ===
TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
ADMIN_ID = 1407143951
BINANCE_API_KEY = "your_binance_api_key"
BINANCE_SECRET = "your_binance_secret"
OPENAI_API_KEY = "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"
COVALENT_API_KEY = "cqt_rQYF3wXMKqTkJGdWPBRy3B8vwrrh"
REDDIT_CLIENT_ID = "26P87OQBghAruUA3KCyXEg"
REDDIT_CLIENT_SECRET = "sactoMALwmRB203rLnwrF9YvgdZ3kQ"
REDDIT_USER_AGENT = "my_reddit_bot_v1"

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
LSTM_LOOKBACK, LSTM_EPOCHS, LSTM_BATCH = 10, 3, 16
SENTIMENT_THRESHOLD = 0.1

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('trading_bot')

# === DATABASE SETUP ===
conn = sqlite3.connect('trading_bot.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, activation_time REAL)')
cursor.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, user_id INTEGER, mode TEXT, symbol TEXT, side TEXT, price REAL, amount REAL, paper INTEGER)')
cursor.execute('CREATE TABLE IF NOT EXISTS last_signals (mode TEXT, symbol TEXT PRIMARY KEY, direction TEXT, timestamp REAL)')
conn.commit()

# === CACHES & MODELS ===
candle_cache = {sym: {tf: deque(maxlen=200) for tf in TIMEFRAMES} for sym in SPOT_PAIRS + FUTURES_PAIRS}
lstm_models = {}
scalers = {}

# === REDDIT CLIENT ===
reddit = praw.Reddit(client_id=REDDIT_CLIENT_ID, client_secret=REDDIT_CLIENT_SECRET, user_agent=REDDIT_USER_AGENT)

# === WEBSOCKET LISTENER ===
async def ws_listener(app):
    client = await AsyncClient.create(BINANCE_API_KEY, BINANCE_SECRET)
    bm = BinanceSocketManager(client)
    streams = [f"{sym.replace('/', '').lower()}@kline_{tf}" for sym in SPOT_PAIRS + FUTURES_PAIRS for tf in TIMEFRAMES]
    async with bm.multiplex_socket(streams) as mstream:
        async for msg in mstream:
            stream = msg['stream']
            data = msg['data']['k']
            if data['x']:
                base = stream.split('@')[0]
                sym = base[:-4].upper() + '/USDT'
                tf_key = stream.split('@kline_')[1]
                candle_cache[sym][tf_key].append({
                    'timestamp': pd.to_datetime(data['t'], unit='ms'),
                    'open': float(data['o']),
                    'high': float(data['h']),
                    'low': float(data['l']),
                    'close': float(data['c']),
                    'volume': float(data['v'])
                })
    await client.close_connection()

# === INDICATOR FUNCTIONS ===
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
    low_min = df['low'].rolling(14).min()
    high_max = df['high'].rolling(14).max()
    k = 100 * (df['close'] - low_min) / (high_max - low_min)
    d = k.rolling(3).mean()
    return k, d


def compute_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    return (tp * df['volume']).cumsum() / df['volume'].cumsum()

# === LSTM TRAIN & PREDICT ===
def train_lstm(symbol, tf_key):
    buf = candle_cache[symbol][tf_key]
    df = pd.DataFrame(buf).set_index('timestamp')
    closes = df['close'].values.reshape(-1, 1)
    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(closes)
    X, y = [], []
    for i in range(len(scaled) - LSTM_LOOKBACK):
        X.append(scaled[i:i + LSTM_LOOKBACK, 0])
        y.append(scaled[i + LSTM_LOOKBACK, 0])
    X, y = np.array(X), np.array(y)
    X = X.reshape((X.shape[0], X.shape[1], 1))
    model = Sequential([LSTM(50, input_shape=(LSTM_LOOKBACK, 1)), Dense(1)])
    model.compile(optimizer='adam', loss='mse')
    model.fit(X, y, epochs=LSTM_EPOCHS, batch_size=LSTM_BATCH, verbose=0)
    lstm_models[(symbol, tf_key)] = model
    scalers[(symbol, tf_key)] = scaler

def predict_lstm(symbol, tf_key):
    key = (symbol, tf_key)
    buf = candle_cache[symbol][tf_key]
    df = pd.DataFrame(buf).set_index('timestamp')
    closes = df['close'].values.reshape(-1, 1)
    if key not in lstm_models:
        train_lstm(symbol, tf_key)
    scaler = scalers[key]
    model = lstm_models[key]
    scaled = scaler.transform(closes)
    seq = scaled[-LSTM_LOOKBACK:].reshape((1, LSTM_LOOKBACK, 1))
    pred = model.predict(seq, verbose=0)
    return float(scaler.inverse_transform(pred)[0, 0])

# === FUNDAMENTALS via Covalent ===
def fetch_onchain(symbol):
    asset = symbol.split('/')[0]
    url = f"https://api.covalenthq.com/v1/pricing/historical_v2/{asset}/USD/?quote-currency=USD&format=JSON&key={COVALENT_API_KEY}"
    r = requests.get(url)
    if r.ok:
        data = r.json().get('data', {}).get('prices', [])
        return data[-1].get('volume_24h_quote', 0) if data else None
    return None

# === SENTIMENT via Reddit ===
def fetch_reddit_sentiment(symbol):
    texts = []
    for submission in reddit.subreddit('cryptocurrency').search(symbol, limit=20):
        texts.append(submission.title + ' ' + submission.selftext)
    if not texts:
        return 0
    prompt = 'Analyze sentiment:' + '\n'.join(texts)
    resp = openai.ChatCompletion.create(
        model='gpt-4o',
        messages=[{'role':'system','content':'You are sentiment analyzer.'}, {'role':'user','content':prompt}],
        max_tokens=50
    )
    try:
        return float(resp.choices[0].message.content.strip().split()[0])
    except:
        return 0

# === SIGNAL GENERATION PHASES 1-5 ===
def generate_signal(symbol, mode='spot'):
    onchain = fetch_onchain(symbol)
    sentiment = fetch_reddit_sentiment(symbol)
    if onchain is None or abs(sentiment) < SENTIMENT_THRESHOLD:
        return None
    results = {}
    for tf_key in TIMEFRAMES:
        buf = candle_cache[symbol][tf_key]
        if len(buf) < 50:
            return None
        df = pd.DataFrame(buf).set_index('timestamp')
        rsi = compute_rsi(df)
        atr = compute_atr(df)
        hist = compute_macd(df)[2]
        mb, ub, lb = compute_bbands(df)
        price_cur = df['close'].iloc[-1]
        rsi_low = np.nanpercentile(rsi.dropna(), RSI_LOW_Q)
        rsi_high = np.nanpercentile(rsi.dropna(), RSI_HIGH_Q)
        atr_low = np.nanpercentile(atr.dropna(), ATR_LOW_Q)
        atr_high = np.nanpercentile(atr.dropna(), ATR_HIGH_Q)
        if not (atr_low < atr.iloc[-1] < atr_high):
            results[tf_key] = 'NONE'
            continue
        long_cond = (rsi.iloc[-2] < rsi_low and rsi.iloc[-1] < rsi_low and price_cur < lb.iloc[-1] and hist.iloc[-1] > 0)
        short_cond = (rsi.iloc[-2] > rsi_high and rsi.iloc[-1] > rsi_high and price_cur > ub.iloc[-1] and hist.iloc[-1] < 0)
        results[tf_key] = 'LONG' if long_cond else 'SHORT' if short_cond else 'NONE'
    if all(v == 'LONG' for v in results.values()):
        direction = 'LONG'
    elif all(v == 'SHORT' for v in results.values()):
        direction = 'SHORT'
    else:
        return None
    entry = round(predict_lstm(symbol, '15m'), 2)
    atr_val = compute_atr(pd.DataFrame(candle_cache[symbol]['15m']).set_index('timestamp')).iloc[-1]
    stop = round(entry - atr_val * (1.5 if direction == 'LONG' else -1.5), 2)
    take = round(entry + atr_val * (3 if direction == 'LONG' else -3), 2)
    hist_val = compute_macd(pd.DataFrame(candle_cache[symbol]['15m']).set_index('timestamp'))[2].iloc[-1]
    p = min(abs(hist_val) / 10, 0.5)
    R = abs((take - entry) / (entry - stop)) if stop != entry else 1
    kelly = max(0, p - (1 - p) / R) if R > 0 else 0.01
    frac = min(max(kelly, 0.01), 0.2)
    budget = BUDGET_SPOT if mode == 'spot' else BUDGET_FUTURES
    size = round(budget * frac / entry, 6)
    quality = round(abs(hist_val), 2)
    return {
        'symbol': symbol,
        'direction': direction,
        'price': entry,
        'stop': stop,
        'take': take,
        'size': size,
        'quality': quality,
        'onchain': onchain,
        'sentiment': sentiment,
        'signal': True
    }

# === STATE STORAGE & RECORDING ===
def get_last_signal(mode, symbol):
    row = cursor.execute('SELECT direction FROM last_signals WHERE mode=? AND symbol=?', (mode, symbol)).fetchone()
    return row[0] if row else None

def update_last_signal(mode, symbol, direction):
    cursor.execute('INSERT OR REPLACE INTO last_signals(mode, symbol, direction, timestamp) VALUES(?,?,?,?)', (mode, symbol, direction, time.time()))
    conn.commit()

def record_trade(user_id, mode, symbol, side, price, amount, paper=True):
    cursor.execute('INSERT INTO trades(timestamp,user_id,mode,symbol,side,price,amount,paper) VALUES(?,?,?,?,?,?,?,?)', (time.time(), user_id, mode, symbol, side, price, amount, int(paper)))
    conn.commit()

# === AI COMMENT & FORMAT ===
def ai_comment(sig):
    prompt = (f"Signal {sig['symbol']} {sig['direction']} entry {sig['price']} size {sig['size']}\nOn-chain: {sig['onchain']}, Sentiment: {sig['sentiment']}\n")
    resp = openai.ChatCompletion.create(model='gpt-4o', messages=[{'role':'system','content':'You are crypto analyst.'},{'role':'user','content':prompt}], max_tokens=80, temperature=0.7)
    return resp.choices[0].message.content.strip()

def format_signal(sig, mode):
    typ = 'FUTURES' if mode == 'futures' else 'SPOT'
    text = (f"⚡ {typ} {sig['symbol']}\nDir: {sig['direction']} Size: {sig['size']}\nEntry: {sig['price']} | Stop: {sig['stop']} | Take: {sig['take']}\nQuality: {sig['quality']}\n<i>{ai_comment(sig)}</i>")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Monitor", callback_data=f"monitor_{mode}_{sig['symbol']}")]])
    return text, kb

# === ASYNC PARALLEL MONITOR ===
async def monitor_markets(app):
    tasks = []
    for mode, pairs in [('spot', SPOT_PAIRS), ('futures', FUTURES_PAIRS)]:
        for sym in pairs:
            tasks.append(process_symbol(app, sym, mode))
    await asyncio.gather(*tasks)

async def process_symbol(app, sym, mode):
    sig = generate_signal(sym, mode)
    if sig and sig['signal'] and sig['quality'] >= QUALITY_THRESHOLD:
        last = get_last_signal(mode, sym)
        if sig['direction'] != last:
            update_last_signal(mode, sym, sig['direction'])
            msg, kb = format_signal(sig, mode)
            users = cursor.execute('SELECT user_id FROM tokens WHERE user_id NOT NULL').fetchall()
            for (uid,) in users:
                await app.bot.send_message(uid, msg, parse_mode='HTML', reply_markup=kb)
                record_trade(uid, mode, sym, sig['direction'], sig['price'], sig['size'], True)

# === DASHBOARD ===
def run_dashboard():
    dash_app = dash.Dash(__name__)
    df = pd.read_sql('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 50', conn)
    dash_app.layout = html.Div([
        html.H2('Recent Trades'),
        dash_table.DataTable(columns=[{'name': col, 'id': col} for col in df.columns], data=df.to_dict('records'), page_size=10),
        dcc.Interval(id='interval', interval=60*1000, n_intervals=0)
    ])
    dash_app.run_server(host='0.0.0.0', port=8050)

# === TELEGRAM HANDLERS ===

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🟢 Spot Signals", callback_data="spot")],
        [InlineKeyboardButton("🔴 Futures Signals", callback_data="futures")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text("<b>Select Mode:</b>", parse_mode='HTML', reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text("<b>Select Mode:</b>", parse_mode='HTML', reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_chat.id
    if cursor.execute('SELECT 1 FROM tokens WHERE user_id=?', (uid,)).fetchone():
        # Show main menu for authorized users
        await main_menu(update, context)
    else:
        await update.message.reply_text('Activate: /activate <token>')

async def generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return await update.message.reply_text('Forbidden')
    token = str(uuid.uuid4())
    cursor.execute('INSERT INTO tokens(token) VALUES(?)', (token,))
    conn.commit()
    await update.message.reply_text(f'Token: {token}')

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        return await update.message.reply_text('Use: /activate <token>')
    token = context.args[0]
    if not cursor.execute('SELECT token FROM tokens WHERE token=? AND user_id IS NULL', (token,)).fetchone():
        return await update.message.reply_text('Invalid token')
    cursor.execute('UPDATE tokens SET user_id=?,username=?,activation_time=? WHERE token=?',
                   (update.effective_chat.id, update.effective_user.username, time.time(), token))
    conn.commit()
    # After activation, show menu
    await main_menu(update, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    uid = update.effective_chat.id
    if data in ('spot', 'futures'):
        # user selected mode, send initial signal now
        mode = data
        await process_symbol(app=context.application, sym=None, mode=mode)  # Immediate scan; sym=None means all
        await update.callback_query.answer()
    else:
        parts = data.split('_')
        action, mode, sym = parts[0], parts[1], '_'.join(parts[2:])
        if action == 'monitor':
            sig = generate_signal(sym, mode)
            status = 'No signal' if not sig else f"{sig['direction']} at {sig['price']}"
            await context.bot.send_message(uid, f"<b>{sym}</b>: {status}", parse_mode='HTML')
        await update.callback_query.answer()

# === MAIN ===
async def main():
    Thread(target=run_dashboard, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('generate_token', generate_token))
    app.add_handler(CommandHandler('activate', activate))
    app.add_handler(CallbackQueryHandler(button_handler))
    asyncio.create_task(ws_listener(app))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(monitor_markets(app)), 'interval', seconds=30)
    scheduler.start()
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
