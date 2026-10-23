#!/usr/bin/env python3
"""Offline, outcome-blind PRE triple-top / triple-bottom labels for Runner Discovery V2.

Only closed 15m OHLC before the REACTION retest timestamp. Runner success,
TP/SL and future 1m paths are NEVER used for feature discovery. No HTTP,
Redis, Binance, PM2 or trading actions.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

VERSION = 'runner-triple-pivot-pre-v1.0'
TF_MS = 15 * 60_000
LOOKBACK = 170
MIN_BARS = 80
LEFT = RIGHT = 3
MIN_GAP = 5        # minimum separation between tops/bottoms in 15m bars
MAX_SPAN = 130     # maximum time between first and third swing
THIRD_RECENCY = 24 # third touch must be within six hours of REACTION
MIN_TROUGH_ATR = .75
PROXIMITY_ATR = 1.0
STATES = ('TRIPLE_TOP_CONFIRMED_PRE', 'TRIPLE_TOP_FORMING_PRE',
          'TRIPLE_BOTTOM_CONFIRMED_PRE','TRIPLE_BOTTOM_FORMING_PRE',
          'NO_PATTERN_PRE', 'UNKNOWN_HISTORY', 'UNKNOWN_PRICE')
COLUMNS = ('event_key','symbol','side','retest_ts','pattern_pre','reason',
           'top_bottom_1_ts','top_bottom_2_ts','top_bottom_3_ts',
           'level_price','level_tolerance','atr14','third_confirmation_ts',
           'n_pre_bars','pre_last_close_ts')


def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()


def results_dir(root):
    # Same canonical location used by Runner Discovery V2.
    from reaction_lab_runner_discovery_v2 import project_results_dir
    return Path(project_results_dir(Path(root)))


def label_path(root):
    return results_dir(root)/'triple_pivot_pre_v1'/'triple_pivot_labels_pre.csv'


def _atomic_csv(path, frame):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.triple-',suffix='.csv')
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            frame.to_csv(f,index=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _atomic_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.triple-',suffix='.json')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _unknown(row, state, reason, n=0, last=0):
    return dict(event_key=str(row['event_key']),symbol=str(row['symbol']),
        side=str(row['side']),retest_ts=int(row['retest_ts']),pattern_pre=state,
        reason=reason,top_bottom_1_ts='',top_bottom_2_ts='',top_bottom_3_ts='',
        level_price='',level_tolerance='',atr14='',third_confirmation_ts='',
        n_pre_bars=int(n),pre_last_close_ts=int(last) if last else '')


def _extrema(frame, side):
    """Strict 3x3 pivots, with right-hand candle closure PRE event."""
    vals=frame['high'].to_numpy(float) if side=='SHORT' else frame['low'].to_numpy(float)
    out=[]
    for i in range(LEFT,len(vals)-RIGHT):
        neighbor=np.r_[vals[i-LEFT:i],vals[i+1:i+RIGHT+1]]
        if side=='SHORT' and np.all(vals[i]>neighbor):out.append(i)
        if side=='LONG' and np.all(vals[i]<neighbor):out.append(i)
    return out


def _pullback_ok(df, first, second, side, atr):
    # Trough/peak between touches must be substantial, not one top plateau.
    if second-first<MIN_GAP:return False
    between=df.iloc[first+1:second]
    if between.empty:return False
    if side=='SHORT':
        depth=min(float(df.iloc[first].high),float(df.iloc[second].high))-float(between.low.min())
    else:
        depth=float(between.high.max())-max(float(df.iloc[first].low),float(df.iloc[second].low))
    return depth>=MIN_TROUGH_ATR*atr


def _candidate(df, ids, side, atr, price, *, confirmed):
    # Search most recent third touch first, ties favor tighter band.
    if len(ids)<3:return None
    close_ts=df.timestamp.to_numpy(np.int64)+TF_MS
    piv=ids
    vals=df.high.to_numpy(float) if side=='SHORT' else df.low.to_numpy(float)
    best=None
    for third in reversed(piv):
        if len(df)-1-third>THIRD_RECENCY:continue
        for j in range(len(piv)-2,-1,-1):
            second=piv[j]
            if second>=third or third-second<MIN_GAP:continue
            for k in range(j-1,-1,-1):
                first=piv[k]
                if second-first<MIN_GAP or third-first>MAX_SPAN:continue
                center=float(np.median([vals[first],vals[second],vals[third]]))
                tol=min(max(.35*atr,.0035*center),.01*center)
                if max(abs(float(vals[v])-center) for v in (first,second,third))>tol:continue
                if abs(price-center)>max(PROXIMITY_ATR*atr,.006*center):continue
                if not _pullback_ok(df,first,second,side,atr):continue
                if not _pullback_ok(df,second,third,side,atr):continue
                # End-of-window location is part of the PRE event definition.
                cand=(third, -max(abs(float(vals[v])-center) for v in (first,second,third)),
                      first,second,third,center,tol)
                if best is None or cand[:2]>best[:2]:best=cand
    if best is None:return None
    _,_,i,j,k,center,tol=best
    return {'pivots':(i,j,k),'center':center,'tol':tol,
            'third_confirmation_ts':int(close_ts[k+RIGHT]) if confirmed else ''}


def classify_pre(row, candles):
    """Pure chronological classification. Never reads `es_runner` or outcomes."""
    side=str(row['side']); event_ts=int(row['retest_ts'])
    if side not in ('LONG','SHORT'):return _unknown(row,'UNKNOWN_PRICE','BAD_SIDE')
    if candles is None or candles.empty:
        return _unknown(row,'UNKNOWN_HISTORY','NO_15M_HISTORY')
    required={'timestamp','open','high','low','close'}
    if not required.issubset(candles.columns):
        return _unknown(row,'UNKNOWN_HISTORY','BAD_HISTORY_SCHEMA')
    df=candles.loc[pd.to_numeric(candles.timestamp,errors='coerce')+TF_MS < event_ts].tail(LOOKBACK).copy()
    df=df.sort_values('timestamp',kind='stable').reset_index(drop=True)
    n=len(df)
    last=int(df.timestamp.iloc[-1])+TF_MS if n else 0
    if n<MIN_BARS:return _unknown(row,'UNKNOWN_HISTORY','INSUFFICIENT_15M_BARS',n,last)
    ts=pd.to_numeric(df.timestamp,errors='coerce').to_numpy(float)
    ohlc=df[['open','high','low','close']].apply(pd.to_numeric,errors='coerce').to_numpy(float)
    if not np.isfinite(ts).all() or np.any(np.diff(ts)!=TF_MS):
        return _unknown(row,'UNKNOWN_HISTORY','GAPPED_15M_HISTORY',n,last)
    if not np.isfinite(ohlc).all() or (ohlc<=0).any() or (
        ohlc[:,1]<np.maximum(ohlc[:,0],ohlc[:,3])).any() or (
        ohlc[:,2]>np.minimum(ohlc[:,0],ohlc[:,3])).any():
        return _unknown(row,'UNKNOWN_HISTORY','INVALID_OHLC',n,last)
    price=pd.to_numeric(pd.Series([row.get('retest_close')]),errors='coerce').iloc[0]
    if not np.isfinite(price) or price<=0:
        return _unknown(row,'UNKNOWN_PRICE','MISSING_RETEST_CLOSE',n,last)
    # All ATR inputs are strictly PRE. True range of most recent 14 closed 15m bars.
    h=ohlc[:,1];l=ohlc[:,2];c=ohlc[:,3]
    tr=np.maximum(h[1:]-l[1:],np.maximum(abs(h[1:]-c[:-1]),abs(l[1:]-c[:-1])))
    atr=float(np.mean(tr[-14:]))
    if not np.isfinite(atr) or atr<=0:
        return _unknown(row,'UNKNOWN_HISTORY','ATR_UNAVAILABLE',n,last)
    confirmed_pivots=_extrema(df,side)
    match=_candidate(df,confirmed_pivots,side,atr,float(price),confirmed=True)
    forming=False
    if match is None and len(confirmed_pivots)>=2:
        # The third attempt may be the *last closed candle* preceding the REACTION.
        # A confirmed 3x3 pivot cannot be required here without lookahead.
        # Consider only a candle that has already CLOSED with a directional rejection.
        vals=df.high.to_numpy(float) if side=='SHORT' else df.low.to_numpy(float)
        candidate_thirds=[]
        for idx in range(max(confirmed_pivots[-1]+MIN_GAP,n-THIRD_RECENCY-1),n):
            op,hi,lo,cl=ohlc[idx]
            full=hi-lo
            if full<=0:continue
            # Short: close in lower half. Long: close in upper half.
            if side=='SHORT' and cl>lo+.5*full:continue
            if side=='LONG' and cl<hi-.5*full:continue
            candidate_thirds.append(idx)
        # Enforce third is beyond second and after two independently confirmed pivots.
        for third in reversed(candidate_thirds):
            candidates=[x for x in confirmed_pivots if x<=third-MIN_GAP]
            if len(candidates)<2:continue
            match=_candidate(df,candidates[-24:]+[third],side,atr,float(price),confirmed=False)
            if match is not None and match['pivots'][2]==third:
                forming=True;break
            match=None
    status=('TRIPLE_TOP_' if side=='SHORT' else 'TRIPLE_BOTTOM_')+('FORMING_PRE' if forming else 'CONFIRMED_PRE') if match else 'NO_PATTERN_PRE'
    out=_unknown(row,status,'THREE_DISTINCT_TOUCHES_PRE' if match else 'NO_QUALIFIED_TRIPLE_PRE',n,last)
    if match:
        i,j,k=match['pivots']
        out.update(top_bottom_1_ts=int(df.timestamp.iloc[i])+TF_MS,
                   top_bottom_2_ts=int(df.timestamp.iloc[j])+TF_MS,
                   top_bottom_3_ts=int(df.timestamp.iloc[k])+TF_MS,
                   third_confirmation_ts=match['third_confirmation_ts'],
                   level_price=round(match['center'],12),level_tolerance=round(match['tol'],12),
                   atr14=round(atr,12))
        if any(int(out[f'top_bottom_{ix}_ts'])>=event_ts for ix in (1,2,3)):
            raise ValueError('Noncausal pivot timestamp')
        if out['third_confirmation_ts'] and int(out['third_confirmation_ts'])>=event_ts:
            raise ValueError('Noncausal pivot confirmation')
    return out


def read_source(root):
    src=results_dir(root)/'runner_discovery_v2_events.csv'
    if not src.is_file():raise FileNotFoundError(f'Runner Discovery CSV no existe: {src}')
    # only identity+context; outcomes not read into classifier.
    h=pd.read_csv(src,nrows=0)
    fields=['event_key','symbol','side','retest_ts','retest_close']
    miss=set(fields)-set(h.columns)
    if miss:raise ValueError(f'Faltan columnas de Runner V2: {sorted(miss)}')
    df=pd.read_csv(src,usecols=fields,dtype={'event_key':str},low_memory=False)
    if df.event_key.isna().any() or df.event_key.duplicated().any():
        raise ValueError('Event ID repetido/nulo en Runner V2')
    if df.retest_ts.isna().any():raise ValueError('Timestamp de REACTION ausente')
    df['retest_ts']=pd.to_numeric(df['retest_ts'],errors='raise').astype('int64')
    if not df.side.isin(('SHORT','LONG')).all():raise ValueError('Side inesperado en Runner V2')
    return src,df


def generate(root, max_events=0):
    """Manual CSV generator, read-only SQLite and no output-based selection."""
    from reaction_compression_breakout_retest_v4 import _merge_tf
    import reaction_structural_level_atlas as atlas
    root=Path(root).resolve();src,events=read_source(root)
    if max_events:events=events.sort_values(['retest_ts','event_key']).head(max_events)
    db=root/atlas.lineage.ROOT_REL/atlas.lineage.CTX_NAME
    histpath=root/atlas.lineage.ROOT_REL/atlas.lineage.HISTORY_NAME
    if not db.is_file():raise FileNotFoundError(f'Falta contexto SQLite {db}')
    history=atlas.lineage.ro(histpath) if histpath.is_file() else None
    frames=[];done=0;syms=events.symbol.nunique()
    try:
        with closing(atlas.lineage.ro(db)) as con:
            for symbol,part in events.groupby('symbol',sort=True):
                try:
                    df15,_,_=_merge_tf(con,history,symbol)
                    candles=df15.reset_index()
                except Exception as exc:
                    print(f'TRIPLE_PRE_WARN {symbol}: {type(exc).__name__} {exc}',flush=True)
                    candles=pd.DataFrame()
                for row in part.to_dict('records'):
                    frames.append(classify_pre(row,candles))
                done+=1
                if done%20==0 or done==syms:
                    print(f'TRIPLE_PRE_PROGRESS {done}/{syms} symbols · {len(frames)}/{len(events)} events',flush=True)
    finally:
        if history is not None:history.close()
    labels=pd.DataFrame(frames,columns=COLUMNS)
    if len(labels)!=len(events) or labels.event_key.duplicated().any():
        raise ValueError('Unmatched event identity')
    if set(labels.event_key)!=set(events.event_key):raise ValueError('Missing label events')
    path=label_path(root) if not max_events else label_path(root).parent/f'pilot_{max_events}.csv'
    _atomic_csv(path,labels)
    manifest={'version':VERSION,'source_sha256':sha(src),'source_path':str(src),
              'labels_sha256':sha(path),'labels_path':str(path),'total':len(labels),
              'symbols':int(events.symbol.nunique()),'pilot_size':int(max_events),
              'counts':{s:int((labels.pattern_pre==s).sum()) for s in STATES},
              'parameters':dict(lookback=LOOKBACK,min_bars=MIN_BARS,swing_left=LEFT,
                 swing_right=RIGHT,min_gap=MIN_GAP,max_span=MAX_SPAN,
                 third_recency=THIRD_RECENCY,min_pullback_atr=MIN_TROUGH_ATR,
                 near_reaction_atr=PROXIMITY_ATR),
              'no_outcomes_used':True,'strict_15m_close_before_event':True}
    _atomic_json(path.with_suffix('.json'),manifest)
    print('TRIPLE_PRE_DONE',json.dumps(manifest,ensure_ascii=False),flush=True)
    return manifest


def verify_labels(root,pilot=0):
    src,events=read_source(root)
    path=label_path(root) if not pilot else label_path(root).parent/f'pilot_{pilot}.csv'
    mf=path.with_suffix('.json')
    if not mf.exists() or not path.exists():raise FileNotFoundError('Faltan etiquetas/manifest triple PRE')
    manifest=json.loads(mf.read_text(encoding='utf-8'))
    if manifest.get('version')!=VERSION or manifest.get('source_sha256')!=sha(src) or manifest.get('labels_sha256')!=sha(path):
        raise ValueError('Triple PRE source/labels hash changed; regenerate')
    labels=pd.read_csv(path,dtype={'event_key':str},low_memory=False)
    if len(labels)!=manifest['total'] or labels.event_key.duplicated().any() or not labels.pattern_pre.isin(STATES).all():
        raise ValueError('Invalid label cardinality or state')
    if not pilot:
        if len(labels)!=len(events) or set(labels.event_key)!=set(events.event_key):
            raise ValueError('Incomplete triple PRE labels')
    for d in ('LONG','SHORT'):
        wrong=(labels.side==d)&labels.pattern_pre.str.startswith('TRIPLE_'+('TOP' if d=='LONG' else 'BOTTOM'))
        if wrong.any():raise ValueError('Triple pivot side mismatch')
    if (labels.pre_last_close_ts.dropna().astype(float)>=labels.retest_ts.dropna().astype(float)).any():
        raise ValueError('Future candle included in PRE')
    print('TRIPLE_PRE_AUDIT',json.dumps({'success':True,'events':len(labels),
          'counts':manifest['counts'],'sha':sha(path)},ensure_ascii=False),flush=True)
    return labels


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',default='.')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true');g.add_argument('--run',action='store_true')
    g.add_argument('--audit',action='store_true')
    p.add_argument('--max-events',type=int,default=0)
    args=p.parse_args();root=Path(args.project_root).resolve()
    if args.max_events<0:raise SystemExit('--max-events >= 0')
    if args.plan:
        src,rows=read_source(root)
        print(json.dumps({'version':VERSION,'events':min(len(rows),args.max_events) if args.max_events else len(rows),
           'symbols':int(rows.symbol.nunique()),'source':str(src),'labels':str(label_path(root)),
           'outcomes_used':False,'external_calls':False},indent=2));return
    if args.run:generate(root,args.max_events)
    if args.audit:verify_labels(root,args.max_events)

if __name__=='__main__':main()
