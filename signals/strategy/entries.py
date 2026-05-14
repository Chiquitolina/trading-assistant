from enums.trend import Trend
from enums.direction import Direction
from enums.momentum import Momentum


# ------------------------------
# LONG COMBINATIONS
# ------------------------------

LONG_RULES = {

    # 🟢 Trend following limpio
    (
        Trend.BULLISH,
        Direction.UP,
        Momentum.BREAKOUT_UP_STRONG
    ),

    (
        Trend.BULLISH,
        Direction.UP,
        Momentum.TREND_CONTINUATION_UP
    ),

    # 🟢 Pullback en tendencia alcista
    (
        Trend.BULLISH,
        Direction.DOWN,
        Momentum.EXHAUSTION_DOWN
    ),

    (
        Trend.BULLISH,
        Direction.DOWN,
        Momentum.INSIDE_BAR
    ),

    (
        Trend.BULLISH,
        Direction.DOWN,
        Momentum.BREAKOUT_DOWN_WEAK
    ),

    # 🟢 Pullback dentro de uptrend
    (
        Trend.BULLISH,
        Direction.UP,
        Momentum.EXHAUSTION_DOWN
    ),

    (
        Trend.BULLISH,
        Direction.UP,
        Momentum.BREAKOUT_DOWN_WEAK
    ),

    (
        Trend.BULLISH,
        Direction.UP,
        Momentum.INSIDE_BULLISH_WEAK
    ),
}


# ------------------------------
# SHORT COMBINATIONS
# ------------------------------

SHORT_RULES = {

    (
        Trend.BEARISH,
        Direction.DOWN,
        Momentum.TREND_CONTINUATION_DOWN
    ),

    # 🟡 adicionales
    (
        Trend.BEARISH,
        Direction.DOWN,
        Momentum.BEARISH_PRESSURE
    ),
}


# ------------------------------
# LONG SETUP
# ------------------------------

def long_setup(
    trend,
    direction,
    momentum
) -> bool:

    return (
        trend,
        direction,
        momentum
    ) in LONG_RULES


# ------------------------------
# SHORT SETUP
# ------------------------------

def short_setup(
    trend,
    direction,
    momentum
) -> bool:

    return (
        trend,
        direction,
        momentum
    ) in SHORT_RULES