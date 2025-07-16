
import ccxt
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
import time

# OKX API credentials
api_key = "41f42e27-2610-49ac-8607-5fe787e520e9"
api_secret = "0BB608AD6C2E79B9B040B484431BB9A9"
passphrase = "your-passphrase"

# Connect to OKX
exchange = ccxt.okx({
    "apiKey": api_key,
    "secret": api_secret,
    "password": passphrase,
    "options": {"defaultType": "spot"},
})

symbol = "BTC/USDT"
timeframe = "1m"

def fetch_data(symbol, timeframe):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df

def main():
    print("Starting OKX bot...")
    while True:
        try:
            data = fetch_data(symbol, timeframe)
            print(data.tail())
            time.sleep(60)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()

