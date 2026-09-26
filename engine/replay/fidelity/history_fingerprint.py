import hashlib
import json
import math

from datetime import datetime, timezone
from pathlib import Path


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value):
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _normalize_number(value):
    if value is None:
        return None

    number = float(value)

    if math.isnan(number):
        return "__NaN__"

    if math.isinf(number):
        if number > 0:
            return "__Infinity__"

        return "__-Infinity__"

    return number


def _normalize_candle(candle):
    return {
        "timestamp": int(
            candle["timestamp"]
        ),
        "open": _normalize_number(
            candle.get("open")
        ),
        "high": _normalize_number(
            candle.get("high")
        ),
        "low": _normalize_number(
            candle.get("low")
        ),
        "close": _normalize_number(
            candle.get("close")
        ),
        "volume": _normalize_number(
            candle.get("volume")
        ),
        "quoteVolume": _normalize_number(
            candle.get("quoteVolume")
        ),
    }


def build_history_fingerprint(
    buffer,
    symbols,
    timeframes,
):
    pairs = {}

    global_full_hash_input = {}
    global_core_hash_input = {}
    global_timestamp_hash_input = {}

    for symbol in sorted(symbols):
        symbol = symbol.upper()

        for tf in sorted(timeframes):
            candles = buffer.get_candles(
                symbol,
                tf,
            )

            normalized = [
                _normalize_candle(candle)
                for candle in candles
            ]

            # ==========================================
            # FULL
            # Includes quoteVolume.
            # ==========================================

            full_hash = _sha256(
                normalized
            )

            # ==========================================
            # CORE
            # Strategy-relevant market data.
            # Excludes quoteVolume.
            # ==========================================

            core = [
                {
                    "timestamp": candle["timestamp"],
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                    "volume": candle["volume"],
                }
                for candle in normalized
            ]

            core_hash = _sha256(
                core
            )

            # ==========================================
            # TIMESTAMPS
            # Pure sequence / gap detection.
            # ==========================================

            timestamps = [
                candle["timestamp"]
                for candle in normalized
            ]

            timestamp_hash = _sha256(
                timestamps
            )

            key = f"{symbol}|{tf}"

            pairs[key] = {
                "symbol": symbol,
                "timeframe": tf,
                "count": len(normalized),

                "first_timestamp": (
                    normalized[0]["timestamp"]
                    if normalized
                    else None
                ),

                "last_timestamp": (
                    normalized[-1]["timestamp"]
                    if normalized
                    else None
                ),

                # backward compatibility
                "sha256": full_hash,

                "full_sha256": (
                    full_hash
                ),

                "core_sha256": (
                    core_hash
                ),

                "timestamps_sha256": (
                    timestamp_hash
                ),
            }

            global_full_hash_input[
                key
            ] = full_hash

            global_core_hash_input[
                key
            ] = core_hash

            global_timestamp_hash_input[
                key
            ] = timestamp_hash

    return {
        "pairs": pairs,

        # backward compatibility
        "global_sha256": _sha256(
            global_full_hash_input
        ),

        "global_full_sha256": _sha256(
            global_full_hash_input
        ),

        "global_core_sha256": _sha256(
            global_core_hash_input
        ),

        "global_timestamps_sha256": _sha256(
            global_timestamp_hash_input
        ),
    }


def write_history_fingerprint(
    buffer,
    symbols,
    timeframes,
    mode,
    branch_label=None,
    output_dir="history_fingerprints",
):
    fingerprint = (
        build_history_fingerprint(
            buffer=buffer,
            symbols=symbols,
            timeframes=timeframes,
        )
    )

    now = datetime.now(
        timezone.utc
    )

    payload = {
        "mode": mode,
        "created_at_utc": (
            now.isoformat()
        ),
        "branch_label": branch_label,
        **fingerprint,
    }

    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = now.strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    path = (
        output_dir
        / f"{mode}_{timestamp}.json"
    )

    path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return path, payload