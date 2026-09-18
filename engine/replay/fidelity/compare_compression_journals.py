import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path("compression_watch_journal")


# ============================================================
# LOAD
# ============================================================

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows = []

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"[WARN] Invalid JSON "
                    f"{path}:{line_number}: {exc}"
                )
                continue

            row["_line_number"] = line_number
            rows.append(row)

    return rows


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_number(value: Any):
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def numbers_equal(
    left: Any,
    right: Any,
    tolerance: float = 1e-10,
) -> bool:
    left = normalize_number(left)
    right = normalize_number(right)

    if isinstance(left, (int, float)) and isinstance(
        right,
        (int, float),
    ):
        return abs(float(left) - float(right)) <= tolerance

    return left == right


def candle_signature(candle: dict | None):
    if not candle:
        return None

    return {
        "timestamp": candle.get("timestamp"),
        "open": normalize_number(candle.get("open")),
        "high": normalize_number(candle.get("high")),
        "low": normalize_number(candle.get("low")),
        "close": normalize_number(candle.get("close")),
        "volume": normalize_number(candle.get("volume")),
    }


def candles_signature(candles):
    if not candles:
        return []

    return [
        candle_signature(candle)
        for candle in candles
    ]


# ============================================================
# EVENT TIME
# ============================================================

def event_timestamp(row: dict):
    """
    Prefer the timestamp of the candle that caused this journal event.

    This lets LIVE and REPLAY be compared using market time instead
    of logged_at wall-clock time.
    """

    last_candle = row.get("last_candle")

    if isinstance(last_candle, dict):
        timestamp = last_candle.get("timestamp")

        if timestamp is not None:
            return int(timestamp)

    for field in (
        "compression_updated_ts",
        "compression_created_ts",
    ):
        value = row.get(field)

        if value is not None:
            return int(value)

    return None


def index_by_market_time(rows: list[dict]):
    """
    A symbol can theoretically have more than one journal event for
    the same market candle, so keep a list instead of overwriting.
    """

    result = {}

    for row in rows:
        timestamp = event_timestamp(row)

        if timestamp is None:
            continue

        result.setdefault(timestamp, []).append(row)

    return result


# ============================================================
# COMPARISON
# ============================================================

FIELDS = [
    "event",
    "reason",
    "watch_age",
    "candles_waiting",
    "compression_created_ts",
    "compression_updated_ts",
    "compression_high",
    "compression_low",
    "compression_score",
    "trend_score",
    "compression_height_pct",
    "compression_duration_candles",
    "compression_lookback",
    "breakout_detected",
    "breakout_reason",
    "pullback_valid",
    "entry_ready",
]


def compare_candle(
    live_candle: dict | None,
    replay_candle: dict | None,
):
    differences = []

    live = candle_signature(live_candle)
    replay = candle_signature(replay_candle)

    if live is None and replay is None:
        return differences

    if live is None or replay is None:
        differences.append(
            {
                "field": "last_candle",
                "live": live,
                "replay": replay,
            }
        )
        return differences

    for field in (
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ):
        if not numbers_equal(
            live.get(field),
            replay.get(field),
        ):
            differences.append(
                {
                    "field": f"last_candle.{field}",
                    "live": live.get(field),
                    "replay": replay.get(field),
                }
            )

    return differences


def compare_last_10(
    live_candles,
    replay_candles,
):
    live = candles_signature(live_candles)
    replay = candles_signature(replay_candles)

    if live == replay:
        return []

    differences = []

    if len(live) != len(replay):
        differences.append(
            {
                "field": "last_10_candles.length",
                "live": len(live),
                "replay": len(replay),
            }
        )

    max_common = min(len(live), len(replay))

    for index in range(max_common):
        live_candle = live[index]
        replay_candle = replay[index]

        for field in (
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ):
            if not numbers_equal(
                live_candle.get(field),
                replay_candle.get(field),
            ):
                differences.append(
                    {
                        "field": (
                            f"last_10_candles"
                            f"[{index}].{field}"
                        ),
                        "live": live_candle.get(field),
                        "replay": replay_candle.get(field),
                    }
                )

    return differences


def compare_rows(
    live: dict,
    replay: dict,
):
    differences = []

    differences.extend(
        compare_candle(
            live.get("last_candle"),
            replay.get("last_candle"),
        )
    )

    differences.extend(
        compare_last_10(
            live.get("last_10_candles"),
            replay.get("last_10_candles"),
        )
    )

    for field in FIELDS:
        left = live.get(field)
        right = replay.get(field)

        if not numbers_equal(left, right):
            differences.append(
                {
                    "field": field,
                    "live": left,
                    "replay": right,
                }
            )

    return differences


