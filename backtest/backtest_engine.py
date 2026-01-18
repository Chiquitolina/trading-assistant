import pandas as pd

from data.market_data import fetch_history
from common.indicators.trend import trend_bias
from indicators.direction import trade_direction
from common.indicators.momentum import momentum_5m
from backtest.metrics import calculate_metrics
from common.indicators.atr import add_atr

from tabulate import tabulate

DAYS = 30

LOOKAHEAD = 2
WARMUP = 200

TAKER_FEE = 0.04   # 0.04% Binance Futures
MAKER_FEE = 0.02  # si después querés
FUNDING_FEE = 0.0 # por ahora

#TP_PCT = 0.01    # +1%
#SL_PCT = 0.005   # -0.5%

SL_MULT = 2.0   # 1 ATR
TP_MULT = 1.15   # 2 ATR

TP_MULT_SHORT = 1.3

MIN_TP = 0.30      # % mínimo aceptable

def align(df, ts):
    return df[df["timestamp"] <= ts]


def simulate_trade(side, entry, future_df, atr):

    if side == "LONG":
        sl = entry - SL_MULT * atr
        tp = entry + TP_MULT * atr
        
        sl_pct = abs((entry - sl) / entry * 100)
        tp_pct = abs((entry - tp) / entry * 100)

        for _, row in future_df.iterrows():
            if row["low"] <= sl:
                
                exit_price = sl
                
                pnl_gross = (exit_price - entry) / entry * 100
                fees = 2 * TAKER_FEE + FUNDING_FEE
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
                    "tp": round(tp, 3)
                }

            if row["high"] >= tp:
                
                exit_price = tp

                pnl_gross = (exit_price - entry) / entry * 100
                fees = 2 * TAKER_FEE + FUNDING_FEE
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
                    "tp": round(tp, 3)
                }

    elif side == "SHORT":
        sl = entry + SL_MULT * atr
        tp = entry - TP_MULT_SHORT * atr
        
        sl_pct = abs((entry - sl) / entry * 100)
        tp_pct = abs((entry - tp) / entry * 100)

        for _, row in future_df.iterrows():
            if row["high"] >= sl:
                
                exit_price = sl
                
                pnl_gross = (entry - exit_price) / entry * 100
                fees = 2 * TAKER_FEE + FUNDING_FEE
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
                    "tp": round(tp, 3)
                }

            if row["low"] <= tp:
                
                exit_price = tp

                pnl_gross = (entry - exit_price) / entry * 100
                fees = 2 * TAKER_FEE + FUNDING_FEE
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
                    "tp": round(tp, 3)
                }

    return 0.0

def backtest(symbol: str):
    df_5m  = fetch_history(symbol, "5m",  (288 * DAYS) + WARMUP)
    df_15m = fetch_history(symbol, "15m", (96  * DAYS) + WARMUP)
    df_1h  = fetch_history(symbol, "1h",  (24  * DAYS) + WARMUP)
    
    df_15m = add_atr(df_15m, period=14)

    trades = []

    for i in range(WARMUP, len(df_15m) - 1):
        signal_ts = df_15m.iloc[i]["timestamp"]
        entry_ts  = df_15m.iloc[i + 1]["timestamp"]

        df5  = align(df_5m, signal_ts)
        df15 = align(df_15m, signal_ts)
        df1h = align(df_1h, signal_ts)

        trend     = trend_bias(df1h)
        direction = trade_direction(df15)
        momentum  = momentum_5m(df5)

        future = df_15m[
            df_15m["timestamp"] > entry_ts
        ].head(LOOKAHEAD)

        # ---------- LONG ----------
        if (
            trend == "bullish"
            and direction == "up"
            and momentum in ("impulse_up", "breakout_up")
        ):
            entry_price = df_15m.iloc[i + 1]["open"]
            
            atr = df_15m.iloc[i]["atr"]
            if pd.isna(atr):
                continue
            
            # --- EXPECTED TP ---
            expected_tp_pct = (atr * TP_MULT) / entry_price * 100
            if expected_tp_pct < MIN_TP:
                continue

            result = simulate_trade("LONG", entry_price, future, atr)

            if result:
                trades.append({
                    "side": "LONG",
                    "entry_ts": entry_ts,
                    "exit_ts": result["exit_ts"],
                    "entry": entry_price,
                    "exit_price": result["exit_price"],  # <--- AGREGAR ESTO
                    "pnl": result["pnl"],
                    "pnl_gross": result["pnl_gross"],
                    "fees": result["fees"],
                    "exit_reason": result["exit_reason"],
                    "sl_pct": result.get("sl_pct"),
                    "tp_pct": result.get("tp_pct"),
                    "sl": result.get("sl"),
                    "tp": result.get("tp")
                })

        # ---------- SHORT ----------
        elif (
            trend == "bearish"
            and direction == "down"
            and momentum != "breakout_up"
        ):
            entry_price = df_15m.iloc[i + 1]["open"]
            
            atr = df_15m.iloc[i]["atr"]
            if pd.isna(atr):
                continue

            result = simulate_trade("SHORT", entry_price, future, atr)

            if result:
                trades.append({
                    "side": "SHORT",
                    "entry_ts": entry_ts,
                    "exit_ts": result["exit_ts"],
                    "entry": entry_price,
                    "exit_price": result["exit_price"],  # <--- AGREGAR ESTO
                    "pnl": result["pnl"],
                    "pnl_gross": result["pnl_gross"],
                    "fees": result["fees"],
                    "exit_reason": result["exit_reason"],
                    "sl_pct": result.get("sl_pct"),
                    "tp_pct": result.get("tp_pct"),
                    "sl": result.get("sl"),
                    "tp": result.get("tp")
                })

    return trades



