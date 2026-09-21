import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path("compression_fidelity_journal")


# ============================================================
# LOAD
# ============================================================

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows = []

    with path.open("r", encoding="utf-8") as f:
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


def values_equal(
    left: Any,
    right: Any,
    tolerance: float = 1e-10,
) -> bool:
    left = normalize_number(left)
    right = normalize_number(right)

    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return abs(float(left) - float(right)) <= tolerance

    return left == right


# ============================================================
# CANDLES
# ============================================================

CANDLE_FIELDS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def candle_signature(candle: dict | None):
    if not candle:
        return None

    return {
        field: normalize_number(candle.get(field))
        for field in CANDLE_FIELDS
    }


def compare_candle(
    field_prefix: str,
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
                "field": field_prefix,
                "live": live,
                "replay": replay,
                "category": "MARKET_INPUT",
            }
        )
        return differences

    for field in CANDLE_FIELDS:
        if not values_equal(
            live.get(field),
            replay.get(field),
        ):
            differences.append(
                {
                    "field": f"{field_prefix}.{field}",
                    "live": live.get(field),
                    "replay": replay.get(field),
                    "category": "MARKET_INPUT",
                }
            )

    return differences


def compare_candle_list(
    field_name: str,
    live_candles,
    replay_candles,
):
    live_candles = live_candles or []
    replay_candles = replay_candles or []

    differences = []

    if len(live_candles) != len(replay_candles):
        differences.append(
            {
                "field": f"{field_name}.length",
                "live": len(live_candles),
                "replay": len(replay_candles),
                "category": "MARKET_INPUT",
            }
        )

    common_length = min(
        len(live_candles),
        len(replay_candles),
    )

    for index in range(common_length):
        differences.extend(
            compare_candle(
                f"{field_name}[{index}]",
                live_candles[index],
                replay_candles[index],
            )
        )

    return differences


# ============================================================
# EVALUATION TIME
# ============================================================

def evaluation_timestamp(row: dict):
    """
    Compare LIVE and REPLAY using market time.

    Never use logged_at:
    LIVE and REPLAY are executed at different wall-clock times.
    """

    value = row.get("evaluation_ts")

    if value is not None:
        return int(value)

    value = row.get("candle_open_ts")

    if value is not None:
        return int(value)

    current_candle = row.get("current_candle")

    if isinstance(current_candle, dict):
        value = current_candle.get("timestamp")

        if value is not None:
            return int(value)

    return None


def index_by_evaluation_time(rows: list[dict]):
    result = {}

    for row in rows:
        timestamp = evaluation_timestamp(row)

        if timestamp is None:
            continue

        result.setdefault(timestamp, []).append(row)

    return result


# ============================================================
# FIELD GROUPS
# ============================================================

IDENTITY_FIELDS = (
    "tf",
    "candle_open_ts",
    "candle_close_ts",
)

BUFFER_FIELDS = (
    "buffer_candles_total",
    "detector_input_candles",
)

TREND_FIELDS = (
    "trend_up",
    "trend_score",
)

COMPRESSION_FIELDS = (
    "compression_detected",
    "compression_score",
    "compression_high",
    "compression_low",
)

STATE_FIELDS = (
    "state_before",
    "state_after",
)

WATCH_FIELDS = (
    "watch_age",
    "candles_waiting",
    "watch_created_ts",
    "watch_updated_ts",
    "frozen_compression_high",
    "frozen_compression_low",
)

BREAKOUT_FIELDS = (
    "breakout_detected",
    "breakout_reason",
    "breakout_extension_pct",
)

PULLBACK_FIELDS = (
    "pullback_valid",
    "entry_ready",
    "entry_from_compression_high_pct",
)


def compare_field_group(
    live: dict,
    replay: dict,
    fields,
    category: str,
):
    differences = []

    for field in fields:
        left = live.get(field)
        right = replay.get(field)

        if not values_equal(left, right):
            differences.append(
                {
                    "field": field,
                    "live": left,
                    "replay": right,
                    "category": category,
                }
            )

    return differences


