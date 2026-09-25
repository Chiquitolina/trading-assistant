import argparse
import csv
from datetime import datetime, timezone

import ccxt

from engine.live.research.swing_detector import (
    SwingDetector,
)


# ==========================================
# CONFIG
# ==========================================

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}


DETECTOR_CONFIGS = (
    ("2x2", 2, 2),
    ("3x3", 3, 3),
    ("5x5", 5, 5),
)


# ==========================================
# HELPERS
# ==========================================

def timestamp_to_iso(timestamp_ms):
    return datetime.fromtimestamp(
        int(timestamp_ms) / 1000,
        tz=timezone.utc,
    ).isoformat()


def normalize_symbol(symbol):
    """
    Permite pasar:
        TAKEUSDT
        TAKE/USDT
        TAKE/USDT:USDT
    """

    symbol = str(symbol).upper().strip()

    if "/" in symbol:
        return symbol

    if symbol.endswith("USDT"):
        base = symbol[:-4]

        return f"{base}/USDT:USDT"

    raise ValueError(
        "Symbol must be a USDT futures symbol. "
        f"Received: {symbol}"
    )


# ==========================================
# FETCH
# ==========================================

def fetch_candles(
    exchange,
    symbol,
    timeframe,
    limit,
):
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}"
        )

    market_symbol = normalize_symbol(
        symbol
    )

    rows = exchange.fetch_ohlcv(
        market_symbol,
        timeframe=timeframe,
        limit=limit,
    )

    candles = []

    for row in rows:
        candles.append({
            "timestamp": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })

    return candles


# ==========================================
# RESEARCH
# ==========================================

def run_detector(
    candles,
    name,
    left_bars,
    right_bars,
    min_prominence_pct,
):
    detector = SwingDetector(
        left_bars=left_bars,
        right_bars=right_bars,
        min_prominence_pct=min_prominence_pct,
    )

    swings = detector.detect_all(
        candles
    )

    rows = []

    for swing in swings:
        rows.append({
            "detector": name,

            "left_bars": left_bars,
            "right_bars": right_bars,

            "side": swing.side,
            "price": swing.price,

            "pivot_timestamp": (
                swing.pivot_timestamp
            ),

            "pivot_time_utc": (
                timestamp_to_iso(
                    swing.pivot_timestamp
                )
            ),

            "confirmed_timestamp": (
                swing.confirmed_timestamp
            ),

            "confirmed_time_utc": (
                timestamp_to_iso(
                    swing.confirmed_timestamp
                )
            ),

            "confirmation_delay_bars": (
                right_bars
            ),

            "prominence_pct": (
                swing.prominence_pct
            ),
        })

    return rows


def research_timeframe(
    exchange,
    symbol,
    timeframe,
    limit,
    min_prominence_pct,
):
    candles = fetch_candles(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )

    if not candles:
        raise RuntimeError(
            "No candles returned | "
            f"symbol={symbol} "
            f"timeframe={timeframe}"
        )

    all_rows = []

    for (
        name,
        left_bars,
        right_bars,
    ) in DETECTOR_CONFIGS:

        rows = run_detector(
            candles=candles,
            name=name,
            left_bars=left_bars,
            right_bars=right_bars,
            min_prominence_pct=(
                min_prominence_pct
            ),
        )

        for row in rows:
            row["symbol"] = symbol
            row["timeframe"] = timeframe

        all_rows.extend(
            rows
        )

    return candles, all_rows


# ==========================================
# SUMMARY
# ==========================================

def print_summary(
    symbol,
    timeframe,
    candles,
    rows,
):
    print()
    print("=" * 72)

    print(
        f"SWING RESEARCH | "
        f"{symbol} | {timeframe}"
    )

    print("=" * 72)

    print(
        f"Candles: {len(candles)}"
    )

    print(
        "Range: "
        f"{timestamp_to_iso(candles[0]['timestamp'])}"
        " -> "
        f"{timestamp_to_iso(candles[-1]['timestamp'])}"
    )

    print()

    for name, _, _ in DETECTOR_CONFIGS:

        detector_rows = [
            row
            for row in rows
            if row["detector"] == name
        ]

        highs = [
            row
            for row in detector_rows
            if row["side"] == "HIGH"
        ]

        lows = [
            row
            for row in detector_rows
            if row["side"] == "LOW"
        ]

        print(
            f"{name:>4} | "
            f"total={len(detector_rows):>4} | "
            f"highs={len(highs):>4} | "
            f"lows={len(lows):>4}"
        )

    print()


# ==========================================
# CSV
# ==========================================

def save_csv(
    rows,
    output_path,
):
    if not rows:
        print(
            "No swings detected. "
            "CSV not created."
        )
        return

    fieldnames = [
        "symbol",
        "timeframe",
        "detector",

        "left_bars",
        "right_bars",

        "side",
        "price",

        "pivot_timestamp",
        "pivot_time_utc",

        "confirmed_timestamp",
        "confirmed_time_utc",

        "confirmation_delay_bars",

        "prominence_pct",
    ]

    with open(
        output_path,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(rows)

    print(
        f"Saved: {output_path}"
    )


# ==========================================
# MAIN
# ==========================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare confirmed swing "
            "definitions on Binance Futures."
        )
    )

    parser.add_argument(
        "--symbol",
        required=True,
    )

    parser.add_argument(
        "--timeframe",
        required=True,
        choices=tuple(
            TIMEFRAME_MS.keys()
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=500,
    )

    parser.add_argument(
        "--min-prominence",
        type=float,
        default=0.0,
    )

    parser.add_argument(
        "--output",
        default=None,
    )

    args = parser.parse_args()

    exchange = ccxt.binanceusdm({
        "enableRateLimit": True,
    })

    candles, rows = research_timeframe(
        exchange=exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        limit=args.limit,
        min_prominence_pct=(
            args.min_prominence
        ),
    )

    print_summary(
        symbol=args.symbol,
        timeframe=args.timeframe,
        candles=candles,
        rows=rows,
    )

    output_path = (
        args.output
        or (
            "swing_research_"
            f"{args.symbol}_"
            f"{args.timeframe}.csv"
        )
    )

    save_csv(
        rows,
        output_path,
    )


if __name__ == "__main__":
    main()