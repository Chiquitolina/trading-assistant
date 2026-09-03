import json
import threading
import time

import pandas as pd
import redis

from engine.live.data.redis_market_data_protocol import (
    CLOSED_CANDLES_STREAM,
    HEARTBEAT_KEY,
    PRICE_CHANNEL,
    STATUS_KEY,
    consumer_cursor_key,
    history_key,
)


class RedisMarketDataProvider:
    def __init__(
        self,
        buffer,
        symbols,
        timeframes,
        consumer_name,
        host="127.0.0.1",
        port=6379,
        db=0,
        ready_timeout=1800,
    ):
        if not consumer_name:
            raise ValueError(
                "consumer_name is required"
            )

        self.buffer = buffer
        self.symbols = {
            symbol.upper()
            for symbol in symbols
        }
        self.timeframes = {
            timeframe.lower()
            for timeframe in timeframes
        }

        self.consumer_name = consumer_name
        self.ready_timeout = ready_timeout

        self.redis = redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
        )

        self.running = False
        self.stream_cursor = None

        self.price_thread = None
        self.closed_thread = None
        self.pubsub = None

        self.stop_event = threading.Event()

    @property
    def is_connected(self):
        if not self.running:
            return False

        try:
            heartbeat_exists = bool(
                self.redis.exists(
                    HEARTBEAT_KEY
                )
            )

            raw_status = self.redis.get(
                STATUS_KEY
            )

            if not heartbeat_exists or not raw_status:
                return False

            status = json.loads(raw_status)

            service_ready = (
                status.get("phase") == "READY"
                and status.get("running") is True
                and status.get("ws_connected") is True
            )

            threads_alive = bool(
                self.price_thread
                and self.price_thread.is_alive()
                and self.closed_thread
                and self.closed_thread.is_alive()
            )

            return (
                service_ready
                and threads_alive
            )

        except Exception:
            return False

    def load_history(self):
        self._wait_until_ready()

        # Capturamos primero el último evento existente.
        # Todo cierre posterior será reproducido al iniciar.
        self.stream_cursor = (
            self._current_stream_tail()
        )

        total = (
            len(self.symbols)
            * len(self.timeframes)
        )

        loaded = 0

        for symbol in sorted(self.symbols):
            for timeframe in sorted(
                self.timeframes
            ):
                raw_candles = self.redis.lrange(
                    history_key(
                        symbol,
                        timeframe,
                    ),
                    0,
                    -1,
                )

                if not raw_candles:
                    raise RuntimeError(
                        "Missing Redis history "
                        f"symbol={symbol} "
                        f"timeframe={timeframe}"
                    )

                candles = [
                    json.loads(raw)
                    for raw in raw_candles
                ]

                dataframe = pd.DataFrame(
                    candles
                )

                self.buffer.load_historical(
                    symbol,
                    timeframe,
                    dataframe,
                )

                loaded += 1

                if (
                    loaded % 100 == 0
                    or loaded == total
                ):
                    print(
                        "[REDIS MARKET DATA] "
                        f"histories={loaded}/{total}"
                    )

        print(
            "[REDIS MARKET DATA] "
            f"history loaded "
            f"stream_cursor={self.stream_cursor}"
        )

    def start(self):
        if self.running:
            return

        if self.stream_cursor is None:
            raise RuntimeError(
                "load_history() must be called "
                "before start()"
            )

        self._wait_until_ready()

        self.running = True
        self.stop_event.clear()

        self.price_thread = threading.Thread(
            target=self._price_loop,
            daemon=True,
            name=(
                f"redis-price-"
                f"{self.consumer_name}"
            ),
        )

        self.closed_thread = threading.Thread(
            target=self._closed_candle_loop,
            daemon=True,
            name=(
                f"redis-closed-"
                f"{self.consumer_name}"
            ),
        )

        self.price_thread.start()
        self.closed_thread.start()

        print(
            "[REDIS MARKET DATA] "
            f"started consumer={self.consumer_name}"
        )

    def stop(self):
        self.running = False
        self.stop_event.set()

        pubsub = self.pubsub
        self.pubsub = None

        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass

        current_thread = (
            threading.current_thread()
        )

        for thread in (
            self.price_thread,
            self.closed_thread,
        ):
            if (
                thread
                and thread.is_alive()
                and thread is not current_thread
            ):
                thread.join(timeout=5)

        self.price_thread = None
        self.closed_thread = None

        print(
            "[REDIS MARKET DATA] "
            f"stopped consumer={self.consumer_name}"
        )

    def _price_loop(self):
        while self.running:
            pubsub = None

            try:
                pubsub = self.redis.pubsub(
                    ignore_subscribe_messages=True
                )

                self.pubsub = pubsub

                pubsub.subscribe(
                    PRICE_CHANNEL
                )

                print(
                    "[REDIS MARKET DATA] "
                    "price subscription ready"
                )

                while self.running:
                    message = pubsub.get_message(
                        timeout=1.0
                    )

                    if not message:
                        continue

                    payload = json.loads(
                        message["data"]
                    )

                    symbol = str(
                        payload["symbol"]
                    ).upper()

                    if symbol not in self.symbols:
                        continue

                    self._emit_price_to_buffer(
                        payload
                    )

            except Exception as exc:
                if self.running:
                    print(
                        "[REDIS MARKET DATA] "
                        f"price error={exc}"
                    )

            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass

                if self.pubsub is pubsub:
                    self.pubsub = None

            if self.running:
                self.stop_event.wait(2)

    def _closed_candle_loop(self):
        while self.running:
            try:
                response = self.redis.xread(
                    {
                        CLOSED_CANDLES_STREAM:
                            self.stream_cursor
                    },
                    count=500,
                    block=1000,
                )

                if not response:
                    continue

                for _, events in response:
                    for event_id, fields in events:
                        self.stream_cursor = (
                            event_id
                        )

                        try:
                            payload = json.loads(
                                fields["payload"]
                            )

                            symbol = str(
                                payload["symbol"]
                            ).upper()

                            timeframe = str(
                                payload["timeframe"]
                            ).lower()

                            if (
                                symbol in self.symbols
                                and timeframe
                                in self.timeframes
                            ):
                                self._emit_closed_to_buffer(
                                    payload
                                )

                        except Exception as exc:
                            print(
                                "[REDIS MARKET DATA] "
                                "invalid closed event "
                                f"id={event_id} "
                                f"error={exc}"
                            )

                self.redis.set(
                    consumer_cursor_key(
                        self.consumer_name
                    ),
                    self.stream_cursor,
                )

            except Exception as exc:
                if self.running:
                    print(
                        "[REDIS MARKET DATA] "
                        f"closed stream error={exc}"
                    )

                    self.stop_event.wait(2)

    def _emit_price_to_buffer(
        self,
        payload,
    ):
        synthetic_message = {
            "e": "kline",
            "s": payload["symbol"],
            "k": {
                "i": "1m",
                "t": int(
                    payload["timestamp"]
                ),
                "c": str(
                    payload["price"]
                ),
                "x": False,
            },
        }

        self.buffer.on_ws_message(
            synthetic_message
        )

    def _emit_closed_to_buffer(
        self,
        payload,
    ):
        timestamp = int(
            payload["timestamp"]
        )

        close_timestamp = int(
            payload.get(
                "close_timestamp",
                timestamp,
            )
        )

        synthetic_message = {
            "e": "kline",
            "E": close_timestamp,
            "s": payload["symbol"],
            "k": {
                "t": timestamp,
                "T": close_timestamp,
                "i": payload["timeframe"],
                "o": str(payload["open"]),
                "h": str(payload["high"]),
                "l": str(payload["low"]),
                "c": str(payload["close"]),
                "v": str(payload["volume"]),
                "q": str(
                    payload.get(
                        "quoteVolume",
                        0,
                    )
                ),
                "x": True,
            },
        }

        self.buffer.on_ws_message(
            synthetic_message
        )

    def _wait_until_ready(self):
        started_at = time.time()
        last_log_at = 0

        while True:
            try:
                heartbeat_exists = bool(
                    self.redis.exists(
                        HEARTBEAT_KEY
                    )
                )

                raw_status = self.redis.get(
                    STATUS_KEY
                )

                if heartbeat_exists and raw_status:
                    status = json.loads(
                        raw_status
                    )

                    if (
                        status.get("phase")
                        == "READY"
                        and status.get("running")
                        is True
                        and status.get(
                            "ws_connected"
                        )
                        is True
                    ):
                        return

                    phase = status.get(
                        "phase",
                        "UNKNOWN",
                    )
                else:
                    phase = "UNAVAILABLE"

            except Exception as exc:
                phase = f"ERROR: {exc}"

            elapsed = (
                time.time() - started_at
            )

            if elapsed >= self.ready_timeout:
                raise TimeoutError(
                    "Market data service "
                    f"not ready after "
                    f"{self.ready_timeout}s"
                )

            if (
                time.time() - last_log_at
                >= 10
            ):
                print(
                    "[REDIS MARKET DATA] "
                    "waiting for service "
                    f"phase={phase}"
                )

                last_log_at = time.time()

            time.sleep(2)

    def _current_stream_tail(self):
        events = self.redis.xrevrange(
            CLOSED_CANDLES_STREAM,
            count=1,
        )

        if not events:
            return "0-0"

        event_id, _ = events[0]

        return event_id