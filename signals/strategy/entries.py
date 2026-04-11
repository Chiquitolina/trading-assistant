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

    # -------- bullish --------

    ("bullish","up","breakout_up_strong"),
    #("bullish","up","breakout_up_weak"),
    #("bullish","up","breakout_down_strong"),
    ("bullish","up","breakout_down_weak"),
    ("bullish","up","bullish_pressure"),
    ("bullish","up","bearish_pressure"),
    #("bullish","up","inside_bar"),
    ("bullish","up","exhaustion_up"),
    ("bullish","up","exhaustion_down"),
    ("bullish","up","trend_continuation_up"),
    ("bullish","up","trend_continuation_down"),
    ("bullish","up","indecision"),
    ("bullish","up","inside_bullish_weak"),
    #("bullish","up","inside_bearish_weak"),
    ("bullish","up",None),

    ("bullish","down","breakout_up_strong"),
    ("bullish","down","breakout_up_weak"),
    ("bullish","down","breakout_down_strong"),
    ("bullish","down","breakout_down_weak"),
    ("bullish","down","bullish_pressure"),
    ("bullish","down","bearish_pressure"),
    ("bullish","down","inside_bar"),
    ("bullish","down","exhaustion_up"),
    ("bullish","down","exhaustion_down"),
    ("bullish","down","trend_continuation_up"),
    ("bullish","down","trend_continuation_down"),
    ("bullish","down","indecision"),
    ("bullish","down","inside_bullish_weak"),
    ("bullish","down","inside_bearish_weak"),
    ("bullish","down",None),

    ("bullish","range","breakout_up_strong"),
    ("bullish","range","breakout_up_weak"),
    ("bullish","range","breakout_down_strong"),
    ("bullish","range","breakout_down_weak"),
    ("bullish","range","bullish_pressure"),
    ("bullish","range","bearish_pressure"),
    ("bullish","range","inside_bar"),
    ("bullish","range","exhaustion_up"),
    ("bullish","range","exhaustion_down"),
    ("bullish","range","trend_continuation_up"),
    ("bullish","range","trend_continuation_down"),
    ("bullish","range","indecision"),
    ("bullish","range","inside_bullish_weak"),
    ("bullish","range","inside_bearish_weak"),
    ("bullish","range",None),

    ("bullish",None,"breakout_up_strong"),
    ("bullish",None,"breakout_up_weak"),
    ("bullish",None,"breakout_down_strong"),
    ("bullish",None,"breakout_down_weak"),
    ("bullish",None,"bullish_pressure"),
    ("bullish",None,"bearish_pressure"),
    ("bullish",None,"inside_bar"),
    ("bullish",None,"exhaustion_up"),
    ("bullish",None,"exhaustion_down"),
    ("bullish",None,"trend_continuation_up"),
    ("bullish",None,"trend_continuation_down"),
    ("bullish",None,"indecision"),
    ("bullish",None,"inside_bullish_weak"),
    ("bullish",None,"inside_bearish_weak"),
    ("bullish",None,None),


    # -------- neutral --------

    ("neutral","up","breakout_up_strong"),
    ("neutral","up","breakout_up_weak"),
    ("neutral","up","breakout_down_strong"),
    ("neutral","up","breakout_down_weak"),
    ("neutral","up","bullish_pressure"),
    ("neutral","up","bearish_pressure"),
    ("neutral","up","inside_bar"),
    ("neutral","up","exhaustion_up"),
    ("neutral","up","exhaustion_down"),
    ("neutral","up","trend_continuation_up"),
    ("neutral","up","trend_continuation_down"),
    ("neutral","up","indecision"),
    ("neutral","up","inside_bullish_weak"),
    ("neutral","up","inside_bearish_weak"),
    ("neutral","up",None),

    ("neutral","down","breakout_up_strong"),
    ("neutral","down","breakout_up_weak"),
    ("neutral","down","breakout_down_strong"),
    ("neutral","down","breakout_down_weak"),
    ("neutral","down","bullish_pressure"),
    ("neutral","down","bearish_pressure"),
    ("neutral","down","inside_bar"),
    ("neutral","down","exhaustion_up"),
    ("neutral","down","exhaustion_down"),
    ("neutral","down","trend_continuation_up"),
    ("neutral","down","trend_continuation_down"),
    ("neutral","down","indecision"),
    ("neutral","down","inside_bullish_weak"),
    ("neutral","down","inside_bearish_weak"),
    ("neutral","down",None),

    ("neutral","range","breakout_up_strong"),
    ("neutral","range","breakout_up_weak"),
    ("neutral","range","breakout_down_strong"),
    ("neutral","range","breakout_down_weak"),
    ("neutral","range","bullish_pressure"),
    ("neutral","range","bearish_pressure"),
    ("neutral","range","inside_bar"),
    ("neutral","range","exhaustion_up"),
    ("neutral","range","exhaustion_down"),
    ("neutral","range","trend_continuation_up"),
    ("neutral","range","trend_continuation_down"),
    ("neutral","range","indecision"),
    ("neutral","range","inside_bullish_weak"),
    ("neutral","range","inside_bearish_weak"),
    ("neutral","range",None),

    ("neutral",None,"breakout_up_strong"),
    ("neutral",None,"breakout_up_weak"),
    ("neutral",None,"breakout_down_strong"),
    ("neutral",None,"breakout_down_weak"),
    ("neutral",None,"bullish_pressure"),
    ("neutral",None,"bearish_pressure"),
    ("neutral",None,"inside_bar"),
    ("neutral",None,"exhaustion_up"),
    ("neutral",None,"exhaustion_down"),
    ("neutral",None,"trend_continuation_up"),
    ("neutral",None,"trend_continuation_down"),
    ("neutral",None,"indecision"),
    ("neutral",None,"inside_bullish_weak"),
    ("neutral",None,"inside_bearish_weak"),
    ("neutral",None,None),
}

