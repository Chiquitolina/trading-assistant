#!/usr/bin/env python3
"""Structural Level Atlas V1: outcome-blind, causal horizontal-level research.

Read-only Full Context + independent Compression History; writes derived CSV only.
All level selection and PRE labels use CLOSED candles ending before retest.
RETEST_CLOSED never decides which level is selected, and must not be used to
simulate an entry earlier than next minute open. No live trading/Redis/network.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd

import reaction_compression_lineage as lineage
import reaction_compression_breakout_retest_v4 as v4

VERSION = 'structural-level-atlas-v1.0'
OUTPUT = 'reaction_structural_level_atlas_v1'
STEP = {'15m': 15*60_000, '30m': 30*60_000}
LOOKBACK = {'15m': 96, '30m': 240}
MIN_BARS = {'15m': 64, '30m': 80}
# Non-tuned structural parameters. Freeze them before outcome comparison.
LEFT = RIGHT = 3
MIN_PIVOT_GAP = 5
MIN_ZONE_PCT = 0.10
ZONE_ATR_MULT = 0.35
ACCEPT_CLOSES = 2
MIN_PIVOT_PROMINENCE_ATR = 0.40
HOLD_LAST_BARS = 4
HOLD_MIN_CLOSES = 3

TF_COLS = [
    'event_key','symbol','side','date_utc','observation_type','retest_ts',
    'timeframe','coverage','source','history_bars','pivot_highs','pivot_lows',
    'candidates','state_pre','state_known','level_origin','level_price',
    'zone_low','zone_high','halfwidth_pct','pivot_count_at_formation',
    'formation_closed_ts','breakout_closed_ts','breakout_age_min',
    'acceptance_favorable_last4','defenses_since_breakout','last_defense_closed_ts',
    'last_pre_close','pre_distance_pct','pre_distance_atr','pre_near_level','retest_touch','retest_hold',
    'known_wick_distance_pct','known_close_distance_pct','ranking_score', 'reason',
]
BEST_COLS = [
    'event_key','symbol','side','date_utc','observation_type','retest_ts',
    'selection_timeframe','selection_state_pre','selection_state_known',
    'selection_level_origin','selection_level_price','selection_zone_low',
    'selection_zone_high','selection_pre_distance_pct','selection_pre_distance_atr','selection_pre_near_level','selection_breakout_age_min',
    'selection_acceptance_last4','selection_defenses','selection_retest_touch',
    'selection_retest_hold','coverage_all','has_level','n_timeframes_with_level',
]


def price_side_pct(price: float, level: float, side: str) -> float:
    return 100.0 * (float(price)/float(level)-1.0)*(1 if side == 'LONG' else -1)


def true_range_at(df: pd.DataFrame, idx: int, span: int=14) -> float | None:
    if idx < span or idx >= len(df): return None
    x = df.iloc[idx-span:idx+1]
    h=x.high.to_numpy(dtype=float)[1:]
    l=x.low.to_numpy(dtype=float)[1:]
    c=x.close.to_numpy(dtype=float)[:-1]
    tr=np.maximum(h-l,np.maximum(abs(h-c),abs(l-c)))
    val=float(np.mean(tr))
    return val if val > 0 and math.isfinite(val) else None


def pivots_confirmed(df: pd.DataFrame, left=LEFT, right=RIGHT):
    """Pivot becomes visible only after RIGHT following CLOSED bars."""
    if len(df)<left+right+1: return []
    hi=df.high.to_numpy(dtype=float)
    lo=df.low.to_numpy(dtype=float)
    ts=df.timestamp.to_numpy(dtype='int64')
    result=[]
    for j in range(left,len(df)-right):
        # strictly unique swing; flat equal highs/lows are not pivots
        is_hi = hi[j]>hi[j-left:j].max() and hi[j]>hi[j+1:j+right+1].max()
        is_lo = lo[j]<lo[j-left:j].min() and lo[j]<lo[j+1:j+right+1].min()
        atr=true_range_at(df,j+right)
        if atr is None:continue
        # Require actual local protrusion relative to its adjacent ranges, not a
        # tiny swing drawn merely because it exceeds both neighbors by a tick.
        high_prom=min(hi[j]-lo[j-left:j].min(),hi[j]-lo[j+1:j+right+1].min())
        low_prom=min(hi[j-left:j].max()-lo[j],hi[j+1:j+right+1].max()-lo[j])
        is_hi=is_hi and high_prom>=MIN_PIVOT_PROMINENCE_ATR*atr
        is_lo=is_lo and low_prom>=MIN_PIVOT_PROMINENCE_ATR*atr
        if is_hi: result.append(dict(kind='HIGH',pivot_i=j,confirm_i=j+right,
            pivot_ts=int(ts[j]),confirm_ts=int(ts[j+right]),price=float(hi[j])))
        if is_lo: result.append(dict(kind='LOW',pivot_i=j,confirm_i=j+right,
            pivot_ts=int(ts[j]),confirm_ts=int(ts[j+right]),price=float(lo[j])))
    return result


def frozen_zones(df, pivots, kind, step):
    """Freeze a zone at the SECOND confirmed separated pivot.

    Candidate center and ATR width cannot be changed by later price/outcomes.
    Formation timestamp is the second pivot's confirmation-close timestamp.
    This deliberately favors precision over coverage and does not chase levels.
    """
    ordered=sorted((p for p in pivots if p['kind']==kind),key=lambda p:p['confirm_i'])
    zones=[]
    for i,a in enumerate(ordered):
        for b in ordered[i+1:]:
            if b['pivot_i']-a['pivot_i']<MIN_PIVOT_GAP: continue
            atr=true_range_at(df,b['confirm_i'])
            if atr is None: continue
            center0=a['price']
            permitted=max(center0*MIN_ZONE_PCT/100,ZONE_ATR_MULT*atr)
            if abs(b['price']-center0)>permitted:continue
            center=(a['price']+b['price'])/2.0
            width=max(center*MIN_ZONE_PCT/100,ZONE_ATR_MULT*atr,abs(a['price']-b['price'])/2)
            zones.append(dict(origin=kind,center=center,low=center-width,high=center+width,
                halfwidth_pct=100*width/center,form_i=b['confirm_i'],
                formation_closed_ts=int(df.timestamp.iloc[b['confirm_i']])+step,
                pivot_count=2,atr_at_formation=atr))
            break
    # Remove almost identical duplicates without looking at event outcome.
    zones.sort(key=lambda x:(x['formation_closed_ts'],x['center']))
    chosen=[]
    for z in zones:
        if any(abs(z['center']-p['center'])<=min(z['high']-z['center'],p['high']-p['center'])
               and z['origin']==p['origin'] for p in chosen):
            continue
        chosen.append(z)
    return chosen


def evaluate_zone(df,z,side,cutoff_ts,step):
    """Classification solely from bars closed STRICTLY BEFORE retest_ts."""
    form=int(z['form_i'])
    after=df.iloc[form+1:]
    if len(after)<ACCEPT_CLOSES: return None
    favorable_high = (side=='LONG')
    kind_flip=('HIGH' if side=='LONG' else 'LOW')
    origin='RESISTANCE_FLIP' if side=='LONG' else 'SUPPORT_FLIP'
    if z['origin']!=kind_flip:
        origin='EXISTING_SUPPORT' if side=='LONG' else 'EXISTING_RESISTANCE'
    close=after.close.to_numpy(dtype=float)
    high=after.high.to_numpy(dtype=float)
    low=after.low.to_numpy(dtype=float)
    if favorable_high:
        outside=close>z['high'];fav=close>=z['center']; adverse=close<z['low']
        visits=(low<=z['high']) & (close>=z['center'])
    else:
        outside=close<z['low'];fav=close<=z['center']; adverse=close>z['high']
        visits=(high>=z['low']) & (close<=z['center'])
    # A flip must have a two-CLOSED-candle breakout beyond frozen zone.
    breakout_i=None
    if z['origin']==kind_flip:
        for q in range(len(outside)-1):
            if outside[q] and outside[q+1]:
                breakout_i=q+1
                break
        if breakout_i is None:return None
    else:
        breakout_i=0  # established support/resistance without needing a break
    subsequent=after.iloc[breakout_i+1:]
    sub_fav=fav[breakout_i+1:]
    sub_adverse=adverse[breakout_i+1:]
    sub_visits=visits[breakout_i+1:]
    if not len(subsequent):return None
    n_accept=int(fav[-HOLD_LAST_BARS:].sum())
    broke_again=bool(np.any(sub_adverse))
    is_accepted=n_accept>=HOLD_MIN_CLOSES
    n_defenses=int(np.sum(sub_visits))
    last_defense=int(subsequent.timestamp.iloc[np.where(sub_visits)[0][-1]])+step if n_defenses else None
    prior_lost=bool(np.any(sub_adverse))
    last_close=float(df.close.iloc[-1]);center=float(z['center'])
    dist=price_side_pct(last_close,center,side)
    # LOST only if current trailing 2 closes are outside adverse side; intermediate
    # temporary loss with recovery is RECLAIMED if acceptance subsequently restored.
    lost_now=bool(adverse[-2:].all())
    if lost_now:state='LOST_LEVEL_PRE'
    elif not is_accepted:state='UNCONFIRMED_ACCEPTANCE'
    elif prior_lost:state='RECLAIM_ACCEPTED'
    elif n_defenses:state='ACCEPTED_DEFENDED'
    else:state='ACCEPTED_NO_DEFENSE'
    bidx=after.iloc[breakout_i]
    age=(cutoff_ts-(int(bidx.timestamp)+step))/60_000
    atr=true_range_at(df,len(df)-1)
    dist_atr=dist/100*center/atr if atr else None
    return dict(level_origin=origin,state_pre=state,level_price=center,
        zone_low=z['low'],zone_high=z['high'],halfwidth_pct=z['halfwidth_pct'],
        pivot_count_at_formation=z['pivot_count'],formation_closed_ts=z['formation_closed_ts'],
        breakout_closed_ts=int(bidx.timestamp)+step if z['origin']==kind_flip else None,
        breakout_age_min=age if z['origin']==kind_flip else None,
        acceptance_favorable_last4=n_accept,defenses_since_breakout=n_defenses,
        last_defense_closed_ts=last_defense,last_pre_close=last_close,
        pre_distance_pct=dist,pre_distance_atr=dist_atr,
        pre_near_level=bool(dist_atr is not None and abs(dist_atr)<=1.5),
        ranking_score=0.0)


def select_level(options):
    """Outcome blind: prior accepted state, normalized proximity, earlier evidence."""
    if not options:return None
    priority={'ACCEPTED_DEFENDED':5,'RECLAIM_ACCEPTED':4,'ACCEPTED_NO_DEFENSE':3,
              'UNCONFIRMED_ACCEPTANCE':2,'LOST_LEVEL_PRE':1}
    def order(z):
        # A faraway strong level is not allowed to win merely on defense count.
        # Distance >=3 ATR is a diagnosis, not an actionable nearby level.
        dn=abs(z['pre_distance_atr']) if z['pre_distance_atr'] is not None else 1e6
        near=dn<=1.5
        score=(int(near),priority[z['state_pre']],-dn,
               min(3,z['defenses_since_breakout']),-z['formation_closed_ts'])
        return score
    return max(options,key=order)


def classify_known(selected,side,retest):
    """Current retest candle is for AFTER-CLOSE description, not PRE selection."""
    if not selected or retest is None:
        return dict(state_known='UNAVAILABLE',retest_touch=None,retest_hold=None,
                    known_wick_distance_pct=None,known_close_distance_pct=None)
    # tuple = open_ts,open,high,low,close
    center=selected['level_price'];zl=selected['zone_low'];zh=selected['zone_high']
    extreme=float(retest[3] if side=='LONG' else retest[2])
    close=float(retest[4])
    touched=(extreme<=zh if side=='LONG' else extreme>=zl)
    held=(close>=center if side=='LONG' else close<=center)
    if touched and held:state='TOUCH_AND_HOLD_KNOWN'
    elif touched:state='TOUCH_FAILED_HOLD_KNOWN'
    else:state='NO_TOUCH_KNOWN'
    return dict(state_known=state,retest_touch=touched,retest_hold=held,
        known_wick_distance_pct=price_side_pct(extreme,center,side),
        known_close_distance_pct=price_side_pct(close,center,side))


def scan_event(event,tf,df,source,pre,retest):
    step=STEP[tf];r=event['retest_ts']
    # CLOSED bars: candle close must be <= retest OPEN, strictly no retest data.
    eligible=df[(df.timestamp+step)<=r].tail(LOOKBACK[tf]).copy().reset_index(drop=True)
    row={k:None for k in TF_COLS}
    row.update({k:event[k] for k in ('event_key','symbol','side','date_utc','observation_type','retest_ts')})
    row.update(timeframe=tf,source=source,history_bars=len(eligible),
               coverage='COMPLETE' if len(eligible)==LOOKBACK[tf] else
                        'PARTIAL' if len(eligible)>=MIN_BARS[tf] else 'INSUFFICIENT')
    if row['coverage']=='INSUFFICIENT':
        row.update(state_pre='UNKNOWN_HISTORY',state_known='NOT_EVALUABLE',reason='Not enough closed HTF bars')
        return row
    # A gap invalidates causal interpretation; not an absence of a level.
    stamps=eligible.timestamp.to_numpy(dtype='int64')
    if not np.all(np.diff(stamps)==step):
        row.update(coverage='GAPPED',state_pre='UNKNOWN_HISTORY',state_known='NOT_EVALUABLE',reason='Discontinuous HTF history')
        return row
    if not np.isfinite(eligible[['open','high','low','close','volume']].to_numpy(dtype=float)).all():
        row.update(coverage='INVALID',state_pre='UNKNOWN_HISTORY',state_known='NOT_EVALUABLE',reason='Invalid OHLCV')
        return row
    pivots=pivots_confirmed(eligible)
    row['pivot_highs']=sum(x['kind']=='HIGH' for x in pivots)
    row['pivot_lows']=sum(x['kind']=='LOW' for x in pivots)
    opts=[]
    for kind in ('HIGH','LOW'):
        for z in frozen_zones(eligible,pivots,kind,step):
            candidate=evaluate_zone(eligible,z,event['side'],r,step)
            if candidate:opts.append(candidate)
    row['candidates']=len(opts)
    winner=select_level(opts)
    if winner is None:
        row.update(state_pre='NO_LEVEL',state_known='NOT_EVALUABLE',reason='No two-pivot confirmed level and causal post-formation structure')
        return row
    row.update(winner)
    row.update(classify_known(winner,event['side'],retest))
    row['reason']='LEVEL_FROZEN_AFTER_SECOND_PIVOT_CONFIRMATION_PRE_RETEST'
    return row


def choose_event(rows):
    options=[r for r in rows if r.get('level_price') is not None and r.get('coverage') in ('COMPLETE','PARTIAL')]
    if not options:return None
    complete=[r for r in options if r.get('coverage')=='COMPLETE']
    # A partially covered series may still describe a level, but must not
    # displace an equally available fully covered timeframe in global selection.
    return select_level(complete if complete else options)


def best_row(e,rows):
    w=choose_event(rows)
    d={k:e[k] for k in ('event_key','symbol','side','date_utc','observation_type','retest_ts')}
    mapping={'selection_timeframe':'timeframe','selection_state_pre':'state_pre',
      'selection_state_known':'state_known','selection_level_origin':'level_origin',
      'selection_level_price':'level_price','selection_zone_low':'zone_low',
      'selection_zone_high':'zone_high','selection_pre_distance_pct':'pre_distance_pct',
      'selection_pre_distance_atr':'pre_distance_atr','selection_pre_near_level':'pre_near_level',
      'selection_breakout_age_min':'breakout_age_min',
      'selection_acceptance_last4':'acceptance_favorable_last4',
      'selection_defenses':'defenses_since_breakout','selection_retest_touch':'retest_touch',
      'selection_retest_hold':'retest_hold'}
    d.update({k:w.get(v) if w is not None else None for k,v in mapping.items()})
    if w is None:d['selection_state_pre']='NO_LEVEL' if all(r['coverage']=='COMPLETE' for r in rows) else 'UNKNOWN_COVERAGE'
    d['coverage_all']=int(all(r['coverage']=='COMPLETE' for r in rows))
    d['has_level']=int(w is not None)
    d['n_timeframes_with_level']=sum(r.get('level_price') is not None for r in rows)
    return d


def write_csv_atomic(path,rows,columns):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.atlas_',suffix='.csv')
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
            w.writeheader();w.writerows(rows);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.isfile(tmp):os.unlink(tmp)


def run(root,max_events=40,cohort='live-stratified'):
    audit=lineage.integrity(root)
    ctx_path=root/lineage.ROOT_REL/lineage.CTX_NAME
    history_path=root/lineage.ROOT_REL/lineage.HISTORY_NAME
    hist=lineage.ro(history_path) if history_path.exists() else None
    allrows=[];best=[]
    with closing(lineage.ro(ctx_path)) as ctx:
        events=v4.select_events(ctx,cohort,max_events)
        by_symbol=defaultdict(list)
        for e in events:by_symbol[e['symbol']].append(e)
        try:
            for symbol,items in by_symbol.items():
                df15,df30,source30=v4._merge_tf(ctx,hist,symbol)
                df15=df15.reset_index();df30=df30.reset_index()
                for e in items:
                    pre,retest=lineage.one_min_pre_and_retest(ctx,symbol,e['retest_ts'])
                    rows=[scan_event(e,'15m',df15,'NATIVE_15M',pre,retest),
                          scan_event(e,'30m',df30,source30,pre,retest)]
                    allrows.extend(rows);best.append(best_row(e,rows))
        finally:
            if hist is not None:hist.close()
    if len(allrows)!=2*len(events) or len(best)!=len(events):raise RuntimeError('Atlas incompleto')
    folder=root/lineage.ROOT_REL/OUTPUT
    write_csv_atomic(folder/'levels_by_timeframe.csv',allrows,TF_COLS)
    write_csv_atomic(folder/'event_best.csv',best,BEST_COLS)
    digest=lineage.hash_file
    summary=dict(version=VERSION,events=len(events),rows=len(allrows),cohort=cohort,
        max_events=max_events,frozen_cohort_sha256=audit['frozen_cohort_sha256'],
        coverage=dict(Counter(r['coverage'] for r in allrows)),
        states_pre=dict(Counter(r['selection_state_pre'] for r in best)),
        events_with_level=sum(r['has_level'] for r in best),
        file_sha256={name:digest(folder/name) for name in ('levels_by_timeframe.csv','event_best.csv')},
        assumptions=['Fixed minimum 2 confirmed, separated same-kind pivots; ATR-derived zone width',
                     'All level anchors frozen at second pivot confirmation, after CLOSE',
                     'PRE levels chosen without retest OHLC or TP/SL outcomes',
                     'RETEST_CLOSED labels descriptive only: next-open entry would require new replay',
                     'All TP/SL comparisons exploratory; chronological independent validation required'])
    (folder/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('LEVEL_ATLAS_DONE',json.dumps({'events':len(events),'rows':len(allrows),
       'coverage':summary['coverage'],'states_pre':summary['states_pre']}),flush=True)
    return summary


def plan(root,max_events,cohort):
    audit=lineage.integrity(root)
    with closing(lineage.ro(root/lineage.ROOT_REL/lineage.CTX_NAME)) as ctx:
        events=v4.select_events(ctx,cohort,max_events)
    path=root/lineage.ROOT_REL/lineage.HISTORY_NAME
    return dict(version=VERSION,frozen_events=audit['events'],selected_events=len(events),
        symbols=len(set(e['symbol'] for e in events)),cohort=cohort,
        selection_by_day_side=dict(Counter(f"{e['date_utc']} {e['side']}" for e in events)),
        shared_cache_exists=path.is_file(),read_only_original_sources=True,
        writes=False,network_calls=False,
        fixed_params={'pivot_left':LEFT,'pivot_right':RIGHT,'min_pivot_gap':MIN_PIVOT_GAP,
                      'min_zone_pct':MIN_ZONE_PCT,'zone_atr_mult':ZONE_ATR_MULT,
                      'min_pivot_prominence_atr':MIN_PIVOT_PROMINENCE_ATR,
                      'acceptance_2_closed_break':ACCEPT_CLOSES,'accepted_last_4_min_3':HOLD_MIN_CLOSES})


def audit_output(root):
    folder=root/lineage.ROOT_REL/OUTPUT
    summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    file_ok=all(lineage.hash_file(folder/name)==digest for name,digest in summary['file_sha256'].items())
    import pandas as pd
    a=pd.read_csv(folder/'levels_by_timeframe.csv',low_memory=False)
    b=pd.read_csv(folder/'event_best.csv',low_memory=False)
    unique=not b.event_key.duplicated().any()
    coverage_all=a.groupby('event_key').size().eq(2).all()
    pre=a.dropna(subset=['formation_closed_ts'])
    chronological=bool((pre.formation_closed_ts<pre.retest_ts).all())
    known=a.dropna(subset=['breakout_closed_ts'])
    chronological=chronological and bool((known.breakout_closed_ts<known.retest_ts).all())
    source_ok=summary['frozen_cohort_sha256']==lineage.integrity(root)['frozen_cohort_sha256']
    success=bool(file_ok and unique and coverage_all and chronological and source_ok and
                 len(a)==2*len(b)==summary['rows'])
    return dict(success=success,events=len(b),rows=len(a),sha256_matches=file_ok,
        unique_events=unique,all_two_timeframes=bool(coverage_all),chronology_valid=chronological,
        frozen_cohort_matches=source_ok,coverage=summary['coverage'],states_pre=summary['states_pre'])


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    op=p.add_mutually_exclusive_group(required=True)
    op.add_argument('--plan',action='store_true');op.add_argument('--run',action='store_true');op.add_argument('--audit',action='store_true')
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    p.add_argument('--cohort',choices=['live-stratified','all-live','all-frozen'],default='live-stratified')
    p.add_argument('--max-events',type=int,default=40,help='0 means whole selected cohort')
    a=p.parse_args(argv);root=a.root.resolve()
    if a.max_events<0:p.error('max-events>=0')
    if a.plan:res=plan(root,a.max_events,a.cohort)
    elif a.run:res=run(root,a.max_events,a.cohort)
    else:res=audit_output(root)
    print(json.dumps(res,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
