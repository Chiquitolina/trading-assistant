import numpy as np
import pandas as pd


def calculate_metrics(trades: list) -> dict:

    if not trades:
        return {
            "trades": 0,
            "winrate": 0,
            "gross_pnl": 0,
            "net_pnl": 0,
            "fees": 0,
            "fee_impact": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "profit_factor": 0,
            "expectancy": 0,
            "max_drawdown": 0,
        }

    df = pd.DataFrame(trades)

    # --------------------
    # NET PNL (REAL)
    # --------------------
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_trades = len(df)
    winrate = len(wins) / total_trades * 100

    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())

    profit_factor = gross_profit / gross_loss if gross_loss != 0 else 0

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
    # GROSS / FEES
    # --------------------
    gross_pnl = df["pnl_gross"].sum()
    net_pnl = df["pnl"].sum()
    fees = df["fees"].sum()

    fee_impact = (
        abs(fees) / abs(gross_pnl) * 100
        if gross_pnl != 0 else 0
    )

    return {
        "trades": total_trades,
        "winrate": round(winrate, 2),

        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "fees": round(fees, 2),
        "fee_impact": round(fee_impact, 2),

        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 4),
        "max_drawdown": round(max_drawdown, 2),
    }

