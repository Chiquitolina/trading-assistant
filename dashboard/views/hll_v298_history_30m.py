#!/usr/bin/env python3
"""HLL V2.9.8: frozen 290-symbol 30m OHLC downloader (offline research).

Read-only toward old HLL/Redis/LIVE. Uses Binance public USD-M futures GET klines.
Resume by symbol, exclusive CSV creation; no interpolation and explicit partial listings.
Must run from repository root: python3 -m dashboard.views.hll_v298_history_30m --plan/--run/--report
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TF = 1_800_000
FIELDS = ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume')
BASE = 'https://fapi.binance.com/fapi/v1/klines'
DEFAULT_PREFLIGHT = Path('reports/horizontal_level_290_preflight_v297/runs/sep290_preflight_v297')
DEFAULT_EXISTING = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
DEFAULT_OUT = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')
START = '2026-08-20T00:00:00Z'
END = '2026-10-04T00:00:00Z'  # 48h+ beyond end of 2026-09, for forward audit


class FatalRateLimit(RuntimeError):
    pass


def ms(s):
    if not s.endswith('Z'):
        raise ValueError('Date must be explicit UTC ending Z')
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)


def iso(t):
    return datetime.fromtimestamp(int(t) / 1000, timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate_symbol(s):
    # Futures symbol strings may be non-ASCII; only reject unsafe path / query values.
    return bool(s) and len(s) <= 40 and s == s.strip() and s.upper() == s and not any(
        c in s for c in '/\\.\x00\r\n\t?#&=')


def frozen_universe(preflight):
    manifest = json.loads((preflight / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 'HLL_V297_290_PREFLIGHT' or manifest.get('symbols') != 290:
        raise ValueError('Preflight freeze is not the required 290-symbol cohort')
    symbols = (preflight / 'universe_290.txt').read_text(encoding='utf-8').splitlines()
    if len(symbols) != 290 or len(set(symbols)) != 290 or symbols != sorted(symbols):
        raise ValueError('Frozen universe has missing/duplicate/unsorted symbols')
    if not all(validate_symbol(s) for s in symbols):
        raise ValueError('Invalid symbol in frozen universe')
    source = Path(manifest['source_universe'])
    if not source.is_file() or digest(source) != manifest['source_universe_sha256']:
        raise ValueError('Source universe CSV no longer matches V297 frozen hash')
    # Also check the names; a matching hash itself does not prove the TXT wasn't changed.
    with source.open(encoding='utf-8-sig', newline='') as f:
        rdr = csv.DictReader(f)
        col = manifest.get('symbol_column') or 'symbol'
        if col not in (rdr.fieldnames or []):
            raise ValueError('Frozen symbol column missing')
        names = sorted({(r.get(col) or '').strip() for r in rdr if (r.get(col) or '').strip()})
    if names != symbols:
        raise ValueError('TXT universe differs from its frozen source CSV')
    return symbols, manifest


def validate_fields(row, sym, ts):
    vals = [float(row[k]) for k in FIELDS[1:]]
    if not all(math.isfinite(x) for x in vals):
        raise ValueError(f'Nonfinite OHLCV {sym} {iso(ts)}')
    o, h, l, c, v, q = vals
    if min(o, h, l, c) <= 0 or min(v, q) < 0 or h < max(o, l, c) or l > min(o, h, c):
        raise ValueError(f'Invalid OHLCV {sym} {iso(ts)}')
    return [str(int(ts))] + [str(row[k]) for k in FIELDS[1:]]


def inspect_csv(path, start, end, *, require_full=False):
    """Validate ALL observed bars; listing boundary gaps are explicit, internal gaps fail."""
    prev = None
    first = None
    n = 0
    with path.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        if set(FIELDS) - set(r.fieldnames or []):
            raise ValueError(f'Missing canonical candle fields: {path}')
        for row in r:
            t = int(row['timestamp'])
            if t < start or t >= end or t % TF:
                raise ValueError(f'Out-of-range or unaligned timestamp in {path}: {t}')
            if prev is not None and t != prev + TF:
                raise ValueError(f'Internal gap/duplicate in {path} after {iso(prev)}')
            validate_fields(row, path.stem, t)
            if first is None:
                first = t
            prev = t
            n += 1
    status = 'NO_HISTORY'
    if n:
        status = 'FULL' if first == start and prev == end-TF else 'PARTIAL_LISTING_OR_END'
    if require_full and status != 'FULL':
        raise ValueError(f'Expected fully covered output, got {status}: {path}')
    return {'status': status, 'bars30': n, 'first_utc': iso(first) if first is not None else None,
            'last_utc': iso(prev) if prev is not None else None,
            'missing_start_bars': (first-start)//TF if first is not None else None,
            'missing_end_bars': (end-TF-prev)//TF if prev is not None else None}


def slice_reusable(source, output, start, end):
    """Copy clipped, fully continuous source; return None if baseline isn't suitable."""
    if not source.is_file():
        return None
    rows = []
    expected = start
    with source.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        if set(FIELDS) - set(r.fieldnames or []):
            return None
        for row in r:
            t = int(row['timestamp'])
            if start <= t < end:
                if t != expected:
                    return None
                rows.append(validate_fields(row, source.stem, t))
                expected += TF
    if expected != end:
        return None
    write_exclusive(output, rows)
    return {'status': 'FULL', 'bars30': len(rows), 'first_utc': iso(start),
            'last_utc': iso(end-TF), 'missing_start_bars': 0, 'missing_end_bars': 0,
            'source': 'CORE15_REUSE', 'original_sha256': digest(source)}


