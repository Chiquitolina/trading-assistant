from enum import Enum

class ExitReason(Enum):

    TP = "TP"
    SL = "SL"

    TIME_STOP = "TIME_STOP"

    MANUAL_CLOSE = "MANUAL_CLOSE"

    STRATEGY_EXIT = "STRATEGY_EXIT"

    REVERSE = "REVERSE"

    UNKNOWN = "UNKNOWN"