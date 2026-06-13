import argparse
import time
import requests
import pandas as pd

TRADES_CSV = "trades.csv"
BASE_URL = "https://fapi.binance.com"

INTERVAL = "15m"
LOOKAHEAD_BARS = 20
CHECKPOINTS = [3, 5, 10, 15, 20]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--csv", default=TRADES_CSV)
    parser.add_argument("--trail-trigger", type=float, default=0.40)
    return parser.parse_args()


def to_ms(ts):
    return int(pd.to_datetime(ts, utc=True).timestamp() * 1000)


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return None


def get_klines(symbol, start_ms, limit):
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": start_ms,
        "limit": limit,
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()

    return [
        {
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        }
        for k in r.json()
    ]


def pct_move(side, base_price, price):
    if side == "LONG":
        return ((price - base_price) / base_price) * 100
    return ((base_price - price) / base_price) * 100


def analyze_trade(trade, trail_trigger):
    symbol = trade["symbol"]
    side = trade["side"]

    real_exit = safe_float(trade.get("real_exit"))
    pnl_gross = safe_float(trade.get("pnl_gross"))
    mfe_before = safe_float(trade.get("mfe"))
    mae_before = safe_float(trade.get("mae"))

    if real_exit is None or pnl_gross is None or mfe_before is None:
        return None

    exit_ms = to_ms(trade["exit_ts"])
    candles = get_klines(symbol, exit_ms, LOOKAHEAD_BARS + 1)
    candles = candles[:LOOKAHEAD_BARS]

    post_favorable = []
    post_adverse = []

    for c in candles:
        if side == "LONG":
            post_favorable.append(pct_move(side, real_exit, c["high"]))
            post_adverse.append(pct_move(side, real_exit, c["low"]))
        else:
            post_favorable.append(pct_move(side, real_exit, c["low"]))
            post_adverse.append(pct_move(side, real_exit, c["high"]))

    post_mfe = max(post_favorable) if post_favorable else 0
    post_mae = min(post_adverse) if post_adverse else 0

    giveback_from_peak = mfe_before - pnl_gross
    extra_after_exit = post_mfe
    full_possible_mfe = mfe_before + post_mfe

    efficiency_vs_pre_exit_peak = (
        pnl_gross / mfe_before * 100
        if mfe_before and mfe_before > 0
        else None
    )

    efficiency_vs_full_possible = (
        pnl_gross / full_possible_mfe * 100
        if full_possible_mfe and full_possible_mfe > 0
        else None
    )

    row = {
        "symbol": symbol,
        "side": side,
        "entry_ts": trade.get("entry_ts"),
        "exit_ts": trade.get("exit_ts"),
        "real_entry": safe_float(trade.get("real_entry")),
        "real_exit": real_exit,

        "trail_trigger_pct": trail_trigger,
        "profit_when_trailing_started_pct": trail_trigger,

        "pnl_gross_pct": pnl_gross,
        "mfe_before_exit_pct": mfe_before,
        "mae_before_exit_pct": mae_before,

        "giveback_from_peak_pct": giveback_from_peak,
        "post_mfe_20_bars_pct": post_mfe,
        "post_mae_20_bars_pct": post_mae,
        "full_possible_mfe_pct": full_possible_mfe,

        "efficiency_vs_pre_exit_peak_pct": efficiency_vs_pre_exit_peak,
        "efficiency_vs_full_possible_pct": efficiency_vs_full_possible,

        "strategy_mode": trade.get("strategy_mode"),
        "router_reason": trade.get("router_reason"),
    }

    for n in CHECKPOINTS:
        if len(candles) >= n:
            close_n = candles[n - 1]["close"]
            row[f"move_after_{n}_bars_pct"] = pct_move(side, real_exit, close_n)
        else:
            row[f"move_after_{n}_bars_pct"] = None

    return row


def summarize(df, group_cols, filename):
    agg = {
        "symbol": "count",
        "pnl_gross_pct": ["mean", "median"],
        "mfe_before_exit_pct": ["mean", "median"],
        "giveback_from_peak_pct": ["mean", "median"],
        "post_mfe_20_bars_pct": ["mean", "median"],
        "full_possible_mfe_pct": ["mean", "median"],
        "efficiency_vs_pre_exit_peak_pct": ["mean", "median"],
        "efficiency_vs_full_possible_pct": ["mean", "median"],
    }

    summary = df.groupby(group_cols).agg(agg).round(4)

    summary.columns = [
        "_".join(col).strip("_")
        for col in summary.columns.to_flat_index()
    ]

    summary = summary.rename(columns={"symbol_count": "trades"})
    summary.to_csv(filename)

    print(f"\n===== {filename} =====")
    print(summary.to_string())


def main():
    args = parse_args()

    trades = pd.read_csv(
        args.csv,
        engine="python",
        on_bad_lines="skip",
    )

    trades = trades.dropna(
        subset=["symbol", "side", "exit_ts", "real_exit", "exit_reason"]
    )

    if args.strategy and "strategy_mode" in trades.columns:
        trades = trades[
            trades["strategy_mode"].astype(str).str.contains(args.strategy, na=False)
        ]

    trades = trades[trades["exit_reason"] == "TRAILING_SL"]

    print(f"📊 TRAILING_SL trades loaded: {len(trades)}")

    rows = []

    for _, trade in trades.iterrows():
        try:
            print(f"🔎 {len(rows) + 1}/{len(trades)} {trade['symbol']} {trade['side']}")
            row = analyze_trade(trade, args.trail_trigger)
            if row:
                rows.append(row)
            time.sleep(0.08)

        except Exception as e:
            print(f"⚠️ Error {trade.get('symbol')} {trade.get('exit_ts')}: {e}")

    df = pd.DataFrame(rows)

    if df.empty:
        print("No hay TRAILING_SL válidos.")
        return

    df.to_csv("trailing_efficiency_analysis.csv", index=False)
    print("\n✅ Guardado trailing_efficiency_analysis.csv")

    summarize(df, ["side"], "trailing_efficiency_by_side.csv")

    if "router_reason" in df.columns:
        summarize(df, ["side", "router_reason"], "trailing_efficiency_by_side_router.csv")

    summarize(df, ["symbol"], "trailing_efficiency_by_symbol.csv")


if __name__ == "__main__":
    main()