# ============================================================
# ROW COMPARISON
# ============================================================

def compare_rows(
    live: dict,
    replay: dict,
):
    differences = []

    # --------------------------------------------------------
    # Evaluation identity
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            IDENTITY_FIELDS,
            "EVALUATION_IDENTITY",
        )
    )

    # --------------------------------------------------------
    # Exact current candle
    # --------------------------------------------------------

    differences.extend(
        compare_candle(
            "current_candle",
            live.get("current_candle"),
            replay.get("current_candle"),
        )
    )

    # --------------------------------------------------------
    # Detector input
    # --------------------------------------------------------

    differences.extend(
        compare_candle_list(
            "detector_input_last_10",
            live.get("detector_input_last_10"),
            replay.get("detector_input_last_10"),
        )
    )

    # Support last_40 as soon as the journal records it.
    if (
        "detector_input_last_40" in live
        or "detector_input_last_40" in replay
    ):
        differences.extend(
            compare_candle_list(
                "detector_input_last_40",
                live.get("detector_input_last_40"),
                replay.get("detector_input_last_40"),
            )
        )

    # --------------------------------------------------------
    # Buffer
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            BUFFER_FIELDS,
            "BUFFER",
        )
    )

    # --------------------------------------------------------
    # Trend detector
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            TREND_FIELDS,
            "TREND",
        )
    )

    # --------------------------------------------------------
    # Compression detector
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            COMPRESSION_FIELDS,
            "COMPRESSION",
        )
    )

    # --------------------------------------------------------
    # State machine
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            STATE_FIELDS,
            "STATE_MACHINE",
        )
    )

    differences.extend(
        compare_field_group(
            live,
            replay,
            WATCH_FIELDS,
            "WATCH",
        )
    )

    # --------------------------------------------------------
    # Breakout
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            BREAKOUT_FIELDS,
            "BREAKOUT",
        )
    )

    # --------------------------------------------------------
    # Pullback / ENTRY_READY
    # --------------------------------------------------------

    differences.extend(
        compare_field_group(
            live,
            replay,
            PULLBACK_FIELDS,
            "PULLBACK",
        )
    )

    return differences


# ============================================================
# CLASSIFICATION
# ============================================================

CATEGORY_PRIORITY = (
    "MISSING_EVALUATION",
    "EVALUATION_IDENTITY",
    "MARKET_INPUT",
    "BUFFER",
    "TREND",
    "COMPRESSION",
    "STATE_MACHINE",
    "WATCH",
    "BREAKOUT",
    "PULLBACK",
)


CLASSIFICATION_NAMES = {
    "MISSING_EVALUATION": "MISSING_EVALUATION",
    "EVALUATION_IDENTITY": "EVALUATION_IDENTITY_DIVERGENCE",
    "MARKET_INPUT": "MARKET_INPUT_DIVERGENCE",
    "BUFFER": "BUFFER_DIVERGENCE",
    "TREND": "TREND_DETECTOR_DIVERGENCE",
    "COMPRESSION": "COMPRESSION_DETECTOR_DIVERGENCE",
    "STATE_MACHINE": "STATE_MACHINE_DIVERGENCE",
    "WATCH": "WATCH_STATE_DIVERGENCE",
    "BREAKOUT": "BREAKOUT_DIVERGENCE",
    "PULLBACK": "PULLBACK_ENTRY_DIVERGENCE",
}


def classify_differences(differences):
    categories = {
        diff.get("category")
        for diff in differences
    }

    for category in CATEGORY_PRIORITY:
        if category in categories:
            return CLASSIFICATION_NAMES[category]

    return "UNKNOWN_DIVERGENCE"


# ============================================================
# REPORT HELPERS
# ============================================================

def describe_row(row: dict | None):
    if row is None:
        return "MISSING"

    return (
        f"line={row.get('_line_number')} "
        f"state_before={row.get('state_before')} "
        f"state_after={row.get('state_after')} "
        f"trend={row.get('trend_up')} "
        f"trend_score={row.get('trend_score')} "
        f"compression={row.get('compression_detected')} "
        f"compression_score={row.get('compression_score')}"
    )


