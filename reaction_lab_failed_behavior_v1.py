#!/usr/bin/env python3
"""FAILED Behavior Lab V1: descriptive, strictly read-only research.

Causal anchor: 15m confirmed swing + first retest's CLOSED 1m candle.
All future measurements start at the next 1m candle and use only the saved
contiguous 1m path in first_touch_paths_v2.sqlite3. No PnL, no trading actions,
no REST/Redis, and no modifications to the source SQLite databases.

Input: reports/reaction_lab/scanner_first_touches_v1.sqlite3
       reports/reaction_lab/first_touch_paths_v2.sqlite3
Output: reports/reaction_lab/failed_behavior_v1/ (CSV, JSON, Markdown)
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import sqlite3
import statistics
import struct
import zlib
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

MINUTE = 60000
BAR = struct.Struct('<qdddd')
HORIZONS = (1, 2, 3, 5, 10, 15, 30, 60, 120, 180, 360)
LANDMARKS = (3, 5, 10, 15)
FUTURES = (15, 30, 60)
EXTRA_THRESHOLDS = (0.25, 0.5, 1.0)
TOL_PCT = 0.10  # canonical continuous scanner parameter; can be overridden


def finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def positive(value):
    x=finite(value)
    return x if x is not None and x > 0 else None


def ro_db(path):
    p=Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    con=sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=25)
    con.execute('PRAGMA query_only=ON')
    return con


def decode(blob):
    if not blob:
        return []
    raw=zlib.decompress(blob)
    if len(raw)%BAR.size:
        raise ValueError('Invalid first-touch 1m path encoding')
    return [BAR.unpack_from(raw,i) for i in range(0,len(raw),BAR.size)]


def read_events(journal, paths):
    """Return all failed entries, including events WITHOUT path coverage."""
    with closing(ro_db(journal)) as con:
        journal_rows=con.execute('''SELECT touch_key,symbol,side,status,retest_ts,
                    first_seen_ms,observation_type,causal_json
                    FROM first_touches WHERE status='TOUCH_FAILED'
                    ORDER BY retest_ts,touch_key''').fetchall()
    with closing(ro_db(paths)) as con:
        paths_by_key={r[0]:r for r in con.execute('''SELECT touch_key,status,state,count,
                     entry_ts,path_blob FROM paths WHERE status='TOUCH_FAILED' ''')}
    out=[]
    for key,symbol,side,status,ts,seen,obs,payload in journal_rows:
        meta=json.loads(payload)
        if not isinstance(meta,dict):raise ValueError('Bad causal_json '+key)
        recorded=paths_by_key.get(key)
        if recorded:
            _,path_status,state,count,entry_ts,blob=recorded
            bars=decode(blob)
            if path_status!=status or count!=len(bars):
                raise ValueError('Invalid path row for '+key)
            if entry_ts!=ts+MINUTE or any(b[0]!=entry_ts+i*MINUTE for i,b in enumerate(bars)):
                raise ValueError('Non-causal/non-contiguous path '+key)
            for b in bars:
                if (b[1]<=0 or b[2]<max(b[1],b[4],b[3]) or
                    b[3]>min(b[1],b[4],b[2]) or
                    not all(math.isfinite(x) for x in b[1:])):
                    raise ValueError('Bad OHLC for '+key)
        else:
            state='NOT_CAPTURED';bars=[]
        out.append(dict(key=key,symbol=symbol,side=side,status=status,ts=int(ts),
                        seen_ms=int(seen),observation=obs,meta=meta,bars=bars,state=state))
    return out


def break_distance(price,level,side):
    """Positive = across swing in failure/breakout direction; negative = reclaimed."""
    return ((level-price)/level*100 if side=='LONG'
            else (price-level)/level*100)


def move_from_fail(price,fail,side):
    """Positive = CONTINUATION of the failure, negative = reversal."""
    return ((fail-price)/fail*100 if side=='LONG' else (price-fail)/fail*100)


def depth_extreme(bar,level,side):
    return break_distance(bar[3] if side=='LONG' else bar[2],level,side)


def recovery_extreme(bar,level,side):
    return break_distance(bar[2] if side=='LONG' else bar[3],level,side)


def band(value,breaks,labels):
    for i,cut in enumerate(breaks):
        if value<cut:return labels[i]
    return labels[-1]


def penetration_bucket(v):
    return band(v, (.20,.35,.60,1.10), ('0.10-0.20','0.20-0.35','0.35-0.60','0.60-1.10','>=1.10'))


def early_bucket(v):
    return band(v,(-0.5,-0.15,0.15,0.5),('<-0.50','-0.50:-0.15','-0.15:+0.15','+0.15:+0.50','>=+0.50'))


def wilson(successes,n,z=1.96):
    if n<=0:return (None,None)
    p=successes/n;d=1+z*z/n
    center=(p+z*z/(2*n))/d
    error=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return (100*(center-error),100*(center+error))


def pct(values):
    arr=[float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not arr:return dict(n=0,mean=None,median=None,p10=None,p90=None)
    arr.sort()
    def quantile(p):
        idx=(len(arr)-1)*p
        l=int(idx);r=min(l+1,len(arr)-1)
        return arr[l]+(arr[r]-arr[l])*(idx-l)
    return dict(n=len(arr),mean=sum(arr)/len(arr),median=quantile(.5),p10=quantile(.1),p90=quantile(.9))


def first_index(values,pred):
    for i,val in enumerate(values,1):
        if pred(val):return i
    return None


def first_sustained(values,pred,n):
    hits=0
    for i,x in enumerate(values,1):
        hits=hits+1 if pred(x) else 0
        if hits>=n:return i
    return None


def first_race(bars,level,side,fail_close,tol,extra,style='CLOSE'):
    """Chronological first threshold from POST-failure candles.

    EXTEND threshold measured from fail CLOSE, not hindsight peak.
    REENTER threshold measured at the failure edge of the fixed swing band.
    CLOSE uses only close. INTRABAR uses H/L and marks same-bar ties AMBIGUOUS.
    Return (label,minute), where NONE is only a completed horizon result.
    """
    threshold=move_from_fail(fail_close,fail_close,side)+extra
    for j,b in enumerate(bars,1):
        if style=='CLOSE':
            extend=move_from_fail(b[4],fail_close,side)>=threshold
            reenter=break_distance(b[4],level,side)<=tol
        else:
            extend=move_from_fail(b[3] if side=='LONG' else b[2],fail_close,side)>=threshold
            reenter=recovery_extreme(b,level,side)<=tol
        if extend and reenter:return 'AMBIGUOUS',j
        if extend:return 'EXTEND_FIRST',j
        if reenter:return 'REENTER_FIRST',j
    return 'NEITHER',None


def minute_snapshot(ev,t,level,fail_close,tol):
    bars=ev['bars'];side=ev['side']
    if len(bars)<t:return None
    b=bars[t-1]
    window=bars[:t]
    closes=[break_distance(x[4],level,side) for x in window]
    initial_d=break_distance(fail_close,level,side)
    outside=[d>tol for d in closes]
    crossings=sum(outside[i]!=outside[i-1] for i in range(1,len(outside)))
    return dict(touch_key=ev['key'],symbol=ev['symbol'],original_side=side,
      observation_type=ev['observation'],minute=t,bar_close_ts=b[0]+MINUTE,
      distance_to_swing_pct=closes[-1],distance_outside_band_pct=closes[-1]-tol,
      signed_change_from_fail_pct=move_from_fail(b[4],fail_close,side),
      close_reclaimed_band=closes[-1]<=tol,
      close_reclaimed_swing=closes[-1]<=0,
      outside_closes=sum(outside),outside_fraction=sum(outside)/t,
      band_crossings=crossings,
      peak_extension_additional_pct=max(0,max(depth_extreme(x,level,side) for x in window)-initial_d),
      peak_reversal_from_fail_pct=max(0,-min(recovery_extreme(x,level,side) for x in window)+initial_d),
      bar_range_pct=(b[2]-b[3])/level*100)


def horizon_stats(ev,h,level,fail_close,tol):
    if len(ev['bars'])<h:return None
    bars=ev['bars'][:h]; side=ev['side']
    initial=break_distance(fail_close,level,side)
    close_d=[break_distance(b[4],level,side) for b in bars]
    ext=[depth_extreme(b,level,side) for b in bars]
    rev=[recovery_extreme(b,level,side) for b in bars]
    first_reenter=first_index(close_d,lambda d:d<=tol)
    first_swing=first_index(close_d,lambda d:d<=0)
    first_two=first_sustained(close_d,lambda d:d<=tol,2)
    first_three=first_sustained(close_d,lambda d:d<=tol,3)
    longest_out=0;current=0
    for d in close_d:
        current=current+1 if d>tol else 0
        longest_out=max(longest_out,current)
    inside=[d<=tol for d in close_d]
    crossings=sum(inside[i]!=inside[i-1] for i in range(1,len(inside)))
    result=dict(touch_key=ev['key'],symbol=ev['symbol'],original_side=side,
      observation_type=ev['observation'],retest_ts=ev['ts'],horizon=h,
      fail_close_penetration_pct=initial,
      last_close_distance_pct=close_d[-1],
      last_close_change_from_fail_pct=move_from_fail(bars[-1][4],fail_close,side),
      highest_penetration_pct=max(ext),
      further_extension_pct=max(0,max(ext)-initial),
      maximum_reversal_from_fail_pct=max(0,initial-min(rev)),
      max_consecutive_outside_closes=longest_out,
      outside_closes=sum(d>tol for d in close_d),outside_fraction=sum(d>tol for d in close_d)/h,
      closed_band_crossings=crossings,
      first_reenter_min=first_reenter,first_reclaim_swing_min=first_swing,
      first_2close_reenter_min=first_two,first_3close_reenter_min=first_three,
      reentered_by_h=first_reenter is not None,
      reclaimed_swing_by_h=first_swing is not None,
      sustained_2close_by_h=first_two is not None,
      sustained_3close_by_h=first_three is not None)
    for th in EXTRA_THRESHOLDS:
        k=str(th).replace('.','p')
        close_type, close_min=first_race(bars,level,side,fail_close,tol,th,'CLOSE')
        wick_type,wick_min=first_race(bars,level,side,fail_close,tol,th,'INTRABAR')
        result[f'close_race_{k}']=close_type
        result[f'close_race_{k}_minute']=close_min
        result[f'wick_race_{k}']=wick_type
        result[f'wick_race_{k}_minute']=wick_min
    return result


def base_row(ev,tol):
    meta=ev['meta'];level=positive(meta.get('swing_price'))
    fail=positive(meta.get('retest_close'))
    low=positive(meta.get('retest_low'));high=positive(meta.get('retest_high'))
    side=ev['side']
    valid=(level is not None and fail is not None and side in ('LONG','SHORT'))
    depth=break_distance(fail,level,side) if valid else None
    # A FAILED candle should CLOSE beyond the *opposite* tolerance edge.
    consistent=valid and depth>tol-1e-8
    wick_depth=(break_distance(low if side=='LONG' else high,level,side)
                if valid and low and high else None)
    out=dict(touch_key=ev['key'],symbol=ev['symbol'],original_side=side,
      status=ev['status'],observation_type=ev['observation'],retest_ts=ev['ts'],
      first_seen_ms=ev['seen_ms'],path_state=ev['state'],saved_1m_bars=len(ev['bars']),
      valid_metadata=valid,consistent_failed=consistent,
      swing_price=level,fail_retest_close=fail,
      fail_retest_high=high,fail_retest_low=low,
      fail_close_penetration_pct=depth,
      fail_close_beyond_band_pct=depth-tol if depth is not None else None,
      fail_wick_penetration_pct=wick_depth,
      fail_wick_minus_close_pct=wick_depth-depth if depth is not None and wick_depth is not None else None,
      fail_range_pct=((high-low)/level*100 if level and low and high else None),
      close_location=finite(meta.get('retest_close_location')),
      max_prior_departure_pct=finite(meta.get('max_departure_pct')),
      rel_volume=finite(meta.get('relative_volume')),
      retest_volume=finite(meta.get('retest_volume')),
      confirmed_to_retest_min=finite(meta.get('confirmed_to_retest_min')),
      penetration_bin=penetration_bucket(depth) if depth is not None else 'MISSING',
      range_bin=(band((high-low)/level*100,(.2,.5,1.0),('<0.2','0.2:0.5','0.5:1.0','>=1.0'))
                 if level and low and high else 'MISSING'),
      volume_bin=(band(finite(meta.get('relative_volume')),(.7,1.2,2.0),('<0.7','0.7:1.2','1.2:2.0','>=2.0'))
                  if finite(meta.get('relative_volume')) is not None else 'MISSING'),
      departure_bin=(band(finite(meta.get('max_departure_pct')),(.5,1.0,2.0),('<0.5','0.5:1.0','1.0:2.0','>=2.0'))
                     if finite(meta.get('max_departure_pct')) is not None else 'MISSING'))
    return out


def analysis(events,tol):
    base=[];horizons=[];landmarks=[];conditional_rows=[]
    bad=[]
    for ev in events:
        b=base_row(ev,tol);base.append(b)
        if not b['consistent_failed']:
            bad.append(ev['key']);continue
        level=b['swing_price'];fail=b['fail_retest_close']
        for h in HORIZONS:
            m=horizon_stats(ev,h,level,fail,tol)
            if m is not None:horizons.append(m)
        for t in HORIZONS:
            s=minute_snapshot(ev,t,level,fail,tol)
            if s is not None:landmarks.append(s)
        for t in LANDMARKS:
            s=minute_snapshot(ev,t,level,fail,tol)
            if s is None:continue
            for h in FUTURES:
                if len(ev['bars'])<t+h:continue
                future=ev['bars'][t:t+h]
                after_t=ev['bars'][t-1][4]
                end=future[-1][4]
                dcl=[break_distance(x[4],level,ev['side']) for x in future]
                conditional_rows.append(dict(
                  touch_key=ev['key'],symbol=ev['symbol'],original_side=ev['side'],
                  observation_type=ev['observation'],retest_ts=ev['ts'],
                  landmark=t,forward=h,penetration_bin=b['penetration_bin'],
                  range_bin=b['range_bin'],volume_bin=b['volume_bin'],
                  departure_bin=b['departure_bin'],
                  early_move_bin=early_bucket(s['signed_change_from_fail_pct']),
                  early_signed_change_pct=s['signed_change_from_fail_pct'],
                  early_outside_fraction=s['outside_fraction'],
                  early_reclaimed=s['close_reclaimed_band'],
                  landmark_distance_pct=s['distance_to_swing_pct'],
                  future_end_signed_change_pct=move_from_fail(end,after_t,ev['side']),
                  future_any_reenter=any(d<=tol for d in dcl),
                  future_any_reclaim_swing=any(d<=0 for d in dcl),
                  future_extension_from_landmark_pct=max(0,
                    max(depth_extreme(x,level,ev['side']) for x in future)-
                    break_distance(after_t,level,ev['side'])),
                  future_reversal_from_landmark_pct=max(0,
                    break_distance(after_t,level,ev['side'])-
                    min(recovery_extreme(x,level,ev['side']) for x in future)),
                  future_outside_fraction=sum(d>tol for d in dcl)/h,
                ))
    return base,horizons,landmarks,conditional_rows,bad


def summary(base,horizons, tol):
    b_by={b['touch_key']:b for b in base}
    keys_by_group={'ALL':set(b_by)}
    for key,b in b_by.items():
        for col,label in [('original_side','SIDE'),('observation_type','OBS'),('penetration_bin','PEN')]:
            keys_by_group.setdefault(f'{label}:{b[col]}',set()).add(key)
    rows=[]
    for group,keys in keys_by_group.items():
        for h in HORIZONS:
            items=[x for x in horizons if x['horizon']==h and x['touch_key'] in keys]
            n=len(items);total=len(keys)
            reentered=sum(x['reentered_by_h'] for x in items)
            reclaim=sum(x['reclaimed_swing_by_h'] for x in items)
            ci=wilson(reentered,n)
            rows.append(dict(group=group,horizon=h,events_total=total,
              complete_n=n,coverage_pct=100*n/total if total else 0,
              reenter_pct=100*reentered/n if n else None,
              reenter_ci95_low=ci[0],reenter_ci95_high=ci[1],
              reclaim_swing_pct=100*reclaim/n if n else None,
              sustained_2close_pct=100*sum(x['sustained_2close_by_h'] for x in items)/n if n else None,
              median_first_reenter_min=pct(x['first_reenter_min'] for x in items)['median'],
              median_additional_extension_pct=pct(x['further_extension_pct'] for x in items)['median'],
              mean_additional_extension_pct=pct(x['further_extension_pct'] for x in items)['mean'],
              median_reversal_from_fail_pct=pct(x['maximum_reversal_from_fail_pct'] for x in items)['median'],
              median_outside_fraction=pct(x['outside_fraction'] for x in items)['median'],
              median_last_change_pct=pct(x['last_close_change_from_fail_pct'] for x in items)['median']))
    return rows


def conditional_summary(rows,min_n):
    groups=defaultdict(list)
    for r in rows:
        keys=[('ALL','ALL'),('PENETRATION',r['penetration_bin']),
              ('EARLY_MOVE',r['early_move_bin']),
              ('FAIL_RANGE',r['range_bin']),
              ('REL_VOLUME',r['volume_bin']),
              ('PRE_DEPARTURE',r['departure_bin']),
              ('SIDE',r['original_side']),
              ('OBSERVATION',r['observation_type']),
              ('PEN_X_EARLY',r['penetration_bin']+'|'+r['early_move_bin']),
              ('EARLY_RECLAIMED','YES' if r['early_reclaimed'] else 'NO'),
              ('EARLY_OUTSIDE', 'ALL' if r['early_outside_fraction']==1
               else 'NONE' if r['early_outside_fraction']==0 else 'SOME')]
        for kind,value in keys:
            groups[(r['landmark'],r['forward'],kind,value)].append(r)
    result=[]
    for (t,h,kind,value),group in sorted(groups.items()):
        n=len(group);re=sum(x['future_any_reenter'] for x in group)
        ci=wilson(re,n)
        result.append(dict(landmark=t,forward=h,cohort_kind=kind,cohort=value,
          n=n,exploratory_small_n=n<min_n,
          future_reenter_pct=100*re/n,
          future_reenter_ci95_low=ci[0],future_reenter_ci95_high=ci[1],
          future_reclaim_swing_pct=100*sum(x['future_any_reclaim_swing'] for x in group)/n,
          future_continuation_close_pct=100*sum(x['future_end_signed_change_pct']>0 for x in group)/n,
          median_future_end_change_pct=pct(x['future_end_signed_change_pct'] for x in group)['median'],
          median_future_extension_pct=pct(x['future_extension_from_landmark_pct'] for x in group)['median'],
          median_future_reversal_pct=pct(x['future_reversal_from_landmark_pct'] for x in group)['median'],
          mean_future_extension_pct=pct(x['future_extension_from_landmark_pct'] for x in group)['mean'],
          mean_future_reversal_pct=pct(x['future_reversal_from_landmark_pct'] for x in group)['mean']))
    return result


def temporal_consistency(rows,min_n):
    """Chronological UTC-date thirds; show stability, not OOS certification.

    Date partitions are formed BEFORE grouping, so same UTC trading day is
    never split across the three descriptive segments.
    """
    unique_dates=sorted({datetime.fromtimestamp(r['retest_ts']/1000,timezone.utc).date().isoformat()
                         for r in rows})
    if not unique_dates:return []
    date_segment={d:min(2,i*3//len(unique_dates)) for i,d in enumerate(unique_dates)}
    groups=defaultdict(list)
    for r in rows:
        date=datetime.fromtimestamp(r['retest_ts']/1000,timezone.utc).date().isoformat()
        segment=date_segment[date]
        for kind,value in [('ALL','ALL'),('EARLY_MOVE',r['early_move_bin']),
                           ('PENETRATION',r['penetration_bin']),('ORIGINAL_SIDE',r['original_side'])]:
            groups[(r['landmark'],r['forward'],segment,kind,value)].append(r)
    out=[]
    for (t,h,segment,kind,value),items in sorted(groups.items()):
        n=len(items);re=sum(r['future_any_reenter'] for r in items)
        lo,hi=wilson(re,n)
        out.append(dict(landmark=t,forward=h,segment_index=segment+1,
          segment_start_date=min(d for d in unique_dates if date_segment[d]==segment),
          segment_end_date=max(d for d in unique_dates if date_segment[d]==segment),
          cohort_kind=kind,cohort=value,n=n,exploratory_small_n=n<min_n,
          future_reenter_pct=100*re/n,ci95_low=lo,ci95_high=hi,
          median_future_signed_change_pct=pct(r['future_end_signed_change_pct'] for r in items)['median']))
    return out


def minute_by_minute(events,tol,max_minute=60):
    """Full longitudinal data, not just preselected landmark snapshots."""
    rows=[]
    for ev in events:
        b=base_row(ev,tol)
        if not b['consistent_failed']:continue
        level=b['swing_price'];fail=b['fail_retest_close'];side=ev['side']
        cumulative_out=0;crossings=0;prior_out=True  # FAIL close outside
        peak_ext=0;peak_reversal=0
        for i,x in enumerate(ev['bars'][:max_minute],1):
            d=break_distance(x[4],level,side)
            outside=d>tol
            cumulative_out+=int(outside)
            crossings+=int(outside!=prior_out)
            prior_out=outside
            peak_ext=max(peak_ext,depth_extreme(x,level,side)-b['fail_close_penetration_pct'])
            peak_reversal=max(peak_reversal,b['fail_close_penetration_pct']-recovery_extreme(x,level,side))
            rows.append(dict(touch_key=ev['key'],symbol=ev['symbol'],original_side=side,
              observation_type=ev['observation'],minute=i,
              close_distance_to_swing_pct=d,close_outside_band=outside,
              signed_close_change_from_fail_pct=move_from_fail(x[4],fail,side),
              signed_open_change_from_fail_pct=move_from_fail(x[1],fail,side),
              outside_closes_so_far=cumulative_out,band_crossings_so_far=crossings,
              peak_extra_extension_pct=max(0,peak_ext),
              peak_reversal_pct=max(0,peak_reversal)))
    return rows


def minute_profile(minutes,total_failed):
    """Per-minute available-case profile; coverage always explicit."""
    groups=defaultdict(list)
    for r in minutes:groups[r['minute']].append(r)
    out=[]
    for t,items in sorted(groups.items()):
        n=len(items)
        signed=pct(r['signed_close_change_from_fail_pct'] for r in items)
        out.append(dict(minute=t,evaluable_n=n,failed_total=total_failed,
          coverage_pct=100*n/total_failed if total_failed else 0,
          outside_close_pct=100*sum(r['close_outside_band'] for r in items)/n,
          continue_close_pct=100*sum(r['signed_close_change_from_fail_pct']>0 for r in items)/n,
          signed_move_mean_pct=signed['mean'],signed_move_median_pct=signed['median'],
          signed_move_p10_pct=signed['p10'],signed_move_p90_pct=signed['p90'],
          median_cumulative_extension_pct=pct(r['peak_extra_extension_pct'] for r in items)['median'],
          median_cumulative_reversal_pct=pct(r['peak_reversal_pct'] for r in items)['median']))
    return out


def reentry_survival(events,tol,max_h=360):
    """Kaplan-Meier-style time to FIRST close inside band, censor at path end.

    Events with zero post-fail bars are counted as immediately censored at t=0.
    This is descriptive only: censoring may be informative, especially legacy
    events whose paths fell out of the 400-minute Redis buffer.
    """
    failures=[]
    for ev in events:
        b=base_row(ev,tol)
        if not b['consistent_failed']:continue
        level=b['swing_price']
        available=min(max_h,len(ev['bars']))
        close_d=[break_distance(x[4],level,ev['side']) for x in ev['bars'][:available]]
        first=first_index(close_d,lambda d:d<=tol)
        failures.append((first if first is not None else available,first is not None))
    result=[];at_risk=len(failures);survival=1.0
    counts=Counter(failures)
    result.append(dict(minute=0,at_risk=at_risk,reentries=0,censored=counts.get((0,False),0),
       km_survival_pct=100,km_reentered_pct=0))
    at_risk-=counts.get((0,False),0)
    for t in range(1,max_h+1):
        at_start=at_risk
        d=counts.get((t,True),0)
        c=counts.get((t,False),0)
        if at_start and d:
            survival*=1-d/at_start
        at_risk-=d+c
        result.append(dict(minute=t,at_risk=at_start,reentries=d,censored=c,
           km_survival_pct=100*survival,km_reentered_pct=100*(1-survival)))
    return result


def races_summary(horizons):
    groups=defaultdict(list)
    for row in horizons:
        for th in EXTRA_THRESHOLDS:
            k=str(th).replace('.','p')
            for style in ('close','wick'):
                groups[(row['horizon'],th,style)].append(row[f'{style}_race_{k}'])
    out=[]
    for (h,th,style),labels in sorted(groups.items()):
        c=Counter(labels);n=len(labels)
        out.append(dict(horizon=h,extra_extension_pct=th,measurement=style,
          evaluable_n=n,extend_first=c['EXTEND_FIRST'],reenter_first=c['REENTER_FIRST'],
          ambiguous_same_bar=c['AMBIGUOUS'],neither=c['NEITHER'],
          extend_first_pct=100*c['EXTEND_FIRST']/n,
          reenter_first_pct=100*c['REENTER_FIRST']/n,
          ambiguous_pct=100*c['AMBIGUOUS']/n))
    return out


def casebook(base,horizons,limit=10):
    by_key={b['touch_key']:b for b in base}
    eligible=[m for m in horizons if m['horizon']==60]
    result=[]
    rules=(('MOST_EXTENSION',lambda x:-x['further_extension_pct']),
           ('EARLIEST_REENTER',lambda x:(x['first_reenter_min'] if x['first_reenter_min'] is not None else 10**9)),
           ('MOST_CROSSINGS',lambda x:-x['closed_band_crossings']),
           ('DEEP_NO_REENTER',lambda x:(0 if not x['reentered_by_h'] else 1,
                                       -x['further_extension_pct'])))
    for rule,order in rules:
        for m in sorted(eligible,key=lambda x:(order(x),x['touch_key']))[:limit]:
            b=by_key[m['touch_key']]
            result.append(dict(case=rule,touch_key=m['touch_key'],symbol=m['symbol'],
              original_side=m['original_side'],retest_ts=m['retest_ts'],
              penetration_pct=b['fail_close_penetration_pct'],
              extension_pct=m['further_extension_pct'],
              reversal_pct=m['maximum_reversal_from_fail_pct'],
              first_reenter_min=m['first_reenter_min'],
              crossings=m['closed_band_crossings']))
    return result


def write_csv(path,rows):
    if not rows:return
    fields=list(rows[0])
    with open(path,'w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        for r in rows:writer.writerow(r)


def fmt(x,d=2):
    return '-' if x is None else f'{x:.{d}f}'


def markdown_report(stats,base,horizons,conditional,races,bad,tol,journal,paths):
    all_stats=[x for x in stats if x['group']=='ALL']
    lines=[
      '# FAILED Behavior Lab V1 — descriptivo, NO estrategia',
      '',f'Generado: {datetime.now(timezone.utc).isoformat()}',
      f'Journal: `{journal}`',f'Paths: `{paths}`','',
      '## Definiciones y criterio causal',
      '',f'- Nivel fijo: `swing_price` conocido al cierre de FAILED; banda canonica ±{tol:.2f}%.',
      '- `t=0` es el CIERRE de la vela FAILED. `t=1` es el CIERRE de la 1m siguiente.',
      '- Distancia positiva = hacia el lado de la ruptura (FAILED LONG -> baja; FAILED SHORT -> sube).',
      '- Penetracion inicial = distancia del cierre FAILED al swing; extensión adicional desde ESE cierre.',
      '- Primer reingreso = primer CIERRE nuevamente dentro del borde externo de la zona; no es necesariamente cruce del swing.',
      '- `CLOSE` usa cierres, evita ambiguedad intrabar. `WICK` usa high/low y registra `AMBIGUOUS` si ambos niveles se tocaron en una misma vela.',
      '- Ausencia de recuperacion en N minutos solo significa `no ocurrio` si N velas contiguas estan presentes.',
      '- Comparaciones condicionadas en minuto 3/5/10/15: rasgos previos solo hasta ese cierre; resultados desde la siguiente vela.',
      '- El estudio NO calcula rentabilidad, entradas, costos ni TP/SL.',
      '', '## Cobertura y observaciones','',
      f'- FAILED en journal: **{len(base)}**.',
      f'- Con metadata inconsistente/faltante: **{len(bad)}** (excluidos del analisis de trayectorias).',
      f'- Sin ninguna vela 1m guardada: **{sum(b["saved_1m_bars"]==0 for b in base)}**.',
      f'- Penetracion inicial, mediana entre metadata valida: **{fmt(pct(b["fail_close_penetration_pct"] for b in base)["median"])}%**.',
      f'- LIVE_OBSERVED: **{sum(b["observation_type"]=="LIVE_OBSERVED" for b in base)}**.',
      f'- RECOVERED_WINDOW: **{sum(b["observation_type"]=="RECOVERED_WINDOW" for b in base)}**.',
      '', '## Resumen por horizonte','',
      '| Min | Completos / Total | Reingresan zona | Recuperan swing | Med. extensión extra | Med. reversión |',
      '|---:|---:|---:|---:|---:|---:|',
    ]
    for s in all_stats:
        lines.append(f'| {s["horizon"]} | {s["complete_n"]}/{s["events_total"]} | {fmt(s["reenter_pct"])}% | {fmt(s["reclaim_swing_pct"])}% | {fmt(s["median_additional_extension_pct"])}% | {fmt(s["median_reversal_from_fail_pct"])}% |')
    lines += ['', '## Orden de acontecimientos: EXTENSIÓN +0,50% vs primer reingreso (cierres)','',
      '| Min | N | Extiende primero | Reingresa primero | Ninguno |',
      '|---:|---:|---:|---:|---:|']
    for r in races:
        if r['measurement']=='close' and r['extra_extension_pct']==.5:
            lines.append(f'| {r["horizon"]} | {r["evaluable_n"]} | {fmt(r["extend_first_pct"])}% | {fmt(r["reenter_first_pct"])}% | {r["neither"]} |')
    lines += ['', '## Cohortes predictivas exploratorias, NO confirmadas','',
      'Las tablas `conditional_groups.csv` contienen N, fraccion de recuperaciones futuras, cambios de precio FUTUROS, medianas y Wilson 95%.',
      'No buscar umbrales optimos en esta muestra para despues reportarlos como prueba fuera de muestra.',
      'La muestra tiene dependencia entre simbolos, dias y eventos: los intervalos Wilson son nominales y pueden ser optimistas.',
      '', '## Archivos principales','',
      '- `failed_events.csv`: una fila por FAILED, tambien los sin cobertura.','- `event_horizons.csv`: recorrido y orden de hechos para cada ventana COMPLETA.',
      '- `minute_landmarks.csv`: trayectoria minuto 1/2/3/5/10/15/30/60/120/180/360.',
      '- `minute_by_minute.csv`: recorrido de cada evento en todos los primeros 60 minutos completos disponibles.',
      '- `minute_profile.csv`: tendencia agregada por minuto 1-60 con cobertura explicitada.',
      '- `reentry_survival.csv`: probabilidad descriptiva KM de primer reingreso, con censura explicita.','- `landmark_future.csv`: features al corte y resultado futuro NO solapado.',
      '- `conditional_groups.csv`: comparaciones de penetration, velocidad, persistencia y sentidos; campos `exploratory_small_n`.',
      '- `temporal_consistency.csv`: estabilidad temporal descriptiva en tres tercios por fecha UTC; no es validacion genuina OOS.',
      '- `first_event_races.csv`: extension vs recuperacion, tanto cierre como mecha con AMBIGUOUS.',
      '- `casebook_60m.csv`: ids concretos para inspeccion manual (no seleccion de trades).',
      '', '## Limitaciones',
      '- El first-touch detector de origen tiene un max-age; los eventos son una muestra seleccionada por esa logica.',
      '- Los caminos empiezan al minuto POSTERIOR; no tenemos el orden intravela dentro del FAILED.',
      '- Datos antiguos que ya salieron de Redis no se recuperan; incompletos = CENSURADOS.',
      '- No hay ATR causal en el journal; se usan porcentajes, no ATR estimado con datos futuros.',
      '- No hay microestructura ni spread real en esta fuente; cualquier ejecucion necesita otros datos.',
      '- Un FAIL es una clasificacion al cierre y no implica automaticamente tendencia favorable.',
      '']
    return '\n'.join(lines)


def run(journal,paths,out,min_n=20,tol=TOL_PCT):
    if not 0<tol<2:raise ValueError('Tolerance percentage out of expected bounds')
    if out.resolve() in (Path(journal).resolve(),Path(paths).resolve()):
        raise ValueError('Cannot write to source database')
    events=read_events(journal,paths)
    if not events:raise RuntimeError('No TOUCH_FAILED found')
    base,horizons,landmarks,condition,bad=analysis(events,tol)
    stats=summary(base,horizons,tol)
    conditioned=conditional_summary(condition,min_n)
    races=races_summary(horizons)
    cases=casebook(base,horizons)
    minute_rows=minute_by_minute(events,tol)
    profiles=minute_profile(minute_rows,len(events))
    temporal=temporal_consistency(condition,min_n)
    survival=reentry_survival(events,tol)
    out.mkdir(parents=True,exist_ok=True)
    for name,rows in [('failed_events',base),('event_horizons',horizons),
       ('minute_landmarks',landmarks),('landmark_future',condition),
       ('horizon_groups',stats),('minute_by_minute',minute_rows),
       ('minute_profile',profiles),('reentry_survival',survival),
       ('conditional_groups',conditioned),('temporal_consistency',temporal),
       ('first_event_races',races),('casebook_60m',cases)]:
        write_csv(out/(name+'.csv'),rows)
    report=dict(version='failed_behavior_v1',created_utc=datetime.now(timezone.utc).isoformat(),
      tolerance_pct=tol,failed_total=len(events),invalid_metadata=len(bad),
      saved_1m_total=sum(len(e['bars']) for e in events),
      path_states=dict(Counter(e['state'] for e in events)),
      observation_types=dict(Counter(e['observation'] for e in events)),
      coverage_by_horizon={str(s['horizon']):{'n':s['complete_n'],'total':s['events_total'],
         'pct':round(s['coverage_pct'],2)} for s in stats if s['group']=='ALL'},
      source_files={'journal':str(journal),'paths':str(paths)},
      output_folder=str(out),
      methodology='Read-only, no PnL; no future information in landmark features; complete consecutive horizons only')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'ANALYSIS.md').write_text(markdown_report(stats,base,horizons,conditioned,races,bad,tol,journal,paths),encoding='utf-8')
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--journal',type=Path)
    p.add_argument('--paths',type=Path)
    p.add_argument('--output',type=Path)
    m=p.add_mutually_exclusive_group()
    m.add_argument('--check',action='store_true')
    m.add_argument('--run',action='store_true')
    p.add_argument('--tolerance-pct',type=float,default=TOL_PCT)
    p.add_argument('--min-group-n',type=int,default=20)
    a=p.parse_args(argv)
    root=a.project.resolve()
    journal=(a.journal or root/'reports/reaction_lab/scanner_first_touches_v1.sqlite3').resolve()
    paths=(a.paths or root/'reports/reaction_lab/first_touch_paths_v2.sqlite3').resolve()
    out=(a.output or root/'reports/reaction_lab/failed_behavior_v1').resolve()
    if a.min_group_n<1:p.error('--min-group-n must be >=1')
    if a.check or not a.run:
        print('FAILED_BEHAVIOR_CHECK '+json.dumps(dict(journal_exists=journal.is_file(),paths_exists=paths.is_file(),
               output=str(out),tolerance_pct=a.tolerance_pct,read_only_sources=True,no_mutations=True),ensure_ascii=False))
        return 0
    report=run(journal,paths,out,a.min_group_n,a.tolerance_pct)
    print('FAILED_BEHAVIOR_REPORT '+json.dumps(report,ensure_ascii=False))
    print('FAILED_BEHAVIOR_SUMMARY '+str(out/'ANALYSIS.md'))
    return 0

if __name__=='__main__':raise SystemExit(main())
