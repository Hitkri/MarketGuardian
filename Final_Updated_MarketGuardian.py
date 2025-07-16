import os
import logging
import time
import uuid
import sqlite3
import ccxt
import pandas as pd
import numpy as np
import ta
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
CRYPTO_PANIC_KEY = "aa2530c4353491b07bc491ec791fa2f78baa60c7"
COINGECKO_URL = "https://api.coingecko.com/api/v3"
COINMARKETCAL_KEY = "n7JjBHcraf566zaQb7Dtq9AHMQqt7kWM5z0FCeWY"

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
cursor.execute("""
CREATE TABLE IF NOT EXISTS portfolio (
    user_id INTEGER,
    pair TEXT,
    type TEXT,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    amount REAL,
    opened_at TIMESTAMP,
    closed_at TIMESTAMP,
    close_price REAL,
    pnl REAL,
    comment TEXT,
    status TEXT,
    PRIMARY KEY (user_id, pair, type, opened_at)
)
""")
conn.commit()

api_keys = {
    "OKX": {"api_key": "320601c1-25e4-4cee-9cac-73c3a1016ccd", "api_secret": "CCF6B5800EC886C200E686A3C3194AA5", "passphrase": "Minoas2020@"},
    "Binance": {"api_key": "7Jr5VPDXj22dQak9tUlJYFyM4v58hP7VarHBQoJPgfLn7qV4rJgzuyNCP8cBHqZx", "api_secret": "mc4htJRKnEJPAKMXERsv9l0S1w4MbLAuO8UjVUzMv3DPYY8nz5LAJ4K98CkGhuvu"},
}
exchanges = {
    "OKX": ccxt.okx({
        "apiKey": api_keys["OKX"]["api_key"], 
        "secret": api_keys["OKX"]["api_secret"], 
        "password": api_keys["OKX"]["passphrase"], 
        "options": {"defaultType": "future"}
    }),
    "Binance": ccxt.binance({
        "apiKey": api_keys["Binance"]["api_key"], 
        "secret": api_keys["Binance"]["api_secret"]
    }),
}

FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "LINK/USDT",
    "MATIC/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "SHIB/USDT", "OP/USDT", "TON/USDT",
    "PEPE/USDT", "SEI/USDT", "WIF/USDT", "UNI/USDT", "ARB/USDT", "LTC/USDT"
]

TOP_SPOT_PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "TON/USDT", "ADA/USDT",
    "AVAX/USDT", "LINK/USDT", "SHIB/USDT", "DOT/USDT", "TRX/USDT", "MATIC/USDT", "UNI/USDT",
    "PEPE/USDT", "NOT/USDT", "JUP/USDT", "TIA/USDT", "ENJ/USDT"
]

# ================== UTILS ==================
def user_has_access(user_id):
    cursor.execute("SELECT user_id FROM tokens WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def calc_signal_score(impulse, rsi, macd, stochrsi, news_score):
    # 0..10 (10 — максимально сильный)
    score = (
        min(impulse*90, 3.5) +    # до 3.5
        min(abs(rsi-50)/7, 2) +   # до 2
        min(abs(macd)*18, 2.5) +  # до 2.5
        min(abs(stochrsi-0.5)*8, 1.5) +  # до 1.5
        news_score                # до 1
    )
    return min(score, 10)

def get_news_sentiment(symbol):
    try:
        resp = requests.get(
            f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_KEY}&currencies={symbol}&filter=hot"
        )
        data = resp.json()
        news_count = len(data.get("results", []))
        keywords = ""
        if news_count > 0:
            hot_news = [x['title'] for x in data['results']]
            keywords = "; ".join(hot_news[:2])
        news_score = min(news_count/3, 1)
        return news_count, keywords, news_score
    except Exception as e:
        logger.error(f"CryptoPanic error: {e}")
        return 0, "", 0