# ------------------------------
# SHORT COMBINATIONS
# ------------------------------

SHORT_RULES = {

    # -------- bearish --------

    ("bearish","up","breakout_up_strong"),
    ("bearish","up","breakout_up_weak"),
    ("bearish","up","breakout_down_strong"),
    ("bearish","up","breakout_down_weak"),
    ("bearish","up","bullish_pressure"),
    ("bearish","up","bearish_pressure"),
    ("bearish","up","inside_bar"),
    ("bearish","up","exhaustion_up"),
    ("bearish","up","exhaustion_down"),
    ("bearish","up","trend_continuation_up"),
    ("bearish","up","trend_continuation_down"),
    ("bearish","up","indecision"),
    ("bearish","up","inside_bullish_weak"),
    ("bearish","up","inside_bearish_weak"),
    ("bearish","up",None),

    ("bearish","down","breakout_up_strong"),
    ("bearish","down","breakout_up_weak"),
    ("bearish","down","breakout_down_strong"),
    ("bearish","down","breakout_down_weak"),
    ("bearish","down","bullish_pressure"),
    ("bearish","down","bearish_pressure"),
    #("bearish","down","inside_bar"),
    ("bearish","down","exhaustion_up"),
    ("bearish","down","exhaustion_down"),
    ("bearish","down","trend_continuation_up"),
    ("bearish","down","trend_continuation_down"),
    ("bearish","down","indecision"),
    ("bearish","down","inside_bullish_weak"),
    ("bearish","down","inside_bearish_weak"),
    ("bearish","down",None),

    ("bearish","range","breakout_up_strong"),
    ("bearish","range","breakout_up_weak"),
    ("bearish","range","breakout_down_strong"),
    ("bearish","range","breakout_down_weak"),
    ("bearish","range","bullish_pressure"),
    ("bearish","range","bearish_pressure"),
    ("bearish","range","inside_bar"),
    ("bearish","range","exhaustion_up"),
    ("bearish","range","exhaustion_down"),
    ("bearish","range","trend_continuation_up"),
    ("bearish","range","trend_continuation_down"),
    ("bearish","range","indecision"),
    ("bearish","range","inside_bullish_weak"),
    ("bearish","range","inside_bearish_weak"),
    ("bearish","range",None),

    ("bearish",None,"breakout_up_strong"),
    ("bearish",None,"breakout_up_weak"),
    ("bearish",None,"breakout_down_strong"),
    ("bearish",None,"breakout_down_weak"),
    ("bearish",None,"bullish_pressure"),
    ("bearish",None,"bearish_pressure"),
    ("bearish",None,"inside_bar"),
    ("bearish",None,"exhaustion_up"),
    ("bearish",None,"exhaustion_down"),
    ("bearish",None,"trend_continuation_up"),
    ("bearish",None,"trend_continuation_down"),
    ("bearish",None,"indecision"),
    ("bearish",None,"inside_bullish_weak"),
    ("bearish",None,"inside_bearish_weak"),
    ("bearish",None,None),


    # -------- neutral --------

    ("neutral","up","breakout_up_strong"),
    ("neutral","up","breakout_up_weak"),
    ("neutral","up","breakout_down_strong"),
    ("neutral","up","breakout_down_weak"),
    ("neutral","up","bullish_pressure"),
    ("neutral","up","bearish_pressure"),
    ("neutral","up","inside_bar"),
    ("neutral","up","exhaustion_up"),
    ("neutral","up","exhaustion_down"),
    ("neutral","up","trend_continuation_up"),
    ("neutral","up","trend_continuation_down"),
    ("neutral","up","indecision"),
    ("neutral","up","inside_bullish_weak"),
    ("neutral","up","inside_bearish_weak"),
    ("neutral","up",None),

    ("neutral","down","breakout_up_strong"),
    ("neutral","down","breakout_up_weak"),
    ("neutral","down","breakout_down_strong"),
    ("neutral","down","breakout_down_weak"),
    ("neutral","down","bullish_pressure"),
    ("neutral","down","bearish_pressure"),
    ("neutral","down","inside_bar"),
    ("neutral","down","exhaustion_up"),
    ("neutral","down","exhaustion_down"),
    ("neutral","down","trend_continuation_up"),
    ("neutral","down","trend_continuation_down"),
    ("neutral","down","indecision"),
    ("neutral","down","inside_bullish_weak"),
    ("neutral","down","inside_bearish_weak"),
    ("neutral","down",None),

    ("neutral","range","breakout_up_strong"),
    ("neutral","range","breakout_up_weak"),
    ("neutral","range","breakout_down_strong"),
    ("neutral","range","breakout_down_weak"),
    ("neutral","range","bullish_pressure"),
    ("neutral","range","bearish_pressure"),
    ("neutral","range","inside_bar"),
    ("neutral","range","exhaustion_up"),
    ("neutral","range","exhaustion_down"),
    ("neutral","range","trend_continuation_up"),
    ("neutral","range","trend_continuation_down"),
    ("neutral","range","indecision"),
    ("neutral","range","inside_bullish_weak"),
    ("neutral","range","inside_bearish_weak"),
    ("neutral","range",None),

    ("neutral",None,"breakout_up_strong"),
    ("neutral",None,"breakout_up_weak"),
    ("neutral",None,"breakout_down_strong"),
    ("neutral",None,"breakout_down_weak"),
    ("neutral",None,"bullish_pressure"),
    ("neutral",None,"bearish_pressure"),
    ("neutral",None,"inside_bar"),
    ("neutral",None,"exhaustion_up"),
    ("neutral",None,"exhaustion_down"),
    ("neutral",None,"trend_continuation_up"),
    ("neutral",None,"trend_continuation_down"),
    ("neutral",None,"indecision"),
    ("neutral",None,"inside_bullish_weak"),
    ("neutral",None,"inside_bearish_weak"),
    ("neutral",None,None),
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

