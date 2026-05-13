# =========================
# MODE CONFIG
# =========================

MODE_CONFIG = {

    "default": {
        "timeframes": ["1h", "15m", "5m"]
    },

    "direction": {
        "timeframes": ["1h", "15m", "5m"]
    },

    "aggressive": {
        "timeframes": ["5m", "1m"]
    }
}


# =========================
# TIMEFRAME CONFIGS
# =========================

TIMEFRAME_CONFIGS = {

    "1m": {
        "timeframe": "1m",
        "candles": 120,
        "atr_period": 14,
        "atr_expansion": 1.08,
        "volume_lookback": 20,
        "min_quote_volume": 2_000_000
    },

    "5m": {
        "timeframe": "5m",
        "candles": 100,
        "atr_period": 14,
        "atr_expansion": 1.05,
        "volume_lookback": 20,
        "min_quote_volume": 5_000_000
    },

    "15m": {
        "timeframe": "15m",
        "candles": 100,
        "atr_period": 14,
        "atr_expansion": 1.03,
        "volume_lookback": 20,
        "min_quote_volume": 10_000_000
    },

    "1h": {
        "timeframe": "1h",
        "candles": 200,
        "atr_period": 14,
        "atr_expansion": 1.02,
        "volume_lookback": 24,
        "min_quote_volume": 20_000_000
    },

    # -------- HIGH TF VOLUME SCAN --------

    "4h": {
        "timeframe": "4h",
        "candles": 120,
        "atr_period": 14,
        "atr_expansion": 1.01,
        "volume_lookback": 6,
        "min_quote_volume": 50_000_000
    },

    "1d": {
        "timeframe": "1D",
        "candles": 90,
        "atr_period": 14,
        "atr_expansion": 1.0,
        "volume_lookback": 5,
        "min_quote_volume": 150_000_000
    }
}