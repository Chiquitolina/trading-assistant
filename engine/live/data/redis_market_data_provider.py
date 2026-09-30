import json
import threading
import time

import pandas as pd
import redis

from config.timeframes import TIMEFRAME_CONFIGS

from engine.live.data.redis_market_data_protocol import (
    CLOSED_CANDLES_STREAM,
    HEARTBEAT_KEY,
    PRICE_CHANNEL,
    REPLAY_CLOCK_KEY,
    STATUS_KEY,
    consumer_cursor_key,
    history_key,
    market_flow_key,
    replay_provider_applied_key,
    replay_consumer_ready_key,
)

from engine.replay.replay_clock import (
    RealClock,
    RedisReplayClock,
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
        clock_mode="real",
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
        
        self.replay_applied_key = (
            replay_provider_applied_key(
                self.consumer_name
            )
        )
        
        self.replay_consumer_ready_key = (
            replay_consumer_ready_key(
                self.consumer_name
            )
        )

        self.redis = redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
        )
        
        self.clock_mode = str(
            clock_mode
        ).strip().lower()

        if self.clock_mode == "real":
            self.clock = RealClock()

        elif self.clock_mode == "replay":
            self.clock = RedisReplayClock(
                redis_client=self.redis,
                key=REPLAY_CLOCK_KEY,
            )

        else:
            raise ValueError(
                "clock_mode must be "
                "'real' or 'replay'"
            )
        

        self.running = False
        self.stream_cursor = None
        
        # ==========================================
        # CLOSED STREAM DIAGNOSTICS
        # ==========================================
        self._stream_diag_by_tf = {
            tf: {}
            for tf in self.timeframes
        }

        self._stream_diag_last_reported_by_tf = {
            tf: None
            for tf in self.timeframes
        }

        self._stream_diag_lock = threading.Lock()

        self._stream_diag_xread_calls = 0
        self._stream_diag_xread_events = 0
        
        # ==========================================
        # LIVE CLOSED-BOUNDARY BARRIER
        # ==========================================
        self._live_boundary_batches = {}
        self._live_boundary_last_released = None

        self._live_startup_history_loaded_at_ms = (
            None
        )

        self._live_startup_seeded_boundaries = (
            set()
        )

        self.price_thread = None
        self.closed_thread = None
        self.pubsub = None

        self.stop_event = threading.Event()
        
        self.market_flow_cache = {}

        self.market_flow_cache_lock = (
            threading.Lock()
        )

        self.market_flow_cache_ttl_seconds = 5

        self.market_flow_last_error = None

    def _is_service_ready(self, status):
        if not isinstance(status, dict):
            return False

        if (
            status.get("phase") != "READY"
            or status.get("running") is not True
            or status.get("bootstrap_buffering") is True
        ):
            return False

        source = str(
            status.get("source", "live")
        ).strip().lower()

        if source == "live":
            return status.get(
                "ws_connected"
            ) is True

        if source == "replay":
            return status.get(
                "replay_ready"
            ) is True

        return False

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
                self._is_service_ready(
                    status
                )
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
        
    def get_market_flow_snapshot(
        self,
        timeframe="4h",
        min_coverage_pct=80.0,
        max_age_seconds=18_000,
        force_refresh=False,
    ):
        timeframe = str(
            timeframe
        ).strip().lower()

        timeframe_ms = {
            "4h": 4 * 60 * 60 * 1000,
        }.get(timeframe)

        if timeframe_ms is None:
            return self._reject_market_flow(
                f"unsupported_timeframe:{timeframe}"
            )

        now_monotonic = time.monotonic()

        with self.market_flow_cache_lock:
            cached = (
                self.market_flow_cache.get(
                    timeframe
                )
            )

            if (
                not force_refresh
                and cached is not None
                and (
                    now_monotonic
                    - cached["loaded_at"]
                    < self.market_flow_cache_ttl_seconds
                )
            ):
                return cached["snapshot"]

        try:
            raw_snapshot = self.redis.get(
                market_flow_key(
                    timeframe
                )
            )
        except Exception as exc:
            return self._reject_market_flow(
                f"redis_error:{exc}"
            )

        if not raw_snapshot:
            return self._reject_market_flow(
                "snapshot_missing"
            )

        try:
            snapshot = json.loads(
                raw_snapshot
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return self._reject_market_flow(
                "invalid_json"
            )

        if not isinstance(snapshot, dict):
            return self._reject_market_flow(
                "snapshot_not_dict"
            )

        if (
            snapshot.get("type")
            != "market_flow_snapshot"
        ):
            return self._reject_market_flow(
                "invalid_snapshot_type"
            )

        snapshot_timeframe = str(
            snapshot.get(
                "timeframe",
                "",
            )
        ).lower()

        if snapshot_timeframe != timeframe:
            return self._reject_market_flow(
                "timeframe_mismatch"
            )

        try:
            candle_timestamp = int(
                snapshot["candle_timestamp"]
            )

            calculated_at = int(
                snapshot["calculated_at"]
            )

            coverage_pct = float(
                snapshot["coverage_pct"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return self._reject_market_flow(
                "invalid_required_fields"
            )

        if coverage_pct < min_coverage_pct:
            return self._reject_market_flow(
                "coverage_below_minimum"
            )

        candle_close_timestamp = (
            candle_timestamp
            + timeframe_ms
        )

        now_ms = self.clock.now_ms()

        # Un pequeño margen tolera diferencias
        # mínimas de reloj entre procesos.
        if (
            candle_close_timestamp
            > now_ms + 5_000
        ):
            return self._reject_market_flow(
                "candle_not_closed"
            )

        snapshot_age_seconds = max(
            0.0,
            (
                now_ms
                - candle_close_timestamp
            ) / 1000,
        )

        if (
            snapshot_age_seconds
            > max_age_seconds
        ):
            return self._reject_market_flow(
                "snapshot_stale"
            )

        if calculated_at > now_ms + 5_000:
            return self._reject_market_flow(
                "calculated_at_in_future"
            )

        symbols = snapshot.get(
            "symbols"
        )

        if not isinstance(symbols, dict):
            return self._reject_market_flow(
                "invalid_symbols_payload"
            )

        validated_snapshot = dict(
            snapshot
        )

        validated_snapshot[
            "candle_close_timestamp"
        ] = candle_close_timestamp

        validated_snapshot[
            "snapshot_age_seconds"
        ] = round(
            snapshot_age_seconds,
            4,
        )

        with self.market_flow_cache_lock:
            self.market_flow_cache[
                timeframe
            ] = {
                "loaded_at": (
                    now_monotonic
                ),
                "snapshot": (
                    validated_snapshot
                ),
            }

        self.market_flow_last_error = None

        return validated_snapshot

    def get_symbol_market_flow(
        self,
        symbol,
        timeframe="4h",
        min_coverage_pct=80.0,
        max_age_seconds=18_000,
    ):
        if not symbol:
            return self._reject_market_flow(
                "symbol_required"
            )

        symbol = str(
            symbol
        ).strip().upper()

        snapshot = (
            self.get_market_flow_snapshot(
                timeframe=timeframe,
                min_coverage_pct=(
                    min_coverage_pct
                ),
                max_age_seconds=(
                    max_age_seconds
                ),
            )
        )

        if snapshot is None:
            return None

        symbol_metrics = (
            snapshot["symbols"].get(
                symbol
            )
        )

        if not isinstance(
            symbol_metrics,
            dict,
        ):
            return self._reject_market_flow(
                f"symbol_unavailable:{symbol}"
            )
            
        primary_sector = (
            symbol_metrics.get(
                "primary_sector"
            )
        )

        sector_metrics_available = bool(
            snapshot.get(
                "sector_context_available"
            )
            and symbol_metrics.get(
                "sector_return_pct_4h"
            )
            is not None
        )

        sector_context_error = None

        if not snapshot.get(
            "sector_context_available"
        ):
            sector_context_error = (
                snapshot.get(
                    "sector_context_error"
                )
                or "sector_context_unavailable"
            )

        elif primary_sector == "Other":
            sector_context_error = (
                "sector_unclassified"
            )

        elif not sector_metrics_available:
            excluded_sector = (
                snapshot.get(
                    "excluded_sectors",
                    {},
                ).get(
                    primary_sector,
                    {},
                )
            )

            sector_context_error = (
                excluded_sector.get(
                    "reason"
                )
                or "sector_metrics_unavailable"
            )

        result = dict(
            symbol_metrics
        )

        result.update({
            "market_breadth_4h": (
                snapshot.get(
                    "market_breadth_4h"
                )
            ),
            "btc_return_pct_4h": (
                snapshot.get(
                    "btc_return_pct_4h"
                )
            ),
            "market_flow_timestamp": (
                snapshot.get(
                    "candle_timestamp"
                )
            ),
            "market_flow_close_timestamp": (
                snapshot.get(
                    "candle_close_timestamp"
                )
            ),
            "market_flow_calculated_at": (
                snapshot.get(
                    "calculated_at"
                )
            ),
            "market_flow_age_seconds": (
                snapshot.get(
                    "snapshot_age_seconds"
                )
            ),
            "market_flow_coverage_pct": (
                snapshot.get(
                    "coverage_pct"
                )
            ),
            "market_flow_universe_size": (
                snapshot.get(
                    "valid_universe_size"
                )
            ),
            "sector_context_available": (
                sector_metrics_available
            ),
            "sector_context_error": (
                sector_context_error
            ),
            "sector_catalog_generated_at": (
                snapshot.get(
                    "sector_catalog_generated_at"
                )
            ),
            "sector_assignment_method": (
                snapshot.get(
                    "sector_assignment_method"
                )
            ),
            "sector_return_aggregation": (
                snapshot.get(
                    "sector_return_aggregation"
                )
            ),
        })

        self.market_flow_last_error = None

        return result
    
    def _live_boundary_step_ms(self):
        return min(
            self._timeframe_ms(tf)
            for tf in self.timeframes
        )

    def _reject_market_flow(
        self,
        reason,
    ):
        self.market_flow_last_error = str(
            reason
        )

        return None

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

        if self.clock_mode == "real":
            self._live_startup_history_loaded_at_ms = (
                int(time.time() * 1000)
            )

            print(
                "[PROVIDER BOUNDARY STARTUP] "
                "history cutoff="
                f"{self._live_startup_history_loaded_at_ms}"
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
        
        if self.clock_mode == "replay":
            self.redis.set(
                self.replay_consumer_ready_key,
                "1",
            )

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
                cursor_before = self.stream_cursor

                response = self.redis.xread(
                    {
                        CLOSED_CANDLES_STREAM:
                            cursor_before
                    },
                    count=500,
                    block=1000,
                )

                self._stream_diag_xread_calls += 1

                if not response:
                    if self.clock_mode == "real":
                        self._flush_complete_live_boundaries()

                    continue

                response_event_count = sum(
                    len(events)
                    for _, events in response
                )

                self._stream_diag_xread_events += (
                    response_event_count
                )

                first_event_id = None
                last_event_id = None

                for _, events in response:
                    if not events:
                        continue

                    if first_event_id is None:
                        first_event_id = events[0][0]

                    last_event_id = events[-1][0]

                print(
                    "[PROVIDER XREAD] "
                    f"consumer={self.consumer_name} "
                    f"cursor_before={cursor_before} "
                    f"events={response_event_count} "
                    f"first_id={first_event_id} "
                    f"last_id={last_event_id}"
                )

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
                            
                            if timeframe in self._stream_diag_by_tf:
                                close_timestamp = int(
                                    payload.get(
                                        "close_timestamp",
                                        payload.get(
                                            "timestamp",
                                            0,
                                        ),
                                    )
                                )

                                with self._stream_diag_lock:
                                    tf_batches = (
                                        self._stream_diag_by_tf[
                                            timeframe
                                        ]
                                    )

                                    batch = tf_batches.setdefault(
                                        close_timestamp,
                                        {
                                            "symbols": set(),
                                            "events": 0,
                                            "first_event_id": (
                                                event_id
                                            ),
                                            "last_event_id": (
                                                event_id
                                            ),
                                        },
                                    )

                                    batch["symbols"].add(
                                        symbol
                                    )

                                    batch["events"] += 1

                                    batch[
                                        "last_event_id"
                                    ] = event_id

                            if (
                                symbol in self.symbols
                                and timeframe in self.timeframes
                            ):
                                if self.clock_mode == "real":
                                    self._queue_live_closed_payload(
                                        payload
                                    )
                                else:
                                    self._emit_closed_to_buffer(
                                        payload
                                    )

                            # En replay este ACK significa:
                            #
                            # "este consumer procesó el stream
                            # correctamente hasta event_id".
                            #
                            # Debe avanzar aunque el evento no
                            # pertenezca al universo de esta rama.
                            if self.clock_mode == "replay":
                                self.redis.set(
                                    self.replay_applied_key,
                                    event_id,
                                )

                        except Exception as exc:
                            print(
                                "[REDIS MARKET DATA] "
                                "invalid closed event "
                                f"id={event_id} "
                                f"error={exc}"
                            )

                            if self.clock_mode == "replay":
                                self.running = False
                                self.stop_event.set()

                                raise
                            
                self._report_stream_tf_coverage()

                print(
                    "[PROVIDER XREAD APPLIED] "
                    f"consumer={self.consumer_name} "
                    f"cursor_before={cursor_before} "
                    f"cursor_after={self.stream_cursor} "
                    f"events={response_event_count}"
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
                        
    def _report_stream_tf_coverage(self):
        with self._stream_diag_lock:
            for timeframe in sorted(
                self._stream_diag_by_tf
            ):
                tf_batches = (
                    self._stream_diag_by_tf[
                        timeframe
                    ]
                )

                timestamps = sorted(
                    tf_batches
                )

                if len(timestamps) < 2:
                    continue

                # Si apareció el siguiente cierre
                # de este TF, consideramos
                # terminado el batch anterior.
                close_timestamp = (
                    timestamps[-2]
                )

                if (
                    self._stream_diag_last_reported_by_tf[
                        timeframe
                    ]
                    == close_timestamp
                ):
                    continue

                batch = tf_batches[
                    close_timestamp
                ]

                received_symbols = set(
                    batch["symbols"]
                )

                expected_symbols = set(
                    self.symbols
                )

                missing_symbols = sorted(
                    expected_symbols
                    - received_symbols
                )

                extra_symbols = sorted(
                    received_symbols
                    - expected_symbols
                )

                duplicate_events = (
                    batch["events"]
                    - len(received_symbols)
                )

                print(
                    "[PROVIDER TF COVERAGE] "
                    f"consumer={self.consumer_name} "
                    f"tf={timeframe} "
                    f"close_ts={close_timestamp} "
                    f"received="
                    f"{len(received_symbols)}/"
                    f"{len(expected_symbols)} "
                    f"events={batch['events']} "
                    f"duplicates={duplicate_events} "
                    f"missing={len(missing_symbols)} "
                    f"extra={len(extra_symbols)} "
                    f"first_id="
                    f"{batch['first_event_id']} "
                    f"last_id="
                    f"{batch['last_event_id']} "
                    f"missing_symbols="
                    f"{','.join(missing_symbols) or '-'}"
                )

                self._stream_diag_last_reported_by_tf[
                    timeframe
                ] = close_timestamp

                # No necesitamos conservar
                # batches ya auditados.
                stale_timestamps = [
                    ts
                    for ts in timestamps
                    if ts <= close_timestamp
                ]

                for ts in stale_timestamps:
                    tf_batches.pop(
                        ts,
                        None,
                    )
                    
    def _timeframe_ms(
        self,
        timeframe,
    ):
        config = TIMEFRAME_CONFIGS.get(
            timeframe,
            {},
        )

        timeframe_ms = int(
            config.get(
                "ms_per_candle",
                0,
            )
        )

        if timeframe_ms <= 0:
            raise ValueError(
                "Invalid timeframe duration "
                f"for {timeframe}"
            )

        return timeframe_ms


    def _expected_timeframes_for_close(
        self,
        close_timestamp,
    ):
        boundary_end_ms = (
            int(close_timestamp)
            + 1
        )

        expected = [
            timeframe
            for timeframe in self.timeframes
            if (
                boundary_end_ms
                % self._timeframe_ms(
                    timeframe
                )
                == 0
            )
        ]

        return sorted(
            expected,
            key=self._timeframe_ms,
        )
        
    def _producer_ready_for_live_release(self):
        try:
            raw_status = self.redis.get(
                STATUS_KEY
            )

            if not raw_status:
                return False

            status = json.loads(
                raw_status
            )

            return self._is_service_ready(
                status
            )

        except Exception:
            return False

    def _seed_live_boundary_from_buffer(
        self,
        close_timestamp,
    ):
        close_timestamp = int(
            close_timestamp
        )

        expected_timeframes = (
            self._expected_timeframes_for_close(
                close_timestamp
            )
        )

        batch = (
            self._live_boundary_batches
            .setdefault(
                close_timestamp,
                {
                    "payloads": {},
                    "first_seen_at": (
                        time.monotonic()
                    ),
                },
            )
        )

        seeded = 0

        for timeframe in expected_timeframes:
            timeframe_ms = (
                self._timeframe_ms(
                    timeframe
                )
            )

            expected_open_timestamp = (
                close_timestamp
                - timeframe_ms
                + 1
            )

            for symbol in self.symbols:
                candles = (
                    self.buffer.get_candles(
                        symbol,
                        timeframe,
                    )
                )

                matching_candle = None

                for candle in reversed(candles):
                    try:
                        candle_timestamp = int(
                            candle["timestamp"]
                        )
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                    ):
                        continue

                    if (
                        candle_timestamp
                        == expected_open_timestamp
                    ):
                        matching_candle = candle
                        break

                    if (
                        candle_timestamp
                        < expected_open_timestamp
                    ):
                        break

                if matching_candle is None:
                    continue

                key = (
                    symbol,
                    timeframe,
                )

                if key in batch["payloads"]:
                    continue

                batch["payloads"][key] = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": (
                        expected_open_timestamp
                    ),
                    "close_timestamp": (
                        close_timestamp
                    ),
                    "open": float(
                        matching_candle["open"]
                    ),
                    "high": float(
                        matching_candle["high"]
                    ),
                    "low": float(
                        matching_candle["low"]
                    ),
                    "close": float(
                        matching_candle["close"]
                    ),
                    "volume": float(
                        matching_candle.get(
                            "volume",
                            0,
                        )
                    ),
                    "quoteVolume": float(
                        matching_candle.get(
                            "quoteVolume",
                            0,
                        )
                    ),
                    "source": (
                        "startup_history_seed"
                    ),
                }

                seeded += 1

        print(
            "[PROVIDER BOUNDARY STARTUP] "
            f"close_ts={close_timestamp} "
            f"seeded={seeded}"
        )

        return seeded

    def _queue_live_closed_payload(
        self,
        payload,
    ):
        symbol = str(
            payload["symbol"]
        ).upper()

        timeframe = str(
            payload["timeframe"]
        ).lower()

        timestamp = int(
            payload["timestamp"]
        )

        close_timestamp = int(
            payload.get(
                "close_timestamp",
                (
                    timestamp
                    + self._timeframe_ms(
                        timeframe
                    )
                    - 1
                ),
            )
        )

        if (
            self._live_boundary_last_released
            is not None
            and close_timestamp
            <= self._live_boundary_last_released
        ):
            print(
                "[PROVIDER BOUNDARY] "
                "late event skipped "
                f"symbol={symbol} "
                f"tf={timeframe} "
                f"close_ts={close_timestamp}"
            )
            return

        startup_cutoff = (
            self._live_startup_history_loaded_at_ms
        )

        if (
            startup_cutoff is not None
            and close_timestamp
            <= startup_cutoff
            and close_timestamp
            not in self._live_startup_seeded_boundaries
        ):
            self._seed_live_boundary_from_buffer(
                close_timestamp
            )

            self._live_startup_seeded_boundaries.add(
                close_timestamp
            )

        batch = (
            self._live_boundary_batches
            .setdefault(
                close_timestamp,
                {
                    "payloads": {},
                    "first_seen_at": (
                        time.monotonic()
                    ),
                },
            )
        )

        key = (
            symbol,
            timeframe,
        )

        existing_payload = (
            batch["payloads"].get(
                key
            )
        )

        # Si esta candle ya estaba en DataBuffer
        # por el history bootstrap, conservamos
        # ese seed. No queremos reenviarla como
        # WS y correr el riesgo de appendear una
        # candle vieja detrás de una más nueva.
        if not (
            isinstance(
                existing_payload,
                dict,
            )
            and existing_payload.get(
                "source"
            )
            == "startup_history_seed"
        ):
            batch["payloads"][
                key
            ] = payload

        self._flush_complete_live_boundaries()


    def _flush_complete_live_boundaries(
        self,
    ):
        if (
            self.clock_mode == "real"
            and not self._producer_ready_for_live_release()
        ):
            return
        
        while self._live_boundary_batches:
            earliest_present = min(
                self._live_boundary_batches
            )

            if self._live_boundary_last_released is None:
                close_timestamp = earliest_present
            else:
                expected_next = (
                    int(self._live_boundary_last_released)
                    + self._live_boundary_step_ms()
                )

                if earliest_present > expected_next:
                    print(
                        "[PROVIDER BOUNDARY] "
                        "gap blocks release "
                        f"expected={expected_next} "
                        f"earliest_present={earliest_present}"
                    )
                    return

                close_timestamp = expected_next

                if (
                    close_timestamp
                    not in self._live_boundary_batches
                ):
                    return

            batch = (
                self._live_boundary_batches[
                    close_timestamp
                ]
            )

            expected_timeframes = (
                self._expected_timeframes_for_close(
                    close_timestamp
                )
            )

            expected_keys = {
                (
                    symbol,
                    timeframe,
                )
                for symbol in self.symbols
                for timeframe
                in expected_timeframes
            }

            present_keys = set(
                batch["payloads"]
            )

            missing_keys = (
                expected_keys
                - present_keys
            )

            if missing_keys:
                return

            ordered_keys = sorted(
                expected_keys,
                key=lambda item: (
                    item[0],
                    self._timeframe_ms(
                        item[1]
                    ),
                ),
            )

            payloads = [
                batch["payloads"][key]
                for key in ordered_keys
            ]

            del self._live_boundary_batches[
                close_timestamp
            ]

            for payload in payloads:
                if (
                    payload.get("source")
                    == "startup_history_seed"
                ):
                    registered = (
                        self.buffer
                        .register_existing_closed_boundary(
                            symbol=payload[
                                "symbol"
                            ],
                            tf=payload[
                                "timeframe"
                            ],
                            close_time=int(
                                payload[
                                    "close_timestamp"
                                ]
                            ),
                            candle=payload,
                        )
                    )

                    if not registered:
                        raise RuntimeError(
                            "Could not register "
                            "startup history boundary "
                            f"symbol="
                            f"{payload['symbol']} "
                            f"tf="
                            f"{payload['timeframe']} "
                            f"close_ts="
                            f"{payload['close_timestamp']}"
                        )

                else:
                    self._emit_closed_to_buffer(
                        payload
                    )

            # Sólo marcamos el boundary como aplicado
            # después de haber cargado TODOS sus eventos
            # en DataBuffer.
            self._live_boundary_last_released = (
                close_timestamp
            )

            print(
                "[PROVIDER BOUNDARY RELEASE] "
                f"close_ts={close_timestamp} "
                f"timeframes="
                f"{','.join(expected_timeframes)} "
                f"events={len(payloads)} "
                f"symbols={len(self.symbols)}"
            )
            
    def is_live_boundary_applied(
        self,
        close_timestamp,
    ):
        if self.clock_mode != "real":
            return True

        last_released = (
            self._live_boundary_last_released
        )

        if last_released is None:
            return False

        return (
            int(last_released)
            >= int(close_timestamp)
        )

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

                    if self._is_service_ready(
                        status
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