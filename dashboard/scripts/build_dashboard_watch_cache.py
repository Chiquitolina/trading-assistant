from pathlib import Path
import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


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

BATCH_SIZE = 5000


WATCH_COLUMNS = [
    "symbol",
    "event",
    "state",
    "reason",
    "compression_created_ts",
    "event_ts",
    "breakout_detected",
    "breakout_ts",
    "breakout_price",
    "entry_ready",
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
    "compression_shape",
    "compression_quality_label",
    "selected_lookback",
    "selection_score",
]


READY_COLUMNS = [
    "symbol",
    "compression_created_ts",
    "journal_entry_ready_ts",
    "entry_ready_watch_age",
    "entry_ready_reason",
]


def normalize_watch_df(df):
    if df.empty:
        return df

    df = df.reindex(columns=WATCH_COLUMNS)

    string_cols = [
        "symbol",
        "event",
        "state",
        "reason",
        "compression_shape",
        "compression_quality_label",
    ]

    numeric_cols = [
        "compression_created_ts",
        "event_ts",
        "breakout_ts",
        "breakout_price",
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
    ]

    for col in string_cols:
        df[col] = df[col].astype("string")

    for col in numeric_cols:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        ).astype("float64")

    for col in [
        "breakout_detected",
        "entry_ready",
    ]:
        df[col] = (
            df[col]
            .map({
                True: True,
                False: False,
                "true": True,
                "false": False,
                1: True,
                0: False,
            })
            .astype("boolean")
        )

    return df


def normalize_ready_df(df):
    if df.empty:
        return df

    df = df.reindex(columns=READY_COLUMNS)

    df["symbol"] = (
        df["symbol"]
        .astype("string")
        .str.upper()
        .str.strip()
    )

    df["entry_ready_reason"] = (
        df["entry_ready_reason"]
        .astype("string")
    )

    for col in [
        "compression_created_ts",
        "journal_entry_ready_ts",
        "entry_ready_watch_age",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        ).astype("float64")

    return df


def flush_watch_batch(
    rows,
    writer,
):
    if not rows:
        return writer, 0

    df = normalize_watch_df(
        pd.DataFrame(rows)
    )

    df = df.dropna(
        subset=[
            "symbol",
            "compression_created_ts",
        ]
    )

    if df.empty:
        rows.clear()
        return writer, 0

    table = pa.Table.from_pandas(
        df,
        preserve_index=False,
    )

    if writer is None:
        writer = pq.ParquetWriter(
            WATCH_EVENTS_FILE,
            table.schema,
            compression="snappy",
        )

    writer.write_table(table)

    count = len(df)

    rows.clear()

    return writer, count


def flush_ready_batch(
    rows,
    writer,
):
    if not rows:
        return writer, 0

    df = normalize_ready_df(
        pd.DataFrame(rows)
    )

    df = df.dropna(
        subset=[
            "symbol",
            "compression_created_ts",
            "journal_entry_ready_ts",
        ]
    )

    if df.empty:
        rows.clear()
        return writer, 0

    table = pa.Table.from_pandas(
        df,
        preserve_index=False,
    )

    if writer is None:
        writer = pq.ParquetWriter(
            ENTRY_READY_FILE,
            table.schema,
            compression="snappy",
        )

    writer.write_table(table)

    count = len(df)

    rows.clear()

    return writer, count


