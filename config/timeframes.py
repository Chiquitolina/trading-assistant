import os
from dotenv import load_dotenv

load_dotenv()

MAIN_TF = os.getenv("MAIN_TF", "30m")

# =========================
# MODE CONFIG
# =========================

MODE_CONFIG = {
    "default": {
        "timeframes": ["1h", "15m", "5m"],
        "trigger_tf": "15m",
        "min_atr_pct": 0.26,
        "min_atr": 200,
        "entry_tf": "15m",
        "atr_tf": "15m",
        "entry_rules": "old"
    },

    "direction": {
        "timeframes": ["1d", "4h", "1h", "15m", "5m", "1m"],
        "trigger_tf": "15m",
        "min_atr_pct": 0.26,
        "min_atr": 200,
        "entry_tf": "15m",
        "atr_tf": "15m",
        "entry_rules": "standard",
        "allow_longs": True,
        "allow_shorts": True,
        "log_blocked_signals": True,
        "use_fixed_levels": True,
        "fixed_tp_pct": 0.30,
        "fixed_sl_pct": 0.40,
    },

    "aggressive": {
        "timeframes": ["1m", "5m", "15m", "1h", "4h"],
        "trigger_tf": "1m",
        "min_atr_pct": 0.15,
        "entry_tf": "1m",
        "atr_tf": "5m",
        "min_atr": 120,
        "entry_rules": "standard"
    },
    
    "compression": {
        "timeframes": [
            "1m",
            "5m",
            "15m",
            "30m",
            "1h",
            "4h",
        ],
        "trigger_tf": MAIN_TF,
        "min_atr_pct": 0.20,
        "min_atr": 120,
        "entry_tf": MAIN_TF,
        "atr_tf": MAIN_TF,
        "entry_rules": "standard",
        "allow_longs": True,
        "allow_shorts": False,

        # Conserva los niveles originales de esta rama.
        "use_fixed_levels": False,
        "fixed_tp_pct": 0.30,
        "fixed_sl_pct": 0.40,

        "compression_enabled": True,
        "compression_tf": MAIN_TF,

        # ======================================
        # SWING HIGH 4H BUCKET
        # ======================================
        "swing_high_4h_bucket_enabled": True,
        "swing_high_4h_bucket_min_pct": -1.0,
        "swing_high_4h_bucket_max_pct": 0.0,
        "swing_high_4h_bucket_router_reason": (
            "compression_breakout"
        ),

        # ======================================
        # HYBRID STRUCTURAL SL
        # ======================================
        "hybrid_structural_sl_enabled": True,
        "hybrid_structural_max_risk_pct": 2.0,
        "hybrid_structural_sl_buffer_pct": 0.0,
    }
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
    "30m": {
        "timeframe": "30m",
        "ms_per_candle": 1_800_000,
        "candles": 100,
        "atr_period": 14,
        "atr_expansion": 1.025,
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