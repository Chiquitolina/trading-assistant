#!/usr/bin/env python3
"""Pivot Historical Memory PRE v1.0 -- research only, all LIVE_OBSERVED REACTIONs.

For the exact 15m swing that originated the REACTION, test whether its price
had previously been defended as a horizontal floor/ceiling. All evidence must
have CLOSED *strictly before the pivot's OPEN*. No outcomes, no Binance, no LIVE.

Use frozen V1 all-live membership + Full Context causal_json and local HTF cache.
Reentrant by symbol, checksums, deterministic choice, explicit UNKNOWNs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

VERSION='pivot-historical-memory-pre-v1.0'
BASE=Path('reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe')
OUTPUT_ROOT=Path('reports/reaction_lab/pivot_historical_memory_pre_v1')
CTX=Path('reports/reaction_lab/reaction_full_context_v1.sqlite3')
HISTORY=Path('reports/reaction_lab/reaction_full_context_history_v1.sqlite3')
STEP=15*60_000
BARS=160
MIN_HISTORY_BARS=80
WARMUP_BARS=15
MIN_GAP=5
MIN_SPAN=20
MAX_RESPONSE=20
MIN_RESPONSE_ATR=.75
TOL_ATR=.35
TOL_PCT=.10
COLS=('event_key','symbol','side','retest_ts','date_utc','observation_type',
      'pivot_ts','pivot_price','pivot_source','pivot_validation','coverage','history_bars_pre_pivot',
      'memory_group_pre','prior_ceiling_visits','prior_floor_visits','total_qualified_visits',
      'tolerance_price','atr_before_pivot','first_visit_ts','last_visit_ts',
      'prior_ceiling_times','prior_floor_times','prior_responses_close_times',
      'reason')
GROUPS=('PRIOR_RESISTANCE','PRIOR_SUPPORT','PRIOR_MIXED_SR','NO_PRIOR_LEVEL',
        'UNKNOWN_HISTORY','UNKNOWN_PIVOT')


def sha(path):
    d=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): d.update(b)
    return d.hexdigest()


def write_csv(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.pivot_memory_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=COLS,extrasaction='ignore')
            w.writeheader();w.writerows(rows);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.pivot_memory_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None


def raw_causal(raw):
    try:
        j=json.loads(raw)
    except (ValueError,TypeError):return {}
    if not isinstance(j,dict):return {}
    # Only explicit unambiguous source fields; inspect nested payloads too.
    result=dict(j)
    for k in ('swing','pivot','signal','reaction','setup','causal'):
        child=j.get(k)
        if isinstance(child,dict):
            for key,val in child.items():result.setdefault(key,val)
    return result


def anchor_from_causal(causal, event):
    """Never synthesize a pivot from the nearest historical swing / outcomes."""
    time_keys=('pivot_timestamp','pivot_ts','pivot_time','swing_pivot_ts','swing_pivot_timestamp','swing_timestamp')
    price_keys=('swing_price','pivot_price','level_price')
    ts=None;source=''
    for k in time_keys:
        v=finite(causal.get(k))
        if v is not None and v.is_integer():ts=int(v);source=k;break
    if ts is None:
        # When scanner stored exact integer pivot-to-retest distance in minutes,
        # the original timestamp can be recovered without guessing a swing.
        age=finite(causal.get('pivot_to_retest_min'))
        if age is not None and age>=0 and (age*60_000).is_integer():
            ts=int(event['retest_ts']-age*60_000)
            source='pivot_to_retest_min_EXACT'
    price=None
    for k in price_keys:
        v=finite(causal.get(k))
        if v is not None and v>0:price=v;break
    if ts is None:return None,'MISSING_PIVOT_TIMESTAMP'
    if ts<=0 or ts%STEP!=0 or ts>=int(event['retest_ts']):
        return None,'INVALID_PIVOT_TIMESTAMP'
    if price is not None and (price<=0 or not math.isfinite(price)):
        return None,'INVALID_PIVOT_PRICE'
    return {'pivot_ts':ts,'pivot_price':price,'pivot_source':source},None


def _atr(bars, idx, span=14):
    if idx<span:return None
    x=bars.iloc[idx-span:idx+1]
    prev=x.close.to_numpy(dtype=float)[:-1]
    tr=np.maximum(x.high.to_numpy(dtype=float)[1:]-x.low.to_numpy(dtype=float)[1:],
                  np.maximum(abs(x.high.to_numpy(dtype=float)[1:]-prev),
                             abs(x.low.to_numpy(dtype=float)[1:]-prev)))
    v=float(np.mean(tr))
    return v if v>0 and math.isfinite(v) else None


def _classify_visits(kind, bars, pivot_open_ts, price, tol, atr):
    """Only confirmed historical pivots and their pre-pivot completed responses.

    Reuses the validated 3x3 swing implementation from structural atlas,
    but qualifies a historical pivot only if its confirmation CLOSE and
    favorable response CLOSE both occur before the new pivot OPEN.
    """
    import reaction_structural_level_atlas as atlas
    pivots=atlas.pivots_confirmed(bars)
    accepted=[]
    for p in pivots:
        j=int(p['pivot_i']); confirm_i=int(p['confirm_i'])
        confirmation_end=int(bars.timestamp.iloc[confirm_i])+STEP
        if confirmation_end>=pivot_open_ts:continue
        if abs(float(p['price'])-price)>tol+max(1e-11,price*1e-9):continue
        # A response must demonstrate a real departure from the pivot's price.
        stop=min(len(bars),j+1+MAX_RESPONSE)
        future=bars.iloc[j+1:stop]
        if future.empty:continue
        if kind=='HIGH':
            mask=future.low <= price-tol-MIN_RESPONSE_ATR*atr
        else:
            mask=future.high >= price+tol+MIN_RESPONSE_ATR*atr
        where=np.flatnonzero(mask.to_numpy())
        if len(where)==0:continue
        response_close=int(future.timestamp.iloc[int(where[0])])+STEP
        if response_close>=pivot_open_ts:continue
        accepted.append({'kind':kind,'pivot_i':j,'pivot_ts':int(p['pivot_ts']),
                         'confirm_closed_ts':confirmation_end,'response_closed_ts':response_close})
    return sorted(accepted,key=lambda a:(a['pivot_i'],a['kind']))


def qualified_histories(bars, anchor):
    """Pure testable implementation: returns group and proof fields."""
    empty={'prior_ceiling_visits':0,'prior_floor_visits':0,'total_qualified_visits':0,
           'tolerance_price':None,'atr_before_pivot':None,'history_bars_pre_pivot':0,
           'first_visit_ts':None,'last_visit_ts':None,
           'prior_ceiling_times':'','prior_floor_times':'','prior_responses_close_times':''}
    ts=int(anchor['pivot_ts'])
    if bars is None or bars.empty:return 'UNKNOWN_HISTORY',empty,'NO_15M_BARS'
    if bars.timestamp.duplicated().any():return 'UNKNOWN_HISTORY',empty,'DUPLICATE_15M_TIMESTAMPS'
    b=bars.sort_values('timestamp').reset_index(drop=True)
    target=b.index[b.timestamp.eq(ts)].tolist()
    if len(target)!=1:return 'UNKNOWN_PIVOT',empty,'PIVOT_BAR_NOT_ARCHIVED'
    idx=target[0]
    if idx<MIN_HISTORY_BARS+WARMUP_BARS:return 'UNKNOWN_HISTORY',empty,'INSUFFICIENT_PRE_PIVOT_BARS'
    # Up to 160 historical bars; allow >=80 complete when the local cache ends
    # earlier. The extra 15 are solely ATR/pivot-confirmation warmup.
    count=min(idx,BARS+WARMUP_BARS)
    context=b.iloc[idx-count:idx].reset_index(drop=True)
    if len(context)!=count or not np.all(np.diff(context.timestamp.to_numpy(dtype='int64'))==STEP) or \
       int(context.timestamp.iloc[-1])+STEP!=ts:
        return 'UNKNOWN_HISTORY',empty,'GAPPED_PRE_PIVOT_15M'
    arr=context[['open','high','low','close','volume']].to_numpy(dtype='float64')
    if not np.isfinite(arr).all() or np.any(arr[:,0:4]<=0) or np.any(arr[:,1]<arr[:,2]) or \
       np.any(arr[:,1]<np.maximum(arr[:,0],arr[:,3])) or np.any(arr[:,2]>np.minimum(arr[:,0],arr[:,3])):
        return 'UNKNOWN_HISTORY',empty,'INVALID_15M_OHLC'
    atr=_atr(context,len(context)-1)
    if atr is None:return 'UNKNOWN_HISTORY',empty,'MISSING_ATR'
    pivot=b.iloc[idx]
    if anchor.get('pivot_price') is None:
        price=float(pivot.low if anchor['side']=='LONG' else pivot.high)
        origin='DERIVED_FROM_EXACT_ANCHOR_CANDLE'
    else:
        price=float(anchor['pivot_price']);origin='EXPLICIT_CAUSAL_PRICE'
        true_price=float(pivot.low if anchor['side']=='LONG' else pivot.high)
        # Reject a source pivot price which conflicts with the actual 15m extreme.
        if abs(price-true_price)>max(true_price*0.0005, 1e-10):
            return 'UNKNOWN_PIVOT',empty,'PIVOT_PRICE_MISMATCH_WITH_15M'
    tol=max(price*TOL_PCT/100,TOL_ATR*atr)
    highs=_classify_visits('HIGH',context,ts,price,tol,atr)
    lows=_classify_visits('LOW',context,ts,price,tol,atr)
    # Both HIGH and LOW on the same candle cannot count as two independent visits.
    all_visits=sorted(highs+lows,key=lambda a:(a['pivot_i'],a['kind']))
    independent=[]
    for proof in all_visits:
        if independent and proof['pivot_i']-independent[-1]['pivot_i']<MIN_GAP:
            # Deterministic priority for overlapping evidence: earliest, HIGH before LOW.
            continue
        independent.append(proof)
    # Spread of 2+ historical visits must be at least 20 bars, preserving real duration.
    eligible=(len(independent)>=2 and
              independent[-1]['pivot_i']-independent[0]['pivot_i']>=MIN_SPAN)
    hi=[v for v in independent if v['kind']=='HIGH']
    lo=[v for v in independent if v['kind']=='LOW']
    group=('PRIOR_MIXED_SR' if hi and lo else
           'PRIOR_RESISTANCE' if len(hi)>=2 else
           'PRIOR_SUPPORT' if len(lo)>=2 else 'NO_PRIOR_LEVEL') if eligible else 'NO_PRIOR_LEVEL'
    record=dict(empty)
    record.update(history_bars_pre_pivot=len(context)-WARMUP_BARS,prior_ceiling_visits=len(hi),prior_floor_visits=len(lo),
                  total_qualified_visits=len(independent),tolerance_price=tol,atr_before_pivot=atr,
                  first_visit_ts=independent[0]['pivot_ts'] if independent else None,
                  last_visit_ts=independent[-1]['pivot_ts'] if independent else None,
                  prior_ceiling_times=';'.join(str(p['pivot_ts']) for p in hi),
                  prior_floor_times=';'.join(str(p['pivot_ts']) for p in lo),
                  prior_responses_close_times=';'.join(str(p['response_closed_ts']) for p in independent))
    return group,record,origin


def classify_event(event,causal,bars):
    base={k:event.get(k) for k in ('event_key','symbol','side','retest_ts','date_utc','observation_type')}
    anchor,reason=anchor_from_causal(raw_causal(causal),event)
    if anchor is None:
        base.update(pivot_ts='',pivot_price='',pivot_source='',pivot_validation='UNKNOWN',
                    coverage='UNKNOWN_PIVOT',memory_group_pre='UNKNOWN_PIVOT',
                    reason=reason)
        return {k:base.get(k,'') for k in COLS}
    anchor['side']=event['side']
    group,proof,why=qualified_histories(bars,anchor)
    base.update(pivot_ts=anchor['pivot_ts'],pivot_price=anchor['pivot_price'] or '',
                pivot_source=anchor['pivot_source'],pivot_validation=why,
                coverage=('COMPLETE' if group not in ('UNKNOWN_PIVOT','UNKNOWN_HISTORY') else group),
                memory_group_pre=group,reason='PRE_PIVOT_ONLY;NO_OUTCOMES' if group not in ('UNKNOWN_PIVOT','UNKNOWN_HISTORY') else why)
    base.update(proof)
    return {k:base.get(k,'') for k in COLS}


def source(root):
    run=root/BASE
    aud=json.loads((run/'audit.json').read_text(encoding='utf-8'))
    epath=run/'event_labels.csv';zpath=run/'candidate_levels.csv'
    if not aud.get('success') or aud.get('events')!=6423 or \
       aud['outputs_sha256']['event_labels.csv']!=sha(epath) or \
       aud['outputs_sha256']['candidate_levels.csv']!=sha(zpath):
        raise RuntimeError('V1 frozen cohort hash mismatch or incomplete audit')
    df=pd.read_csv(epath,dtype={'event_key':str},low_memory=False)
    if df.event_key.isna().any() or df.event_key.duplicated().any() or len(df)!=6423 or \
       not df.observation_type.eq('LIVE_OBSERVED').all():
        raise RuntimeError('V1 event cohort identity invalid')
    from reaction_structural_level_atlas import lineage
    ctx=root/lineage.ROOT_REL/lineage.CTX_NAME
    hist=root/lineage.ROOT_REL/lineage.HISTORY_NAME
    if ctx!=root/CTX:raise RuntimeError('Unexpected Full Context source location')
    return df,ctx,hist,{'v1_events_sha':sha(epath),'v1_zones_sha':sha(zpath),'cohort_events':6423}


def _select_pilot(df, n):
    """Deterministic date/side stratified events; selection never uses outcomes."""
    if not n:return df
    buckets={}
    for pair,sub in df.groupby(['date_utc','side'],sort=True):
        sub=sub.assign(_hash=sub.event_key.map(lambda k:hashlib.sha256(k.encode()).hexdigest()))
        buckets[pair]=sub.sort_values(['_hash','event_key']).event_key.tolist()
    order=[]
    while len(order)<n and any(buckets.values()):
        for pair in sorted(buckets):
            if buckets[pair] and len(order)<n:order.append(buckets[pair].pop(0))
    return df[df.event_key.isin(order)].copy()


def setup(root,max_events=0):
    cohort,ctx,hist,prov=source(root)
    cohort=_select_pilot(cohort,max_events)
    prov=dict(prov,selected_events=len(cohort),max_events=int(max_events),
        selected_key_sha=hashlib.sha256('\n'.join(sorted(cohort.event_key)).encode()).hexdigest())
    return cohort,ctx,hist,prov,root/OUTPUT_ROOT/('pilot_'+str(max_events) if max_events else 'all_live')

def pivot_stats(ctx, cohort):
    from reaction_structural_level_atlas import lineage
    out=Counter();keys=set(cohort.event_key)
    with closing(lineage.ro(ctx)) as con:
        for k,raw in con.execute('SELECT event_key,causal_json FROM events'):
            if k not in keys:continue
            ev={'retest_ts':int(k.rsplit('|',1)[1])}
            a,why=anchor_from_causal(raw_causal(raw),ev)
            out['anchor_present' if a else why]+=1
    return dict(out)


def plan(root,max_events=0):
    cohort,ctx,hist,prov,out=setup(root,max_events)
    return dict(version=VERSION,events=len(cohort),symbols=cohort.symbol.nunique(),
        max_history_window_pre_pivot_15m=BARS,min_history_bars=MIN_HISTORY_BARS,history_warmup=WARMUP_BARS,
        minimum_prior_contacts=2,min_span_bars=MIN_SPAN,
        min_response_atr=MIN_RESPONSE_ATR,provenance=prov,
        anchor_availability=pivot_stats(ctx,cohort),
        history_cache_present=hist.is_file(),free_disk_gb=round(shutil.disk_usage(root).free/1024**3,2),
        out=str(out),no_outcomes_read=True,no_network_calls=True)


def shard(out,symbol):
    tag=''.join(c for c in symbol if c.isalnum() or c in ('-','_'))
    return out/'shards'/f'{tag}.csv',out/'shards'/f'{tag}.sha.json'


def run(root,min_free_gb=2,max_events=0):
    cohort,ctx,hist,prov,out=setup(root,max_events)
    if shutil.disk_usage(root).free/1024**3<min_free_gb:
        raise RuntimeError('Insufficient free disk. No files written')
    config={'version':VERSION,'provenance':prov,'parameters':
        {'bars':BARS,'min_bars':MIN_HISTORY_BARS,'gap':MIN_GAP,'span':MIN_SPAN,'response':MIN_RESPONSE_ATR,'tol_atr':TOL_ATR,'tol_pct':TOL_PCT}}
    f=out/'run_config.json'
    if f.exists() and json.loads(f.read_text())!=config:
        raise RuntimeError('Different run configuration: refuse shard reuse')
    write_json(f,config)
    from reaction_structural_level_atlas import lineage
    import reaction_compression_breakout_retest_v4 as compression
    expected=cohort.groupby('symbol',sort=True)
    reused=0;new=0
    history= lineage.ro(hist) if hist.is_file() else None
    try:
        with closing(lineage.ro(ctx)) as con:
            for symbol,part in expected:
                outcsv,meta=shard(out,symbol)
                keys=part.event_key.tolist()
                if outcsv.exists() and meta.exists():
                    old=json.loads(meta.read_text())
                    if old.get('keys')==keys and old.get('sha256')==sha(outcsv):
                        reused+=1;continue
                raw_by_key={k:v for k,v in con.execute('SELECT event_key,causal_json FROM events WHERE symbol=?',(symbol,))}
                # Per-symbol load: reuses the native/aggregated HTF cache, never requests Binance.
                f15,_,_=compression._merge_tf(con,history,symbol)
                f15=f15.reset_index()
                rows=[]
                for e in part.to_dict('records'):
                    if e['event_key'] not in raw_by_key:raise RuntimeError(f'Missing frozen event {e["event_key"]}')
                    rows.append(classify_event(e,raw_by_key[e['event_key']],f15))
                write_csv(outcsv,rows)
                write_json(meta,{'symbol':symbol,'keys':keys,'sha256':sha(outcsv),'rows':len(rows)})
                new+=1
                if (new+reused)%10==0 or new+reused==len(expected):
                    print(f'PIVOT_MEMORY_PROGRESS {new+reused}/{len(expected)} symbols new={new} resumed={reused}',flush=True)
    finally:
        if history is not None:history.close()
    result=audit(root,max_events)
    print('PIVOT_MEMORY_DONE',json.dumps(result,ensure_ascii=False),flush=True)
    return result


def audit(root,max_events=0):
    cohort,_,_,prov,out=setup(root,max_events)
    config=json.loads((out/'run_config.json').read_text())
    if config['version']!=VERSION or config['provenance']!=prov:
        raise RuntimeError('V1 source changed')
    rows=[]
    for sym,part in cohort.groupby('symbol',sort=True):
        p,m=shard(out,sym)
        if not p.exists() or not m.exists():raise RuntimeError(f'Missing shard {sym}')
        md=json.loads(m.read_text())
        if md['sha256']!=sha(p) or md['keys']!=part.event_key.tolist():
            raise RuntimeError(f'Invalid shard hash/membership for {sym}')
        with p.open(newline='',encoding='utf-8') as f:rs=list(csv.DictReader(f))
        if len(rs)!=len(part) or [x['event_key'] for x in rs]!=md['keys']:
            raise RuntimeError(f'Missing/duplicate events in {sym}')
        for r in rs:
            if r['memory_group_pre'] not in GROUPS:raise RuntimeError('Bad memory group')
            if r['memory_group_pre'] not in ('UNKNOWN_HISTORY','UNKNOWN_PIVOT'):
                if int(r['pivot_ts'])>=int(r['retest_ts']):raise RuntimeError('Noncausal pivot')
                for col in ('prior_ceiling_times','prior_floor_times','prior_responses_close_times'):
                    for val in filter(None,r[col].split(';')):
                        if int(val)>=int(r['pivot_ts']):raise RuntimeError('Evidence on/after pivot')
                n=int(r['total_qualified_visits'])
                if n!=int(r['prior_ceiling_visits'])+int(r['prior_floor_visits']):
                    raise RuntimeError('Incorrect visit accounting')
                if r['memory_group_pre']!='NO_PRIOR_LEVEL' and n<2:
                    raise RuntimeError('Qualified with fewer than two visits')
        rows.extend(rs)
    if len(rows)!=len(cohort) or len({x['event_key'] for x in rows})!=len(cohort):
        raise RuntimeError('Cohort join/membership mismatch')
    write_csv(out/'pivot_memory_labels_pre.csv',rows)
    labels_path=out/'pivot_memory_labels_pre.csv'
    result={'success':True,'version':VERSION,'events':len(rows),'symbols':cohort.symbol.nunique(),
            'groups':dict(Counter(r['memory_group_pre'] for r in rows)),
            'by_side':{side:dict(Counter(r['memory_group_pre'] for r in rows if r['side']==side)) for side in ('LONG','SHORT')},
            'source_hashes':prov,'labels_sha':sha(labels_path),
            'no_outcomes_used':True,'folder':str(out)}
    write_json(out/'audit.json',result)
    return result


def main():
    pa=argparse.ArgumentParser(description=__doc__)
    mode=pa.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    mode.add_argument('--audit',action='store_true')
    pa.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    pa.add_argument('--min-free-gb',type=float,default=2)
    pa.add_argument('--max-events',type=int,default=0,help='0=todas; 40=piloto estratificado separado')
    args=pa.parse_args();root=args.root.resolve()
    if args.max_events<0:pa.error('--max-events must be >=0')
    if args.plan:res=plan(root,args.max_events)
    elif args.run:res=run(root,args.min_free_gb,args.max_events)
    else:res=audit(root,args.max_events)
    print(json.dumps(res,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