def print_side(label: str, row: dict | None):
    print(label)

    if row is None:
        print("  MISSING")
        return

    print(
        f"  line                : "
        f"{row.get('_line_number')}"
    )
    print(
        f"  evaluation_ts       : "
        f"{evaluation_timestamp(row)}"
    )
    print(
        f"  candle_open_ts      : "
        f"{row.get('candle_open_ts')}"
    )
    print(
        f"  candle_close_ts     : "
        f"{row.get('candle_close_ts')}"
    )
    print(
        f"  state_before        : "
        f"{row.get('state_before')}"
    )
    print(
        f"  state_after         : "
        f"{row.get('state_after')}"
    )
    print(
        f"  trend_up            : "
        f"{row.get('trend_up')}"
    )
    print(
        f"  trend_score         : "
        f"{row.get('trend_score')}"
    )
    print(
        f"  compression_detected: "
        f"{row.get('compression_detected')}"
    )
    print(
        f"  compression_score   : "
        f"{row.get('compression_score')}"
    )
    print(
        f"  compression_high    : "
        f"{row.get('compression_high')}"
    )
    print(
        f"  compression_low     : "
        f"{row.get('compression_low')}"
    )
    print(
        f"  breakout_detected   : "
        f"{row.get('breakout_detected')}"
    )
    print(
        f"  pullback_valid      : "
        f"{row.get('pullback_valid')}"
    )
    print(
        f"  entry_ready         : "
        f"{row.get('entry_ready')}"
    )


# ============================================================
# SYMBOL COMPARISON
# ============================================================

