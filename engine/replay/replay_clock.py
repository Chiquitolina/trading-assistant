import time
from typing import Optional


class RealClock:
    """
    Clock backed by the real system time.

    Used by LIVE services when an explicit clock dependency
    is required.
    """

    def now_ms(self) -> int:
        return int(time.time() * 1000)


class ReplayClock:
    """
    Deterministic clock controlled by the historical replay engine.

    Time only advances when the replay engine explicitly moves it.
    """

    def __init__(self):
        self._current_ms: Optional[int] = None

    def set(self, timestamp_ms: int) -> None:
        self._current_ms = int(timestamp_ms)

    def now_ms(self) -> int:
        if self._current_ms is None:
            raise RuntimeError(
                "ReplayClock has not been initialized"
            )

        return self._current_ms

    @property
    def initialized(self) -> bool:
        return self._current_ms is not None