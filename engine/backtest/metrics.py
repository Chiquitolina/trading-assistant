import numpy as np
import pandas as pd

from engine.backtest.metrics_config import METRIC_GROUPS

MIN_GROSS_PNL = 1.0  # umbral anti-explosión


def calculate_metrics(trades: list) -> dict:

    if not trades:
        return {
            "trades": 0,
            "winrate": 0,
            "gross_pnl": 0,
            "net_pnl": 0,
            "fees": 0,
            "fee_impact": None,
            "fee_drag": None,
            "fees_per_trade": 0,
            "fee_to_avg_win": None,
            "avg_win": 0,
            "avg_loss": 0,
            "profit_factor": 0,
            "expectancy": 0,
            "max_drawdown": 0,
        }

    df = pd.DataFrame(trades)

    # --------------------
    # CORE STATS
    # --------------------
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_trades = len(df)
    winrate = len(wins) / total_trades * 100

    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0 else 0
    )

    avg_win = wins["pnl"].mean() if not wins.empty else 0
    avg_loss = losses["pnl"].mean() if not losses.empty else 0

    expectancy = (
        (winrate / 100) * avg_win
        + (1 - winrate / 100) * avg_loss
    )

    equity = df["pnl"].cumsum()
    drawdown = equity - equity.cummax()
    max_drawdown = drawdown.min()

    # --------------------
    # PNL / FEES
    # --------------------
    gross_pnl = df["pnl_gross"].sum()
    net_pnl = df["pnl"].sum()
    fees = df["fees"].sum()

    # Fee Impact (SAFE)
    fee_impact = (
        (fees / gross_pnl) * 100
        if gross_pnl > MIN_GROSS_PNL
        else None
    )

    # Fee Drag (realidad pura)
    fee_drag = (
        ((gross_pnl - net_pnl) / abs(gross_pnl)) * 100
        if gross_pnl != 0
        else None
    )

    fees_per_trade = fees / total_trades

    fee_to_avg_win = (
        fees_per_trade / avg_win
        if avg_win > 0
        else None
    )

    return {
        "trades": total_trades,
        "winrate": round(winrate, 2),

        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "fees": round(fees, 2),

        "fee_impact": round(fee_impact, 2) if fee_impact is not None else "N/A",
        "fee_drag": round(fee_drag, 2) if fee_drag is not None else "N/A",

        "fees_per_trade": round(fees_per_trade, 4),
        "fee_to_avg_win": (
            round(fee_to_avg_win * 100, 2)
            if fee_to_avg_win is not None else "N/A"
        ),

        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),

        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 4),
        "max_drawdown": round(max_drawdown, 2),
    }
    
def pretty_metrics(all_m, long_m, short_m):
    COL_LABEL = 28
    COL_VAL = 18

    header = (
        f"{'':<{COL_LABEL}}"
        f"{'ALL':>{COL_VAL}}"
        f"{'LONG':>{COL_VAL}}"
        f"{'SHORT':>{COL_VAL}}"
    )

    lines = ["📊 SUMMARY\n", header]

    for group in METRIC_GROUPS:
        for key in group:
            def fmt(v):
                return f"{v}" if isinstance(v, str) else f"{v:.2f}"

            lines.append(
                f"{key:<{COL_LABEL}}"
                f"{fmt(all_m.get(key, 'N/A')):>{COL_VAL}}"
                f"{fmt(long_m.get(key, 'N/A')):>{COL_VAL}}"
                f"{fmt(short_m.get(key, 'N/A')):>{COL_VAL}}"
            )

        # 👇 línea en blanco entre bloques
        lines.append("")

    return "\n".join(lines)