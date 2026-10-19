"""Micro Flow V3: read-only, causal one-second replay of recorded aggTrades.

Produces immutable JSONL research features, NOT trading signals or executions.
A second is published only when a later trade from the SAME continuous segment
has actually arrived. The first and last observed second of each segment are
excluded (possibly incomplete). No synthetic seconds, backfills, or cross-gap
rolling state.
"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from .tape import SCHEMA_VERSION, replay_digest


class ReplayError(ValueError):
    pass


def _as_text(value):
    return format(value, 'f')


def _break_marker(reason):
    if not reason:
        return False
    return reason.startswith((
        'PROCESS_START', 'PROCESS_STOP', 'PROCESS_FAILED',
        'WS_CLOSED', 'WS_HEALTH_DISCONNECTED', 'WS_TRANSPORT_',
        'RECONNECT_COVERAGE_UNVERIFIED', 'CONNECT_ATTEMPT',
        'LOW_DISK', 'INVALID_', 'UNEXPECTED_BINARY',
        'CALLBACK_QUEUE', 'WS_CLIENT_FATAL', 'NO_FIRST_',
        'ZERO_TRADES_', 'ID_JUMP', 'ID_NONINCREASING',
        'TRADE_TIME_REGRESSION', 'TRADE_TIME_AHEAD_', 'GAP_DETAILS',
    ))


class Bucket:
    def __init__(self, row):
        self.second_ns = row['recv_wall_ns'] // 1_000_000_000 * 1_000_000_000
        self.first_seq = row['seq']
        self.last_seq = row['seq']
        self.first_id = row['agg_id']
        self.last_id = row['agg_id']
        self.count = 0
        self.buy_qty = Decimal(0)
        self.sell_qty = Decimal(0)
        self.buy_quote = Decimal(0)
        self.sell_quote = Decimal(0)
        self.open = self.high = self.low = self.close = None
        self.add(row)

    def add(self, row):
        price = Decimal(row['price'])
        qty = Decimal(row['quantity'])
        if not price.is_finite() or not qty.is_finite() or price <= 0 or qty <= 0:
            raise ReplayError('Invalid price or qty in stored trade')
        self.count += 1
        self.last_seq = row['seq']
        self.last_id = row['agg_id']
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if row['buyer_is_maker'] == 0:  # Buyer is taker: aggressive buy
            self.buy_qty += qty
            self.buy_quote += qty * price
        elif row['buyer_is_maker'] == 1:  # Seller is taker: aggressive sell
            self.sell_qty += qty
            self.sell_quote += qty * price
        else:
            raise ReplayError('buyer_is_maker must be 0 or 1')

    def event(self, symbol, segment_id, available_ns, cvd_qty):
        return {
            'event_type': 'BUCKET_1S', 'symbol': symbol,
            'segment_id': segment_id,
            'second_start_ms': self.second_ns // 1_000_000,
            'second_end_ms_exclusive': self.second_ns // 1_000_000 + 1000,
            'available_wall_ns': available_ns,
            'available_ms': available_ns // 1_000_000,
            'first_seq': self.first_seq, 'last_seq': self.last_seq,
            'first_agg_id': self.first_id, 'last_agg_id': self.last_id,
            'trades': self.count,
            'open': _as_text(self.open), 'high': _as_text(self.high),
            'low': _as_text(self.low), 'close': _as_text(self.close),
            'buy_qty': _as_text(self.buy_qty),
            'sell_qty': _as_text(self.sell_qty),
            'buy_quote': _as_text(self.buy_quote),
            'sell_quote': _as_text(self.sell_quote),
            'delta_qty': _as_text(self.buy_qty - self.sell_qty),
            'cvd_qty': _as_text(cvd_qty),
        }


def _open_ro(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def replay_tape(db_path, output_path, *, summary_path=None):
    """Replay ONE completed tape; never mutates its SQLite or existing outputs.

    A segment boundary discards its partial head/tail seconds. A bucket is
    available only upon the *next* received trade in a later observed second.
    """
    source = Path(db_path).resolve()
    output = Path(output_path).resolve()
    summary_output = Path(summary_path).resolve() if summary_path else None
    if source == output or (summary_output and source == summary_output):
        raise ReplayError('Replay output must differ from the source SQLite')
    if output.exists() or (summary_output and summary_output.exists()):
        raise FileExistsError('Output already exists; use a new research run path')
    if summary_output and output == summary_output:
        raise ReplayError('Summary and output paths must differ')

    with _open_ro(source) as con:
        meta = dict(con.execute('SELECT key,value FROM meta').fetchall())
        if meta.get('schema_version') != SCHEMA_VERSION or not meta.get('symbol'):
            raise ReplayError('Unknown tape schema or missing symbol')
        if meta.get('data_classification') != 'LIVE_OBSERVED_NOT_HISTORICAL_BACKFILL':
            raise ReplayError('Tape has unknown provenance')
        tail = con.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        if not tail or tail['kind'] != 'MARKER' or tail['reason'] != 'PROCESS_STOP':
            raise ReplayError('Tape is not closed. Replay a completed tape, not the active PM2 SQLite.')

    source_rows, source_digest = replay_digest(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    if summary_output:
        summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        'schema': 'micro-flow-v3-causal-buckets-v1',
        'symbol': meta['symbol'],
        'source_tape_rows': source_rows,
        'source_tape_digest_sha256': source_digest,
        'closed_source_required': True,
        'bucket_time_basis': 'observed_callback_receive_wall_ns',
        'availability': 'first_later_observed_trade_in_same_segment',
        'head_tail_policy': 'discard_first_and_last_second_per_segment',
        'segments': [], 'trades_seen': 0, 'trades_excluded': 0,
        'buckets_emitted': 0, 'head_buckets_discarded': 0,
        'tail_buckets_discarded': 0, 'boundary_events': 0,
    }
    h = hashlib.sha256()
    seg = None
    bucket = None
    prev = None
    pending_reason = 'TAPE_START'
    pending_agg_quality_marker = False
    last_bucket_ns = None

    def finish_segment():
        nonlocal seg, bucket, prev, last_bucket_ns
        if seg is not None:
            if bucket is not None:
                # Last second may be incomplete; do not publish it.
                summary['tail_buckets_discarded'] += 1
                seg['tail_buckets_discarded'] += 1
            summary['segments'].append(seg)
        seg, bucket, prev, last_bucket_ns = None, None, None, None

    def start_segment(row, reason):
        nonlocal seg, pending_reason
        seg = {
            'segment_id': len(summary['segments']) + 1,
            'start_reason': reason,
            'first_seq': row['seq'], 'last_seq': row['seq'],
            'first_agg_id': row['agg_id'], 'last_agg_id': row['agg_id'],
            'trades': 0, 'buckets_emitted': 0,
            'head_buckets_discarded': 0, 'tail_buckets_discarded': 0,
        }
        pending_reason = ''

    # Consuming a completed tape from beginning is deterministic; no wall-clock
    # calls, no random seeds, no process-local state in emitted JSON lines.
    created_output = False
    created_summary = False
    try:
        with _open_ro(source) as con, output.open('x', encoding='utf-8', newline='\n') as out:
            created_output = True
            rows = con.execute('''SELECT seq,kind,symbol,recv_wall_ns,recv_mono_ns,
                                  trade_ms,agg_id,price,quantity,buyer_is_maker,
                                  connection,reason,quality
                                  FROM tape ORDER BY seq''')
            for row in rows:
                if row['kind'] == 'MARKER':
                    reason = row['reason'] or ''
                    if pending_agg_quality_marker and reason.startswith((
                        'ID_JUMP', 'ID_NONINCREASING', 'TRADE_TIME_', 'GAP_DETAILS')):
                        continue  # TapeStore echo after its already-flagged trade
                    if _break_marker(reason):
                        if seg is not None:
                            finish_segment()
                        pending_reason = reason
                        summary['boundary_events'] += 1
                    continue
                if row['kind'] != 'TRADE':
                    continue
                summary['trades_seen'] += 1
                pending_agg_quality_marker = False
                if row['symbol'] != meta['symbol']:
                    raise ReplayError(f'Unexpected symbol on seq={row["seq"]}')
                if any(row[name] is None for name in (
                    'recv_wall_ns','recv_mono_ns','agg_id','price','quantity','buyer_is_maker')):
                    raise ReplayError(f'Missing required field on seq={row["seq"]}')
                if row['quality'] not in (None, 'OK'):
                    # Detect and reset *before* ingesting suspect trade.
                    if seg is not None:
                        finish_segment()
                    pending_reason = 'TRADE_QUALITY:' + str(row['quality'])
                    pending_agg_quality_marker = True
                    if 'ID_NONINCREASING' in row['quality']:
                        summary['trades_excluded'] += 1
                        continue
                if seg is not None and prev is not None:
                    if (row['agg_id'] != prev['agg_id'] + 1
                            or row['recv_wall_ns'] < prev['recv_wall_ns']
                            or row['recv_mono_ns'] < prev['recv_mono_ns']
                            or row['connection'] != prev['connection']):
                        finish_segment()
                        pending_reason = 'OBSERVED_ORDER_OR_GENERATION_DISCONTINUITY'
                if seg is None:
                    start_segment(row, pending_reason)
                seg['trades'] += 1
                seg['last_seq'] = row['seq']
                seg['last_agg_id'] = row['agg_id']

                second_ns = row['recv_wall_ns'] // 1_000_000_000 * 1_000_000_000
                if bucket is None:
                    bucket = Bucket(row)
                elif second_ns == bucket.second_ns:
                    bucket.add(row)
                elif second_ns > bucket.second_ns:
                    if last_bucket_ns is None:
                        # Do not pretend capture started at the beginning of a second.
                        summary['head_buckets_discarded'] += 1
                        seg['head_buckets_discarded'] += 1
                    else:
                        cvd = seg.get('_cvd', Decimal(0)) + bucket.buy_qty - bucket.sell_qty
                        seg['_cvd'] = cvd
                        event = bucket.event(meta['symbol'], seg['segment_id'], row['recv_wall_ns'], cvd)
                        payload = json.dumps(event, sort_keys=True, ensure_ascii=False,
                                             separators=(',', ':')).encode('utf-8') + b'\n'
                        out.write(payload.decode('utf-8'))
                        h.update(payload)
                        summary['buckets_emitted'] += 1
                        seg['buckets_emitted'] += 1
                    last_bucket_ns = bucket.second_ns
                    bucket = Bucket(row)
                else:
                    # Timestamp regression is not a valid same-segment 1s bucket.
                    finish_segment()
                    start_segment(row, 'RECEIVE_SECOND_REGRESSION')
                    bucket = Bucket(row)
                prev = row
            finish_segment()
        # Decimal accumulators are internal only.
        for item in summary['segments']:
            item.pop('_cvd', None)
        summary['replay_jsonl_sha256'] = h.hexdigest()
        if summary_output:
            with summary_output.open('x', encoding='utf-8') as f:
                created_summary = True
                json.dump(summary, f, sort_keys=True, ensure_ascii=False, indent=2)
                f.write('\n')
        return summary
    except Exception:
        # Keep no partial outputs on replay failure; source SQLite untouched.
        if created_output:
            output.unlink(missing_ok=True)
        if created_summary:
            summary_output.unlink(missing_ok=True)
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description='Causal, read-only Micro Flow V3 1-second replay')
    p.add_argument('--db', required=True, help='Completed / closed SQLite tape')
    p.add_argument('--out', required=True, help='New output JSONL path')
    p.add_argument('--summary', help='New JSON summary path')
    args = p.parse_args(argv)
    summary = replay_tape(args.db, args.out, summary_path=args.summary)
    print(json.dumps({k: summary[k] for k in (
        'symbol', 'trades_seen', 'trades_excluded', 'buckets_emitted',
        'head_buckets_discarded','tail_buckets_discarded', 'replay_jsonl_sha256')},
        sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
