#!/usr/bin/env python3
"""Causal historical compression-box -> breakout -> reaction retest research.

Uses the EXISTING compression scanner implementation, no reimplementation of
compression_detector. Frozen Full Context DB is opened read-only. Optional
independent history cache extends source coverage, never overwrites the frozen DB.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import os
import time

import numpy as np
import pandas as pd

ROOT_REL = Path('reports/reaction_lab')
OUT_NAME = 'reaction_compression_lineage_v1'
CTX_NAME = 'reaction_full_context_v1.sqlite3'
HISTORY_NAME = 'reaction_compression_history_v1.sqlite3'
VERSION = 'compression-lineage-v1.0'
M = 60_000
TF = {'15m':15*M, '30m':30*M}
WINDOWS = {'15m':(20,30), '30m':(40,60,80)}
OUTPUT_COLS = ('event_key','symbol','side','retest_ts','date_utc','observation_type',
               'timeframe','window','base_window','coverage','history_source',
               'snapshots_eligible','snapshots_qualified','lineage_state','box_start_ts',
               'box_last_close_ts','box_high','box_low','wall','breakout_ts',
               'breakout_close','breakout_pct','age_break_to_retest_min',
               'pre_wall_distance_pct','known_wall_touch','known_wall_distance_pct',
               'readiness_score','range_ratio','atr_ratio','volume_ratio',
               'compression_height_pct','touches_high','touches_low',
               'compression_shape','micro_range_ratio','reason')


def ro(path):
    path=Path(path)
    if not path.is_file(): raise FileNotFoundError(path)
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    con.execute('PRAGMA query_only=ON')
    return con


def hash_file(path):
    digest=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def integrity(root):
    audit_file=root/ROOT_REL/'reaction_full_context_v1'/'audit.json'
    summary_file=root/ROOT_REL/'reaction_first_touch_lab_v1'/'summary.json'
    a=json.loads(audit_file.read_text(encoding='utf-8'))
    b=json.loads(summary_file.read_text(encoding='utf-8'))
    if not a.get('success') or a.get('events')!=7631 or a.get('frozen_cohort_sha256')!=b.get('frozen_cohort_sha256'):
        raise RuntimeError('La cohorte congelada o su auditoría es inconsistente')
    return a


def _merge_bar_data(ctx,history,symbol,tf):
    """Archive-first, optional verified native cache; conflicting native bars fail closed."""
    rows={}
    for row in ctx.execute('SELECT open_ts,open,high,low,close,volume,quote_volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY open_ts',(symbol,tf)):
        rows[int(row[0])]=tuple(row[:7])
    origins={t:'ARCHIVED_NATIVE' for t in rows}
    if history is not None:
        for row in history.execute('SELECT open_ts,open,high,low,close,volume,quote_volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY open_ts',(symbol,tf)):
            k=int(row[0]);v=tuple(row[:7])
            if k in rows:
                a=rows[k]
                if any(not math.isclose(float(x),float(y),rel_tol=1e-9,abs_tol=1e-10) for x,y in zip(a[1:],v[1:])):
                    raise RuntimeError(f'Conflict 15m/30m archival vs history {symbol} {tf} {k}')
            else:rows[k]=v;origins[k]='BACKFILL_NATIVE'
    ordered=sorted(rows.values())
    return pd.DataFrame(ordered,columns=['timestamp','open','high','low','close','volume','quoteVolume']),origins


def derive_30m(df15):
    """Strict contiguous 15m OHLCV aggregation. Explicit proxy, NOT native 30m."""
    out=[]
    idx={int(row.timestamp):row for row in df15.itertuples(index=False)}
    for t in sorted(idx):
        if t % TF['30m']: continue
        a,b=idx[t],idx.get(t+TF['15m'])
        if b is None:continue
        out.append((t,a.open,max(a.high,b.high),min(a.low,b.low),b.close,
                    a.volume+b.volume,a.quoteVolume+b.quoteVolume))
    return pd.DataFrame(out,columns=df15.columns)


def validate_slice(df, end_open, n, step):
    """Return requested N closed, exact, gap-free bars ending at end_open."""
    if df.empty:return None
    # index is open timestamp, may contain large historical gaps
    need=[end_open-(n-i-1)*step for i in range(n)]
    try:
        cut=df.loc[need]
    except KeyError:
        return None
    if len(cut)!=n or not np.isfinite(cut[['open','high','low','close','volume']].to_numpy(dtype=float)).all():
        return None
    return cut.reset_index()


def candidate_snapshots(retest_ts,tf_ms,hours=48):
    """Fixed search grid; no outcome-dependent placement or tuning.

    Scan every bar in recent 6h; every fourth closed bar for older history.
    End of candidate box <= retest minus two timeframe bars, leaving at least
    one complete post-box bar for the potential breakout.
    """
    last=(retest_ts//tf_ms)*tf_ms-2*tf_ms
    floor=retest_ts-hours*60*M
    t=last;result=[]
    while t>=floor:
        result.append(t)
        t -= tf_ms if (retest_ts-t)<=6*60*M else 4*tf_ms
    return result


def _reference_score(row):
    return float(row.get('readiness_score',0))


def scan_snapshot(cut,symbol,tf,n,frame_ms):
    """Call original production detector via scanner; no imitation of thresholds."""
    from dashboard.services.compression_market_scanner_service import scan_compression_market, filter_compression_market
    close=int(cut.timestamp.iloc[-1])+frame_ms-1
    raw,diagnostics=scan_compression_market({symbol:cut},lookback=n,
        base_lookback=n*2,now_ms=close,timeframe_ms=frame_ms,max_age_candles=0)
    if len(raw)!=1:
        # The scanner may suppress a source validation/detector error. Never
        # turn that into an ordinary 'no compression' negative.
        if any(diagnostics.get(name,0) for name in
               ('insufficient','invalid','gapped','stale','evaluation_errors')) or len(raw)!=0:
            raise RuntimeError(f'Compression scanner could not evaluate {symbol} {tf} {n}: {diagnostics}')
        raise RuntimeError(f'Compression scanner yielded no result with valid closed bars: {diagnostics}')
    raw['timeframe']=tf
    qualified=filter_compression_market(raw,only_near_wall=False,
       min_quote_volume=25_000.0, max_range_ratio=.80,max_atr_ratio=.90,
       max_volume_ratio=1.10,max_body_pct=.60,min_conditions=3,
       max_height_pct=4.0,min_touches=2)
    return qualified.iloc[0].to_dict() if len(qualified)==1 else None


def _break_history(df,snapshot_end_open,tf_ms,retest_ts,hi,lo,side,margin_pct=.10):
    """First verified close outside fixed box, before retest. Opposite first blocks."""
    last_open=(retest_ts//tf_ms)*tf_ms-tf_ms
    part=df.loc[(df.index>snapshot_end_open)&(df.index<=last_open)]
    if part.empty:return 'NO_BREAK',None,None
    expected=np.arange(snapshot_end_open+tf_ms,last_open+1,tf_ms,dtype='int64')
    if len(part)!=len(expected) or not np.array_equal(part.index.to_numpy(dtype='int64'),expected):
        return 'GAP_AFTER_BOX',None,None
    for row in part.itertuples():
        close=float(row.close)
        isup=close>hi*(1+margin_pct/100)
        isdown=close<lo*(1-margin_pct/100)
        if isup or isdown:
            desired_up=(side=='LONG')
            if isup==desired_up:return 'DIRECTION_BREAK',int(row.Index),close
            return 'OPPOSITE_FIRST',int(row.Index),close
    return 'NO_BREAK',None,None


def classify_box(e,bar,df,pre_close,retest,tf_ms,wall_tolerance_pct=.25):
    sym,side,r=e['symbol'],e['side'],e['retest_ts']
    hi=float(bar['high']);lo=float(bar['low'])
    wall=hi if side=='LONG' else lo
    endopen=int(bar['end_ts'])-tf_ms+1
    stage,bt,bc=_break_history(df,endopen,tf_ms,r,hi,lo,side)
    known=False;known_dist=None
    if retest:
        # LONG touches the old ceiling with its low, SHORT the old floor with high.
        touch=float(retest[2] if side=='LONG' else retest[1])
        known_dist=100*(touch/wall-1)
        known=abs(known_dist)<=wall_tolerance_pct
    pre_dist=100*((pre_close/wall)-1) if pre_close is not None else None
    if stage=='DIRECTION_BREAK':
        if known:rel='BROKEN_RETEST_KNOWN'
        elif pre_dist is not None and abs(pre_dist)<=wall_tolerance_pct:
            rel='BROKEN_NEAR_WALL_PRE'
        else:rel='BROKEN_NO_RETEST'
    elif stage=='NO_BREAK':
        if pre_close is not None and lo<=pre_close<=hi:rel='INSIDE_BOX'
        else:rel='NO_BREAK_BEFORE_RETEST'
    else:rel=stage
    return dict(lineage_state=rel,wall=wall,breakout_ts=bt,breakout_close=bc,
        breakout_pct=(100*(bc/wall-1)*(1 if side=='LONG' else -1) if bc is not None else None),
        age_break_to_retest_min=((r-bt-tf_ms)/M if bt is not None else None),
        pre_wall_distance_pct=pre_dist,known_wall_touch=known,
        known_wall_distance_pct=known_dist)


def decide_best(options):
    """Select box using ONLY information known before the 1m retest closes.

    The KNOWN candle can label the selected box, never decide which box wins.
    """
    if not options:return None
    def key(x):
        state=x['lineage_state']
        directional=state.startswith('BROKEN_')
        group=(3 if directional else 2 if state=='INSIDE_BOX' else
               1 if state in ('OPPOSITE_FIRST','NO_BREAK_BEFORE_RETEST') else 0)
        # All candidates are frozen; pre_wall_distance uses the prior CLOSED 1m.
        distance=abs(x['pre_wall_distance_pct']) if x['pre_wall_distance_pct'] is not None else 1e9
        return (group,-distance,float(x.get('readiness_score') or 0),
                int(x.get('box_last_close_ts') or 0))
    return max(options,key=key)


def _full_row(event,tf,n,eligible,qualified,best,source,coverage):
    result={col:None for col in OUTPUT_COLS}
    result.update(event_key=event['event_key'],symbol=event['symbol'],side=event['side'],
       retest_ts=event['retest_ts'],date_utc=event['date_utc'],
       observation_type=event['observation_type'],timeframe=tf,window=n,
       base_window=n*2,coverage=coverage,history_source=source,
       snapshots_eligible=eligible,snapshots_qualified=qualified)
    result['lineage_state']='NO_QUALIFIED_BOX' if eligible and not best else 'INSUFFICIENT_HISTORY'
    if best:
        result.update(best)
        result['reason']='HISTORICAL_CLOSED_BOX_BEFORE_BREAKOUT'
    elif not eligible:result['reason']='NOT_ENOUGH_CONTIGUOUS_PRE_BARS'
    else:result['reason']='NO_QUALIFIED_BOX_ON_FIXED_GRID'
    return result


def event_window(event,df,n,tf,source,pre_close,retest,cache,search_hours):
    frame_ms=TF[tf]
    times=candidate_snapshots(event['retest_ts'],frame_ms,search_hours)
    found=[];eligible=0;qualified=0
    for end_open in times:
        key=(tf,n,end_open)
        if key not in cache:
            cut=validate_slice(df,end_open,3*n,frame_ms)
            cache[key]=('INSUFFICIENT',None) if cut is None else ('AVAILABLE',scan_snapshot(cut,event['symbol'],tf,n,frame_ms))
        status,comp=cache[key]
        if status=='INSUFFICIENT':continue
        eligible+=1
        if comp is None:continue
        qualified+=1
        c=classify_box(event,comp,df,pre_close,retest,frame_ms)
        c.update(box_start_ts=comp.get('start_ts'),box_last_close_ts=comp.get('end_ts'),
          box_high=comp.get('high'),box_low=comp.get('low'),
          readiness_score=comp.get('readiness_score'),range_ratio=comp.get('range_ratio'),
          atr_ratio=comp.get('atr_ratio'),volume_ratio=comp.get('volume_ratio'),
          compression_height_pct=comp.get('compression_height_pct'),
          touches_high=comp.get('touches_high'),touches_low=comp.get('touches_low'),
          compression_shape=comp.get('compression_shape'),micro_range_ratio=comp.get('micro_range_ratio'))
        found.append(c)
    # No-box claim only when ALL scheduled snapshots were evaluable.
    coverage=('COMPLETE_SEARCH' if eligible==len(times) else
              'PARTIAL_SEARCH' if eligible else 'INSUFFICIENT_HISTORY')
    return _full_row(event,tf,n,eligible,qualified,decide_best(found),source,coverage)


def load_events(con,max_events=0):
    rows=con.execute('SELECT event_key,symbol,side,retest_ts,observation_type FROM events ORDER BY retest_ts,event_key').fetchall()
    if max_events: rows=rows[:max_events]
    return [dict(event_key=k,symbol=s,side=side,retest_ts=int(r),observation_type=o,
        date_utc=datetime.fromtimestamp(r/1000,timezone.utc).date().isoformat()) for k,s,side,r,o in rows]


def one_min_pre_and_retest(con,symbol,r):
    prev=con.execute('SELECT close FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?',
       (symbol,'1m',r-M)).fetchone()
    bar=con.execute('SELECT open_ts,open,high,low,close FROM candles WHERE symbol=? AND timeframe=? AND open_ts=?',
       (symbol,'1m',r)).fetchone()
    return (float(prev[0]) if prev else None),bar


def build(root,max_events=0,search_hours=48,progress=100):
    audit=integrity(root)
    ctxpath=root/ROOT_REL/CTX_NAME
    historypath=root/ROOT_REL/HISTORY_NAME
    hist=ro(historypath) if historypath.is_file() else None
    results=[];counts=Counter();events_done=0
    with closing(ro(ctxpath)) as ctx:
        events=load_events(ctx,max_events)
        groups=defaultdict(list)
        for e in events:groups[e['symbol']].append(e)
        try:
            for symbol,items in groups.items():
                df15,origins=_merge_bar_data(ctx,hist,symbol,'15m')
                df30native,native_origins=_merge_bar_data(ctx,hist,symbol,'30m')
                if not df30native.empty:
                    # For partial native history, fill only genuinely absent bars
                    # from contiguous native 15m pairs; preserve provenance.
                    derived=derive_30m(df15)
                    missing=derived[~derived.timestamp.isin(df30native.timestamp)]
                    df30=pd.concat([df30native,missing],ignore_index=True).sort_values('timestamp').reset_index(drop=True)
                    thirty_source='MIXED_NATIVE_AND_15M_PROXY' if not missing.empty else 'NATIVE_30M'
                else:
                    df30=derive_30m(df15)
                    thirty_source='AGGREGATED_15M_PROXY'
                df15=df15.set_index('timestamp',drop=True)
                df30=df30.set_index('timestamp',drop=True)
                cache={}
                for event in items:
                    pre,retest=one_min_pre_and_retest(ctx,symbol,event['retest_ts'])
                    for tf,ns in WINDOWS.items():
                        df=df15 if tf=='15m' else df30
                        src='NATIVE_15M' if tf=='15m' else thirty_source
                        for n in ns:
                            row=event_window(event,df,n,tf,src,pre,retest,cache,search_hours)
                            results.append(row);counts[row['coverage']]+=1
                            counts[row['lineage_state']]+=1
                    events_done+=1
                    if progress and events_done%progress==0:
                        print('LINEAGE_PROGRESS',json.dumps({'events_done':events_done,'total':len(events),'rows':len(results)}),flush=True)
        finally:
            if hist is not None:hist.close()
    if len(results)!=len(events)*sum(len(x) for x in WINDOWS.values()):
        raise RuntimeError('Incompleto: cada evento debe tener todos los TF/ventanas')
    rootout=root/ROOT_REL/OUT_NAME;rootout.mkdir(parents=True,exist_ok=True)
    full_csv=rootout/'lineage_structures.csv'
    fd,tmp=tempfile.mkstemp(prefix='.lineage_',suffix='.csv',dir=rootout)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as file:
            writer=csv.DictWriter(file,fieldnames=OUTPUT_COLS)
            writer.writeheader();writer.writerows(results);file.flush();os.fsync(file.fileno())
        os.replace(tmp,full_csv)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    # One causal multitimeframe classification per event. Unknown coverage stays unknown.
    aligned=[]
    by_event=defaultdict(list)
    for row in results:by_event[row['event_key']].append(row)
    for event in events:
        group=by_event[event['event_key']]
        a=[x for x in group if x['timeframe']=='15m']
        b=[x for x in group if x['timeframe']=='30m']
        positive=lambda rows:[x for x in rows if x['lineage_state']=='BROKEN_RETEST_KNOWN']
        aa,bb=positive(a),positive(b)
        complete15=all(x['coverage']=='COMPLETE_SEARCH' for x in a)
        complete30=all(x['coverage']=='COMPLETE_SEARCH' for x in b)
        if aa and bb:cl='DUAL_TF_RETEST'
        elif aa:cl='ONLY_15M_RETEST' if complete30 else '15M_RETEST_30M_UNKNOWN'
        elif bb:cl='ONLY_30M_RETEST' if complete15 else '30M_RETEST_15M_UNKNOWN'
        elif not complete15 or not complete30:cl='NO_MATCH_INCOMPLETE_COVERAGE'
        else:cl='NO_MATCH_WITH_COVERAGE'
        aligned.append({'event_key':event['event_key'],'symbol':event['symbol'],
            'side':event['side'],'date_utc':event['date_utc'],
            'observation_type':event['observation_type'],'confluence':cl,
            'matched_15m':int(bool(aa)),'matched_30m':int(bool(bb)),
            'search15m_covered':int(complete15),'search30m_covered':int(complete30),
            'best_15m_window':max(aa,key=lambda x:x['readiness_score'])['window'] if aa else '',
            'best_30m_window':max(bb,key=lambda x:x['readiness_score'])['window'] if bb else '',
            'source30m':b[0]['history_source']})
    alignment_file=rootout/'lineage_confluence.csv'
    atomic_headers=list(aligned[0]) if aligned else []
    fd,tmp=tempfile.mkstemp(prefix='.lineage_',suffix='.csv',dir=rootout)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as handle:
            writer=csv.DictWriter(handle,fieldnames=atomic_headers)
            writer.writeheader();writer.writerows(aligned)
        os.replace(tmp,alignment_file)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    meta={'version':VERSION,'events':len(events),'total_frozen_events':audit['events'],
         'complete_universe':len(events)==audit['events'],
         'frozen_cohort_sha256':audit['frozen_cohort_sha256'],
         'rows':len(results),'windows':{x:list(y) for x,y in WINDOWS.items()},
         'search_hours':search_hours,'max_events':max_events,
         'quality_counts':dict(counts),'sha256':hash_file(full_csv),
         'confluence_sha256':hash_file(alignment_file),
         'history_source_path':str(historypath) if historypath.is_file() else None,
         'source_archive_read_only':True,'network_calls':False,
         'assumptions':['Boxes frozen on CLOSED 15m/30m bars; separate 2x baseline; standard compression-scanner thresholds',
            'Search grid: every bar in recent 6h, one in four bars earlier up to search_hours',
            'Breakout first outside close is evaluated only AFTER the box was known and BEFORE the retest',
            'KNOWN retest touch uses the 1m retest candle AFTER its close; not a pre-retest predictor',
            '30m from pairs of original 15m bars is explicitly marked PROXY, not an independently verified native 30m candle',
            'PARTIAL_SEARCH and INSUFFICIENT_HISTORY may not be counted as verified no compression',
            'No future outcome data used to construct lineage; this is historical discovery only']}
    (rootout/'summary.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('LINEAGE_DONE',json.dumps({'events':len(events),'rows':len(results),'summary':dict(counts),'complete_universe':meta['complete_universe']}),flush=True)
    return meta


def audit_output(root):
    meta=json.loads((root/ROOT_REL/OUT_NAME/'summary.json').read_text(encoding='utf-8'))
    path=root/ROOT_REL/OUT_NAME/'lineage_structures.csv'
    with path.open(newline='',encoding='utf-8') as handle: rows=list(csv.DictReader(handle))
    keys=Counter((r['event_key'],r['timeframe'],r['window']) for r in rows)
    valid=(len(rows)==meta['rows']==meta['events']*sum(map(len,WINDOWS.values()))
       and all(n==1 for n in keys.values()) and hash_file(path)==meta['sha256']
       and meta['frozen_cohort_sha256']==integrity(root)['frozen_cohort_sha256'])
    conf=root/ROOT_REL/OUT_NAME/'lineage_confluence.csv'
    with conf.open(newline='',encoding='utf-8') as handle:confluence=list(csv.DictReader(handle))
    confluence_valid=(len(confluence)==meta['events'] and
        len({x['event_key'] for x in confluence})==len(confluence) and
        hash_file(conf)==meta['confluence_sha256'])
    valid=valid and confluence_valid
    # Causal chronology on discovered directional breakouts.
    chronological=all(
       int(x['box_last_close_ts']) < int(x['breakout_ts']) < int(x['retest_ts'])
       for x in rows if x['lineage_state'].startswith('BROKEN_')
       and x['breakout_ts'] not in ('','None')
    )
    return {'success':bool(valid and chronological),'events':meta['events'],
       'rows':len(rows),'total_frozen_events':meta['total_frozen_events'],
       'complete_universe':meta['complete_universe'],'sha256_matches':hash_file(path)==meta['sha256'],
       'chronology_valid':chronological,'confluence_valid':confluence_valid,
       'quality_counts':meta['quality_counts']}


def plan(root):
    a=integrity(root);cache=root/ROOT_REL/HISTORY_NAME
    with closing(ro(root/ROOT_REL/CTX_NAME)) as ctx:
        n15=ctx.execute("SELECT COUNT(*) FROM candles WHERE timeframe='15m'").fetchone()[0]
        n30=ctx.execute("SELECT COUNT(*) FROM candles WHERE timeframe='30m'").fetchone()[0]
    return {'frozen_events':a['events'],'cohort_sha256':a['frozen_cohort_sha256'],
       'native_archive_bars':{'15m':n15,'30m':n30},
       'optional_history_cache_exists':cache.is_file(),
       'windows':{tf:[{'compression':n,'separate_base':2*n,'required_contiguous_bars':3*n,
            'required_hours':3*n*TF[tf]/(60*60*M)} for n in ns] for tf,ns in WINDOWS.items()},
       'run_is_offline':True,'original_sources_read_only':True,
       'warning':'30m 40-80 boxes need 60-120h contiguous BEFORE the box; 96 closed 15m candles alone cannot prove absence'}


def main(argv=None):
    cli=argparse.ArgumentParser(description=__doc__)
    g=cli.add_mutually_exclusive_group(required=True)
    for flag in ('plan','run','audit'):g.add_argument('--'+flag,action='store_true')
    cli.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    cli.add_argument('--max-events',type=int,default=0)
    cli.add_argument('--search-hours',type=int,default=48)
    args=cli.parse_args(argv)
    root=args.root.resolve()
    if args.plan:res=plan(root)
    elif args.run:
        if args.search_hours<8 or args.search_hours>168:cli.error('search-hours between 8 and 168')
        if args.max_events<0:cli.error('max-events >=0')
        res=build(root,args.max_events,args.search_hours)
    else:res=audit_output(root)
    print(json.dumps(res,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
