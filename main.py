

import requests
import asyncio
import json
import os
from datetime import datetime, timezone
from binance.client import Client
from binance.enums import HistoricalKlinesType
from telegram import Bot

# # ===== STARTUP VALIDATION =====
# def validate_config():
#     required_vars = ["npWpNsw98SrIjDSgYRSeQXuKkdZPigx4VDNTjNH1NleR61nADQnskjVKxq9zVKw5", "XJAtZ6V5fa93VfkD5cGbPRVLCTL2LeMnqQYMxRMGYWTi5LOxxH1ZNE4zG6vtC7bl", "7838823091:AAEXMGY6kQVLK6h2XZgTU63vxTPkxmkD0zs", "-1002915874071"]
#     missing = [var for var in required_vars if not os.getenv(var)]
#     if missing:
#         raise EnvironmentError(f"❌ Missing environment variables: {missing}")

# validate_config()
# print("✅ All environment variables loaded.")

# ===== CONFIG =====

TELEGRAM_TOKEN = "7838823091:AAEXMGY6kQVLK6h2XZgTU63vxTPkxmkD0zs"
CHAT_ID = "-1002915874071"

# Initialize clients
client = Client()
bot = Bot(token=TELEGRAM_TOKEN)

# ===== STATE TRACKING FILE =====
SEEN_COINS_FILE = "seen_daily_breakouts.json"

def load_seen_coins():
    """Load coins already alerted today"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not os.path.exists(SEEN_COINS_FILE):
        return set()
    try:
        with open(SEEN_COINS_FILE, "r") as f:
            data = json.load(f)
            return set(data.get(today, []))
    except:
        return set()

def save_seen_coin(symbol):
    """Save coin as alerted today"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {}
    if os.path.exists(SEEN_COINS_FILE):
        try:
            with open(SEEN_COINS_FILE, "r") as f:
                data = json.load(f)
        except:
            pass
    if today not in data:
        data[today] = []
    if symbol not in data[today]:
        data[today].append(symbol)
        with open(SEEN_COINS_FILE, "w") as f:
            json.dump(data, f)


# ===== TELEGRAM WRAPPER =====
async def send_telegram(msg: str):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        print(f"❌ Telegram send failed: {e}")


# ===== GET VALID SYMBOLS =====
def get_valid_symbols():
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING']
        return set(symbols)
    except Exception as e:
        print(f"❌ Failed to fetch valid symbols: {e}")
        return set()


# ===== CALCULATE ATR(14) =====
def calculate_atr(symbol):
    try:
        klines = client.get_historical_klines(
            symbol, Client.KLINE_INTERVAL_1DAY, "15 days ago UTC",
            klines_type=HistoricalKlinesType.FUTURES
        )
        if len(klines) < 14:
            return 0

        tr_sum = 0
        for i in range(1, 15):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i-1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_sum += tr

        return round(tr_sum / 14, 4)
    except Exception as e:
        print(f"❌ ATR calc failed for {symbol}: {e}")
        return 0


# ===== CHECK DAILY BREAKOUT CONFIRMATION =====
def check_atr_breakout_confirmation(symbol):
    """
    Returns True only if:
    - Today's close >= Yesterday's High + ATR(14)
    """
    try:
        klines = client.get_historical_klines(
            symbol, Client.KLINE_INTERVAL_1DAY, "2 days ago UTC",
            klines_type=HistoricalKlinesType.FUTURES
        )
        if len(klines) < 2:
            return False, 0, 0

        prev_candle = klines[-2]  # yesterday
        today_candle = klines[-1]  # today

        prev_high = float(prev_candle[2])
        today_close = float(today_candle[4])

        atr = calculate_atr(symbol)
        breakout_level = prev_high + atr

        confirmed = today_close >= breakout_level
        change_from_high = ((today_close - prev_high) / prev_high) * 100

        return confirmed, round(change_from_high, 2), atr

    except Exception as e:
        print(f"❌ Error checking breakout: {e}")
        return False, 0, 0


