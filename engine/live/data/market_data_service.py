import argparse

import copy

import json

import signal

import threading

import time

import uuid

from collections import deque



from config.strategies.v1 import SYMBOLS

from config.timeframes import MODE_CONFIG, TIMEFRAME_CONFIGS

from data.market_data import (

    fetch_closed_futures_candle,

    fetch_closed_history_before,

)

from engine.live.data.redis_market_data_protocol import (

    HEARTBEAT_KEY,

    HEARTBEAT_TTL_SECONDS,

    PRODUCER_LOCK_KEY,

    PRODUCER_LOCK_TTL_SECONDS,

    STATUS_KEY,

    history_key,
    last_closed_key,

)

from engine.live.data.redis_market_data_publisher import (

    RedisMarketDataPublisher,

)



from engine.live.data.market_flow_analyzer import (

    MarketFlowAnalyzer,

)



from engine.live.data.market_sector_catalog import (

    MarketSectorCatalog,

)



from engine.live.data.market_sector_flow_analyzer import (

    MarketSectorFlowAnalyzer,

)



from engine.live.ws.ws_client import WSClient

from engine.live.research.micro_flow_second_aggregator import (
    MicroFlowSecondAggregator,
)







MARKET_FLOW_TIMEFRAMES = ("1h", "4h")

MARKET_FLOW_TIMEFRAME_MS = {
    "1h": 1 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}

PRODUCER_LOCK_STARTUP_WAIT_SECONDS = (
    PRODUCER_LOCK_TTL_SECONDS + 15
)

PRODUCER_LOCK_STARTUP_POLL_SECONDS = 2

# Esperamos que termine de llegar el batch

# antes de calcular el ranking global.

MARKET_FLOW_SETTLE_SECONDS = 15

WS_CONNECTING_FAILFAST_SECONDS = 180

CLOSED_INTEGRITY_CHECK_SECONDS = 30
CLOSED_INTEGRITY_SETTLE_SECONDS = 20
CLOSED_INTEGRITY_LOOKBACK_SECONDS = 180

# Evita publicar rankings construidos sobre

# una porción demasiado pequeña del universo.

MARKET_FLOW_MIN_COVERAGE_PCT = 95.0



# Tiempo máximo que conservamos un batch

# incompleto esperando nuevos cierres.

MARKET_FLOW_MAX_WAIT_SECONDS = 60



