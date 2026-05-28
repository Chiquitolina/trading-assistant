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
from config.timeframes import MODE_CONFIG
from ui.trade_formatter import format_trade_timestamps

from signals.state.regime_detector import RegimeDetector
from signals.state.market_state import MarketStateBuilder

from ta.trend import EMAIndicator

ARG_TZ = "America/Argentina/Cordoba"
MODE = "default"
MODE_CFG = MODE_CONFIG[MODE]

MIN_ATR = MODE_CFG.get("min_atr")
MIN_ATR_PCT = MODE_CFG.get("min_atr_pct")

SYMBOLS = [
    "SUI/USDT",
]

def atr_pct(atr: float, price: float) -> float:
    if not price:
        return 0.0
    return (atr / price) * 100


def atr_filter_ok(atr: float, price: float) -> bool:
    current_atr_pct = atr_pct(atr, price)

    if MIN_ATR_PCT is not None:
        return current_atr_pct >= MIN_ATR_PCT

    if MIN_ATR is not None:
        return atr >= MIN_ATR

    return True


def get_atr_pct_bucket(value: float) -> str:
    if value < 0.10:
        return "<0.10%"
    elif value < 0.15:
        return "0.10-0.15%"
    elif value < 0.20:
        return "0.15-0.20%"
    elif value < 0.30:
        return "0.20-0.30%"
    return "0.30%+"


def align(df, ts):
    return df[df["timestamp"] <= ts]


def align_closed_1h(df, signal_ts):
    last_closed_1h_ts = signal_ts.floor("1h") - pd.Timedelta(hours=1)
    return df[df["timestamp"] <= last_closed_1h_ts]


