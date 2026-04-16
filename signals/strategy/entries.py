# signals/strategy/entries.py

# ------------------------------
# LONG COMBINATIONS
# ------------------------------
# signals/strategy/entries.py

# signals/strategy/entries.py

# ------------------------------
# LONG COMBINATIONS
# ------------------------------

LONG_RULES = {
    # 🟢 Trend following limpio
    ("bullish", "up", "breakout_up_strong"),
    ("bullish", "up", "trend_continuation_up"),
    #("bullish", "up", "bullish_pressure"),

    # 🟢 Pullback en tendencia alcista
    ("bullish", "down", "exhaustion_down"),
    ("bullish", "down", "inside_bar"),
    ("bullish", "down", "breakout_down_weak"),

    # 🟢 Pullback dentro de uptrend
    ("bullish", "up", "exhaustion_down"),
    ("bullish", "up", "breakout_down_weak"),
    ("bullish", "up", "inside_bullish_weak"),

    # 🟡 Neutral con impulso claro
    #("neutral", "up", "breakout_up_strong"),
    #("neutral", "up", "exhaustion_down"),

    # 🟡 Neutral pullback / reversal controlado
    #("neutral", "down", "breakout_down_strong"),
    #("neutral", "down", "exhaustion_down"),
    #("neutral", "down", "inside_bar"),
}

# SOLO PARA BACKTEST / INVESTIGACIÓN
SHORT_RULES = {

    # 🔥 core (los mejores)
    ("bearish", "down", "breakout_down_strong"),
    ("bearish", "down", "trend_continuation_down"),

    # 🟡 opcionales pero buenos
    ("bearish", "down", "bearish_pressure"),
    
}

# ------------------------------
# LONG SETUP
# ------------------------------
def long_setup(trend, direction, momentum) -> bool:
    return (trend, direction, momentum) in LONG_RULES


# ------------------------------
# SHORT SETUP
# ------------------------------
def short_setup(trend, direction, momentum) -> bool:
    return (trend, direction, momentum) in SHORT_RULES

