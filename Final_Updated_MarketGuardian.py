import os
import logging
import time
import uuid
import sqlite3
import ccxt
import random
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
ADMIN_ID = 1407143951

BUDGET_FUTURES = 500
BUDGET_SPOT = 3000

FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USUSDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "MATIC/USDT",
    "SHIB/USDT", "DOT/USDT", "OP/USDT", "TON/USDT", "ARB/USDT",
    "SEI/USDT", "SUI/USDT", "LTC/USDT", "BCH/USDT", "INJ/USDT"
]

binance = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})
binance_spot = ccxt.binance({"enableRateLimit": True})

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
cursor.execute("""
CREATE TABLE IF NOT EXISTS portfolio (
    user_id INTEGER,
    pair TEXT,
    side TEXT,
    entry REAL,
    stop REAL,
    take REAL,
    leverage INTEGER,
    typ TEXT,
    time TIMESTAMP
)
""")
conn.commit()

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
        [InlineKeyboardButton("🟢 Сильные сигналы СПОТ", callback_data="spot_recommend")],
        [InlineKeyboardButton("💎 Фьючерсы (топ-20)", callback_data="futures_manual")],
        [InlineKeyboardButton("⏰ Включить автосигналы (ежечасно)", callback_data="autotrade")],
        [InlineKeyboardButton("📊 Портфель/история", callback_data="portfolio")],
        [InlineKeyboardButton("📈 Аналитика", callback_data="analytics")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text("<b>Выбери режим:</b>", reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text("<b>Выбери режим:</b>", reply_markup=reply_markup, parse_mode="HTML")

# ========== CALLBACK ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.message.chat.id

    if data == "spot_recommend":
        await send_spot_signal(user_id, context)
    elif data == "futures_manual":
        await choose_futures_pair(user_id, query)
    elif data.startswith("futures_pair_"):
        pair = data.split("futures_pair_")[1]
        await send_futures_signal(user_id, pair, context)
    elif data == "portfolio":
        await show_portfolio(user_id, context)
    elif data == "analytics":
        await show_analytics(user_id, context)
    elif data == "autotrade":
        await start_autotrade(user_id, context)
    elif data == "stop_auto":
        context.user_data['auto'] = False
        await query.edit_message_text("Автосигналы остановлены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
    elif data == "main_menu":
        await main_menu(update, context)

async def choose_futures_pair(user_id, query):
    keyboard = [
        [InlineKeyboardButton(pair, callback_data=f"futures_pair_{pair}")]
        for pair in FUTURES_PAIRS
    ]
    buttons = [keyboard[i:i+2] for i in range(0, len(keyboard), 2)]
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("Выбери пару для сигнала:", reply_markup=markup)

def get_liquid_spot_pairs(limit=30):
    try:
        markets = binance_spot.fetch_tickers()
        pairs = []
        for pair, t in markets.items():
            if pair.endswith("/USDT"):
                vol = t['quoteVolume']
                if vol and vol > 10000000:
                    pairs.append((pair, vol))
        pairs = sorted(pairs, key=lambda x: x[1], reverse=True)
        return [p[0] for p in pairs[:limit]]
    except Exception:
        return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]

def spot_analyze(pair, budget):
    try:
        ohlcv = binance_spot.fetch_ohlcv(pair, timeframe="1h", limit=20)
        closes = [x[4] for x in ohlcv]
        price = closes[-1]
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes) / 20
        rsi = calc_rsi(closes)
        if price > ma10 > ma20 and 58 < rsi < 72:
            stop = round(price * 0.97, 4)
            take = round(price * 1.03, 4)
            return {
                "pair": pair, "price": price, "stop": stop, "take": take,
                "side": "BUY", "leverage": 1, "rsi": rsi, "ma10": ma10, "ma20": ma20, "budget": budget
            }
    except Exception:
        pass
    return None

async def send_spot_signal(user_id, context):
    pairs = get_liquid_spot_pairs(30)
    signals = []
    for pair in pairs:
        sig = spot_analyze(pair, BUDGET_SPOT)
        if sig:
            signals.append(sig)
    if not signals:
        msg = "Сейчас нет сильных сетапов на споте. Попробуй позже!"
    else:
        best = max(signals, key=lambda x: x['rsi'])
        save_portfolio(user_id, best, "spot")
        msg = make_signal_msg(best, "СПОТ")
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

def futures_analyze(pair, budget):
    try:
        ohlcv = binance.fetch_ohlcv(pair, timeframe="1h", limit=20)
        closes = [x[4] for x in ohlcv]
        price = closes[-1]
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes) / 20
        rsi = calc_rsi(closes)
        direction = None
        if ma10 > ma20 and rsi > 58:
            direction = "LONG"
            stop = round(price * 0.97, 4)
            take = round(price * 1.03, 4)
        elif ma10 < ma20 and rsi < 42:
            direction = "SHORT"
            stop = round(price * 1.03, 4)
            take = round(price * 0.97, 4)
        else:
            return None
        leverage = 10 if "BTC" not in pair and "ETH" not in pair else 5
        return {
            "pair": pair, "price": price, "stop": stop, "take": take,
            "side": direction, "leverage": leverage, "rsi": rsi, "ma10": ma10, "ma20": ma20, "budget": budget
        }
    except Exception:
        pass
    return None

