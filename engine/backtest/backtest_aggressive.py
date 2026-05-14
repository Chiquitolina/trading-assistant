import pandas as pd

from data.market_data import fetch_history
from ta.trend import EMAIndicator

from signals.indicators.direction import trade_direction
from signals.indicators.atr import add_atr
from signals.indicators.micro_momentum import micro_momentum_1m

from signals.strategy.filters import min_expected_tp_ok
from signals.strategy.risk import compute_levels

from engine.backtest.metrics import calculate_metrics, pretty_metrics
from ui.banners import print_backtest_banner

from config.strategies.v1 import (
    FEES,
    BACKTEST_AGGRESSIVE,
    LONG_AGGRESSIVE,
    SHORT_AGGRESSIVE
)

from enums.direction import Direction
from enums.momentum import Momentum


# =========================
# CONFIG
# =========================
MIN_ATR = 120
COOLDOWN_BARS = 5
HTF_EMA_PERIOD = 100


# =========================
# SIMULATE TRADE (PURE EXEC ENGINE)
# =========================
def simulate_trade(side, entry, future_df, atr):

    cfg = LONG_AGGRESSIVE if side == "LONG" else SHORT_AGGRESSIVE

    sl, tp, sl_pct, tp_pct = compute_levels(
        side=side,
        entry=entry,
        atr=atr,
        cfg=cfg
    )

    for _, row in future_df.iterrows():

        if side == "LONG":

            if row["low"] <= sl:
                pnl_gross = ((sl - entry) / entry) * 100
                fees = 2 * FEES["taker"] + FEES["funding"]

                return {
                    "pnl": round(pnl_gross - fees, 4),
                    "pnl_gross": round(pnl_gross, 4),
                    "fees": round(fees, 4),
                    "exit_ts": row["timestamp"],
                    "exit_price": sl,
                    "exit_reason": "SL",
                    "sl_pct": round(sl_pct, 4),
                    "tp_pct": round(tp_pct, 4),
                    "sl": round(sl, 2),
                    "tp": round(tp, 2),
                }

            if row["high"] >= tp:
                pnl_gross = ((tp - entry) / entry) * 100
                fees = 2 * FEES["taker"] + FEES["funding"]

                return {
                    "pnl": round(pnl_gross - fees, 4),
                    "pnl_gross": round(pnl_gross, 4),
                    "fees": round(fees, 4),
                    "exit_ts": row["timestamp"],
                    "exit_price": tp,
                    "exit_reason": "TP",
                    "sl_pct": round(sl_pct, 4),
                    "tp_pct": round(tp_pct, 4),
                    "sl": round(sl, 2),
                    "tp": round(tp, 2),
                }

        else:  # SHORT

            if row["high"] >= sl:
                pnl_gross = ((entry - sl) / entry) * 100
                fees = 2 * FEES["taker"] + FEES["funding"]

                return {
                    "pnl": round(pnl_gross - fees, 4),
                    "pnl_gross": round(pnl_gross, 4),
                    "fees": round(fees, 4),
                    "exit_ts": row["timestamp"],
                    "exit_price": sl,
                    "exit_reason": "SL",
                    "sl_pct": round(sl_pct, 4),
                    "tp_pct": round(tp_pct, 4),
                    "sl": round(sl, 2),
                    "tp": round(tp, 2),
                }

            if row["low"] <= tp:
                pnl_gross = ((entry - tp) / entry) * 100
                fees = 2 * FEES["taker"] + FEES["funding"]

                return {
                    "pnl": round(pnl_gross - fees, 4),
                    "pnl_gross": round(pnl_gross, 4),
                    "fees": round(fees, 4),
                    "exit_ts": row["timestamp"],
                    "exit_price": tp,
                    "exit_reason": "TP",
                    "sl_pct": round(sl_pct, 4),
                    "tp_pct": round(tp_pct, 4),
                    "sl": round(sl, 2),
                    "tp": round(tp, 2),
                }

    return None


