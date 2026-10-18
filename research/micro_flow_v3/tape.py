"""Append-only SQLite aggTrade tape, including receive timestamps and outages.

Only research records are produced. No orders, Redis, V1/V2 or shared shard state.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = '1'


class TapeStore:
    def __init__(self, path, symbol='ETHUSDT', *, batch_size=200, flush_interval=0.25):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.symbol = symbol.upper()
        if not self.symbol.endswith('USDT') or not self.symbol.replace('USDT','').isalnum():
            raise ValueError('Expected Binance USDT futures symbol e.g. ETHUSDT')
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tape(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            recv_wall_ns INTEGER NOT NULL,
            recv_mono_ns INTEGER NOT NULL,
            trade_ms INTEGER,
            exchange_event_ms INTEGER,
            agg_id INTEGER,
            price TEXT,
            quantity TEXT,
            buyer_is_maker INTEGER,
            connection INTEGER,
            reason TEXT,
            raw_json TEXT,
            quality TEXT
        );
        CREATE INDEX IF NOT EXISTS tape_agg ON tape(symbol, agg_id);
        CREATE INDEX IF NOT EXISTS tape_trade_ms ON tape(symbol, trade_ms);
        ''')
        metadata = dict(self.db.execute('SELECT key,value FROM meta'))
        if metadata:
            if metadata.get('schema_version') != SCHEMA_VERSION or metadata.get('symbol') != self.symbol:
                self.db.close()
                raise ValueError('Tape has a different schema version or symbol; use a distinct file')
        else:
            if self.db.execute('SELECT COUNT(*) FROM tape').fetchone()[0]:
                self.db.close()
                raise ValueError('Existing tape has rows but no metadata')
            self.db.executemany('INSERT INTO meta(key,value) VALUES (?,?)', [
                ('schema_version', SCHEMA_VERSION), ('symbol', self.symbol),
                ('source', 'binance-usdm-aggTrade-websocket'),
                ('data_classification', 'LIVE_OBSERVED_NOT_HISTORICAL_BACKFILL'),
            ])
            self.db.commit()
        last = self.db.execute(
            'SELECT agg_id,trade_ms FROM tape WHERE kind=? AND agg_id IS NOT NULL ORDER BY seq DESC LIMIT 1',
            ('TRADE',)
        ).fetchone()
        self.last_agg_id = last[0] if last else None
        self.last_trade_ms = last[1] if last else None
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.01, float(flush_interval))
        self.pending = []
        self.last_flush = time.monotonic()
        self.count = 0
        self.anomalies = 0

    @staticmethod
    def _now():
        return time.time_ns(), time.monotonic_ns()

    def _append(self, kind, *, wall_ns, mono_ns, trade_ms=None, event_ms=None,
                agg_id=None, price=None, qty=None, maker=None, connection=None,
                reason=None, raw=None, quality=None):
        self.pending.append((kind, self.symbol, int(wall_ns), int(mono_ns), trade_ms,
                             event_ms, agg_id, price, qty, maker, connection,
                             reason, raw, quality))
        self.maybe_flush()

    def marker(self, reason, *, connection=None, wall_ns=None, mono_ns=None):
        if wall_ns is None or mono_ns is None:
            wall_ns, mono_ns = self._now()
        self._append('MARKER', wall_ns=wall_ns, mono_ns=mono_ns,
                     reason=str(reason), connection=connection)

    def ingest(self, raw_json, *, connection=None, wall_ns=None, mono_ns=None):
        """Record one observed message; anomalies are saved, never silently repaired.

        Returns TRADE or ignored message kind. Invalid payloads are stored as markers
        only with a digest (not copied verbatim), to avoid accidental secret storage.
        """
        if wall_ns is None or mono_ns is None:
            wall_ns, mono_ns = self._now()
        try:
            payload = json.loads(raw_json)
            if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
                payload = payload['data']
            if not isinstance(payload, dict) or payload.get('e') != 'aggTrade':
                return 'IGNORED'
            symbol = str(payload['s']).upper()
            if symbol != self.symbol:
                raise ValueError('wrong symbol')
            agg_id = int(payload['a'])
            trade_ms = int(payload['T'])
            event_ms = int(payload['E']) if 'E' in payload else None
            price, qty = str(payload['p']), str(payload['q'])
            if float(price) <= 0 or float(qty) <= 0 or not isinstance(payload['m'], bool):
                raise ValueError('invalid price, quantity or aggressor')
            maker = int(payload['m'])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError, OverflowError):
            self.anomalies += 1
            digest = hashlib.sha256(str(raw_json).encode('utf-8',errors='replace')).hexdigest()[:16]
            self.marker('INVALID_PAYLOAD_SHA256_PREFIX:'+digest, connection=connection,
                        wall_ns=wall_ns, mono_ns=mono_ns)
            return 'INVALID'

        flags = []
        if self.last_agg_id is not None:
            if agg_id <= self.last_agg_id:
                flags.append('ID_NONINCREASING')
            elif agg_id > self.last_agg_id + 1:
                flags.append('ID_JUMP_SUSPECTED')
        if self.last_trade_ms is not None and trade_ms < self.last_trade_ms:
            flags.append('TRADE_TIME_REGRESSION')
        if trade_ms > (wall_ns // 1_000_000) + 10_000:
            flags.append('TRADE_TIME_AHEAD_OF_LOCAL_CLOCK')
        self._append('TRADE', wall_ns=wall_ns, mono_ns=mono_ns,
                     trade_ms=trade_ms, event_ms=event_ms, agg_id=agg_id,
                     price=price, qty=qty, maker=maker, connection=connection,
                     raw=raw_json, quality='|'.join(flags) if flags else 'OK')
        self.count += 1
        if flags:
            self.anomalies += 1
            self.marker('|'.join(flags), connection=connection,
                        wall_ns=wall_ns, mono_ns=mono_ns)
        # Do not regress baseline on a duplicate or out-of-order observation.
        if self.last_agg_id is None or agg_id > self.last_agg_id:
            self.last_agg_id = agg_id
        if self.last_trade_ms is None or trade_ms > self.last_trade_ms:
            self.last_trade_ms = trade_ms
        return 'TRADE'

    def maybe_flush(self, *, force=False):
        if not self.pending:
            return
        if not force and len(self.pending) < self.batch_size and time.monotonic() - self.last_flush < self.flush_interval:
            return
        with self.db:
            self.db.executemany('''INSERT INTO tape(
                kind,symbol,recv_wall_ns,recv_mono_ns,trade_ms,exchange_event_ms,
                agg_id,price,quantity,buyer_is_maker,connection,reason,raw_json,quality
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', self.pending)
        self.pending.clear()
        self.last_flush = time.monotonic()

    def close(self):
        try:
            self.maybe_flush(force=True)
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            self.db.close()


def replay_digest(path):
    """Read every row in stored sequence, hashing a canonical, stable representation.

    Repeating this is a tape determinism/readability test, NOT live/replay strategy parity.
    """
    con = sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro', uri=True)
    try:
        cols = ('seq','kind','symbol','recv_wall_ns','recv_mono_ns','trade_ms',
                'exchange_event_ms','agg_id','price','quantity','buyer_is_maker',
                'connection','reason','raw_json','quality')
        h = hashlib.sha256()
        count = 0
        for row in con.execute('SELECT '+','.join(cols)+' FROM tape ORDER BY seq'):
            h.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
            h.update(b'\n')
            count += 1
        return count, h.hexdigest()
    finally:
        con.close()
