import argparse
from collections import defaultdict
from datetime import datetime, timezone

from config.strategies.v1 import SYMBOLS
from config.timeframes import MODE_CONFIG

from data.market_data import fetch_futures_klines_range

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

CONSUMER_NAME = (
    "lookback-10-base-superpuesta-main-tf-30m"
)

DAYS_BY_TF = {
    "1m": 1,
    "5m": 2,
    "15m": 3,
    "30m": 5,
    "1h": 7,
    "4h": 25,
    "1d": 180,
}

DAY_MS = 86_400_000

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
# TIME HELPERS
# =========================================================

def parse_utc(value: str) -> int:
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

    dt = datetime.fromisoformat(value)

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


def format_ts(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


# =========================================================
# HISTORICAL DOWNLOAD
# =========================================================

def load_warmup(
    publisher,
    symbol,
    timeframe,
    replay_start_ms,
):
    days = DAYS_BY_TF[timeframe]

    warmup_start_ms = (
        replay_start_ms
        - days * DAY_MS
    )

    warmup_end_ms = (
        replay_start_ms - 1
    )

    candles = fetch_futures_klines_range(
        symbol=symbol,
        timeframe=timeframe,
        start_ms=warmup_start_ms,
        end_ms=warmup_end_ms,
    )

    # CRITICAL:
    # only candles fully closed before replay starts.
    #
    # Never allow a candle that was still open at
    # replay_start_ms into the initial history.
    candles = [
        candle
        for candle in candles
        if int(
            candle["close_timestamp"]
        ) < replay_start_ms
    ]

    publisher.replace_history(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
    )

    return len(candles)


def load_replay_candles(
    symbol,
    timeframe,
    replay_start_ms,
    replay_end_ms,
):
    tf_ms = TF_MS[timeframe]

    # Go one TF backwards so that if replay starts
    # in the middle of a native candle, we still
    # capture that candle when it eventually closes.
    fetch_start_ms = max(
        0,
        replay_start_ms - tf_ms,
    )

    candles = fetch_futures_klines_range(
        symbol=symbol,
        timeframe=timeframe,
        start_ms=fetch_start_ms,
        end_ms=replay_end_ms,
    )

    # Replay is driven by candle CLOSE time.
    candles = [
        candle
        for candle in candles
        if (
            replay_start_ms
            <= int(
                candle["close_timestamp"]
            )
            <= replay_end_ms
        )
    ]

    return candles


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

    args = parser.parse_args()

    replay_start_ms = parse_utc(
        args.start
    )

    replay_end_ms = parse_utc(
        args.end
    )

    if replay_end_ms <= replay_start_ms:
        raise ValueError(
            "--end must be after --start"
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

    print("=" * 70)
    print()

    publisher = RedisMarketDataPublisher(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
    )

    service = HistoricalReplayService(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
    )

    publisher.ping()

    # -----------------------------------------------------
    # IMPORTANT:
    #
    # FLUSHDB IS INTENTIONALLY NOT DONE HERE.
    #
    # Replay Redis must be cleaned manually before starting.
    # This prevents any possibility of accidentally flushing
    # the live Redis instance.
    # -----------------------------------------------------

    service.clear_consumer_barrier(
        CONSUMER_NAME
    )

    # =====================================================
    # 1. DOWNLOAD + INSTALL WARMUP
    # =====================================================

    print(
        "[REPLAY] Loading warmup histories..."
    )

    total_warmup = 0

    for symbol_index, symbol in enumerate(
        SYMBOLS,
        start=1,
    ):
        print(
            f"[WARMUP] "
            f"{symbol_index}/{len(SYMBOLS)} "
            f"{symbol}"
        )

        for timeframe in TIMEFRAMES:
            try:
                count = load_warmup(
                    publisher=publisher,
                    symbol=symbol,
                    timeframe=timeframe,
                    replay_start_ms=(
                        replay_start_ms
                    ),
                )

                total_warmup += count

                print(
                    f"    {timeframe:<3} "
                    f"candles={count}"
                )

            except Exception as exc:
                print(
                    f"    {timeframe:<3} "
                    f"ERROR={exc}"
                )

                raise

    print()
    print(
        f"[REPLAY] Warmup ready | "
        f"candles={total_warmup}"
    )
    print()

    # =====================================================
    # 2. DOWNLOAD REPLAY DATA
    # =====================================================

    print(
        "[REPLAY] Downloading replay candles..."
    )

    events_by_close = defaultdict(
        list
    )

    total_replay_candles = 0

    symbols_with_1m = set()

    for symbol_index, symbol in enumerate(
        SYMBOLS,
        start=1,
    ):
        print(
            f"[DATA] "
            f"{symbol_index}/{len(SYMBOLS)} "
            f"{symbol}"
        )

        for timeframe in TIMEFRAMES:
            try:
                candles = load_replay_candles(
                    symbol=symbol,
                    timeframe=timeframe,
                    replay_start_ms=(
                        replay_start_ms
                    ),
                    replay_end_ms=(
                        replay_end_ms
                    ),
                )

            except Exception as exc:
                print(
                    f"    {timeframe:<3} "
                    f"ERROR={exc}"
                )
                raise

            print(
                f"    {timeframe:<3} "
                f"candles={len(candles)}"
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

    print()
    print(
        f"[REPLAY] Historical data ready | "
        f"candles={total_replay_candles} | "
        f"boundaries={len(events_by_close)} | "
        f"symbols_1m={len(symbols_with_1m)}"
    )

    if not events_by_close:
        raise RuntimeError(
            "No replay candles found"
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
        for close_timestamp, events
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

    first_boundary = boundaries[0]

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
        timeout_seconds=args.barrier_timeout,
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

    try:
        for boundary_index, boundary_ts in enumerate(
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

            # Deterministic ordering.
            #
            # All events belong to the SAME market boundary.
            # The engine barrier prevents advancing to the
            # next minute until everything is processed.
            events.sort(
                key=lambda item: (
                    item[0],
                    TF_MS[item[1]],
                )
            )

            last_stream_id = None

            for (
                symbol,
                timeframe,
                candle,
            ) in events:

                result = (
                    publisher
                    .publish_historical_candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        candle=candle,
                    )
                )

                last_stream_id = result[
                    "stream_id"
                ]

            if last_stream_id is None:
                raise RuntimeError(
                    "Boundary without published events"
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
            # It can now safely process it.
            # ---------------------------------------------

            service.publish_boundary_ready(
                timestamp_ms=boundary_ts,
                end_stream_id=last_stream_id,
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
            # Progress
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

                print(
                    f"[REPLAY CLOCK] "
                    f"{boundary_index}/"
                    f"{len(boundaries)} "
                    f"({pct:.1f}%) | "
                    f"{format_ts(boundary_ts)} UTC | "
                    f"events={len(events)} | "
                    f"stream={last_stream_id}"
                )

    except KeyboardInterrupt:
        print()
        print(
            "[REPLAY] Interrupted by user"
        )

    finally:
        service.stop()

    print()
    print("=" * 70)
    print("REPLAY FINISHED")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()