"""Research-only clock-time outcomes backed by continuous Replay V2 evidence.

An inferred no-trade interval is not an executable quote. This evaluator
preserves the existing observed-entry/observed-endpoint requirements and only
recovers MFE/MAE/return proxy through RAW-ID-chain-consistent silent seconds.
It never backfills OHLC, alters detectors, or retroactively makes an entry.
"""
from bisect import bisect_left
import json
from pathlib import Path

import math


class DiscoveryError(ValueError):
    pass


def validate_buckets(buckets):
    """Independent V2 input checks (no dependency on LIVE engine imports)."""
    result=[]
    last_time_by_segment={}
    last_avail_by_segment={}
    seen_segments=set()
    previous_segment=None
    symbol=None
    for b in buckets:
        row=dict(b)
        if row.get('event_type')!='BUCKET_1S':
            raise DiscoveryError('Unknown bucket type')
        seg=int(row['segment_id'])
        t=int(row['second_start_ms'])
        end=int(row['second_end_ms_exclusive'])
        avail=int(row['available_wall_ns'])
        if seg<1 or t%1000 or end!=t+1000 or avail<end*1_000_000:
            raise DiscoveryError('Non-causal bucket')
        if previous_segment!=seg:
            if seg in seen_segments:
                raise DiscoveryError('Interleaved segments')
            seen_segments.add(seg)
            previous_segment=seg
        if t<=last_time_by_segment.get(seg,-1) or avail<=last_avail_by_segment.get(seg,-1):
            raise DiscoveryError('Non-monotonic bucket')
        last_time_by_segment[seg]=t
        last_avail_by_segment[seg]=avail
        if symbol is None:symbol=row['symbol']
        if symbol!=row['symbol']:
            raise DiscoveryError('Mixed symbols')
        for col in ('open','high','low','close','buy_qty','sell_qty'):
            row[col]=float(row[col])
            if not math.isfinite(row[col]):
                raise DiscoveryError('Nonfinite price/volume')
        if row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):
            raise DiscoveryError('Incoherent OHLC')
        row['segment_id']=seg
        row['second_start_ms']=t
        row['available_wall_ns']=avail
        result.append(row)
    return result


def load_gap_aware_inputs(replay_jsonl, coverage_jsonl, summary_json):
    """Verify bytes against immutable Replay-V2 manifest BEFORE evaluating."""
    import hashlib
    replay = Path(replay_jsonl)
    coverage = Path(coverage_jsonl)
    summary = json.loads(Path(summary_json).read_text())
    if summary.get('schema') != 'micro-flow-v3-continuous-replay-v2':
        raise DiscoveryError('Expected continuous Replay V2 manifest')
    for p,key in ((replay,'replay_jsonl_sha256'),(coverage,'coverage_jsonl_sha256')):
        h=hashlib.sha256()
        with p.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):
                h.update(chunk)
        if h.hexdigest()!=summary.get(key):
            raise DiscoveryError('Continuous Replay V2 hash mismatch')
    rows=validate_buckets(json.loads(x) for x in replay.read_text().splitlines())
    intervals=[json.loads(x) for x in coverage.read_text().splitlines()]
    validate_intervals(intervals)
    return rows,intervals,summary


def validate_intervals(intervals):
    """No untrusted intervals can overlap, cross segments, or predate validation."""
    last_by_seg={}
    for item in intervals:
        if item.get('event_type')!='ID_CHAIN_CONSISTENT_NO_TRADE_INTERVAL_DIAGNOSTIC':
            raise DiscoveryError('Unknown coverage interval type')
        seg=int(item['segment_id'])
        start=int(item['start_ms'])
        end=int(item['end_ms_exclusive'])
        if (seg<1 or start<0 or start%1000 or end%1000 or end<=start or
                end-start!=int(item['seconds'])*1000 or
                int(item['next_agg_id'])!=int(item['prev_agg_id'])+1 or
                int(item['verified_only_after_wall_ns'])<end*1_000_000):
            raise DiscoveryError('Invalid coverage interval')
        if seg in last_by_seg and last_by_seg[seg]>start:
            raise DiscoveryError('Overlapping or unsorted coverage intervals')
        last_by_seg[seg]=end
    return intervals


