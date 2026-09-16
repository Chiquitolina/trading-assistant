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
    
class RedisReplayClock:
    """
    Replay clock backed by Redis.

    Used by processes that consume replay market data but do not
    share memory with the HistoricalReplayService.
    """

    def __init__(self, redis_client, key):
        self.redis = redis_client
        self.key = key

    def now_ms(self) -> int:
        value = self.redis.get(self.key)

        if value is None:
            raise RuntimeError(
                "Replay clock has not been initialized in Redis"
            )

        return int(value)