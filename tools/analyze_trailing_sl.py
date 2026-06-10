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


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return None


def analyze_trade(trade):
    symbol = trade["symbol"]
    side = trade["side"]

    real_entry = safe_float(trade.get("real_entry"))
    real_exit = safe_float(trade.get("real_exit"))
    mfe_before = safe_float(trade.get("mfe"))
    pnl = safe_float(trade.get("pnl"))
    pnl_gross = safe_float(trade.get("pnl_gross"))

    if real_exit is None:
        return None

    exit_ms = to_ms(trade["exit_ts"])

    candles = get_klines(symbol, exit_ms, LOOKAHEAD_BARS + 1)
    candles = candles[:LOOKAHEAD_BARS]

    row = {
        "symbol": symbol,
        "side": side,
        "entry_ts": trade.get("entry_ts"),
        "exit_ts": trade.get("exit_ts"),
        "real_entry": real_entry,
        "real_exit": real_exit,
        "pnl": pnl,
        "pnl_gross": pnl_gross,
        "mfe_before_exit": mfe_before,
        "mae_before_exit": safe_float(trade.get("mae")),
        "strategy_mode": trade.get("strategy_mode"),
        "router_reason": trade.get("router_reason"),
    }

    # cuánto devolvió desde el mejor punto antes del trailing hasta el cierre real
    if mfe_before is not None and pnl_gross is not None:
        row["giveback_pct"] = mfe_before - pnl_gross
    else:
        row["giveback_pct"] = None

    if not candles:
        return row

    favorable = []
    adverse = []

    for c in candles:
        if side == "LONG":
            favorable.append(pct_move(side, real_exit, c["high"]))
            adverse.append(pct_move(side, real_exit, c["low"]))
        else:
            favorable.append(pct_move(side, real_exit, c["low"]))
            adverse.append(pct_move(side, real_exit, c["high"]))

    row["post_mfe_20_bars_pct"] = max(favorable)
    row["post_mae_20_bars_pct"] = min(adverse)

    # cuánto más siguió después del trailing comparado contra el MFE previo
    if row["post_mfe_20_bars_pct"] is not None:
        row["total_possible_extra_after_exit_pct"] = row["post_mfe_20_bars_pct"]

    if mfe_before is not None and row["post_mfe_20_bars_pct"] is not None:
        row["full_possible_mfe_if_held_pct"] = mfe_before + row["post_mfe_20_bars_pct"]

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
        "pnl_gross": ["mean", "median"],
        "mfe_before_exit": ["mean", "median"],
        "giveback_pct": ["mean", "median"],
        "post_mfe_20_bars_pct": ["mean", "median"],
        "post_mae_20_bars_pct": ["mean", "median"],
        "full_possible_mfe_if_held_pct": ["mean", "median"],
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


def threshold_analysis(df, filename):
    thresholds = [0.5, 1.0, 1.5, 2.0]

    rows = []

    for side in ["LONG", "SHORT"]:
        side_df = df[df["side"] == side]

        if side_df.empty:
            continue

        total = len(side_df)

        row = {
            "side": side,
            "trades": total,
            "avg_pnl_gross": round(side_df["pnl_gross"].mean(), 4),
            "avg_mfe_before_exit": round(side_df["mfe_before_exit"].mean(), 4),
            "avg_giveback_pct": round(side_df["giveback_pct"].mean(), 4),
            "avg_post_mfe": round(side_df["post_mfe_20_bars_pct"].mean(), 4),
            "avg_full_possible_mfe": round(side_df["full_possible_mfe_if_held_pct"].mean(), 4),
        }

        for th in thresholds:
            count = (side_df["post_mfe_20_bars_pct"] >= th).sum()
            pct = count / total * 100

            row[f"post_mfe_ge_{th}_count"] = int(count)
            row[f"post_mfe_ge_{th}_pct"] = round(pct, 2)

        rows.append(row)

    result = pd.DataFrame(rows)
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
        subset=["symbol", "side", "exit_ts", "real_exit", "exit_reason"]
    )

    if args.strategy and "strategy_mode" in trades.columns:
        trades = trades[
            trades["strategy_mode"].astype(str).str.contains(args.strategy, na=False)
        ]

    trades = trades[trades["exit_reason"] == "TRAILING_SL"]

    print(f"📊 TRAILING_SL trades loaded: {len(trades)}")

    rows = []

    for idx, trade in trades.iterrows():
        try:
            print(f"🔎 {len(rows) + 1}/{len(trades)} {trade['symbol']} {trade['side']}")
            row = analyze_trade(trade)
            if row:
                rows.append(row)
            time.sleep(0.08)

        except Exception as e:
            print(f"⚠️ Error {trade.get('symbol')} {trade.get('exit_ts')}: {e}")

    df = pd.DataFrame(rows)

    if df.empty:
        print("No hay TRAILING_SL para analizar.")
        return

    df.to_csv("trailing_sl_analysis.csv", index=False)
    print("\n✅ Guardado trailing_sl_analysis.csv")

    summarize(df, ["side"], "trailing_sl_by_side.csv")

    if "router_reason" in df.columns:
        summarize(df, ["side", "router_reason"], "trailing_sl_by_side_router.csv")

    summarize(df, ["symbol"], "trailing_sl_by_symbol.csv")

    threshold_analysis(df, "trailing_sl_thresholds.csv")


if __name__ == "__main__":
    main()