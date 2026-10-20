"""Opt-in multi-tape causal Replay V2 (research only, never rewrites V1).

A planned tape rotation is joined ONLY if source metadata, marker evidence,
same WS connection, ID adjacency and both local clocks agree. Actual WS
reconnects and old PM2 six-hour restarts NEVER bridge. Long silent periods
remain unverified. Seconds without trades are represented as a separate
*diagnostic* with publication at the next observed trade; they are NOT
synthetic OHLC buckets or executable fills.
"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .replay import Bucket, ReplayError, _break_marker, _open_ro
from .tape import SCHEMA_VERSION, replay_digest


def _source(path):
    path = Path(path).resolve()
    with _open_ro(path) as db:
        meta = dict(db.execute('SELECT key,value FROM meta'))
        if (meta.get('schema_version') != SCHEMA_VERSION or
                meta.get('data_classification') != 'LIVE_OBSERVED_NOT_HISTORICAL_BACKFILL'):
            raise ReplayError('Unknown RAW schema/provenance')
        tail = db.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        if not tail or (tail['kind'], tail['reason']) != ('MARKER', 'PROCESS_STOP'):
            raise ReplayError(f'RAW source not closed: {path}')
        first = db.execute("SELECT * FROM tape WHERE kind='TRADE' ORDER BY seq LIMIT 1").fetchone()
        last = db.execute("SELECT * FROM tape WHERE kind='TRADE' ORDER BY seq DESC LIMIT 1").fetchone()
        if not first or not last:
            raise ReplayError(f'RAW has no trades: {path}')
        ends = [r[0] for r in db.execute(
            "SELECT reason FROM tape WHERE kind='MARKER' AND "
            "(reason LIKE 'ROTATION_OUT_%' OR reason LIKE 'ROTATION_IN_%')")]
    count,digest = replay_digest(path)
    return {'path': path,'meta':meta,'first':dict(first),'last':dict(last),
            'markers':ends,'source_rows':count,'source_digest_sha256':digest}


def _certified_join(a, b, max_silence_seconds):
    ma, mb = a['meta'], b['meta']
    x,y = a['last'], b['first']
    try:
        return _certified_join_checked(a, b, max_silence_seconds)
    except (TypeError, ValueError):
        return False


def _certified_join_checked(a, b, max_silence_seconds):
    ma, mb = a['meta'], b['meta']
    x, y = a['last'], b['first']
    checks = (
        ma.get('continuous_capture_id') and
        ma.get('continuous_capture_id') == mb.get('continuous_capture_id') and
        ma.get('rotation_mode') == 'SAME_PROCESS_SAME_WS_READ_LOOP' and
        mb.get('rotation_mode') == 'SAME_PROCESS_SAME_WS_READ_LOOP' and
        int(mb.get('continuous_tape_index',-1)) == int(ma.get('continuous_tape_index',-2)) + 1 and
        mb.get('previous_tape_basename') == a['path'].name and
        mb.get('previous_last_agg_id') == str(x['agg_id']) and
        mb.get('previous_last_connection') == str(x['connection']) and
        mb.get('previous_last_recv_wall_ns') == str(x['recv_wall_ns']) and
        mb.get('previous_last_recv_mono_ns') == str(x['recv_mono_ns']) and
        'ROTATION_OUT_CONTINUOUS_WS' in a['markers'] and
        'ROTATION_IN_CONTINUOUS_WS_UNVERIFIED_UNTIL_NEXT_ID' in b['markers'] and
        x['symbol'] == y['symbol'] and
        x['connection'] is not None and x['connection'] == y['connection'] and
        y['agg_id'] == x['agg_id'] + 1 and
        y['recv_wall_ns'] >= x['recv_wall_ns'] and
        y['recv_mono_ns'] >= x['recv_mono_ns'] and
        (y['recv_wall_ns'] - x['recv_wall_ns']) <= max_silence_seconds * 1_000_000_000 and
        (x['quality'] or 'OK') == 'OK' and (y['quality'] or 'OK') == 'OK'
    )
    return bool(checks)


def replay_continuous(tapes, out_path, *, summary_path=None, coverage_path=None,
                      max_silence_seconds=15):
    paths = [Path(p).resolve() for p in tapes]
    if not paths or len(set(paths)) != len(paths):
        raise ReplayError('Require unique, ordered, closed RAW inputs')
    if not (1 <= max_silence_seconds <= 60):
        raise ReplayError('Invalid max_silence_seconds')
    out = Path(out_path).resolve()
    summary_out = Path(summary_path).resolve() if summary_path else None
    cov_out = Path(coverage_path).resolve() if coverage_path else None
    dest = [out] + ([summary_out] if summary_out else []) + ([cov_out] if cov_out else [])
    if len(set(dest)) != len(dest) or any(p.exists() or p in paths for p in dest):
        raise ReplayError('Output already exists, equals RAW, or conflicts with another output')
    sources = [_source(p) for p in paths]
    symbols = {s['meta'].get('symbol') for s in sources}
    if len(symbols) != 1:
        raise ReplayError('Mixed source symbols')
    joined = [_certified_join(a,b,max_silence_seconds) for a,b in zip(sources,sources[1:])]
    summary = {
        'schema': 'micro-flow-v3-continuous-replay-v2', 'symbol': next(iter(symbols)),
        'source_tapes': [{k:(str(v) if isinstance(v,Path) else v) for k,v in s.items()
                          if k in ('path','source_rows','source_digest_sha256')} for s in sources],
        'boundary_join_certified': joined,
        'boundary_join_count': sum(joined), 'boundary_unverified_count': len(joined)-sum(joined),
        'max_silence_seconds':max_silence_seconds,
        'availability':'first_later_trade_same_verified_segment',
        'no_trade_interval_policy':'DIAGNOSTIC_ONLY_ID_CHAIN_CONSISTENT_NOT_ZERO_VOLUME_FILL',
        'segments':[], 'buckets_emitted':0,'trades_seen':0,'trades_excluded':0,
        'head_buckets_discarded':0,'tail_buckets_discarded':0,
        'inferred_no_trade_seconds':0,'coverage_intervals':0,'long_silence_boundaries':0,
    }
    for p in dest:
        p.parent.mkdir(parents=True, exist_ok=True)
    seg = bucket = prev = None
    last_bucket_ns = None
    pending_reason = 'TAPE_START'
    echoed_quality = False
    h = hashlib.sha256()
    hc = hashlib.sha256()
    created = []

    def finish():
        nonlocal seg,bucket,prev,last_bucket_ns
        if seg is not None:
            if bucket is not None:
                summary['tail_buckets_discarded'] += 1
                seg['tail_buckets_discarded'] += 1
            summary['segments'].append(seg)
        seg=bucket=prev=last_bucket_ns=None

    def begin(row,reason):
        nonlocal seg,pending_reason
        seg={'segment_id':len(summary['segments'])+1,'start_reason':reason,
             'first_virtual_seq':row['seq'],'last_virtual_seq':row['seq'],
             'first_agg_id':row['agg_id'],'last_agg_id':row['agg_id'],
             'trades':0,'buckets_emitted':0,'head_buckets_discarded':0,
             'tail_buckets_discarded':0}
        pending_reason=''

    try:
        with out.open('x',encoding='utf-8',newline='\n') as fout:
            created.append(out)
            f_cov = cov_out.open('x',encoding='utf-8',newline='\n') if cov_out else None
            if f_cov:
                created.append(cov_out)
            try:
                for file_idx,source in enumerate(sources):
                    if file_idx and not joined[file_idx-1]:
                        finish()
                        pending_reason = 'UNCERTIFIED_TAPE_BOUNDARY'
                    with _open_ro(source['path']) as db:
                        q=db.execute('''SELECT seq,kind,symbol,recv_wall_ns,recv_mono_ns,
                            trade_ms,agg_id,price,quantity,buyer_is_maker,connection,
                            reason,quality FROM tape ORDER BY seq''')
                        for item in q:
                            row=dict(item)
                            row['seq'] += file_idx*1_000_000_000_000
                            if row['kind']=='MARKER':
                                reason=row['reason'] or ''
                                if (file_idx < len(joined) and joined[file_idx] and
                                        reason in ('ROTATION_OUT_CONTINUOUS_WS','PROCESS_STOP')):
                                    continue
                                if (file_idx and joined[file_idx-1] and reason in (
                                        'PROCESS_START_ROTATED_SAME_WS_UNVERIFIED',
                                        'ROTATION_IN_CONTINUOUS_WS_UNVERIFIED_UNTIL_NEXT_ID')):
                                    continue
                                if echoed_quality and reason.startswith((
                                        'ID_JUMP','ID_NONINCREASING','TRADE_TIME_','GAP_DETAILS')):
                                    continue
                                if _break_marker(reason):
                                    finish()
                                    pending_reason=reason
                                continue
                            if row['kind']!='TRADE':
                                continue
                            summary['trades_seen']+=1
                            echoed_quality=False
                            if row['symbol']!=summary['symbol'] or any(row.get(k) is None for k in
                                    ('recv_wall_ns','recv_mono_ns','agg_id','price','quantity','buyer_is_maker')):
                                raise ReplayError('Invalid raw trade fields')
                            if row['quality'] not in (None,'OK'):
                                finish()
                                pending_reason='TRADE_QUALITY:'+str(row['quality'])
                                echoed_quality=True
                                if 'ID_NONINCREASING' in row['quality']:
                                    summary['trades_excluded']+=1
                                    continue
                            if seg is not None and prev is not None:
                                order_ok=(row['agg_id']==prev['agg_id']+1 and
                                          row['recv_wall_ns']>=prev['recv_wall_ns'] and
                                          row['recv_mono_ns']>=prev['recv_mono_ns'] and
                                          row['connection']==prev['connection'])
                                if not order_ok:
                                    finish()
                                    pending_reason='OBSERVED_ORDER_OR_GENERATION_DISCONTINUITY'
                                elif row['recv_wall_ns']-prev['recv_wall_ns'] > max_silence_seconds*1_000_000_000:
                                    finish()
                                    pending_reason='LONG_SILENCE_UNVERIFIED'
                                    summary['long_silence_boundaries']+=1
                            if seg is None:
                                begin(row,pending_reason)
                            if prev is not None:
                                a=prev['recv_wall_ns']//1_000_000_000
                                b=row['recv_wall_ns']//1_000_000_000
                                if b>a+1:
                                    interval={
                                        'event_type':'ID_CHAIN_CONSISTENT_NO_TRADE_INTERVAL_DIAGNOSTIC',
                                        'segment_id':seg['segment_id'],
                                        'start_ms':(a+1)*1000,'end_ms_exclusive':b*1000,
                                        'seconds':b-a-1,
                                        'prev_agg_id':prev['agg_id'],'next_agg_id':row['agg_id'],
                                        'previous_trade_price':prev['price'],
                                        'verified_only_after_wall_ns':row['recv_wall_ns'],
                                        'caution':'NOT_A_LIVE_SIGNAL_OR_EXECUTABLE_FILL',
                                    }
                                    data=json.dumps(interval,sort_keys=True,separators=(',',':')).encode()+b'\n'
                                    if f_cov:f_cov.write(data.decode())
                                    hc.update(data)
                                    summary['coverage_intervals']+=1
                                    summary['inferred_no_trade_seconds']+=b-a-1
                            seg['trades']+=1
                            seg['last_virtual_seq']=row['seq']
                            seg['last_agg_id']=row['agg_id']
                            second_ns=row['recv_wall_ns']//1_000_000_000*1_000_000_000
                            if bucket is None:
                                bucket=Bucket(row)
                            elif second_ns==bucket.second_ns:
                                bucket.add(row)
                            elif second_ns>bucket.second_ns:
                                if last_bucket_ns is None:
                                    summary['head_buckets_discarded']+=1
                                    seg['head_buckets_discarded']+=1
                                else:
                                    cvd=seg.get('_cvd',Decimal(0))+bucket.buy_qty-bucket.sell_qty
                                    seg['_cvd']=cvd
                                    event=bucket.event(summary['symbol'],seg['segment_id'],row['recv_wall_ns'],cvd)
                                    data=json.dumps(event,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()+b'\n'
                                    fout.write(data.decode())
                                    h.update(data)
                                    summary['buckets_emitted']+=1
                                    seg['buckets_emitted']+=1
                                last_bucket_ns=bucket.second_ns
                                bucket=Bucket(row)
                            else:
                                finish()
                                begin(row,'RECEIVE_SECOND_REGRESSION')
                                bucket=Bucket(row)
                            prev=row
                finish()
            finally:
                if f_cov:f_cov.close()
        for s in summary['segments']:
            s.pop('_cvd',None)
        summary['replay_jsonl_sha256']=h.hexdigest()
        summary['coverage_jsonl_sha256']=hc.hexdigest()
        if summary_out:
            with summary_out.open('x',encoding='utf-8') as f:
                created.append(summary_out)
                json.dump(summary,f,indent=2,sort_keys=True)
                f.write('\n')
        return summary
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def main(argv=None):
    p=argparse.ArgumentParser(description='V3 offline continuous gap-aware replay')
    p.add_argument('--db',action='append',required=True,help='ordered closed raw tape; repeat')
    p.add_argument('--out',required=True)
    p.add_argument('--summary',required=True)
    p.add_argument('--coverage',required=True)
    p.add_argument('--max-silence-seconds',type=int,default=15)
    a=p.parse_args(argv)
    s=replay_continuous(a.db,a.out,summary_path=a.summary,
                        coverage_path=a.coverage,max_silence_seconds=a.max_silence_seconds)
    print(json.dumps({k:s[k] for k in (
        'buckets_emitted','trades_seen','boundary_join_count',
        'boundary_unverified_count','inferred_no_trade_seconds',
        'replay_jsonl_sha256')},sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
