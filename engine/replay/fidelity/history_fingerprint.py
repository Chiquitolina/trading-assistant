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
    global_hash_input = {}

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

            pair_hash = _sha256(
                normalized
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
                "sha256": pair_hash,
            }

            global_hash_input[key] = (
                pair_hash
            )

    return {
        "pairs": pairs,
        "global_sha256": _sha256(
            global_hash_input
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