async def send_futures_signal(user_id, pair, context):
    sig = futures_analyze(pair, BUDGET_FUTURES)
    if not sig:
        msg = "Нет сильного сигнала для этой пары сейчас. Попробуй позже!"
    else:
        save_portfolio(user_id, sig, "futures")
        msg = make_signal_msg(sig, "ФЬЮЧЕРСЫ")
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

def save_portfolio(user_id, sig, typ):
    cursor.execute("""
        INSERT INTO portfolio (user_id, pair, side, entry, stop, take, leverage, typ, time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, sig['pair'], sig['side'], sig['price'], sig['stop'], sig['take'], sig['leverage'], typ, datetime.now()))
    conn.commit()

async def show_portfolio(user_id, context):
    cursor.execute("SELECT pair, side, entry, stop, take, leverage, typ, time FROM portfolio WHERE user_id=? ORDER BY time DESC LIMIT 15", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        msg = "Портфель пуст."
    else:
        msg = "<b>Последние сигналы:</b>\n"
        for row in rows:
            msg += (f"{row[7][5:16]} | <b>{row[0]}</b> [{row[6].upper()}] — {row[1]}\n"
                    f"Вход: {row[2]} | Стоп: {row[3]} | Тейк: {row[4]} | Плечо: {row[5]}\n\n")
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

async def show_analytics(user_id, context):
    cursor.execute("SELECT entry, take, stop, side, typ FROM portfolio WHERE user_id=? ORDER BY time DESC LIMIT 50", (user_id,))
    rows = cursor.fetchall()
    profit, loss, win, lose, long, short = 0,0,0,0,0,0
    for entry, take, stop, side, typ in rows:
        if side == "LONG" or side == "BUY":
            diff = take - entry
            long += 1
        else:
            diff = entry - take
            short += 1
        if diff > 0:
            profit += diff
            win += 1
        else:
            loss += abs(diff)
            lose += 1
    total = len(rows)
    msg = f"<b>Аналитика трейдера за {total} сигналов:</b>\n"
    msg += f"✅ В плюс: {win} | ❌ В минус: {lose}\n"
    msg += f"Лонг/Бай: {long} | Шорт: {short}\n"
    msg += f"<b>Суммарный профит:</b> <code>{profit-loss:.2f}</code>\n"
    msg += f"<b>Средний профит за сигнал:</b> <code>{(profit-loss)/total:.3f}</code>" if total else ""
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

async def start_autotrade(user_id, context):
    context.user_data['auto'] = True
    await context.bot.send_message(chat_id=user_id, text="Автосигналы включены! Бот будет присылать лучшие сигналы каждый час.\nОстановить: /stop или кнопка ниже.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Остановить автосигналы", callback_data="stop_auto")],[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
    await autotrade_signals(user_id, context)

async def autotrade_signals(user_id, context):
    while context.user_data.get('auto', False):
        # Spot
        pairs = get_liquid_spot_pairs(15)
        signals = []
        for pair in pairs:
            sig = spot_analyze(pair, BUDGET_SPOT)
            if sig:
                signals.append(sig)
        if signals:
            best = max(signals, key=lambda x: x['rsi'])
            save_portfolio(user_id, best, "spot")
            msg = make_signal_msg(best, "СПОТ (Auto)")
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
        # Futures
        fut_signals = []
        for pair in FUTURES_PAIRS:
            sig = futures_analyze(pair, BUDGET_FUTURES)
            if sig:
                fut_signals.append(sig)
        if fut_signals:
            best = max(fut_signals, key=lambda x: abs(x['rsi']-50))
            save_portfolio(user_id, best, "futures")
            msg = make_signal_msg(best, "ФЬЮЧЕРСЫ (Auto)")
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
        await asyncio.sleep(3600)  # раз в час

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return 50
    deltas = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
    ups = [d for d in deltas if d > 0]
    downs = [-d for d in deltas if d < 0]
    avg_gain = sum(ups)/period if ups else 0.0001
    avg_loss = sum(downs)/period if downs else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def make_signal_msg(sig, typ):
    return (f"<b>⚡ {typ} сигнал</b> <b>{sig['pair']}</b>\n"
            f"🔸 Направление: <b>{sig['side']}</b>\n"
            f"🔸 Вход: <b>{sig['price']}</b>\n"
            f"🔸 Стоп: <b>{sig['stop']}</b>  Тейк: <b>{sig['take']}</b>\n"
            f"🔸 Плечо: <b>{sig['leverage']}</b>\n"
            f"RSI: <b>{sig['rsi']:.1f}</b> | MA10: <b>{sig['ma10']:.1f}</b> | MA20: <b>{sig['ma20']:.1f}</b>")

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("generate_token", generate_token))
    app.add_handler(CommandHandler("activate", activate_token))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
