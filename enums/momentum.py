from enum import Enum


class Momentum(Enum):

    # ---------- DATA / INVALID ----------
    NO_DATA = "no_data"
    FLAT_ZERO_RANGE = "flat_zero_range"

    # ---------- STRUCTURE ----------
    INSIDE_BAR = "inside_bar"

    # ---------- BREAKOUTS ----------
    BREAKOUT_UP_STRONG = "breakout_up_strong"
    BREAKOUT_UP_WEAK = "breakout_up_weak"

    BREAKOUT_DOWN_STRONG = "breakout_down_strong"
    BREAKOUT_DOWN_WEAK = "breakout_down_weak"

    # ---------- EXHAUSTION ----------
    EXHAUSTION_UP = "exhaustion_up"
    EXHAUSTION_DOWN = "exhaustion_down"

    # ---------- CONTINUATION ----------
    TREND_CONTINUATION_UP = "trend_continuation_up"
    TREND_CONTINUATION_DOWN = "trend_continuation_down"

    # ---------- PRESSURE ----------
    BULLISH_PRESSURE = "bullish_pressure"
    BEARISH_PRESSURE = "bearish_pressure"

    # ---------- NEUTRAL ----------
    INDECISION = "indecision"

    # ---------- WEAK INTERNAL MOVES ----------
    INSIDE_BULLISH_WEAK = "inside_bullish_weak"
    INSIDE_BEARISH_WEAK = "inside_bearish_weak"