# ============================================================
# REPORT
# ============================================================

def print_event(row: dict | None):
    if row is None:
        return "MISSING"

    return (
        f"{row.get('event')} "
        f"(line={row.get('_line_number')})"
    )


def compare_symbol(
    symbol: str,
    live_path: Path,
    replay_path: Path,
    show_all: bool = False,
):
    live_rows = load_jsonl(live_path)
    replay_rows = load_jsonl(replay_path)

    print("=" * 72)
    print(f"FIDELITY COMPARISON: {symbol}")
    print("=" * 72)

    print(f"LIVE   : {live_path}")
    print(f"REPLAY : {replay_path}")
    print()

    print(
        f"Journal events: "
        f"LIVE={len(live_rows)} "
        f"REPLAY={len(replay_rows)}"
    )

    live_index = index_by_market_time(live_rows)
    replay_index = index_by_market_time(replay_rows)

    all_timestamps = sorted(
        set(live_index)
        | set(replay_index)
    )

    if not all_timestamps:
        print()
        print("No comparable market timestamps found.")
        return 1

    first_divergence = None
    matches = 0
    divergences = 0

    for timestamp in all_timestamps:
        live_events = live_index.get(timestamp, [])
        replay_events = replay_index.get(timestamp, [])

        # For the current journal structure, normally there is one
        # event per symbol/candle. If there are multiple, compare
        # positionally and report the cardinality difference.
        max_events = max(
            len(live_events),
            len(replay_events),
        )

        for event_index in range(max_events):
            live = (
                live_events[event_index]
                if event_index < len(live_events)
                else None
            )

            replay = (
                replay_events[event_index]
                if event_index < len(replay_events)
                else None
            )

            if live is None or replay is None:
                differences = [
                    {
                        "field": "journal_event",
                        "live": print_event(live),
                        "replay": print_event(replay),
                    }
                ]
            else:
                differences = compare_rows(
                    live,
                    replay,
                )

            if not differences:
                matches += 1

                if show_all:
                    print(
                        f"[MATCH] ts={timestamp} "
                        f"event={live.get('event')}"
                    )

                continue

            divergences += 1

            divergence = {
                "timestamp": timestamp,
                "event_index": event_index,
                "live": live,
                "replay": replay,
                "differences": differences,
            }

            if first_divergence is None:
                first_divergence = divergence

            if show_all:
                print()
                print(
                    f"[DIVERGENCE] "
                    f"ts={timestamp}"
                )

                for diff in differences:
                    print(
                        f"  {diff['field']}: "
                        f"LIVE={diff['live']} "
                        f"REPLAY={diff['replay']}"
                    )

    print()
    print("-" * 72)
    print("SUMMARY")
    print("-" * 72)
    print(f"Matches     : {matches}")
    print(f"Divergences : {divergences}")

    if first_divergence is None:
        print()
        print("NO DIVERGENCES FOUND")
        return 0

    timestamp = first_divergence["timestamp"]
    live = first_divergence["live"]
    replay = first_divergence["replay"]

    print()
    print("=" * 72)
    print("FIRST DIVERGENCE")
    print("=" * 72)
    print(f"market_timestamp : {timestamp}")
    print()

    print(
        "LIVE   : "
        + print_event(live)
    )
    print(
        "REPLAY : "
        + print_event(replay)
    )

    print()
    print("DIFFERENCES:")

    for diff in first_divergence["differences"]:
        print(
            f"  {diff['field']}"
        )
        print(
            f"    LIVE   = {diff['live']}"
        )
        print(
            f"    REPLAY = {diff['replay']}"
        )

    return 2


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare LIVE and REPLAY "
            "compression watch journals."
        )
    )

    parser.add_argument(
        "--symbol",
        required=True,
        help="Example: AWEUSDT",
    )

    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Print every match/divergence.",
    )

    args = parser.parse_args()

    symbol = args.symbol.upper()
    root = Path(args.root)

    live_path = (
        root
        / "live"
        / f"{symbol}.jsonl"
    )

    replay_path = (
        root
        / "replay"
        / f"{symbol}.jsonl"
    )

    raise SystemExit(
        compare_symbol(
            symbol=symbol,
            live_path=live_path,
            replay_path=replay_path,
            show_all=args.all,
        )
    )


if __name__ == "__main__":
    main()