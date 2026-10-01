import argparse
import pickle
import time

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


from config.strategies.v1 import SYMBOLS
from config.timeframes import MODE_CONFIG, MAIN_TF


from data.market_data import (
    fetch_closed_history_before,
    fetch_futures_klines_range,
)


from engine.live.data.redis_market_data_publisher import (
    RedisMarketDataPublisher,
)


from engine.replay.historical_replay_service import (
    HistoricalReplayService,
)


# =========================================================
# CONFIG
# =========================================================

STRATEGY_MODE = "compression"


TIMEFRAMES = MODE_CONFIG[
    STRATEGY_MODE
]["timeframes"]


# IMPORTANT:
#
# Keep this synchronized with engine/live/main.py.
#
# Do NOT make this depend on the experimental compression
# lookback/base variables yet, because the replay barrier
# consumer name must match exactly on runner + engine.
CONSUMER_NAME = (
    f"lookback-10-base-superpuesta-main-tf-{MAIN_TF}"
)


TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


# =========================================================
# REPLAY CACHE
# =========================================================

REPLAY_CACHE_VERSION = "v1"


def _cache_path(
    cache_dir,
    kind,
    symbol,
    timeframe,
    start_ms,
    end_ms,
):
    """
    Creates a deterministic cache path.

    Different:
    - symbols
    - timeframes
    - replay ranges
    - cache versions

    never share the same file.
    """

    return (
        Path(cache_dir)
        / REPLAY_CACHE_VERSION
        / kind
        / (
            f"{symbol}_"
            f"{timeframe}_"
            f"{int(start_ms)}_"
            f"{int(end_ms)}.pkl"
        )
    )


def _load_cache_file(path):
    """
    Returns:
        (data, cache_hit)
    """

    if not path.exists():
        return None, False

    try:
        with open(path, "rb") as f:
            data = pickle.load(f)

        if not isinstance(data, list):
            raise TypeError(
                "cached candle payload is not a list"
            )

        return data, True

    except Exception as exc:
        print(
            "[REPLAY CACHE] invalid cache | "
            f"path={path} | "
            f"error={exc}"
        )

        # Corrupted cache must never contaminate replay.
        try:
            path.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        return None, False


def _save_cache_file(
    path,
    data,
):
    """
    Atomic-ish local cache write.

    We first write to .tmp and only then replace
    the final file.
    """

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with open(tmp_path, "wb") as f:
        pickle.dump(
            data,
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    tmp_path.replace(path)


# =========================================================
# TIME HELPERS
# =========================================================

def parse_utc(
    value: str,
) -> int:
    """
    Accepts:

        2026-09-10
        2026-09-10T00:00
        2026-09-10T00:00:00

    Always interpreted as UTC.
    """

    value = value.strip()

    if len(value) == 10:
        value += "T00:00:00"

    dt = datetime.fromisoformat(
        value
    )

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    else:
        dt = dt.astimezone(
            timezone.utc
        )

    return int(
        dt.timestamp() * 1000
    )


def format_ts(
    timestamp_ms: int,
) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


def format_duration(
    seconds,
):
    seconds = max(
        0,
        int(seconds),
    )

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    if hours:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{seconds:02d}"
    )


# =========================================================
# HISTORICAL DOWNLOAD + CACHE
# =========================================================

def load_warmup(
    publisher,
    symbol,
    timeframe,
    replay_start_ms,
    cache_dir,
    refresh_cache=False,
):
    """
    IMPORTANT:
    Preserve the exact existing warmup semantics.

    We still use fetch_closed_history_before().
    The only optimization is storing/reusing its result.
    """

    cache_path = _cache_path(
        cache_dir=cache_dir,
        kind="warmup",
        symbol=symbol,
        timeframe=timeframe,
        start_ms=0,
        end_ms=replay_start_ms - 1,
    )

    candles = None
    cache_hit = False

    if not refresh_cache:
        candles, cache_hit = (
            _load_cache_file(
                cache_path
            )
        )

    if not cache_hit:
        candles = (
            fetch_closed_history_before(
                symbol=symbol,
                timeframe=timeframe,
                cutoff_ms=replay_start_ms,
            )
        )

        _save_cache_file(
            cache_path,
            candles,
        )

    # The Redis history is rebuilt on every replay.
    #
    # We cache Binance/download work,
    # NOT replay state.
    publisher.replace_history(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
    )

    return (
        len(candles),
        cache_hit,
    )