class MarketDataService:

    def __init__(

        self,

        redis_host="127.0.0.1",

        redis_port=6379,

        redis_db=0,

        chunk_size=40,

        stale_after=90,

        enable_micro_flow=True,

    ):

        mode_config = MODE_CONFIG["compression"]



        self.symbols = list(SYMBOLS)

        self.timeframes = list(

            mode_config["timeframes"]

        )



        self.publisher = RedisMarketDataPublisher(

            host=redis_host,

            port=redis_port,

            db=redis_db,

        )

        # Micro Flow research is intentionally isolated from the normal candle
        # publishing path. WS callbacks only aggregate/enqueue locally; Redis
        # writes happen from the service loop so high aggTrade traffic cannot
        # block candle callbacks.
        self.enable_micro_flow = bool(enable_micro_flow)
        self.micro_flow_aggregator = (
            MicroFlowSecondAggregator(
                finalize_grace_ms=750,
            )
            if self.enable_micro_flow
            else None
        )
        self.micro_flow_queue_lock = threading.Lock()
        self.micro_flow_publish_queue = deque(
            maxlen=200_000
        )
        self.micro_flow_queue_dropped = 0
        self.micro_flow_seconds_published = 0
        self.micro_flow_publish_errors = 0
        self.micro_flow_last_published_at_ms = None



        self.market_flow_analyzer = (

            MarketFlowAnalyzer(

                redis_client=(

                    self.publisher.redis

                ),

                baseline_candles=42,

            )

        )



        self.market_sector_catalog = None

        self.market_sector_flow_analyzer = None

        self.market_sector_catalog_error = None



        self._load_market_sector_catalog()



        self.market_flow_lock = (

            threading.Lock()

        )



        # Batches and last-published timestamps are independent per Market
        # Flow timeframe so a 1h close can never suppress a 4h close (or vice
        # versa) when both share the same candle-open timestamp.
        self.market_flow_batches = {}

        self.last_market_flow_timestamp = {
            timeframe: None
            for timeframe in MARKET_FLOW_TIMEFRAMES
        }



        self.chunk_size = chunk_size

        self.stale_after = stale_after



        self.service_id = str(uuid.uuid4())



        self.ws = None

        self.ws_thread = None

        self.heartbeat_thread = None



        self.running = False

        self.phase = "CREATED"



        self.histories_loaded = 0

        self.history_candles_loaded = 0

        self.history_cutoff_ms = None

        self.bootstrap_lock = threading.Lock()
        self.bootstrap_buffering = False
        self.bootstrap_closed_messages = []
        self.bootstrap_buffer_sequence = 0
        self.ws_was_connected = False
        self.ws_disconnect_detected_at_ms = None
        self.ws_recovery_in_progress = False
        self.ws_connecting_since_monotonic = None
        self.closed_integrity_last_check_monotonic = 0.0


        self.stop_event = threading.Event()



    def run(self):

        if not self.publisher.ping():

            raise RuntimeError(

                "Redis connection failed"

            )



        if not self._acquire_producer_lock_with_wait():
            raise RuntimeError(
                "Another market data producer is active "
                "after producer-lock startup wait"
            )



        self.running = True

        self.stop_event.clear()



        self._start_heartbeat()



        try:

            # Connect WS first and buffer every closed candle
            # while the deterministic REST seed is installed.
            self.phase = "CONNECTING_WS"
            self._begin_bootstrap_ws_buffering()
            self._write_status()

            self._start_websocket()

            if (
                self.ws is None
                or not self.ws.is_connected
            ):
                raise RuntimeError(
                    "WebSocket did not become ready "
                    "before history bootstrap"
                )

            # One shared cutoff for every symbol/timeframe.
            # REST owns everything strictly before this point.
            # WS buffer owns everything at/after this point.
            self.history_cutoff_ms = int(
                time.time() * 1000
            )

            print(
                "[MARKET DATA HISTORY] "
                f"cutoff_ms={self.history_cutoff_ms}"
            )

            self.phase = "LOADING_HISTORY"
            self._write_status()

            self._load_all_history()

            # Publish buffered WS closes only after REST seed
            # is fully installed, preserving chronological order.
            self.phase = "CATCHING_UP"
            self._write_status()

            self._flush_bootstrap_ws_buffer()

            self._publish_initial_market_flow()



            print(

                "[MARKET DATA SERVICE] "

                f"started service_id={self.service_id}"

            )



            print(

                "[MARKET DATA SERVICE] "

                f"symbols={len(self.symbols)} "

                f"timeframes={len(self.timeframes)}"

            )



            while self.running:
                now_ms = int(
                    time.time() * 1000
                )

                transition = (
                    self._detect_ws_connection_transition(
                        now_ms
                    )
                )

                if transition is not None:
                    if (
                        transition["type"]
                        == "disconnected"
                    ):
                        self.phase = "CONNECTING_WS"

                        started = (
                            self._begin_ws_recovery_buffering()
                        )

                        print(
                            "[MARKET DATA RECOVERY] "
                            "disconnect detected "
                            f"at="
                            f"{transition['detected_at_ms']} "
                            f"buffer_started={started}"
                        )

                    elif (
                        transition["type"]
                        == "reconnected"
                    ):
                        self._run_ws_recovery(
                            disconnected_at_ms=(
                                transition[
                                    "disconnected_at_ms"
                                ]
                            ),
                            reconnected_at_ms=(
                                transition[
                                    "detected_at_ms"
                                ]
                            ),
                        )
                        
                self._check_ws_connection_watchdog()

                if not self.ws_recovery_in_progress:
                    if (
                        self.ws
                        and self.ws.is_connected
                    ):
                        self.phase = "READY"
                    else:
                        self.phase = "CONNECTING_WS"
                        
                self._check_closed_candle_integrity()

                self._flush_micro_flow_seconds()


                self.publisher.report_closed_candle_coverage(
                    expected_symbols=self.symbols
                )

                self._maybe_publish_market_flow()

                self.stop_event.wait(1)



        finally:

            self.stop()



    def stop(self):

        if not self.running and self.phase == "STOPPED":

            return



        self.running = False

        self.stop_event.set()



        if self.ws is not None:

            self.ws.stop()



        if (

            self.ws_thread

            and self.ws_thread.is_alive()

            and self.ws_thread is not threading.current_thread()

        ):

            self.ws_thread.join(timeout=5)



        self.phase = "STOPPED"

        self._write_status()



        self._delete_if_owned(

            HEARTBEAT_KEY,

        )



        self._delete_if_owned(

            PRODUCER_LOCK_KEY,

        )



        print(

            "[MARKET DATA SERVICE] stopped"

        )



    def _load_all_history(self):

        total = (

            len(self.symbols)

            * len(self.timeframes)

        )



        current = 0



        for symbol in self.symbols:

            for timeframe in self.timeframes:

                if not self.running:

                    return



                current += 1



                candles_loaded = (

                    self._load_history_with_retry(

                        symbol,

                        timeframe,

                    )

                )



                self.histories_loaded += 1

                self.history_candles_loaded += (

                    candles_loaded

                )



                print(

                    "[MARKET DATA HISTORY] "

                    f"{current}/{total} "

                    f"symbol={symbol} "

                    f"tf={timeframe} "

                    f"candles={candles_loaded}"

                )



            self._write_status()



    def _load_history_with_retry(

        self,

        symbol,

        timeframe,

        max_attempts=5,

    ):

        delay_seconds = 5



        for attempt in range(

            1,

            max_attempts + 1,

        ):

            try:

                if self.history_cutoff_ms is None:

                    raise RuntimeError(
                        "History cutoff is not initialized"
                    )



                candles = fetch_closed_history_before(

                    symbol=symbol,

                    timeframe=timeframe,

                    cutoff_ms=self.history_cutoff_ms,

                )



                return self.publisher.replace_history(

                    symbol,

                    timeframe,

                    candles,

                )



            except Exception as exc:

                print(

                    "[MARKET DATA HISTORY] "

                    f"error symbol={symbol} "

                    f"tf={timeframe} "

                    f"attempt={attempt}/{max_attempts} "

                    f"error={exc}"

                )



                if attempt >= max_attempts:

                    raise



                if self.stop_event.wait(

                    delay_seconds

                ):

                    raise RuntimeError(

                        "Service stopped while loading history"

                    )



                delay_seconds = min(

                    delay_seconds * 2,

                    60,

                )



        raise RuntimeError(

            f"History unavailable: "

            f"{symbol} {timeframe}"

        )



    def _load_market_sector_catalog(

        self,

    ):

        try:

            catalog = MarketSectorCatalog(

                required_symbols=(

                    self.symbols

                ),

            )



        except Exception as exc:

            self.market_sector_catalog = None

            self.market_sector_flow_analyzer = None



            self.market_sector_catalog_error = (

                f"{type(exc).__name__}:"

                f"{exc}"

            )



            print(

                "[MARKET SECTORS] "

                "catalog unavailable "

                f"error="

                f"{self.market_sector_catalog_error}"

            )



            return False



        self.market_sector_catalog = catalog

        self.market_sector_catalog_error = None



        self.market_sector_flow_analyzer = (

            MarketSectorFlowAnalyzer(

                catalog=catalog,

            )

        )



        grouped = (

            catalog

            .symbols_by_primary_sector()

        )



        group_summary = ",".join(

            f"{sector}:{len(symbols)}"

            for sector, symbols

            in grouped.items()

        )



        print(

            "[MARKET SECTORS] "

            "catalog loaded "

            f"symbols={len(catalog)} "

            f"generated_at="

            f"{catalog.generated_at} "

            f"groups={group_summary}"

        )



        return True



    def _enrich_market_flow_snapshot(
        self,
        snapshot,
        timeframe=None,
    ):
        timeframe = str(
            timeframe
            or snapshot.get("timeframe")
            or ""
        ).lower()

        if timeframe != "4h":
            enriched_snapshot = dict(snapshot)
            enriched_snapshot["sector_context_available"] = False
            enriched_snapshot["sector_context_error"] = (
                "sector_context_only_supported_for_4h"
            )
            return enriched_snapshot

        if self.market_sector_flow_analyzer is None:
            enriched_snapshot = dict(snapshot)
            enriched_snapshot["sector_context_available"] = False
            enriched_snapshot["sector_context_error"] = (
                self.market_sector_catalog_error
                or "sector_analyzer_unavailable"
            )
            return enriched_snapshot

        try:
            enriched_snapshot = (
                self.market_sector_flow_analyzer
                .enrich_snapshot(snapshot)
            )
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            print(
                "[MARKET SECTORS] "
                "snapshot enrichment failed "
                f"error={error}"
            )
            enriched_snapshot = dict(snapshot)
            enriched_snapshot["sector_context_available"] = False
            enriched_snapshot["sector_context_error"] = error
            return enriched_snapshot

        enriched_snapshot["sector_context_error"] = None
        print(
            "[MARKET SECTORS] "
            "snapshot enriched "
            f"timestamp={enriched_snapshot.get('candle_timestamp')} "
            f"sectors={enriched_snapshot.get('sector_count')} "
            f"excluded={len(enriched_snapshot.get('excluded_sectors', {}))}"
        )
        return enriched_snapshot

    def _publish_initial_market_flow(self):
        for timeframe in MARKET_FLOW_TIMEFRAMES:
            self._publish_initial_market_flow_for_timeframe(
                timeframe
            )

    def _publish_initial_market_flow_for_timeframe(
        self,
        timeframe,
    ):
        print(
            "[MARKET FLOW] "
            f"building initial snapshot tf={timeframe}"
        )

        raw_btc_history = self.publisher.redis.lrange(
            history_key("BTCUSDT", timeframe),
            -3,
            -1,
        )

        if not raw_btc_history:
            print(
                "[MARKET FLOW] "
                "initial snapshot skipped: "
                f"BTC {timeframe} history unavailable"
            )
            return

        now_ms = int(time.time() * 1000)
        timeframe_ms = self._timeframe_ms(timeframe)
        candle_timestamp = None

        for raw_candle in reversed(raw_btc_history):
            try:
                candle = json.loads(raw_candle)
                candidate_timestamp = int(candle["timestamp"])
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue

            candidate_close_timestamp = (
                candidate_timestamp + timeframe_ms
            )
            if candidate_close_timestamp <= now_ms:
                candle_timestamp = candidate_timestamp
                break

        if candle_timestamp is None:
            print(
                "[MARKET FLOW] "
                "initial snapshot skipped: "
                f"no fully closed BTC {timeframe} candle"
            )
            return

        try:
            snapshot = self.market_flow_analyzer.calculate(
                symbols=self.symbols,
                timeframe=timeframe,
                candle_timestamp=candle_timestamp,
            )
        except Exception as exc:
            print(
                "[MARKET FLOW] "
                "initial calculation failed "
                f"tf={timeframe} error={exc}"
            )
            return

        coverage_pct = float(snapshot.get("coverage_pct", 0.0))
        if coverage_pct < MARKET_FLOW_MIN_COVERAGE_PCT:
            print(
                "[MARKET FLOW] "
                "initial snapshot rejected "
                f"tf={timeframe} "
                f"timestamp={candle_timestamp} "
                f"coverage={coverage_pct:.2f}% "
                f"minimum={MARKET_FLOW_MIN_COVERAGE_PCT:.2f}%"
            )
            return

        snapshot = self._enrich_market_flow_snapshot(
            snapshot,
            timeframe=timeframe,
        )
        publication = self.publisher.publish_market_flow_snapshot(
            timeframe=timeframe,
            snapshot=snapshot,
        )
        self.last_market_flow_timestamp[timeframe] = candle_timestamp

        breadth_key = f"market_breadth_{timeframe}"
        print(
            "[MARKET FLOW] "
            "initial snapshot published "
            f"tf={timeframe} "
            f"key={publication['key']} "
            f"timestamp={candle_timestamp} "
            f"valid={snapshot['valid_universe_size']}/"
            f"{snapshot['configured_universe_size']} "
            f"coverage={coverage_pct:.2f}% "
            f"breadth={snapshot.get(breadth_key)}"
        )

    def _begin_bootstrap_ws_buffering(self):
        with self.bootstrap_lock:
            self.bootstrap_closed_messages = []
            self.bootstrap_buffer_sequence = 0
            self.bootstrap_buffering = True

        print(
            "[MARKET DATA BOOTSTRAP] "
            "WS closed-candle buffering enabled"
        )
        
    def _timeframe_ms(
        self,
        timeframe,
    ):
        config = TIMEFRAME_CONFIGS.get(
            timeframe,
            {}
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


    def _recovery_scan_until_ms(
        self,
        minimum_ms=None,
    ):
        settled_now_ms = (
            int(time.time() * 1000)
            - CLOSED_INTEGRITY_SETTLE_SECONDS * 1000
        )

        if minimum_ms is None:
            return settled_now_ms

        return max(
            int(minimum_ms),
            settled_now_ms,
        )

    def _latest_fully_closed_open_timestamp(
        self,
        timeframe,
        now_ms,
    ):
        timeframe_ms = self._timeframe_ms(
            timeframe
        )

        return (
            (int(now_ms) // timeframe_ms)
            * timeframe_ms
            - timeframe_ms
        )
        
    def _begin_ws_recovery_buffering(self):
        with self.bootstrap_lock:
            if self.bootstrap_buffering:
                return False

            self.bootstrap_closed_messages = []
            self.bootstrap_buffer_sequence = 0
            self.bootstrap_buffering = True

        print(
            "[MARKET DATA RECOVERY] "
            "WS closed-candle buffering enabled"
        )

        return True

    def _detect_ws_connection_transition(
        self,
        now_ms,
    ):
        connected = bool(
            self.ws
            and self.ws.is_connected
        )

        if connected:
            if not self.ws_was_connected:
                self.ws_was_connected = True

                if (
                    self.ws_disconnect_detected_at_ms
                    is not None
                ):
                    return {
                        "type": "reconnected",
                        "detected_at_ms": int(now_ms),
                        "disconnected_at_ms": int(
                            self.ws_disconnect_detected_at_ms
                        ),
                    }

            return None

        if self.ws_was_connected:
            self.ws_was_connected = False

            self.ws_disconnect_detected_at_ms = int(
                now_ms
            )

            return {
                "type": "disconnected",
                "detected_at_ms": int(now_ms),
            }

        return None
    
    def _recovery_candidate_timestamps(
        self,
        timeframe,
        disconnected_at_ms,
        reconnected_at_ms,
    ):
        timeframe_ms = self._timeframe_ms(
            timeframe
        )

        start_timestamp = (
            self._latest_fully_closed_open_timestamp(
                timeframe=timeframe,
                now_ms=disconnected_at_ms,
            )
        )

        end_timestamp = (
            self._latest_fully_closed_open_timestamp(
                timeframe=timeframe,
                now_ms=reconnected_at_ms,
            )
        )

        if end_timestamp < start_timestamp:
            return []

        return list(
            range(
                start_timestamp,
                end_timestamp + timeframe_ms,
                timeframe_ms,
            )
        )
        
    def _find_recovery_gaps(
        self,
        disconnected_at_ms,
        reconnected_at_ms,
    ):
        gaps = {}
        
        with self.bootstrap_lock:
            buffered_keys = {
                (
                    symbol,
                    timeframe,
                    int(open_timestamp),
                )
                for (
                    _close_timestamp,
                    symbol,
                    timeframe,
                    open_timestamp,
                    _sequence,
                    _message,
                )
                in self.bootstrap_closed_messages
            }

        for timeframe in self.timeframes:
            candidate_timestamps = (
                self._recovery_candidate_timestamps(
                    timeframe=timeframe,
                    disconnected_at_ms=(
                        disconnected_at_ms
                    ),
                    reconnected_at_ms=(
                        reconnected_at_ms
                    ),
                )
            )

            if not candidate_timestamps:
                continue

            candidate_set = {
                int(timestamp)
                for timestamp
                in candidate_timestamps
            }

            # Alcanzan algunas candles extra porque
            # durante recovery los cierres nuevos
            # quedan bufferizados.
            lookback = (
                len(candidate_timestamps)
                + 3
            )

            pipeline = (
                self.publisher.redis.pipeline(
                    transaction=False
                )
            )

            for symbol in self.symbols:
                pipeline.lrange(
                    history_key(
                        symbol,
                        timeframe,
                    ),
                    -lookback,
                    -1,
                )

            histories = pipeline.execute()

            present_by_symbol = {}

            for symbol, raw_history in zip(
                self.symbols,
                histories,
            ):
                present = set()

                for raw_candle in raw_history:
                    try:
                        candle = json.loads(
                            raw_candle
                        )

                        timestamp = int(
                            candle["timestamp"]
                        )

                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        continue

                    if timestamp in candidate_set:
                        present.add(timestamp)

                present_by_symbol[symbol] = (
                    present
                )

            timeframe_gaps = {}

            for timestamp in candidate_timestamps:
                missing_symbols = [
                    symbol
                    for symbol in self.symbols
                    if (
                        int(timestamp)
                        not in present_by_symbol[
                            symbol
                        ]
                        and (
                            symbol,
                            timeframe,
                            int(timestamp),
                        )
                        not in buffered_keys
                    )
                ]

                if missing_symbols:
                    timeframe_gaps[
                        int(timestamp)
                    ] = missing_symbols

            if timeframe_gaps:
                gaps[timeframe] = (
                    timeframe_gaps
                )

        return gaps
    
    def _repair_ws_recovery_gaps(
        self,
        gaps,
        max_attempts=3,
    ):
        tasks = []

        for timeframe, timeframe_gaps in gaps.items():
            timeframe_ms = self._timeframe_ms(
                timeframe
            )

            for (
                candle_timestamp,
                missing_symbols,
            ) in timeframe_gaps.items():
                candle_timestamp = int(
                    candle_timestamp
                )

                close_timestamp = (
                    candle_timestamp
                    + timeframe_ms
                    - 1
                )

                for symbol in sorted(
                    set(missing_symbols)
                ):
                    tasks.append(
                        (
                            close_timestamp,
                            symbol,
                            timeframe,
                            candle_timestamp,
                        )
                    )

        # Mismo criterio temporal que usamos
        # al vaciar el buffer de WS:
        # primero cierre, luego símbolo y TF.
        tasks.sort(
            key=lambda item: (
                item[0],
                item[1],
                self._timeframe_ms(
                    item[2]
                ),
                item[3],
            )
        )

        recovered = []
        failed = []

        print(
            "[MARKET DATA RECOVERY] "
            f"repair starting tasks={len(tasks)}"
        )

        for (
            _close_timestamp,
            symbol,
            timeframe,
            candle_timestamp,
        ) in tasks:
            if not self.running:
                failed.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": (
                            candle_timestamp
                        ),
                        "error": (
                            "service_stopping"
                        ),
                    }
                )
                continue

            last_error = None
            publication = None

            for attempt in range(
                1,
                max_attempts + 1,
            ):
                try:
                    candle = (
                        fetch_closed_futures_candle(
                            symbol=symbol,
                            timeframe=timeframe,
                            candle_timestamp=(
                                candle_timestamp
                            ),
                        )
                    )

                    if candle is None:
                        raise RuntimeError(
                            "candle_unavailable"
                        )

                    publication = (
                        self.publisher
                        .publish_recovered_candle(
                            symbol=symbol,
                            timeframe=timeframe,
                            candle=candle,
                        )
                    )

                    if (
                        int(
                            publication[
                                "timestamp"
                            ]
                        )
                        != candle_timestamp
                    ):
                        raise RuntimeError(
                            "published_timestamp_"
                            "mismatch"
                        )

                    last_error = None
                    break

                except Exception as exc:
                    last_error = (
                        f"{type(exc).__name__}:"
                        f"{exc}"
                    )

                    if attempt < max_attempts:
                        if self.stop_event.wait(1):
                            last_error = (
                                "service_stopping"
                            )
                            break

            if last_error is not None:
                failed.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": (
                            candle_timestamp
                        ),
                        "error": last_error,
                    }
                )

                print(
                    "[MARKET DATA RECOVERY] "
                    f"repair failed "
                    f"symbol={symbol} "
                    f"tf={timeframe} "
                    f"timestamp="
                    f"{candle_timestamp} "
                    f"error={last_error}"
                )

                continue

            recovered.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": (
                        candle_timestamp
                    ),
                }
            )

            # El general recovery también debe
            # alimentar el batch de Market Flow.
            if timeframe in MARKET_FLOW_TIMEFRAMES:
                self._register_market_flow_close(
                    symbol=symbol,
                    timeframe=timeframe,
                    candle_timestamp=candle_timestamp,
                )

        print(
            "[MARKET DATA RECOVERY] "
            f"repair completed "
            f"requested={len(tasks)} "
            f"recovered={len(recovered)} "
            f"failed={len(failed)}"
        )

        return {
            "requested": len(tasks),
            "recovered": len(recovered),
            "failed": len(failed),
            "recovered_items": recovered,
            "failed_items": failed,
        }
        
    def _flush_ws_recovery_buffer(self):
        published_keys = set()
        published = 0
        duplicates = 0
        passes = 0

        while True:
            with self.bootstrap_lock:
                batch = (
                    self.bootstrap_closed_messages
                )

                self.bootstrap_closed_messages = []

            if batch:
                passes += 1

                batch.sort(
                    key=lambda item: (
                        item[0],
                        item[1],
                        int(
                            TIMEFRAME_CONFIGS.get(
                                item[2],
                                {},
                            ).get(
                                "ms_per_candle",
                                10**18,
                            )
                        ),
                        item[3],
                        item[4],
                    )
                )

                for (
                    _close_timestamp,
                    symbol,
                    timeframe,
                    open_timestamp,
                    _sequence,
                    message,
                ) in batch:
                    key = (
                        symbol,
                        timeframe,
                        int(open_timestamp),
                    )

                    if key in published_keys:
                        duplicates += 1
                        continue

                    self._process_ws_message(
                        message,
                        publish_price=False,
                    )

                    published_keys.add(key)
                    published += 1

            with self.bootstrap_lock:
                if self.bootstrap_closed_messages:
                    continue

                # Antes del handoff final verificamos que
                # el WS siga físicamente conectado.
                #
                # Si volvió a caer durante recovery,
                # mantenemos el buffer activo y NO
                # regresamos a publicación directa.
                if not (
                    self.ws
                    and self.ws.is_connected
                ):
                    print(
                        "[MARKET DATA RECOVERY] "
                        "buffer handoff blocked: "
                        "WS disconnected again"
                    )

                    return {
                        "passes": passes,
                        "published": published,
                        "duplicates": duplicates,
                        "released": False,
                    }

                # Handoff atómico:
                # desde este punto los callbacks
                # vuelven a publicar directamente.
                self.bootstrap_buffering = False
                break

        print(
            "[MARKET DATA RECOVERY] "
            f"buffer flush complete "
            f"passes={passes} "
            f"published={published} "
            f"duplicates={duplicates}"
        )

        return {
            "passes": passes,
            "published": published,
            "duplicates": duplicates,
            "released": True,
        }
        
    def _run_ws_recovery(
        self,
        disconnected_at_ms,
        reconnected_at_ms,
    ):
        if self.ws_recovery_in_progress:
            return None

        self.ws_recovery_in_progress = True
        self.phase = "RECOVERING_WS"
        self._write_status()

        print(
            "[MARKET DATA RECOVERY] "
            f"starting disconnected_at="
            f"{disconnected_at_ms} "
            f"reconnected_at="
            f"{reconnected_at_ms}"
        )

        try:
            scan_until_ms = (
                self._recovery_scan_until_ms(
                    minimum_ms=reconnected_at_ms,
                )
            )

            gaps = self._find_recovery_gaps(
                disconnected_at_ms=(
                    disconnected_at_ms
                ),
                reconnected_at_ms=(
                    scan_until_ms
                ),
            )

            gap_count = sum(
                len(missing_symbols)
                for timeframe_gaps
                in gaps.values()
                for missing_symbols
                in timeframe_gaps.values()
            )

            print(
                "[MARKET DATA RECOVERY] "
                f"initial gaps={gap_count}"
            )

            repair_result = (
                self._repair_ws_recovery_gaps(
                    gaps
                )
            )
            

            if repair_result["failed"] > 0:
                raise RuntimeError(
                    "WS recovery failed "
                    f"for "
                    f"{repair_result['failed']} "
                    "candles"
                )

            # Segunda verificación.
            #
            # Mientras hacíamos REST pudo haber
            # cerrado otra candle o incluso haber
            # ocurrido otra interrupción.
            verify_until_ms = (
                self._recovery_scan_until_ms(
                    minimum_ms=scan_until_ms,
                )
            )

            remaining_gaps = (
                self._find_recovery_gaps(
                    disconnected_at_ms=(
                        disconnected_at_ms
                    ),
                    reconnected_at_ms=(
                        verify_until_ms
                    ),
                )
            )

            if remaining_gaps:
                second_repair = (
                    self._repair_ws_recovery_gaps(
                        remaining_gaps
                    )
                )

                if second_repair["failed"] > 0:
                    raise RuntimeError(
                        "WS recovery verification "
                        "failed for "
                        f"{second_repair['failed']} "
                        "candles"
                    )

            # Verificación final antes de liberar
            # nuevamente la publicación directa.
            final_until_ms = (
                self._recovery_scan_until_ms(
                    minimum_ms=verify_until_ms,
                )
            )

            final_gaps = self._find_recovery_gaps(
                disconnected_at_ms=(
                    disconnected_at_ms
                ),
                reconnected_at_ms=(
                    final_until_ms
                ),
            )

            if final_gaps:
                final_gap_count = sum(
                    len(missing_symbols)
                    for timeframe_gaps
                    in final_gaps.values()
                    for missing_symbols
                    in timeframe_gaps.values()
                )

                raise RuntimeError(
                    "WS recovery incomplete: "
                    f"{final_gap_count} gaps remain"
                )

            flush_result = (
                self._flush_ws_recovery_buffer()
            )
            
            if not flush_result.get(
                "released",
                False,
            ):
                redetected_at_ms = int(
                    scan_until_ms
                )

                # La caída ocurrió mientras el recovery
                # estaba ejecutándose, por lo que el loop
                # principal todavía no pudo observarla.
                #
                # Dejamos explícitamente armado el estado
                # para que el próximo reconnect dispare
                # un recovery nuevo.
                self.ws_was_connected = False

                self.ws_disconnect_detected_at_ms = (
                    redetected_at_ms
                )

                print(
                    "[MARKET DATA RECOVERY] "
                    "aborted safely: "
                    "WS disconnected during recovery "
                    f"detected_at={redetected_at_ms}"
                )

                return {
                    "repair": repair_result,
                    "flush": flush_result,
                    "aborted": (
                        "ws_disconnected_during_recovery"
                    ),
                }

            print(
                "[MARKET DATA RECOVERY] "
                "completed successfully "
                f"buffer_published="
                f"{flush_result['published']}"
            )

            self.ws_disconnect_detected_at_ms = (
                None
            )

            return {
                "repair": repair_result,
                "flush": flush_result,
            }

        finally:
            self.ws_recovery_in_progress = False

            if (
                self.ws
                and self.ws.is_connected
            ):
                self.phase = "READY"
            else:
                self.phase = "CONNECTING_WS"

            self._write_status()

    def _closed_ws_metadata(self, message):
        payload = message

        if (
            isinstance(payload, dict)
            and "data" in payload
        ):
            payload = payload["data"]

        if not isinstance(payload, dict):
            return None

        if payload.get("e") not in (
            "continuous_kline",
            "kline",
        ):
            return None

        kline = payload.get("k")

        if not isinstance(kline, dict):
            return None

        if not kline.get("x"):
            return None

        symbol = (
            payload.get("s")
            or payload.get("ps")
        )

        timeframe = kline.get("i")

        if not symbol or not timeframe:
            return None

        try:
            open_timestamp = int(
                kline["t"]
            )
            close_timestamp = int(
                kline["T"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

        return {
            "symbol": str(symbol).upper(),
            "timeframe": str(timeframe),
            "timestamp": open_timestamp,
            "close_timestamp": close_timestamp,
        }


    def _buffer_closed_ws_message_if_needed(
        self,
        message,
    ):
        metadata = self._closed_ws_metadata(
            message
        )

        if metadata is None:
            return None

        with self.bootstrap_lock:
            if not self.bootstrap_buffering:
                return None

            self.bootstrap_buffer_sequence += 1

            self.bootstrap_closed_messages.append(
                (
                    metadata["close_timestamp"],
                    metadata["symbol"],
                    metadata["timeframe"],
                    metadata["timestamp"],
                    self.bootstrap_buffer_sequence,
                    copy.deepcopy(message),
                )
            )

        return {
            "type": "buffered_closed_candle",
            **metadata,
        }


    def _process_ws_message(
        self,
        message,
        publish_price=True,
    ):
        result = self.publisher.publish_ws_message(
            message,
            publish_price=publish_price,
        )
        if not isinstance(result, dict):
            return result
        if result.get("type") != "closed_candle":
            return result

        timeframe = result.get("timeframe")
        if timeframe not in MARKET_FLOW_TIMEFRAMES:
            return result

        symbol = result.get("symbol")
        candle_timestamp = result.get("timestamp")
        if not symbol or candle_timestamp is None:
            return result

        self._register_market_flow_close(
            symbol=symbol,
            timeframe=timeframe,
            candle_timestamp=candle_timestamp,
        )
        return result

    def _flush_bootstrap_ws_buffer(self):
        if self.history_cutoff_ms is None:
            raise RuntimeError(
                "History cutoff is not initialized"
            )

        cutoff_ms = int(
            self.history_cutoff_ms
        )

        published_keys = set()
        published = 0
        discarded_before_cutoff = 0
        duplicates = 0
        passes = 0

        while True:
            with self.bootstrap_lock:
                batch = (
                    self.bootstrap_closed_messages
                )
                self.bootstrap_closed_messages = []

            if batch:
                passes += 1

                batch.sort(
                    key=lambda item: (
                        item[0],
                        item[1],
                        int(
                            TIMEFRAME_CONFIGS.get(
                                item[2],
                                {},
                            ).get(
                                "ms_per_candle",
                                10**18,
                            )
                        ),
                        item[3],
                        item[4],
                    )
                )

                for (
                    close_timestamp,
                    symbol,
                    timeframe,
                    open_timestamp,
                    _sequence,
                    message,
                ) in batch:
                    if (
                        int(close_timestamp)
                        < cutoff_ms
                    ):
                        discarded_before_cutoff += 1
                        continue

                    key = (
                        symbol,
                        timeframe,
                        int(open_timestamp),
                    )

                    if key in published_keys:
                        duplicates += 1
                        continue

                    self._process_ws_message(
                        message,
                        publish_price=False,
                    )

                    published_keys.add(key)
                    published += 1

            with self.bootstrap_lock:
                if self.bootstrap_closed_messages:
                    continue

                # Atomic handoff:
                # callbacks blocked on this same lock will either
                # have been buffered already or will observe False
                # and publish directly after the lock is released.
                self.bootstrap_buffering = False
                break

        print(
            "[MARKET DATA BOOTSTRAP] "
            f"catchup complete "
            f"passes={passes} "
            f"published={published} "
            f"discarded_before_cutoff="
            f"{discarded_before_cutoff} "
            f"duplicates={duplicates}"
        )

        return {
            "passes": passes,
            "published": published,
            "discarded_before_cutoff": (
                discarded_before_cutoff
            ),
            "duplicates": duplicates,
        }


    def _enqueue_micro_flow_seconds(
        self,
        states,
    ):
        if not states:
            return 0

        queued = 0
        with self.micro_flow_queue_lock:
            for state in states:
                if (
                    self.micro_flow_publish_queue.maxlen
                    is not None
                    and len(self.micro_flow_publish_queue)
                    >= self.micro_flow_publish_queue.maxlen
                ):
                    self.micro_flow_queue_dropped += 1
                    continue

                self.micro_flow_publish_queue.append(
                    state
                )
                queued += 1

        return queued


    def _drain_micro_flow_queue(
        self,
        max_items=5000,
    ):
        batch = []
        with self.micro_flow_queue_lock:
            while (
                self.micro_flow_publish_queue
                and len(batch) < int(max_items)
            ):
                batch.append(
                    self.micro_flow_publish_queue.popleft()
                )
        return batch


    def _flush_micro_flow_seconds(self):
        if (
            not self.enable_micro_flow
            or self.micro_flow_aggregator is None
        ):
            return

        now_ms = int(time.time() * 1000)
        ready = self.micro_flow_aggregator.flush_ready(
            now_ms
        )
        self._enqueue_micro_flow_seconds(ready)

        batch = self._drain_micro_flow_queue(
            max_items=5000
        )
        if not batch:
            return

        try:
            published = (
                self.publisher.publish_micro_flow_seconds(
                    batch
                )
            )
            self.micro_flow_seconds_published += int(
                published
            )
            self.micro_flow_last_published_at_ms = now_ms

        except Exception as exc:
            # Micro Flow is research-only. A temporary problem in this path
            # must not take down the canonical candle producer. Requeue the
            # batch when possible and expose the error in status/logs.
            self.micro_flow_publish_errors += 1
            self._enqueue_micro_flow_seconds(batch)
            print(
                "[MICRO FLOW] publish error "
                f"error={type(exc).__name__}:{exc}"
            )


    def _on_ws_message(self, message):
        if (
            self.enable_micro_flow
            and self.micro_flow_aggregator is not None
            and self.micro_flow_aggregator.is_agg_trade_message(
                message
            )
        ):
            ready = (
                self.micro_flow_aggregator
                .ingest_ws_message(message)
            )
            self._enqueue_micro_flow_seconds(ready)
            return {
                "type": "agg_trade",
                "queued_seconds": len(ready),
            }

        buffered = (
            self._buffer_closed_ws_message_if_needed(
                message
            )
        )

        if buffered is not None:
            return buffered

        return self._process_ws_message(
            message
        )


    def _register_market_flow_close(
        self,
        symbol,
        timeframe,
        candle_timestamp,
    ):
        timeframe = str(timeframe)
        if timeframe not in MARKET_FLOW_TIMEFRAMES:
            return

        candle_timestamp = int(candle_timestamp)
        now = time.monotonic()
        batch_key = (timeframe, candle_timestamp)

        with self.market_flow_lock:
            batch = self.market_flow_batches.setdefault(
                batch_key,
                {
                    "timeframe": timeframe,
                    "candle_timestamp": candle_timestamp,
                    "symbols": set(),
                    "first_seen_at": now,
                    "last_seen_at": now,
                },
            )
            batch["symbols"].add(symbol)
            batch["last_seen_at"] = now

    def _repair_market_flow_candles(
        self,
        timeframe,
        candle_timestamp,
        missing_symbols,
    ):
        timeframe = str(timeframe)
        candle_timestamp = int(candle_timestamp)
        missing_symbols = sorted(set(missing_symbols))
        recovered_symbols = []
        failed_symbols = {}

        print(
            "[MARKET FLOW REPAIR] "
            f"starting tf={timeframe} "
            f"timestamp={candle_timestamp} "
            f"missing={len(missing_symbols)}"
        )

        for symbol in missing_symbols:
            if not self.running:
                failed_symbols[symbol] = "service_stopping"
                continue

            candle = None
            last_error = None
            for attempt in range(1, 4):
                try:
                    candle = fetch_closed_futures_candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        candle_timestamp=candle_timestamp,
                    )
                    if candle is None:
                        last_error = "candle_unavailable"
                    else:
                        break
                except Exception as exc:
                    last_error = str(exc)

                if attempt < 3 and self.stop_event.wait(1):
                    last_error = "service_stopping"
                    break

            if candle is None:
                failed_symbols[symbol] = last_error or "candle_unavailable"
                print(
                    "[MARKET FLOW REPAIR] "
                    f"failed tf={timeframe} "
                    f"symbol={symbol} "
                    f"timestamp={candle_timestamp} "
                    f"error={failed_symbols[symbol]}"
                )
                continue

            try:
                publication = self.publisher.publish_recovered_candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    candle=candle,
                )
            except Exception as exc:
                failed_symbols[symbol] = f"publish_failed:{exc}"
                print(
                    "[MARKET FLOW REPAIR] "
                    f"failed tf={timeframe} "
                    f"symbol={symbol} "
                    f"timestamp={candle_timestamp} "
                    f"error={failed_symbols[symbol]}"
                )
                continue

            recovered_symbols.append(symbol)
            if int(publication["timestamp"]) != candle_timestamp:
                failed_symbols[symbol] = "published_timestamp_mismatch"
                recovered_symbols.remove(symbol)

        print(
            "[MARKET FLOW REPAIR] "
            f"completed tf={timeframe} "
            f"timestamp={candle_timestamp} "
            f"requested={len(missing_symbols)} "
            f"recovered={len(recovered_symbols)} "
            f"failed={len(failed_symbols)}"
        )

        return {
            "requested": len(missing_symbols),
            "recovered": len(recovered_symbols),
            "failed": len(failed_symbols),
            "recovered_symbols": recovered_symbols,
            "failed_symbols": failed_symbols,
        }

    def _maybe_publish_market_flow(self):
        now = time.monotonic()
        expected_symbols = len(set(self.symbols))
        batch_to_process = None
        selected_key = None

        with self.market_flow_lock:
            # Sort primarily by candle timestamp, then timeframe. If 1h and 4h
            # close together they are processed independently on consecutive
            # loop iterations.
            for batch_key in sorted(
                self.market_flow_batches,
                key=lambda item: (item[1], item[0]),
            ):
                batch = self.market_flow_batches[batch_key]
                received_symbols = len(batch["symbols"])
                age_seconds = now - batch["first_seen_at"]
                quiet_seconds = now - batch["last_seen_at"]
                complete = received_symbols >= expected_symbols
                settled = (
                    age_seconds >= MARKET_FLOW_SETTLE_SECONDS
                    and quiet_seconds >= 3
                )
                timed_out = age_seconds >= MARKET_FLOW_MAX_WAIT_SECONDS

                if complete or settled or timed_out:
                    selected_key = batch_key
                    batch_to_process = {
                        "timeframe": batch["timeframe"],
                        "candle_timestamp": batch["candle_timestamp"],
                        "received_symbols": received_symbols,
                        "expected_symbols": expected_symbols,
                        "age_seconds": age_seconds,
                    }
                    del self.market_flow_batches[batch_key]
                    break

        if batch_to_process is None:
            return

        timeframe = batch_to_process["timeframe"]
        candle_timestamp = batch_to_process["candle_timestamp"]

        if self.last_market_flow_timestamp.get(timeframe) == candle_timestamp:
            return

        print(
            "[MARKET FLOW] "
            f"calculating tf={timeframe} "
            f"timestamp={candle_timestamp} "
            f"received={batch_to_process['received_symbols']}/"
            f"{batch_to_process['expected_symbols']} "
            f"waited={batch_to_process['age_seconds']:.1f}s"
        )

        try:
            snapshot = self.market_flow_analyzer.calculate(
                symbols=self.symbols,
                timeframe=timeframe,
                candle_timestamp=candle_timestamp,
            )
        except Exception as exc:
            print(
                "[MARKET FLOW] calculation failed "
                f"tf={timeframe} "
                f"timestamp={candle_timestamp} "
                f"error={exc}"
            )
            return

        missing_symbols = [
            symbol
            for symbol, reason in snapshot.get("excluded_symbols", {}).items()
            if reason == "missing_batch_candle"
        ]

        if missing_symbols:
            repair_result = self._repair_market_flow_candles(
                timeframe=timeframe,
                candle_timestamp=candle_timestamp,
                missing_symbols=missing_symbols,
            )
            if repair_result["recovered"] > 0:
                try:
                    snapshot = self.market_flow_analyzer.calculate(
                        symbols=self.symbols,
                        timeframe=timeframe,
                        candle_timestamp=candle_timestamp,
                    )
                except Exception as exc:
                    print(
                        "[MARKET FLOW] recalculation failed "
                        f"tf={timeframe} "
                        f"timestamp={candle_timestamp} "
                        f"error={exc}"
                    )
                    return

        coverage_pct = float(snapshot.get("coverage_pct", 0.0))
        if coverage_pct < MARKET_FLOW_MIN_COVERAGE_PCT:
            print(
                "[MARKET FLOW] snapshot rejected "
                f"tf={timeframe} "
                f"timestamp={candle_timestamp} "
                f"coverage={coverage_pct:.2f}% "
                f"minimum={MARKET_FLOW_MIN_COVERAGE_PCT:.2f}%"
            )
            return

        snapshot = self._enrich_market_flow_snapshot(
            snapshot,
            timeframe=timeframe,
        )
        publication = self.publisher.publish_market_flow_snapshot(
            timeframe=timeframe,
            snapshot=snapshot,
        )
        self.last_market_flow_timestamp[timeframe] = candle_timestamp

        breadth_key = f"market_breadth_{timeframe}"
        print(
            "[MARKET FLOW] published "
            f"tf={timeframe} "
            f"key={publication['key']} "
            f"timestamp={candle_timestamp} "
            f"valid={snapshot['valid_universe_size']}/"
            f"{snapshot['configured_universe_size']} "
            f"coverage={coverage_pct:.2f}% "
            f"breadth={snapshot.get(breadth_key)}"
        )

    def _check_closed_candle_integrity(self):
        if (
            not self.running
            or self.phase != "READY"
            or self.ws_recovery_in_progress
            or self.bootstrap_buffering
            or self.ws is None
            or not self.ws.is_connected
        ):
            return

        now_monotonic = time.monotonic()

        if (
            now_monotonic
            - self.closed_integrity_last_check_monotonic
            < CLOSED_INTEGRITY_CHECK_SECONDS
        ):
            return

        self.closed_integrity_last_check_monotonic = (
            now_monotonic
        )

        now_ms = int(time.time() * 1000)

        # No auditamos una candle que acaba de cerrar.
        # Le damos tiempo al WS para completar el batch.
        scan_until_ms = (
            now_ms
            - CLOSED_INTEGRITY_SETTLE_SECONDS * 1000
        )

        scan_from_ms = (
            scan_until_ms
            - CLOSED_INTEGRITY_LOOKBACK_SECONDS * 1000
        )

        gaps = self._find_recovery_gaps(
            disconnected_at_ms=scan_from_ms,
            reconnected_at_ms=scan_until_ms,
        )

        gap_count = sum(
            len(missing_symbols)
            for timeframe_gaps in gaps.values()
            for missing_symbols
            in timeframe_gaps.values()
        )

        if gap_count == 0:
            return

        print(
            "[MARKET DATA INTEGRITY] "
            f"gaps detected={gap_count} "
            f"scan_from={scan_from_ms} "
            f"scan_until={scan_until_ms}"
        )

        # Desde acá bufferizamos nuevos closes
        # mientras hacemos repair REST.
        started = self._begin_ws_recovery_buffering()

        if not started:
            return

        self.ws_recovery_in_progress = True
        self.phase = "RECOVERING_WS"
        self._write_status()

        try:
            # Recalculamos después de activar buffering.
            gaps = self._find_recovery_gaps(
                disconnected_at_ms=scan_from_ms,
                reconnected_at_ms=scan_until_ms,
            )

            gap_count = sum(
                len(missing_symbols)
                for timeframe_gaps in gaps.values()
                for missing_symbols
                in timeframe_gaps.values()
            )

            if gap_count:
                repair_result = (
                    self._repair_ws_recovery_gaps(
                        gaps
                    )
                )

                if repair_result["failed"] > 0:
                    raise RuntimeError(
                        "Closed-candle integrity repair "
                        f"failed for "
                        f"{repair_result['failed']} candles"
                    )

            # El repair pudo tardar.
            # Movemos el horizonte antes de verificar.
            verify_scan_until_ms = (
                self._recovery_scan_until_ms(
                    minimum_ms=scan_until_ms,
                )
            )

            remaining_gaps = (
                self._find_recovery_gaps(
                    disconnected_at_ms=scan_from_ms,
                    reconnected_at_ms=(
                        verify_scan_until_ms
                    ),
                )
            )

            if remaining_gaps:
                second_repair = (
                    self._repair_ws_recovery_gaps(
                        remaining_gaps
                    )
                )

                if second_repair["failed"] > 0:
                    raise RuntimeError(
                        "Closed-candle integrity "
                        "verification failed for "
                        f"{second_repair['failed']} candles"
                    )

            # El segundo repair también pudo tardar.
            # Movemos el horizonte una vez más.
            final_scan_until_ms = (
                self._recovery_scan_until_ms(
                    minimum_ms=verify_scan_until_ms,
                )
            )

            final_gaps = self._find_recovery_gaps(
                disconnected_at_ms=scan_from_ms,
                reconnected_at_ms=(
                    final_scan_until_ms
                ),
            )

            final_gap_count = sum(
                len(missing_symbols)
                for timeframe_gaps
                in final_gaps.values()
                for missing_symbols
                in timeframe_gaps.values()
            )

            if final_gap_count:
                raise RuntimeError(
                    "Closed-candle integrity "
                    f"incomplete: "
                    f"{final_gap_count} gaps remain"
                )

            flush_result = (
                self._flush_ws_recovery_buffer()
            )

            if not flush_result.get(
                "released",
                False,
            ):
                raise RuntimeError(
                    "Closed-candle integrity repair "
                    "could not release WS buffer"
                )

            print(
                "[MARKET DATA INTEGRITY] "
                f"repaired successfully "
                f"initial_gaps={gap_count} "
                f"buffer_published="
                f"{flush_result['published']}"
            )

        finally:
            self.ws_recovery_in_progress = False

            if (
                self.ws
                and self.ws.is_connected
            ):
                self.phase = "READY"
            else:
                self.phase = "CONNECTING_WS"

            self._write_status()

    def _check_ws_connection_watchdog(self):
        connected = bool(
            self.ws
            and self.ws.is_connected
        )

        now = time.monotonic()

        if connected:
            if (
                self.ws_connecting_since_monotonic
                is not None
            ):
                disconnected_for = (
                    now
                    - self.ws_connecting_since_monotonic
                )

                print(
                    "[MARKET DATA WS WATCHDOG] "
                    "connection recovered "
                    f"after={disconnected_for:.1f}s"
                )

            self.ws_connecting_since_monotonic = None
            return

        if self.ws_connecting_since_monotonic is None:
            self.ws_connecting_since_monotonic = now

            print(
                "[MARKET DATA WS WATCHDOG] "
                "disconnected timer started "
                f"timeout="
                f"{WS_CONNECTING_FAILFAST_SECONDS}s"
            )

            return

        disconnected_for = (
            now
            - self.ws_connecting_since_monotonic
        )

        if (
            disconnected_for
            < WS_CONNECTING_FAILFAST_SECONDS
        ):
            return

        print(
            "[MARKET DATA WS WATCHDOG] "
            "FATAL websocket remained disconnected "
            f"for={disconnected_for:.1f}s "
            "forcing process restart"
        )

        raise RuntimeError(
            "WebSocket stuck disconnected for "
            f"{disconnected_for:.1f}s"
        )


    def _start_websocket(self):

        self.ws = WSClient(

            self._on_ws_message,

            timeframes=self.timeframes,

            symbols=self.symbols,

            chunk_size=self.chunk_size,

            stale_after=self.stale_after,

            include_agg_trades=(
                self.enable_micro_flow
            ),

        )



        self.ws.start()



        self.ws_thread = threading.Thread(

            target=self.ws.run,

            daemon=True,

            name="market-data-service-ws",

        )



        self.ws_thread.start()



    def _start_heartbeat(self):

        self.heartbeat_thread = threading.Thread(

            target=self._heartbeat_loop,

            daemon=True,

            name="market-data-heartbeat",

        )



        self.heartbeat_thread.start()



    def _heartbeat_loop(self):

        while self.running:

            lock_renewed = (

                self._renew_producer_lock()

            )



            if not lock_renewed:

                print(

                    "[MARKET DATA SERVICE] "

                    "producer lock lost"

                )



                self.running = False

                self.stop_event.set()

                return



            heartbeat = {

                "service_id": self.service_id,

                "phase": self.phase,

                "timestamp": int(

                    time.time() * 1000

                ),

            }



            self.publisher.redis.set(

                HEARTBEAT_KEY,

                json.dumps(

                    heartbeat,

                    separators=(",", ":"),

                ),

                ex=HEARTBEAT_TTL_SECONDS,

            )



            self._write_status()



            self.stop_event.wait(5)



    def _write_status(self):

        status = {

            "service_id": self.service_id,

            "phase": self.phase,

            "running": self.running,

            "ws_connected": bool(

                self.ws

                and self.ws.is_connected

            ),

            "symbols": len(self.symbols),

            "market_sector_catalog_available": (

                self.market_sector_catalog

                is not None

            ),

            "market_sector_catalog_symbols": (

                len(

                    self.market_sector_catalog

                )

                if self.market_sector_catalog

                is not None

                else 0

            ),

            "market_sector_catalog_generated_at": (

                self.market_sector_catalog.generated_at

                if self.market_sector_catalog

                is not None

                else None

            ),

            "market_sector_catalog_error": (

                self.market_sector_catalog_error

            ),

            "timeframes": self.timeframes,
            "market_flow_timeframes": list(MARKET_FLOW_TIMEFRAMES),
            "micro_flow_enabled": self.enable_micro_flow,
            "micro_flow_seconds_published": (
                self.micro_flow_seconds_published
            ),
            "micro_flow_queue_depth": (
                len(self.micro_flow_publish_queue)
            ),
            "micro_flow_queue_dropped": (
                self.micro_flow_queue_dropped
            ),
            "micro_flow_publish_errors": (
                self.micro_flow_publish_errors
            ),
            "micro_flow_last_published_at_ms": (
                self.micro_flow_last_published_at_ms
            ),
            "micro_flow_aggregator": (
                self.micro_flow_aggregator.diagnostics()
                if self.micro_flow_aggregator is not None
                else None
            ),

            "history_cutoff_ms": (

                self.history_cutoff_ms

            ),

            "bootstrap_buffering": (

                self.bootstrap_buffering

            ),

            "bootstrap_buffered_closed": (

                len(self.bootstrap_closed_messages)

            ),

            "histories_loaded": (

                self.histories_loaded

            ),

            "history_candles_loaded": (

                self.history_candles_loaded

            ),

            "updated_at": int(

                time.time() * 1000

            ),

        }



        self.publisher.redis.set(

            STATUS_KEY,

            json.dumps(

                status,

                separators=(",", ":"),

            ),

        )

    def _acquire_producer_lock_with_wait(self):
        started = time.monotonic()

        while not self.stop_event.is_set():
            if self._acquire_producer_lock():
                waited = time.monotonic() - started

                print(
                    "[MARKET DATA SERVICE] "
                    "producer lock acquired "
                    f"service_id={self.service_id} "
                    f"waited={waited:.1f}s"
                )

                return True

            elapsed = time.monotonic() - started

            try:
                owner = self.publisher.redis.get(
                    PRODUCER_LOCK_KEY
                )

                lock_ttl = self.publisher.redis.ttl(
                    PRODUCER_LOCK_KEY
                )

                heartbeat_raw = self.publisher.redis.get(
                    HEARTBEAT_KEY
                )

                heartbeat_ttl = self.publisher.redis.ttl(
                    HEARTBEAT_KEY
                )

                heartbeat_owner = None

                if heartbeat_raw:
                    try:
                        heartbeat_payload = json.loads(
                            heartbeat_raw
                        )

                        heartbeat_owner = (
                            heartbeat_payload.get(
                                "service_id"
                            )
                        )
                    except Exception:
                        heartbeat_owner = None

                print(
                    "[MARKET DATA SERVICE] "
                    "producer lock busy | "
                    f"owner={owner} "
                    f"lock_ttl={lock_ttl}s "
                    f"heartbeat_owner={heartbeat_owner} "
                    f"heartbeat_ttl={heartbeat_ttl}s "
                    f"waited={elapsed:.1f}s"
                )

            except Exception as exc:
                print(
                    "[MARKET DATA SERVICE] "
                    "producer lock diagnostics failed | "
                    f"error={exc}"
                )

            if (
                elapsed
                >= PRODUCER_LOCK_STARTUP_WAIT_SECONDS
            ):
                return False

            if self.stop_event.wait(
                PRODUCER_LOCK_STARTUP_POLL_SECONDS
            ):
                return False

        return False


    def _acquire_producer_lock(self):

        return bool(

            self.publisher.redis.set(

                PRODUCER_LOCK_KEY,

                self.service_id,

                nx=True,

                ex=PRODUCER_LOCK_TTL_SECONDS,

            )

        )



    def _renew_producer_lock(self):

        result = self.publisher.redis.eval(

            """

            if redis.call("get", KEYS[1]) == ARGV[1] then

                return redis.call("expire", KEYS[1], ARGV[2])

            end

            return 0

            """,

            1,

            PRODUCER_LOCK_KEY,

            self.service_id,

            PRODUCER_LOCK_TTL_SECONDS,

        )



        return bool(result)



    def _delete_if_owned(self, key):

        self.publisher.redis.eval(

            """

            if redis.call("get", KEYS[1]) == ARGV[1] then

                return redis.call("del", KEYS[1])

            end

            return 0

            """,

            1,

            key,

            self.service_id,

        )





def parse_args():

    parser = argparse.ArgumentParser()



    parser.add_argument(

        "--redis-host",

        default="127.0.0.1",

    )



    parser.add_argument(

        "--redis-port",

        type=int,

        default=6379,

    )



    parser.add_argument(

        "--redis-db",

        type=int,

        default=0,

    )

    parser.add_argument(

        "--disable-micro-flow",

        action="store_true",

        help=(
            "Do not subscribe to aggTrade streams or publish "
            "Micro Flow second states."
        ),

    )



    return parser.parse_args()





def main():

    args = parse_args()



    service = MarketDataService(

        redis_host=args.redis_host,

        redis_port=args.redis_port,

        redis_db=args.redis_db,

        enable_micro_flow=(
            not args.disable_micro_flow
        ),

    )



    def request_stop(

        signum,

        frame,

    ):

        print(

            "[MARKET DATA SERVICE] "

            f"received signal={signum}"

        )



        service.running = False

        service.stop_event.set()



    signal.signal(

        signal.SIGTERM,

        request_stop,

    )



    signal.signal(

        signal.SIGINT,

        request_stop,

    )



    service.run()





if __name__ == "__main__":

    main()