def build_watch_cache():
    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Nunca dejar caches incompletos de una ejecución anterior.
    WATCH_EVENTS_FILE.unlink(
        missing_ok=True
    )

    ENTRY_READY_FILE.unlink(
        missing_ok=True
    )

    paths = sorted(
        WATCH_JOURNAL_DIR.glob("*.jsonl")
    )

    print(
        f"Found {len(paths)} watch journal files."
    )

    watch_rows = []
    ready_rows = []

    watch_writer = None
    ready_writer = None

    total_watch_events = 0
    total_entry_ready = 0

    try:
        for index, path in enumerate(
            paths,
            start=1,
        ):
            print(
                f"[{index}/{len(paths)}] "
                f"{path.name}"
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

                        watch_rows.append({
                            "symbol": symbol,
                            "event": record.get("event"),
                            "state": record.get("state"),
                            "reason": record.get("reason"),

                            "compression_created_ts":
                                record.get(
                                    "compression_created_ts"
                                ),

                            "event_ts":
                                last_candle.get(
                                    "timestamp"
                                ),

                            "breakout_detected":
                                record.get(
                                    "breakout_detected"
                                ),

                            "breakout_ts":
                                record.get(
                                    "breakout_ts"
                                ),

                            "breakout_price":
                                record.get(
                                    "breakout_price"
                                ),

                            "entry_ready":
                                record.get(
                                    "entry_ready"
                                ),

                            "entry_ready_ts":
                                record.get(
                                    "entry_ready_ts"
                                ),

                            "watch_age":
                                record.get(
                                    "watch_age"
                                ),

                            "candles_waiting":
                                record.get(
                                    "candles_waiting"
                                ),

                            "compression_score":
                                record.get(
                                    "compression_score"
                                ),

                            "trend_score":
                                record.get(
                                    "trend_score"
                                ),

                            "range_ratio":
                                record.get(
                                    "range_ratio"
                                ),

                            "atr_ratio":
                                record.get(
                                    "atr_ratio"
                                ),

                            "volume_ratio":
                                record.get(
                                    "volume_ratio"
                                ),

                            "avg_body_pct":
                                record.get(
                                    "avg_body_pct"
                                ),

                            "compression_range_pct":
                                record.get(
                                    "compression_range_pct"
                                ),

                            "compression_height_pct":
                                record.get(
                                    "compression_height_pct"
                                ),

                            "compression_duration":
                                record.get(
                                    "compression_duration"
                                ),

                            "inside_ratio":
                                record.get(
                                    "inside_ratio"
                                ),

                            "touches_high":
                                record.get(
                                    "touches_high"
                                ),

                            "touches_low":
                                record.get(
                                    "touches_low"
                                ),

                            "touches_high_ratio":
                                record.get(
                                    "touches_high_ratio"
                                ),

                            "touches_low_ratio":
                                record.get(
                                    "touches_low_ratio"
                                ),

                            "touch_imbalance_ratio":
                                record.get(
                                    "touch_imbalance_ratio"
                                ),

                            "upper_slope":
                                record.get(
                                    "upper_slope"
                                ),

                            "lower_slope":
                                record.get(
                                    "lower_slope"
                                ),

                            "slope_difference":
                                record.get(
                                    "slope_difference"
                                ),

                            "compression_shape":
                                record.get(
                                    "compression_shape"
                                ),

                            "compression_quality_label":
                                record.get(
                                    "compression_quality_label"
                                ),

                            "selected_lookback":
                                record.get(
                                    "selected_lookback"
                                ),

                            "selection_score":
                                record.get(
                                    "selection_score"
                                ),
                        })

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

                            ready_rows.append({
                                "symbol": symbol,

                                "compression_created_ts":
                                    record.get(
                                        "compression_created_ts"
                                    ),

                                "journal_entry_ready_ts":
                                    entry_ready_ts,

                                "entry_ready_watch_age":
                                    record.get(
                                        "watch_age"
                                    ),

                                "entry_ready_reason":
                                    record.get(
                                        "reason"
                                    ),
                            })

                        if (
                            len(watch_rows)
                            >= BATCH_SIZE
                        ):
                            (
                                watch_writer,
                                written,
                            ) = flush_watch_batch(
                                watch_rows,
                                watch_writer,
                            )

                            total_watch_events += written

                        if (
                            len(ready_rows)
                            >= BATCH_SIZE
                        ):
                            (
                                ready_writer,
                                written,
                            ) = flush_ready_batch(
                                ready_rows,
                                ready_writer,
                            )

                            total_entry_ready += written

            except Exception as exc:
                print(
                    f"ERROR reading "
                    f"{path}: {exc}"
                )

        # Últimos restos
        (
            watch_writer,
            written,
        ) = flush_watch_batch(
            watch_rows,
            watch_writer,
        )

        total_watch_events += written

        (
            ready_writer,
            written,
        ) = flush_ready_batch(
            ready_rows,
            ready_writer,
        )

        total_entry_ready += written

    finally:
        if watch_writer is not None:
            watch_writer.close()

        if ready_writer is not None:
            ready_writer.close()

    print()
    print("Done.")
    print(
        f"Watch events: "
        f"{total_watch_events:,}"
    )
    print(
        f"Entry ready events: "
        f"{total_entry_ready:,}"
    )
    print(
        f"Saved to: {CACHE_DIR}"
    )


if __name__ == "__main__":
    build_watch_cache()