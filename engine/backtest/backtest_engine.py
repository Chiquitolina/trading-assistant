import pandas as pd

from data.market_data import fetch_history
from signals.indicators.trend import trend_bias
from signals.indicators.direction import trade_direction
from signals.indicators.momentum import momentum_5m
from engine.backtest.metrics import calculate_metrics, pretty_metrics
from signals.indicators.atr import add_atr
from signals.strategy.filters import min_expected_tp_ok
from signals.strategy.risk import compute_levels
from ui.banners import print_backtest_banner
from signals.strategy.entries import long_setup, short_setup
from config.strategies.v1 import LONG, SHORT, FEES, BACKTEST
from ui.trade_formatter import format_trade_timestamps


ARG_TZ = "America/Argentina/Cordoba"


def align(df, ts):
    return df[df["timestamp"] <= ts]


def simulate_trade(side, entry, future_df, atr):
    if side == "LONG":
        sl, tp, sl_pct, tp_pct = compute_levels(
            side="LONG",
            entry=entry,
            atr=atr,
            cfg=LONG
        )

        for _, row in future_df.iterrows():
            if row["low"] <= sl:
                exit_price = sl
                pnl_gross = (exit_price - entry) / entry * 100
                fees = 2 * FEES["taker"] + FEES["funding"]
                pnl_net = pnl_gross - fees
                return {
                    "pnl": round(pnl_net, 3),
                    "pnl_gross": round(pnl_gross, 3),
                    "fees": round(fees, 3),
                    "exit_ts": row["timestamp"],
                    "exit_price": sl,
                    "exit_reason": "SL",
                    "sl_pct": round(sl_pct, 3),
                    "tp_pct": round(tp_pct, 3),
                    "sl": round(abs(sl), 3),
                    "tp": round(tp, 3),
                }

            if row["high"] >= tp:
                exit_price = tp
                pnl_gross = (exit_price - entry) / entry * 100
                fees = 2 * FEES["taker"] + FEES["funding"]
                pnl_net = pnl_gross - fees
                return {
                    "pnl": round(pnl_net, 3),
                    "pnl_gross": round(pnl_gross, 3),
                    "fees": round(fees, 3),
                    "exit_ts": row["timestamp"],
                    "exit_price": tp,
                    "exit_reason": "TP",
                    "sl_pct": round(sl_pct, 3),
                    "tp_pct": round(tp_pct, 3),
                    "sl": round(abs(sl), 3),
                    "tp": round(tp, 3),
                }

    elif side == "SHORT":
        sl, tp, sl_pct, tp_pct = compute_levels(
            side="SHORT",
            entry=entry,
            atr=atr,
            cfg=SHORT
        )

        for _, row in future_df.iterrows():
            if row["high"] >= sl:
                exit_price = sl
                pnl_gross = (entry - exit_price) / entry * 100
                fees = 2 * FEES["taker"] + FEES["funding"]
                pnl_net = pnl_gross - fees
                return {
                    "pnl": round(pnl_net, 3),
                    "pnl_gross": round(pnl_gross, 3),
                    "fees": round(fees, 3),
                    "exit_ts": row["timestamp"],
                    "exit_price": sl,
                    "exit_reason": "SL",
                    "sl_pct": round(sl_pct, 3),
                    "tp_pct": round(tp_pct, 3),
                    "sl": round(abs(sl), 3),
                    "tp": round(tp, 3),
                }

            if row["low"] <= tp:
                exit_price = tp
                pnl_gross = (entry - exit_price) / entry * 100
                fees = 2 * FEES["taker"] + FEES["funding"]
                pnl_net = pnl_gross - fees
                return {
                    "pnl": round(pnl_net, 3),
                    "pnl_gross": round(pnl_gross, 3),
                    "fees": round(fees, 3),
                    "exit_ts": row["timestamp"],
                    "exit_price": tp,
                    "exit_reason": "TP",
                    "sl_pct": round(sl_pct, 3),
                    "tp_pct": round(tp_pct, 3),
                    "sl": round(abs(sl), 3),
                    "tp": round(tp, 3),
                }

    return None


