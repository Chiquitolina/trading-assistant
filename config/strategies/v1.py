# ====================
# ====================

BACKTEST = {
    "days": 60,
    "lookahead": 1,
    "warmup": 100,
}

BACKTEST_AGGRESSIVE = {
    "days": 5,
    "warmup": 5,
    "lookahead":10
}

LONG_AGGRESSIVE = {
    "sl_mult": 2.1,
    "tp_mult": 1.7,
    "min_tp": 0.31,
}

SHORT_AGGRESSIVE = {
    "sl_mult": 2.1,
    "tp_mult": 1.7,
    "min_tp": 0.25,
}

FEES = {
    "taker": 0.05,
    "maker": 0.02,
    "funding": 0.0,
}

LONG = {
    "sl_mult": 1.9,
    "tp_mult": 1.28,
    "min_tp": 0.31,
}

SHORT = {
    "sl_mult": 1.9,
    "tp_mult": 1.56,
    "min_tp": 0.25,
}
