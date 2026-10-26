#!/usr/bin/env python3
"""Fetch public Binance USD-M Futures 30m historical OHLCV for HLL V2.1.

Never reads or writes LIVE data, trading systems, existing SQLite archives,
order APIs, secrets, Redis, PM2, or dashboard code.

Writes ONLY two independent CSVs in a new --output-dir, suitable for
  horizontal_level_lifecycle_v21.py --candles-dir <output-dir>

Uses Binance public GET /fapi/v1/klines, no API keys. UTC interval is [start, end).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://fapi.binance.com/fapi/v1/klines"
TF = 30 * 60 * 1000
FIELDS = ["timestamp", "open", "high", "low", "close", "volume", "quoteVolume"]
DEFAULT_OUTPUT = "reports/horizontal_level_lifecycle_v21/reference_candles_init_tree_20261001_20261025"


def utc_ms(s: str) -> int:
    if not s.endswith("Z"):
        raise ValueError("UTC timestamp must end with Z")
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(d.timestamp() * 1000)


def fetch_page(symbol: str, start: int, end: int, limit: int = 1000):
    params = urlencode({
        "symbol": symbol, "interval": "30m", "startTime": start,
        "endTime": end - 1, "limit": limit,
    })
    req = Request(BASE + "?" + params, headers={"User-Agent": "HLL-V21-HistoricalResearch/1.0"})
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=25) as response:
                body = response.read()
            data = json.loads(body)
            if not isinstance(data, list):
                raise ValueError(f"Unexpected Binance response: {data!r}")
            return data
        except HTTPError as exc:
            raw = exc.read(300).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Binance HTTP {exc.code} for {symbol}: {raw}")
            if exc.code not in (418, 429, 500, 502, 503, 504):
                raise last_error from exc
        except (URLError, TimeoutError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(1 + 2 * attempt)
    raise RuntimeError(f"Could not fetch {symbol}: {last_error}")


def download(symbol: str, start: int, end: int):
    if end <= start or start % TF or end % TF:
        raise ValueError("Require aligned UTC 30m start and end (start < end)")
    cursor = start
    result = {}
    pages = 0
    while cursor < end:
        batch = fetch_page(symbol, cursor, end, 1000)
        pages += 1
        if not batch:
            break
        stamps = [int(x[0]) for x in batch]
        if stamps != sorted(set(stamps)):
            raise ValueError(f"Unsorted/duplicate Binance page for {symbol}")
        for k in batch:
            t = int(k[0])
            if not (start <= t < end) or t % TF:
                raise ValueError(f"Out-of-range or misaligned candle: {symbol} {t}")
            if t in result:
                raise ValueError(f"Duplicate candle across pages: {symbol} {t}")
            fields = [t, str(k[1]), str(k[2]), str(k[3]), str(k[4]), str(k[5]), str(k[7])]
            o, h, lo, c, v = map(float, fields[1:6])
            if min(o, h, lo, c) <= 0 or v < 0 or h < max(o, c, lo) or lo > min(o, c, h):
                raise ValueError(f"Invalid OHLCV for {symbol} {t}")
            result[t] = fields
        new_cursor = stamps[-1] + TF
        if new_cursor <= cursor:
            raise ValueError(f"Non-progressing Binance paging: {symbol}")
        cursor = new_cursor
    return [result[t] for t in sorted(result)], pages


def write_new_csv(path: Path, rows):
    """Fail closed: never replace an existing file, even a partial archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hll-", suffix=".csv", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            out = csv.writer(stream)
            out.writerow(FIELDS)
            out.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive destination creation, so an existing file is never overwritten.
        os.link(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    p = argparse.ArgumentParser(description="Fetch public historical Binance Futures 30m candles for INIT/TREE comparison")
    p.add_argument("--symbols", default="INITUSDT,TREEUSDT")
    p.add_argument("--start-utc", default="2026-10-01T00:00:00Z")
    p.add_argument("--end-utc", default="2026-10-25T00:00:00Z")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT, type=Path)
    args = p.parse_args(argv)
    start, end = utc_ms(args.start_utc), utc_ms(args.end_utc)
    if start % TF or end % TF or start >= end:
        p.error("start/end must be aligned UTC 30m boundaries, start < end")
    if end > int(time.time() * 1000) - TF:
        p.error("End date must be safely in the past; no forming candles")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols or len(set(symbols)) != len(symbols) or any(not re.fullmatch(r"[A-Z0-9]{3,20}", s) for s in symbols):
        p.error("invalid/duplicate --symbols")
    outdir = args.output_dir.resolve()
    if outdir.exists() and any(outdir.iterdir()):
        p.error(f"Output directory is not empty: {outdir}. Use a new --output-dir")
    # Fetch and validate BOTH symbols before creating any output files.
    # A temporary API failure on the second symbol should not leave a partial set.
    downloaded = []
    for symbol in symbols:
        rows, pages = download(symbol, start, end)
        if not rows:
            raise SystemExit(f"No historical 30m candles from Binance Futures for {symbol}; cannot fabricate data")
        downloaded.append((symbol, rows, pages))
        print("HLL_REFERENCE_FETCHED", json.dumps(dict(symbol=symbol, bars30=len(rows), pages=pages)), flush=True)

    manifest = []
    for symbol, rows, pages in downloaded:
        stamps = [int(r[0]) for r in rows]
        gaps = sum(b - a != TF for a, b in zip(stamps, stamps[1:]))
        first = datetime.fromtimestamp(stamps[0] / 1000, timezone.utc).isoformat()
        last = datetime.fromtimestamp(stamps[-1] / 1000, timezone.utc).isoformat()
        file = outdir / f"{symbol}_30m.csv"
        sha = write_new_csv(file, rows)
        report = dict(symbol=symbol, bars30=len(rows), gaps30=gaps, pages=pages,
                      first_open_utc=first, last_open_utc=last, sha256=sha, path=str(file))
        manifest.append(report)
        print("HLL_REFERENCE_DOWNLOAD", json.dumps(report), flush=True)
    # Keep this beside CSVs; Archive ignores it.
    mpath = outdir / "source_manifest.json"
    with mpath.open("x", encoding="utf-8") as fp:
        json.dump(dict(source="Binance USD-M Futures public /fapi/v1/klines",
                       timeframe="30m", start_utc=args.start_utc,
                       end_utc_exclusive=args.end_utc, symbols=manifest), fp, indent=2)
    print("HLL_REFERENCE_READY", str(outdir), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
