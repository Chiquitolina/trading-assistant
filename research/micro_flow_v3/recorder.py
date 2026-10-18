"""Micro Flow V3 research-only aggTrade tape recorder.

Uses the project's existing WSClient (python-binance) with one configurable
symbol. No order execution, Redis writes, or shared shard changes.

Important: python-binance delivers a *decoded dict*, not original wire bytes.
The tape stores a canonical JSON representation plus callback-observed receive
times. Reconnection markers record observed transport state; their timestamps
must not be misinterpreted as exact boundaries of missing market trades.
"""
import argparse
import fcntl
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

from .tape import TapeStore, replay_digest

SYMBOL_RE = re.compile(r'^[A-Z0-9]{2,30}USDT$')
QUEUE_MAX = 50_000


def _disk_free(path):
    v = os.statvfs(path)
    return v.f_bavail * v.f_frsize


def _canonical_message(message):
    """WSClient callbacks contain dicts (often Binance multiplex wrappers)."""
    return json.dumps(message, separators=(',', ':'), ensure_ascii=False)


def run_live(symbol, path, *, min_free_gb=2, max_hours=None):
    # Deferred import keeps offline replay/tests independent of Binance.
    from engine.live.ws.ws_client import WSClient

    symbol = symbol.upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError('Invalid Binance USD-M symbol')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + '.lock')
    lock = lock_path.open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another V3 recorder owns this tape')

    writer = None
    ws = None
    ws_thread = None
    q = queue.Queue(maxsize=QUEUE_MAX)
    stop_event = threading.Event()
    overflow = threading.Event()
    started = time.monotonic()
    old_handlers = {}
    connection_seen = None
    state_seen = None
    callback_errors = 0
    try:
        writer = TapeStore(path, symbol=symbol)
        writer.marker('PROCESS_START_UNVERIFIED_PRIOR_HISTORY')
        writer.marker('TRANSPORT_PYTHON_BINANCE_DECODED_JSON_NOT_RAW_WIRE')
        writer.maybe_flush(force=True)

        def on_message(message):
            nonlocal callback_errors
            # Earliest practical observation timestamp in our own callback.
            wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
            try:
                raw = _canonical_message(message)
                # Read-only generation from the project's WSClient; increments
                # when it creates a replacement python-binance WS manager.
                gen = int(getattr(ws, '_connection_generation', 0))
                q.put_nowait((raw, gen, wall_ns, mono_ns))
            except (queue.Full, TypeError, ValueError, OverflowError):
                callback_errors += 1
                overflow.set()  # fail closed; never report a partial tape as clean

        def request_stop(_signum, _frame):
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, request_stop)

        ws = WSClient(
            on_message=on_message,
            timeframes=(),
            symbols=[symbol],
            chunk_size=1,
            stale_after=45,
            include_agg_trades=True,
            client_name=f'micro-flow-v3-{symbol.lower()}',
            min_reconnect_interval=15,
            reconnect_delay_floor=5,
            reconnect_delay_cap=60,
            enable_coverage_audit=False,
        )
        writer.marker('CONNECT_ATTEMPT')
        writer.maybe_flush(force=True)
        ws.start()
        ws_thread = threading.Thread(target=ws.run, daemon=True, name='micro-flow-v3-ws-monitor')
        ws_thread.start()
        last_status_at = time.monotonic()

        while not stop_event.is_set():
            if max_hours is not None and time.monotonic() - started >= max_hours * 3600:
                print('[V3] Requested duration reached', flush=True)
                break
            if _disk_free(path.parent) < int(min_free_gb * 1024 ** 3):
                writer.marker('LOW_DISK_FAIL_CLOSED')
                raise RuntimeError('Free disk below requested floor')
            if overflow.is_set():
                writer.marker(f'CALLBACK_QUEUE_OR_ENCODING_LOSS_FAIL_CLOSED:{callback_errors}')
                raise RuntimeError('V3 callback buffer or encoding loss; capture invalid')
            if ws.fatal_error:
                writer.marker('WS_CLIENT_FATAL:' + str(ws.fatal_error)[:200])
                raise RuntimeError('WSClient entered fatal state')

            # Health markers are snapshots, not precise reconnection timestamps.
            connected = bool(ws.is_connected)
            generation = int(getattr(ws, '_connection_generation', 0))
            current_state = (connected, generation)
            if current_state != state_seen:
                writer.marker(
                    'WS_HEALTH_CONNECTED' if connected else 'WS_HEALTH_DISCONNECTED',
                    connection=generation,
                )
                state_seen = current_state

            try:
                raw, gen, wall_ns, mono_ns = q.get(timeout=0.1)
            except queue.Empty:
                writer.maybe_flush(force=True)
                continue
            writer.ingest(raw, connection=gen, wall_ns=wall_ns, mono_ns=mono_ns)
            writer.maybe_flush()
            if time.monotonic() - last_status_at > 15:
                print(f'[V3] symbol={symbol} trades={writer.count} anomalies={writer.anomalies} queue={q.qsize()}',flush=True)
                last_status_at = time.monotonic()

        writer.marker('PROCESS_STOP_REQUESTED')
    finally:
        if ws is not None:
            ws.stop()
        if ws_thread is not None:
            ws_thread.join(timeout=20)
        if writer is not None:
            # Drain callbacks already observed before shutdown.
            while not q.empty():
                raw, gen, wall_ns, mono_ns = q.get_nowait()
                writer.ingest(raw, connection=gen, wall_ns=wall_ns, mono_ns=mono_ns)
            if overflow.is_set():
                writer.marker(f'CALLBACK_QUEUE_OR_ENCODING_LOSS_FAIL_CLOSED:{callback_errors}')
            writer.marker('PROCESS_STOP')
            writer.close()
            print(f'[V3] saved={writer.count} anomalies={writer.anomalies} db={path}',flush=True)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main():
    p = argparse.ArgumentParser(description='Micro Flow V3 python-binance tape recorder; research only')
    p.add_argument('--symbol', default='ETHUSDT')
    p.add_argument('--db', default=None)
    p.add_argument('--min-free-gb', type=float, default=2.0)
    p.add_argument('--max-hours', type=float, default=None)
    p.add_argument('--verify', help='Offline readability and replay digest of SQLite tape')
    args = p.parse_args()
    if args.verify:
        a, b = replay_digest(args.verify), replay_digest(args.verify)
        print('replay_read_digest:', a)
        print('deterministic_read:', a == b)
        return 0 if a == b else 2
    symbol = args.symbol.upper()
    path = args.db or f'reports/micro_flow_v3/tapes/{symbol}/aggtrades_observed.sqlite3'
    run_live(symbol, path, min_free_gb=args.min_free_gb, max_hours=args.max_hours)
    return 0


if __name__ == '__main__':
    sys.exit(main())