def coingecko_is_hot(symbol):
    try:
        cg = requests.get(f"{COINGECKO_URL}/coins/markets", params={
            "vs_currency": "usd", "ids": "", "order": "market_cap_desc", "per_page": "250", "page": "1"
        }).json()
        coin = None
        for c in cg:
            if c['symbol'].lower() == symbol.lower():
                coin = c
                break
        if coin and coin['price_change_percentage_24h'] and abs(coin['price_change_percentage_24h']) > 6:
            return True
    except Exception as e:
        logger.error(f"CoinGecko error: {e}")
    return False

def coinmarketcal_event(symbol):
    try:
        headers = {'x-api-key': COINMARKETCAL_KEY}
        resp = requests.get(
            f"https://developers.coinmarketcal.com/v1/events?coins={symbol}&sortBy=date&max=1", headers=headers
        )
        res = resp.json()
        if "body" in res and res["body"]:
            return res["body"][0]["title"]
    except Exception as e:
        logger.error(f"CoinMarketCal error: {e}")
    return None

# ========== СПОТ ================
def spot_best_signal():
    exchange = exchanges["Binance"]
    usdt_spot_pairs = [p for p in TOP_SPOT_PAIRS if p in exchange.load_markets()]
    best_signal = None
    best_score = -999

    for pair in usdt_spot_pairs:
        try:
            ohlcv = exchange.fetch_ohlcv(pair, timeframe="5m", limit=100)
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
            if len(df) < 60: continue
            avg_vol = df['vol'][-20:].mean()
            if avg_vol * df['close'].iloc[-1] < 20000: continue
            df['MA30'] = df['close'].rolling(window=30).mean()
            df['RSI'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            df['ATR'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            macd = ta.trend.MACD(df['close'])
            df['MACD'] = macd.macd()
            df['MACD_diff'] = macd.macd_diff()
            stoch = ta.momentum.StochRSIIndicator(df['close'])
            df['StochRSI'] = stoch.stochrsi()

            last = df.iloc[-1]
            impulse = (last['close'] - df['close'].iloc[-3]) / df['close'].iloc[-3]
            if last['ATR'] / last['close'] < 0.003: continue
            if impulse < 0.012: continue
            if (last['close'] > last['MA30']) and (last['RSI'] < 74) and (last['MACD'] > 0):
                symbol = pair.replace('/USDT', '').upper()
                news_count, news_titles, news_score = get_news_sentiment(symbol)
                cg_hot = coingecko_is_hot(symbol)
                event = coinmarketcal_event(symbol)
                score = calc_signal_score(impulse, last['RSI'], last['MACD'], last['StochRSI'], news_score)
                if news_count > 0: score += 0.5
                if cg_hot: score += 0.5
                if event: score += 0.5
                if score > best_score:
                    best_signal = {
                        "pair": pair,
                        "entry": round(float(last["close"]), 4),
                        "score": round(score,2),
                        "rsi": round(last['RSI'],1),
                        "macd": round(last['MACD'],4),
                        "stochrsi": round(last['StochRSI'],3),
                        "news": f"{news_count} новостей: {news_titles}" if news_count else "",
                        "coingecko": "🚀 CoinGecko: сильный рост!" if cg_hot else "",
                        "event": f"CoinMarketCal: {event}" if event else "",
                        "comment": f"Импульс: {round(impulse*100,2)}% | RSI: {round(last['RSI'],1)}",
                        "stop_loss": round(last["close"] * 0.98, 4),
                        "take_profit": round(last["close"] * 1.022, 4),
                        "type": "SPOT"
                    }
                    best_score = score
        except Exception as e:
            logger.error(f"Spot signal error for {pair}: {e}")

    return best_signal

def get_signal_futures(pair):
    exchange = exchanges["Binance"]
    try:
        ohlcv = exchange.fetch_ohlcv(pair, timeframe="5m", limit=100)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
        avg_vol = df['vol'][-20:].mean()
        if avg_vol * df['close'].iloc[-1] < 20000: return None
        df['MA30'] = df['close'].rolling(window=30).mean()
        df['RSI'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        macd = ta.trend.MACD(df['close'])
        df['MACD'] = macd.macd()
        stoch = ta.momentum.StochRSIIndicator(df['close'])
        df['StochRSI'] = stoch.stochrsi()

        last = df.iloc[-1]
        impulse = (last['close'] - df['close'].iloc[-3]) / df['close'].iloc[-3]
        if (last['close'] > last['MA30']) and (last['RSI'] < 74) and (last['MACD'] > 0) and impulse > 0.012:
            entry = round(float(last["close"]), 4)
            leverage = min(max(int(300 / entry), 1), 20)
            stop_loss = round(entry * 0.98, 4)
            take_profit = round(entry * 1.022, 4)
            symbol = pair.replace('/USDT', '').upper()
            news_count, news_titles, news_score = get_news_sentiment(symbol)
            score = calc_signal_score(impulse, last['RSI'], last['MACD'], last['StochRSI'], news_score)
            return {
                "pair": pair, "signal": "LONG", "entry": entry, "stop_loss": stop_loss, "take_profit": take_profit,
                "leverage": leverage, "rsi": round(last["RSI"],1), "comment": f"Импульс {round(impulse*100,2)}%",
                "score": score, "type": "LONG"
            }
        if (last['close'] < last['MA30']) and (last['RSI'] > 25) and (last['MACD'] < 0) and impulse < -0.012:
            entry = round(float(last["close"]), 4)
            leverage = min(max(int(300 / entry), 1), 20)
            stop_loss = round(entry * 1.02, 4)
            take_profit = round(entry * 0.978, 4)
            symbol = pair.replace('/USDT', '').upper()
            news_count, news_titles, news_score = get_news_sentiment(symbol)
            score = calc_signal_score(-impulse, last['RSI'], last['MACD'], last['StochRSI'], news_score)
            return {
                "pair": pair, "signal": "SHORT", "entry": entry, "stop_loss": stop_loss, "take_profit": take_profit,
                "leverage": leverage, "rsi": round(last["RSI"],1), "comment": f"Импульс {round(impulse*100,2)}%",
                "score": score, "type": "SHORT"
            }
    except Exception as e:
        logger.error(f"Futures signal error for {pair}: {e}")
    return None

def get_best_futures_signal():
    exchange = exchanges["Binance"]
    all_markets = exchange.load_markets()
    usdt_futures_pairs = [m for m in all_markets if m.endswith('/USDT') and all_markets[m].get('future')]
    best_signal = None
    best_score = -999
    for pair in usdt_futures_pairs:
        signal = get_signal_futures(pair)
        if not signal: continue
        score = signal["score"]
        if score > best_score:
            best_signal = signal
            best_score = score
    return best_signal

# ============= PORTFOLIO =============
def add_signal_to_portfolio(user_id, pair, type, entry, stop_loss, take_profit, amount, comment):
    opened_at = int(time.time())
    cursor.execute("""
        INSERT OR IGNORE INTO portfolio
        (user_id, pair, type, entry, stop_loss, take_profit, amount, opened_at, status, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, pair, type, entry, stop_loss, take_profit, amount, opened_at, "open", comment))
    conn.commit()
    return opened_at

def close_portfolio_trade(user_id, pair, type, entry, price):
    now = int(time.time())
    cursor.execute("""
        SELECT * FROM portfolio WHERE user_id=? AND pair=? AND type=? AND entry=? AND status='open'
    """, (user_id, pair, type, entry))
    trade = cursor.fetchone()
    if not trade:
        return None
    amount = trade[6]
    pnl = round((price - entry) * amount, 2) if type == "LONG" or type == "SPOT" else round((entry - price) * amount, 2)
    cursor.execute("""
        UPDATE portfolio SET closed_at=?, close_price=?, pnl=?, status='closed'
        WHERE user_id=? AND pair=? AND type=? AND entry=? AND status='open'
    """, (now, price, pnl, user_id, pair, type, entry))
    conn.commit()
    return pnl

def get_open_signals(user_id):
    cursor.execute("""
        SELECT pair, type, entry, stop_loss, take_profit, amount, opened_at, comment FROM portfolio
        WHERE user_id=? AND status='open'
    """, (user_id,))
    return cursor.fetchall()

def get_report(user_id):
    cursor.execute("""
        SELECT * FROM portfolio WHERE user_id=?
    """, (user_id,))
    rows = cursor.fetchall()
    if not rows:
        return "Нет сделок в журнале."
    total = len(rows)
    wins = len([r for r in rows if r[-2] and r[-2]>0])
    losses = len([r for r in rows if r[-2] and r[-2]<0])
    avg_pnl = np.mean([r[-2] for r in rows if r[-2] is not None]) if total else 0
    sum_pnl = np.sum([r[-2] for r in rows if r[-2] is not None]) if total else 0
    return (
        f"Всего сделок: {total}\n"
        f"В плюсе: {wins} | В минусе: {losses}\n"
        f"Средний результат: {avg_pnl:.2f} $\n"
        f"Суммарно: {sum_pnl:.2f} $"
    )

# ============ SIGNAL MONITORING ===========
import asyncio

async def monitor_signal(context, user_id, signal, mode="futures"):
    pair = signal["pair"]
    entry = signal["entry"]
    stop_loss = signal["stop_loss"]
    take_profit = signal["take_profit"]
    type = signal.get("signal", signal.get("type"))
    amount = 300 / entry if entry else 0

    exchange = exchanges["Binance"]
    while True:
        await asyncio.sleep(20)
        ticker = exchange.fetch_ticker(pair)
        price = ticker["last"]
        hit = None
        if type in ("LONG", "SPOT"):
            if price >= take_profit:
                hit = ("take", price)
            elif price <= stop_loss:
                hit = ("stop", price)
        elif type == "SHORT":
            if price <= take_profit:
                hit = ("take", price)
            elif price >= stop_loss:
                hit = ("stop", price)
        if hit:
            kind, close_price = hit
            pnl = close_portfolio_trade(user_id, pair, type, entry, close_price)
            text = f"🚨 <b>{pair} ({type})</b>\n"
            if kind == "take":
                text += f"Тейк Профит выполнен! ✔️\n"
            else:
                text += f"Стоп-Лосс сработал! ❌\n"
            text += f"Вход: {entry}\nЗакрытие: {close_price}\nPnL на $300: <b>{pnl}$</b>"
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            await show_main_menu_by_id(context, user_id)
            break

# =============== UI ===================
def get_futures_buttons():
    keyboard = [
        [InlineKeyboardButton(pair, callback_data=f"futures:{pair}")] for pair in FUTURES_PAIRS
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_signal_buttons(signal=None):
    buttons = [[InlineKeyboardButton("🛑 Остановить мониторинг", callback_data="stop_signals")]]
    if signal:
        pair, entry, sigtype = signal["pair"], signal["entry"], signal.get("signal", signal.get("type"))
        buttons.append([InlineKeyboardButton("💼 Добавить в портфель", callback_data=f"add_portfolio:{pair}:{sigtype}:{entry}")])
    buttons.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)

def get_resume_buttons():
    keyboard = [
        [InlineKeyboardButton("🔄 Возобновить мониторинг", callback_data="resume_signals")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🟢 Рекомендации на спот (Binance)", callback_data="spot_autopick")],
        [InlineKeyboardButton("⚡️ Выбор пары (фьючерсы)", callback_data="futures_select")],
        [InlineKeyboardButton("🔥 Автопоиск лучших входов (фьючерсы)", callback_data="futures_autopick")],
        [InlineKeyboardButton("📊 Мой отчёт", callback_data="my_report")],
        [InlineKeyboardButton("💼 Мой портфель", callback_data="my_portfolio")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if getattr(update, "message", None):
        await update.message.reply_text("Выбери режим:", reply_markup=reply_markup)
    elif getattr(update, "callback_query", None):
        await update.callback_query.edit_message_text("Выбери режим:", reply_markup=reply_markup)

async def show_main_menu_by_id(context, chat_id):
    keyboard = [
        [InlineKeyboardButton("🟢 Рекомендации на спот (Binance)", callback_data="spot_autopick")],
        [InlineKeyboardButton("⚡️ Выбор пары (фьючерсы)", callback_data="futures_select")],
        [InlineKeyboardButton("🔥 Автопоиск лучших входов (фьючерсы)", callback_data="futures_autopick")],
        [InlineKeyboardButton("📊 Мой отчёт", callback_data="my_report")],
        [InlineKeyboardButton("💼 Мой портфель", callback_data="my_portfolio")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text="Главное меню:", reply_markup=reply_markup)

# ============== BOT ================
async def send_spot_signal(chat_id, context):
    await context.bot.send_message(chat_id=chat_id, text="⏳ Поиск лучших входов на споте, подожди 10-20 сек...")
    signal = spot_best_signal()
    if signal:
        text = f"""🟢 <b>Spot Buy Recommendation</b>
Пара: <b>{signal['pair']}</b>
Вход: <b>{signal['entry']}</b>
Тейк: <b>{signal['take_profit']}</b>
Стоп: <b>{signal['stop_loss']}</b>
Сила сигнала: <b>{signal['score']} / 10 ⭐️</b>
Комментарий: {signal['comment']}"""
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=get_signal_buttons(signal), parse_mode="HTML")
        add_signal_to_portfolio(chat_id, signal["pair"], "SPOT", signal["entry"], signal["stop_loss"], signal["take_profit"], 300/signal["entry"], signal['comment'])
        asyncio.create_task(monitor_signal(context, chat_id, signal, mode="spot"))
    else:
        await context.bot.send_message(chat_id=chat_id, text="Нет сильных спотовых входов сейчас.")
        await show_main_menu_by_id(context, chat_id)

async def send_futures_signal(chat_id, context, pair):
    signal = get_signal_futures(pair)
    if signal:
        text = f"""🔥 <b>Futures Signal</b>
Пара: <b>{signal['pair']}</b>
Тип: <b>{signal['signal']}</b>
Вход: <b>{signal['entry']}</b>
Тейк: <b>{signal['take_profit']}</b>
Стоп: <b>{signal['stop_loss']}</b>
Плечо: <b>x{signal['leverage']}</b>
Сила сигнала: <b>{round(signal['score'],2)} / 10 ⭐️</b>
Комментарий: {signal['comment']}"""
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=get_signal_buttons(signal), parse_mode="HTML")
        add_signal_to_portfolio(chat_id, signal["pair"], signal["signal"], signal["entry"], signal["stop_loss"], signal["take_profit"], 300/signal["entry"], signal['comment'])
        asyncio.create_task(monitor_signal(context, chat_id, signal))
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"Нет сильного сигнала по {pair} сейчас.")
        await show_main_menu_by_id(context, chat_id)

async def send_best_futures_signal(chat_id, context):
    signal = get_best_futures_signal()
    if signal:
        text = f"""🔥 <b>Лучший фьючерс-сигнал (автопоиск)</b>
Пара: <b>{signal['pair']}</b>
Тип: <b>{signal['signal']}</b>
Вход: <b>{signal['entry']}</b>
Тейк: <b>{signal['take_profit']}</b>
Стоп: <b>{signal['stop_loss']}</b>
Плечо: <b>x{signal['leverage']}</b>
Сила сигнала: <b>{round(signal['score'],2)} / 10 ⭐️</b>
Комментарий: {signal['comment']}"""
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=get_signal_buttons(signal), parse_mode="HTML")
        add_signal_to_portfolio(chat_id, signal["pair"], signal["signal"], signal["entry"], signal["stop_loss"], signal["take_profit"], 300/signal["entry"], signal['comment'])
        asyncio.create_task(monitor_signal(context, chat_id, signal))
    else:
        await context.bot.send_message(chat_id=chat_id, text="Сейчас нет уверенного входа на фьючерсах.")
        await show_main_menu_by_id(context, chat_id)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(":")
    logger.info(f"Нажата кнопка: {query.data}")

    if data[0] == "stop_signals":
        if 'job' in context.user_data:
            context.user_data['job'].schedule_removal()
            del context.user_data['job']
        await query.answer("Мониторинг остановлен.")
        await show_main_menu(update, context)
        return

    if data[0] == "resume_signals":
        await query.answer("Мониторинг возобновлён.")
        await show_main_menu(update, context)
        return

    if data[0] == "main_menu":
        await show_main_menu(update, context)
        return

    if query.data == "spot_autopick":
        chat_id = query.message.chat.id
        await send_spot_signal(chat_id, context)
        return

    if query.data == "futures_select":
        await query.edit_message_text("Выбери пару для мониторинга (фьючерсы):", reply_markup=get_futures_buttons())
        return

    if query.data == "futures_autopick":
        chat_id = query.message.chat.id
        await send_best_futures_signal(chat_id, context)
        return

    if data[0] == "futures" and len(data) == 2:
        chat_id = query.message.chat.id
        pair = data[1]
        await send_futures_signal(chat_id, context, pair)
        return

    if query.data == "my_report":
        chat_id = query.message.chat.id
        await query.edit_message_text(get_report(chat_id), reply_markup=get_resume_buttons())
        return

    if query.data == "my_portfolio":
        chat_id = query.message.chat.id
        rows = get_open_signals(chat_id)
        if not rows:
            await query.edit_message_text("Открытых позиций нет.", reply_markup=get_resume_buttons())
        else:
            text = "🟩 Открытые сделки:\n"
            for row in rows:
                text += f"{row[0]} ({row[1]}) | Вход: {row[2]} | SL: {row[3]} | TP: {row[4]}\n"
            await query.edit_message_text(text, reply_markup=get_resume_buttons())
        return

    if data[0] == "add_portfolio" and len(data) == 4:
        chat_id = query.message.chat.id
        pair, sigtype, entry = data[1], data[2], float(data[3])
        add_signal_to_portfolio(chat_id, pair, sigtype, entry, entry*0.98, entry*1.022, 300/entry, "Добавлено вручную")
        await query.answer("Добавлено в портфель!")
        await show_main_menu(update, context)

async def generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = 1407143951
    if update.message.chat_id != admin_id:
        await update.message.reply_text("❌ У вас нет прав для генерации токенов.")
        return
    token = str(uuid.uuid4())
    logger.info(f"Генерация токена: {token}")
    cursor.execute("INSERT INTO tokens (token) VALUES (?)", (token,))
    conn.commit()
    await update.message.reply_text(f"✅ Новый токен: {token}")

async def activate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    username = update.message.chat.username
    if len(context.args) != 1:
        await update.message.reply_text("❌ Используйте: /activate <токен>")
        return
    token = context.args[0]
    cursor.execute("SELECT token FROM tokens WHERE token = ? AND user_id IS NULL", (token,))
    if cursor.fetchone():
        activation_time = time.time()
        cursor.execute("UPDATE tokens SET user_id = ?, username = ?, activation_time = ? WHERE token = ?", (user_id, username, activation_time, token))
        conn.commit()
        await update.message.reply_text("✅ Токен активирован! Теперь у вас есть доступ к боту.")
        await show_main_menu(update, context)
    else:
        await update.message.reply_text("❌ Неверный или уже использованный токен.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_has_access(user_id):
        await show_main_menu(update, context)
    else:
        await update.message.reply_text("❌ У вас нет доступа. Пожалуйста, активируйте токен для использования бота.")

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    if application.job_queue is None:
        application.job_queue = JobQueue()
        application.job_queue.set_application(application)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate_token", generate_token))
    application.add_handler(CommandHandler("activate", activate_token))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.run_polling()

if __name__ == "__main__":
    main()
