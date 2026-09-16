import json

import redis

from engine.live.data.redis_market_data_protocol import (
    HEARTBEAT_KEY,
    REPLAY_CLOCK_KEY,
    STATUS_KEY,
)
from engine.replay.replay_clock import ReplayClock


class HistoricalReplayService:
    def __init__(
        self,
        host="127.0.0.1",
        port=6380,
        db=0,
    ):
        self.redis = redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
        )

        self.clock = ReplayClock()
        self.running = False
        self.ready = False

    def initialize(
        self,
        timestamp_ms,
    ):
        self.redis.ping()

        self.clock.set(
            timestamp_ms
        )

        self._publish_clock()

        self.running = True
        self.ready = True

        self._publish_heartbeat()
        self._publish_status()

    def set_market_time(
        self,
        timestamp_ms,
    ):
        if not self.running:
            raise RuntimeError(
                "HistoricalReplayService "
                "has not been initialized"
            )

        self.clock.set(
            timestamp_ms
        )

        self._publish_clock()
        self._publish_heartbeat()

    def stop(self):
        self.ready = False
        self.running = False

        self._publish_status()

    def _publish_clock(self):
        self.redis.set(
            REPLAY_CLOCK_KEY,
            self.clock.now_ms(),
        )

    def _publish_heartbeat(self):
        self.redis.set(
            HEARTBEAT_KEY,
            self.clock.now_ms(),
        )

    def _publish_status(self):
        status = {
            "phase": (
                "READY"
                if self.ready
                else "STOPPED"
            ),
            "running": self.running,
            "source": "replay",
            "replay_ready": self.ready,
            "ws_connected": False,
            "market_timestamp": (
                self.clock.now_ms()
                if self.clock.initialized
                else None
            ),
        }

        self.redis.set(
            STATUS_KEY,
            json.dumps(status),
        )