def print_metrics(title, metrics):
    print(f"\n📊 {title}")
    print(f"Trades: {metrics['trades']}")
    print(f"Winrate: {metrics['winrate']}%")

    print(f"Gross PnL: {metrics['gross_pnl']}%")
    print(f"Net PnL:   {metrics['net_pnl']}%")
    print(f"Fees:     {metrics['fees']}%")

    print(f"Fee Impact: {metrics['fee_impact']}%")

    print(f"Avg Win: {metrics['avg_win']}%")
    print(f"Avg Loss: {metrics['avg_loss']}%")
    print(f"Profit Factor: {metrics['profit_factor']}")
    print(f"Expectancy: {metrics['expectancy']}")
    print(f"Max Drawdown: {metrics['max_drawdown']}%")


if __name__ == "__main__":
    symbol = "BTC/USDT"

    trades = backtest(symbol)

    # 🔥 SEPARACIÓN POR SIDE (ACÁ ESTABA LO QUE FALTABA)
    long_trades = [t for t in trades if t["side"] == "LONG"]
    
    long_pnls_net   = [t["pnl"] for t in long_trades]
    long_pnls_gross = [t["pnl_gross"] for t in long_trades]
    long_fees  = [t["fees"] for t in long_trades]

    short_trades = [t for t in trades if t["side"] == "SHORT"]

    metrics_all = calculate_metrics(trades)
    metrics_long = calculate_metrics(long_trades)
    metrics_short = calculate_metrics(short_trades)
    
    print("\n📌 MAIN:")
    print("\n")
    table = pd.DataFrame.from_dict(
    {
        "ALL": metrics_all,
        "LONG": metrics_long,
        "SHORT": metrics_short,
    },
    orient="columns"
    )
    
    pd.options.display.float_format = "{:.2f}".format
    print(table.to_string(
    float_format="{:.2f}".format,
    col_space=18
    ))
    
    df_trades = pd.DataFrame(trades)
    
    df_trades["entry_ts"] = (
        pd.to_datetime(df_trades["entry_ts"], unit="ms", utc=True)
        .dt.tz_convert("America/Argentina/Buenos_Aires")
    )

    df_trades["exit_ts"] = (
        pd.to_datetime(df_trades["exit_ts"], unit="ms", utc=True)
        .dt.tz_convert("America/Argentina/Buenos_Aires")
    )

    df_trades["entry_ts"] = df_trades["entry_ts"].dt.strftime("%m-%d %H:%M")
    df_trades["exit_ts"]  = df_trades["exit_ts"].dt.strftime("%m-%d %H:%M")
    
    print("\n")
    print("\n📌 DETALLE DE TRADES:")
    print("\n")
    print(df_trades.to_string(
    col_space=7,
    justify="center",
    index=False))  