# =========================
# BACKTEST ENGINE
# =========================
def backtest_aggressive(symbol: str):

    print("BACKTEST AGGRESSIVE STARTING...")

    df_1m = fetch_history(symbol, "1m",
                          BACKTEST_AGGRESSIVE["days"] + BACKTEST_AGGRESSIVE["warmup"])
    df_5m = fetch_history(symbol, "5m",
                          BACKTEST_AGGRESSIVE["days"] + BACKTEST_AGGRESSIVE["warmup"])

    if df_1m.empty or df_5m.empty:
        print("❌ Empty dataframe")
        return []

    # =========================
    # CLEAN TIME
    # =========================
    df_1m["timestamp"] = pd.to_datetime(df_1m["timestamp"], utc=True)
    df_5m["timestamp"] = pd.to_datetime(df_5m["timestamp"], utc=True)

    df_1m = df_1m.sort_values("timestamp").reset_index(drop=True)
    df_5m = df_5m.sort_values("timestamp").reset_index(drop=True)

    # =========================
    # INDICATORS
    # =========================
    df_1m = add_atr(df_1m, period=14)
    df_5m = add_atr(df_5m, period=14)

    df_5m["ema100"] = EMAIndicator(
        df_5m["close"],
        window=HTF_EMA_PERIOD
    ).ema_indicator()

    # =========================
    # ENGINE STATE
    # =========================
    trades = []

    last_long_sl_idx = -999
    last_short_sl_idx = -999

    i = BACKTEST_AGGRESSIVE["warmup"]

    # =========================
    # MAIN LOOP
    # =========================
    while i < len(df_1m) - 20:

        row = df_1m.iloc[i + 1]
        entry_ts = row["timestamp"]
        entry_price = row["open"]

        df1 = df_1m[df_1m["timestamp"] <= entry_ts]
        df5 = df_5m[df_5m["timestamp"] <= entry_ts]

        if df1.empty or df5.empty:
            i += 1
            continue

        atr_5m = df5.iloc[-1]["atr"]
        ema100 = df5.iloc[-1]["ema100"]

        if pd.isna(atr_5m) or pd.isna(ema100) or atr_5m < MIN_ATR:
            i += 1
            continue

        price_5m = df5.iloc[-1]["close"]

        htf_bullish = price_5m > ema100
        htf_bearish = price_5m < ema100

        micro = micro_momentum_1m(df1, atr=atr_5m)
        direction_5m = trade_direction(df5)

        long_signal = (
            micro == Momentum.EXHAUSTION_DOWN
            and (htf_bullish or direction_5m == Direction.DOWN)
            and (i - last_long_sl_idx) > COOLDOWN_BARS
        )

        short_signal = (
            micro == Momentum.EXHAUSTION_UP
            and (htf_bearish or direction_5m == Direction.UP)
            and (i - last_short_sl_idx) > COOLDOWN_BARS
        )

        future = df_1m[df_1m["timestamp"] > entry_ts].head(
            BACKTEST_AGGRESSIVE["lookahead"]
        )

        # =========================
        # LONG EXECUTION
        # =========================
        if long_signal:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                LONG_AGGRESSIVE["tp_mult"],
                LONG_AGGRESSIVE["min_tp"]
            )

            if ok:
                result = simulate_trade("LONG", entry_price, future, atr_5m)

                if result:
                    if result["exit_reason"] == "SL":
                        last_long_sl_idx = i

                    trades.append({
                        "side": "LONG",
                        "entry_ts": entry_ts,
                        "entry_price": entry_price,
                        **result
                    })

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])
                    continue

        # =========================
        # SHORT EXECUTION
        # =========================
        if short_signal:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                SHORT_AGGRESSIVE["tp_mult"],
                SHORT_AGGRESSIVE["min_tp"]
            )

            if ok:
                result = simulate_trade("SHORT", entry_price, future, atr_5m)

                if result:
                    if result["exit_reason"] == "SL":
                        last_short_sl_idx = i

                    trades.append({
                        "side": "SHORT",
                        "entry_ts": entry_ts,
                        "entry_price": entry_price,
                        **result
                    })

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])
                    continue

        i += 1

    # =========================
    # SAVE ONLY REAL TRADES
    # =========================
    pd.DataFrame(trades).to_csv(
        f"trades_{symbol.replace('/', '')}_1m.csv",
        index=False
    )

    return trades


# =========================
# MAIN
# =========================
if __name__ == "__main__":

    symbol = "BTC/USDT"

    trades = backtest_aggressive(symbol)

    print("\n--------------------------------------\n")
    print_backtest_banner()

    print("\n--------------------------------------\n")

    metrics = calculate_metrics(trades)
    print(pretty_metrics(metrics, metrics, metrics))

    if trades:
        print("\n📌 TRADES DETAILS:\n")
        print(pd.DataFrame(trades).to_string(index=False))
    else:
        print("\nNo trades found.\n")