import os
import logging
import time
import uuid
import sqlite3
import ccxt
import requests
import openai
import random
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# === ТОКЕНЫ И КЛЮЧИ ===
TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
OPENAI_API_KEY = "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"
CRYPTO_PANIC_KEY = "aa2530c4353491b07bc491ec791fa2f78baa60c7"
COINMARKETCAL_KEY = "n7JjBHcraf566zaQb7Dtq9AHMQqt7kWM5z0FCeWY"
openai.api_key = OPENAI_API_KEY

BUDGET_FUTURES = 500   # для фьючей
BUDGET_SPOT = 3000     # для спота

ADMIN_ID = 1407143951

FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "MATIC/USDT",
    "SHIB/USDT", "DOT/USDT", "OP/USDT", "TON/USDT", "ARB/USDT",
    "SEI/USDT", "SUI/USDT", "LTC/USDT", "BCH/USDT", "INJ/USDT"
]
SPOT_PAIRS = FUTURES_PAIRS.copy()

# === ЛОГИ ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === БД ДЛЯ ТОКЕНОВ И ЖУРНАЛА ===
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
CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT,
    mode TEXT,
    pair TEXT,
    direction TEXT,
    entry REAL,
    take REAL,
    stop REAL,
    result REAL,
    comment TEXT
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
        [InlineKeyboardButton("📈 Портфель/Журнал", callback_data="journal")],
        [InlineKeyboardButton("⚙️ Аналитика/Отчёты", callback_data="analytics")]
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
    elif data.startswith("futures_pair_"):
        pair = data.split("futures_pair_")[1]
        await send_futures_signal(user_id, pair, context)
    elif data == "main_menu":
        await main_menu(update, context)
    elif data == "journal":
        await show_journal(user_id, context)
    elif data == "analytics":
        await show_analytics(user_id, context)

async def choose_futures_pair(user_id, query):
    keyboard = [
        [InlineKeyboardButton(pair, callback_data=f"futures_pair_{pair}")]
        for pair in FUTURES_PAIRS
    ]
    # По 2 кнопки в ряд
    buttons = [keyboard[i:i+2] for i in range(0, len(keyboard), 2)]
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("Выбери пару для сигнала:", reply_markup=markup)

# === SPOT SIGNAL ===
async def send_spot_signal(user_id, context):
    best_signal = None
    best_score = -100
    best_pair = ""
    for pair in SPOT_PAIRS:
        signal = analyze_pair(pair, budget=BUDGET_SPOT, mode="spot")
        if signal["score"] > best_score:
            best_signal = signal
            best_pair = pair
            best_score = signal["score"]
    comment = get_ai_comment(best_pair, best_signal)
    msg = make_signal_message(best_signal, best_pair, "СПОТ", comment)
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
    save_journal(user_id, "spot", best_pair, best_signal, comment)

# === FUTURES SIGNAL ===
async def send_futures_signal(user_id, pair, context):
    signal = analyze_pair(pair, budget=BUDGET_FUTURES, mode="futures")
    comment = get_ai_comment(pair, signal)
    msg = make_signal_message(signal, pair, "ФЬЮЧЕРСЫ", comment)
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
    save_journal(user_id, "futures", pair, signal, comment)

# === ANALYTICS/ЖУРНАЛ ===
def save_journal(user_id, mode, pair, signal, comment):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute("""INSERT INTO trade_journal (user_id, date, mode, pair, direction, entry, take, stop, result, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, now, mode, pair, signal["direction"], signal["price"], signal["take"], signal["stop"], 0, comment))
    conn.commit()

async def show_journal(user_id, context):
    cursor.execute("SELECT date,mode,pair,direction,entry,take,stop,result FROM trade_journal WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        await context.bot.send_message(chat_id=user_id, text="Портфель/журнал пуст.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))
        return
    text = "<b>Последние сигналы:</b>\n"
    for r in rows:
        text += f"<b>{r[0]} {r[1].upper()} {r[2]}</b>\n{r[3]} Вход: {r[4]} Тейк: {r[5]} Стоп: {r[6]} PnL: {r[7]}\n"
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

async def show_analytics(user_id, context):
    cursor.execute("SELECT COUNT(*), SUM(result) FROM trade_journal WHERE user_id=?", (user_id,))
    count, total = cursor.fetchone()
    text = f"<b>Всего сигналов:</b> {count}\n<b>Суммарный PnL:</b> {total if total else 0}\n"
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]))

# === СИГНАЛ-ГЕНЕРАТОР ===
def analyze_pair(pair, budget, mode="futures"):
    try:
        ticker = binance.fetch_ticker(pair)
        price = float(ticker["last"])
        open_ = float(ticker["open"])
        high = float(ticker["high"])
        low = float(ticker["low"])
        volume = float(ticker["quoteVolume"])
        direction = "LONG" if price > open_ else "SHORT"
        # risk/stop/take
        risk = round(budget * 0.1, 2)
        take_profit = round(price * (1.3 if direction == "LONG" else 0.7), 3)
        stop_loss = round(price * (0.9 if direction == "LONG" else 1.1), 3)
        # качество сигнала (учитывает тренд, движение, объём, новости)
        news_impact = get_news_impact(pair)
        trend_score = (price - open_) / open_ * 100
        vol_score = min(10, volume / 1000000)
        score = trend_score + vol_score + news_impact
        leverage = 5 if "BTC" in pair or "ETH" in pair else 10
        return {
            "price": price, "direction": direction, "stop": stop_loss,
            "take": take_profit, "leverage": leverage, "score": score,
            "budget": budget
        }
    except Exception:
        return {
            "price": 0, "direction": "NONE", "stop": 0, "take": 0,
            "leverage": 1, "score": 0, "budget": budget
        }

# === NEWS IMPACT ===
def get_news_impact(pair):
    try:
        symbol = pair.split("/")[0]
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_KEY}&currencies={symbol}&filter=rising"
        r = requests.get(url, timeout=10)
        data = r.json()
        # если есть свежие хорошие новости — +5 к score, плохие — -5
        for n in data.get("results", []):
            if "bullish" in n["tags"]: return 5
            if "bearish" in n["tags"]: return -5
        return 0
    except: return 0

# === AI-КОММЕНТАРИЙ ===
def get_ai_comment(pair, signal):
    prompt = f"""
    Проанализируй сигнал для {pair} на {signal['direction']}: Цена: {signal['price']}, Плечо: {signal['leverage']}, Тейк: {signal['take']}, Стоп: {signal['stop']}, Score: {signal['score']}. Напиши почему он может быть сильным или слабым, и стоит ли входить!
    """
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Ты опытный крипто-трейдер."}, {"role": "user", "content": prompt}],
            max_tokens=90, temperature=0.5
        )
        return response.choices[0].message["content"]
    except Exception:
        return "Комментарий недоступен."

# === ФОРМАТ СИГНАЛА ===
def make_signal_message(signal, pair, typ, comment):
    return (f"<b>⚡ {typ} ({pair})</b>\n"
            f"Плечо: <b>{signal['leverage']}</b>\n"
            f"Вход: <b>{signal['price']}</b>\n"
            f"Тейк: <b>{signal['take']}</b> | Стоп: <b>{signal['stop']}</b>\n"
            f"Направление: <b>{signal['direction']}</b>\n"
            f"Score: <b>{signal['score']:.1f}</b>\n"
            f"<i>{comment}</i>")

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
