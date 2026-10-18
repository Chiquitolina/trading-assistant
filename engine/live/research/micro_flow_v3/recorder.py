"""Isolated Binance USD-M aggTrade capture. Never places orders.

Dependency: pip install websocket-client. Only one process per tape path.
"""
import argparse
import fcntl
import os
import re
import signal
import sys
import time
from pathlib import Path

from .tape import TapeStore, replay_digest

SYMBOL_RE = re.compile(r'^[A-Z0-9]{2,30}USDT$')


def _disk_free(path):
    v = os.statvfs(path)
    return v.f_bavail * v.f_frsize


def run_live(symbol, path, *, min_free_gb=2, max_hours=None):
    import websocket  # deferred import so offline tests need no WebSocket package
    symbol = symbol.upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError('Invalid Binance USD-M symbol')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_suffix(path.suffix+'.lock')
    lock = lock_file.open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another V3 recorder owns the tape lock')
    stop = False
    def request_stop(signum, _frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    writer = TapeStore(path, symbol=symbol)
    started = time.monotonic()
    connection = 0
    had_connected = False
    try:
        writer.marker('PROCESS_START_UNVERIFIED_PRIOR_HISTORY')
        writer.maybe_flush(force=True)
        url = f'wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade'
        while not stop:
            if max_hours is not None and (time.monotonic()-started) >= max_hours*3600:
                print('Reached requested duration',flush=True)
                break
            if _disk_free(path.parent) < int(min_free_gb * 1024**3):
                writer.marker('LOW_DISK_FAIL_CLOSED')
                writer.maybe_flush(force=True)
                raise RuntimeError('Free disk below floor; stopped without deleting any data')
            connection += 1
            sock = None
            writer.marker('CONNECT_ATTEMPT', connection=connection)
            writer.maybe_flush(force=True)
            try:
                sock = websocket.create_connection(url,timeout=5,enable_multithread=True)
                sock.settimeout(1)
                writer.marker('CONNECTED',connection=connection)
                writer.maybe_flush(force=True)
                had_connected = True
                print(f'[V3] CONNECTED symbol={symbol} connection={connection}',flush=True)
                last_recv = time.monotonic()
                while not stop:
                    if max_hours is not None and time.monotonic()-started >= max_hours*3600:
                        stop = True
                        break
                    if _disk_free(path.parent) < int(min_free_gb * 1024**3):
                        writer.marker('LOW_DISK_FAIL_CLOSED',connection=connection)
                        writer.maybe_flush(force=True)
                        raise RuntimeError('Free disk below floor')
                    try:
                        raw = sock.recv()
                    except websocket.WebSocketTimeoutException:
                        writer.maybe_flush(force=True)
                        if time.monotonic()-last_recv > 30:
                            raise TimeoutError('No WebSocket message for 30s')
                        continue
                    # Capture before parsing and before SQLite insertion.
                    wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
                    if not raw:
                        raise ConnectionError('Socket closed')
                    last_recv = time.monotonic()
                    writer.ingest(raw,connection=connection,wall_ns=wall_ns,mono_ns=mono_ns)
                    writer.maybe_flush()
            except Exception as exc:
                writer.marker('DISCONNECTED:'+type(exc).__name__, connection=connection)
                writer.maybe_flush(force=True)
                if isinstance(exc, RuntimeError) and 'Free disk' in str(exc):
                    raise
                print(f'[V3] disconnected connection={connection} cause={type(exc).__name__}',flush=True)
            finally:
                if sock is not None:
                    try: sock.close()
                    except Exception: pass
            if stop:
                break
            time.sleep(3)
        writer.marker('PROCESS_STOP')
        writer.maybe_flush(force=True)
        print(f'[V3] saved={writer.count} anomalies={writer.anomalies} db={path}',flush=True)
    finally:
        writer.close()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main():
    p=argparse.ArgumentParser(description='Micro Flow V3 standalone aggTrade recorder, research only')
    p.add_argument('--symbol',default='ETHUSDT')
    p.add_argument('--db',default=None)
    p.add_argument('--min-free-gb',type=float,default=2.0)
    p.add_argument('--max-hours',type=float,default=None,help='Pilot duration; optional')
    p.add_argument('--verify',help='Offline replay and consistency of a SQLite tape')
    args=p.parse_args()
    if args.verify:
        first=replay_digest(args.verify)
        second=replay_digest(args.verify)
        print('replay_read_digest:',first)
        print('deterministic_read:',first==second)
        return 0 if first==second else 2
    symbol=args.symbol.upper()
    db=args.db or f'reports/micro_flow_v3/tapes/{symbol}/aggtrades_observed.sqlite3'
    run_live(symbol,db,min_free_gb=args.min_free_gb,max_hours=args.max_hours)
    return 0


if __name__=='__main__':
    sys.exit(main())
