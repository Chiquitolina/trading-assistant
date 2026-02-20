# metrics/config.py

METRIC_GROUPS = [
    # Core
    [
        "trades",
        "winrate",
        "gross_pnl",
        "net_pnl",
    ],

    # Fees
    [
        "fees",
        "fee_impact",
        "fee_drag",
        "fees_per_trade",
        "fee_to_avg_win",
    ],

    # Trade stats
    [
        "avg_win",
        "avg_loss",
    ],

    # Performance
    [
        "profit_factor",
        "expectancy",
        "max_drawdown",
    ],
]