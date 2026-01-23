VOLUME_CONFIG = {
    "5m": {
        "lookback": 20,
        "min_quote_volume": 5_000_000,
    },
    "15m": {
        "lookback": 20,
        "min_quote_volume": 10_000_000,
    },
    "1h": {
        "lookback": 24,
        "min_quote_volume": 20_000_000,
    },
    "4h": {
        "lookback": 6,        # 1 día
        "min_quote_volume": 50_000_000,
    },
    "1d": {
        "lookback": 5,        # última semana
        "min_quote_volume": 150_000_000,
    },
}