def get_atr_bucket(atr: float) -> str:
    if atr < 100:
        return "<100"
    elif atr < 200:
        return "100-200"
    elif atr < 300:
        return "200-300"
    return "300+"


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

    df_5m["timestamp"] = pd.to_datetime(df_5m["timestamp"], utc=True)
    df_15m["timestamp"] = pd.to_datetime(df_15m["timestamp"], utc=True)
    df_1h["timestamp"] = pd.to_datetime(df_1h["timestamp"], utc=True)

    df_15m = add_atr(df_15m, period=14)
    df_1h = trend_bias(df_1h)
    
    df_1h["ema20"] = EMAIndicator(df_1h["close"], window=20).ema_indicator()
    df_1h["ema50"] = EMAIndicator(df_1h["close"], window=50).ema_indicator()
    
    df_15m["ema20"] = EMAIndicator(df_15m["close"], window=20).ema_indicator()
    df_15m["ema50"] = EMAIndicator(df_15m["close"], window=50).ema_indicator()

    end_ts = df_15m.iloc[-1]["timestamp"]
    start_ts = end_ts - pd.Timedelta(days=BACKTEST["days"])

    valid_entries = df_15m.index[df_15m["timestamp"] >= start_ts].tolist()
    if not valid_entries:
        return []

    first_real_idx = valid_entries[0]
    start_i = max(BACKTEST["warmup"], first_real_idx - 1)

    trades = []
    all_signals = []
    market_states = []

    market_state_builder = MarketStateBuilder()
    regime_state_builder = MarketStateBuilder()

    regime_detector_1h = RegimeDetector(
        max_states=24,          # bloque máximo: 24 velas 1h
        min_states=8,           # mínimo: 8 velas 1h para decidir
        min_shifts=2,
        fail_rate_threshold=0.50,
        ema_memory_threshold=0.55,
        structure_threshold=0.50,
        regime_confirm_bars=2,
    )

    last_regime = "UNKNOWN"
    last_regime_ts = None
    
    i = start_i
    while i < len(df_15m) - 1:
        signal_ts = df_15m.iloc[i]["timestamp"]
        entry_ts = df_15m.iloc[i + 1]["timestamp"]
        signal_close_ts = entry_ts - pd.Timedelta(milliseconds=1)

        if entry_ts < start_ts:
            i += 1
            continue

        df5 = align(df_5m, signal_close_ts)
        df15 = align(df_15m, signal_close_ts)
        df1h = align_closed_1h(df_1h, signal_ts)

        if df5.empty or df15.empty or df1h.empty:
            i += 1
            continue

        trend = df1h.iloc[-1]["trend"]
        direction = trade_direction(df15) or ""
        momentum = momentum_5m(df5)

        # ================================
        # REGIME 1H
        # ================================
        current_1h_ts = df1h.iloc[-1]["timestamp"]

        if last_regime_ts != current_1h_ts:
            regime_state = regime_state_builder.build(
                candle=df1h.iloc[-1],
                trend=trend,
                direction=direction,
                momentum=momentum,
            )

            last_regime = regime_detector_1h.update(regime_state)
            last_regime_ts = current_1h_ts
            
            print(
                    f"[REGIME 1H] ts={last_regime_ts} "
                    f"regime={last_regime} "
                    f"trend={trend} "
                    f"direction={direction} "
                    f"momentum={momentum}"
                )   

        # ================================
        # MARKET STATE 15M
        # ================================
        state = market_state_builder.build(
            candle=df_15m.iloc[i],
            trend=trend.value if trend else None,
            direction=direction.value if direction else None,
            momentum=momentum.value if momentum else None,
        )

        state["regime"] = last_regime
        state["regime_tf"] = "1h"
        state["regime_ts"] = last_regime_ts
        
        state["regime_window_max"] = 24
        state["regime_window_min"] = 8

        market_states.append(state)
        
        #print("\033[95m[BACKTEST DEBUG]\033[0m 1h candle used:")
        #print(df1h.tail(1)[["timestamp", "open", "high", "low", "close"]])

        #print("\033[95m[BACKTEST DEBUG]\033[0m 15m candle used:")
        #print(df15.tail(1)[["timestamp", "open", "high", "low", "close"]])

        #print("\033[95m[BACKTEST DEBUG]\033[0m last 5 candles of 5m used for momentum:")
        #print(df5.tail(5)[["timestamp", "open", "high", "low", "close"]])

        #if not df5.empty:
        #    print(f"\033[95m[BACKTEST DEBUG]\033[0m last 5m used ts: {df5.tail(1).iloc[0]['timestamp']}")

        #print("\033[95m[BACKTEST DEBUG]\033[0m indicator result:")
        #print(f"signal_ts(open)  : {signal_ts}")
        #print(f"signal_ts(close) : {signal_close_ts}")
        #print(f"entry_ts(next)   : {entry_ts}")
        #print(f"trend            : {trend}")
        #print(f"direction        : {direction}")
        #print(f"momentum         : {momentum}\n")

        future = df_15m[df_15m["timestamp"] > entry_ts].head(BACKTEST["lookahead"])

        # ---------- LONG ----------
        if long_setup(trend, direction, momentum):
            all_signals.append({
                "timestamp": entry_ts,
                "tf": "15m",
                "side": "LONG",
                "signal_price": df_15m.iloc[i]["close"],
                "trend": trend.value if trend else None,
                "dir": direction.value if direction else None,
                "momentum": momentum.value if momentum else None,
            })

            entry_price = df_15m.iloc[i + 1]["open"]
            atr = df_15m.iloc[i]["atr"]

            if pd.isna(atr):
                i += 1
                continue

            current_atr_pct = atr_pct(atr, entry_price)

            if not atr_filter_ok(atr, entry_price):
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
                    "trend": trend.value if trend else None,
                    "direction": direction.value if direction else None,
                    "momentum": momentum.value if momentum else None,
                    "regime": last_regime,
                    "regime_tf": "1h",
                    "regime_ts": last_regime_ts,
                    "atr": round(atr, 3),
                    "atr_pct": round(current_atr_pct, 4),
                    "atr_pct_bucket": get_atr_pct_bucket(current_atr_pct),
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
                "trend": trend.value if trend else None,
                "dir": direction.value if direction else None,
                "momentum": momentum.value if momentum else None,
            })

            entry_price = df_15m.iloc[i + 1]["open"]
            atr = df_15m.iloc[i]["atr"]

            if pd.isna(atr):
                i += 1
                continue

            current_atr_pct = atr_pct(atr, entry_price)

            if not atr_filter_ok(atr, entry_price):
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
                    "trend": trend.value if trend else None,
                    "direction": direction.value if direction else None,
                    "momentum": momentum.value if momentum else None,
                    "regime": last_regime,
                    "regime_tf": "1h",
                    "regime_ts": last_regime_ts,
                    "atr": round(atr, 3),
                    "atr_pct": round(current_atr_pct, 4),
                    "atr_pct_bucket": get_atr_pct_bucket(current_atr_pct),
                })

                exit_idx = df_15m.index[df_15m["timestamp"] == result["exit_ts"]][0]
                i = exit_idx
                continue

        i += 1
        
    # ================================
    # SAVE MARKET STATES
    # ================================
    if market_states:
        df_states = pd.DataFrame(market_states)
        df_states["timestamp"] = pd.to_datetime(df_states["timestamp"], utc=True)

        df_states = df_states[df_states["timestamp"] >= start_ts].copy()
        df_states.sort_values("timestamp", inplace=True)

        # ================================
        # DEBUG DUPLICADOS ANTES DE GUARDAR
        # ================================
        dups = df_states[df_states.duplicated(subset=["timestamp"], keep=False)]

        print("\n==============================")
        print("[DEBUG] MARKET STATES")
        print("==============================")
        print("total rows:", len(df_states))
        print("unique timestamps:", df_states["timestamp"].nunique())
        print("duplicated rows:", len(dups))

        if not dups.empty:
            print("\n[DUPLICATED MARKET STATES]")
            cols = [
                "timestamp",
                "regime",
                "trend",
                "direction",
                "momentum",
                "close",
                "ema50",
            ]
            cols = [c for c in cols if c in df_states.columns]

            print(dups[cols].to_string(index=False))

        # ================================
        # ANTI-DUPLICADOS
        # una sola fila por vela
        # ================================
        df_states = (
            df_states
            .drop_duplicates(subset=["timestamp"], keep="last")
            .copy()
        )

        df_states["timestamp"] = (
            df_states["timestamp"]
            .dt.tz_convert(ARG_TZ)
            .dt.tz_localize(None)
        )

        file_name = f"market_states_{symbol.replace('/', '')}.csv"
        df_states.to_csv(file_name, index=False)
        
        print(
            f"✅ {symbol} | trades={len(trades)} "
            f"| states={len(df_states)}"
        )

        print(f"\033[95m[BACKTEST]\033[0m 💾 Saved market states CSV to {file_name}")

    if all_signals:
        df_signals = pd.DataFrame(all_signals)
        df_signals["timestamp"] = pd.to_datetime(df_signals["timestamp"], utc=True)

        df_signals = df_signals[df_signals["timestamp"] >= start_ts].copy()
        df_signals.sort_values("timestamp", inplace=True)

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

    all_results = []

    for symbol in SYMBOLS:
        print("\n" + "=" * 80)
        print(f"🚀 RUNNING BACKTEST / MARKET STATES FOR {symbol}")
        print("=" * 80)

        try:
            trades = backtest(symbol)

            df_trades = pd.DataFrame(trades)

            if not df_trades.empty:
                df_trades["symbol"] = symbol.replace("/", "")

                metrics_all = calculate_metrics(trades)
                metrics_long = calculate_metrics(
                    [t for t in trades if t["side"] == "LONG"]
                )
                metrics_short = calculate_metrics(
                    [t for t in trades if t["side"] == "SHORT"]
                )

                all_results.append({
                    "symbol": symbol.replace("/", ""),
                    "trades": metrics_all.get("trades"),
                    "winrate": metrics_all.get("winrate"),
                    "net_pnl": metrics_all.get("net_pnl"),
                    "profit_factor": metrics_all.get("profit_factor"),
                    "long_trades": metrics_long.get("trades"),
                    "long_winrate": metrics_long.get("winrate"),
                    "long_net_pnl": metrics_long.get("net_pnl"),
                    "short_trades": metrics_short.get("trades"),
                    "short_winrate": metrics_short.get("winrate"),
                    "short_net_pnl": metrics_short.get("net_pnl"),
                })

            else:
                all_results.append({
                    "symbol": symbol.replace("/", ""),
                    "trades": 0,
                    "winrate": 0,
                    "net_pnl": 0,
                    "profit_factor": 0,
                    "long_trades": 0,
                    "long_winrate": 0,
                    "long_net_pnl": 0,
                    "short_trades": 0,
                    "short_winrate": 0,
                    "short_net_pnl": 0,
                })

        except Exception as e:
            print(f"❌ ERROR running {symbol}: {e}")

            all_results.append({
                "symbol": symbol.replace("/", ""),
                "error": str(e),
            })

    df_results = pd.DataFrame(all_results)
    df_results.to_csv("multi_symbol_backtest_summary.csv", index=False)

    print("\n" + "=" * 80)
    print("✅ MULTI SYMBOL BACKTEST FINISHED")
    print("=" * 80)
    print(df_results.to_string(index=False))

