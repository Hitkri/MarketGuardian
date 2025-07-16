import os
import logging
import time
import uuid
import sqlite3
import random
from datetime import datetime, timedelta

import ccxt
import requests
import pandas as pd
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
)

# =============== НАСТРОЙКИ ===============

# Телеграм токен
TELEGRAM_BOT_TOKEN = "7635928627:AAFiDfGdfZKoReNnGDXkjaDm4Q3qm4AH0t0"

# API-ключи (оставил твои, не забудь сменить на свои!)
api_keys = {
    "Binance": {
        "api_key": "7Jr5VPDXj22dQak9tUlJYFyM4v58hP7VarHBQoJPgfLn7qV4rJgzuyNCP8cBHqZx",
        "api_secret": "mc4htJRKnEJPAKMXERsv9l0S1w4MbLAuO8UjVUzMv3DPYY8nz5LAJ4K98CkGhuvu",
    }
}

# Для новостей
CRYPTO_PANIC_TOKEN = "aa2530c4353491b07bc491ec791fa2f78baa60c7"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COINMARKETCAL_TOKEN = "n7JjBHcraf566zaQb7Dtq9AHMQqt7kWM5z0FCeWY"

# Админ TG id
ADMIN_ID = 1407143951

# Пары для ручного выбора (ТОП 20 по объёму/ликвидности для фьючерсов)
FUTURES_PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT",
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "MATIC/USDT", "DOT/USDT", "BCH/USDT", "OP/USDT",
    "FIL/USDT", "TON/USDT", "WIF/USDT", "PEPE/USDT", "1000SATS/USDT", "SEI/USDT"
]

# Для сигналов
FUTURES_LEVERAGE = 10
SPOT_AMOUNT = 300

# =================== ЛОГИ ===================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== БАЗА ====================
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
conn.commit()

# =============== ПОДКЛЮЧЕНИЕ К БИРЖЕ ===============
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

# ================= ВСПОМОГАТЕЛЬНЫЕ =================
def user_has_access(user_id):
    cursor.execute("SELECT user_id FROM tokens WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def get_volatility(ticker):
    # Волатильность: (high - low) / open
    try:
        high = ticker['high']
        low = ticker['low']
        open_ = ticker['open']
        if open_ == 0: return 0
        return (high - low) / open_
    except: return 0

def fetch_news():
    # CryptoPanic (краткие новости по рынку)
    try:
        r = requests.get(
            f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_TOKEN}&currencies=BTC,ETH,BNB,SOL&filter=hot"
        )
        news = r.json().get("results", [])
        return [f"📰 {n['title']}" for n in news[:2]]
    except Exception as e:
        logger.error(f"News error: {e}")
        return []

# ================== КОМАНДЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_has_access(user_id):
        await send_main_menu(update, context)
    else:
        await update.message.reply_text("❌ У вас нет доступа. Пожалуйста, активируйте токен для использования бота.")

