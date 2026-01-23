TIMEFRAME_CONFIGS = {
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
        "candles": 120,              # ~20 días
        "atr_period": 14,
        "atr_expansion": 1.01,       # ATR se mueve lento acá
        "volume_lookback": 6,        # 6 velas = 1 día
        "min_quote_volume": 50_000_000
    },
    "1d": {
        "timeframe": "1D",
        "candles": 90,               # 3 meses
        "atr_period": 14,
        "atr_expansion": 1.0,        # casi no se usa
        "volume_lookback": 5,        # última semana
        "min_quote_volume": 150_000_000
    }
}

TIMEFRAMES = {
    "5m": {"tf": "5m", "candles": 100},
    "15m": {"tf": "15m", "candles": 100},
    "1h": {"tf": "1h", "candles": 200},
    "4h": {"tf": "4h", "candles": 120},
    "1d": {"tf": "1D", "candles": 90},
}
