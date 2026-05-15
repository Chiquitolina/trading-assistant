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
    SHORT_AGGRESSIVE,
)

from enums.direction import Direction
from enums.momentum import Momentum

from config.timeframes import MODE_CONFIG

# =========================
# CONFIG
# =========================
MODE = MODE_CONFIG["aggressive"]

MIN_ATR = MODE.get("min_atr", 120)
MIN_ATR_PCT = MODE.get("min_atr_pct", 0.15) / 100

COOLDOWN_BARS = 5
HTF_EMA_PERIOD = 100

SWING_LOOKBACK = 3
SWING_DISTANCE_ATR = 0.7

MIN_SWING_MOVE_PCT = 0.00025  # 0.025%

EMA_LIST = [20, 34, 50]


def detect_swing_high(df, idx, lookback=2):
    if idx < lookback:
        return False

    current_high = df.iloc[idx]["high"]
    left_max = df.iloc[idx - lookback : idx]["high"].max()

    move_pct = (current_high - left_max) / left_max

    return current_high > left_max and move_pct >= MIN_SWING_MOVE_PCT


def detect_swing_low(df, idx, lookback=2):
    if idx < lookback:
        return False

    current_low = df.iloc[idx]["low"]
    left_min = df.iloc[idx - lookback : idx]["low"].min()

    move_pct = (left_min - current_low) / left_min

    return current_low < left_min and move_pct >= MIN_SWING_MOVE_PCT

# =========================
# FIND LAST SWINGS
# =========================
def find_last_swing_low(df, idx):
    for j in range(idx - 1, SWING_LOOKBACK, -1):
        if detect_swing_low(df, j, SWING_LOOKBACK):
            return df.iloc[j]["low"]
    return None


def find_last_swing_high(df, idx):
    for j in range(idx - 1, SWING_LOOKBACK, -1):
        if detect_swing_high(df, j, SWING_LOOKBACK):
            return df.iloc[j]["high"]
    return None


# =========================
# EMA CONTEXT (NEW)
# =========================
def ema_trade_context(df, idx, entry_price):
    out = {}

    for n in EMA_LIST:
        ema = df.iloc[idx].get(f"ema_{n}")

        if pd.isna(ema):
            out[f"ema_{n}_dist"] = None
            out[f"ema_{n}_bias"] = None
        else:
            out[f"ema_{n}_dist"] = abs(entry_price - ema)
            out[f"ema_{n}_bias"] = 1 if entry_price > ema else -1

    return out


# =========================
# PRICE TOO FAR FROM SWING
# =========================
def too_far_from_swing(entry, swing, atr):
    if swing is None:
        return True

    return abs(entry - swing) > (atr * SWING_DISTANCE_ATR)