# ===== CALCULATE RSI(14) on 1H =====
def calculate_rsi(symbol):
    try:
        klines = client.get_historical_klines(
            symbol, Client.KLINE_INTERVAL_1HOUR, "15 hours ago UTC",
            klines_type=HistoricalKlinesType.FUTURES
        )
        if len(klines) < 15:
            return 0

        closes = [float(k[4]) for k in klines]
        gains = []
        losses = []

        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))

        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    except Exception as e:
        print(f"❌ RSI calc failed: {e}")
        return 0


# ===== MAIN SIGNAL JOB =====
async def job():
    print("🔄 Checking for confirmed daily breakout signals...")

    # seen_coins = load_seen_coins()  # 🔴 DISABLED: Allow repeated alerts for same coin
    valid_symbols = get_valid_symbols()
    if not valid_symbols:
        print("❌ No valid symbols fetched. Skipping.")
        return

    try:
        tickers = client.futures_ticker()
        usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT") and t["symbol"] in valid_symbols]

        # Filter: high volume, real coins
        filtered_pairs = []
        for t in usdt_pairs:
            volume = float(t["quoteVolume"])
            price = float(t["lastPrice"])
            symbol = t["symbol"]

            if volume < 10_000_000:  # $10M+
                continue
            if price < 0.000001:
                continue
            if any(x in symbol for x in ["BEAR", "BULL", "UP", "DOWN", "HALF"]):
                continue

            filtered_pairs.append(t)

        # Sort by 24h change
        top_20 = sorted(filtered_pairs, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:30]

        print(f"✅ Scanning {len(top_20)} high-volume gainers for confirmed breakouts...")

        alert_count = 0
        for ticker in top_20:
            symbol = ticker["symbol"]

            # if symbol in seen_coins:  # 🔴 REMOVED: No more daily blocking
            #     continue

            # 🔍 Step 1: Check breakout confirmation
            confirmed, change_from_high, atr = check_atr_breakout_confirmation(symbol)
            if not confirmed:
                continue  # Skip if no confirmed breakout

            # 🔍 Step 2: Get RSI
            rsi = calculate_rsi(symbol)

            # 📊 RSI Status
            if rsi < 30:
                rsi_status = "🟢 Oversold"
            elif rsi > 70:
                rsi_status = "🔴 Overbought"
            else:
                rsi_status = "⚪ Neutral"

            # ✅ Send only if passed all filters
            msg = (
                f"🚀 *DAILY BREAKOUT*\n\n"
                f"🔥 *{symbol}*\n"
                f"💰 *Price:* {ticker['lastPrice']}\n"
                f"📈 *24h Change:* {ticker['priceChangePercent']}%\n"
                f"🎯 *Close vs Prev High:* +{change_from_high:.2f}%\n"
                f"📏 *ATR(14):* {atr}\n"
                f"✅ *Breakout Level:* `{round(float(ticker['lastPrice']) - atr, 8)}` + ATR\n"
                f"📊 *RSI(1H):* {rsi} ({rsi_status})"
            )
            await send_telegram(msg)
            # save_seen_coin(symbol)  # 🔴 REMOVED: No tracking anymore
            alert_count += 1
            await asyncio.sleep(1)  # Rate limit

        print(f"✅ Sent {alert_count} breakout alerts.")

    except Exception as e:
        print(f"❌ Error in job: {e}")


# ===== SCHEDULER LOOP =====
async def main_loop():
    print("🚀 Binance Breakout Bot Started (Daily Confirmed Signals Only)")

    # Run immediately first time
    await job()

    # Run every 15 minutes
    while True:
        await asyncio.sleep(60 * 15)  # Wait 15 mins
        await job()


# ===== START BOT =====
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped manually.")
    except Exception as e:
        print(f"💥 Critical error: {e}")






