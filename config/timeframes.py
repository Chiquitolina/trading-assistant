# =========================
# MODE CONFIG
# =========================

MODE_CONFIG = {
    "default": {
        "timeframes": ["1h", "15m", "5m"],
        "trigger_tf": "15m",
        "min_atr_pct": 0.26,
        "min_atr": 200,
        "entry_tf": "1m",
        "atr_tf": "15m",
    },

    "direction": {
        "timeframes": ["1h", "15m", "5m"],
        "trigger_tf": "15m",
        "min_atr_pct": 0.26,
        "min_atr": 200,
        "entry_tf": "1m",
        "atr_tf": "15m",
    },

    "aggressive": {
        "timeframes": ["1m", "5m", "15m", "1h", "4h"],
        "trigger_tf": "1m",
        "min_atr_pct": 0.15,
        "entry_tf": "1m",
        "atr_tf": "5m",
        "min_atr": 120,
    },
}

# =========================
# TIMEFRAME CONFIGS
# =========================

TIMEFRAME_CONFIGS = {

    "1m": {
        "timeframe": "1m",
        "ms_per_candle": 60_000,
        "candles": 120,
        "atr_period": 14,
        "atr_expansion": 1.08,
        "volume_lookback": 20,
        "min_quote_volume": 2_000_000
    },

    "5m": {
        "timeframe": "5m",
        "ms_per_candle": 300_000,
        "candles": 100,
        "atr_period": 14,
        "atr_expansion": 1.05,
        "volume_lookback": 20,
        "min_quote_volume": 5_000_000
    },

    "15m": {
        "timeframe": "15m",
        "ms_per_candle": 900_000,
        "candles": 100,
        "atr_period": 14,
        "atr_expansion": 1.03,
        "volume_lookback": 20,
        "min_quote_volume": 10_000_000
    },

    "1h": {
        "timeframe": "1h",
        "ms_per_candle": 3_600_000,
        "candles": 200,
        "atr_period": 14,
        "atr_expansion": 1.02,
        "volume_lookback": 24,
        "min_quote_volume": 20_000_000
    },

    "4h": {
        "timeframe": "4h",
        "ms_per_candle": 14_400_000,
        "candles": 120,
        "atr_period": 14,
        "atr_expansion": 1.01,
        "volume_lookback": 6,
        "min_quote_volume": 50_000_000
    },

    "1d": {
        "timeframe": "1D",
        "ms_per_candle": 86_400_000,
        "candles": 90,
        "atr_period": 14,
        "atr_expansion": 1.0,
        "volume_lookback": 5,
        "min_quote_volume": 150_000_000
    }
}