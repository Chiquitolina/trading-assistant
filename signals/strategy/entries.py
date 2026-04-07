# signals/strategy/entries.py

# ------------------------------
# LONG COMBINATIONS
# ------------------------------
# signals/strategy/entries.py

LONG_RULES = {

    # -------- bullish --------

    ("bullish","up","breakout_up_strong"),
    ("bullish","up","breakout_up_weak"),
    ("bullish","up","bullish_pressure"),
    #("bullish","up","breakout_down_strong"),
    ("bullish","up","breakout_down_weak"),
    #("bullish","up","bearish_pressure"),
    ("bullish","up",None),

    ("bullish","down","breakout_up_strong"),
    ("bullish","down","breakout_up_weak"),
    ("bullish","down","bullish_pressure"),
    #("bullish","down","breakout_down_strong"),
    #("bullish","down","breakout_down_weak"),
    #("bullish","down","bearish_pressure"),
    #("bullish","down",None),

    ("bullish","range","breakout_up_strong"),
    ("bullish","range","breakout_up_weak"),
    ("bullish","range","bullish_pressure"),
    #("bullish","range","breakout_down_strong"),
    #("bullish","range","breakout_down_weak"),
    #("bullish","range","bearish_pressure"),
    ("bullish","range",None),

    ("bullish",None,"breakout_up_strong"),
    ("bullish",None,"breakout_up_weak"),
    ("bullish",None,"bullish_pressure"),
    #("bullish",None,"breakout_down_strong"),
    #("bullish",None,"breakout_down_weak"),
    #("bullish",None,"bearish_pressure"),
    ("bullish",None,None),


    # -------- neutral --------

    ("neutral","up","breakout_up_strong"),
    ("neutral","up","breakout_up_weak"),
    ("neutral","up","bullish_pressure"),
    ("neutral","up","breakout_down_strong"),
    ("neutral","up","breakout_down_weak"),
    ("neutral","up","bearish_pressure"),
    #("neutral","up",None),

    ("neutral","down","breakout_up_strong"),
    #("neutral","down","breakout_up_weak"),
    ("neutral","down","bullish_pressure"),
    #("neutral","down","breakout_down_strong"),
    #("neutral","down","breakout_down_weak"),
    #("neutral","down","bearish_pressure"),
    #("neutral","down",None),

    #("neutral","range","breakout_up_strong"),
    #("neutral","range","breakout_up_weak"),
    #("neutral","range","bullish_pressure"),
    #("neutral","range","breakout_down_strong"),
    #("neutral","range","breakout_down_weak"),
    #("neutral","range","bearish_pressure"),
    #("neutral","range",None),

    ("neutral",None,"breakout_up_strong"),
    ("neutral",None,"breakout_up_weak"),
    ("neutral",None,"bullish_pressure"),
    ("neutral",None,"breakout_down_strong"),
    ("neutral",None,"breakout_down_weak"),
    ("neutral",None,"bearish_pressure"),
    ("neutral",None,None),
}


# ------------------------------
# SHORT COMBINATIONS
# ------------------------------
SHORT_RULES = {

    # -------- bearish --------

    #("bearish","up","breakout_up_strong"),
    #("bearish","up","breakout_up_weak"),
    #("bearish","up","bullish_pressure"),
    #("bearish","up","breakout_down_strong"),
    #("bearish","up","breakout_down_weak"),
    #("bearish","up","bearish_pressure"),
    #("bearish","up",None),

    #("bearish","down","breakout_up_strong"),
    #("bearish","down","breakout_up_weak"),
    #("bearish","down","bullish_pressure"),
    ("bearish","down","breakout_down_strong"),
    ("bearish","down","breakout_down_weak"),
    ("bearish","down","bearish_pressure"),
    ("bearish","down",None),

    #("bearish","range","breakout_up_strong"),
    #("bearish","range","breakout_up_weak"),
    #("bearish","range","bullish_pressure"),
    ("bearish","range","breakout_down_strong"),
    ("bearish","range","breakout_down_weak"),
    ("bearish","range","bearish_pressure"),
    ("bearish","range",None),

    ("bearish",None,"breakout_up_strong"),
    ("bearish",None,"breakout_up_weak"),
    #("bearish",None,"bullish_pressure"),
    ("bearish",None,"breakout_down_strong"),
    ("bearish",None,"breakout_down_weak"),
    ("bearish",None,"bearish_pressure"),
    ("bearish",None,None),


    # -------- neutral --------

    #("neutral","up","breakout_up_strong"),
    #("neutral","up","breakout_up_weak"),
    #("neutral","up","bullish_pressure"),
    ("neutral","up","breakout_down_strong"),
    #("neutral","up","breakout_down_weak"),
    ("neutral","up","bearish_pressure"),
    #("neutral","up",None),

    #("neutral","down","breakout_up_strong"),
    #("neutral","down","breakout_up_weak"),
    #("neutral","down","bullish_pressure"),
    #("neutral","down","breakout_down_strong"),
    #("neutral","down","breakout_down_weak"),
    ("neutral","down","bearish_pressure"),
    #("neutral","down",None),

    #("neutral","range","breakout_up_strong"),
    #("neutral","range","breakout_up_weak"),
    #("neutral","range","bullish_pressure"),
    #("neutral","range","breakout_down_strong"),
    #("neutral","range","breakout_down_weak"),
    #("neutral","range","bearish_pressure"),
    #("neutral","range",None),

    ("neutral",None,"breakout_up_strong"),
    ("neutral",None,"breakout_up_weak"),
    ("neutral",None,"bullish_pressure"),
    ("neutral",None,"breakout_down_strong"),
    ("neutral",None,"breakout_down_weak"),
    ("neutral",None,"bearish_pressure"),
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