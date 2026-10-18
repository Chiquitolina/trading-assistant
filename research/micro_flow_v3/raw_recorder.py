"""Micro Flow V3 research-only Binance USD-M aggTrade TEXT tape recorder.

Independent asyncio + websockets transport. Doesn't import or touch WSClient,
PM2, Redis, Market Data or trading systems. `raw_json` is the WebSocket TEXT
payload as delivered by `websockets.recv()` (not TCP/WS frame bytes).

The recorder detects ID discontinuities but does NOT repair them or claim
complete coverage. This is a tape producer, not a strategy replay engine.
"""
import argparse
import asyncio
import fcntl
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .tape import TapeStore, replay_digest

SYMBOL_RE = re.compile(r'^[A-Z0-9]{2,30}USDT$')
# Binance migrated USD-M market-data streams to /market in 2026.
BASE_WS_URL = 'wss://fstream.binance.com/market/ws'


def _disk_free(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def _now():
    return time.time_ns(), time.monotonic_ns()


def _limit_reason(prefix, exc):
    detail = str(exc).replace('\n', ' ').replace('\r', ' ')[:200]
    return f'{prefix}:{type(exc).__name__}:{detail}'


async def _sleep_until(stop_flag, *, seconds, deadline):
    """Sleep in short steps so SIGINT, SIGTERM, and deadline remain responsive."""
    end = min(time.monotonic() + seconds, deadline)
    while not stop_flag.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.1))


