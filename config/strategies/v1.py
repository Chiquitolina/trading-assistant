# ====================
# ====================

BACKTEST = {
    "days": 30,
    "lookahead": 1,
    "warmup": 100,
}

FEES = {
    "taker": 0.04,
    "maker": 0.02,
    "funding": 0.0,
}

LONG = {
    "sl_mult": 2,
    "tp_mult": 1.18,
    "min_tp": 0.31,
}

SHORT = {
    "sl_mult": 2,
    "tp_mult": 1.56,
    "min_tp": 0.25,
}