def write_exclusive(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.stem + '.', suffix='.partial')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            w.writerows(rows)
            f.flush(); os.fsync(f.fileno())
        os.link(temp, path)  # atomic, never overwrites existing CSV
    finally:
        Path(temp).unlink(missing_ok=True)


def fetch_page(sym, start, end, *, sleep_s=0.35):
    params = urlencode({'symbol': sym, 'interval': '30m', 'startTime': start,
                        'endTime': end - 1, 'limit': 1000})
    url = BASE + '?' + params
    last = None
    for i in range(5):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'HLL-V298-30m-FrozenResearch/1.0'}), timeout=30) as r:
                data = json.load(r)
            if not isinstance(data, list):
                raise ValueError(f'Unexpected API response: {str(data)[:200]}')
            if sleep_s:
                time.sleep(sleep_s)
            return data
        except HTTPError as e:
            last = f'HTTP {e.code}: {e.read(250).decode("utf-8", errors="replace")}'
            if e.code == 418:
                raise FatalRateLimit('Binance HTTP 418 IP ban; stop downloads immediately') from e
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(last) from e
            pause = 60 if e.code == 429 else min(30, 2**(i+1))
            if e.code == 429:
                try:
                    pause = max(pause, min(180, int(e.headers.get('Retry-After', '60'))))
                except (TypeError, ValueError):
                    pass
        except (URLError, TimeoutError, OSError) as e:
            last = repr(e)
            pause = min(30, 2**(i+1))
        if i < 4:
            time.sleep(pause)
    raise RuntimeError(f'Binance request failed for {sym}: {last}')


def download(sym, start, end, *, fetcher=fetch_page, sleep_s=0.35):
    rows = []
    cursor = start
    previous = None
    pages = 0
    while cursor < end:
        page = fetcher(sym, cursor, end, sleep_s=sleep_s)
        pages += 1
        if not page:
            break
        stamps = []
        for k in page:
            if len(k) < 8:
                raise ValueError(f'Invalid kline Binance payload: {sym}')
            ts = int(k[0])
            if not (cursor <= ts < end) or ts % TF:
                raise ValueError(f'Out-of-range Binance timestamp: {sym} {ts}')
            if previous is not None and ts != previous + TF:
                raise ValueError(f'Internal gap in Binance history: {sym} {iso(previous)} => {iso(ts)}')
            item = dict(zip(FIELDS, (ts, k[1], k[2], k[3], k[4], k[5], k[7])))
            rows.append(validate_fields(item, sym, ts))
            stamps.append(ts)
            previous = ts
        if stamps != sorted(set(stamps)):
            raise ValueError(f'Duplicate/unsorted Binance page: {sym}')
        nxt = stamps[-1] + TF
        if nxt <= cursor:
            raise ValueError(f'Nonprogressing Binance paging: {sym}')
        cursor = nxt
    status = ('NO_HISTORY' if not rows else
              'FULL' if int(rows[0][0]) == start and int(rows[-1][0]) == end-TF else
              'PARTIAL_LISTING_OR_END')
    return rows, {'status': status, 'bars30': len(rows), 'pages': pages,
                  'first_utc': iso(rows[0][0]) if rows else None,
                  'last_utc': iso(rows[-1][0]) if rows else None,
                  'missing_start_bars': (int(rows[0][0])-start)//TF if rows else None,
                  'missing_end_bars': (end-TF-int(rows[-1][0]))//TF if rows else None,
                  'source': 'BINANCE_FAPI_PUBLIC'}


def save_json_exclusive(path, info):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, sort_keys=True, indent=2)