async def _capture(symbol, writer, *, stop_flag, deadline, min_free_gb,
                   connect_factory=None, reconnect_min=0.5,
                   reconnect_max=3.0, status_interval=15.0,
                   read_timeout=30.0, first_trade_timeout=20.0):
    """Record WebSocket TEXT frames in observed receive order.

    `connect_factory` allows fully offline tests. Only the real default imports
    `websockets`; no API access is required for tape replay or tests.
    """
    if connect_factory is None:
        from websockets.asyncio.client import connect
        connect_factory = connect

    url = f'{BASE_WS_URL}/{symbol.lower()}@aggTrade'
    gen = 0
    reconnects = 0
    missing_ids = 0
    ignored = 0
    last_status = time.monotonic()

    while not stop_flag.is_set() and time.monotonic() < deadline:
        if _disk_free(writer.path.parent) < int(min_free_gb * 1024 ** 3):
            writer.marker('LOW_DISK_FAIL_CLOSED')
            raise RuntimeError('V3 free disk below configured floor')

        gen += 1
        writer.marker('CONNECT_ATTEMPT', connection=gen)
        writer.maybe_flush(force=True)
        connection_opened = False
        connection_failed = False
        try:
            # We own exactly one connection and one reconnect loop. Disable
            # the library's automatic retry iterator by using one async context
            # manager per explicit attempt, not `async for ... in connect(...)`.
            async with connect_factory(
                url,
                open_timeout=10,
                close_timeout=2,
                ping_interval=20,
                ping_timeout=20,
                max_size=1024 * 1024,
                max_queue=32,
                compression=None,
            ) as ws:
                connection_opened = True
                ws_opened_at = time.monotonic()
                trade_seen_on_connection = False
                writer.marker('WS_OPENED', connection=gen)
                writer.maybe_flush(force=True)
                print(f'[V3 RAW] WS opened generation={gen} symbol={symbol}', flush=True)

                while not stop_flag.is_set() and time.monotonic() < deadline:
                    if (not trade_seen_on_connection
                            and time.monotonic() - ws_opened_at >= first_trade_timeout):
                        writer.marker('NO_FIRST_AGGTRADE_TIMEOUT_FAIL_CLOSED', connection=gen)
                        raise RuntimeError(
                            f'No aggTrade received within {first_trade_timeout:g}s '
                            'of WebSocket open; check endpoint and subscription'
                        )
                    if _disk_free(writer.path.parent) < int(min_free_gb * 1024 ** 3):
                        writer.marker('LOW_DISK_FAIL_CLOSED', connection=gen)
                        raise RuntimeError('V3 free disk below configured floor')

                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        break
                    # Timeout is a periodic safety/flush checkpoint, not a
                    # claim of network disconnection or missing market data.
                    try:
                        msg = await asyncio.wait_for(
                            ws.recv(), timeout=min(1.0, time_left)
                        )
                    except asyncio.TimeoutError:
                        writer.maybe_flush(force=True)
                        continue
                    wall_ns, mono_ns = _now()  # as soon as recv completes

                    if not isinstance(msg, str):
                        writer.marker('UNEXPECTED_BINARY_FRAME_FAIL_CLOSED',
                                      wall_ns=wall_ns, mono_ns=mono_ns,
                                      connection=gen)
                        raise RuntimeError('Unexpected binary AggTrade frame')

                    previous_id = writer.last_agg_id
                    result = writer.ingest(msg, connection=gen,
                                           wall_ns=wall_ns, mono_ns=mono_ns)
                    if result == 'TRADE':
                        trade_seen_on_connection = True
                    if result == 'INVALID':
                        writer.marker('INVALID_AGGTRADE_FAIL_CLOSED',
                                      wall_ns=wall_ns, mono_ns=mono_ns,
                                      connection=gen)
                        raise RuntimeError('Invalid AggTrade received; tape must be audited')
                    if result == 'IGNORED':
                        ignored += 1
                        writer.marker('UNEXPECTED_NON_AGGTRADE_MESSAGE',
                                      wall_ns=wall_ns, mono_ns=mono_ns,
                                      connection=gen)
                    elif previous_id is not None and writer.last_agg_id > previous_id + 1:
                        missing = writer.last_agg_id - previous_id - 1
                        missing_ids += missing
                        writer.marker(
                            f'GAP_DETAILS:previous={previous_id}:'
                            f'next={writer.last_agg_id}:missing_ids={missing}',
                            wall_ns=wall_ns, mono_ns=mono_ns, connection=gen,
                        )
                    writer.maybe_flush()
                    if time.monotonic() - last_status >= status_interval:
                        print(
                            f'[V3 RAW] symbol={symbol} generation={gen} '
                            f'trades={writer.count} anomalies={writer.anomalies} '
                            f'missing_ids={missing_ids} reconnects={reconnects}',
                            flush=True,
                        )
                        last_status = time.monotonic()

        except Exception as exc:
            # Exceptions from recv/open/close are recorded; then we decide
            # whether to reconnect. Never silently classify a gap as clean.
            writer.marker(_limit_reason('WS_TRANSPORT_EXCEPTION', exc),
                          connection=gen)
            writer.maybe_flush(force=True)
            connection_failed = True
            print(f'[V3 RAW] generation={gen} transport error: '
                  f'{type(exc).__name__}: {str(exc)[:180]}', flush=True)
            if isinstance(exc, RuntimeError) and str(exc).startswith((
                'V3 free disk', 'Unexpected binary', 'Invalid AggTrade',
                'No aggTrade',
            )):
                raise
        finally:
            if connection_opened:
                writer.marker('WS_CLOSED', connection=gen)
            writer.maybe_flush(force=True)

        if stop_flag.is_set() or time.monotonic() >= deadline:
            break
        # Any close before a requested stop is a coverage boundary, even when
        # the next received ID happens to be consecutive.
        writer.marker('RECONNECT_COVERAGE_UNVERIFIED', connection=gen)
        reconnects += 1
        delay = min(reconnect_max,
                    reconnect_min * (2 ** min(reconnects - 1, 5)))
        if connection_failed:
            print(f'[V3 RAW] reconnect #{reconnects} in {delay:.2f}s', flush=True)
        await _sleep_until(stop_flag, seconds=delay, deadline=deadline)

    writer.maybe_flush(force=True)
    return {'trades': writer.count, 'anomalies': writer.anomalies,
            'generations': gen, 'reconnects': reconnects,
            'missing_ids': missing_ids, 'ignored': ignored}


