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
    }
}