def compare_symbol(
    symbol: str,
    live_path: Path,
    replay_path: Path,
    show_all: bool = False,
    comparable_only: bool = False,
    from_ts: int | None = None,
    to_ts: int | None = None,
):
    live_rows = load_jsonl(live_path)
    replay_rows = load_jsonl(replay_path)

    print("=" * 80)
    print(f"COMPRESSION FIDELITY: {symbol}")
    print("=" * 80)

    print(f"LIVE   : {live_path}")
    print(f"REPLAY : {replay_path}")
    print()

    print(
        f"Evaluations: "
        f"LIVE={len(live_rows)} "
        f"REPLAY={len(replay_rows)}"
    )

    if not live_rows:
        print()
        print("No LIVE fidelity evaluations found.")
        return 1

    if not replay_rows:
        print()
        print("No REPLAY fidelity evaluations found.")
        return 1

    live_index = index_by_evaluation_time(live_rows)
    replay_index = index_by_evaluation_time(replay_rows)

    common_timestamps = sorted(
        set(live_index)
        & set(replay_index)
    )

    live_only_timestamps = sorted(
        set(live_index)
        - set(replay_index)
    )

    replay_only_timestamps = sorted(
        set(replay_index)
        - set(live_index)
    )

    print(
        f"Comparable timestamps : "
        f"{len(common_timestamps)}"
    )
    print(
        f"LIVE-only timestamps  : "
        f"{len(live_only_timestamps)}"
    )
    print(
        f"REPLAY-only timestamps: "
        f"{len(replay_only_timestamps)}"
    )

    if comparable_only:
        selected_timestamps = set(live_index) & set(replay_index)
    else:
        selected_timestamps = set(live_index) | set(replay_index)

    if from_ts is not None:
        selected_timestamps = {
            ts for ts in selected_timestamps
            if ts >= from_ts
        }

    if to_ts is not None:
        selected_timestamps = {
            ts for ts in selected_timestamps
            if ts <= to_ts
        }

    all_timestamps = sorted(selected_timestamps)
    print(
        f"Selected timestamps   : "
        f"{len(all_timestamps)}"
    )

    print(
        f"Comparison mode       : "
        f"{'COMPARABLE ONLY' if comparable_only else 'ALL'}"
    )

    if from_ts is not None:
        print(f"From timestamp        : {from_ts}")

    if to_ts is not None:
        print(f"To timestamp          : {to_ts}")

    if not all_timestamps:
        print()
        print("No comparable market timestamps found.")
        return 1

    matches = 0
    divergences = 0
    first_divergence = None

    for timestamp in all_timestamps:
        live_evaluations = live_index.get(timestamp, [])
        replay_evaluations = replay_index.get(timestamp, [])

        max_count = max(
            len(live_evaluations),
            len(replay_evaluations),
        )

        for evaluation_index in range(max_count):
            live = (
                live_evaluations[evaluation_index]
                if evaluation_index < len(live_evaluations)
                else None
            )

            replay = (
                replay_evaluations[evaluation_index]
                if evaluation_index < len(replay_evaluations)
                else None
            )

            if live is None or replay is None:
                differences = [
                    {
                        "field": "evaluation",
                        "live": describe_row(live),
                        "replay": describe_row(replay),
                        "category": "MISSING_EVALUATION",
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
                        f"[MATCH] "
                        f"ts={timestamp} "
                        f"state={live.get('state_after')}"
                    )

                continue

            divergences += 1

            classification = classify_differences(
                differences
            )

            divergence = {
                "timestamp": timestamp,
                "evaluation_index": evaluation_index,
                "live": live,
                "replay": replay,
                "differences": differences,
                "classification": classification,
            }

            if first_divergence is None:
                first_divergence = divergence

            if show_all:
                print()
                print(
                    f"[DIVERGENCE] "
                    f"ts={timestamp} "
                    f"class={classification}"
                )

                for diff in differences:
                    print(
                        f"  [{diff['category']}] "
                        f"{diff['field']}: "
                        f"LIVE={diff['live']} "
                        f"REPLAY={diff['replay']}"
                    )

    # ========================================================
    # SUMMARY
    # ========================================================

    print()
    print("-" * 80)
    print("SUMMARY")
    print("-" * 80)

    print(f"Matches     : {matches}")
    print(f"Divergences : {divergences}")

    if first_divergence is None:
        print()
        print("NO DIVERGENCES FOUND")
        return 0

    # ========================================================
    # FIRST DIVERGENCE
    # ========================================================

    timestamp = first_divergence["timestamp"]
    live = first_divergence["live"]
    replay = first_divergence["replay"]

    print()
    print("=" * 80)
    print("FIRST DIVERGENCE")
    print("=" * 80)

    print(
        f"evaluation_ts : {timestamp}"
    )
    print(
        f"classification: "
        f"{first_divergence['classification']}"
    )

    print()

    print_side(
        "LIVE:",
        live,
    )

    print()

    print_side(
        "REPLAY:",
        replay,
    )

    print()
    print("-" * 80)
    print("DIFFERENCES")
    print("-" * 80)

    for diff in first_divergence["differences"]:
        print(
            f"[{diff['category']}] "
            f"{diff['field']}"
        )
        print(
            f"  LIVE   = {diff['live']}"
        )
        print(
            f"  REPLAY = {diff['replay']}"
        )

    return 2


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare LIVE and REPLAY compression "
            "fidelity evaluations."
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
        help=(
            "Root containing live/ and replay/ "
            "fidelity journals."
        ),
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Print every match and divergence.",
    )
    
    parser.add_argument(
        "--comparable-only",
        action="store_true",
        help=(
            "Compare only market timestamps present "
            "in both LIVE and REPLAY."
        ),
    )

    parser.add_argument(
        "--from-ts",
        type=int,
        default=None,
        help=(
            "Ignore evaluations before this market "
            "timestamp in milliseconds."
        ),
    )

    parser.add_argument(
        "--to-ts",
        type=int,
        default=None,
        help=(
            "Ignore evaluations after this market "
            "timestamp in milliseconds."
        ),
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
            comparable_only=args.comparable_only,
            from_ts=args.from_ts,
            to_ts=args.to_ts,
        )
    )

if __name__ == "__main__":
    main()