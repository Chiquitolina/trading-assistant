"""Offline-only BTCUSDT Futures 1m candle archive for HLL visual inspection.

Does not import or modify LIVE collectors, databases, or HLL detectors.
CLI: --plan (no network, no writes), --run (network and new frozen output).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MINUTE = 60_000
LIMIT = 1000
BASE_URL = 'https://fapi.binance.com/fapi/v1/klines'
FIELDS = ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume')
VERSION = 'hll-btc-visual-candles-v1.0'


def timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Specify UTC explicitly using Z or +00:00')
    parsed = parsed.astimezone(timezone.utc)
    if parsed.second or parsed.microsecond:
        raise ValueError('Time interval must align with minute boundaries')
    return int(parsed.timestamp() * 1000)


def sha256(file: Path) -> str:
    h = hashlib.sha256()
    with file.open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def normalize(raw: list, expected_timestamp: int) -> tuple:
    if not isinstance(raw, list) or len(raw) < 8:
        raise ValueError('Invalid Futures kline response shape')
    ts = int(raw[0])
    if ts != expected_timestamp:
        raise ValueError(f'Discontinuous candles: expected {expected_timestamp}, received {ts}')
    values = tuple(str(raw[i]) for i in (1, 2, 3, 4, 5, 7))
    try:
        o, h, l, c, v, q = map(float, values)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'Non-numeric candle at {ts}') from exc
    from math import isfinite
    if not all(isfinite(x) for x in (o, h, l, c, v, q)) or min(o, h, l, c) <= 0:
        raise ValueError(f'Invalid numeric candle at {ts}')
    if h < max(o, c, l) or l > min(o, c, h) or v < 0 or q < 0:
        raise ValueError(f'Invalid OHLC consistency at {ts}')
    if int(raw[6]) != ts + MINUTE - 1:
        raise ValueError(f'Unexpected 1m close timestamp at {ts}')
    return (ts, *values)


def verify_csv(path: Path, start_ms: int, end_ms: int) -> dict:
    expected = start_ms
    n = 0
    with path.open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError('Unexpected CSV header')
        for row in reader:
            ts = int(row['timestamp'])
            normalize([ts, row['open'], row['high'], row['low'], row['close'], row['volume'],
                       ts + MINUTE - 1, row['quoteVolume']], expected)
            n += 1
            expected += MINUTE
    if expected != end_ms:
        raise ValueError(f'CSV ends prematurely: expected {end_ms}, reached {expected}')
    return {'bars': n, 'first_open_ms': start_ms, 'last_open_ms': end_ms - MINUTE}


def fetch_chunk(start_ms: int, end_ms: int, *, attempts: int = 4,
                request_func=None, sleeper=time.sleep) -> list:
    """Retrieve a bounded batch. Injection permits network-free unit tests."""
    if end_ms <= start_ms or (end_ms - start_ms) // MINUTE > LIMIT:
        raise ValueError('Chunk must hold 1..1000 complete minute bars')
    query = urlencode({'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': start_ms,
                       'endTime': end_ms - 1, 'limit': LIMIT})
    url = BASE_URL + '?' + query
    req = Request(url, headers={'User-Agent': 'HLL-BTC-Visual-Research/1.0', 'Accept': 'application/json'})
    request_func = request_func or (lambda r: urlopen(r, timeout=25))
    for attempt in range(attempts):
        try:
            response = request_func(req)
            with response as fd:
                raw = json.load(fd)
            if not isinstance(raw, list):
                raise ValueError(f'Binance returned non-list payload: {str(raw)[:180]}')
            want = (end_ms - start_ms) // MINUTE
            if len(raw) != want:
                raise ValueError(f'Incomplete response: {len(raw)} of {want} candles')
            return [normalize(candle, start_ms + j * MINUTE) for j, candle in enumerate(raw)]
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f'Binance request failed for {start_ms}: {exc}') from exc
            sleeper(min(2 ** attempt, 8))
    raise AssertionError('unreachable')


def plan(start_ms: int, end_ms: int, out_dir: Path) -> dict:
    if start_ms % MINUTE or end_ms % MINUTE or end_ms <= start_ms:
        raise ValueError('Invalid minute-aligned UTC interval')
    count = (end_ms - start_ms) // MINUTE
    if count > 60 * 24 * 90:
        raise ValueError('Limit research archive requests to 90 days')
    return {'version': VERSION, 'mode': 'PLAN_READ_ONLY', 'symbol': 'BTCUSDT',
            'source': 'Binance USD-M Futures REST 1m', 'start_ms': start_ms, 'end_exclusive_ms': end_ms,
            'expected_bars': count, 'max_requests': (count + LIMIT - 1) // LIMIT,
            'output_dir': str(out_dir.resolve()), 'existing_output': out_dir.exists(),
            'modifies_live': False}


def run(start_ms: int, end_ms: int, out_dir: Path, *, fetcher=fetch_chunk) -> dict:
    info = plan(start_ms, end_ms, out_dir)
    if out_dir.exists():
        raise FileExistsError(f'Output already exists, will not overwrite: {out_dir}')
    if not out_dir.parent.is_dir():
        raise FileNotFoundError(f'Create output parent explicitly first: {out_dir.parent}')
    # Fail closed: exclusive creation; only this new directory is ever written.
    out_dir.mkdir(exist_ok=False)
    temporary = out_dir / 'BTCUSDT_1m.csv.partial'
    result = out_dir / 'BTCUSDT_1m.csv'
    try:
        with temporary.open('w', newline='', encoding='utf-8') as fp:
            writer = csv.writer(fp, lineterminator='\n')
            writer.writerow(FIELDS)
            cursor = start_ms
            while cursor < end_ms:
                chunk_end = min(end_ms, cursor + LIMIT * MINUTE)
                chunk = fetcher(cursor, chunk_end)
                if len(chunk) != (chunk_end - cursor) // MINUTE:
                    raise ValueError('Fetcher gave incomplete chunk')
                for i, candle in enumerate(chunk):
                    if int(candle[0]) != cursor + i * MINUTE:
                        raise ValueError('Fetcher gave discontinuous candles')
                    writer.writerow(candle)
                cursor = chunk_end
            fp.flush()
            os.fsync(fp.fileno())
        validation = verify_csv(temporary, start_ms, end_ms)
        temporary.rename(result)
        manifest = {**{k: v for k, v in info.items() if k not in ('mode', 'existing_output')},
                    'mode': 'RUN_OFFLINE', 'integrity': 'CONTIGUOUS_1M_VERIFIED',
                    'files_sha256': {result.name: sha256(result)}, **validation,
                    'research_only': True, 'immutable_source': 'Binance Futures historical klines',
                    'no_live_changes': True}
        tmp_manifest = out_dir / 'manifest.json.partial'
        tmp_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        tmp_manifest.rename(out_dir / 'manifest.json')
        return manifest
    except Exception:
        # Leave a visible .partial file, if any, as evidence of failed/incomplete run;
        # never expose a finished manifest or CSV for an invalid archive.
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='.')
    parser.add_argument('--start', default='2026-10-24T00:00:00Z')
    parser.add_argument('--end', default='2026-11-08T01:00:00Z', help='Exclusive UTC endpoint')
    parser.add_argument('--tag', default='btc_oct24_nov08_visual_v1')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    if '/' in args.tag or '\\' in args.tag or args.tag in ('.', '..'):
        parser.error('Tag must be one simple directory name')
    root = Path(args.project).resolve()
    folder = root / 'reports' / 'reaction_lab' / 'btc_visual_candles_v1'
    output = folder / args.tag
    start_ms, end_ms = timestamp_ms(args.start), timestamp_ms(args.end)
    if args.plan:
        print('BTC_VISUAL_CANDLES_V1 ' + json.dumps(plan(start_ms, end_ms, output)))
    else:
        folder.mkdir(parents=True, exist_ok=True)
        print('BTC_VISUAL_CANDLES_V1 ' + json.dumps(run(start_ms, end_ms, output)))


if __name__ == '__main__':
    main()