def backtest(symbol: str):
    df_5m = fetch_history(symbol, "5m", BACKTEST["days"] + BACKTEST["warmup"])
    df_15m = fetch_history(symbol, "15m", BACKTEST["days"] + BACKTEST["warmup"])
    df_1h = fetch_history(symbol, "1h", BACKTEST["days"] + BACKTEST["warmup"])

    # asumir que vienen en UTC y convertir a datetime timezone-aware
    df_5m["timestamp"] = pd.to_datetime(df_5m["timestamp"], utc=True)
    df_15m["timestamp"] = pd.to_datetime(df_15m["timestamp"], utc=True)
    df_1h["timestamp"] = pd.to_datetime(df_1h["timestamp"], utc=True)

    df_15m = add_atr(df_15m, period=14)
    df_1h = trend_bias(df_1h)

    end_ts = df_15m.iloc[-1]["timestamp"]
    start_ts = end_ts - pd.Timedelta(days=BACKTEST["days"])

    valid_entries = df_15m.index[df_15m["timestamp"] >= start_ts].tolist()
    if not valid_entries:
        return []

    first_real_idx = valid_entries[0]
    start_i = max(BACKTEST["warmup"], first_real_idx - 1)

    trades = []
    all_signals = []

    i = start_i
    while i < len(df_15m) - 1:
        signal_ts = df_15m.iloc[i]["timestamp"]
        entry_ts = df_15m.iloc[i + 1]["timestamp"]

        if entry_ts < start_ts:
            i += 1
            continue

        df5 = align(df_5m, signal_ts)
        df15 = align(df_15m, signal_ts)
        df1h = align(df_1h, signal_ts)

        if df5.empty or df15.empty or df1h.empty:
            i += 1
            continue

        trend = df1h.iloc[-1]["trend"]
        direction = trade_direction(df15) or ""
        momentum = momentum_5m(df5)

        future = df_15m[df_15m["timestamp"] > entry_ts].head(BACKTEST["lookahead"])

        # ---------- LONG ----------
        if long_setup(trend, direction, momentum):
            all_signals.append({
                "timestamp": entry_ts,
                "tf": "15m",
                "side": "LONG",
                "signal_price": df_15m.iloc[i]["close"],
                "dir": direction,
                "trend": trend,
                "momentum": momentum,
            })

            entry_price = df_15m.iloc[i + 1]["open"]
            atr = df_15m.iloc[i]["atr"]

            if pd.isna(atr):
                i += 1
                continue

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr,
                LONG["tp_mult"],
                LONG["min_tp"]
            )

            if not ok:
                i += 1
                continue

            result = simulate_trade("LONG", entry_price, future, atr)

            if result:
                trades.append({
                    "side": "LONG",
                    "entry_ts": entry_ts,
                    "exit_ts": result["exit_ts"],
                    "entry": entry_price,
                    "exit_price": result["exit_price"],
                    "pnl": result["pnl"],
                    "pnl_gross": result["pnl_gross"],
                    "fees": result["fees"],
                    "exit_reason": result["exit_reason"],
                })

                exit_idx = df_15m.index[df_15m["timestamp"] == result["exit_ts"]][0]
                i = exit_idx
                continue

        # ---------- SHORT ----------
        elif short_setup(trend, direction, momentum):
            all_signals.append({
                "timestamp": entry_ts,
                "tf": "15m",
                "side": "SHORT",
                "signal_price": df_15m.iloc[i]["close"],
                "dir": direction,
                "trend": trend,
                "momentum": momentum,
            })

            entry_price = df_15m.iloc[i + 1]["open"]
            atr = df_15m.iloc[i]["atr"]

            if pd.isna(atr):
                i += 1
                continue

            ok, _ = min_expected_tp_ok(
                entry_price,
                atr,
                SHORT["tp_mult"],
                SHORT["min_tp"]
            )

            if not ok:
                i += 1
                continue

            result = simulate_trade("SHORT", entry_price, future, atr)

            if result:
                trades.append({
                    "side": "SHORT",
                    "entry_ts": entry_ts,
                    "exit_ts": result["exit_ts"],
                    "entry": entry_price,
                    "exit_price": result["exit_price"],
                    "pnl": result["pnl"],
                    "pnl_gross": result["pnl_gross"],
                    "fees": result["fees"],
                    "exit_reason": result["exit_reason"],
                })

                exit_idx = df_15m.index[df_15m["timestamp"] == result["exit_ts"]][0]
                i = exit_idx
                continue

        i += 1

    if all_signals:
        df_signals = pd.DataFrame(all_signals)
        df_signals["timestamp"] = pd.to_datetime(df_signals["timestamp"], utc=True)

        # guardar SOLO señales dentro de los últimos `days`
        df_signals = df_signals[df_signals["timestamp"] >= start_ts].copy()
        df_signals.sort_values("timestamp", inplace=True)

        # convertir a horario de Argentina para CSV
        df_signals["timestamp"] = (
            df_signals["timestamp"]
            .dt.tz_convert(ARG_TZ)
            .dt.tz_localize(None)
        )

        file_name = f"backtest_signals_{symbol.replace('/', '')}.csv"
        df_signals.to_csv(file_name, index=False)
        print(f"\033[95m[BACKTEST]\033[0m 💾 Saved replay-style CSV to {file_name}")

    return trades


if __name__ == "__main__":
    symbol = "BTC/USDT"
    trades = backtest(symbol)

    long_trades = [t for t in trades if t["side"] == "LONG"]
    short_trades = [t for t in trades if t["side"] == "SHORT"]

    metrics_all = calculate_metrics(trades)
    metrics_long = calculate_metrics(long_trades)
    metrics_short = calculate_metrics(short_trades)

    print("\n----------------------------------------------------------------------------------------------------------\n")
    print_backtest_banner()
    print("\n----------------------------------------------------------------------------------------------------------\n")

    print(
        pretty_metrics(
            metrics_all,
            metrics_long,
            metrics_short
        )
    )

    df_trades = pd.DataFrame(trades)

    if not df_trades.empty:
        # si querés también imprimir trades en horario Argentina
        df_trades["entry_ts"] = (
            pd.to_datetime(df_trades["entry_ts"], utc=True)
            .dt.tz_convert(ARG_TZ)
            .dt.tz_localize(None)
        )
        df_trades["exit_ts"] = (
            pd.to_datetime(df_trades["exit_ts"], utc=True)
            .dt.tz_convert(ARG_TZ)
            .dt.tz_localize(None)
        )

        df_trades = format_trade_timestamps(df_trades)

        print("\n\n📌 TRADES DETAILS:\n\n")
        text = df_trades.to_string(col_space=7, justify="center", index=False)
        text = text.replace("\n", "\n\n")
        print(text)
    else:
        print("\n\n📌 TRADES DETAILS:\n\nNo trades found.")

    print("\n")