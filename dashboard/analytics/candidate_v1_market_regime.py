"""Persisted causal 4h market-regime context for Candidate V1 research.

The Candidate signal itself is not changed.  For each Candidate REACTION we
map the signal-known timestamp to the latest *fully closed* 4h market boundary,
rebuild the same cross-sectional Market Flow primitives from Redis history,
and persist the resulting snapshot.  Later dashboard interactions only read the
pickle store; they do not rescan 4h histories.

The calculations intentionally mirror the live MarketFlowAnalyzer semantics:
- return_pct_4h = current 4h close / previous 4h close - 1
- relative_volume_4h = current quote volume / median(previous 42 quote volumes)
- cross-sectional percentile ranks use average tie ranks over [0, 100]
- market breadth = positive-return symbols / valid symbols

Sector aggregates mirror MarketSectorFlowAnalyzer using a static primary-sector
mapping supplied by the dashboard.  Sector assignment is metadata; all market
values are reconstructed causally at the Candidate timestamp.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from engine.live.data.redis_market_data_protocol import history_key


FOUR_H_MS = 4 * 60 * 60 * 1000
DEFAULT_BASELINE_CANDLES = 42
DEFAULT_MAX_HISTORY = 500

_STORE_LOCK = threading.Lock()
_STORE_CACHE: dict[str, tuple[float | None, pd.DataFrame]] = {}

STORE_COLUMNS = [
    "flow_candle_timestamp",
    "flow_close_timestamp",
    "symbol",
    "return_pct_4h",
    "quote_volume_4h",
    "median_quote_volume_4h",
    "relative_volume_4h",
    "return_rank_pct_4h",
    "volume_rank_pct_4h",
    "symbol_strength_vs_btc_4h",
    "market_breadth_4h",
    "btc_return_pct_4h",
    "positive_symbols",
    "valid_universe_size",
    "configured_universe_size",
    "coverage_pct",
    "primary_sector",
    "sector_return_pct_4h",
    "sector_breadth_4h",
    "sector_relative_volume_4h",
    "sector_return_rank_pct_4h",
    "sector_strength_vs_btc_4h",
    "symbol_strength_vs_sector_4h",
    "sector_valid_symbols",
    "sector_configured_symbols",
    "sector_coverage_pct",
    "universe_signature",
]


def _empty_store() -> pd.DataFrame:
    return pd.DataFrame(columns=STORE_COLUMNS)


def _mtime(path: Path) -> float | None:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return None


def load_market_regime_store(store_path: str | Path) -> pd.DataFrame:
    path = Path(store_path)
    key = str(path.resolve())
    mtime = _mtime(path)
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

    for column in STORE_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan

    if not frame.empty:
        for column in (
            "flow_candle_timestamp",
            "flow_close_timestamp",
            "return_pct_4h",
            "quote_volume_4h",
            "median_quote_volume_4h",
            "relative_volume_4h",
            "return_rank_pct_4h",
            "volume_rank_pct_4h",
            "symbol_strength_vs_btc_4h",
            "market_breadth_4h",
            "btc_return_pct_4h",
            "positive_symbols",
            "valid_universe_size",
            "configured_universe_size",
            "coverage_pct",
            "sector_return_pct_4h",
            "sector_breadth_4h",
            "sector_relative_volume_4h",
            "sector_return_rank_pct_4h",
            "sector_strength_vs_btc_4h",
            "symbol_strength_vs_sector_4h",
            "sector_valid_symbols",
            "sector_configured_symbols",
            "sector_coverage_pct",
        ):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        frame = (
            frame.dropna(subset=["flow_candle_timestamp", "symbol"])
            .sort_values(["flow_candle_timestamp", "symbol"], kind="stable")
            .drop_duplicates(
                subset=["flow_candle_timestamp", "symbol"],
                keep="last",
            )
            .reset_index(drop=True)
        )

    _STORE_CACHE[key] = (mtime, frame.copy())
    return frame


def _atomic_write(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frame.to_pickle(tmp)
    os.replace(tmp, path)


def _candidate_records(candidate_rows: pd.DataFrame) -> pd.DataFrame:
    if candidate_rows is None or candidate_rows.empty:
        return pd.DataFrame()

    required = {"candidate_v1_event_key", "symbol", "retest_timestamp"}
    if not required.issubset(candidate_rows.columns):
        return pd.DataFrame()

    result = candidate_rows.copy()
    result["candidate_v1_event_key"] = result["candidate_v1_event_key"].astype(str)
    result["symbol"] = result["symbol"].astype(str).str.upper()
    result["retest_timestamp"] = pd.to_numeric(
        result["retest_timestamp"], errors="coerce"
    )
    result = result.dropna(subset=["retest_timestamp"])
    if result.empty:
        return result

    result["reaction_known_timestamp"] = (
        result["retest_timestamp"].astype("int64") + 60_000
    )
    # Same fully-closed convention used by the live market-data service.
    result["flow_candle_timestamp"] = (
        (result["reaction_known_timestamp"] // FOUR_H_MS) * FOUR_H_MS
        - FOUR_H_MS
    ).astype("int64")

    return (
        result.drop_duplicates(subset=["candidate_v1_event_key"], keep="last")
        .reset_index(drop=True)
    )


def _extract_quote_volume(candle: Mapping) -> float | None:
    value = (
        candle.get("quoteVolume")
        or candle.get("quote_volume")
        or candle.get("quote_asset_volume")
    )
    if value is not None:
        try:
            value = float(value)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    try:
        fallback = float(candle["volume"]) * float(candle["close"])
        if fallback > 0:
            return fallback
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _percentile_ranks(values: Sequence[float]) -> list[float]:
    values = [float(value) for value in values]
    if not values:
        return []
    if len(values) == 1:
        return [100.0]

    sorted_values = sorted(values)
    denominator = len(values) - 1
    ranks: list[float] = []
    for value in values:
        left = bisect.bisect_left(sorted_values, value)
        right = bisect.bisect_right(sorted_values, value)
        average_index = (left + right - 1) / 2.0
        ranks.append(round(average_index / denominator * 100.0, 4))
    return ranks


def _universe_signature(symbols: Sequence[str]) -> str:
    normalized = tuple(sorted({str(symbol).upper() for symbol in symbols if symbol}))
    # Stable, human-readable enough for invalidation without Python hash salt.
    return f"n={len(normalized)}|first={','.join(normalized[:5])}|last={','.join(normalized[-5:])}"


def _latest_4h_timestamp(redis_client) -> int | None:
    try:
        raw = redis_client.lindex(history_key("BTCUSDT", "4h"), -1)
        if not raw:
            return None
        candle = json.loads(raw)
        return int(candle["timestamp"])
    except Exception:
        return None


def _history_size_for_boundaries(
    boundaries: Sequence[int],
    latest_available: int | None,
    baseline_candles: int,
    max_history: int,
) -> int:
    if not boundaries:
        return min(max_history, baseline_candles + 4)

    oldest = int(min(boundaries))
    if latest_available is None:
        return int(max_history)

    age_bars = max(0, int((int(latest_available) - oldest) // FOUR_H_MS))
    needed = int(baseline_candles) + age_bars + 5
    return max(int(baseline_candles) + 3, min(int(max_history), needed))


def _parse_histories(
    universe_symbols: Sequence[str],
    raw_histories: Sequence[Sequence[str]],
) -> dict[str, tuple[list[dict], dict[int, int]]]:
    result: dict[str, tuple[list[dict], dict[int, int]]] = {}
    for symbol, raw_history in zip(universe_symbols, raw_histories):
        candles: list[dict] = []
        for raw in raw_history or []:
            try:
                candle = json.loads(raw) if isinstance(raw, str) else dict(raw)
                candle["timestamp"] = int(candle["timestamp"])
                candles.append(candle)
            except Exception:
                continue
        candles.sort(key=lambda item: int(item["timestamp"]))
        dedup: dict[int, dict] = {int(item["timestamp"]): item for item in candles}
        candles = [dedup[key] for key in sorted(dedup)]
        index_by_ts = {int(item["timestamp"]): idx for idx, item in enumerate(candles)}
        result[str(symbol).upper()] = (candles, index_by_ts)
    return result


def _symbol_metrics_at(
    candles: list[dict],
    index_by_ts: dict[int, int],
    boundary: int,
    baseline_candles: int,
) -> dict | None:
    idx = index_by_ts.get(int(boundary))
    if idx is None or idx < int(baseline_candles) or idx < 1:
        return None

    current = candles[idx]
    previous = candles[idx - 1]
    try:
        previous_close = float(previous["close"])
        current_close = float(current["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if previous_close <= 0:
        return None

    baseline_rows = candles[idx - int(baseline_candles) : idx]
    baseline_volumes = [_extract_quote_volume(item) for item in baseline_rows]
    if len(baseline_volumes) != int(baseline_candles) or any(
        value is None for value in baseline_volumes
    ):
        return None

    current_quote_volume = _extract_quote_volume(current)
    if current_quote_volume is None:
        return None
    median_quote_volume = float(median([float(v) for v in baseline_volumes]))
    if median_quote_volume <= 0:
        return None

    return {
        "return_pct_4h": round((current_close / previous_close - 1.0) * 100.0, 4),
        "quote_volume_4h": round(float(current_quote_volume), 4),
        "median_quote_volume_4h": round(float(median_quote_volume), 4),
        "relative_volume_4h": round(
            float(current_quote_volume) / float(median_quote_volume), 4
        ),
    }


def _build_boundary_snapshot(
    boundary: int,
    histories: dict[str, tuple[list[dict], dict[int, int]]],
    universe_symbols: Sequence[str],
    sector_map: Mapping[str, str] | None,
    baseline_candles: int,
    min_sector_symbols: int,
    universe_signature: str,
) -> pd.DataFrame:
    symbol_metrics: dict[str, dict] = {}
    for symbol in universe_symbols:
        candles, index_by_ts = histories.get(str(symbol).upper(), ([], {}))
        metrics = _symbol_metrics_at(
            candles,
            index_by_ts,
            int(boundary),
            int(baseline_candles),
        )
        if metrics is not None:
            symbol_metrics[str(symbol).upper()] = metrics

    valid_symbols = list(symbol_metrics)
    if not valid_symbols:
        return pd.DataFrame(columns=STORE_COLUMNS)

    returns = [symbol_metrics[symbol]["return_pct_4h"] for symbol in valid_symbols]
    rel_volumes = [
        symbol_metrics[symbol]["relative_volume_4h"] for symbol in valid_symbols
    ]
    return_ranks = _percentile_ranks(returns)
    volume_ranks = _percentile_ranks(rel_volumes)
    for idx, symbol in enumerate(valid_symbols):
        symbol_metrics[symbol]["return_rank_pct_4h"] = return_ranks[idx]
        symbol_metrics[symbol]["volume_rank_pct_4h"] = volume_ranks[idx]

    valid_n = len(valid_symbols)
    configured_n = len(universe_symbols)
    positive_n = int(sum(float(value) > 0 for value in returns))
    breadth = positive_n / valid_n * 100.0 if valid_n else np.nan
    coverage = valid_n / configured_n * 100.0 if configured_n else 0.0
    btc_return = (
        symbol_metrics.get("BTCUSDT", {}).get("return_pct_4h", np.nan)
    )

    normalized_sector_map = {
        str(symbol).upper(): str(sector)
        for symbol, sector in (sector_map or {}).items()
        if symbol and sector
    }
    configured_sector_sizes: dict[str, int] = {}
    for symbol in universe_symbols:
        sector = normalized_sector_map.get(str(symbol).upper())
        if sector and sector != "Other" and str(symbol).upper() != "BTCUSDT":
            configured_sector_sizes[sector] = configured_sector_sizes.get(sector, 0) + 1

    sector_rows: dict[str, list[tuple[str, dict]]] = {}
    for symbol, metrics in symbol_metrics.items():
        sector = normalized_sector_map.get(symbol)
        if not sector or sector == "Other" or symbol == "BTCUSDT":
            continue
        sector_rows.setdefault(sector, []).append((symbol, metrics))

    sector_metrics: dict[str, dict] = {}
    for sector, configured_size in configured_sector_sizes.items():
        rows = sector_rows.get(sector, [])
        valid_size = len(rows)
        sector_coverage = (
            valid_size / configured_size * 100.0 if configured_size else 0.0
        )
        if valid_size < int(min_sector_symbols):
            continue
        sector_returns = [float(metrics["return_pct_4h"]) for _, metrics in rows]
        sector_positive = sum(value > 0 for value in sector_returns)
        sector_return = float(median(sector_returns))
        quote_total = sum(float(metrics["quote_volume_4h"]) for _, metrics in rows)
        baseline_total = sum(
            float(metrics["median_quote_volume_4h"]) for _, metrics in rows
        )
        sector_rel_volume = quote_total / baseline_total if baseline_total > 0 else np.nan
        sector_metrics[sector] = {
            "sector_return_pct_4h": round(sector_return, 4),
            "sector_breadth_4h": round(sector_positive / valid_size * 100.0, 4),
            "sector_relative_volume_4h": (
                round(float(sector_rel_volume), 4)
                if np.isfinite(sector_rel_volume)
                else np.nan
            ),
            "sector_strength_vs_btc_4h": (
                round(sector_return - float(btc_return), 4)
                if pd.notna(btc_return)
                else np.nan
            ),
            "sector_valid_symbols": int(valid_size),
            "sector_configured_symbols": int(configured_size),
            "sector_coverage_pct": round(float(sector_coverage), 4),
        }

    if sector_metrics:
        sector_names = list(sector_metrics)
        sector_ranks = _percentile_ranks(
            [sector_metrics[name]["sector_return_pct_4h"] for name in sector_names]
        )
        for idx, name in enumerate(sector_names):
            sector_metrics[name]["sector_return_rank_pct_4h"] = sector_ranks[idx]

    rows: list[dict] = []
    for symbol, metrics in symbol_metrics.items():
        primary_sector = normalized_sector_map.get(symbol)
        sector = sector_metrics.get(primary_sector, {}) if primary_sector else {}
        symbol_return = float(metrics["return_pct_4h"])
        sector_return = sector.get("sector_return_pct_4h", np.nan)
        row = {
            "flow_candle_timestamp": int(boundary),
            "flow_close_timestamp": int(boundary) + FOUR_H_MS,
            "symbol": symbol,
            **metrics,
            "symbol_strength_vs_btc_4h": (
                round(symbol_return - float(btc_return), 4)
                if pd.notna(btc_return)
                else np.nan
            ),
            "market_breadth_4h": round(float(breadth), 4),
            "btc_return_pct_4h": btc_return,
            "positive_symbols": int(positive_n),
            "valid_universe_size": int(valid_n),
            "configured_universe_size": int(configured_n),
            "coverage_pct": round(float(coverage), 4),
            "primary_sector": primary_sector,
            "sector_return_pct_4h": sector.get("sector_return_pct_4h", np.nan),
            "sector_breadth_4h": sector.get("sector_breadth_4h", np.nan),
            "sector_relative_volume_4h": sector.get("sector_relative_volume_4h", np.nan),
            "sector_return_rank_pct_4h": sector.get("sector_return_rank_pct_4h", np.nan),
            "sector_strength_vs_btc_4h": sector.get("sector_strength_vs_btc_4h", np.nan),
            "symbol_strength_vs_sector_4h": (
                round(symbol_return - float(sector_return), 4)
                if pd.notna(sector_return)
                else np.nan
            ),
            "sector_valid_symbols": sector.get("sector_valid_symbols", np.nan),
            "sector_configured_symbols": sector.get("sector_configured_symbols", np.nan),
            "sector_coverage_pct": sector.get("sector_coverage_pct", np.nan),
            "universe_signature": universe_signature,
        }
        rows.append(row)

    result = pd.DataFrame(rows)
    for column in STORE_COLUMNS:
        if column not in result.columns:
            result[column] = np.nan
    return result[STORE_COLUMNS]


def refresh_market_regime_store(
    candidate_rows: pd.DataFrame,
    *,
    store_path: str | Path,
    redis_client,
    universe_symbols: Sequence[str],
    sector_map: Mapping[str, str] | None = None,
    baseline_candles: int = DEFAULT_BASELINE_CANDLES,
    min_sector_symbols: int = 3,
    max_history: int = DEFAULT_MAX_HISTORY,
    force: bool = False,
) -> pd.DataFrame:
    """Build only missing 4h boundaries and persist all symbol metrics per boundary."""
    candidates = _candidate_records(candidate_rows)
    path = Path(store_path)
    universe = tuple(sorted({str(symbol).upper() for symbol in universe_symbols if symbol}))
    if candidates.empty or not universe:
        return load_market_regime_store(path)

    requested = sorted(
        {int(value) for value in candidates["flow_candle_timestamp"].dropna().tolist()}
    )
    signature = _universe_signature(universe)

    with _STORE_LOCK:
        store = load_market_regime_store(path)
        existing_boundaries = set()
        if not store.empty:
            valid_store = store.loc[
                store["universe_signature"].astype(str).eq(signature)
            ]
            existing_boundaries = set(
                pd.to_numeric(
                    valid_store["flow_candle_timestamp"], errors="coerce"
                ).dropna().astype("int64")
            )

        missing = requested if force else [b for b in requested if b not in existing_boundaries]
        if not missing:
            return store.copy()

        latest = _latest_4h_timestamp(redis_client)
        history_size = _history_size_for_boundaries(
            missing,
            latest,
            int(baseline_candles),
            int(max_history),
        )

        pipeline = redis_client.pipeline(transaction=False)
        for symbol in universe:
            pipeline.lrange(history_key(symbol, "4h"), -int(history_size), -1)
        raw_histories = pipeline.execute()
        histories = _parse_histories(universe, raw_histories)

        additions: list[pd.DataFrame] = []
        for boundary in missing:
            snapshot = _build_boundary_snapshot(
                int(boundary),
                histories,
                universe,
                sector_map,
                int(baseline_candles),
                int(min_sector_symbols),
                signature,
            )
            if not snapshot.empty:
                additions.append(snapshot)

        if not additions:
            return store.copy()

        added = pd.concat(additions, ignore_index=True, sort=False)
        if force and not store.empty:
            store = store.loc[
                ~pd.to_numeric(
                    store["flow_candle_timestamp"], errors="coerce"
                ).isin(missing)
            ].copy()

        combined = pd.concat([store, added], ignore_index=True, sort=False)
        combined = (
            combined.sort_values(["flow_candle_timestamp", "symbol"], kind="stable")
            .drop_duplicates(
                subset=["flow_candle_timestamp", "symbol"], keep="last"
            )
            .reset_index(drop=True)
        )
        for column in STORE_COLUMNS:
            if column not in combined.columns:
                combined[column] = np.nan
        combined = combined[STORE_COLUMNS]
        _atomic_write(combined, path)
        _STORE_CACHE[str(path.resolve())] = (_mtime(path), combined.copy())
        return combined.copy()


def attach_candidate_market_context(
    candidate_rows: pd.DataFrame,
    snapshot_store: pd.DataFrame,
) -> pd.DataFrame:
    """Return one causal market-context row per Candidate event key."""
    candidates = _candidate_records(candidate_rows)
    if candidates.empty:
        return pd.DataFrame()

    base_cols = [
        "candidate_v1_event_key",
        "symbol",
        "reaction_known_timestamp",
        "flow_candle_timestamp",
    ]
    base = candidates[base_cols].copy()
    if snapshot_store is None or snapshot_store.empty:
        return base

    store = snapshot_store.copy()
    store["symbol"] = store["symbol"].astype(str).str.upper()
    store["flow_candle_timestamp"] = pd.to_numeric(
        store["flow_candle_timestamp"], errors="coerce"
    )

    context_cols = [
        column
        for column in STORE_COLUMNS
        if column not in {"symbol", "flow_candle_timestamp", "universe_signature"}
    ]
    result = base.merge(
        store[["flow_candle_timestamp", "symbol"] + context_cols],
        on=["flow_candle_timestamp", "symbol"],
        how="left",
        validate="many_to_one",
    )
    result["market_context_available"] = pd.to_numeric(
        result.get("market_breadth_4h"), errors="coerce"
    ).notna()
    return result
