import argparse
import json
import signal
import threading
import time
import uuid
from collections import deque

from config.strategies.v1 import SYMBOLS
from engine.live.data.redis_market_data_protocol import (
    MICRO_FLOW_DEFAULT_SHARD_COUNT,
    MICRO_FLOW_SHARD_HEARTBEAT_TTL_SECONDS,
    MICRO_FLOW_SHARD_LOCK_TTL_SECONDS,
    micro_flow_shard_heartbeat_key,
    micro_flow_shard_lock_key,
    micro_flow_shard_status_key,
)
from engine.live.data.redis_market_data_publisher import (
    RedisMarketDataPublisher,
)
from engine.live.research.micro_flow_second_aggregator import (
    MicroFlowSecondAggregator,
)
from engine.live.ws.ws_client import WSClient


SHARD_LOCK_STARTUP_WAIT_SECONDS = MICRO_FLOW_SHARD_LOCK_TTL_SECONDS + 15
SHARD_LOCK_STARTUP_POLL_SECONDS = 2
SHARD_DISCONNECTED_RESTART_SECONDS = 180
SHARD_STATUS_INTERVAL_SECONDS = 2
SHARD_HEARTBEAT_INTERVAL_SECONDS = 5


class MicroFlowShardService:
    """Independent aggTrade producer for one deterministic symbol shard.

    Each process owns one disjoint subset of Futures symbols, aggregates executed
    aggTrades into completed 1-second states and publishes them to the shared
    Micro Flow Redis stream. Transport gaps are shard-scoped so a failed process
    invalidates only its own symbols in the research collector.
    """

    def __init__(
        self,
        shard_id,
        shard_count=MICRO_FLOW_DEFAULT_SHARD_COUNT,
        redis_host="127.0.0.1",
        redis_port=6379,
        redis_db=0,
        chunk_size=60,
        stale_after=45,
        queue_maxlen=50_000,
        finalize_grace_ms=750,
    ):
        self.shard_id = int(shard_id)
        self.shard_count = int(shard_count)
        if self.shard_count <= 0:
            raise ValueError("shard_count must be > 0")
        if self.shard_id < 0 or self.shard_id >= self.shard_count:
            raise ValueError(
                f"shard_id must be in [0, {self.shard_count - 1}]"
            )

        all_symbols = list(dict.fromkeys(
            str(symbol).upper()
            for symbol in SYMBOLS
            if symbol
        ))
        self.symbols = [
            symbol
            for index, symbol in enumerate(all_symbols)
            if index % self.shard_count == self.shard_id
        ]
        if not self.symbols:
            raise RuntimeError(
                f"Micro Flow shard {self.shard_id} owns no symbols"
            )

        self.publisher = RedisMarketDataPublisher(
            host=redis_host,
            port=redis_port,
            db=redis_db,
        )
        self.redis = self.publisher.redis

        self.chunk_size = max(1, int(chunk_size))
        self.stale_after = max(10, int(stale_after))
        self.aggregator = MicroFlowSecondAggregator(
            finalize_grace_ms=finalize_grace_ms,
        )

        self.service_id = str(uuid.uuid4())
        self.running = False
        self.phase = "CREATED"
        self.stop_event = threading.Event()

        self.ws = None
        self.ws_thread = None
        self.ws_was_connected = False
        self.disconnected_since_monotonic = None

        self.queue_lock = threading.Lock()
        self.publish_queue = deque(maxlen=max(1000, int(queue_maxlen)))
        self.queue_dropped = 0
        self.seconds_published = 0
        self.publish_errors = 0
        self.last_published_at_ms = None
        self.messages_dropped_during_gap = 0

        self.transport_gap_open = False
        self.transport_gap_started_at_ms = None
        self.transport_gap_count = 0
        self.transport_gap_last_duration_ms = None

        self.started_at_ms = int(time.time() * 1000)
        self.last_status_monotonic = 0.0
        self.last_heartbeat_monotonic = 0.0

        self.status_key = micro_flow_shard_status_key(self.shard_id)
        self.heartbeat_key = micro_flow_shard_heartbeat_key(self.shard_id)
        self.lock_key = micro_flow_shard_lock_key(self.shard_id)

        self._refresh_lock_script = self.redis.register_script(
            """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
                redis.call('EXPIRE', KEYS[1], ARGV[2])
                return 1
            end
            return 0
            """
        )
        self._release_lock_script = self.redis.register_script(
            """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
                return redis.call('DEL', KEYS[1])
            end
            return 0
            """
        )

    def _acquire_lock_with_wait(self):
        deadline = time.monotonic() + SHARD_LOCK_STARTUP_WAIT_SECONDS
        waited = 0.0

        while self.running and time.monotonic() <= deadline:
            acquired = self.redis.set(
                self.lock_key,
                self.service_id,
                nx=True,
                ex=MICRO_FLOW_SHARD_LOCK_TTL_SECONDS,
            )
            if acquired:
                print(
                    f"[MICRO FLOW SHARD {self.shard_id}] "
                    f"producer lock acquired service_id={self.service_id} "
                    f"waited={waited:.1f}s"
                )
                return True

            owner = self.redis.get(self.lock_key)
            ttl = self.redis.ttl(self.lock_key)
            print(
                f"[MICRO FLOW SHARD {self.shard_id}] producer lock busy "
                f"owner={owner} ttl={ttl}s waited={waited:.1f}s"
            )
            self.stop_event.wait(SHARD_LOCK_STARTUP_POLL_SECONDS)
            waited += SHARD_LOCK_STARTUP_POLL_SECONDS

        return False

    def _refresh_lock(self):
        result = self._refresh_lock_script(
            keys=[self.lock_key],
            args=[self.service_id, MICRO_FLOW_SHARD_LOCK_TTL_SECONDS],
        )
        return bool(int(result or 0))

    def _release_lock(self):
        try:
            self._release_lock_script(
                keys=[self.lock_key],
                args=[self.service_id],
            )
        except Exception:
            pass

    def _enqueue_seconds(self, states):
        if not states:
            return 0

        queued = 0
        with self.queue_lock:
            for state in states:
                if len(self.publish_queue) >= self.publish_queue.maxlen:
                    self.queue_dropped += 1
                    continue

                normalized = dict(state)
                normalized["shard_id"] = self.shard_id
                normalized["shard_count"] = self.shard_count
                normalized["producer_service_id"] = self.service_id
                self.publish_queue.append(normalized)
                queued += 1

        return queued

    def _drain_queue(self, max_items=5000):
        batch = []
        with self.queue_lock:
            while self.publish_queue and len(batch) < int(max_items):
                batch.append(self.publish_queue.popleft())
        return batch

    def _flush_seconds(self):
        now_ms = int(time.time() * 1000)
        ready = self.aggregator.flush_ready(now_ms)
        self._enqueue_seconds(ready)

        batch = self._drain_queue(max_items=5000)
        if not batch:
            return 0

        try:
            published = self.publisher.publish_micro_flow_seconds(batch)
            self.seconds_published += int(published)
            self.last_published_at_ms = now_ms
            return int(published)
        except Exception as exc:
            self.publish_errors += 1
            self._enqueue_seconds(batch)
            print(
                f"[MICRO FLOW SHARD {self.shard_id}] publish error "
                f"error={type(exc).__name__}:{exc}"
            )
            return 0

    def _publish_transport_marker(self, event_type, timestamp_ms, **metadata):
        try:
            return self.publisher.publish_micro_flow_transport_event(
                event_type=event_type,
                timestamp_ms=int(timestamp_ms),
                scope="shard",
                shard_id=self.shard_id,
                shard_count=self.shard_count,
                service_id=self.service_id,
                symbols=self.symbols,
                **metadata,
            )
        except Exception as exc:
            self.publish_errors += 1
            print(
                f"[MICRO FLOW SHARD {self.shard_id}] transport marker error "
                f"event={event_type} error={type(exc).__name__}:{exc}"
            )
            return None

    def _open_transport_gap(self, detected_at_ms, reason):
        detected_at_ms = int(detected_at_ms)
        if self.transport_gap_open:
            return False

        self._flush_seconds()
        discarded = self.aggregator.reset_transport_state()
        self.transport_gap_open = True
        self.transport_gap_started_at_ms = detected_at_ms
        self.transport_gap_count += 1

        self._publish_transport_marker(
            "transport_gap_start",
            detected_at_ms,
            reason=str(reason),
            discarded_active_buckets=int(discarded),
        )

        print(
            f"[MICRO FLOW SHARD {self.shard_id}] transport gap opened "
            f"reason={reason} at={detected_at_ms} "
            f"symbols={len(self.symbols)} "
            f"discarded_active_buckets={discarded}"
        )
        return True

    def _close_transport_gap(self, reconnected_at_ms):
        if not self.transport_gap_open:
            return False

        reconnected_at_ms = int(reconnected_at_ms)
        started_at_ms = self.transport_gap_started_at_ms
        discarded = self.aggregator.reset_transport_state()
        duration_ms = (
            max(0, reconnected_at_ms - int(started_at_ms))
            if started_at_ms is not None
            else None
        )
        self.transport_gap_last_duration_ms = duration_ms

        self._publish_transport_marker(
            "transport_gap_end",
            reconnected_at_ms,
            gap_started_at_ms=started_at_ms,
            gap_duration_ms=duration_ms,
            discarded_active_buckets=int(discarded),
        )

        self.transport_gap_open = False
        self.transport_gap_started_at_ms = None

        print(
            f"[MICRO FLOW SHARD {self.shard_id}] transport gap closed "
            f"at={reconnected_at_ms} duration_ms={duration_ms} "
            f"discarded_active_buckets={discarded}"
        )
        return True

    def _on_ws_message(self, message):
        if not self.aggregator.is_agg_trade_message(message):
            return None

        if self.transport_gap_open:
            self.messages_dropped_during_gap += 1
            return {"type": "agg_trade_dropped_transport_gap"}

        ready = self.aggregator.ingest_ws_message(message)
        self._enqueue_seconds(ready)
        return {
            "type": "agg_trade",
            "queued_seconds": len(ready),
        }

    def _start_websocket(self):
        self.ws = WSClient(
            self._on_ws_message,
            timeframes=(),
            symbols=self.symbols,
            chunk_size=self.chunk_size,
            stale_after=self.stale_after,
            include_agg_trades=True,
            client_name=f"micro-flow-shard-{self.shard_id}",
            min_reconnect_interval=15,
            reconnect_delay_floor=5,
            reconnect_delay_cap=60,
            enable_coverage_audit=False,
        )
        self.ws.start()
        self.ws_thread = threading.Thread(
            target=self.ws.run,
            daemon=True,
            name=f"micro-flow-shard-{self.shard_id}-ws",
        )
        self.ws_thread.start()

    def _check_transport(self, now_ms):
        connected = bool(self.ws and self.ws.is_connected)

        if connected:
            if not self.ws_was_connected:
                self.ws_was_connected = True
                self.disconnected_since_monotonic = None
                self._close_transport_gap(now_ms)
            self.phase = "READY"
            return

        if self.ws_was_connected:
            self.ws_was_connected = False
            self.disconnected_since_monotonic = time.monotonic()
            self._open_transport_gap(now_ms, reason="ws_disconnected")
        elif self.disconnected_since_monotonic is None:
            self.disconnected_since_monotonic = time.monotonic()

        self.phase = "RECOVERING"

        disconnected_for = time.monotonic() - self.disconnected_since_monotonic
        if disconnected_for >= SHARD_DISCONNECTED_RESTART_SECONDS:
            raise RuntimeError(
                f"Micro Flow shard {self.shard_id} websocket remained "
                f"disconnected for {disconnected_for:.1f}s"
            )

    def _write_heartbeat(self):
        now_ms = int(time.time() * 1000)
        heartbeat = {
            "service_id": self.service_id,
            "shard_id": self.shard_id,
            "shard_count": self.shard_count,
            "timestamp": now_ms,
        }
        pipeline = self.redis.pipeline(transaction=False)
        pipeline.set(
            self.heartbeat_key,
            json.dumps(heartbeat, separators=(",", ":")),
            ex=MICRO_FLOW_SHARD_HEARTBEAT_TTL_SECONDS,
        )
        pipeline.execute()

        if not self._refresh_lock():
            raise RuntimeError(
                f"Micro Flow shard {self.shard_id} lost producer lock"
            )

    def _status_payload(self):
        return {
            "service_id": self.service_id,
            "shard_id": self.shard_id,
            "shard_count": self.shard_count,
            "phase": self.phase,
            "running": bool(self.running),
            "ws_connected": bool(self.ws and self.ws.is_connected),
            "ws_fatal_error": (
                getattr(self.ws, "fatal_error", None)
                if self.ws is not None
                else None
            ),
            "ws_forced_detaches": int(
                getattr(self.ws, "_forced_detach_count", 0)
                if self.ws is not None
                else 0
            ),
            "symbols_count": len(self.symbols),
            "symbols": list(self.symbols),
            "chunk_size": self.chunk_size,
            "stale_after": self.stale_after,
            "transport_gap_open": bool(self.transport_gap_open),
            "transport_gap_started_at_ms": self.transport_gap_started_at_ms,
            "transport_gap_count": int(self.transport_gap_count),
            "transport_gap_last_duration_ms": self.transport_gap_last_duration_ms,
            "messages_dropped_during_gap": int(
                self.messages_dropped_during_gap
            ),
            "seconds_published": int(self.seconds_published),
            "queue_depth": len(self.publish_queue),
            "queue_dropped": int(self.queue_dropped),
            "publish_errors": int(self.publish_errors),
            "last_published_at_ms": self.last_published_at_ms,
            "aggregator": self.aggregator.diagnostics(),
            "started_at_ms": self.started_at_ms,
            "updated_at_ms": int(time.time() * 1000),
        }

    def _write_status(self):
        try:
            self.redis.set(
                self.status_key,
                json.dumps(self._status_payload(), separators=(",", ":")),
            )
        except Exception as exc:
            print(
                f"[MICRO FLOW SHARD {self.shard_id}] status write error "
                f"error={type(exc).__name__}:{exc}"
            )

    def run(self):
        if not self.publisher.ping():
            raise RuntimeError("Redis connection failed")

        self.running = True
        self.stop_event.clear()
        self.phase = "STARTING"

        if not self._acquire_lock_with_wait():
            raise RuntimeError(
                f"Another producer owns Micro Flow shard {self.shard_id}"
            )

        now_ms = int(time.time() * 1000)
        self._open_transport_gap(now_ms, reason="producer_start")
        self.phase = "CONNECTING"
        self._write_status()

        try:
            self._start_websocket()
            self.ws_was_connected = bool(self.ws and self.ws.is_connected)
            if self.ws_was_connected:
                self._close_transport_gap(int(time.time() * 1000))
                self.phase = "READY"
            else:
                self.disconnected_since_monotonic = time.monotonic()
                self.phase = "RECOVERING"

            print(
                f"[MICRO FLOW SHARD {self.shard_id}] started "
                f"service_id={self.service_id} "
                f"shard={self.shard_id + 1}/{self.shard_count} "
                f"symbols={len(self.symbols)} "
                f"first={self.symbols[0]} last={self.symbols[-1]}"
            )

            while self.running:
                now_ms = int(time.time() * 1000)

                if self.ws is not None and getattr(self.ws, "fatal_error", None):
                    raise RuntimeError(
                        f"Micro Flow shard {self.shard_id} fatal websocket: "
                        f"{self.ws.fatal_error}"
                    )

                self._check_transport(now_ms)
                self._flush_seconds()

                now_mono = time.monotonic()
                if (
                    now_mono - self.last_heartbeat_monotonic
                    >= SHARD_HEARTBEAT_INTERVAL_SECONDS
                ):
                    self._write_heartbeat()
                    self.last_heartbeat_monotonic = now_mono

                if (
                    now_mono - self.last_status_monotonic
                    >= SHARD_STATUS_INTERVAL_SECONDS
                ):
                    self._write_status()
                    self.last_status_monotonic = now_mono

                self.stop_event.wait(0.20)

        finally:
            self.phase = "STOPPING"
            self.running = False

            try:
                self._open_transport_gap(
                    int(time.time() * 1000),
                    reason="producer_stop",
                )
            except Exception:
                pass

            if self.ws is not None:
                self.ws.stop()

            if self.ws_thread is not None and self.ws_thread.is_alive():
                self.ws_thread.join(timeout=20)

            self.phase = "STOPPED"
            self._write_status()
            try:
                self.redis.delete(self.heartbeat_key)
            except Exception:
                pass
            self._release_lock()

            print(
                f"[MICRO FLOW SHARD {self.shard_id}] stopped"
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sharded Binance Futures aggTrade producer for Micro Flow"
    )
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument(
        "--shard-count",
        type=int,
        default=MICRO_FLOW_DEFAULT_SHARD_COUNT,
    )
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-db", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=60)
    parser.add_argument("--stale-after", type=int, default=45)
    return parser.parse_args()


def main():
    args = parse_args()
    service = MicroFlowShardService(
        shard_id=args.shard_id,
        shard_count=args.shard_count,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        chunk_size=args.chunk_size,
        stale_after=args.stale_after,
    )

    def request_stop(signum, frame):
        print(
            f"[MICRO FLOW SHARD {service.shard_id}] received signal={signum}"
        )
        service.running = False
        service.stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    service.run()


if __name__ == "__main__":
    main()
