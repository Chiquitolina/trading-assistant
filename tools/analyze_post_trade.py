import argparse
import time
import requests
import pandas as pd

TRADES_CSV = "trades.csv"
BASE_URL = "https://fapi.binance.com"

INTERVAL = "15m"
CHECKPOINTS = [3, 5, 10, 15, 20]
LOOKAHEAD_BARS = 20


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--csv", default=TRADES_CSV)
    return parser.parse_args()


def to_ms(ts):
    return int(pd.to_datetime(ts, utc=True).timestamp() * 1000)


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


def analyze_trade(trade):
    symbol = trade["symbol"]
    side = trade["side"]
    exit_price = float(trade["real_exit"])
    exit_ms = to_ms(trade["exit_ts"])

    candles = get_klines(symbol, exit_ms, LOOKAHEAD_BARS + 1)

    # Sacamos posible vela parcial del momento exacto del cierre
    candles = candles[:LOOKAHEAD_BARS]

    row = {
        "symbol": symbol,
        "side": side,
        "entry_ts": trade.get("entry_ts"),
        "exit_ts": trade.get("exit_ts"),
        "exit_reason": trade.get("exit_reason"),
        "real_entry": trade.get("real_entry"),
        "real_exit": exit_price,
        "pnl": trade.get("pnl"),
        "pnl_gross": trade.get("pnl_gross"),
        "mfe_before_exit": trade.get("mfe"),
        "mae_before_exit": trade.get("mae"),
        "strategy_mode": trade.get("strategy_mode"),
        "router_reason": trade.get("router_reason"),
    }

    if not candles:
        return row

    favorable = []
    adverse = []

    for c in candles:
        if side == "LONG":
            favorable.append(pct_move(side, exit_price, c["high"]))
            adverse.append(pct_move(side, exit_price, c["low"]))
        else:
            favorable.append(pct_move(side, exit_price, c["low"]))
            adverse.append(pct_move(side, exit_price, c["high"]))

    row["post_mfe_20_bars_pct"] = max(favorable)
    row["post_mae_20_bars_pct"] = min(adverse)

    for n in CHECKPOINTS:
        if len(candles) >= n:
            close_n = candles[n - 1]["close"]
            row[f"move_after_{n}_bars_pct"] = pct_move(side, exit_price, close_n)
        else:
            row[f"move_after_{n}_bars_pct"] = None

    return row


def summarize(df, group_cols, filename):
    agg = {
        "symbol": "count",
        "post_mfe_20_bars_pct": ["mean", "median"],
        "post_mae_20_bars_pct": ["mean", "median"],
    }

    for n in CHECKPOINTS:
        agg[f"move_after_{n}_bars_pct"] = ["mean", "median"]

    summary = df.groupby(group_cols).agg(agg).round(4)

    summary.columns = [
        "_".join(col).strip("_")
        for col in summary.columns.to_flat_index()
    ]

    summary = summary.rename(columns={"symbol_count": "trades"})
    summary.to_csv(filename)

    print(f"\n===== {filename} =====")
    print(summary.to_string())
    
def threshold_analysis(df, exit_reason):
    subset = df[df["exit_reason"] == exit_reason].copy()

    if subset.empty:
        print(f"\n===== {exit_reason} THRESHOLD ANALYSIS =====")
        print("No trades.")
        return

    thresholds = [0.5, 1.0, 1.5, 2.0]

    rows = []

    for side in ["LONG", "SHORT"]:
        side_df = subset[subset["side"] == side]

        if side_df.empty:
            continue

        total = len(side_df)

        row = {
            "exit_reason": exit_reason,
            "side": side,
            "trades": total,
            "avg_post_mfe": round(side_df["post_mfe_20_bars_pct"].mean(), 4),
            "median_post_mfe": round(side_df["post_mfe_20_bars_pct"].median(), 4),
            "avg_post_mae": round(side_df["post_mae_20_bars_pct"].mean(), 4),
            "median_post_mae": round(side_df["post_mae_20_bars_pct"].median(), 4),
        }

        for th in thresholds:
            count = (side_df["post_mfe_20_bars_pct"] >= th).sum()
            pct = count / total * 100

            row[f"mfe_after_ge_{th}_count"] = int(count)
            row[f"mfe_after_ge_{th}_pct"] = round(pct, 2)

        rows.append(row)

    result = pd.DataFrame(rows)

    filename = f"post_trade_15m_{exit_reason.lower()}_thresholds.csv"
    result.to_csv(filename, index=False)

    print(f"\n===== {filename} =====")
    print(result.to_string(index=False))


def main():
    args = parse_args()

    trades = pd.read_csv(
        args.csv,
        engine="python",
        on_bad_lines="skip",
    )

    trades = trades.dropna(
        subset=["symbol", "side", "exit_ts", "real_exit"]
    )

    if args.strategy and "strategy_mode" in trades.columns:
        trades = trades[
            trades["strategy_mode"].astype(str).str.contains(args.strategy, na=False)
        ]

    print(f"📊 Trades loaded: {len(trades)}")

    rows = []

    for idx, trade in trades.iterrows():
        try:
            print(f"🔎 {len(rows) + 1}/{len(trades)} {trade['symbol']} {trade['exit_reason']}")
            rows.append(analyze_trade(trade))
            time.sleep(0.08)

        except Exception as e:
            print(f"⚠️ Error {trade.get('symbol')} {trade.get('exit_ts')}: {e}")

    df = pd.DataFrame(rows)

    df.to_csv("post_trade_15m_analysis.csv", index=False)
    print("\n✅ Guardado post_trade_15m_analysis.csv")

    if df.empty:
        print("No hay trades para analizar.")
        return

    summarize(df, ["exit_reason"], "post_trade_15m_by_exit_reason.csv")
    summarize(df, ["side"], "post_trade_15m_by_side.csv")
    summarize(df, ["exit_reason"], "post_trade_15m_by_exit_reason.csv")
    summarize(df, ["side"], "post_trade_15m_by_side.csv")

    summarize(
        df,
        ["exit_reason", "side"],
        "post_trade_15m_by_exit_reason_side.csv"
    )

    if "router_reason" in df.columns:
        summarize(df, ["router_reason"], "post_trade_15m_by_router_reason.csv")

        summarize(
            df,
            ["exit_reason", "side", "router_reason"],
            "post_trade_15m_by_exit_reason_side_router.csv"
        )

    summarize(df, ["symbol"], "post_trade_15m_by_symbol.csv")

    if "router_reason" in df.columns:
        summarize(df, ["router_reason"], "post_trade_15m_by_router_reason.csv")

    summarize(df, ["symbol"], "post_trade_15m_by_symbol.csv")
    
if __name__ == "__main__":
    main()