def _covered(start_ms,end_ms,segments):
    """Require positive proof for EACH absent second, not only endpoints."""
    if start_ms>=end_ms:
        return True
    cursor=start_ms
    for a,b in segments:
        if b<=cursor:
            continue
        if a>cursor:
            break
        cursor=max(cursor,b)
        if cursor>=end_ms:
            return True
    return False


def evaluate_gap_aware_outcome(rows, intervals, *, segment_id, entry_second_start_ms,
                               entry_price, side, horizon_seconds,
                               endpoint_freshness_seconds=3):
    """Directional terminal-return and print-only excursion proxy.

    The entry must be a previously established causal proxy; we never select
    entries from the future outcome. The endpoint still MUST have a fresh
    observed trade print near the clock target, as in the original evaluator.
    """
    if side not in ('HIGH','LOW') or horizon_seconds<=0:
        raise DiscoveryError('Invalid side/horizon')
    direction=-1 if side=='HIGH' else 1
    data=[r for r in rows if int(r['segment_id'])==int(segment_id)]
    starts=[r['second_start_ms'] for r in data]
    i=bisect_left(starts,int(entry_second_start_ms))
    if i==len(data) or starts[i]!=int(entry_second_start_ms):
        raise DiscoveryError('Entry must exist in its causal segment')
    price=float(entry_price)
    if price<=0 or abs(float(data[i]['open'])/price-1)>1e-10:
        raise DiscoveryError('Entry price differs from observed bucket OPEN')
    target=starts[i]+int(horizon_seconds)*1000
    j=bisect_left(starts,target)
    if (j>=len(data) or starts[j]-target>endpoint_freshness_seconds*1000 or
            data[j]['available_wall_ns']<=data[i]['available_wall_ns']):
        return {'state':'CENSORED_NO_FRESH_ENDPOINT','return_directional_pct':None,
                'mfe_directional_pct':None,'mae_directional_pct':None}
    relevant=sorted((int(x['start_ms']),int(x['end_ms_exclusive']))
                    for x in intervals if int(x['segment_id'])==int(segment_id))
    recovered=0
    for k in range(i+1,j+1):
        a,b=starts[k-1],starts[k]
        if b-a<=endpoint_freshness_seconds*1000:
            continue  # Legacy policy retains small observed gaps unchanged.
        if not _covered(a+1000,b,relevant):
            return {'state':'CENSORED_UNVERIFIED_OBSERVATION_GAP',
                    'return_directional_pct':None,'mfe_directional_pct':None,
                    'mae_directional_pct':None}
        recovered+=b//1000-a//1000-1
    path=data[i+1:j+1]
    if direction>0:
        mfe=max([0.]+[(float(r['high'])/price-1)*100 for r in path])
        mae=min([0.]+[(float(r['low'])/price-1)*100 for r in path])
    else:
        mfe=max([0.]+[(1-float(r['low'])/price)*100 for r in path])
        mae=min([0.]+[(1-float(r['high'])/price)*100 for r in path])
    return {
        'state':('OBSERVED_PROXY_WITH_AUDITED_SILENT_SECONDS'
                 if recovered else 'OBSERVED_PROXY'),
        'return_directional_pct':direction*(float(data[j]['close'])/price-1)*100,
        'mfe_directional_pct':mfe,'mae_directional_pct':mae,
        'id_chain_consistent_silent_seconds':recovered,
        'observed_trade_buckets_in_path':len(path),
        'endpoint_available_wall_ns':data[j]['available_wall_ns'],
        'caution':'TRADE_PRINT_PROXY_NOT_EXECUTABLE_FILL_OR_ORDER_BOOK',
    }


def evaluate_many_gap_aware(rows,intervals,entries,horizons=(60,180,300,600,900,1800,3600)):
    """Evaluate existing entry anchors; caller must guard entry source provenance."""
    result=[]
    for e in entries:
        for h in horizons:
            result.append({'entry_id':e['entry_id'],'horizon_seconds':h,
                           **evaluate_gap_aware_outcome(rows,intervals,
                               segment_id=e['segment_id'],
                               entry_second_start_ms=e['entry_second_start_ms'],
                               entry_price=e['entry_price'], side=e['side'],
                               horizon_seconds=h)})
    return result
