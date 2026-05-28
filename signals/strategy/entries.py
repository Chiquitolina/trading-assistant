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

LONG_OLD_RULES = {
    # bullish / up
    (Trend.BULLISH, Direction.UP, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BULLISH, Direction.UP, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BULLISH, Direction.UP, Momentum.BULLISH_PRESSURE),
    (Trend.BULLISH, Direction.UP, Momentum.BEARISH_PRESSURE),
    (Trend.BULLISH, Direction.UP, Momentum.EXHAUSTION_UP),
    (Trend.BULLISH, Direction.UP, Momentum.EXHAUSTION_DOWN),
    (Trend.BULLISH, Direction.UP, Momentum.TREND_CONTINUATION_UP),
    (Trend.BULLISH, Direction.UP, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BULLISH, Direction.UP, Momentum.INDECISION),
    (Trend.BULLISH, Direction.UP, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BULLISH, Direction.UP, None),

    # bullish / down
    (Trend.BULLISH, Direction.DOWN, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BULLISH, Direction.DOWN, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BULLISH, Direction.DOWN, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BULLISH, Direction.DOWN, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BULLISH, Direction.DOWN, Momentum.BULLISH_PRESSURE),
    (Trend.BULLISH, Direction.DOWN, Momentum.BEARISH_PRESSURE),
    (Trend.BULLISH, Direction.DOWN, Momentum.INSIDE_BAR),
    (Trend.BULLISH, Direction.DOWN, Momentum.EXHAUSTION_UP),
    (Trend.BULLISH, Direction.DOWN, Momentum.EXHAUSTION_DOWN),
    (Trend.BULLISH, Direction.DOWN, Momentum.TREND_CONTINUATION_UP),
    (Trend.BULLISH, Direction.DOWN, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BULLISH, Direction.DOWN, Momentum.INDECISION),
    (Trend.BULLISH, Direction.DOWN, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BULLISH, Direction.DOWN, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BULLISH, Direction.DOWN, None),

    # bullish / range
    (Trend.BULLISH, Direction.RANGE, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BULLISH, Direction.RANGE, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BULLISH, Direction.RANGE, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BULLISH, Direction.RANGE, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BULLISH, Direction.RANGE, Momentum.BULLISH_PRESSURE),
    (Trend.BULLISH, Direction.RANGE, Momentum.BEARISH_PRESSURE),
    (Trend.BULLISH, Direction.RANGE, Momentum.INSIDE_BAR),
    (Trend.BULLISH, Direction.RANGE, Momentum.EXHAUSTION_UP),
    (Trend.BULLISH, Direction.RANGE, Momentum.EXHAUSTION_DOWN),
    (Trend.BULLISH, Direction.RANGE, Momentum.TREND_CONTINUATION_UP),
    (Trend.BULLISH, Direction.RANGE, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BULLISH, Direction.RANGE, Momentum.INDECISION),
    (Trend.BULLISH, Direction.RANGE, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BULLISH, Direction.RANGE, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BULLISH, Direction.RANGE, None),

    # bullish / None
    (Trend.BULLISH, None, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BULLISH, None, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BULLISH, None, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BULLISH, None, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BULLISH, None, Momentum.BULLISH_PRESSURE),
    (Trend.BULLISH, None, Momentum.BEARISH_PRESSURE),
    (Trend.BULLISH, None, Momentum.INSIDE_BAR),
    (Trend.BULLISH, None, Momentum.EXHAUSTION_UP),
    (Trend.BULLISH, None, Momentum.EXHAUSTION_DOWN),
    (Trend.BULLISH, None, Momentum.TREND_CONTINUATION_UP),
    (Trend.BULLISH, None, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BULLISH, None, Momentum.INDECISION),
    (Trend.BULLISH, None, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BULLISH, None, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BULLISH, None, None),

    # neutral / up
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_UP_STRONG),
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_UP_WEAK),
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.NEUTRAL, Direction.UP, Momentum.BULLISH_PRESSURE),
    (Trend.NEUTRAL, Direction.UP, Momentum.BEARISH_PRESSURE),
    (Trend.NEUTRAL, Direction.UP, Momentum.INSIDE_BAR),
    (Trend.NEUTRAL, Direction.UP, Momentum.EXHAUSTION_UP),
    (Trend.NEUTRAL, Direction.UP, Momentum.EXHAUSTION_DOWN),
    (Trend.NEUTRAL, Direction.UP, Momentum.TREND_CONTINUATION_UP),
    (Trend.NEUTRAL, Direction.UP, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.NEUTRAL, Direction.UP, Momentum.INDECISION),
    (Trend.NEUTRAL, Direction.UP, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.NEUTRAL, Direction.UP, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.NEUTRAL, Direction.UP, None),

    # neutral / down
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_UP_STRONG),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_UP_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BULLISH_PRESSURE),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BEARISH_PRESSURE),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INSIDE_BAR),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.EXHAUSTION_UP),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.EXHAUSTION_DOWN),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.TREND_CONTINUATION_UP),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INDECISION),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, None),
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

