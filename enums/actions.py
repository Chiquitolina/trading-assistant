from enum import Enum


class Action(Enum):

    LONG = "LONG"
    SHORT = "SHORT"

    HOLD = "HOLD"
    CLOSE = "CLOSE"

    REVERSE_TO_LONG = "REVERSE_TO_LONG"
    REVERSE_TO_SHORT = "REVERSE_TO_SHORT"