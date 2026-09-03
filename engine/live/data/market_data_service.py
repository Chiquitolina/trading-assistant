import argparse
import json
import signal
import threading
import time
import uuid

from config.strategies.v1 import SYMBOLS
from config.timeframes import MODE_CONFIG
from data.market_data import fetch_history
from engine.live.data.redis_market_data_protocol import (
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    PRODUCER_LOCK_KEY,
    PRODUCER_LOCK_TTL_SECONDS,
    STATUS_KEY,
)
from engine.live.data.redis_market_data_publisher import (
    RedisMarketDataPublisher,
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

    def _start_websocket(self):
        self.ws = WSClient(
            self.publisher.publish_ws_message,
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