import argparse
import json
import signal
import threading
import time
import uuid

from config.strategies.v1 import SYMBOLS
from config.timeframes import MODE_CONFIG
from data.market_data import (
    fetch_closed_futures_candle,
    fetch_history,
)
from engine.live.data.redis_market_data_protocol import (
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    PRODUCER_LOCK_KEY,
    PRODUCER_LOCK_TTL_SECONDS,
    STATUS_KEY,
    history_key,
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


DAYS_BY_TF = {
    "1m": 1,
    "5m": 2,
    "15m": 3,
    "30m": 5,
    "1h": 7,
    "4h": 25,
    "1d": 180,
}

MARKET_FLOW_TIMEFRAME = "4h"

MARKET_FLOW_TIMEFRAME_MS = (
    4 * 60 * 60 * 1000
)

# Esperamos que termine de llegar el batch
# antes de calcular el ranking global.
MARKET_FLOW_SETTLE_SECONDS = 15

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
        chunk_size=15,
        stale_after=90,
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

        self.market_flow_batches = {}

        self.last_market_flow_timestamp = None

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

        self.stop_event = threading.Event()

    def run(self):
        if not self.publisher.ping():
            raise RuntimeError(
                "Redis connection failed"
            )

        if not self._acquire_producer_lock():
            raise RuntimeError(
                "Another market data producer is active"
            )

        self.running = True
        self.stop_event.clear()

        self._start_heartbeat()

        try:
            self.phase = "LOADING_HISTORY"
            self._write_status()

            self._load_all_history()
            
            self._publish_initial_market_flow()

            self.phase = "CONNECTING_WS"
            self._write_status()

            self._start_websocket()

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
                if (
                    self.ws
                    and self.ws.is_connected
                ):
                    self.phase = "READY"
                else:
                    self.phase = "CONNECTING_WS"
                    
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
                days = DAYS_BY_TF.get(
                    timeframe,
                    3,
                )

                dataframe = fetch_history(
                    symbol,
                    timeframe,
                    days,
                )

                candles = dataframe.to_dict(
                    orient="records",
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
    ):
        if (
            self.market_sector_flow_analyzer
            is None
        ):
            enriched_snapshot = dict(
                snapshot
            )

            enriched_snapshot[
                "sector_context_available"
            ] = False

            enriched_snapshot[
                "sector_context_error"
            ] = (
                self.market_sector_catalog_error
                or "sector_analyzer_unavailable"
            )

            return enriched_snapshot

        try:
            enriched_snapshot = (
                self.market_sector_flow_analyzer
                .enrich_snapshot(
                    snapshot
                )
            )

        except Exception as exc:
            error = (
                f"{type(exc).__name__}:"
                f"{exc}"
            )

            print(
                "[MARKET SECTORS] "
                "snapshot enrichment failed "
                f"error={error}"
            )

            enriched_snapshot = dict(
                snapshot
            )

            enriched_snapshot[
                "sector_context_available"
            ] = False

            enriched_snapshot[
                "sector_context_error"
            ] = error

            return enriched_snapshot

        enriched_snapshot[
            "sector_context_error"
        ] = None

        print(
            "[MARKET SECTORS] "
            "snapshot enriched "
            f"timestamp="
            f"{enriched_snapshot.get('candle_timestamp')} "
            f"sectors="
            f"{enriched_snapshot.get('sector_count')} "
            f"excluded="
            f"{len(enriched_snapshot.get('excluded_sectors', {}))}"
        )

        return enriched_snapshot
        
    def _publish_initial_market_flow(
        self,
    ):
        print(
            "[MARKET FLOW] "
            "building initial snapshot"
        )

        raw_btc_history = (
            self.publisher.redis.lrange(
                history_key(
                    "BTCUSDT",
                    MARKET_FLOW_TIMEFRAME,
                ),
                -3,
                -1,
            )
        )

        if not raw_btc_history:
            print(
                "[MARKET FLOW] "
                "initial snapshot skipped: "
                "BTC 4h history unavailable"
            )
            return

        now_ms = int(
            time.time() * 1000
        )

        candle_timestamp = None

        for raw_candle in reversed(
            raw_btc_history
        ):
            try:
                candle = json.loads(
                    raw_candle
                )

                candidate_timestamp = int(
                    candle["timestamp"]
                )

            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue

            candidate_close_timestamp = (
                candidate_timestamp
                + MARKET_FLOW_TIMEFRAME_MS
            )

            if (
                candidate_close_timestamp
                <= now_ms
            ):
                candle_timestamp = (
                    candidate_timestamp
                )
                break

        if candle_timestamp is None:
            print(
                "[MARKET FLOW] "
                "initial snapshot skipped: "
                "no fully closed BTC 4h candle"
            )
            return

        try:
            snapshot = (
                self.market_flow_analyzer.calculate(
                    symbols=self.symbols,
                    timeframe=(
                        MARKET_FLOW_TIMEFRAME
                    ),
                    candle_timestamp=(
                        candle_timestamp
                    ),
                )
            )

        except Exception as exc:
            print(
                "[MARKET FLOW] "
                "initial calculation failed "
                f"error={exc}"
            )
            return

        coverage_pct = float(
            snapshot.get(
                "coverage_pct",
                0.0,
            )
        )

        if (
            coverage_pct
            < MARKET_FLOW_MIN_COVERAGE_PCT
        ):
            print(
                "[MARKET FLOW] "
                "initial snapshot rejected "
                f"timestamp={candle_timestamp} "
                f"coverage={coverage_pct:.2f}% "
                f"minimum="
                f"{MARKET_FLOW_MIN_COVERAGE_PCT:.2f}%"
            )
            return
        
        snapshot = (
            self._enrich_market_flow_snapshot(
                snapshot
            )
        )

        publication = (
            self.publisher
            .publish_market_flow_snapshot(
                timeframe=(
                    MARKET_FLOW_TIMEFRAME
                ),
                snapshot=snapshot,
            )
        )

        self.last_market_flow_timestamp = (
            candle_timestamp
        )

        print(
            "[MARKET FLOW] "
            "initial snapshot published "
            f"key={publication['key']} "
            f"timestamp={candle_timestamp} "
            f"valid="
            f"{snapshot['valid_universe_size']}/"
            f"{snapshot['configured_universe_size']} "
            f"coverage={coverage_pct:.2f}% "
            f"breadth="
            f"{snapshot['market_breadth_4h']}"
        )
        
    def _on_ws_message(self, message):
        result = (
            self.publisher.publish_ws_message(
                message
            )
        )

        if not isinstance(result, dict):
            return result

        if (
            result.get("type")
            != "closed_candle"
        ):
            return result

        timeframe = result.get(
            "timeframe"
        )

        if (
            timeframe
            != MARKET_FLOW_TIMEFRAME
        ):
            return result

        symbol = result.get("symbol")
        candle_timestamp = result.get(
            "timestamp"
        )

        if (
            not symbol
            or candle_timestamp is None
        ):
            return result

        self._register_market_flow_close(
            symbol=symbol,
            candle_timestamp=(
                candle_timestamp
            ),
        )

        return result
    
    def _register_market_flow_close(
        self,
        symbol,
        candle_timestamp,
    ):
        candle_timestamp = int(
            candle_timestamp
        )

        now = time.monotonic()

        with self.market_flow_lock:
            batch = (
                self.market_flow_batches.setdefault(
                    candle_timestamp,
                    {
                        "symbols": set(),
                        "first_seen_at": now,
                        "last_seen_at": now,
                    },
                )
            )

            batch["symbols"].add(
                symbol
            )

            batch["last_seen_at"] = now
            
    def _repair_market_flow_candles(
        self,
        candle_timestamp,
        missing_symbols,
    ):
        candle_timestamp = int(
            candle_timestamp
        )

        missing_symbols = sorted(
            set(missing_symbols)
        )

        recovered_symbols = []
        failed_symbols = {}

        print(
            "[MARKET FLOW REPAIR] "
            f"starting timestamp={candle_timestamp} "
            f"missing={len(missing_symbols)}"
        )

        for symbol in missing_symbols:
            if not self.running:
                failed_symbols[symbol] = (
                    "service_stopping"
                )
                continue

            candle = None
            last_error = None

            for attempt in range(1, 4):
                try:
                    candle = (
                        fetch_closed_futures_candle(
                            symbol=symbol,
                            timeframe=(
                                MARKET_FLOW_TIMEFRAME
                            ),
                            candle_timestamp=(
                                candle_timestamp
                            ),
                        )
                    )

                    if candle is None:
                        last_error = (
                            "candle_unavailable"
                        )
                    else:
                        break

                except Exception as exc:
                    last_error = str(exc)

                if attempt < 3:
                    if self.stop_event.wait(1):
                        last_error = (
                            "service_stopping"
                        )
                        break

            if candle is None:
                failed_symbols[symbol] = (
                    last_error
                    or "candle_unavailable"
                )

                print(
                    "[MARKET FLOW REPAIR] "
                    f"failed symbol={symbol} "
                    f"timestamp={candle_timestamp} "
                    f"error={failed_symbols[symbol]}"
                )

                continue

            try:
                publication = (
                    self.publisher
                    .publish_recovered_candle(
                        symbol=symbol,
                        timeframe=(
                            MARKET_FLOW_TIMEFRAME
                        ),
                        candle=candle,
                    )
                )

            except Exception as exc:
                failed_symbols[symbol] = (
                    f"publish_failed:{exc}"
                )

                print(
                    "[MARKET FLOW REPAIR] "
                    f"failed symbol={symbol} "
                    f"timestamp={candle_timestamp} "
                    f"error={failed_symbols[symbol]}"
                )

                continue

            recovered_symbols.append(
                symbol
            )

            if (
                int(publication["timestamp"])
                != candle_timestamp
            ):
                failed_symbols[symbol] = (
                    "published_timestamp_mismatch"
                )

                recovered_symbols.remove(
                    symbol
                )

        print(
            "[MARKET FLOW REPAIR] "
            f"completed timestamp={candle_timestamp} "
            f"requested={len(missing_symbols)} "
            f"recovered={len(recovered_symbols)} "
            f"failed={len(failed_symbols)}"
        )

        return {
            "requested": len(missing_symbols),
            "recovered": len(recovered_symbols),
            "failed": len(failed_symbols),
            "recovered_symbols": (
                recovered_symbols
            ),
            "failed_symbols": (
                failed_symbols
            ),
        }

    def _maybe_publish_market_flow(
        self,
    ):
        now = time.monotonic()

        expected_symbols = len(
            set(self.symbols)
        )

        batch_to_process = None

        with self.market_flow_lock:
            for candle_timestamp in sorted(
                self.market_flow_batches
            ):
                batch = (
                    self.market_flow_batches[
                        candle_timestamp
                    ]
                )

                received_symbols = len(
                    batch["symbols"]
                )

                age_seconds = (
                    now
                    - batch["first_seen_at"]
                )

                quiet_seconds = (
                    now
                    - batch["last_seen_at"]
                )

                complete = (
                    received_symbols
                    >= expected_symbols
                )

                settled = (
                    age_seconds
                    >= MARKET_FLOW_SETTLE_SECONDS
                    and quiet_seconds >= 3
                )

                timed_out = (
                    age_seconds
                    >= MARKET_FLOW_MAX_WAIT_SECONDS
                )

                if (
                    complete
                    or settled
                    or timed_out
                ):
                    batch_to_process = {
                        "candle_timestamp": (
                            candle_timestamp
                        ),
                        "received_symbols": (
                            received_symbols
                        ),
                        "expected_symbols": (
                            expected_symbols
                        ),
                        "age_seconds": (
                            age_seconds
                        ),
                    }

                    del self.market_flow_batches[
                        candle_timestamp
                    ]

                    break

        if batch_to_process is None:
            return

        candle_timestamp = (
            batch_to_process[
                "candle_timestamp"
            ]
        )

        if (
            self.last_market_flow_timestamp
            == candle_timestamp
        ):
            return

        print(
            "[MARKET FLOW] "
            f"calculating tf="
            f"{MARKET_FLOW_TIMEFRAME} "
            f"timestamp={candle_timestamp} "
            f"received="
            f"{batch_to_process['received_symbols']}/"
            f"{batch_to_process['expected_symbols']} "
            f"waited="
            f"{batch_to_process['age_seconds']:.1f}s"
        )

        try:
            snapshot = (
                self.market_flow_analyzer.calculate(
                    symbols=self.symbols,
                    timeframe=(
                        MARKET_FLOW_TIMEFRAME
                    ),
                    candle_timestamp=(
                        candle_timestamp
                    ),
                )
            )

        except Exception as exc:
            print(
                "[MARKET FLOW] "
                f"calculation failed "
                f"timestamp={candle_timestamp} "
                f"error={exc}"
            )
            return
        
        missing_symbols = [
            symbol
            for symbol, reason
            in snapshot.get(
                "excluded_symbols",
                {},
            ).items()
            if reason == "missing_batch_candle"
        ]

        if missing_symbols:
            repair_result = (
                self._repair_market_flow_candles(
                    candle_timestamp=(
                        candle_timestamp
                    ),
                    missing_symbols=(
                        missing_symbols
                    ),
                )
            )

            if repair_result["recovered"] > 0:
                try:
                    snapshot = (
                        self.market_flow_analyzer
                        .calculate(
                            symbols=self.symbols,
                            timeframe=(
                                MARKET_FLOW_TIMEFRAME
                            ),
                            candle_timestamp=(
                                candle_timestamp
                            ),
                        )
                    )

                except Exception as exc:
                    print(
                        "[MARKET FLOW] "
                        "recalculation failed "
                        f"timestamp={candle_timestamp} "
                        f"error={exc}"
                    )
                    return

        coverage_pct = float(
            snapshot.get(
                "coverage_pct",
                0.0,
            )
        )

        if (
            coverage_pct
            < MARKET_FLOW_MIN_COVERAGE_PCT
        ):
            print(
                "[MARKET FLOW] "
                "snapshot rejected "
                f"timestamp={candle_timestamp} "
                f"coverage={coverage_pct:.2f}% "
                f"minimum="
                f"{MARKET_FLOW_MIN_COVERAGE_PCT:.2f}%"
            )
            return
        
        snapshot = (
            self._enrich_market_flow_snapshot(
                snapshot
            )
        )

        publication = (
            self.publisher
            .publish_market_flow_snapshot(
                timeframe=(
                    MARKET_FLOW_TIMEFRAME
                ),
                snapshot=snapshot,
            )
        )

        self.last_market_flow_timestamp = (
            candle_timestamp
        )

        print(
            "[MARKET FLOW] "
            "published "
            f"key={publication['key']} "
            f"timestamp={candle_timestamp} "
            f"valid="
            f"{snapshot['valid_universe_size']}/"
            f"{snapshot['configured_universe_size']} "
            f"coverage={coverage_pct:.2f}% "
            f"breadth="
            f"{snapshot['market_breadth_4h']}"
        )

    def _start_websocket(self):
        self.ws = WSClient(
            self._on_ws_message,
            timeframes=self.timeframes,
            symbols=self.symbols,
            chunk_size=self.chunk_size,
            stale_after=self.stale_after,
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

    return parser.parse_args()


def main():
    args = parse_args()

    service = MarketDataService(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
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