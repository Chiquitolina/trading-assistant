def long_setup(trend, direction, momentum) -> bool:
    return (
        trend == "bullish"
        and direction == "up"
        and momentum in ['breakout_up_strong', 'bullish_pressure']
    )


def short_setup(trend, direction, momentum) -> bool:
    return (
        trend == "bearish"
        and direction == "down"
        and momentum in {
            "breakout_down_strong",
            "bearish_pressure",
            }
    )