def run_live_raw(symbol, path, *, min_free_gb=2, max_hours=None,
                 reconnect_min=0.5, reconnect_max=3.0,
                 connect_factory=None, first_trade_timeout=20.0):
    """Run one capture into a NEW tape; never append a second recording session."""
    symbol = symbol.upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError('Invalid Binance USD-M symbol')
    if max_hours is not None and max_hours <= 0:
        raise ValueError('max-hours must be positive')
    if (min_free_gb < 0 or reconnect_min < 0 or reconnect_max < reconnect_min
            or first_trade_timeout <= 0):
        raise ValueError('Invalid disk or reconnect parameters')

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + '.lock')
    lock = lock_path.open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another recorder owns this tape')

    writer = None
    handlers = {}
    stop_flag = threading.Event()
    try:
        if path.exists():
            raise FileExistsError(f'Tape already exists; choose a NEW --db path: {path}')
        if _disk_free(path.parent) < int(min_free_gb * 1024 ** 3):
            raise RuntimeError('V3 free disk below configured floor')

        writer = TapeStore(path, symbol=symbol)
        writer.marker('PROCESS_START_UNVERIFIED_PRIOR_HISTORY')
        writer.marker('TRANSPORT_WEBSOCKETS_TEXT_PAYLOAD_NOT_TCP_OR_WS_FRAME_BYTES')
        writer.marker('NEW_TAPE_NO_CROSS_SESSION_APPEND')
        writer.maybe_flush(force=True)

        def handle_stop(_sig, _frame):
            stop_flag.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, handle_stop)

        start = time.monotonic()
        deadline = start + (max_hours * 3600 if max_hours is not None
                            else 10 ** 12)
        try:
            result = asyncio.run(_capture(
                symbol, writer, stop_flag=stop_flag, deadline=deadline,
                min_free_gb=min_free_gb, connect_factory=connect_factory,
                reconnect_min=reconnect_min, reconnect_max=reconnect_max,
                first_trade_timeout=first_trade_timeout,
            ))
            if result['trades'] == 0:
                writer.marker('ZERO_TRADES_CAPTURE_FAIL_CLOSED')
                raise RuntimeError('No aggTrades captured; zero-data tape is not a valid pilot')
            if stop_flag.is_set():
                writer.marker('PROCESS_STOP_SIGNAL')
            elif max_hours is not None and time.monotonic() >= deadline:
                writer.marker('PROCESS_STOP_DURATION_REACHED')
            else:
                writer.marker('PROCESS_STOP_NORMAL')
            return result
        except BaseException as exc:
            writer.marker(_limit_reason('PROCESS_FAILED', exc))
            raise
    finally:
        if writer is not None:
            writer.marker('PROCESS_STOP')
            writer.close()
            print(f'[V3 RAW] saved={writer.count} anomalies={writer.anomalies} '
                  f'db={path}', flush=True)
        for sig, old_handler in handlers.items():
            signal.signal(sig, old_handler)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main():
    p = argparse.ArgumentParser(
        description='Independent Binance USD-M aggTrade TEXT recorder (research only)'
    )
    p.add_argument('--symbol', default='ETHUSDT')
    p.add_argument('--db', default=None,
                   help='New SQLite tape; refusing to overwrite/append is intentional')
    p.add_argument('--max-hours', type=float, default=None)
    p.add_argument('--min-free-gb', type=float, default=2.0)
    p.add_argument('--reconnect-min', type=float, default=0.5)
    p.add_argument('--reconnect-max', type=float, default=3.0)
    p.add_argument('--first-trade-timeout', type=float, default=20.0,
                   help='Fail closed if an open WS delivers no aggTrades within N seconds')
    p.add_argument('--verify', help='Offline check: stable SQLite read digest')
    args = p.parse_args()
    if args.verify:
        a = replay_digest(args.verify)
        b = replay_digest(args.verify)
        print('replay_read_digest:', a)
        print('deterministic_read:', a == b)
        return 0 if a == b else 2

    symbol = args.symbol.upper()
    if args.db:
        path = args.db
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path = f'reports/micro_flow_v3/tapes/{symbol}/aggtrades_raw_{stamp}.sqlite3'
    result = run_live_raw(symbol, path, min_free_gb=args.min_free_gb,
                          max_hours=args.max_hours,
                          reconnect_min=args.reconnect_min,
                          reconnect_max=args.reconnect_max,
                          first_trade_timeout=args.first_trade_timeout)
    print(f'[V3 RAW] summary={result}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