# =========================
# SIMULATE TRADE
# =========================
def simulate_trade(side, entry, future_df, atr):

    cfg = LONG_AGGRESSIVE if side == "LONG" else SHORT_AGGRESSIVE

    sl, tp, sl_pct, tp_pct = compute_levels(
        side=side, entry=entry, atr=atr, cfg=cfg
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

        else:

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
# EMA ANALYSIS (NEW)
# =========================
def ema_summary(trades_df):

    print("\n📊 EMA PERFORMANCE ANALYSIS\n")

    for n in EMA_LIST:

        col = f"ema_{n}_bias"
        if col not in trades_df:
            continue

        print(f"\nEMA {n}:")

        for side in ["LONG", "SHORT"]:

            subset = trades_df[trades_df["side"] == side]

            if subset.empty:
                continue

            winrate = (subset["pnl"] > 0).mean()
            avg_pnl = subset["pnl"].mean()

            above = subset[subset[col] == 1]
            below = subset[subset[col] == -1]

            print(f"  {side}")
            print(f"    Trades: {len(subset)}")
            print(f"    Winrate: {winrate:.2f}")
            print(f"    Avg PnL: {avg_pnl:.3f}")

            if len(above) > 0:
                print(
                    f"    Above EMA winrate: {(above['pnl'] > 0).mean():.2f}"
                )

            if len(below) > 0:
                print(
                    f"    Below EMA winrate: {(below['pnl'] > 0).mean():.2f}"
                )


# =========================
# BACKTEST ENGINE
# =========================
def backtest_aggressive(symbol: str):

    print("BACKTEST AGGRESSIVE STARTING...")

    df_1m = fetch_history(
        symbol,
        "1m",
        BACKTEST_AGGRESSIVE["days"] + BACKTEST_AGGRESSIVE["warmup"],
    )

    df_5m = fetch_history(
        symbol,
        "5m",
        BACKTEST_AGGRESSIVE["days"] + BACKTEST_AGGRESSIVE["warmup"],
    )

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
        df_5m["close"], window=HTF_EMA_PERIOD
    ).ema_indicator()

    df_5m["ema20"] = EMAIndicator(df_5m["close"], window=20).ema_indicator()

    # NEW: EMAs 1m
    for n in EMA_LIST:
        df_1m[f"ema_{n}"] = EMAIndicator(
            df_1m["close"], window=n
        ).ema_indicator()

    # =========================
    # ENGINE STATE
    # =========================
    trades = []
    last_long_sl_idx = -999
    last_short_sl_idx = -999

    last_trend_long_sl_idx = -999
    last_trend_short_sl_idx = -999

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

        if len(df5) < 2:
            i += 1
            continue

        atr_5m = df5.iloc[-1]["atr"]

        ema20_now = df5.iloc[-1]["ema20"]
        dist_ema20_5m = abs(entry_price - ema20_now)

        dist_ema20_5m_atr = (
            dist_ema20_5m / atr_5m
            if atr_5m > 0 else 999
        )   
        ema20_prev = df5.iloc[-2]["ema20"]

        ema100 = df5.iloc[-1]["ema100"]
        price_5m = df5.iloc[-1]["close"]

        if (
            pd.isna(atr_5m)
            or pd.isna(ema20_now)
            or pd.isna(ema20_prev)
            or pd.isna(ema100)
        ):
            i += 1
            continue

        atr_pct = atr_5m / entry_price

        if atr_pct < MIN_ATR_PCT:
            i += 1
            continue

        ema20_slope = ema20_now - ema20_prev

        htf_bullish = ema20_slope > 0
        htf_bearish = price_5m < ema100

        micro = micro_momentum_1m(df1, atr=atr_5m)
        micro_prev1 = micro_momentum_1m(df1.iloc[:-1], atr=atr_5m)

        direction_5m = trade_direction(df5)

        bad_short_after_bullish_pressure = (
            micro == Momentum.BEARISH_PRESSURE
            and micro_prev1 == Momentum.BULLISH_PRESSURE
        )

        ema20_1m = row["ema_20"]
        ema34_1m = row["ema_34"]
        ema50_1m = row["ema_50"]

        if pd.isna(ema20_1m) or pd.isna(ema34_1m) or pd.isna(ema50_1m):
            i += 1
            continue

        # =========================
        # TREND PULLBACK
        # =========================

        ema_alignment_bullish = ema20_1m > ema34_1m > ema50_1m

        near_ema20_long = entry_price > ema20_1m and abs(
            entry_price - ema20_1m
        ) <= (atr_5m * 0.35)

        near_ema20_short = entry_price < ema20_1m and abs(
            entry_price - ema20_1m
        ) <= (atr_5m * 0.35)

        near_ema50_long = abs(entry_price - ema50_1m) <= (atr_5m * 0.35)

        # =========================
        # SWINGS
        # =========================
        swing_low = find_last_swing_low(df_1m, i)
        swing_high = find_last_swing_high(df_1m, i)

        near_swing_low = swing_low is not None and abs(
            entry_price - swing_low
        ) <= (atr_5m * 1.2)

        near_swing_high = swing_high is not None and abs(
            entry_price - swing_high
        ) <= (atr_5m * 1.2)

        # =========================
        # LIVE-LIKE FILTERS
        # =========================
        dist_ema20_pct = abs(
            (entry_price - ema20_1m)
            / ema20_1m
            * 100
        )

        overextended = dist_ema20_5m_atr > 2.0

        weak_late_short = (
            near_swing_low
            and micro in [
                Momentum.BEARISH_PRESSURE,
                Momentum.INSIDE_BEARISH_WEAK,
            ]
        )

        weak_late_long = (
            near_swing_high
            and micro in [
                Momentum.BULLISH_PRESSURE,
                Momentum.INSIDE_BULLISH_WEAK,
            ]
        )

        near_swing_low = swing_low is not None and abs(
            entry_price - swing_low
        ) <= (atr_5m * 1.2)

        near_swing_high = swing_high is not None and abs(
            entry_price - swing_high
        ) <= (atr_5m * 1.2)

        # =========================
        # SIGNALS
        # =========================
        long_signal = (
            micro == Momentum.EXHAUSTION_DOWN
            and near_swing_low
            and htf_bullish
            and direction_5m != Direction.DOWN
            and not overextended
            and (i - last_long_sl_idx) > COOLDOWN_BARS
        )

        short_signal = (
            micro == Momentum.EXHAUSTION_UP
            and near_swing_high
            and htf_bearish
            and direction_5m != Direction.UP
            and not overextended
            and (i - last_short_sl_idx) > COOLDOWN_BARS
        )

        trend_long_signal = (
            htf_bullish
            and direction_5m == Direction.UP
            and near_ema20_long
            and micro in [
                Momentum.BULLISH_PRESSURE,
                Momentum.INSIDE_BULLISH_WEAK,
                Momentum.TREND_CONTINUATION_UP,
            ]
            and not overextended
            and not weak_late_long
            and (i - last_trend_long_sl_idx) > COOLDOWN_BARS
        )

        trend_short_signal = (
            htf_bearish
            and direction_5m == Direction.DOWN
            and near_ema20_short
            and micro in [
                Momentum.BEARISH_PRESSURE,
                Momentum.INSIDE_BEARISH_WEAK,
                Momentum.TREND_CONTINUATION_DOWN,
            ]
            and not overextended
            and not weak_late_short
            and not bad_short_after_bullish_pressure
            and (i - last_trend_short_sl_idx) > COOLDOWN_BARS
        )

        trend_long_ema50 = (
            htf_bullish
            and direction_5m == Direction.UP
            and ema_alignment_bullish
            and near_ema50_long
            and row["close"] > ema50_1m
            and micro
            in [
                Momentum.EXHAUSTION_DOWN,
                Momentum.INSIDE_BULLISH_WEAK,
                Momentum.BULLISH_PRESSURE,
            ]
        )

        future = df_1m[df_1m["timestamp"] > entry_ts].head(
            BACKTEST_AGGRESSIVE["lookahead"]
        )

        # =========================
        # LONG
        # =========================
        if long_signal:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                LONG_AGGRESSIVE["tp_mult"],
                LONG_AGGRESSIVE["min_tp"],
            )

            if ok:

                result = simulate_trade("LONG", entry_price, future, atr_5m)

                if result:

                    if result["exit_reason"] == "SL":
                        last_long_sl_idx = i

                    ema_ctx = ema_trade_context(df_1m, i, entry_price)

                    trades.append(
                        {
                            "side": "LONG",
                            "entry_ts": entry_ts,
                            "entry_price": entry_price,
                            "micro": str(micro),
                            "micro_prev1": str(micro_prev1),
                            "direction_5m": str(direction_5m),
                            "swing_low": swing_low,
                            **ema_ctx,
                            **result,
                        }
                    )

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])
                    continue

        # =========================
        # TREND LONG
        # =========================
        if trend_long_signal:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                LONG_AGGRESSIVE["tp_mult"],
                LONG_AGGRESSIVE["min_tp"],
            )

            if ok:

                result = simulate_trade("LONG", entry_price, future, atr_5m)

                if result:

                    if result["exit_reason"] == "SL":
                        last_trend_long_sl_idx = i

                    ema_ctx = ema_trade_context(df_1m, i, entry_price)

                    trades.append(
                        {
                            "strategy": "TREND",
                            "side": "LONG",
                            "entry_ts": entry_ts,
                            "entry_price": entry_price,
                            "micro": str(micro),
                            "micro_prev1": str(micro_prev1),
                            "direction_5m": str(direction_5m),
                            **ema_ctx,
                            **result,
                        }
                    )

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])

                    continue

        # =========================
        # EMA50 LONG
        # =========================
        if trend_long_ema50:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                LONG_AGGRESSIVE["tp_mult"],
                LONG_AGGRESSIVE["min_tp"],
            )

            if ok:

                result = simulate_trade("LONG", entry_price, future, atr_5m)

                if result:

                    ema_ctx = ema_trade_context(df_1m, i, entry_price)

                    trades.append(
                        {
                            "strategy": "EMA50_PULLBACK",
                            "side": "LONG",
                            "entry_ts": entry_ts,
                            "entry_price": entry_price,
                            "micro": str(micro),
                            "micro_prev1": str(micro_prev1),
                            "direction_5m": str(direction_5m),
                            **ema_ctx,
                            **result,
                        }
                    )

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])

                    continue

        # =========================
        # TREND SHORT
        # =========================
        if trend_short_signal:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                SHORT_AGGRESSIVE["tp_mult"],
                SHORT_AGGRESSIVE["min_tp"],
            )

            if ok:

                result = simulate_trade("SHORT", entry_price, future, atr_5m)

                if result:

                    if result["exit_reason"] == "SL":
                        last_trend_short_sl_idx = i

                    ema_ctx = ema_trade_context(df_1m, i, entry_price)

                    trades.append(
                        {
                            "side": "SHORT",
                            "entry_ts": entry_ts,
                            "entry_price": entry_price,
                            "micro": str(micro),
                            "micro_prev1": str(micro_prev1),
                            "direction_5m": str(direction_5m),
                            "strategy": "trend_following",
                            **ema_ctx,
                            **result,
                        }
                    )

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])

                    continue

        # =========================
        # SHORT
        # =========================
        if short_signal:

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr_5m,
                SHORT_AGGRESSIVE["tp_mult"],
                SHORT_AGGRESSIVE["min_tp"],
            )

            if ok:

                result = simulate_trade("SHORT", entry_price, future, atr_5m)

                if result:

                    if result["exit_reason"] == "SL":
                        last_short_sl_idx = i

                    ema_ctx = ema_trade_context(df_1m, i, entry_price)

                    trades.append(
                        {
                            "side": "SHORT",
                            "entry_ts": entry_ts,
                            "entry_price": entry_price,
                            "micro": str(micro),
                            "micro_prev1": str(micro_prev1),
                            "direction_5m": str(direction_5m),
                            "swing_high": swing_high,
                            **ema_ctx,
                            **result,
                        }
                    )

                    i = df_1m["timestamp"].searchsorted(result["exit_ts"])
                    continue

        i += 1

    # =========================
    # OUTPUT
    # =========================
    pd.DataFrame(trades).to_csv(
        f"trades_{symbol.replace('/', '')}_1m.csv", index=False
    )

    print("\n--------------------------------------\n")
    print_backtest_banner()
    print("\n--------------------------------------\n")

    df = pd.DataFrame(trades)

    all_m = calculate_metrics(trades)

    long_trades = df[df["side"] == "LONG"].to_dict("records")
    short_trades = df[df["side"] == "SHORT"].to_dict("records")

    long_m = calculate_metrics(long_trades)
    short_m = calculate_metrics(short_trades)

    print(pretty_metrics(all_m, long_m, short_m))

    if trades:
        df_trades = pd.DataFrame(trades)
        print("\n📌 TRADES DETAILS:\n")
        print(df_trades.to_string(index=False))

        ema_summary(df_trades)

    else:
        print("\nNo trades found.\n")

    return trades


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    symbol = "TIA/USDT"
    backtest_aggressive(symbol)