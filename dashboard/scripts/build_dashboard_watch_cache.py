from pathlib import Path
import json

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[2]

WATCH_JOURNAL_DIR = (
    BASE_DIR / "compression_watch_journal"
)

CACHE_DIR = (
    BASE_DIR
    / "reports"
    / "dashboard_cache"
)

WATCH_EVENTS_FILE = (
    CACHE_DIR / "watch_events.parquet"
)

ENTRY_READY_FILE = (
    CACHE_DIR / "entry_ready_events.parquet"
)


def build_watch_cache():
    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    watch_rows = []
    entry_ready_rows = []

    paths = sorted(
        WATCH_JOURNAL_DIR.glob("*.jsonl")
    )

    print(
        f"Found {len(paths)} watch journal files."
    )

    for index, path in enumerate(paths, start=1):
        print(
            f"[{index}/{len(paths)}] {path.name}"
        )

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:

                for line in file:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        record = json.loads(line)
                    except (
                        json.JSONDecodeError,
                        TypeError,
                    ):
                        continue

                    symbol = str(
                        record.get(
                            "symbol",
                            path.stem,
                        )
                    ).upper().strip()

                    last_candle = (
                        record.get("last_candle")
                        or {}
                    )

                    # =========================
                    # ALL WATCH EVENTS
                    # =========================

                    watch_rows.append({
                        "symbol": symbol,
                        "event": record.get("event"),
                        "state": record.get("state"),
                        "reason": record.get("reason"),

                        "compression_created_ts": (
                            record.get(
                                "compression_created_ts"
                            )
                        ),

                        "event_ts": (
                            last_candle.get("timestamp")
                        ),

                        "breakout_detected": (
                            record.get(
                                "breakout_detected"
                            )
                        ),

                        "breakout_ts": (
                            record.get("breakout_ts")
                        ),

                        "breakout_price": (
                            record.get("breakout_price")
                        ),

                        "entry_ready": (
                            record.get("entry_ready")
                        ),

                        "entry_ready_ts": (
                            record.get("entry_ready_ts")
                        ),

                        "watch_age": (
                            record.get("watch_age")
                        ),

                        "candles_waiting": (
                            record.get(
                                "candles_waiting"
                            )
                        ),

                        "compression_score": (
                            record.get(
                                "compression_score"
                            )
                        ),

                        "trend_score": (
                            record.get("trend_score")
                        ),

                        "range_ratio": (
                            record.get("range_ratio")
                        ),

                        "atr_ratio": (
                            record.get("atr_ratio")
                        ),

                        "volume_ratio": (
                            record.get(
                                "volume_ratio"
                            )
                        ),

                        "avg_body_pct": (
                            record.get(
                                "avg_body_pct"
                            )
                        ),

                        "compression_range_pct": (
                            record.get(
                                "compression_range_pct"
                            )
                        ),

                        "compression_height_pct": (
                            record.get(
                                "compression_height_pct"
                            )
                        ),

                        "compression_duration": (
                            record.get(
                                "compression_duration"
                            )
                        ),

                        "inside_ratio": (
                            record.get("inside_ratio")
                        ),

                        "touches_high": (
                            record.get("touches_high")
                        ),

                        "touches_low": (
                            record.get("touches_low")
                        ),

                        "touches_high_ratio": (
                            record.get(
                                "touches_high_ratio"
                            )
                        ),

                        "touches_low_ratio": (
                            record.get(
                                "touches_low_ratio"
                            )
                        ),

                        "touch_imbalance_ratio": (
                            record.get(
                                "touch_imbalance_ratio"
                            )
                        ),

                        "upper_slope": (
                            record.get("upper_slope")
                        ),

                        "lower_slope": (
                            record.get("lower_slope")
                        ),

                        "slope_difference": (
                            record.get(
                                "slope_difference"
                            )
                        ),

                        "compression_shape": (
                            record.get(
                                "compression_shape"
                            )
                        ),

                        "compression_quality_label": (
                            record.get(
                                "compression_quality_label"
                            )
                        ),

                        "selected_lookback": (
                            record.get(
                                "selected_lookback"
                            )
                        ),

                        "selection_score": (
                            record.get(
                                "selection_score"
                            )
                        ),
                    })

                    # =========================
                    # ENTRY READY ONLY
                    # =========================

                    if (
                        record.get("event")
                        == "ENTRY_READY"
                    ):
                        entry_ready_ts = (
                            record.get(
                                "entry_ready_ts"
                            )
                            or last_candle.get(
                                "timestamp"
                            )
                            or record.get(
                                "compression_updated_ts"
                            )
                        )

                        entry_ready_rows.append({
                            "symbol": symbol,

                            "compression_created_ts": (
                                record.get(
                                    "compression_created_ts"
                                )
                            ),

                            "journal_entry_ready_ts": (
                                entry_ready_ts
                            ),

                            "entry_ready_watch_age": (
                                record.get(
                                    "watch_age"
                                )
                            ),

                            "entry_ready_reason": (
                                record.get("reason")
                            ),
                        })

        except Exception as exc:
            print(
                f"ERROR reading {path}: {exc}"
            )

    # ==========================================
    # WATCH EVENTS
    # ==========================================

    watch_df = pd.DataFrame(watch_rows)

    if not watch_df.empty:
        numeric_cols = [
            "compression_created_ts",
            "event_ts",
            "breakout_ts",
            "entry_ready_ts",
            "watch_age",
            "candles_waiting",
            "compression_score",
            "trend_score",
            "range_ratio",
            "atr_ratio",
            "volume_ratio",
            "avg_body_pct",
            "compression_range_pct",
            "compression_height_pct",
            "compression_duration",
            "inside_ratio",
            "touches_high",
            "touches_low",
            "touches_high_ratio",
            "touches_low_ratio",
            "touch_imbalance_ratio",
            "upper_slope",
            "lower_slope",
            "slope_difference",
            "selected_lookback",
            "selection_score",
            "breakout_price",
        ]

        for col in numeric_cols:
            if col in watch_df.columns:
                watch_df[col] = pd.to_numeric(
                    watch_df[col],
                    errors="coerce",
                )

        watch_df = watch_df.dropna(
            subset=[
                "symbol",
                "compression_created_ts",
            ]
        )

        watch_df.to_parquet(
            WATCH_EVENTS_FILE,
            index=False,
        )

    # ==========================================
    # ENTRY READY
    # ==========================================

    ready_df = pd.DataFrame(
        entry_ready_rows
    )

    if not ready_df.empty:
        ready_df[
            "compression_created_ts"
        ] = pd.to_numeric(
            ready_df[
                "compression_created_ts"
            ],
            errors="coerce",
        )

        ready_df[
            "journal_entry_ready_ts"
        ] = pd.to_numeric(
            ready_df[
                "journal_entry_ready_ts"
            ],
            errors="coerce",
        )

        ready_df = (
            ready_df
            .dropna(
                subset=[
                    "symbol",
                    "compression_created_ts",
                    "journal_entry_ready_ts",
                ]
            )
            .drop_duplicates(
                subset=[
                    "symbol",
                    "compression_created_ts",
                ],
                keep="last",
            )
            .reset_index(drop=True)
        )

        ready_df.to_parquet(
            ENTRY_READY_FILE,
            index=False,
        )

    print()
    print("Done.")
    print(
        f"Watch events: {len(watch_df):,}"
    )
    print(
        f"Entry ready events: {len(ready_df):,}"
    )
    print(
        f"Saved to: {CACHE_DIR}"
    )


if __name__ == "__main__":
    build_watch_cache()