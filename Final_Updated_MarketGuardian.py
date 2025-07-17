import os
import logging
import time
import uuid
import sqlite3
from datetime import datetime
import ccxt
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
)

# ==== Настройки ====
TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"
ADMIN_ID = 1407143951

api_keys = {
    "Binance": {
        "api_key": "7Jr5VPDXj22dQak9tUlJYFyM4v58hP7VarHBQoJPgfLn7qV4rJgzuyNCP8cBHqZx",
        "api_secret": "mc4htJRKnEJPAKMXERsv9l0S1w4MbLAuO8UjVUzMv3DPYY8nz5LAJ4K98CkGhuvu",
    }
}

FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT",
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "MATIC/USDT", "DOT/USDT", "BCH/USDT", "OP/USDT",
    "FIL/USDT", "TON/USDT", "WIF/USDT", "PEPE/USDT", "1000SATS/USDT", "SEI/USDT"
]

FUTURES_LEVERAGE = 10
SPOT_AMOUNT = 3000
FUTURES_BUDGET = 500

# ==== База данных ====
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
CREATE TABLE IF NOT EXISTS signals_log (
    user_id INTEGER,
    ts TIMESTAMP,
    type TEXT,
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

# ==== Binance ====
exchanges = {
    "Binance": ccxt.binance({
        "apiKey": api_keys["Binance"]["api_key"],
        "secret": api_keys["Binance"]["api_secret"],
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    }),
}
spot_exchange = ccxt.binance({
    "apiKey": api_keys["Binance"]["api_key"],
    "secret": api_keys["Binance"]["api_secret"],
    "enableRateLimit": True,
})

# ==== Access ====
def user_has_access(user_id):
    cursor.execute("SELECT user_id FROM tokens WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

# ==== Вспомогательные функции ====
def get_volatility(ticker):
    try:
        high = ticker['high']
        low = ticker['low']
        open_ = ticker['open']
        if open_ == 0: return 0
        return (high - low) / open_
    except: return 0

def now_time_str():
    return datetime.now().strftime('%H:%M')

def is_time_allowed():
    now = datetime.now().time()
    start = datetime.strptime("08:00", "%H:%M").time()
    end = datetime.strptime("00:00", "%H:%M").time()
    if start <= now or now <= end:  # 00:00 — это полночь
        return True
    return False

def explain_signal(pair, direction, change, vol, volume):
    # Индивидуальный комментарий под каждую пару
    base = f"{pair}: "
    trend = "сильное движение" if abs(change) > 1 else "умеренное движение"
    volatility = "высокая волатильность" if vol > 0.02 else "низкая волатильность"
    liquidity = "высокий объём" if volume > 1_000_000 else "обычный объём"
    if "BTC" in pair:
        comment = f"{base}Основание: {trend}, {volatility}, {liquidity}. На BTC часто формируются большие движения."
    elif "ETH" in pair:
        comment = f"{base}ETH реагирует на общерыночные тренды. {trend}, {liquidity} — хорошая точка входа."
    elif "SOL" in pair:
        comment = f"{base}SOL даёт быстрые импульсы — {trend}, {volatility}."
    elif "XRP" in pair:
        comment = f"{base}XRP часто идёт против рынка. Сейчас {trend}, {volatility}."
    else:
        comment = f"{base}{trend}, {volatility}, {liquidity}."
    if direction == "LONG":
        comment += " Возможен рост при продолжении импульса."
    else:
        comment += " Возможен откат при смене тренда."
    return comment

# ==== Главное меню ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_has_access(user_id):
        await send_main_menu(update, context)
    else:
        await update.message.reply_text("❌ У вас нет доступа. Пожалуйста, активируйте токен для использования бота.")

async def send_main_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("📊 Спот сигналы", callback_data="menu:spot")],
        [InlineKeyboardButton("⚡️ Фьючерсы (ручной выбор)", callback_data="menu:manual_futures")],
        [InlineKeyboardButton("🤖 Автопоиск (фьючерсы)", callback_data="menu:auto_futures")],
        [InlineKeyboardButton("🗂 Отчёт", callback_data="menu:portfolio")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "<b>MarketGuardian PRO</b>\n\n"
        "Выберите режим:\n"
        "📊 Спот сигналы\n"
        "⚡️ Фьючерсы (ручной)\n"
        "🤖 Автопоиск (фьючерсы)\n"
        "🗂 Отчёт/Журнал\n"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

async def generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет прав для генерации токенов.")
        return
    token = str(uuid.uuid4())
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
        cursor.execute(
            "UPDATE tokens SET user_id = ?, username = ?, activation_time = ? WHERE token = ?",
            (user_id, username, activation_time, token)
        )
        conn.commit()
        await update.message.reply_text("✅ Токен активирован! Теперь у вас есть доступ к боту.")
        await send_main_menu(update, context)
    else:
        await update.message.reply_text("❌ Неверный или уже использованный токен.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(":")
    logger.info(f"Button: {data}")
    if data[0] == "menu":
        if data[1] == "spot":
            await spot_signal_handler(update, context)
        elif data[1] == "manual_futures":
            await manual_futures_menu(update, context)
        elif data[1] == "auto_futures":
            await auto_futures_handler(update, context)
        elif data[1] == "portfolio":
            await portfolio_handler(update, context)
    elif data[0] == "select_futures":
        await send_manual_futures_signal(update, context, data[1])
    elif data[0] == "back":
        await send_main_menu(update, context)

# ==== СПОТ СИГНАЛЫ ====
async def spot_signal_handler(update, context):
    await update.callback_query.edit_message_text("⏳ Ищем лучшие сигналы на споте (Binance)...")
    pairs = spot_exchange.load_markets()
    spot_pairs = [s for s in pairs if "/USDT" in s and not pairs[s]['future'] and pairs[s]['active'] and pairs[s]['quote'] == "USDT"]
    spot_pairs = sorted(spot_pairs, key=lambda x: -pairs[x]['info'].get('quoteVolume', 0))[:20]
    signals = []
    for pair in spot_pairs:
        try:
            ticker = spot_exchange.fetch_ticker(pair)
            price = ticker['last']
            vol = get_volatility(ticker)
            change = ticker.get('percentage', 0)
            volume = ticker['quoteVolume']
            target_profit = 50  # $50
            stop_loss_pct = 0.03  # 3% от позиции
            direction = "LONG" if change > 0 else "SHORT"
            budget = SPOT_AMOUNT
            stop = round(price * (1 - stop_loss_pct if direction == "LONG" else 1 + stop_loss_pct), 4)
            take = price + target_profit if direction == "LONG" else price - target_profit
            # Фильтр: вход, если за день движение и объём выше среднего
            if abs(change) > 1 and volume > 1_000_000 and budget/price >= 10:
                comment = explain_signal(pair, direction, change, vol, volume)
                signals.append({
                    "pair": pair, "price": price, "direction": direction,
                    "take": take, "stop": stop, "comment": comment
                })
        except Exception as e:
            continue

    if signals:
        text = "<b>Лучшие спот-сигналы (Binance):</b>\n\n"
        for s in signals:
            text += (
                f"{s['pair']} | {s['direction']}\n"
                f"Вход: {s['price']}\n"
                f"Тейк: {round(s['take'],2)} | Стоп: {round(s['stop'],2)}\n"
                f"{s['comment']}\n\n"
            )
        keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(
            "Нет хороших входов на споте сейчас. Попробуй чуть позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]])
        )

# ==== ФЬЮЧЕРСЫ РУЧНО ====
async def manual_futures_menu(update, context):
    keyboard = []
    for pair in FUTURES_PAIRS:
        keyboard.append([InlineKeyboardButton(pair, callback_data=f"select_futures:{pair}")])
    keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")])
    await update.callback_query.edit_message_text(
        "<b>Выберите фьючерсную пару:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )

async def send_manual_futures_signal(update, context, pair):
    await update.callback_query.edit_message_text(f"⏳ Анализ {pair}...")
    try:
        exchange = exchanges["Binance"]
        ticker = exchange.fetch_ticker(pair)
        price = ticker['last']
        vol = get_volatility(ticker)
        change = ticker.get('percentage', 0)
        volume = ticker['quoteVolume']
        direction = "LONG" if change > 0 else "SHORT"
        stop_loss_pct = 0.10
        target_profit = 100  # $100
        stop = round(price * (1 - stop_loss_pct if direction == "LONG" else 1 + stop_loss_pct), 4)
        take = price + target_profit if direction == "LONG" else price - target_profit
        if is_time_allowed():
            comment = explain_signal(pair, direction, change, vol, volume)
            text = (
                f"⚡️ <b>Фьючерсы (Binance): {pair}</b>\n"
                f"Плечо: <b>{FUTURES_LEVERAGE}x</b>\n"
                f"Вход: <b>{price}</b>\n"
                f"Тейк: <b>{round(take,2)}</b> | Стоп: <b>{round(stop,2)}</b>\n"
                f"Направление: <b>{direction}</b>\n"
                f"{comment}"
            )
        else:
            text = "⚡️ Фьючерсные сигналы доступны только с 08:00 до 00:00 по Киеву/МСК."
    except Exception as e:
        text = f"Ошибка анализа {pair}: {e}"
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ==== ФЬЮЧЕРСЫ АВТО ====
async def auto_futures_handler(update, context):
    await update.callback_query.edit_message_text("⏳ Автопоиск лучших входов по фьючерсам...")
    pairs = exchanges["Binance"].load_markets()
    fut_pairs = [p for p in pairs if "/USDT" in p and pairs[p]['future'] and pairs[p]['active'] and pairs[p]['quote'] == "USDT"]
    signals = []
    for pair in fut_pairs:
        try:
            ticker = exchanges["Binance"].fetch_ticker(pair)
            price = ticker['last']
            vol = get_volatility(ticker)
            change = ticker.get('percentage', 0)
            volume = ticker['quoteVolume']
            direction = "LONG" if change > 0 else "SHORT"
            stop_loss_pct = 0.10
            target_profit = 100
            stop = round(price * (1 - stop_loss_pct if direction == "LONG" else 1 + stop_loss_pct), 4)
            take = price + target_profit if direction == "LONG" else price - target_profit
            if is_time_allowed() and abs(change) > 1 and volume > 1_000_000:
                comment = explain_signal(pair, direction, change, vol, volume)
                signals.append({
                    "pair": pair, "price": price, "direction": direction,
                    "take": take, "stop": stop, "comment": comment
                })
        except Exception as e:
            continue

    if signals:
        text = "<b>Топ фьючерсные сигналы (Binance):</b>\n\n"
        for s in signals:
            text += (
                f"{s['pair']} | {s['direction']}\n"
                f"Вход: {s['price']}\n"
                f"Тейк: {round(s['take'],2)} | Стоп: {round(s['stop'],2)}\n"
                f"{s['comment']}\n\n"
            )
        keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(
            "Сейчас нет топовых сигналов по твоим условиям. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]])
        )

# ==== ОТЧЁТ ====
async def portfolio_handler(update, context):
    user_id = update.callback_query.message.chat_id
    cursor.execute("SELECT * FROM signals_log WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    cnt = len(rows)
    avg = round(sum([row[8] for row in rows]) / cnt, 2) if cnt > 0 else 0
    text = (
        f"🗂 <b>Ваш отчёт:</b>\n"
        f"- Получено сигналов: <b>{cnt}</b>\n"
        f"- Средний результат по сделке: <b>{avg}$</b>\n"
        f"- Стратегия: тейк частями, стоп строго!\n"
        f"<i>Журнал сигналов скоро станет доступен прямо в боте!</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ==== ЕЖЕЧАСНЫЕ АВТОСИГНАЛЫ (по расписанию, топовые спот+фьючи) ====
async def hourly_signals_job(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = []
    cursor.execute("SELECT user_id FROM tokens WHERE user_id IS NOT NULL")
    for row in cursor.fetchall():
        chat_ids.append(row[0])
    for chat_id in chat_ids:
        # Спот
        try:
            pairs = spot_exchange.load_markets()
            spot_pairs = [s for s in pairs if "/USDT" in s and not pairs[s]['future'] and pairs[s]['active'] and pairs[s]['quote'] == "USDT"]
            spot_pairs = sorted(spot_pairs, key=lambda x: -pairs[x]['info'].get('quoteVolume', 0))[:20]
            best_signal = None
            best_score = -999
            for pair in spot_pairs:
                try:
                    ticker = spot_exchange.fetch_ticker(pair)
                    price = ticker['last']
                    vol = get_volatility(ticker)
                    change = ticker.get('percentage', 0)
                    score = change * vol * ticker['quoteVolume']
                    if score > best_score:
                        best_score = score
                        best_signal = {
                            "pair": pair, "price": price, "change": change, "vol": vol, "score": score
                        }
                except: continue
            if best_signal:
                direction = "LONG" if best_signal["change"] > 0 else "SHORT"
                stop_loss_pct = 0.03
                stop = round(best_signal["price"] * (1 - stop_loss_pct if direction == "LONG" else 1 + stop_loss_pct), 4)
                take = best_signal["price"] + 50 if direction == "LONG" else best_signal["price"] - 50
                comment = explain_signal(
                    best_signal["pair"], direction, best_signal["change"], best_signal["vol"], best_signal["score"]
                )
                text = (
                    f"⏰ <b>Ежечасный сигнал на спот (Binance)</b>\n"
                    f"Пара: <b>{best_signal['pair']}</b>\n"
                    f"Цена: <b>{best_signal['price']}</b>\n"
                    f"Тейк: <b>{round(take,2)}</b> | Стоп: <b>{round(stop,2)}</b>\n"
                    f"Изменение: <b>{round(best_signal['change'],2)}%</b>\n"
                    f"Волатильность: <b>{round(best_signal['vol'],4)}</b>\n"
                    f"{comment}\n"
                )
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except: pass
        # Фьючерсы
        try:
            pairs = exchanges["Binance"].load_markets()
            fut_pairs = [p for p in pairs if "/USDT" in p and pairs[p]['future'] and pairs[p]['active'] and pairs[p]['quote'] == "USDT"]
            best_signal = None
            best_score = -999
            for pair in fut_pairs:
                try:
                    ticker = exchanges["Binance"].fetch_ticker(pair)
                    vol = get_volatility(ticker)
                    price = ticker['last']
                    change = ticker.get('percentage', 0)
                    score = abs(change) * vol * ticker['quoteVolume']
                    if score > best_score:
                        best_score = score
                        best_signal = {
                            "pair": pair, "price": price, "change": change, "vol": vol, "score": score
                        }
                except: continue
            if best_signal and is_time_allowed():
                direction = "LONG" if best_signal["change"] > 0 else "SHORT"
                stop_loss_pct = 0.10
                stop = round(best_signal["price"] * (1 - stop_loss_pct if direction == "LONG" else 1 + stop_loss_pct), 4)
                take = best_signal["price"] + 100 if direction == "LONG" else best_signal["price"] - 100
                comment = explain_signal(
                    best_signal["pair"], direction, best_signal["change"], best_signal["vol"], best_signal["score"]
                )
                text = (
                    f"⏰ <b>Ежечасный автосигнал (Binance Фьючерсы)</b>\n"
                    f"Пара: <b>{best_signal['pair']}</b>\n"
                    f"Плечо: <b>{FUTURES_LEVERAGE}x</b>\n"
                    f"Вход: <b>{best_signal['price']}</b>\n"
                    f"Тейк: <b>{round(take,2)}</b> | Стоп: <b>{round(stop,2)}</b>\n"
                    f"Направление: <b>{direction}</b>\n"
                    f"Волатильность: <b>{round(best_signal['vol'],4)}</b>\n"
                    f"{comment}\n"
                )
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except: pass

# ==== MAIN ====
def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate_token", generate_token))
    application.add_handler(CommandHandler("activate", activate_token))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.job_queue.run_repeating(hourly_signals_job, interval=3600, first=15)
    application.run_polling()

if __name__ == "__main__":
    main()
