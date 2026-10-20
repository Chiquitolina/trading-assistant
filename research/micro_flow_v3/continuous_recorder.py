"""Opt-in isolated V3 recorder with internal tape rotation, not PM2 rotation.

Never connects to shared Market Data, Redis, V1/V2 or order execution.
Old raw_recorder and its CLI retain their original behavior.
"""
import argparse
import asyncio
import fcntl
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .raw_recorder import SYMBOL_RE, _capture, _disk_free, _limit_reason
from .tape import TapeStore


class RotatingTapeWriter:
    """Same websocket receive loop across files; never appends to an old tape.

    Rotation happens on the next incoming trade. Trade-ID state and connection
    identity carry across the boundary; each file records an audit trail.
    """
    def __init__(self, directory, symbol, *, rotate_seconds=21600):
        if rotate_seconds <= 0:
            raise ValueError('rotate_seconds must be positive')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.symbol = symbol
        self.rotate_seconds = rotate_seconds
        self.capture_id = uuid.uuid4().hex
        self.file_index = 0
        self.total_count = 0
        self.total_anomalies = 0
        self.last_connection = None
        self.last_recv_wall_ns = None
        self.last_recv_mono_ns = None
        self._writer = None
        self._file_open_monotonic = 0
        self._open_new()

    def _open_new(self, previous=None):
        # Nanosecond UUID-free path remains compatible with existing tape glob.
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        suffix = f'{time.time_ns() % 1_000_000_000:09d}'
        path = self.directory / f'aggtrades_raw_{stamp}_{suffix}.sqlite3'
        if path.exists():
            raise FileExistsError(path)
        writer = TapeStore(path, self.symbol)
        self.file_index += 1
        metadata = {
            'continuous_capture_id': self.capture_id,
            'continuous_tape_index': str(self.file_index),
            'rotation_mode': 'SAME_PROCESS_SAME_WS_READ_LOOP',
            'previous_tape_basename': previous['name'] if previous else '',
            'previous_last_agg_id': str(previous['last_agg_id']) if previous and previous['last_agg_id'] is not None else '',
            'previous_last_connection': str(previous['connection']) if previous and previous['connection'] is not None else '',
            'previous_last_recv_wall_ns': str(previous['wall_ns']) if previous and previous['wall_ns'] is not None else '',
            'previous_last_recv_mono_ns': str(previous['mono_ns']) if previous and previous['mono_ns'] is not None else '',
        }
        with writer.db:
            writer.db.executemany('INSERT INTO meta(key,value) VALUES (?,?)', metadata.items())
        if previous:
            # IDs carry for discontinuity detection across the file boundary.
            writer.last_agg_id = previous['last_agg_id']
            writer.last_trade_ms = previous['last_trade_ms']
            writer.marker('PROCESS_START_ROTATED_SAME_WS_UNVERIFIED')
            writer.marker('ROTATION_IN_CONTINUOUS_WS_UNVERIFIED_UNTIL_NEXT_ID', connection=self.last_connection)
        else:
            writer.marker('PROCESS_START_UNVERIFIED_PRIOR_HISTORY')
            writer.marker('TRANSPORT_WEBSOCKETS_TEXT_PAYLOAD_NOT_TCP_OR_WS_FRAME_BYTES')
            writer.marker('NEW_TAPE_NO_CROSS_SESSION_APPEND')
        writer.maybe_flush(force=True)
        self._writer = writer
        self._file_open_monotonic = time.monotonic()
        print(f'[V3 CONTINUOUS] opened tape index={self.file_index} path={path}', flush=True)

    @property
    def path(self):
        return self._writer.path

    @property
    def last_agg_id(self):
        return self._writer.last_agg_id

    @property
    def count(self):
        return self.total_count

    @property
    def anomalies(self):
        return self.total_anomalies + self._writer.anomalies

    def marker(self, reason, **kwargs):
        self._writer.marker(reason, **kwargs)

    def maybe_flush(self, **kwargs):
        self._writer.maybe_flush(**kwargs)

    def ingest(self, raw_json, *, connection=None, wall_ns=None, mono_ns=None):
        if (self._writer.count > 0 and
                time.monotonic() - self._file_open_monotonic >= self.rotate_seconds):
            previous = {
                'name': self.path.name, 'last_agg_id': self._writer.last_agg_id,
                'last_trade_ms': self._writer.last_trade_ms,
                'connection': self.last_connection,
                'wall_ns': self.last_recv_wall_ns,
                'mono_ns': self.last_recv_mono_ns,
            }
            self._writer.marker('ROTATION_OUT_CONTINUOUS_WS', connection=self.last_connection)
            self._writer.marker('PROCESS_STOP')
            self._writer.close()
            self.total_anomalies += self._writer.anomalies
            self._writer = None
            self._open_new(previous)
        result = self._writer.ingest(raw_json, connection=connection,
                                      wall_ns=wall_ns, mono_ns=mono_ns)
        if result == 'TRADE':
            self.total_count += 1
            self.last_connection = connection
            self.last_recv_wall_ns = wall_ns
            self.last_recv_mono_ns = mono_ns
        return result

    def close(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def run_continuous(symbol, directory, *, rotate_hours=6., min_free_gb=4.,
                   duration_hours=None, connect_factory=None, first_trade_timeout=20.,
                   reconnect_min=0.5, reconnect_max=3.):
    """Single isolated recorder; launch only after stopping the old V3 process.

    duration_hours is for tests or an explicitly bounded capture. No duration
    means continuous until signal or failure. All failures close the current RAW.
    """
    symbol = symbol.upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError('Invalid symbol')
    if (rotate_hours <= 0 or min_free_gb < 0 or
            (duration_hours is not None and duration_hours <= 0)):
        raise ValueError('Invalid rotation/duration/disk parameters')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock_file = directory / '.continuous-recorder.lock'
    lock = lock_file.open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('A V3 continuous recorder already owns this symbol directory')
    writer = None
    stop = threading.Event()
    old_handlers = {}
    try:
        if _disk_free(directory) < int(min_free_gb * 1024**3):
            raise RuntimeError('V3 free disk below configured floor')
        writer = RotatingTapeWriter(directory, symbol, rotate_seconds=rotate_hours * 3600)
        def on_stop(_sig, _frame):
            stop.set()
        for s in (signal.SIGINT, signal.SIGTERM):
            old_handlers[s] = signal.getsignal(s)
            signal.signal(s, on_stop)
        deadline = time.monotonic() + (duration_hours * 3600 if duration_hours is not None else 10**12)
        try:
            result = asyncio.run(_capture(
                symbol, writer, stop_flag=stop, deadline=deadline,
                min_free_gb=min_free_gb, connect_factory=connect_factory,
                first_trade_timeout=first_trade_timeout,
                reconnect_min=reconnect_min, reconnect_max=reconnect_max,
            ))
            if writer.count == 0:
                writer.marker('ZERO_TRADES_CAPTURE_FAIL_CLOSED')
                raise RuntimeError('No aggTrades captured')
            writer.marker('PROCESS_STOP_SIGNAL' if stop.is_set() else 'PROCESS_STOP_NORMAL')
            return {**result, 'tapes': writer.file_index, 'capture_id':writer.capture_id}
        except BaseException as exc:
            if writer._writer is not None:
                writer.marker(_limit_reason('PROCESS_FAILED', exc))
            raise
    finally:
        if writer is not None and writer._writer is not None:
            writer.marker('PROCESS_STOP')
            writer.close()
        for s, original in old_handlers.items():
            signal.signal(s, original)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main(argv=None):
    p = argparse.ArgumentParser(description='V3 continuous research-only recorder')
    p.add_argument('--symbol', default='ETHUSDT')
    p.add_argument('--db-dir', default='reports/micro_flow_v3/tapes/ETHUSDT')
    p.add_argument('--rotate-hours', type=float, default=6.0)
    p.add_argument('--min-free-gb', type=float, default=4.0)
    p.add_argument('--first-trade-timeout', type=float, default=20.0)
    args = p.parse_args(argv)
    print(run_continuous(args.symbol, args.db_dir, rotate_hours=args.rotate_hours,
                         min_free_gb=args.min_free_gb,
                         first_trade_timeout=args.first_trade_timeout), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