SHORT_OLD_RULES = {

    # =================================
    # bearish / up
    # =================================

    (Trend.BEARISH, Direction.UP, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BEARISH, Direction.UP, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BEARISH, Direction.UP, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BEARISH, Direction.UP, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BEARISH, Direction.UP, Momentum.BULLISH_PRESSURE),
    (Trend.BEARISH, Direction.UP, Momentum.BEARISH_PRESSURE),
    (Trend.BEARISH, Direction.UP, Momentum.INSIDE_BAR),
    (Trend.BEARISH, Direction.UP, Momentum.EXHAUSTION_UP),
    (Trend.BEARISH, Direction.UP, Momentum.EXHAUSTION_DOWN),
    (Trend.BEARISH, Direction.UP, Momentum.TREND_CONTINUATION_UP),
    (Trend.BEARISH, Direction.UP, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BEARISH, Direction.UP, Momentum.INDECISION),
    (Trend.BEARISH, Direction.UP, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BEARISH, Direction.UP, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BEARISH, Direction.UP, None),

    # =================================
    # bearish / down
    # =================================

    (Trend.BEARISH, Direction.DOWN, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BEARISH, Direction.DOWN, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BEARISH, Direction.DOWN, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BEARISH, Direction.DOWN, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BEARISH, Direction.DOWN, Momentum.BULLISH_PRESSURE),
    (Trend.BEARISH, Direction.DOWN, Momentum.BEARISH_PRESSURE),

    # OLD tenia inside_bar comentado
    # (Trend.BEARISH, Direction.DOWN, Momentum.INSIDE_BAR),

    (Trend.BEARISH, Direction.DOWN, Momentum.EXHAUSTION_UP),
    (Trend.BEARISH, Direction.DOWN, Momentum.EXHAUSTION_DOWN),
    (Trend.BEARISH, Direction.DOWN, Momentum.TREND_CONTINUATION_UP),
    (Trend.BEARISH, Direction.DOWN, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BEARISH, Direction.DOWN, Momentum.INDECISION),
    (Trend.BEARISH, Direction.DOWN, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BEARISH, Direction.DOWN, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BEARISH, Direction.DOWN, None),

    # =================================
    # bearish / range
    # =================================

    (Trend.BEARISH, Direction.RANGE, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BEARISH, Direction.RANGE, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BEARISH, Direction.RANGE, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BEARISH, Direction.RANGE, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BEARISH, Direction.RANGE, Momentum.BULLISH_PRESSURE),
    (Trend.BEARISH, Direction.RANGE, Momentum.BEARISH_PRESSURE),
    (Trend.BEARISH, Direction.RANGE, Momentum.INSIDE_BAR),
    (Trend.BEARISH, Direction.RANGE, Momentum.EXHAUSTION_UP),
    (Trend.BEARISH, Direction.RANGE, Momentum.EXHAUSTION_DOWN),
    (Trend.BEARISH, Direction.RANGE, Momentum.TREND_CONTINUATION_UP),
    (Trend.BEARISH, Direction.RANGE, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BEARISH, Direction.RANGE, Momentum.INDECISION),
    (Trend.BEARISH, Direction.RANGE, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BEARISH, Direction.RANGE, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BEARISH, Direction.RANGE, None),

    # =================================
    # bearish / None
    # =================================

    (Trend.BEARISH, None, Momentum.BREAKOUT_UP_STRONG),
    (Trend.BEARISH, None, Momentum.BREAKOUT_UP_WEAK),
    (Trend.BEARISH, None, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.BEARISH, None, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.BEARISH, None, Momentum.BULLISH_PRESSURE),
    (Trend.BEARISH, None, Momentum.BEARISH_PRESSURE),
    (Trend.BEARISH, None, Momentum.INSIDE_BAR),
    (Trend.BEARISH, None, Momentum.EXHAUSTION_UP),
    (Trend.BEARISH, None, Momentum.EXHAUSTION_DOWN),
    (Trend.BEARISH, None, Momentum.TREND_CONTINUATION_UP),
    (Trend.BEARISH, None, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.BEARISH, None, Momentum.INDECISION),
    (Trend.BEARISH, None, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.BEARISH, None, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.BEARISH, None, None),

    # =================================
    # neutral / up
    # =================================

    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_UP_STRONG),
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_UP_WEAK),
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.NEUTRAL, Direction.UP, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.NEUTRAL, Direction.UP, Momentum.BULLISH_PRESSURE),
    (Trend.NEUTRAL, Direction.UP, Momentum.BEARISH_PRESSURE),
    (Trend.NEUTRAL, Direction.UP, Momentum.INSIDE_BAR),
    (Trend.NEUTRAL, Direction.UP, Momentum.EXHAUSTION_UP),
    (Trend.NEUTRAL, Direction.UP, Momentum.EXHAUSTION_DOWN),
    (Trend.NEUTRAL, Direction.UP, Momentum.TREND_CONTINUATION_UP),
    (Trend.NEUTRAL, Direction.UP, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.NEUTRAL, Direction.UP, Momentum.INDECISION),
    (Trend.NEUTRAL, Direction.UP, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.NEUTRAL, Direction.UP, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.NEUTRAL, Direction.UP, None),

    # =================================
    # neutral / down
    # =================================

    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_UP_STRONG),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_UP_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BULLISH_PRESSURE),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.BEARISH_PRESSURE),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INSIDE_BAR),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.EXHAUSTION_UP),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.EXHAUSTION_DOWN),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.TREND_CONTINUATION_UP),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INDECISION),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.NEUTRAL, Direction.DOWN, None),

    # =================================
    # neutral / range
    # =================================

    (Trend.NEUTRAL, Direction.RANGE, Momentum.BREAKOUT_UP_STRONG),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.BREAKOUT_UP_WEAK),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.BULLISH_PRESSURE),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.BEARISH_PRESSURE),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.INSIDE_BAR),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.EXHAUSTION_UP),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.EXHAUSTION_DOWN),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.TREND_CONTINUATION_UP),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.INDECISION),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.NEUTRAL, Direction.RANGE, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.NEUTRAL, Direction.RANGE, None),

    # =================================
    # neutral / None
    # =================================

    (Trend.NEUTRAL, None, Momentum.BREAKOUT_UP_STRONG),
    (Trend.NEUTRAL, None, Momentum.BREAKOUT_UP_WEAK),
    (Trend.NEUTRAL, None, Momentum.BREAKOUT_DOWN_STRONG),
    (Trend.NEUTRAL, None, Momentum.BREAKOUT_DOWN_WEAK),
    (Trend.NEUTRAL, None, Momentum.BULLISH_PRESSURE),
    (Trend.NEUTRAL, None, Momentum.BEARISH_PRESSURE),
    (Trend.NEUTRAL, None, Momentum.INSIDE_BAR),
    (Trend.NEUTRAL, None, Momentum.EXHAUSTION_UP),
    (Trend.NEUTRAL, None, Momentum.EXHAUSTION_DOWN),
    (Trend.NEUTRAL, None, Momentum.TREND_CONTINUATION_UP),
    (Trend.NEUTRAL, None, Momentum.TREND_CONTINUATION_DOWN),
    (Trend.NEUTRAL, None, Momentum.INDECISION),
    (Trend.NEUTRAL, None, Momentum.INSIDE_BULLISH_WEAK),
    (Trend.NEUTRAL, None, Momentum.INSIDE_BEARISH_WEAK),
    (Trend.NEUTRAL, None, None),
}

# ------------------------------
# ENTRY RULE SETS
# ------------------------------

ENTRY_RULE_SETS = {
    "standard": {
        "long": LONG_RULES,
        "short": SHORT_RULES,
    },

    "old": {
        "long": LONG_OLD_RULES,
        "short": SHORT_OLD_RULES,
    },
}


# ------------------------------
# LONG SETUP
# ------------------------------

def long_setup(
    trend,
    direction,
    momentum,
    entry_rules="standard"
) -> bool:

    rules = ENTRY_RULE_SETS.get(
        entry_rules,
        ENTRY_RULE_SETS["standard"]
    )["long"]

    return (
        trend,
        direction,
        momentum
    ) in rules


# ------------------------------
# SHORT SETUP
# ------------------------------

def short_setup(
    trend,
    direction,
    momentum,
    entry_rules="standard"
) -> bool:

    rules = ENTRY_RULE_SETS.get(
        entry_rules,
        ENTRY_RULE_SETS["standard"]
    )["short"]

    return (
        trend,
        direction,
        momentum
    ) in rules