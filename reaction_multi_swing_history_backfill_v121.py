#!/usr/bin/env python3
"""V1.2.1 isolated offline backfill for Multi-Swing 1m forward outcomes.

Fetch *closed* public Binance USD-M Futures 1m OHLC for the exact original
V1.1.1 touch cohort.  Never edits a scanner, Redis, journal, path SQLite, or
existing outputs.  PRE features are copied unchanged.  No trading or orders.

Usage:
  python3 reaction_multi_swing_history_backfill_v121.py \
      --features reports/reaction_multiswing_quality_v11/pilot_20261106_identityfix/features.csv \
      --project /home/multiservices-1m --plan
  python3 reaction_multi_swing_history_backfill_v121.py \
      --features reports/reaction_multiswing_quality_v11/pilot_20261106_identityfix/features.csv \
      --project /home/multiservices-1m \
      --out reports/reaction_multiswing_quality_v121/pilot_history_a --run
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from reaction_multi_swing_outcome_join_v12 import (
    BAR, MINUTE, OUTCOME_COLS, PATH_RELATIVE, decode_path,
    load_features, new_outcome, open_ro, sha256_file,
)

SCHEMA = 'REACTION_MULTI_SWING_HISTORY_V121'
PROVIDER = 'BINANCE_USDM_PUBLIC_REST_1M'
ENDPOINT = 'https://fapi.binance.com/fapi/v1/klines'
HORIZON = 60
EXTRA_COLS = (
    'v121_source', 'v121_bars_available', 'v121_first_missing_ts',
    'v121_full_30m', 'v121_full_60m', 'v121_data_quality',
)


def utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isoformat()


def check_features(rows):
    for f in rows:
        if f.get('v11_state') != 'V11_OK':
            # Existing V12 handles not-covered, but do not assume a PRE signal exists.
            continue
        retest = int(f['retest_ts'])
        known = int(f['known_ts'])
        if known != retest + MINUTE or retest % MINUTE or known % MINUTE:
            raise ValueError(f"Invalid known_ts/retest_ts for {f['touch_key']}")
        if f['side'] not in ('LONG', 'SHORT'):
            raise ValueError(f"Unknown side for {f['touch_key']}")


def grouped_intervals(rows):
    """Merge only the forward 60 closed bars required per touch, by symbol."""
    spans = defaultdict(list)
    for f in rows:
        if f.get('v11_state') != 'V11_OK':
            continue
        start = int(f['known_ts'])
        spans[f['symbol']].append((start, start + (HORIZON-1)*MINUTE))
    out = {}
    for sym, intervals in spans.items():
        intervals.sort()
        merged = []
        for a,b in intervals:
            if merged and a <= merged[-1][1] + MINUTE:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a,b))
        out[sym] = merged
    return out


def plan_summary(rows):
    spans=grouped_intervals(rows)
    return {'events':len(rows),'symbols':sorted(spans),
            'request_intervals':{s:[{'start_utc':utc(a),'end_utc':utc(b),'minute_bars':(b-a)//MINUTE+1}
                                       for a,b in items] for s,items in sorted(spans.items())},
            'estimated_requests_upper_bound':sum(sum((b-a)//(1000*MINUTE)+1 for a,b in items)
                                                   for items in spans.values())}


def validate_kline(symbol, kline, lo, hi, max_closed_ts):
    if not isinstance(kline, list) or len(kline) < 5:
        raise ValueError(f'{symbol}: invalid Binance OHLC record')
    ts = int(kline[0])
    if ts % MINUTE or not lo <= ts <= hi or ts > max_closed_ts:
        raise ValueError(f'{symbol}: out-of-range or unclosed 1m bar: {ts}')
    op, high, low, close = (float(v) for v in kline[1:5])
    if (not all(math.isfinite(v) and v > 0 for v in (op, high, low, close))
            or high < max(op, low, close) or low > min(op, high, close)):
        raise ValueError(f'{symbol}: malformed OHLC at {ts}')
    return (ts, op, high, low, close)


def http_fetch(symbol, start_ms, end_ms, timeout=30, endpoint=ENDPOINT):
    query=urllib.parse.urlencode({'symbol':symbol,'interval':'1m','startTime':start_ms,
                                  'endTime':end_ms,'limit':1000})
    url=f'{endpoint}?{query}'
    req=urllib.request.Request(url, headers={'User-Agent':'ReactionMultiSwingOfflineAudit/1.2.1'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            payload=json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Binance HTTP {exc.code} for {symbol} {utc(start_ms)}; stop, no retry storm') from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f'Binance connection failed for {symbol} {utc(start_ms)}: {exc}') from exc
    if not isinstance(payload,list):
        raise RuntimeError(f'Binance returned non-array response for {symbol} {utc(start_ms)}')
    return payload


def fetch_all(spans, *, fetcher=http_fetch, pause=0.18, timeout=30, max_closed_ts=None):
    """Fetch exact source intervals; no missing bars are synthesized or interpolated."""
    if max_closed_ts is None:
        max_closed_ts=(int(time.time()*1000)//MINUTE-1)*MINUTE
    data = {}
    queries = []
    for symbol, intervals in sorted(spans.items()):
        rows = {}
        for original_a, original_b in intervals:
            if original_a > max_closed_ts:
                continue
            end=min(original_b, max_closed_ts)
            a=original_a
            while a <= end:
                b=min(a+999*MINUTE,end)
                payload=fetcher(symbol,a,b,timeout=timeout)
                queries.append({'symbol':symbol,'start_ms':a,'end_ms':b,'records':len(payload)})
                if len(queries) == 1 or len(queries) % 10 == 0:
                    print(f'HIST_1M_PROGRESS requests={len(queries)} symbol={symbol} '
                          f'cached_bars={len(rows)}', file=sys.stderr, flush=True)
                for k in payload:
                    bar=validate_kline(symbol,k,a,b,max_closed_ts)
                    if bar[0] in rows and rows[bar[0]] != bar:
                        raise ValueError(f'{symbol}: conflicting historical 1m candle {bar[0]}')
                    rows[bar[0]]=bar
                a=b+MINUTE
                if pause>0:
                    time.sleep(pause)
        data[symbol]=rows
    return data, queries, max_closed_ts


def contiguous_path(f, data):
    """Consecutive closed 1m starting exactly at event known_ts (next OPEN)."""
    rows=data.get(f['symbol'],{})
    start=int(f['known_ts'])
    out=[]
    for i in range(HORIZON):
        ts=start+i*MINUTE
        if ts not in rows:
            return out, ts
        out.append(rows[ts])
    return out, None


def build_output(features, data):
    results=[]; states=Counter()
    for f in features:
        out=dict(f)
        if set(out).intersection(OUTCOME_COLS) or set(out).intersection(EXTRA_COLS):
            raise ValueError('Input already has outcome columns')
        bars, missing=contiguous_path(f,data)
        if bars:
            blob=zlib.compress(b''.join(BAR.pack(*x) for x in bars))
            fake=(f['symbol'],f['side'],f['original_status'],int(f['retest_ts']),
                  int(f['known_ts']),'OFFLINE_HISTORICAL',len(bars),blob)
            outcome=new_outcome(f,fake)
        else:
            outcome=new_outcome(f,None)
            outcome['v12_failure_reason']='HISTORICAL_1M_START_MISSING'
        out.update(outcome)
        out.update({'v121_source':PROVIDER,'v121_bars_available':len(bars),
                    'v121_first_missing_ts':'' if missing is None else missing,
                    'v121_full_30m':int(len(bars)>=30),
                    'v121_full_60m':int(len(bars)>=60),
                    'v121_data_quality':('FULL_60M' if len(bars)>=60 else
                                         'FULL_30M_ONLY' if len(bars)>=30 else
                                         'PARTIAL' if bars else 'NO_START_BAR')})
        results.append(out)
        states['quality:'+out['v12_quality_state']]+=1
        states['defense:'+out['v12_defense_30m']]+=1
        states['break:'+out['v12_opposite_break_60m']]+=1
        states['data:'+out['v121_data_quality']]+=1
    return results,dict(sorted(states.items()))


def parity_with_existing(features, data, db_file):
    """Read-only cross-check against V2 collector: timestamp and OHLC parity on overlap."""
    summary=Counter()
    examples=[]
    if not db_file.is_file():
        return {'db_found':False,'events_found':0}
    with closing(open_ro(db_file)) as con:
        for f in features:
            key=f['touch_key']
            path=con.execute('SELECT entry_ts,count,path_blob FROM paths WHERE touch_key=?',(key,)).fetchone()
            if path is None: continue
            summary['events_found']+=1
            entry,count,blob=path
            try:
                old=decode_path(blob,int(count),int(entry))
            except (ValueError,TypeError,zlib.error) as exc:
                summary['source_decode_invalid']+=1
                if len(examples)<10: examples.append({'touch_key':key,'reason':str(exc)})
                continue
            rows=data.get(f['symbol'],{})
            # Only the first 60 bars were requested. Paths in the collector
            # may extend up to 360 minutes; later bars are outside this audit.
            for ts,op,hi,lo,cl in old[:HORIZON]:
                if ts not in rows:
                    summary['old_bars_absent_from_download']+=1
                    if len(examples)<10: examples.append({'touch_key':key,'timestamp':ts,'reason':'ABSENT_FROM_DOWNLOAD'})
                    continue
                a=rows[ts]
                if not all(math.isclose(x,y,rel_tol=1e-12,abs_tol=1e-9)
                           for x,y in zip((op,hi,lo,cl),a[1:])):
                    summary['ohlc_mismatches']+=1
                    if len(examples)<10: examples.append({'touch_key':key,'timestamp':ts,'reason':'OHLC_DIFFERS'})
                else:
                    summary['matching_bars']+=1
    return {'db_found':True,**dict(summary),'examples':examples}


def write_results(out_dir, outputs, data, queries, *, features_path, feature_manifest,
                  code_path, parity, cutoff_ts):
    out_dir.mkdir(parents=True,exist_ok=False)
    feat_path=out_dir/'features_outcomes.csv'
    fields=list(outputs[0]) if outputs else ['touch_key']
    with feat_path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(outputs)
    candle_file=out_dir/'historical_1m.csv.gz'
    with gzip.open(candle_file,'wt',newline='',encoding='utf-8') as f:
        w=csv.writer(f)
        w.writerow(['symbol','timestamp','open','high','low','close'])
        for symbol,rows in sorted(data.items()):
            for bar in sorted(rows.values()):
                w.writerow([symbol,*bar])
    counts=Counter()
    for r in outputs:
        counts['quality:'+r['v12_quality_state']]+=1
        counts['defense:'+r['v12_defense_30m']]+=1
        counts['break:'+r['v12_opposite_break_60m']]+=1
        counts['data:'+r['v121_data_quality']]+=1
    manifest={'schema':SCHEMA,'provider':PROVIDER,'source_endpoint':ENDPOINT,
              'source_type':'historical public closed 1m OHLC, no orders',
              'feature_sha256':sha256_file(features_path),
              'feature_manifest_sha256':sha256_file(features_path.with_name('manifest.json')),
              'feature_structural_cutoff':feature_manifest['structural_cutoff'],
              'code_sha256':sha256_file(code_path),
              'v12_code_sha256':sha256_file(Path(__import__('reaction_multi_swing_outcome_join_v12').__file__)),
              'events':len(outputs),'symbols':sorted(data),
              'last_closed_minute_start_utc':utc(cutoff_ts),
              'requested_chunks':queries,
              'candle_counts':{s:len(bars) for s,bars in sorted(data.items())},
              'counts':dict(sorted(counts.items())),
              'source_parity':parity,
              'outputs':{'features_outcomes.csv':sha256_file(feat_path),
                         'historical_1m.csv.gz':sha256_file(candle_file)},
              'warning':('Exploratory offline. Original REACTION labels and all PRE fields unchanged. '
                         'CENSORED/NOT_COVERED not failures. Same-bar target vs invalidation AMBIGUOUS. '
                         'No fees, slippage, trades or predictive validation.')}
    manifest_file=out_dir/'manifest.json'
    manifest_file.write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    return {'csv':str(feat_path),'historical_1m':str(candle_file),
            'manifest':str(manifest_file),'events':len(outputs),'counts':manifest['counts'],
            'source_parity':parity}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--features',type=Path,required=True)
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--out',type=Path)
    mode=ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true',help='No network or writes')
    mode.add_argument('--run',action='store_true',help='Explicit historical REST download; new output directory')
    ap.add_argument('--pause',type=float,default=0.18)
    ap.add_argument('--timeout',type=int,default=30)
    args=ap.parse_args(argv)
    features,feature_manifest=load_features(args.features)
    check_features(features)
    summary=plan_summary(features)
    if args.plan:
        print(json.dumps({'mode':'PLAN_NO_NETWORK',**summary},indent=2,ensure_ascii=False))
        return 0
    if args.out is None:
        ap.error('--run requires --out NEW_DIR')
    if args.out.exists():
        ap.error(f'Output already exists; refusing to overwrite: {args.out}')
    if args.pause < 0 or args.timeout < 1:
        ap.error('invalid pause/timeout')
    data,queries,cutoff=fetch_all(grouped_intervals(features),pause=args.pause,timeout=args.timeout)
    results,_=build_output(features,data)
    db=args.project/PATH_RELATIVE
    parity=parity_with_existing(features,data,db)
    print(json.dumps(write_results(args.out,results,data,queries,features_path=args.features,
                                   feature_manifest=feature_manifest,code_path=Path(__file__),
                                   parity=parity,cutoff_ts=cutoff),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
