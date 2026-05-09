import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))

from data.market_data import fetch_history

SYMBOL = "BTCUSDT"
FETCH_SYMBOL = "BTC/USDT"
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]

DAYS = 650
OUTPUT_DIR = BASE_DIR / "data" / "chart_cache"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    for tf in TIMEFRAMES:
        print(f"Fetching {FETCH_SYMBOL} {tf}...")

        df = fetch_history(FETCH_SYMBOL, tf, DAYS)

        path = OUTPUT_DIR / f"{SYMBOL}_{tf}.csv"
        df.to_csv(path, index=False)

        print(f"Saved: {path}")


if __name__ == "__main__":
    main()