def long_setup(trend, direction, momentum) -> bool:
    return (
        trend in ["bullish", "neutral"]
        # and direction in ["up", "range"]
        #and momentum in {'breakout_up_strong', 'breakout_up_weak', 'bullish_pressure'}
    )


def short_setup(trend, direction, momentum) -> bool:
    return (
        trend in ["bearish", "neutral"]
       # and direction in ["down", "range"]
       # and momentum in {
       #     "breakout_down_strong",
       #     "breakout_down_weak",
       #     "bearish_pressure",
       #    }
    )