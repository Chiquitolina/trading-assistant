#!/usr/bin/env python3
"""Horizontal Level Lifecycle V2.0 (phase A) — offline causal 30m atlas.

Registers every persistent support/resistance from 2 *confirmed* 2x2 pivots.
Observes subsequent interactions, crossings, and returns in either direction.
No 1m outcome, PnL, trading, exchange or Redis. Reads only local candle archives.
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
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from horizontal_breakout_lab import Archive, ROOT, TF, aggregate_15m_to_30m, normalize

VERSION = 'horizontal-level-lifecycle-v2.0-phase-a'
DEFAULT_OUTPUT = Path('reports/horizontal_level_lifecycle_v2')
FORMATION_COLS = ('level_id','symbol','origin','formation_closed_ts','formation_utc',
                  'first_pivot_open_ts','second_pivot_open_ts','center','zone_low',
                  'zone_high','atr_at_formation','width_atr','pivot_separation_bars',
                  'confirmation_count','segment','source_30m')
EVENT_COLS = ('event_id','level_id','symbol','origin','event_type','observed_closed_ts',
              'observed_utc','bar_open_ts','previous_relation','close_relation','side_at_pre',
              'close','high','low','center','zone_low','zone_high','atr_at_formation',
              'age_bars','visit_number','segment','source_30m')
END_COLS = ('level_id','symbol','origin','formation_closed_ts','end_state',
            'end_closed_ts','n_visits_after_formation','n_cross_up','n_cross_down',
            'n_returns','bars_observed')
COVERAGE_COLS=('symbol','source_30m','bars30','segments','gaps30','levels','visits',
               'cross_up','cross_down','returns','status','error')


@dataclass(frozen=True)
class Config:
    left: int = 2
    right: int = 2
    min_separation_bars: int = 5
    max_pivot_span_bars: int = 96
    min_history_bars: int = 32
    atr_bars: int = 14
    tolerance_atr: float = 0.35
    tolerance_floor_pct: float = 0.10
    min_zone_width_pct: float = 0.10
    max_age_bars: int = 160
    visit_rearm_bars: int = 3
    max_active_per_origin: int = 35


def _iso(ms):
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).isoformat()


def _hash(*parts):
    return hashlib.sha256('|'.join(str(x) for x in parts).encode()).hexdigest()[:24]


def _relation(price, lo, hi):
    return 'ABOVE' if price > hi else ('BELOW' if price < lo else 'INSIDE')


def _atr(frame, i, n=14):
    if i < n:
        return None
    highs=frame.high.iloc[i-n+1:i+1].to_numpy(dtype=float)
    lows=frame.low.iloc[i-n+1:i+1].to_numpy(dtype=float)
    prev_closes=frame.close.iloc[i-n:i].to_numpy(dtype=float)
    tr=np.maximum(highs-lows,np.maximum(np.abs(highs-prev_closes),np.abs(lows-prev_closes)))
    val=float(np.mean(tr))
    return val if math.isfinite(val) and val>0 else None


def _pivots_confirmed_now(frame, i, cfg):
    """Pivot j=i-right is known only at close of bar i, NOT earlier."""
    j=i-cfg.right
    if j<cfg.left or i>=len(frame):
        return []
    high=float(frame.high.iloc[j]);low=float(frame.low.iloc[j])
    others_hi=np.r_[frame.high.iloc[j-cfg.left:j].to_numpy(),frame.high.iloc[j+1:i+1].to_numpy()]
    others_lo=np.r_[frame.low.iloc[j-cfg.left:j].to_numpy(),frame.low.iloc[j+1:i+1].to_numpy()]
    out=[]
    if high>max(others_hi):out.append(('HIGH',high,j))
    if low<min(others_lo):out.append(('LOW',low,j))
    return out


def _segment_bounds(frame):
    """Never join history, pivots or interactions across a missing 30m candle."""
    if frame.empty:return []
    cuts=[0]
    for i in range(1,len(frame)):
        if int(frame.timestamp.iloc[i])-int(frame.timestamp.iloc[i-1])!=TF:
            cuts.append(i)
    return [(a,b) for a,b in zip(cuts,cuts[1:]+[len(frame)])]


def scan_symbol(symbol, thirty, cfg=Config(), source='UNKNOWN'):
    """All decisions at i use only bars with index <= i. No outcomes loaded."""
    frame=normalize(thirty, TF)
    formation=[];events=[];ends=[]
    bounds=_segment_bounds(frame)
    for segno,(start,stop) in enumerate(bounds,1):
        seg=frame.iloc[start:stop].reset_index(drop=True)
        active=[]; prior={'HIGH':[],'LOW':[]}
        for i in range(len(seg)):
            bar=seg.iloc[i]
            clock=int(bar.timestamp)+TF
            if i>=cfg.min_history_bars:
                # 1. Advance prior levels using the *newly closed* candle.
                #    Levels formed at this same close are not processed here.
                for level in list(active):
                    age=i-level['form_index']
                    if age>cfg.max_age_bars:
                        level['end_state']='MAX_AGE'
                        level['end_closed_ts']=int(seg.timestamp.iloc[i-1])+TF
                        active.remove(level)
                        ends.append(_end(level))
                        continue
                    pre=float(seg.close.iloc[i-1]); close=float(bar.close)
                    lo=level['zone_low'];hi=level['zone_high']
                    a=_relation(pre,lo,hi);b=_relation(close,lo,hi)
                    old=level['last_relation']
                    intersects=float(bar.low)<=hi and float(bar.high)>=lo
                    # An interaction requires a newly entered wick intersection,
                    # or a rearmed revisit after separation from the zone.
                    visit=(intersects and not level['was_intersecting']
                           and i-level['last_visit_index']>=cfg.visit_rearm_bars)
                    if visit:
                        level['visits']+=1
                        level['last_visit_index']=i
                        _event(events,level,'VISIT',bar,clock,a,b,segno,source)
                    typ=None
                    if b=='ABOVE' and a!='ABOVE':typ='CROSS_UP'
                    elif b=='BELOW' and a!='BELOW':typ='CROSS_DOWN'
                    elif b=='INSIDE' and a!='INSIDE':typ='RETURN_INSIDE'
                    if typ:
                        _event(events,level,typ,bar,clock,a,b,segno,source)
                        level[{'CROSS_UP':'n_up','CROSS_DOWN':'n_down','RETURN_INSIDE':'n_return'}[typ]]+=1
                    level['was_intersecting']=intersects
                    level['last_relation']=b
                    level['bars_observed']+=1
            if i<max(cfg.min_history_bars,cfg.atr_bars+1):
                continue
            atr=_atr(seg,i,cfg.atr_bars)
            if atr is None:
                continue
            # 2. Confirm today's pivot ONLY AFTER both right-side candles closed.
            for origin,price,j in _pivots_confirmed_now(seg,i,cfg):
                pivots=prior[origin]
                pivots[:]=[p for p in pivots if j-p['j']<=cfg.max_pivot_span_bars]
                matches=[]
                for p in pivots:
                    if j-p['j']<cfg.min_separation_bars:continue
                    tolerance=max(cfg.tolerance_atr*atr,price*cfg.tolerance_floor_pct/100)
                    if abs(p['price']-price)<=tolerance:
                        matches.append((abs(p['price']-price),p))
                if matches:
                    p=min(matches,key=lambda item:(item[0],item[1]['j']))[1]
                    center=(price+p['price'])/2
                    width=max(cfg.min_zone_width_pct*center/100,
                              cfg.tolerance_atr*atr,abs(p['price']-price)/2)
                    zone_low=center-width;zone_high=center+width
                    # Do not spawn multiple nearly identical concurrent zones.
                    duplicate=any(z['origin']==origin and abs(z['center']-center)<=
                                  min(z['zone_high']-z['center'],width) for z in active)
                    if not duplicate:
                        same_origin=sum(z['origin']==origin for z in active)
                        if same_origin<cfg.max_active_per_origin:
                            lv=dict(level_id=_hash(VERSION,symbol,origin,int(seg.timestamp.iloc[p['j']]),int(seg.timestamp.iloc[j])),
                                    symbol=symbol,origin=origin,formation_closed_ts=clock,
                                    formation_utc=_iso(clock),first_pivot_open_ts=int(seg.timestamp.iloc[p['j']]),
                                    second_pivot_open_ts=int(seg.timestamp.iloc[j]),center=center,
                                    zone_low=zone_low,zone_high=zone_high,
                                    atr_at_formation=atr,width_atr=width/atr,
                                    pivot_separation_bars=j-p['j'],confirmation_count=2,
                                    segment=segno,source_30m=source,form_index=i,
                                    visits=0,n_up=0,n_down=0,n_return=0,
                                    bars_observed=0,last_visit_index=i,
                                    was_intersecting=(float(bar.low)<=zone_high and float(bar.high)>=zone_low),
                                    last_relation=_relation(float(bar.close),zone_low,zone_high),
                                    end_state='ACTIVE_AT_END',end_closed_ts=None)
                            active.append(lv)
                            formation.append({k:lv[k] for k in FORMATION_COLS})
                # New confirmed pivot is available for a future second pivot.
                pivots.append(dict(j=j,price=price))
        # End state is not a formation-time feature. A gap stops the lifecycle.
        for lv in active:
            lv['end_state']='GAP_CENSORED' if stop<len(frame) else 'ACTIVE_AT_END'
            lv['end_closed_ts']=int(seg.timestamp.iloc[-1])+TF
            ends.append(_end(lv))
    report=dict(symbol=symbol,source_30m=source,bars30=len(frame),segments=len(bounds),
                gaps30=max(0,len(bounds)-1),levels=len(formation),
                visits=sum(e['event_type']=='VISIT' for e in events),
                cross_up=sum(e['event_type']=='CROSS_UP' for e in events),
                cross_down=sum(e['event_type']=='CROSS_DOWN' for e in events),
                returns=sum(e['event_type']=='RETURN_INSIDE' for e in events),
                status='COMPLETE' if len(frame)>=cfg.min_history_bars else 'INSUFFICIENT_30M',error='')
    return formation,events,ends,report


def _event(events,lv,kind,bar,closed,a,b,seg,source):
    events.append(dict(event_id=_hash(lv['level_id'],int(closed),kind),
        level_id=lv['level_id'],symbol=lv['symbol'],origin=lv['origin'],event_type=kind,
        observed_closed_ts=int(closed),observed_utc=_iso(closed),bar_open_ts=int(bar.timestamp),
        previous_relation=a,close_relation=b,side_at_pre=a,close=float(bar.close),
        high=float(bar.high),low=float(bar.low),center=lv['center'],
        zone_low=lv['zone_low'],zone_high=lv['zone_high'],atr_at_formation=lv['atr_at_formation'],
        age_bars=int((int(closed)-lv['formation_closed_ts'])//TF),
        visit_number=lv['visits'],segment=seg,source_30m=source))


def _end(lv):
    return dict(level_id=lv['level_id'],symbol=lv['symbol'],origin=lv['origin'],
        formation_closed_ts=lv['formation_closed_ts'],end_state=lv['end_state'],
        end_closed_ts=lv['end_closed_ts'],n_visits_after_formation=lv['visits'],
        n_cross_up=lv['n_up'],n_cross_down=lv['n_down'],n_returns=lv['n_return'],
        bars_observed=lv['bars_observed'])


def _atomic_csv(path,rows,columns):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.hllv2-',dir=path.parent,suffix='.csv')
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as h:
            writer=csv.DictWriter(h,fieldnames=columns,extrasaction='ignore')
            writer.writeheader();writer.writerows(rows)
            h.flush();os.fsync(h.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.hllv2-',dir=path.parent,suffix='.json')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as h:
            json.dump(value,h,indent=2,sort_keys=True,ensure_ascii=False)
            h.flush();os.fsync(h.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load_30m(arc,symbol):
    native,src=arc._bars(symbol,'30m')
    fifteen,_=arc._bars(symbol,'15m')
    if not fifteen.empty:
        derived=aggregate_15m_to_30m(fifteen)
        if native.empty:
            return derived,'PROXY_15M_PAIR'
        missing=derived.loc[~derived.timestamp.isin(native.timestamp)]
        if not missing.empty:
            # Never silently accept conflicting 30m and 15m OHLC; preference is native.
            native=normalize(pd.concat([native,missing],ignore_index=True),TF)
            src+='+PROXY_15M_PAIR'
    return native,src


def parser(argv=None):
    p=argparse.ArgumentParser(description='Causal historical support/resistance lifecycle; no LIVE or order execution')
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--candles-dir',type=Path,default=None)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--symbols',type=str,default='')
    p.add_argument('--run-tag',type=str,default='pilot')
    p.add_argument('--min-free-gb',type=float,default=0.5)
    return p.parse_args(argv)


def main(argv=None):
    args=parser(argv)
    cfg=Config()
    if not args.run_tag or not all(x.isalnum() or x in '._-' for x in args.run_tag):
        raise SystemExit('Invalid --run-tag')
    root=args.root.resolve()
    output=root/DEFAULT_OUTPUT/args.run_tag
    with Archive(root,args.candles_dir) as arc:
        syms=arc.symbols()
        if args.symbols:
            requested=set(s.strip().upper() for s in args.symbols.split(',') if s.strip())
            syms=[s for s in syms if s in requested]
            if set(syms)!=requested:
                raise SystemExit('Missing archived symbols: '+','.join(sorted(requested-set(syms))))
        if args.max_symbols>0:syms=syms[:args.max_symbols]
        info=dict(version=VERSION,mode='PLAN_READ_ONLY' if args.plan else 'RUN_ARCHIVE_ONLY',
                  output=str(output),symbols=len(syms),config=asdict(cfg),
                  note='Zero future outcome access; no orders, no Redis, no network')
        print('HLL_V2_PLAN',json.dumps(info,ensure_ascii=False),flush=True)
        if args.plan:
            for sym in syms[:5]:
                b,src=load_30m(arc,sym)
                print('HLL_V2_COVERAGE',json.dumps(dict(symbol=sym,bars30=len(b),source=src)),flush=True)
            return 0
        if not syms:raise SystemExit('No archived symbols')
        if shutil.disk_usage(root).free/1024**3 < args.min_free_gb:
            raise SystemExit('Not enough disk space')
        fingerprint=_hash(VERSION,json.dumps(info['config'],sort_keys=True),','.join(syms),str(args.candles_dir))
        manifest=dict(version=VERSION,config=asdict(cfg),symbols=syms,source_type='CANDLES_DIR' if args.candles_dir else 'ARCHIVE_SQLITE',fingerprint=fingerprint)
        mfile=output/'run_config.json'
        if output.exists():
            raise SystemExit('RUN_TAG_EXISTS: this lab never overwrites a prior freeze; choose a new --run-tag')
        _atomic_json(mfile,manifest)
        shard=output/'shards';shard.mkdir(exist_ok=True)
        coverage=[]
        for ix,sym in enumerate(syms,1):
            try:
                b,src=load_30m(arc,sym)
                lv,ev,en,diag=scan_symbol(sym,b,cfg,src)
                for filename,records,cols in (('formation',lv,FORMATION_COLS),('events',ev,EVENT_COLS),('ends',en,END_COLS)):
                    _atomic_csv(shard/(sym+'.'+filename+'.csv'),records,cols)
            except Exception as exc:
                diag=dict(symbol=sym,source_30m='ERROR',bars30=0,segments=0,gaps30=0,
                    levels=0,visits=0,cross_up=0,cross_down=0,returns=0,status='SOURCE_ERROR',error=f'{type(exc).__name__}: {exc}')
            coverage.append(diag)
            print(f'HLL_V2_PROGRESS {ix}/{len(syms)} {sym}: {diag["status"]} levels={diag["levels"]} visits={diag["visits"]} crossings={diag["cross_up"]+diag["cross_down"]}',flush=True)
        _atomic_csv(output/'coverage.csv',coverage,COVERAGE_COLS)
        errors=[x for x in coverage if x['status']=='SOURCE_ERROR']
        audit=dict(version=VERSION,symbols=len(syms),source_errors=len(errors),
                   bars30=sum(x['bars30'] for x in coverage),
                   levels=sum(x['levels'] for x in coverage),
                   visits=sum(x['visits'] for x in coverage),
                   cross_up=sum(x['cross_up'] for x in coverage),
                   cross_down=sum(x['cross_down'] for x in coverage),
                   returns=sum(x['returns'] for x in coverage),
                   source_statuses=dict(Counter(x['status'] for x in coverage)),
                   phase='ATLAS_AND_CAUSAL_EVENTS_ONLY',
                   caveat='No future 1m outcomes, no TP/SL or profitability claim. 30m closes only.')
        _atomic_json(output/'audit.json',audit)
        print('HLL_V2_RESULT',json.dumps(audit,ensure_ascii=False),flush=True)
        return 2 if errors else 0


if __name__=='__main__':
    raise SystemExit(main())
