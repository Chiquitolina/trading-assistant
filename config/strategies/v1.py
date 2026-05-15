# ====================
# ====================

SYMBOLS = [
    # majors
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",

    # momentum / narratives
    "SEIUSDT",
    "FETUSDT",
    "TIAUSDT",
    "APTUSDT",
    "OPUSDT",
    "RAYUSDT",
    "WIFUSDT",
    
    #"ALTUSDT",
    "NEARUSDT",
    "DOGEUSDT",
    #"CUSDT",
    #"QNTUSDT",
    #"PLAYUSDT",
    "ARBUSDT",
    "SUIUSDT",
    "INJUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "XRPUSDT",
    "TONUSDT",
]

MAX_GLOBAL_POSITIONS = 2

BACKTEST = {
    "days": 7,
    "lookahead": 1,
    "warmup": 100,
}

BACKTEST_AGGRESSIVE = {
    "days": 7,
    "warmup": 5,
    "lookahead":10
}

LONG_AGGRESSIVE = {
    "sl_mult": 1.75,
    "tp_mult": 1.75,
    "min_tp": 0.31,
}

SHORT_AGGRESSIVE = {
    "sl_mult": 1.75,
    "tp_mult": 1.75,
    "min_tp": 0.25,
    "min_tp": 0.25,
}

FEES = {
    "taker": 0.05,
    "maker": 0.02,
    "funding": 0.0,
}

LONG = {
    "sl_mult": 1.9,
    "tp_mult": 1.38,
    "min_tp": 0.31,
}

SHORT = {
    "sl_mult": 1.9,
    "tp_mult": 1.56,
    "min_tp": 0.25,
}
