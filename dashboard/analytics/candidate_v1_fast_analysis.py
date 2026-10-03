"""Fast persisted 1m path analysis for Candidate V1 dashboard research.

This module deliberately contains no Candidate selection logic. The dashboard
passes already-qualified Candidate rows plus its existing candle loader. We
persist each event's causal 1m path (next candle after REACTION, up to 360m)
so later TP/SL matrices, 180/240/360 follow-up, fees and filters do not need to
re-read/re-scan Redis history on every Streamlit widget interaction.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd


_STORE_LOCK = threading.Lock()
_STORE_CACHE: dict[str, tuple[float | None, pd.DataFrame]] = {}
_LAST_REFRESH: dict[str, float] = {}

PATH_COLUMNS = [
    "candidate_v1_event_key",
    "candidate_v1_cohort",
    "symbol",
    "side",
    "reaction_timestamp",
    "entry_timestamp",
    "bar_offset",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
]


def _empty_store() -> pd.DataFrame:
    return pd.DataFrame(columns=PATH_COLUMNS)


def _file_mtime(path: Path) -> float | None:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return None


def load_path_store(store_path: str | Path) -> pd.DataFrame:
    path = Path(store_path)
    key = str(path.resolve())
    mtime = _file_mtime(path)

    cached = _STORE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1].copy()

    if not path.exists():
        frame = _empty_store()
    else:
        try:
            frame = pd.read_pickle(path)
        except Exception:
            frame = _empty_store()

    for column in PATH_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan

    if not frame.empty:
        for column in (
            "reaction_timestamp",
            "entry_timestamp",
            "bar_offset",
            "timestamp",
        ):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        frame = (
            frame.dropna(subset=["candidate_v1_event_key", "timestamp"])
            .sort_values(
                ["candidate_v1_event_key", "timestamp"],
                kind="stable",
            )
            .drop_duplicates(
                subset=["candidate_v1_event_key", "timestamp"],
                keep="last",
            )
            .reset_index(drop=True)
        )

    _STORE_CACHE[key] = (mtime, frame.copy())
    return frame


def _atomic_write_store(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frame.to_pickle(tmp)
    os.replace(tmp, path)


def _candidate_records(candidate_rows: pd.DataFrame) -> pd.DataFrame:
    if candidate_rows is None or candidate_rows.empty:
        return pd.DataFrame()

    required = {
        "candidate_v1_event_key",
        "symbol",
        "signal",
        "retest_timestamp",
    }
    if not required.issubset(candidate_rows.columns):
        return pd.DataFrame()

    result = candidate_rows.copy()
    result["candidate_v1_event_key"] = (
        result["candidate_v1_event_key"].astype(str)
    )
    result["symbol"] = result["symbol"].astype(str)
    result["signal"] = result["signal"].astype(str).str.upper()
    result["retest_timestamp"] = pd.to_numeric(
        result["retest_timestamp"], errors="coerce"
    )
    if "candidate_v1_cohort" not in result.columns:
        result["candidate_v1_cohort"] = "FORWARD"

    return (
        result.loc[result["signal"].isin(["LONG", "SHORT"])]
        .dropna(subset=["retest_timestamp"])
        .drop_duplicates(subset=["candidate_v1_event_key"], keep="last")
        .reset_index(drop=True)
    )


def _event_is_complete(event_store: pd.DataFrame, max_horizon: int) -> bool:
    if event_store is None or event_store.empty:
        return False
    offsets = pd.to_numeric(event_store["bar_offset"], errors="coerce").dropna()
    if offsets.empty:
        return False
    return int(offsets.max()) >= int(max_horizon) - 1


def refresh_path_store(
    candidate_rows: pd.DataFrame,
    *,
    store_path: str | Path,
    candle_loader: Callable[[str, str, int], pd.DataFrame],
    prepare_candles: Callable[[pd.DataFrame], pd.DataFrame],
    candle_limit: int = 5000,
    max_horizon: int = 360,
    refresh_interval_seconds: int = 60,
    force: bool = False,
) -> pd.DataFrame:
    """Append/refresh causal 1m paths and persist them to a compact pickle store.

    Completed 360-bar paths are immutable and never hit Redis again. Incomplete
    events are refreshed at most once per refresh interval, unless a previously
    unseen event is supplied. Existing early bars are retained, so paths remain
    analyzable after Redis rolls forward.
    """
    candidates = _candidate_records(candidate_rows)
    path = Path(store_path)
    key = str(path.resolve())

    with _STORE_LOCK:
        store = load_path_store(path)
        requested_keys = set(candidates.get("candidate_v1_event_key", []))
        known_keys = set(store.get("candidate_v1_event_key", pd.Series(dtype=str)).astype(str))
        unseen = bool(requested_keys - known_keys)

        now = time.monotonic()
        last = _LAST_REFRESH.get(key, 0.0)
        if (
            not force
            and not unseen
            and store is not None
            and (now - last) < max(1, int(refresh_interval_seconds))
        ):
            return store.copy()

        if candidates.empty:
            _LAST_REFRESH[key] = now
            return store.copy()

        # Only events that are unseen or not yet complete need candle access.
        needs_rows = []
        for _, candidate in candidates.iterrows():
            event_key = str(candidate["candidate_v1_event_key"])
            existing_event = store.loc[
                store["candidate_v1_event_key"].astype(str).eq(event_key)
            ] if not store.empty else _empty_store()
            if not _event_is_complete(existing_event, max_horizon):
                needs_rows.append(candidate)

        if not needs_rows:
            _LAST_REFRESH[key] = now
            return store.copy()

        needs = pd.DataFrame(needs_rows)
        additions = []

        for symbol, symbol_events in needs.groupby("symbol", sort=False):
            try:
                raw = candle_loader(str(symbol), "1m", int(candle_limit))
                prepared = prepare_candles(raw)
            except Exception:
                continue
            if prepared is None or prepared.empty:
                continue

            work = prepared.copy()
            for column in ("timestamp", "open", "high", "low", "close"):
                if column not in work.columns:
                    work = pd.DataFrame()
                    break
                work[column] = pd.to_numeric(work[column], errors="coerce")
            if work.empty:
                continue
            work = (
                work.dropna(subset=["timestamp", "open", "high", "low", "close"])
                .sort_values("timestamp")
                .drop_duplicates(subset=["timestamp"], keep="last")
                .reset_index(drop=True)
            )
            if work.empty:
                continue

            for _, candidate in symbol_events.iterrows():
                event_key = str(candidate["candidate_v1_event_key"])
                reaction_ts = int(candidate["retest_timestamp"])
                entry_ts = reaction_ts + 60_000
                end_ts = entry_ts + (int(max_horizon) - 1) * 60_000

                current = work.loc[
                    work["timestamp"].between(entry_ts, end_ts, inclusive="both")
                ][["timestamp", "open", "high", "low", "close"]].copy()

                existing_event = store.loc[
                    store["candidate_v1_event_key"].astype(str).eq(event_key)
                ][["timestamp", "open", "high", "low", "close"]].copy() \
                    if not store.empty else pd.DataFrame()

                merged = pd.concat([existing_event, current], ignore_index=True)
                if merged.empty:
                    continue
                merged = (
                    merged.dropna(subset=["timestamp", "open", "high", "low", "close"])
                    .sort_values("timestamp")
                    .drop_duplicates(subset=["timestamp"], keep="last")
                    .reset_index(drop=True)
                )

                # Keep only the contiguous prefix beginning exactly at entry.
                timestamps = merged["timestamp"].astype("int64").to_numpy()
                start = int(np.searchsorted(timestamps, entry_ts, side="left"))
                if start >= len(timestamps) or int(timestamps[start]) != entry_ts:
                    continue
                contiguous_len = 1
                while (
                    start + contiguous_len < len(timestamps)
                    and contiguous_len < int(max_horizon)
                    and int(timestamps[start + contiguous_len])
                    == entry_ts + contiguous_len * 60_000
                ):
                    contiguous_len += 1

                event_path = merged.iloc[start : start + contiguous_len].copy()
                event_path["bar_offset"] = np.arange(len(event_path), dtype=int)
                event_path["candidate_v1_event_key"] = event_key
                event_path["candidate_v1_cohort"] = str(
                    candidate.get("candidate_v1_cohort", "FORWARD")
                )
                event_path["symbol"] = str(symbol)
                event_path["side"] = str(candidate["signal"]).upper()
                event_path["reaction_timestamp"] = reaction_ts
                event_path["entry_timestamp"] = entry_ts
                additions.append(event_path[PATH_COLUMNS])

        if additions:
            updated_keys = {
                str(frame["candidate_v1_event_key"].iloc[0])
                for frame in additions
                if not frame.empty
            }
            untouched = store.loc[
                ~store["candidate_v1_event_key"].astype(str).isin(updated_keys)
            ].copy() if not store.empty else _empty_store()
            store = pd.concat([untouched, *additions], ignore_index=True, sort=False)
            store = (
                store.sort_values(
                    ["candidate_v1_event_key", "timestamp"], kind="stable"
                )
                .drop_duplicates(
                    subset=["candidate_v1_event_key", "timestamp"], keep="last"
                )
                .reset_index(drop=True)
            )
            try:
                _atomic_write_store(store, path)
                mtime = _file_mtime(path)
                _STORE_CACHE[key] = (mtime, store.copy())
            except Exception:
                # Dashboard research should remain usable even if persistence
                # fails; keep the in-process cache for this session.
                _STORE_CACHE[key] = (_file_mtime(path), store.copy())

        _LAST_REFRESH[key] = now
        return store.copy()


def _first_crossing(values: np.ndarray, threshold: float, max_index: int) -> int | None:
    if values.size == 0 or max_index < 0:
        return None
    upper = min(int(max_index) + 1, int(values.size))
    hits = np.flatnonzero(values[:upper] >= float(threshold))
    return int(hits[0]) if len(hits) else None


def build_execution_grid(
    candidate_rows: pd.DataFrame,
    path_store: pd.DataFrame,
    *,
    tp_values: Iterable[float],
    sl_values: Iterable[float],
    horizon_min: int = 180,
    entry_fee_pct: float = 0.05,
    exit_fee_pct: float = 0.05,
    entry_slippage_pct: float = 0.0,
    exit_slippage_pct: float = 0.0,
    notional_usdt: float = 100.0,
) -> pd.DataFrame:
    """Vectorized-ish first-touch matrix from already-materialized paths.

    Candle scanning is O(events × (TP levels + SL levels) × horizon), not
    O(events × TP × SL × horizon). Pair combination is then O(TP × SL).
    """
    candidates = _candidate_records(candidate_rows)
    if candidates.empty or path_store is None or path_store.empty:
        return pd.DataFrame()

    tp_values = tuple(sorted({float(v) for v in tp_values if float(v) > 0}))
    sl_values = tuple(sorted({float(v) for v in sl_values if float(v) > 0}))
    if not tp_values or not sl_values:
        return pd.DataFrame()

    horizon = max(1, int(horizon_min))
    notional = max(0.0, float(notional_usdt))
    execution_cost_pct = max(
        0.0,
        float(entry_fee_pct)
        + float(exit_fee_pct)
        + float(entry_slippage_pct)
        + float(exit_slippage_pct),
    )

    metadata = candidates.set_index("candidate_v1_event_key", drop=False)
    wanted = set(metadata.index.astype(str))
    paths = path_store.loc[
        path_store["candidate_v1_event_key"].astype(str).isin(wanted)
    ].copy()
    if paths.empty:
        return pd.DataFrame()

    rows = []
    for event_key, event_path in paths.groupby("candidate_v1_event_key", sort=False):
        event_key = str(event_key)
        if event_key not in metadata.index:
            continue
        candidate = metadata.loc[event_key]
        if isinstance(candidate, pd.DataFrame):
            candidate = candidate.iloc[-1]

        event_path = event_path.sort_values("bar_offset", kind="stable")
        opens = pd.to_numeric(event_path["open"], errors="coerce").to_numpy(dtype=float)
        highs = pd.to_numeric(event_path["high"], errors="coerce").to_numpy(dtype=float)
        lows = pd.to_numeric(event_path["low"], errors="coerce").to_numpy(dtype=float)
        closes = pd.to_numeric(event_path["close"], errors="coerce").to_numpy(dtype=float)
        timestamps = pd.to_numeric(event_path["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
        if len(opens) == 0 or not np.isfinite(opens[0]) or opens[0] <= 0:
            continue

        side = str(candidate["signal"]).upper()
        if side not in {"LONG", "SHORT"}:
            continue
        entry_price = float(opens[0])
        available = int(len(opens))
        max_idx = min(available, horizon) - 1
        if max_idx < 0:
            continue

        if side == "LONG":
            favorable = (highs / entry_price - 1.0) * 100.0
            adverse = (1.0 - lows / entry_price) * 100.0
            time_return = (closes / entry_price - 1.0) * 100.0
        else:
            favorable = (1.0 - lows / entry_price) * 100.0
            adverse = (highs / entry_price - 1.0) * 100.0
            time_return = (1.0 - closes / entry_price) * 100.0

        cumulative_mfe = np.maximum.accumulate(np.maximum(favorable, 0.0))
        cumulative_mae = np.maximum.accumulate(np.maximum(adverse, 0.0))
        tp_first = {
            tp: _first_crossing(favorable, tp, max_idx) for tp in tp_values
        }
        sl_first = {
            sl: _first_crossing(adverse, sl, max_idx) for sl in sl_values
        }
        path_complete = available >= horizon

        for tp_pct in tp_values:
            tp_idx = tp_first[tp_pct]
            for sl_pct in sl_values:
                sl_idx = sl_first[sl_pct]
                outcome = None
                hit_idx = None
                gross = np.nan

                if tp_idx is not None and sl_idx is not None:
                    if tp_idx == sl_idx:
                        outcome = "SL_AMBIGUOUS"
                        hit_idx = sl_idx
                        gross = -float(sl_pct)
                    elif sl_idx < tp_idx:
                        outcome = "SL"
                        hit_idx = sl_idx
                        gross = -float(sl_pct)
                    else:
                        outcome = "TP"
                        hit_idx = tp_idx
                        gross = float(tp_pct)
                elif sl_idx is not None:
                    outcome = "SL"
                    hit_idx = sl_idx
                    gross = -float(sl_pct)
                elif tp_idx is not None:
                    outcome = "TP"
                    hit_idx = tp_idx
                    gross = float(tp_pct)
                elif path_complete:
                    outcome = "TIME_EXIT"
                    hit_idx = horizon - 1
                    gross = float(time_return[hit_idx])
                else:
                    outcome = "PENDING"

                resolved = outcome != "PENDING"
                exit_ts = int(timestamps[hit_idx]) if hit_idx is not None else np.nan
                if outcome == "TP":
                    exit_price = (
                        entry_price * (1.0 + tp_pct / 100.0)
                        if side == "LONG"
                        else entry_price * (1.0 - tp_pct / 100.0)
                    )
                elif outcome in {"SL", "SL_AMBIGUOUS"}:
                    exit_price = (
                        entry_price * (1.0 - sl_pct / 100.0)
                        if side == "LONG"
                        else entry_price * (1.0 + sl_pct / 100.0)
                    )
                elif outcome == "TIME_EXIT":
                    exit_price = float(closes[hit_idx])
                else:
                    exit_price = np.nan

                excursion_idx = hit_idx if hit_idx is not None else max_idx
                mfe = float(cumulative_mfe[excursion_idx])
                mae = float(cumulative_mae[excursion_idx])
                net = (
                    float(gross) - execution_cost_pct
                    if resolved and pd.notna(gross)
                    else np.nan
                )

                rows.append({
                    "candidate_v1_event_key": event_key,
                    "candidate_v1_cohort": str(
                        candidate.get("candidate_v1_cohort", "FORWARD")
                    ),
                    "symbol": str(candidate["symbol"]),
                    "side": side,
                    "reaction_timestamp": int(candidate["retest_timestamp"]),
                    "entry_timestamp": int(candidate["retest_timestamp"]) + 60_000,
                    "entry_price": entry_price,
                    "TP %": float(tp_pct),
                    "SL %": float(sl_pct),
                    "Outcome": outcome,
                    "first_touch_min": float(hit_idx) if hit_idx is not None else np.nan,
                    "exit_timestamp": exit_ts,
                    "exit_price": exit_price,
                    "gross_pnl_pct": gross,
                    "execution_cost_pct": execution_cost_pct if resolved else np.nan,
                    "net_pnl_pct": net,
                    "net_pnl_usdt": (
                        notional * float(net) / 100.0 if pd.notna(net) else np.nan
                    ),
                    "mfe_until_exit_pct": mfe,
                    "mae_until_exit_pct": mae,
                    "observed_bars": available,
                    "path_complete": bool(path_complete),
                })

    return pd.DataFrame(rows)


def build_followup_metrics(
    candidate_rows: pd.DataFrame,
    path_store: pd.DataFrame,
    *,
    threshold_pct: float = 2.0,
    horizons: Iterable[int] = (180, 240, 360),
) -> pd.DataFrame:
    """Compute MAE-before-target and 180/240/360 metrics in one path pass."""
    candidates = _candidate_records(candidate_rows)
    if candidates.empty or path_store is None or path_store.empty:
        return pd.DataFrame()

    horizons = tuple(sorted({int(h) for h in horizons if int(h) > 0}))
    if not horizons:
        return pd.DataFrame()
    max_horizon = max(horizons)
    threshold = float(threshold_pct)

    metadata = candidates.set_index("candidate_v1_event_key", drop=False)
    wanted = set(metadata.index.astype(str))
    paths = path_store.loc[
        path_store["candidate_v1_event_key"].astype(str).isin(wanted)
    ].copy()
    rows = []

    for event_key, event_path in paths.groupby("candidate_v1_event_key", sort=False):
        event_key = str(event_key)
        if event_key not in metadata.index:
            continue
        candidate = metadata.loc[event_key]
        if isinstance(candidate, pd.DataFrame):
            candidate = candidate.iloc[-1]

        try:
            baseline = float(candidate["retest_close"])
            reaction_ts = int(candidate["retest_timestamp"])
            side = str(candidate["signal"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        if baseline <= 0 or side not in {"LONG", "SHORT"}:
            continue

        event_path = event_path.sort_values("bar_offset", kind="stable")
        highs = pd.to_numeric(event_path["high"], errors="coerce").to_numpy(dtype=float)
        lows = pd.to_numeric(event_path["low"], errors="coerce").to_numpy(dtype=float)
        timestamps = pd.to_numeric(event_path["timestamp"], errors="coerce").to_numpy(dtype=np.int64)
        available = min(len(highs), max_horizon)
        if available <= 0:
            continue

        if side == "LONG":
            favorable = (highs[:available] / baseline - 1.0) * 100.0
            adverse = (1.0 - lows[:available] / baseline) * 100.0
        else:
            favorable = (1.0 - lows[:available] / baseline) * 100.0
            adverse = (highs[:available] / baseline - 1.0) * 100.0

        # Extended first hit is observational up to the largest requested
        # horizon. Official MAE-before-target remains strictly tied to the
        # frozen 180m Candidate V1 horizon.
        hit_positions = np.flatnonzero(favorable >= threshold)
        first_hit_idx = int(hit_positions[0]) if len(hit_positions) else None
        first_hit_min_360 = (
            float((int(timestamps[first_hit_idx]) - reaction_ts) / 60_000.0)
            if first_hit_idx is not None
            else np.nan
        )

        official_horizon = 180
        official_observed = min(available, official_horizon)
        official_hits = np.flatnonzero(
            favorable[:official_observed] >= threshold
        )
        official_hit_idx = (
            int(official_hits[0]) if len(official_hits) else None
        )
        official_first_hit_min = (
            float(
                (int(timestamps[official_hit_idx]) - reaction_ts)
                / 60_000.0
            )
            if official_hit_idx is not None
            else np.nan
        )

        if official_hit_idx is not None:
            adverse_end = official_hit_idx - 1
            mae_scope = "STRICT_BEFORE_HIT_CANDLE"
        else:
            adverse_end = official_observed - 1
            mae_scope = "OBSERVED_TO_HORIZON_OR_GAP"
        if adverse_end < 0:
            mae_before = 0.0
            adverse_bars = 0
        else:
            mae_before = max(0.0, float(np.nanmax(adverse[: adverse_end + 1])))
            adverse_bars = adverse_end + 1

        metrics = {
            "candidate_v1_event_key": event_key,
            "candidate_v1_mae_before_target_pct": float(mae_before),
            "candidate_v1_mae_scope": mae_scope,
            "candidate_v1_first_hit_min_path": official_first_hit_min,
            "candidate_v1_observed_path_min": int(official_observed),
            "candidate_v1_adverse_bars": int(adverse_bars),
            "candidate_v1_first_hit_2pct_min_360": first_hit_min_360,
            "candidate_v1_observed_followup_min": int(available),
        }

        if pd.notna(first_hit_min_360):
            if first_hit_min_360 <= 180:
                bucket = "<=180m"
            elif first_hit_min_360 <= 240:
                bucket = "181-240m"
            elif first_hit_min_360 <= 360:
                bucket = "241-360m"
            else:
                bucket = ">360m"
        elif available >= 360:
            bucket = "NO_HIT_360"
        else:
            bucket = "PENDING_360"
        metrics["candidate_v1_hit_timing_bucket"] = bucket

        cumulative_mfe = np.maximum.accumulate(np.maximum(favorable, 0.0))
        cumulative_mae = np.maximum.accumulate(np.maximum(adverse, 0.0))

        for horizon in horizons:
            observed = min(available, horizon)
            if observed <= 0:
                continue
            end = observed - 1
            hit_by_horizon = first_hit_idx is not None and first_hit_idx <= end
            if hit_by_horizon:
                outcome = "HIT"
            elif available >= horizon:
                outcome = "NO_HIT"
            else:
                outcome = "PENDING"
            metrics[f"candidate_v1_outcome_{horizon}m"] = outcome
            metrics[f"candidate_v1_mfe_{horizon}m_pct"] = float(cumulative_mfe[end])
            metrics[f"candidate_v1_mae_{horizon}m_pct"] = float(cumulative_mae[end])
            metrics[f"candidate_v1_observed_{horizon}m_min"] = int(observed)

        rows.append(metrics)

    return pd.DataFrame(rows)