def process_one(sym, args, start, end, *, fetcher=fetch_page):
    dest = args.output_dir / (sym + '_30m.csv')
    meta = args.output_dir / 'provenance' / (sym + '.json')
    if dest.is_file():
        stat = inspect_csv(dest, start, end)
        if meta.is_file():
            saved = json.loads(meta.read_text(encoding='utf-8'))
            if saved.get('sha256') != digest(dest) or saved.get('start_utc') != args.start_utc or saved.get('end_utc_exclusive') != args.end_utc:
                raise ValueError(f'Existing file/source manifest mismatch: {dest}')
        else:
            save_json_exclusive(meta, {'symbol': sym, **stat, 'sha256': digest(dest),
                                       'source': 'EXISTING_VALID_FILE_METADATA_RECOVERED',
                                       'start_utc': args.start_utc, 'end_utc_exclusive': args.end_utc})
        return {'symbol': sym, **stat, 'source': 'EXISTING_VALID_SKIP'}
    reused = None
    if args.reuse_core15:
        reused = slice_reusable(args.existing_dir / (sym + '_30m.csv'), dest, start, end)
    if reused is None:
        rows, stat = download(sym, start, end, fetcher=fetcher, sleep_s=args.sleep_s)
        if not rows:
            return {'symbol': sym, **stat}
        write_exclusive(dest, rows)
        reused = stat
    result = {'symbol': sym, **reused, 'sha256': digest(dest),
              'start_utc': args.start_utc, 'end_utc_exclusive': args.end_utc}
    save_json_exclusive(meta, result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--report', action='store_true')
    p.add_argument('--preflight-dir', type=Path, default=DEFAULT_PREFLIGHT)
    p.add_argument('--existing-dir', type=Path, default=DEFAULT_EXISTING)
    p.add_argument('--output-dir', type=Path, default=DEFAULT_OUT)
    p.add_argument('--start-utc', default=START)
    p.add_argument('--end-utc', default=END)
    p.add_argument('--batch-start', type=int, default=0)
    p.add_argument('--batch-size', type=int, default=20)
    p.add_argument('--sleep-s', type=float, default=0.35)
    p.add_argument('--min-free-gb', type=float, default=7.0)
    p.add_argument('--no-reuse-core15', dest='reuse_core15', action='store_false')
    a = p.parse_args(argv)
    start, end = ms(a.start_utc), ms(a.end_utc)
    if start >= end or start % TF or end % TF or a.batch_start < 0 or a.batch_size < 1 or a.batch_size > 290 or a.sleep_s < .1 or a.min_free_gb < 1:
        p.error('Invalid UTC window/batch/rate/disk guard')
    if end >= int(time.time() * 1000)-TF:
        p.error('End may not be in current/forming candle')
    symbols, source = frozen_universe(a.preflight_dir)
    picked = symbols[a.batch_start:a.batch_start+a.batch_size]
    if a.run and not picked:
        p.error('Empty batch')
    already = sum((a.output_dir / (s + '_30m.csv')).is_file() for s in symbols)
    plan = {'version': 'HLL_V298_HISTORY_30M', 'symbols_frozen': len(symbols),
            'start_utc': a.start_utc, 'end_utc_exclusive': a.end_utc,
            'batch_start': a.batch_start, 'batch_size': len(picked),
            'batch_first': picked[0] if picked else None, 'batch_last': picked[-1] if picked else None,
            'present_files': already, 'remaining_without_file': len(symbols)-already,
            'output_dir': str(a.output_dir), 'reuse_core15': a.reuse_core15,
            'mode': 'READ_ONLY_PLAN' if a.plan else 'PUBLIC_HISTORICAL_FETCH' if a.run else 'FILE_SUMMARY',
            'does_not_change_live': True}
    print('HLL_V298_PLAN ' + json.dumps(plan, ensure_ascii=False), flush=True)
    if a.plan:
        return 0
    if a.report:
        statuses = Counter()
        for s in symbols:
            path = a.output_dir / (s + '_30m.csv')
            if not path.is_file():
                statuses['MISSING'] += 1
                continue
            try:
                info = inspect_csv(path, start, end)
                meta = a.output_dir / 'provenance' / (s + '.json')
                if not meta.is_file() or json.loads(meta.read_text(encoding='utf-8')).get('sha256') != digest(path):
                    statuses['UNVERIFIED_PROVENANCE'] += 1
                else:
                    statuses[info['status']] += 1
            except (OSError, ValueError, KeyError) as e:
                statuses['INVALID_FILE'] += 1
                print('HLL_V298_BAD_FILE '+json.dumps({'symbol':s,'error':str(e)[:200]},ensure_ascii=False), flush=True)
        print('HLL_V298_REPORT '+json.dumps({'symbols':len(symbols),**dict(statuses)},ensure_ascii=False), flush=True)
        return 0
    a.output_dir.mkdir(parents=True,exist_ok=True)
    counts = Counter()
    for idx,s in enumerate(picked,a.batch_start+1):
        if shutil.disk_usage(a.output_dir).free < a.min_free_gb * 1024**3:
            raise SystemExit(f'DISK_GUARD: < {a.min_free_gb} GiB available, stopped safely')
        try:
            info = process_one(s, a, start, end)
            counts[info['status']] += 1
            print('HLL_V298_SYMBOL '+json.dumps({'index':idx,'of':len(symbols),**info},ensure_ascii=False),flush=True)
        except FatalRateLimit:
            raise
        except (RuntimeError, ValueError, OSError, KeyError) as e:
            counts['ERROR'] += 1
            print('HLL_V298_ERROR '+json.dumps({'index':idx,'symbol':s,'error':type(e).__name__+': '+str(e)[:240]},ensure_ascii=False),flush=True)
    print('HLL_V298_BATCH_DONE '+json.dumps({'batch_start':a.batch_start,'attempted':len(picked),**dict(counts)},ensure_ascii=False),flush=True)
    return 0 if counts['ERROR'] == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