def load_replay_candles(
    symbol,
    timeframe,
    replay_start_ms,
    replay_end_ms,
    cache_dir,
    refresh_cache=False,
):
    tf_ms = TF_MS[
        timeframe
    ]

    # Go one TF backwards so that if replay starts
    # in the middle of a native candle, we still
    # capture that candle when it eventually closes.
    fetch_start_ms = max(
        0,
        replay_start_ms - tf_ms,
    )

    cache_path = _cache_path(
        cache_dir=cache_dir,
        kind="replay",
        symbol=symbol,
        timeframe=timeframe,
        start_ms=fetch_start_ms,
        end_ms=replay_end_ms,
    )

    candles = None
    cache_hit = False

    if not refresh_cache:
        candles, cache_hit = (
            _load_cache_file(
                cache_path
            )
        )

    if not cache_hit:
        candles = (
            fetch_futures_klines_range(
                symbol=symbol,
                timeframe=timeframe,
                start_ms=fetch_start_ms,
                end_ms=replay_end_ms,
            )
        )

        _save_cache_file(
            cache_path,
            candles,
        )

    # Replay is driven by candle CLOSE time.
    #
    # Keep this filtering even with cache.
    # It preserves the original replay semantics.
    candles = [
        candle
        for candle in candles
        if (
            replay_start_ms
            <= int(
                candle[
                    "close_timestamp"
                ]
            )
            <= replay_end_ms
        )
    ]

    return (
        candles,
        cache_hit,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Historical market replay "
            "through Redis."
        )
    )


    parser.add_argument(
        "--start",
        required=True,
        help=(
            "Replay start UTC. "
            "Example: 2026-09-10T00:00:00"
        ),
    )


    parser.add_argument(
        "--end",
        required=True,
        help=(
            "Replay end UTC. "
            "Example: 2026-09-10T06:00:00"
        ),
    )


    parser.add_argument(
        "--redis-host",
        default="127.0.0.1",
    )


    parser.add_argument(
        "--redis-port",
        type=int,
        default=6380,
    )


    parser.add_argument(
        "--redis-db",
        type=int,
        default=0,
    )


    parser.add_argument(
        "--barrier-timeout",
        type=float,
        default=120.0,
    )


    parser.add_argument(
        "--cache-dir",
        default="replay_cache",
        help=(
            "Local directory used for "
            "historical replay cache."
        ),
    )


    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help=(
            "Ignore existing cache and "
            "download historical candles again."
        ),
    )


    args = parser.parse_args()


    replay_start_ms = parse_utc(
        args.start
    )


    replay_end_ms = parse_utc(
        args.end
    )


    if (
        replay_end_ms
        <= replay_start_ms
    ):
        raise ValueError(
            "--end must be after --start"
        )


    run_started = (
        time.perf_counter()
    )


    print()
    print("=" * 70)
    print("HISTORICAL REPLAY")
    print("=" * 70)


    print(
        f"strategy={STRATEGY_MODE}"
    )


    print(
        f"consumer={CONSUMER_NAME}"
    )


    print(
        f"redis="
        f"{args.redis_host}:"
        f"{args.redis_port}/"
        f"{args.redis_db}"
    )


    print(
        f"start="
        f"{format_ts(replay_start_ms)} UTC"
    )


    print(
        f"end="
        f"{format_ts(replay_end_ms)} UTC"
    )


    print(
        f"symbols={len(SYMBOLS)}"
    )


    print(
        f"timeframes={TIMEFRAMES}"
    )


    print(
        f"cache_dir="
        f"{Path(args.cache_dir).resolve()}"
    )


    print(
        f"refresh_cache="
        f"{args.refresh_cache}"
    )


    print("=" * 70)
    print()


    publisher = (
        RedisMarketDataPublisher(
            host=args.redis_host,
            port=args.redis_port,
            db=args.redis_db,
        )
    )


    service = (
        HistoricalReplayService(
            host=args.redis_host,
            port=args.redis_port,
            db=args.redis_db,
        )
    )


    publisher.ping()


    # -----------------------------------------------------
    # IMPORTANT:
    #
    # FLUSHDB IS INTENTIONALLY NOT DONE HERE.
    #
    # Replay Redis must be cleaned manually before starting.
    #
    # This prevents any possibility of accidentally
    # flushing the live Redis instance.
    # -----------------------------------------------------

    service.clear_consumer_barrier(
        CONSUMER_NAME
    )


    # =====================================================
    # 1. LOAD + INSTALL WARMUP
    # =====================================================

    warmup_started = (
        time.perf_counter()
    )


    print(
        "[REPLAY] Loading warmup histories..."
    )


    total_warmup = 0

    warmup_cache_hits = 0
    warmup_cache_misses = 0


    for (
        symbol_index,
        symbol,
    ) in enumerate(
        SYMBOLS,
        start=1,
    ):
        print(
            f"[WARMUP] "
            f"{symbol_index}/"
            f"{len(SYMBOLS)} "
            f"{symbol}"
        )


        for timeframe in TIMEFRAMES:
            try:
                (
                    count,
                    cache_hit,
                ) = load_warmup(
                    publisher=publisher,
                    symbol=symbol,
                    timeframe=timeframe,
                    replay_start_ms=(
                        replay_start_ms
                    ),
                    cache_dir=(
                        args.cache_dir
                    ),
                    refresh_cache=(
                        args.refresh_cache
                    ),
                )


                total_warmup += count


                if cache_hit:
                    warmup_cache_hits += 1
                    cache_label = "HIT"

                else:
                    warmup_cache_misses += 1
                    cache_label = "MISS"


                print(
                    f"    "
                    f"{timeframe:<3} "
                    f"candles={count} "
                    f"cache={cache_label}"
                )


            except Exception as exc:
                print(
                    f"    "
                    f"{timeframe:<3} "
                    f"ERROR={exc}"
                )

                raise


    warmup_elapsed = (
        time.perf_counter()
        - warmup_started
    )


    print()

    print(
        f"[REPLAY] Warmup ready | "
        f"candles={total_warmup} | "
        f"cache_hits="
        f"{warmup_cache_hits} | "
        f"cache_misses="
        f"{warmup_cache_misses} | "
        f"elapsed="
        f"{format_duration(warmup_elapsed)}"
    )

    print()


    # =====================================================
    # 2. LOAD REPLAY DATA
    # =====================================================

    replay_data_started = (
        time.perf_counter()
    )


    print(
        "[REPLAY] Loading replay candles..."
    )


    events_by_close = (
        defaultdict(list)
    )


    total_replay_candles = 0

    symbols_with_1m = set()


    replay_cache_hits = 0
    replay_cache_misses = 0


    for (
        symbol_index,
        symbol,
    ) in enumerate(
        SYMBOLS,
        start=1,
    ):
        print(
            f"[DATA] "
            f"{symbol_index}/"
            f"{len(SYMBOLS)} "
            f"{symbol}"
        )


        for timeframe in TIMEFRAMES:
            try:
                (
                    candles,
                    cache_hit,
                ) = load_replay_candles(
                    symbol=symbol,
                    timeframe=timeframe,
                    replay_start_ms=(
                        replay_start_ms
                    ),
                    replay_end_ms=(
                        replay_end_ms
                    ),
                    cache_dir=(
                        args.cache_dir
                    ),
                    refresh_cache=(
                        args.refresh_cache
                    ),
                )


            except Exception as exc:
                print(
                    f"    "
                    f"{timeframe:<3} "
                    f"ERROR={exc}"
                )

                raise


            if cache_hit:
                replay_cache_hits += 1
                cache_label = "HIT"

            else:
                replay_cache_misses += 1
                cache_label = "MISS"


            print(
                f"    "
                f"{timeframe:<3} "
                f"candles="
                f"{len(candles)} "
                f"cache="
                f"{cache_label}"
            )


            if (
                timeframe == "1m"
                and candles
            ):
                symbols_with_1m.add(
                    symbol
                )


            for candle in candles:
                close_timestamp = int(
                    candle[
                        "close_timestamp"
                    ]
                )


                events_by_close[
                    close_timestamp
                ].append(
                    (
                        symbol,
                        timeframe,
                        candle,
                    )
                )


                total_replay_candles += 1


    replay_data_elapsed = (
        time.perf_counter()
        - replay_data_started
    )


    print()

    print(
        f"[REPLAY] Historical data ready | "
        f"candles="
        f"{total_replay_candles} | "
        f"boundaries="
        f"{len(events_by_close)} | "
        f"symbols_1m="
        f"{len(symbols_with_1m)} | "
        f"cache_hits="
        f"{replay_cache_hits} | "
        f"cache_misses="
        f"{replay_cache_misses} | "
        f"elapsed="
        f"{format_duration(replay_data_elapsed)}"
    )


    if not events_by_close:
        raise RuntimeError(
            "No replay candles found"
        )


    # =====================================================
    # PRE-SORT BOUNDARY EVENTS
    # =====================================================

    # Do the deterministic sorting once,
    # before starting the clock.

    for events in (
        events_by_close.values()
    ):
        events.sort(
            key=lambda item: (
                item[0],
                TF_MS[item[1]],
            )
        )


    # =====================================================
    # 3. MASTER CLOCK
    # =====================================================

    #
    # The master clock is ONLY 1m candle closes.
    #
    # Higher TF candles are published at the same boundary
    # when their native close_timestamp matches that 1m
    # close.
    #

    boundaries = sorted(
        close_timestamp
        for (
            close_timestamp,
            events,
        )
        in events_by_close.items()
        if any(
            timeframe == "1m"
            for _, timeframe, _
            in events
        )
    )


    if not boundaries:
        raise RuntimeError(
            "No 1m boundaries found"
        )


    first_boundary = (
        boundaries[0]
    )


    print()

    print(
        f"[REPLAY] First boundary: "
        f"{format_ts(first_boundary)} UTC"
    )


    print(
        f"[REPLAY] Last boundary:  "
        f"{format_ts(boundaries[-1])} UTC"
    )


    print(
        f"[REPLAY] Total boundaries: "
        f"{len(boundaries)}"
    )


    print()

    print(
        "[REPLAY] Initializing replay service..."
    )


    # This is the point where replay becomes READY.
    #
    # engine/live/main.py may now finish load_history()
    # and start consuming the replay stream.
    service.initialize(
        first_boundary
    )


    print(
        "[REPLAY] Service READY"
    )


    print(
        "[REPLAY] Waiting for consumer startup..."
    )


    service.wait_consumer_ready(
        consumer_name=CONSUMER_NAME,
        timeout_seconds=(
            args.barrier_timeout
        ),
    )


    print(
        "[REPLAY] Consumer READY | "
        f"consumer={CONSUMER_NAME}"
    )


    print(
        "[REPLAY] Starting historical clock..."
    )

    print()


    # =====================================================
    # 4. REPLAY LOOP
    # =====================================================

    clock_started = (
        time.perf_counter()
    )


    try:
        for (
            boundary_index,
            boundary_ts,
        ) in enumerate(
            boundaries,
            start=1,
        ):

            # ---------------------------------------------
            # Move market clock first.
            # ---------------------------------------------

            service.set_market_time(
                boundary_ts
            )


            events = events_by_close[
                boundary_ts
            ]


            # Events were already deterministically sorted
            # before starting the replay clock.


            publish_started = (
                time.perf_counter()
            )

            result = (
                publisher
                .publish_historical_boundary(
                    events=events,
                    boundary_ts=boundary_ts,
                )
            )

            publish_elapsed = (
                time.perf_counter()
                - publish_started
            )

            last_stream_id = result[
                "stream_id"
            ]

            if last_stream_id is None:
                raise RuntimeError(
                    "Boundary without "
                    "published events"
                )

            if last_stream_id is None:
                raise RuntimeError(
                    "Boundary without "
                    "published events"
                )


            # ---------------------------------------------
            # BARRIER 1
            #
            # Provider must have applied every stream event
            # belonging to this market boundary.
            # ---------------------------------------------

            service.wait_provider_applied(
                consumer_name=CONSUMER_NAME,
                expected_stream_id=(
                    last_stream_id
                ),
                timeout_seconds=(
                    args.barrier_timeout
                ),
            )


            # ---------------------------------------------
            # Tell engine that this boundary is complete.
            #
            # It can now safely process it.
            # ---------------------------------------------

            service.publish_boundary_ready(
                timestamp_ms=boundary_ts,
                end_stream_id=(
                    last_stream_id
                ),
            )


            # ---------------------------------------------
            # BARRIER 2
            #
            # Engine must finish the ENTIRE strategy cycle
            # before historical time can advance.
            # ---------------------------------------------

            service.wait_engine_processed(
                consumer_name=CONSUMER_NAME,
                expected_timestamp=(
                    boundary_ts
                ),
                timeout_seconds=(
                    args.barrier_timeout
                ),
            )


            # ---------------------------------------------
            # Progress + speed + ETA
            # ---------------------------------------------

            if (
                boundary_index == 1
                or boundary_index % 30 == 0
                or boundary_index
                == len(boundaries)
            ):
                pct = (
                    boundary_index
                    / len(boundaries)
                    * 100
                )


                elapsed = (
                    time.perf_counter()
                    - clock_started
                )


                boundaries_per_second = (
                    boundary_index / elapsed
                    if elapsed > 0
                    else 0
                )


                remaining = (
                    len(boundaries)
                    - boundary_index
                )


                eta_seconds = (
                    remaining
                    / boundaries_per_second
                    if boundaries_per_second > 0
                    else 0
                )


                print(
                    f"[REPLAY CLOCK] "
                    f"{boundary_index}/"
                    f"{len(boundaries)} "
                    f"({pct:.1f}%) | "
                    f"{format_ts(boundary_ts)} UTC | "
                    f"events={len(events)} | "
                    f"stream={last_stream_id} | "
                    f"publish_ms={publish_elapsed * 1000:.1f} | "
                    f"elapsed="
                    f"{format_duration(elapsed)} | "
                    f"speed="
                    f"{boundaries_per_second:.2f} "
                    f"boundaries/s | "
                    f"ETA="
                    f"{format_duration(eta_seconds)}"
                )


    except KeyboardInterrupt:
        print()
        print(
            "[REPLAY] Interrupted by user"
        )


    finally:
        service.stop()


    clock_elapsed = (
        time.perf_counter()
        - clock_started
    )


    total_elapsed = (
        time.perf_counter()
        - run_started
    )


    print()
    print("=" * 70)
    print("REPLAY FINISHED")
    print("=" * 70)


    print(
        f"warmup_elapsed="
        f"{format_duration(warmup_elapsed)}"
    )


    print(
        f"replay_data_elapsed="
        f"{format_duration(replay_data_elapsed)}"
    )


    print(
        f"clock_elapsed="
        f"{format_duration(clock_elapsed)}"
    )


    print(
        f"total_elapsed="
        f"{format_duration(total_elapsed)}"
    )


    print(
        f"warmup_cache="
        f"{warmup_cache_hits} HIT / "
        f"{warmup_cache_misses} MISS"
    )


    print(
        f"replay_cache="
        f"{replay_cache_hits} HIT / "
        f"{replay_cache_misses} MISS"
    )


    print("=" * 70)
    print()


if __name__ == "__main__":
    main()