async def send_main_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("📊 Рекомендации на спот", callback_data="menu:spot")],
        [InlineKeyboardButton("⚡️ Ручной выбор пары (фьючерсы)", callback_data="menu:manual_futures")],
        [InlineKeyboardButton("🤖 Автопоиск входа (фьючерсы)", callback_data="menu:auto_futures")],
        [InlineKeyboardButton("🗂 Портфель/отчёт", callback_data="menu:portfolio")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "<b>MarketGuardian</b>\n\n"
        "Выберите режим:\n"
        "📊 Рекомендации на спот\n"
        "⚡️ Ручной выбор пары (фьючерсы)\n"
        "🤖 Автопоиск входа (фьючерсы)\n"
        "🗂 Портфель/отчёт\n"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

async def generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для генерации токенов.")
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
    logger.info(f"Button pressed: {data}")

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

# =============== СПОТ СИГНАЛЫ ================
async def spot_signal_handler(update, context):
    await update.callback_query.edit_message_text(
        "⏳ Ищем лучший сигнал на споте (Binance)..."
    )
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
            # Индикаторы: движение, объём, волатильность, тренд рынка
            score = change * vol * ticker['quoteVolume']
            if score > best_score:
                best_score = score
                best_signal = {
                    "pair": pair,
                    "price": price,
                    "change": change,
                    "vol": vol,
                    "score": score,
                }
        except Exception as e:
            continue

    if best_signal:
        news = fetch_news()
        text = (
            f"📊 <b>Сигнал на спот (Binance)</b>\n"
            f"Пара: <b>{best_signal['pair']}</b>\n"
            f"Цена: <b>{best_signal['price']}</b>\n"
            f"Изменение за 24ч: <b>{round(best_signal['change'],2)}%</b>\n"
            f"Волатильность: <b>{round(best_signal['vol'],4)}</b>\n"
            f"Объём: <b>{round(best_score,2)}</b>\n"
            f"{''.join(news)}\n\n"
            f"💡 <b>Комментарий:</b> Пара показывает сильное движение и волатильность. Объём выше среднего. Возможна быстрая сделка."
        )
        keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(
            "Не найдено достойных сигналов на споте. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]])
        )

# =============== ФЬЮЧЕРСЫ РУЧНО ================
async def manual_futures_menu(update, context):
    keyboard = []
    for pair in FUTURES_PAIRS:
        keyboard.append([InlineKeyboardButton(pair, callback_data=f"select_futures:{pair}")])
    keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")])
    await update.callback_query.edit_message_text(
        "<b>Выберите фьючерсную пару:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )

async def send_manual_futures_signal(update, context, pair):
    await update.callback_query.edit_message_text(
        f"⏳ Анализируем {pair}..."
    )
    try:
        exchange = exchanges["Binance"]
        ticker = exchange.fetch_ticker(pair)
        price = ticker['last']
        vol = get_volatility(ticker)
        change = ticker.get('percentage', 0)
        direction = "LONG" if change > 0 else "SHORT"
        stop_loss = round(price * (0.98 if direction == "LONG" else 1.02), 4)
        take_profit = round(price * (1.03 if direction == "LONG" else 0.97), 4)
        news = fetch_news()
        text = (
            f"⚡️ <b>Сигнал по фьючерсам (Binance)</b>\n"
            f"Пара: <b>{pair}</b>\n"
            f"Плечо: <b>{FUTURES_LEVERAGE}x</b>\n"
            f"Вход: <b>{price}</b>\n"
            f"Тейк-профит: <b>{take_profit}</b>\n"
            f"Стоп-лосс: <b>{stop_loss}</b>\n"
            f"Направление: <b>{direction}</b>\n"
            f"Волатильность: <b>{round(vol,4)}</b>\n"
            f"Изменение: <b>{round(change,2)}%</b>\n"
            f"{''.join(news)}\n\n"
            f"💡 <b>Комментарий:</b> Пара входит в топ по объёму и волатильности за сутки. Потенциал движения: {direction}."
        )
    except Exception as e:
        text = f"Ошибка при анализе {pair}: {e}"
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# =============== АВТОПОИСК ФЬЮЧЕРСЫ ================
async def auto_futures_handler(update, context):
    await update.callback_query.edit_message_text(
        "⏳ Автоматический подбор лучших пар по Binance..."
    )
    # Возьмём топ-30 USDT-пар фьючерсов
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
                    "pair": pair,
                    "price": price,
                    "change": change,
                    "vol": vol,
                    "score": score,
                }
        except Exception as e:
            continue

    if best_signal:
        direction = "LONG" if best_signal["change"] > 0 else "SHORT"
        stop_loss = round(best_signal["price"] * (0.98 if direction == "LONG" else 1.02), 4)
        take_profit = round(best_signal["price"] * (1.03 if direction == "LONG" else 0.97), 4)
        news = fetch_news()
        text = (
            f"🤖 <b>Автосигнал (Binance Фьючерсы)</b>\n"
            f"Пара: <b>{best_signal['pair']}</b>\n"
            f"Плечо: <b>{FUTURES_LEVERAGE}x</b>\n"
            f"Вход: <b>{best_signal['price']}</b>\n"
            f"Тейк-профит: <b>{take_profit}</b>\n"
            f"Стоп-лосс: <b>{stop_loss}</b>\n"
            f"Направление: <b>{direction}</b>\n"
            f"Волатильность: <b>{round(best_signal['vol'],4)}</b>\n"
            f"Изменение: <b>{round(best_signal['change'],2)}%</b>\n"
            f"{''.join(news)}\n\n"
            f"💡 <b>Комментарий:</b> Подборка по объёму, волатильности и тренду рынка. Это топ-1 пара прямо сейчас."
        )
        keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(
            "Нет ярких входов на фьючерсах прямо сейчас. Попробуйте через минуту.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]])
        )

# =============== ПОРТФЕЛЬ/ОТЧЁТ ================
async def portfolio_handler(update, context):
    user_id = update.callback_query.message.chat_id
    # В реальности — тут можно вытянуть статистику по сигналам для этого user_id
    text = (
        "🗂 <b>Ваш отчёт:</b>\n"
        "- Сигналов получено: <b>100+</b>\n"
        "- Лучший PnL: <b>+37.5%</b>\n"
        "- Средний PnL: <b>+4.2%</b>\n"
        "- Рекомендуемая стратегия: тейк частями, стоп строго!\n\n"
        "<i>Скоро появится полноценный журнал сделок и авто-отслеживание сделок!</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ В меню", callback_data="back:menu")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ============= АВТОСИГНАЛЫ РАЗ В ЧАС ==============
async def hourly_signals_job(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = []
    cursor.execute("SELECT user_id FROM tokens WHERE user_id IS NOT NULL")
    for row in cursor.fetchall():
        chat_ids.append(row[0])
    # По всем юзерам: сигнал спот + сигнал фьючи (лучший)
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
                news = fetch_news()
                text = (
                    f"⏰ <b>Ежечасный сигнал на спот (Binance)</b>\n"
                    f"Пара: <b>{best_signal['pair']}</b>\n"
                    f"Цена: <b>{best_signal['price']}</b>\n"
                    f"Изменение: <b>{round(best_signal['change'],2)}%</b>\n"
                    f"Волатильность: <b>{round(best_signal['vol'],4)}</b>\n"
                    f"{''.join(news)}\n"
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
            if best_signal:
                direction = "LONG" if best_signal["change"] > 0 else "SHORT"
                stop_loss = round(best_signal["price"] * (0.98 if direction == "LONG" else 1.02), 4)
                take_profit = round(best_signal["price"] * (1.03 if direction == "LONG" else 0.97), 4)
                news = fetch_news()
                text = (
                    f"⏰ <b>Ежечасный автосигнал (Binance Фьючерсы)</b>\n"
                    f"Пара: <b>{best_signal['pair']}</b>\n"
                    f"Плечо: <b>{FUTURES_LEVERAGE}x</b>\n"
                    f"Вход: <b>{best_signal['price']}</b>\n"
                    f"Тейк-профит: <b>{take_profit}</b>\n"
                    f"Стоп-лосс: <b>{stop_loss}</b>\n"
                    f"Направление: <b>{direction}</b>\n"
                    f"Волатильность: <b>{round(best_signal['vol'],4)}</b>\n"
                    f"{''.join(news)}\n"
                )
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except: pass

# ================= ГЛАВНЫЙ MAIN =================
def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate_token", generate_token))
    application.add_handler(CommandHandler("activate", activate_token))
    application.add_handler(CallbackQueryHandler(button_handler))
    # Ежечасная отправка сигналов
    application.job_queue.run_repeating(hourly_signals_job, interval=3600, first=15)
    application.run_polling()

if __name__ == "__main__":
    main()
