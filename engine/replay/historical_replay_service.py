import json

import redis
import time

from engine.live.data.redis_market_data_protocol import (
    HEARTBEAT_KEY,
    REPLAY_BOUNDARY_READY_KEY,
    REPLAY_CLOCK_KEY,
    STATUS_KEY,
    replay_engine_processed_key,
    replay_provider_applied_key,
    replay_consumer_ready_key,
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
        

        self.redis.delete(
            REPLAY_BOUNDARY_READY_KEY
        )

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
        self._publish_status()

    def stop(self):
        self.ready = False
        self.running = False

        self._publish_status()
            
    def clear_consumer_barrier(
        self,
        consumer_name,
    ):
        self.redis.delete(
            replay_provider_applied_key(
                consumer_name
            )
        )

        self.redis.delete(
            replay_engine_processed_key(
                consumer_name
            )
        )

        self.redis.delete(
            replay_consumer_ready_key(
                consumer_name
            )
        )
        
    def wait_consumer_ready(
        self,
        consumer_name,
        timeout_seconds=120.0,
    ):
        key = replay_consumer_ready_key(
            consumer_name
        )

        deadline = (
            time.monotonic()
            + float(timeout_seconds)
        )

        while time.monotonic() < deadline:
            ready = self.redis.get(key)

            if ready == "1":
                return True

            time.sleep(0.01)

        raise TimeoutError(
            "Replay consumer startup timeout | "
            f"consumer={consumer_name} | "
            f"ready={self.redis.get(key)}"
        )


    def publish_boundary_ready(
        self,
        timestamp_ms,
        end_stream_id,
    ):
        payload = {
            "timestamp": int(timestamp_ms),
            "end_stream_id": str(
                end_stream_id
            ),
        }

        self.redis.set(
            REPLAY_BOUNDARY_READY_KEY,
            json.dumps(payload),
        )

        return payload


    def wait_provider_applied(
        self,
        consumer_name,
        expected_stream_id,
        timeout_seconds=10.0,
    ):
        key = replay_provider_applied_key(
            consumer_name
        )

        deadline = (
            time.monotonic()
            + float(timeout_seconds)
        )

        while time.monotonic() < deadline:
            applied = self.redis.get(key)

            if (
                applied is not None
                and self._stream_id_gte(
                    applied,
                    expected_stream_id,
                )
            ):
                return applied

            time.sleep(0.01)

        raise TimeoutError(
            "Replay provider barrier timeout | "
            f"consumer={consumer_name} | "
            f"expected={expected_stream_id} | "
            f"actual={self.redis.get(key)}"
        )


    def wait_engine_processed(
        self,
        consumer_name,
        expected_timestamp,
        timeout_seconds=10.0,
    ):
        key = replay_engine_processed_key(
            consumer_name
        )

        expected_timestamp = int(
            expected_timestamp
        )

        deadline = (
            time.monotonic()
            + float(timeout_seconds)
        )

        while time.monotonic() < deadline:
            processed = self.redis.get(key)

            if (
                processed is not None
                and int(processed)
                >= expected_timestamp
            ):
                return int(processed)

            time.sleep(0.01)

        raise TimeoutError(
            "Replay engine barrier timeout | "
            f"consumer={consumer_name} | "
            f"expected={expected_timestamp} | "
            f"actual={self.redis.get(key)}"
        )


    @staticmethod
    def _stream_id_tuple(
        stream_id,
    ):
        milliseconds, sequence = str(
            stream_id
        ).split("-", 1)

        return (
            int(milliseconds),
            int(sequence),
        )


    @classmethod
    def _stream_id_gte(
        cls,
        actual,
        expected,
    ):
        return (
            cls._stream_id_tuple(actual)
            >= cls._stream_id_tuple(expected)
        )

    def _publish_clock(self):
        self.redis.set(
            REPLAY_CLOCK_KEY,
            self.clock.now_ms(),
        )

    def _publish_heartbeat(self):
        self.redis.set(
            HEARTBEAT_KEY,
            int(time.time() * 1000),
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