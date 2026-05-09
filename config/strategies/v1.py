# ====================
# ====================

BACKTEST = {
    "days": 600,
    "lookahead": 1,
    "warmup": 100,
}

FEES = {
    "taker": 0.05,
    "maker": 0.02,
    "funding": 0.0,
}

LONG = {
    "sl_mult": 2.1,
    "tp_mult": 1.28,
    "min_tp": 0.31,
}

SHORT = {
    "sl_mult": 2.3,
    "tp_mult": 1.56,
    "min_tp": 0.25,
}