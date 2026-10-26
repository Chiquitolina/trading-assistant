#!/usr/bin/env python3
"""Backfill exactly the 1m candles V2.2 needs, without modifying LIVE or SQLite.

Reads frozen V2.2 signals.csv and calculates merged half-open minute windows
[known_closed_ts + 1m, known_closed_ts + (1+max_horizon)m).
Writes SYMBOL_1m.csv for horizontal_breakout_lab.Archive, in a NEW output dir.
The CSV intentionally may have gaps BETWEEN requested windows, never WITHIN.
No credentials, Redis, PM2, database writes, futures orders or dashboard changes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = 'https://fapi.binance.com/fapi/v1/klines'
M = 60_000
FIELDS = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume']


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_windows(path: Path, horizon: int, symbols=None):
    if not path.is_file():
        raise FileNotFoundError(f'Frozen signals.csv not found: {path}')
    raw = defaultdict(list)
    with path.open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if not {'symbol', 'event_id', 'known_closed_ts'}.issubset(set(reader.fieldnames or [])):
            raise ValueError('Expected V2.2 signals.csv with symbol/event_id/known_closed_ts')
        for row in reader:
            sym = row['symbol'].strip().upper()
            if symbols is not None and sym not in symbols:
                continue
            if not re.fullmatch(r'[A-Z0-9]{3,25}', sym):
                raise ValueError(f'Invalid symbol {sym!r}')
            t = int(row['known_closed_ts'])
            if t % M:
                raise ValueError(f'Non-minute-aligned event: {sym} {t}')
            first = t + M  # V2.2 first executable one-minute OPEN
            last = first + horizon * M  # EXCLUSIVE
            raw[sym].append((first, last))
    if not raw:
        raise ValueError('No signals matched --symbols')
    return {sym: merge_windows(vals) for sym, vals in sorted(raw.items())}


def merge_windows(windows):
    result = []
    for start, end in sorted(windows):
        if start >= end or start % M or end % M:
            raise ValueError('Invalid 1m request window')
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def expected_minutes(windows):
    return sum((b - a) // M for a, b in windows)


def fetch_page(symbol, start, end, limit=1000, sleep_s=0.40):
    params = urlencode(dict(symbol=symbol, interval='1m', startTime=start,
                             endTime=end - 1, limit=limit))
    req = Request(BASE + '?' + params, headers={'User-Agent': 'HLL-V22-Research-Candle-Backfill/1.0'})
    err = None
    for attempt in range(5):
        try:
            with urlopen(req, timeout=30) as response:
                value = json.load(response)
            if not isinstance(value, list):
                raise ValueError(f'Unexpected Binance payload: {str(value)[:180]}')
            if sleep_s > 0:
                time.sleep(sleep_s)
            return value
        except HTTPError as exc:
            excerpt = exc.read(250).decode('utf-8', errors='replace')
            err = RuntimeError(f'HTTP {exc.code} for {symbol}: {excerpt}')
            if exc.code == 418:
                raise RuntimeError('Binance IP ban 418: stop download; do not retry immediately') from exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise err from exc
            if exc.code == 429:
                try:
                    backoff = min(120, max(30, int(exc.headers.get('Retry-After', '60'))))
                except (TypeError, ValueError):
                    backoff = 60
            else:
                backoff = min(35, 2 ** (attempt + 1))
        except (URLError, TimeoutError, OSError) as exc:
            err = exc
            backoff = min(35, 2 ** (attempt + 1))
        if attempt < 4:
            time.sleep(backoff)
    raise RuntimeError(f'Klines unavailable for {symbol}: {err}')


def checked_row(k, expected, sym):
    if not isinstance(k, list) or len(k) < 8:
        raise ValueError(f'Invalid Binance kline format {sym} @ {expected}')
    ts = int(k[0])
    if ts != expected:
        raise ValueError(f'Missing/unsorted minute for {sym}: expected {iso(expected)}, got {iso(ts)}')
    o, hi, lo, c, v, q = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7]))
    if (not all(map(math.isfinite, (o, hi, lo, c, v, q))) or
            min(o, hi, lo, c) <= 0 or min(v, q) < 0 or
            hi < max(o, lo, c) or lo > min(o, hi, c)):
        raise ValueError(f'Invalid OHLCV {sym} @ {iso(ts)}')
    return [str(ts), str(k[1]), str(k[2]), str(k[3]), str(k[4]), str(k[5]), str(k[7])]


def download_symbol(sym, windows, outdir: Path, *, fetcher=fetch_page, min_free_gb=1.0):
    dest = outdir / f'{sym}_1m.csv'
    if dest.exists():
        stats = check_file(dest, windows)
        print('HLC_BACKFILL_SKIP_VALID', json.dumps({'symbol': sym, **stats}), flush=True)
        return stats
    if shutil.disk_usage(outdir).free < min_free_gb * 1024 ** 3:
        raise RuntimeError(f'Free disk below {min_free_gb} GiB guard')
    fd, temp_name = tempfile.mkstemp(prefix=f'.{sym}.', suffix='.partial', dir=outdir)
    temp = Path(temp_name)
    count, pages = 0, 0
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as fh:
            writer = csv.writer(fh)
            writer.writerow(FIELDS)
            for start, end in windows:
                cursor = start
                while cursor < end:
                    desired = min(1000, (end - cursor) // M)
                    boundary = min(end, cursor + desired * M)
                    batch = fetcher(sym, cursor, boundary, desired)
                    pages += 1
                    if len(batch) != desired:
                        raise ValueError(f'{sym}: requested {desired} 1m klines at {iso(cursor)}; received {len(batch)}')
                    for k in batch:
                        writer.writerow(checked_row(k, cursor, sym))
                        cursor += M
                        count += 1
                    if pages % 20 == 0:
                        print('HLC_BACKFILL_PROGRESS', json.dumps({'symbol':sym,'pages':pages,'bars1m':count}), flush=True)
                    if pages % 50 == 0 and shutil.disk_usage(outdir).free < min_free_gb * 1024 ** 3:
                        raise RuntimeError(f'Free disk below {min_free_gb} GiB guard')
            fh.flush()
            os.fsync(fh.fileno())
        if count != expected_minutes(windows):
            raise AssertionError(f'Unexpected output count {sym}')
        # Exclusive destination link: existing outputs can never be overwritten.
        os.link(temp, dest)
        return {'symbol':sym,'bars1m':count,'windows':len(windows),'pages':pages,
                'sha256':sha_file(dest),'first_open_utc':iso(windows[0][0]),
                'last_open_utc':iso(windows[-1][1] - M)}
    finally:
        temp.unlink(missing_ok=True)


def check_file(path, windows):
    """Safely resume: accept existing CSV only if exact requested intervals match."""
    count = 0
    wid = 0
    want = windows[0][0]
    with path.open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != FIELDS:
            raise ValueError(f'Existing output has unexpected columns: {path}')
        for row in reader:
            if wid >= len(windows):
                raise ValueError(f'Existing output has excess candles: {path}')
            if int(row['timestamp']) != want:
                raise ValueError(f'Existing output gap/unexpected minute: {path} at {iso(want)}')
            count += 1
            want += M
            if want == windows[wid][1]:
                wid += 1
                if wid < len(windows):
                    want = windows[wid][0]
    if wid != len(windows) or count != expected_minutes(windows):
        raise ValueError(f'Existing output incomplete: {path}')
    return {'bars1m':count,'windows':len(windows),'sha256':sha_file(path)}


def atomic_manifest(path, data):
    fd, temp_name = tempfile.mkstemp(prefix='.manifest-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    parser.add_argument('--run-dir', required=True, type=Path, help='Frozen V2.2 scan run with signals.csv')
    parser.add_argument('--output-dir', required=True, type=Path, help='NEW folder with *_1m.csv for V2.2 --minute-dir')
    parser.add_argument('--max-horizon-min', type=int, default=360)
    parser.add_argument('--symbols', default='', help='Optional comma-separated symbols to limit the run')
    parser.add_argument('--min-free-gb', type=float, default=1.0)
    parser.add_argument('--sleep-seconds', type=float, default=0.40)
    args = parser.parse_args(argv)
    if args.max_horizon_min < 1 or args.max_horizon_min > 10080:
        parser.error('max-horizon-min must be between 1 and 10080')
    if args.sleep_seconds < 0.2:
        parser.error('sleep-seconds must be >= 0.2')
    if args.min_free_gb < 0:
        parser.error('min-free-gb must be nonnegative')
    infile = args.run_dir.resolve() / 'signals.csv'
    outdir = args.output_dir.resolve()
    if outdir == args.run_dir.resolve():
        parser.error('--output-dir must NOT equal --run-dir')
    selected = {s.strip().upper() for s in args.symbols.split(',') if s.strip()} if args.symbols else None
    if selected is not None and any(not re.fullmatch(r'[A-Z0-9]{3,25}', s) for s in selected):
        parser.error('--symbols contains invalid symbol')
    windows_by_sym = load_windows(infile, args.max_horizon_min, selected)
    signals_sha = sha_file(infile)
    total = sum(expected_minutes(w) for w in windows_by_sym.values())
    calls = sum(sum(math.ceil((b-a)/M/1000) for a,b in windows) for windows in windows_by_sym.values())
    report = {'symbols':len(windows_by_sym),'bars1m_required':total,'estimated_api_requests':calls,
              'max_horizon_min':args.max_horizon_min,'source_signals_sha256':signals_sha,
              'output_dir':str(outdir)}
    print('HLC_BACKFILL_PLAN', json.dumps(report), flush=True)
    for sym, windows in windows_by_sym.items():
        print('HLC_BACKFILL_SYMBOL_PLAN', json.dumps({
            'symbol':sym,'windows':len(windows),'bars1m':expected_minutes(windows),
            'first_open_utc':iso(windows[0][0]), 'last_open_utc':iso(windows[-1][1]-M),
        }), flush=True)
    if args.plan:
        return 0
    if outdir.exists() and not outdir.is_dir():
        raise SystemExit('output-dir is not a directory')
    # Prevent accidental historical overwrites. Only this downloader's CSV names are written.
    outdir.mkdir(parents=True, exist_ok=True)
    manifest_file = outdir / 'backfill_manifest.json'
    if manifest_file.is_file():
        old = json.loads(manifest_file.read_text(encoding='utf-8'))
        if old.get('source_signals_sha256') != signals_sha or old.get('max_horizon_min') != args.max_horizon_min:
            raise SystemExit('Existing output belongs to another signals/horizon version: use NEW --output-dir')
    results = {}
    failures = {}
    today_ms = int(time.time() * 1000)
    for sym, windows in windows_by_sym.items():
        if windows[-1][1] > today_ms - 60 * M:
            failures[sym] = 'Latest requested horizon is too close to the present; no forming candles'
            print('HLC_BACKFILL_ERROR', json.dumps({'symbol':sym,'error':failures[sym]}), flush=True)
            continue
        try:
            def limited_fetch(symbol, start, end, limit):
                return fetch_page(symbol, start, end, limit, args.sleep_seconds)
            stats = download_symbol(sym, windows, outdir, fetcher=limited_fetch,
                                    min_free_gb=args.min_free_gb)
            results[sym] = stats
            print('HLC_BACKFILL_SYMBOL_OK', json.dumps(stats), flush=True)
        except Exception as exc:
            failures[sym] = str(exc)
            print('HLC_BACKFILL_ERROR', json.dumps({'symbol':sym,'error':str(exc)}), flush=True)
            if 'Binance IP ban 418' in str(exc):
                break
        atomic_manifest(manifest_file, {**report,'source_signals':str(infile),
             'download_source':'Binance USD-M Futures public /fapi/v1/klines',
             'results':results,'failures':failures,
             'note':'Gaps BETWEEN separate signal windows are intentional. No gaps INSIDE windows are accepted.'})
    print('HLC_BACKFILL_DONE', json.dumps({'completed':len(results),
          'requested_symbols':len(windows_by_sym),'failed':failures,'output_dir':str(outdir)}), flush=True)
    return 0 if len(results) == len(windows_by_sym) else 1


if __name__ == '__main__':
    sys.exit(main())
