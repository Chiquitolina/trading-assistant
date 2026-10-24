#!/usr/bin/env python3
"""Pilot de OHLCV nativo y CONTIGUO para Horizontal Breakout Lab.

SOLO: endpoint público Binance USD-M Futures /fapi/v1/klines.
NUNCA importa LIVE ni Redis, no lee/escribe SQLite del proyecto, no envía órdenes.
Archivos de salida aislados bajo reports/horizontal_breakout_lab_v1/native_ohlcv_<id>/.

Ejemplo:
python3 horizontal_breakout_native_candles_pilot_v1.py --plan --start 2026-10-08 --end 2026-10-20
python3 horizontal_breakout_native_candles_pilot_v1.py --run --start 2026-10-08 --end 2026-10-20
python3 horizontal_breakout_native_candles_pilot_v1.py --audit --start 2026-10-08 --end 2026-10-20
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ONE = 60_000
THIRTY = 30 * ONE
DEFAULT_SYMBOLS = (
    '1000BONKUSDT', '1000FLOKIUSDT', '1000PEPEUSDT', '1000XECUSDT', '1INCHUSDT'
)
COLUMNS = ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume')
ENDPOINT = 'https://fapi.binance.com/fapi/v1/klines'
ROOT = Path(__file__).resolve().parent
VERSION = 'hbl-native-candles-pilot-v1.0'


def parse_date(s):
    if len(s) != 10:
        raise ValueError('Use YYYY-MM-DD en UTC')
    dt = datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def safe_json(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as file:
        json.dump(record, file, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, path)


def hash_file(p):
    digest = hashlib.sha256()
    with p.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_chunk(symbol, timeframe, start, end, api_delay):
    qs = urllib.parse.urlencode({'symbol': symbol, 'interval': timeframe, 'startTime': start,
                                 'endTime': end - 1, 'limit': 1000})
    req = urllib.request.Request(ENDPOINT + '?' + qs,
                                 headers={'User-Agent': 'horizontal-breakout-research/1.0'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                data = json.load(response)
            if not isinstance(data, list):
                raise RuntimeError(f'Unexpected Binance response: {str(data)[:160]}')
            if api_delay:
                time.sleep(api_delay)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                raise RuntimeError(f'Binance rate-limit HTTP {exc.code}; detener, no reintentar ahora') from exc
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == 3:
                raise RuntimeError(f'Binance download failed {symbol} {timeframe}: {exc}') from exc
        time.sleep(2 ** attempt)
    raise AssertionError('unreachable')


def validate_candle(raw, interval, start, end):
    if not isinstance(raw, (tuple, list)) or len(raw) < 8:
        raise ValueError('Malformed Binance kline')
    ts = int(raw[0])
    if not start <= ts < end or ts % interval:
        raise ValueError(f'Invalid/unexpected kline ts {ts}')
    values = [float(raw[i]) for i in (1, 2, 3, 4, 5, 7)]
    op, hi, lo, cl, volume, qv = values
    if not all(math.isfinite(x) for x in values):
        raise ValueError(f'Non-finite OHLCV at {ts}')
    if min(op, hi, lo, cl) <= 0 or min(volume, qv) < 0:
        raise ValueError(f'Invalid OHLCV at {ts}')
    if hi < max(op, cl, lo) or lo > min(op, cl, hi):
        raise ValueError(f'Inconsistent candle at {ts}')
    return (ts, op, hi, lo, cl, volume, qv)


def download_range(symbol, timeframe, start, end, delay):
    interval = ONE if timeframe == '1m' else THIRTY
    candles = {}
    cursor = start
    while cursor < end:
        chunk = fetch_chunk(symbol, timeframe, cursor, end, delay)
        if not chunk:
            raise RuntimeError(f'No klines for {symbol} {timeframe}, from {cursor}')
        last = None
        for raw in chunk:
            row = validate_candle(raw, interval, start, end)
            ts = row[0]
            old = candles.get(ts)
            if old is not None and old != row:
                raise ValueError(f'Conflicting candle {symbol} {timeframe} {ts}')
            candles[ts] = row
            last = ts if last is None else max(last, ts)
        if last is None or last < cursor:
            raise RuntimeError(f'Binance cursor did not advance: {symbol} {timeframe}')
        cursor = last + interval
    data = [candles[k] for k in sorted(candles)]
    expected = (end - start) // interval
    if len(data) != expected:
        missing = next((start + i * interval for i, row in enumerate(data) if row[0] != start + i * interval),
                       start + len(data) * interval)
        raise RuntimeError(f'Incomplete {symbol} {timeframe}: {len(data)}/{expected} first gap at {missing}')
    return data


def write_csv(path, candles):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        with tmp.open('w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(COLUMNS)
            writer.writerows(candles)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def validate_csv(path, start, end, interval):
    expected = (end - start) // interval
    n, previous = 0, None
    with path.open(newline='', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(f'CSV schema mismatch: {path}')
        for row in reader:
            ts = int(row['timestamp'])
            if ts != start + n * interval:
                raise ValueError(f'Gap/duplicate at {ts} in {path}')
            vals = [float(row[k]) for k in COLUMNS[1:]]
            op, hi, lo, cl, vol, qv = vals
            if (not all(math.isfinite(x) for x in vals) or min(op, hi, lo, cl) <= 0
                    or min(vol, qv) < 0 or hi < max(op, lo, cl) or lo > min(op, hi, cl)):
                raise ValueError(f'Invalid candle {ts} in {path}')
            previous = ts
            n += 1
    if n != expected:
        raise ValueError(f'Incomplete file {path}: {n}/{expected}')
    return dict(rows=n, first=start, last=previous)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--audit', action='store_true')
    ap.add_argument('--start', required=True, help='Start UTC YYYY-MM-DD inclusive')
    ap.add_argument('--end', required=True, help='End UTC YYYY-MM-DD exclusive for 30m signals')
    ap.add_argument('--symbols', default=','.join(DEFAULT_SYMBOLS))
    ap.add_argument('--tail-hours', type=int, default=48)
    ap.add_argument('--delay', type=float, default=0.15, help='Delay per Binance request in seconds')
    ap.add_argument('--min-free-gb', type=float, default=2.0)
    ap.add_argument('--output', type=Path, default=None)
    args = ap.parse_args()
    start = parse_date(args.start)
    end = parse_date(args.end)
    if end <= start or end - start > 40 * 24 * 60 * ONE:
        ap.error('Date window must be >0 and <=40 days')
    if not 48 <= args.tail_hours <= 96:
        ap.error('--tail-hours must be 48..96')
    if not 0.05 <= args.delay <= 10 or args.min_free_gb < 0:
        ap.error('Invalid request delay/disk guard')
    symbols = [x.strip().upper() for x in args.symbols.split(',') if x.strip()]
    if not symbols or len(symbols) > 5 or len(set(symbols)) != len(symbols) or not all(
            s.isascii() and s.isalnum() and s.endswith('USDT') for s in symbols):
        ap.error('Provide 1..5 distinct Binance USDT symbols')
    tail_end = end + args.tail_hours * 60 * ONE
    if tail_end > int(time.time() * 1000) - THIRTY:
        ap.error('The requested future tail has not closed yet')
    out = (args.output or ROOT / 'reports/horizontal_breakout_lab_v1' /
           f'native_ohlcv_{args.start.replace("-", "")}_{args.end.replace("-", "")}_tail{args.tail_hours}h').resolve()
    settings = dict(version=VERSION, symbols=symbols, start=args.start, end=args.end,
                    tail_hours=args.tail_hours, source='Binance USD-M Futures public klines',
                    no_live_writes=True)
    print('NATIVE_HBL', json.dumps(dict(mode='PLAN' if args.plan else ('RUN' if args.run else 'AUDIT'),
                          folder=str(out), settings=settings,
                          required_1m_per_symbol=(tail_end-start)//ONE,
                          required_30m_per_symbol=(end-start)//THIRTY,
                          approx_binance_requests_per_symbol=math.ceil((tail_end-start)/ONE/1000)+math.ceil((end-start)/THIRTY/1000)),
                          ensure_ascii=False), flush=True)
    if args.plan:
        return
    if args.run and shutil.disk_usage(ROOT).free < args.min_free_gb * 1024 ** 3:
        raise SystemExit('Insufficient free disk; no downloads started')
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / 'dataset_config.json'
    if config_path.exists() and json.loads(config_path.read_text()) != settings:
        raise SystemExit('Folder already belongs to a different date/symbol configuration')
    if args.run:
        safe_json(config_path, settings)
    failures = []
    for symbol in symbols:
        for tf, period, last_end in (('30m', THIRTY, end), ('1m', ONE, tail_end)):
            f = out / f'{symbol}_{tf}.csv'
            meta = f.with_suffix('.meta.json')
            signature = {'symbol':symbol, 'timeframe':tf, 'start_ms':start, 'end_ms':last_end,
                         'source': 'Binance USD-M Futures native closed klines', 'version':VERSION}
            try:
                if f.exists() and meta.exists():
                    metadata = json.loads(meta.read_text())
                    if all(metadata.get(k) == v for k, v in signature.items()) and hash_file(f) == metadata.get('sha256'):
                        info = validate_csv(f, start, last_end, period)
                        print('CACHED', symbol, tf, info['rows'], flush=True)
                        continue
                    raise ValueError('Existing file/manifest mismatch; refusing overwrite')
                if args.audit:
                    raise FileNotFoundError(f)
                if f.exists() or meta.exists():
                    raise RuntimeError('Partial/inconsistent existing dataset files; inspect manually')
                print('FETCH', symbol, tf, flush=True)
                candles = download_range(symbol, tf, start, last_end, args.delay)
                if shutil.disk_usage(ROOT).free < args.min_free_gb * 1024 ** 3:
                    raise RuntimeError('Disk guard after download')
                write_csv(f, candles)
                info = validate_csv(f, start, last_end, period)
                safe_json(meta, {**signature, **info, 'sha256':hash_file(f)})
                print('COMPLETE', symbol, tf, info['rows'], flush=True)
            except Exception as exc:
                failures.append({'symbol':symbol,'timeframe':tf,'error':str(exc)})
                print('ERROR', symbol, tf, str(exc), flush=True)
                if args.run:
                    break
    result = dict(success=not failures, settings=settings, files_expected=len(symbols)*2,
                  files_valid=len(symbols)*2-len(failures) if not failures else None, errors=failures)
    safe_json(out / 'native_audit.json', result)
    print('NATIVE_HBL_AUDIT', json.dumps(result, ensure_ascii=False), flush=True)
    if failures:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
