#!/usr/bin/env python3
"""HLL V2.6 | causal resistance-structure research, 30m, offline only.

Consumes frozen V2.1 level formation/event shards and V2.2 CROSS_UP census.
Never alters inputs, LIVE, orders, Redis, or PnL. Entry research is deferred.
Features are timestamped at the attempted breakout; lifecycle transitions are
stored in a distinct file, with knowledge timestamps (no future leakage).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = 'hll-v2.6-causal-structure-v1'
TF = 30 * 60 * 1000
DEFAULT_ATLAS = Path('reports/horizontal_level_lifecycle_v21/core15_extended_sep_oct_v21')
DEFAULT_SIGNAL = Path('reports/horizontal_level_cross_research_v22/runs/core15_extended_sep_oct_v22')
DEFAULT_CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
DEFAULT_OUT = Path('reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26')

@dataclass(frozen=True)
class Rules:
    acceptance_window_bars: int = 4
    initial_failure_window_bars: int = 4
    retest_window_bars: int = 24
    defense_window_bars: int = 4
    defense_distance_atr: float = .50
    sustained_window_bars: int = 4
    pressure_window_bars: int = 24
    pressure_long_window_bars: int = 48
    pressure_pivot_min_steps: int = 2   # three known 2x2 LOW pivots
    pressure_higher_low_atr: float = .10
    pressure_near_atr: float = 1.0
    min_free_gb: float = .5


def _safe_float(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def _int(x, fallback=0):
    v = _safe_float(x)
    return int(v) if v is not None else fallback


def _round(x, precision=7):
    v = _safe_float(x)
    return round(v, precision) if v is not None else ''


def _iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def _digest(*parts):
    return hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:24]


def _sha_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _atomic_csv(p, rows, cols):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=p.parent, prefix='.v26-', suffix='.csv')
    try:
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader(); w.writerows(rows)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, p)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def _atomic_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=p.parent, prefix='.v26-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, p)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def _at(i, n):
    return 0 <= i < n


def _event(rows, att, kind, ts, note='', **extra):
    if ts is not None:
        rows.append(dict(attempt_id=att, transition=kind, known_closed_ts=int(ts),
                         known_utc=_iso(ts), note=note, **extra))


def classify_followup(known_ts, idx, open_ts, lows, highs, closes, zone_low, zone_high, atr,
                      rules=Rules(), last_eligible_ts=None):
    """Return retrospective outcome + per-transition events with actual knowledge clocks.

    open_ts is a contiguous 30m array. No future transitions returned at/before entry.
    Outcome CENSORED when observations insufficient. Acceptance requires two
    consecutive ABOVE closes *starting from the initial breakout bar*, hence
    normally the next bar. No post-hoc rewriting of early features is allowed.
    """
    n = len(open_ts)
    assert _at(idx, n) and int(open_ts[idx]) + TF == int(known_ts)
    result = dict(status='', first_4b_status='', accepted_closed_ts='',
                  failed_closed_ts='', retest_closed_ts='', defense_closed_ts='',
                  flip_sustained_closed_ts='', direct_classification_closed_ts='',
                  followup_bars_available=0)
    transitions = []
    last_idx = n-1
    if last_eligible_ts is not None:
        # Level may expire as a DETECTOR, but its forward history can still be
        # studied. Do not apply active_end cutoff as an invented price barrier.
        pass

    def valid(i):
        return i <= last_idx and i >= idx and int(open_ts[i]) == int(open_ts[idx]) + (i-idx)*TF

    accepted_i = None
    for j in range(idx+1, idx+rules.acceptance_window_bars):
        if not valid(j):
            result['status'] = result['first_4b_status'] = 'CENSORED_ACCEPTANCE'
            result['followup_bars_available'] = max(0, j-idx-1)
            return result, transitions
        c = float(closes[j]); ts = int(open_ts[j])+TF
        if c < zone_low:
            result.update(status='FAILED_BREAKOUT', first_4b_status='FAILED_BREAKOUT', failed_closed_ts=ts,
                          followup_bars_available=j-idx)
            _event(transitions, '', 'FAILED_BREAKOUT', ts, 'close below zone_low before acceptance')
            return result, transitions
        if c > zone_high and float(closes[j-1]) > zone_high:
            accepted_i = j
            result['accepted_closed_ts'] = ts
            result['first_4b_status'] = 'ACCEPTED_ABOVE'
            _event(transitions, '', 'ACCEPTED_ABOVE', ts, 'two consecutive closes above zone_high')
            break
    if accepted_i is None:
        # Initial breakout plus three subsequent 30m bars observed.
        result['status'] = result['first_4b_status'] = 'UNRESOLVED_FIRST4'
        result['followup_bars_available'] = rules.acceptance_window_bars - 1
        _event(transitions, '', 'UNRESOLVED_FIRST4', int(open_ts[idx+rules.acceptance_window_bars-1])+TF)
        return result, transitions

    for j in range(accepted_i+1, accepted_i+rules.retest_window_bars+1):
        if not valid(j):
            result['status'] = 'CENSORED_POST_ACCEPTANCE'
            result['followup_bars_available'] = max(0,j-idx-1)
            return result, transitions
        ts = int(open_ts[j])+TF
        c = float(closes[j])
        # The first contact is physical, even when V2.1's independent test is
        # not yet armed. A fully skipped zone with close below is also a loss.
        contact = float(lows[j]) <= zone_high and float(highs[j]) >= zone_low
        if contact:
            result['retest_closed_ts'] = ts
            _event(transitions, '', 'RETEST_CONTACT_FROM_ABOVE', ts, 'physical 30m candle range overlaps zone')
            if c < zone_low:
                result.update(status='RETEST_FAILED', failed_closed_ts=ts,
                              followup_bars_available=j-idx)
                _event(transitions, '', 'RETEST_FAILED', ts, 'contact candle closed below zone_low')
                return result, transitions
            return _resolve_retest(result, transitions, idx, j, open_ts, closes,
                                   zone_low, zone_high, atr, rules)
        if c < zone_low:
            result.update(status='ACCEPTED_THEN_FAILED_NO_CONTACT', failed_closed_ts=ts,
                          followup_bars_available=j-idx)
            _event(transitions, '', 'ACCEPTED_THEN_FAILED_NO_CONTACT', ts, 'price skipped entire band')
            return result, transitions
    end_i = accepted_i+rules.retest_window_bars
    result.update(status='DIRECT_NO_RETEST_24B', direct_classification_closed_ts=int(open_ts[end_i])+TF,
                  followup_bars_available=end_i-idx)
    _event(transitions, '', 'DIRECT_NO_RETEST_24B', int(open_ts[end_i])+TF,
           'no physical zone contact or loss during 24 closed bars')
    return result, transitions


def _resolve_retest(result, transitions, breakout_i, retest_i, opens, closes, lo, hi, atr, rules):
    n = len(opens)
    for k in range(retest_i+1, retest_i+rules.defense_window_bars+1):
        if k >= n or int(opens[k]) != int(opens[retest_i])+(k-retest_i)*TF:
            result.update(status='CENSORED_RETEST', followup_bars_available=k-breakout_i-1)
            return result, transitions
        c = float(closes[k]); ts = int(opens[k])+TF
        if c < lo:
            result.update(status='RETEST_FAILED', failed_closed_ts=ts,
                          followup_bars_available=k-breakout_i)
            _event(transitions, '', 'RETEST_FAILED', ts, 'close below zone_low before defense')
            return result, transitions
        if c > hi + rules.defense_distance_atr*atr:
            result['defense_closed_ts'] = ts
            _event(transitions, '', 'FLIP_CONFIRMED', ts, 'retest defended by >= 0.5 formation ATR')
            for j in range(k+1, k+rules.sustained_window_bars+1):
                if j>=n or int(opens[j]) != int(opens[k])+(j-k)*TF:
                    result.update(status='FLIP_CONFIRMED_SUSTAINED_CENSORED',
                                  followup_bars_available=j-breakout_i-1)
                    return result, transitions
                jt = int(opens[j])+TF
                if float(closes[j]) < lo:
                    result.update(status='FLIP_CONFIRMED_LATER_LOST', failed_closed_ts=jt,
                                  followup_bars_available=j-breakout_i)
                    _event(transitions, '', 'FLIP_LATER_LOST', jt)
                    return result, transitions
            ts = int(opens[k+rules.sustained_window_bars])+TF
            result.update(status='FLIP_SUSTAINED_4B', flip_sustained_closed_ts=ts,
                          followup_bars_available=k+rules.sustained_window_bars-breakout_i)
            _event(transitions, '', 'FLIP_SUSTAINED_4B', ts, 'four subsequent closes did not lose band')
            return result, transitions
    result.update(status='RETEST_UNRESOLVED', followup_bars_available=retest_i+rules.defense_window_bars-breakout_i)
    _event(transitions, '', 'RETEST_UNRESOLVED', int(opens[retest_i+rules.defense_window_bars])+TF)
    return result, transitions


def _confirmed_low_pivots(opens, highs, lows):
    """Returns (pivot_index, confirmation_closed_ts, price); 2x2 confirmed only."""
    rows=[]
    for j in range(2,len(opens)-2):
        v=float(lows[j]); before=lows[j-2:j]; after=lows[j+1:j+3]
        if v < min(before) and v < min(after):
            rows.append((j,int(opens[j+2])+TF,v))
    return rows


def _pressure_features(i, opens, highs, lows, closes, zone_high, atr_pre, low_pivots, rules):
    """Only bars strictly before breakout are inspected; pivot confirmation <= prebar close."""
    result={}
    prev_ts=int(opens[i-1])+TF
    for w in (rules.pressure_window_bars,rules.pressure_long_window_bars):
        piv=[(j,p) for j,ts,p in low_pivots if i-w<=j<i and ts<=prev_ts]
        piv.sort()
        pvals=[p for _,p in piv]
        rises=sum(pvals[k]-pvals[k-1]>=rules.pressure_higher_low_atr*atr_pre
                  for k in range(1,len(pvals))) if atr_pre>0 else 0
        result[f'confirmed_low_pivots_pre{w}']=len(pvals)
        result[f'higher_low_steps_pre{w}']=rises
        result[f'last_3_pivot_lows_pre{w}_json']=json.dumps([_round(v,11) for v in pvals[-3:]])
        result[f'higher_lows_last3_pre{w}']=(int(len(pvals)>=3 and all(
            pvals[k]-pvals[k-1]>=rules.pressure_higher_low_atr*atr_pre
            for k in (-2,-1))) if atr_pre>0 else 0)
        v=[float(x) for x in closes[max(0,i-w):i]]
        result[f'pre{w}_close_change_pct']=_round(100*(v[-1]/v[0]-1)) if len(v)>=2 and v[0]>0 else ''
        if i>=w:
            span=highs[i-w:i]-lows[i-w:i]
            denom=float(np.median(span[:max(2,w//3)]))
            result[f'pre{w}_range_contraction_ratio']=_round(float(np.median(span[-max(2,w//3):]))/denom) if denom>0 else ''
        else:
            result[f'pre{w}_range_contraction_ratio']=''
    near = (zone_high-float(closes[i-1]))/atr_pre if atr_pre>0 else None
    result['preclose_distance_to_zone_high_atr']=_round(near)
    result['pressure_build_up'] = int(bool(
        result[f'higher_lows_last3_pre{rules.pressure_window_bars}']
        and near is not None and -0.01<=near<=rules.pressure_near_atr
    ))
    return result


def _touches_for_level(level, evs, index_by_close, opens, highs, lows, closes):
    """V2.1 TEST_FROM_* are the canonical independent contact episodes."""
    atr=float(level['atr_at_formation']);lo=float(level['zone_low']);hi=float(level['zone_high'])
    resolves={str(e['test_id']):e for e in evs if e['event_type'] in
              ('DEFENSE_CONFIRMED','LOSS_CONFIRMED','TEST_TIMEOUT') and str(e.get('test_id','')) not in ('','nan')}
    touches=[]
    for e in evs:
        if not e['event_type'].startswith('TEST_FROM_'):continue
        ts=int(e['observed_closed_ts']); idx=index_by_close.get(ts)
        if idx is None:continue
        side=e['event_type'].replace('TEST_FROM_','')
        depth=(float(highs[idx])-lo) if side=='BELOW' else (hi-float(lows[idx]))
        rejection=(lo-float(closes[idx])) if side=='BELOW' else (float(closes[idx])-hi)
        r=resolves.get(str(e.get('test_id','')))
        rts=int(r['observed_closed_ts']) if r else None
        rclose=float(closes[index_by_close[rts]]) if rts in index_by_close else None
        rej_distance=((lo-rclose) if side=='BELOW' else (rclose-hi)) if rclose is not None else None
        touches.append(dict(level_id=str(level['level_id']),symbol=str(level['symbol']),
                            test_id=str(e.get('test_id','')),touch_closed_ts=ts,
                            touch_utc=_iso(ts), side=side,
                            depth_atr=_round(depth/atr),rejection_atr=_round(rejection/atr),
                            resolver_type=str(r['event_type']) if r else 'UNRESOLVED',
                            resolver_known_ts=rts if rts is not None else '',
                            confirmed_rejection_distance_atr=_round(rej_distance/atr) if r else ''))
    return sorted(touches,key=lambda x:(x['touch_closed_ts'],x['test_id']))


def _prior_touch_features(touches, ts, prior_events, prev_attempts, current_atr):
    t=[a for a in touches if a['touch_closed_ts']<ts]
    from_below=[a for a in t if a['side']=='BELOW']
    from_above=[a for a in t if a['side']=='ABOVE']
    rej=[a for a in from_below if a['resolver_type']=='DEFENSE_CONFIRMED'
         and a['resolver_known_ts']!='' and a['resolver_known_ts']<ts]
    spacing=[int((t[k]['touch_closed_ts']-t[k-1]['touch_closed_ts'])//TF) for k in range(1,len(t))]
    rej_vals=[_safe_float(a['confirmed_rejection_distance_atr']) for a in rej]
    rej_vals=[x for x in rej_vals if x is not None]
    last=t[-1] if t else None
    pri=[e for e in prior_events if int(e['observed_closed_ts'])<ts]
    fails=[a for a in prev_attempts if a.get('failed_closed_ts') and a['failed_closed_ts']<ts]
    failures=sum(a['status']=='FAILED_BREAKOUT' for a in fails)
    later_failures=sum(a['status'].startswith('ACCEPTED_THEN_FAILED') or
                       a['status'] in ('RETEST_FAILED','FLIP_CONFIRMED_LATER_LOST') for a in fails)
    return dict(prior_independent_touches=len(t),prior_touches_from_below=len(from_below),
        prior_touches_from_above=len(from_above),prior_confirmed_rejections=len(rej),
        prior_failed_breakouts=failures,prior_accepted_then_failed=later_failures,
        prior_reconfirmations=sum(e['event_type']=='LEVEL_RECONFIRMED' for e in pri),
        prior_cross_up=sum(e['event_type']=='CROSS_UP' for e in pri),
        prior_cross_down=sum(e['event_type']=='CROSS_DOWN' for e in pri),
        bars_since_last_touch=(int((ts-last['touch_closed_ts'])//TF) if last else ''),
        last_touch_depth_atr=last['depth_atr'] if last else '',
        last_touch_rejection_atr=last['rejection_atr'] if last else '',
        last_touch_side=last['side'] if last else '',
        touch_spacing_bars_json=json.dumps(spacing),
        touch_spacing_median_bars=_round(statistics.median(spacing)) if spacing else '',
        touch_spacing_trend_bars=_round(spacing[-1]-spacing[0]) if len(spacing)>=2 else '',
        total_touch_span_bars=(int((t[-1]['touch_closed_ts']-t[0]['touch_closed_ts'])//TF)
                              if len(t)>=2 else ''),
        rejection_distance_trend_atr=_round(rej_vals[-1]-rej_vals[0]) if len(rej_vals)>=2 else '')


def _new_zone_distance(level, forms, end_by_level, ts, atr):
    """Distance to next HIGH zone active and already confirmed at event close."""
    hi=float(level['zone_high']);candidates=[]
    for o in forms:
        if o['level_id']==level['level_id'] or o['origin']!='HIGH' or int(o['formation_closed_ts'])>ts:
            continue
        end=end_by_level.get(str(o['level_id']))
        if end is None or ts > int(end['end_closed_ts']):
            continue
        low=float(o['zone_low'])
        if low>hi:candidates.append((low-hi)/atr)
    return _round(min(candidates)) if candidates and atr>0 else ''


def run_symbol(symbol, atlas_dir, cand_dir, source_signals, rules=Rules()):
    p=cand_dir / f'{symbol}_30m.csv'
    f=pd.read_csv(p).sort_values('timestamp').reset_index(drop=True)
    needed=['timestamp','open','high','low','close','volume','quoteVolume']
    if not set(needed).issubset(f.columns):raise ValueError(f'{symbol}: invalid 30m schema')
    ot=f.timestamp.astype('int64').to_numpy(); op=f.open.to_numpy(dtype=float)
    hi=f.high.to_numpy(dtype=float);lo=f.low.to_numpy(dtype=float); cl=f.close.to_numpy(dtype=float)
    closed_by_idx=ot+TF; index_by_close={int(t):i for i,t in enumerate(closed_by_idx)}
    if np.any(np.diff(ot)!=TF): raise ValueError(f'{symbol}: 30m gaps unsupported; no silent bridging')
    if f.duplicated('timestamp').any(): raise ValueError(f'{symbol}: duplicate bars')
    sh=atlas_dir/'shards'
    forms_df=pd.read_csv(sh / f'{symbol}.formation.csv',keep_default_na=False)
    events_df=pd.read_csv(sh / f'{symbol}.events.csv',keep_default_na=False)
    ends_df=pd.read_csv(sh / f'{symbol}.ends.csv',keep_default_na=False)
    forms=forms_df.to_dict('records'); events=events_df.to_dict('records')
    events_by_level=defaultdict(list)
    for e in events:events_by_level[str(e['level_id'])].append(e)
    for evs in events_by_level.values():evs.sort(key=lambda e:(int(e['observed_closed_ts']),e['event_type']))
    end_by_level={str(r['level_id']):r for r in ends_df.to_dict('records')}
    sig={ (str(r['level_id']),int(r['known_closed_ts'])):r for r in source_signals
          if str(r['symbol'])==symbol and r['stage']=='RESISTANCE_CROSS_UP'}
    if len(sig)!=sum(1 for r in source_signals if r['symbol']==symbol and r['stage']=='RESISTANCE_CROSS_UP'):
        raise RuntimeError('Duplicate V22 CROSS_UP source event key')
    low_pivots=_confirmed_low_pivots(ot,hi,lo)
    level_records=[];touch_records=[];attempts=[];transitions=[];outcomes=[]
    source_seen=set()
    for level in forms:
        lid=str(level['level_id'])
        if level['origin']!='HIGH':continue
        atr0=float(level['atr_at_formation'])
        bandlo=float(level['zone_low']);bandhi=float(level['zone_high']);center=float(level['center'])
        end=end_by_level.get(lid)
        if end is None:raise RuntimeError(f'No end for {lid}')
        level_records.append(dict(level_id=lid,symbol=symbol,origin='HIGH',
            formation_closed_ts=_int(level['formation_closed_ts']),
            first_pivot_open_ts=_int(level['first_pivot_open_ts']),
            second_pivot_open_ts=_int(level['second_pivot_open_ts']),
            formation_pivots=_int(level['confirmation_count']),
            pivot_separation_bars=_int(level['pivot_separation_bars']),
            formation_price_spread_atr=_round(abs(hi[index_by_close[_int(level['first_pivot_open_ts'])+TF]]
                                                -hi[index_by_close[_int(level['second_pivot_open_ts'])+TF]])/atr0),
            zone_low=bandlo,zone_high=bandhi,center=center,atr_at_formation=atr0,
            zone_width_atr=_round((bandhi-bandlo)/atr0),
            zone_width_pct=_round(100*(bandhi-bandlo)/center),
            quality_flag=level['quality_flag'],
            detector_end_state=end['end_state'],detector_end_closed_ts=_int(end['end_closed_ts'])))
        evs=events_by_level.get(lid,[])
        ts=_touches_for_level(level,evs,index_by_close,ot,hi,lo,cl)
        touch_records.extend(ts)
        crosses=[e for e in evs if e['event_type']=='CROSS_UP']
        prev_att=[]
        for e in crosses:
            known_ts=int(e['observed_closed_ts']);i=index_by_close.get(known_ts)
            if i is None:raise RuntimeError(f'{lid}: CROSS_UP not mapped to 30m')
            if known_ts<=int(level['formation_closed_ts']):raise RuntimeError('CROSS before formation')
            if known_ts>int(end['end_closed_ts']):raise RuntimeError('CROSS after detector expiration')
            source=sig.get((lid,known_ts))
            if source is None:raise RuntimeError(f'{symbol} {lid} {known_ts}: missing V2.2 CROSS_UP')
            source_seen.add((lid,known_ts))
            if i<1:raise RuntimeError('No prebar')
            atr_pre=_safe_float(source['atr_pre'])
            if atr_pre is None or atr_pre<=0:
                # independent V2.1 formation ATR remains available; be explicit
                atr_pre=atr0
                atr_source='FORMATION_FALLBACK'
            else:atr_source='V22_PRE14'
            before='ABOVE' if cl[i-1]>bandhi else ('BELOW' if cl[i-1]<bandlo else 'INSIDE')
            if before=='ABOVE' or cl[i]<=bandhi:raise RuntimeError('V21 CROSS semantics mismatch')
            pre=_prior_touch_features(ts,known_ts,evs,prev_att,atr_pre)
            pres=_pressure_features(i,ot,hi,lo,cl,bandhi,atr_pre,low_pivots,rules)
            opi=float(op[i]); rng=float(hi[i]-lo[i]);body=abs(float(cl[i])-opi)
            prev_vol=source.get('vol_ratio','')
            aid=_digest(VERSION,source['event_id'])
            attempt=dict(attempt_id=aid,source_event_id=source['event_id'],level_id=lid,
                symbol=symbol,origin='HIGH',stage='RESISTANCE_CROSS_UP',
                known_closed_ts=known_ts,known_utc=_iso(known_ts),
                formation_closed_ts=_int(level['formation_closed_ts']),
                level_age_bars=int((known_ts-int(level['formation_closed_ts']))//TF),
                formation_pivots=_int(level['confirmation_count']),
                pivot_separation_bars=_int(level['pivot_separation_bars']),
                zone_low=bandlo,zone_high=bandhi,center=center,zone_width_atr=_round((bandhi-bandlo)/atr0),
                zone_width_pct=_round(100*(bandhi-bandlo)/center),
                zone_quality_flag=level['quality_flag'],
                prior_relation=before,close_relation='ABOVE',
                breakout_open=opi,breakout_high=float(hi[i]),breakout_low=float(lo[i]),
                breakout_close=float(cl[i]),
                breakout_body_atr=_round(body/atr_pre),breakout_range_atr=_round(rng/atr_pre),
                breakout_body_pct=_round(100*body/rng) if rng>0 else '',
                breakout_close_location=_round((cl[i]-lo[i])/rng) if rng>0 else '',
                breakout_upper_wick_atr=_round((hi[i]-max(opi,cl[i]))/atr_pre),
                breakout_lower_wick_atr=_round((min(opi,cl[i])-lo[i])/atr_pre),
                breakout_close_beyond_high_atr=_round((cl[i]-bandhi)/atr_pre),
                atr_pre=atr_pre,atr_reference=atr_source,
                volume_ratio_pre20=prev_vol,volume_source=source.get('volume_source',''),
                vol_pre5_20_ratio=source.get('vol_pre5_20_ratio',''),
                next_resistance_distance_atr=_new_zone_distance(level,forms,end_by_level,known_ts,atr_pre),
                cluster_symbol_closed=f'{symbol}|{known_ts}',
                **pre,**pres)
            attempts.append(attempt)
            result,tx=classify_followup(known_ts,i,ot,lo,hi,cl,bandlo,bandhi,atr0,rules)
            for t in tx:t.update(attempt_id=aid,level_id=lid,symbol=symbol)
            transitions.append(dict(attempt_id=aid,level_id=lid,symbol=symbol,
                                    transition='CROSS_UP',known_closed_ts=known_ts,
                                    known_utc=_iso(known_ts),note='initial crossing close'))
            transitions.extend(tx)
            outcomes.append(dict(attempt_id=aid,level_id=lid,symbol=symbol,
                breakout_closed_ts=known_ts,**result,
                has_pressure=int(pres['pressure_build_up']),
                n_prior_touches=pre['prior_independent_touches']))
            prev_att.append(dict(status=result['status'],failed_closed_ts=result['failed_closed_ts']))
    if source_seen!=set(sig):
        missing=set(sig)-source_seen
        raise RuntimeError(f'{symbol}: V22 CROSS_UP not mapped to V21: {len(missing)}')
    return level_records,touch_records,attempts,transitions,outcomes


def _cols(rows):
    if not rows:return []
    c=list(rows[0]); more=set().union(*(set(r) for r in rows))
    return c+sorted(more-set(c))


def main(argv=None):
    p=argparse.ArgumentParser(description='Frozen V2.1→V2.6 resistance structure lab. Offline/read-only inputs.')
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true');m.add_argument('--run',action='store_true')
    p.add_argument('--atlas-run',type=Path,default=DEFAULT_ATLAS)
    p.add_argument('--signal-run',type=Path,default=DEFAULT_SIGNAL)
    p.add_argument('--candles-dir',type=Path,default=DEFAULT_CANDLES)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUT)
    p.add_argument('--max-symbols',type=int,default=0,help='For explicit development pilots only')
    p.add_argument('--min-free-gb',type=float,default=.5)
    a=p.parse_args(argv)
    if a.output_dir.resolve().is_relative_to(a.atlas_run.resolve()) or a.output_dir.resolve().is_relative_to(a.signal_run.resolve()) or a.output_dir.resolve().is_relative_to(a.candles_dir.resolve()):
        raise SystemExit('Output cannot reside in frozen input directories')
    if a.output_dir.resolve().exists() and a.run and any(a.output_dir.iterdir()):
        raise SystemExit(f'Output already exists and is non-empty (no overwrite): {a.output_dir}')
    for f in [a.atlas_run/'run_config.json',a.atlas_run/'audit.json',a.signal_run/'freeze.json',a.signal_run/'signals.csv']:
        if not f.exists():raise SystemExit(f'Missing frozen input: {f}')
    conf=json.loads((a.atlas_run/'run_config.json').read_text())
    freeze=json.loads((a.signal_run/'freeze.json').read_text())
    symbols=list(conf['symbols'])
    if a.max_symbols>0: symbols=symbols[:a.max_symbols]
    if freeze['signals_sha256'] != _sha_file(a.signal_run/'signals.csv'):
        raise SystemExit('FATAL: V2.2 signals do not match frozen SHA')
    if not set(symbols).issubset(set(freeze['symbols'])):
        raise SystemExit('FATAL: symbols not present in frozen V2.2 source')
    for sym in symbols:
        for path in [a.candles_dir/f'{sym}_30m.csv',*(a.atlas_run/'shards'/f'{sym}.{s}.csv'
                                                         for s in ['formation','events','ends'])]:
            if not path.is_file():raise SystemExit(f'Missing {path}')
    print('HLL_V26_PLAN',json.dumps(dict(symbols=len(symbols),atlas=str(a.atlas_run),
          source_events_total=sum(1 for _ in open(a.signal_run/'signals.csv',encoding='utf-8'))-1,
          signals_sha256=freeze['signals_sha256'],out=str(a.output_dir),
          rules=asdict(Rules()),phase='STRUCTURES_ONLY_NO_TP_SL_NO_MARKET_FLOW'),sort_keys=True),flush=True)
    if a.plan:return 0
    if shutil.disk_usage(Path.cwd()).free < a.min_free_gb*1024**3:
        raise SystemExit(f'Insufficient disk free: need {a.min_free_gb} GiB')
    data=pd.read_csv(a.signal_run/'signals.csv',keep_default_na=False)
    src=data[['event_id','symbol','level_id','known_closed_ts','stage','atr_pre','vol_ratio',
              'volume_source','vol_pre5_20_ratio']].to_dict('records')
    all_levels=[];all_touches=[];all_attempts=[];all_tx=[];all_outcomes=[]
    for i,sym in enumerate(symbols,1):
        l,t,b,x,o=run_symbol(sym,a.atlas_run,a.candles_dir,src)
        all_levels+=l;all_touches+=t;all_attempts+=b;all_tx+=x;all_outcomes+=o
        print('HLL_V26_SYMBOL',json.dumps(dict(index=i,symbol=sym,levels=len(l),
                 touches=len(t),attempts=len(b),outcomes=Counter(r['status'] for r in o))),flush=True)
    if len({r['attempt_id'] for r in all_attempts})!=len(all_attempts):
        raise SystemExit('Duplicate attempt_ids')
    if len(all_attempts)!=len(data[(data.stage=='RESISTANCE_CROSS_UP') & data.symbol.isin(symbols)]):
        raise SystemExit('V2.2 CROSS_UP coverage mismatch')
    # Never use retrospective outcome to compute entry features. Summary is
    # separately labeled OBSERVED and based only on complete/censored outcomes.
    summ=[]
    for status,n in sorted(Counter(x['status'] for x in all_outcomes).items()):
        grp=[x for x in all_outcomes if x['status']==status]
        summ.append(dict(dimension='OUTCOME',label=status,n=n,
                         symbols=len(set(x['symbol'] for x in grp))))
    for t in sorted(set(x['prior_independent_touches'] for x in all_attempts)):
        group=[x for x in all_attempts if x['prior_independent_touches']==t]
        summ.append(dict(dimension='PRIOR_TOUCHES',label=str(t),n=len(group),
                         symbols=len(set(x['symbol'] for x in group))))
    for pval in (0,1):
        grp=[x for x in all_attempts if x['pressure_build_up']==pval]
        summ.append(dict(dimension='PRESSURE_BEFORE_CROSS',label=str(pval),n=len(grp),
                         symbols=len(set(x['symbol'] for x in grp))))
    # Exploratory contingency tables. Outcome columns are explicitly separate
    # from the initial causal feature records; do NOT filter trades on them.
    outcome_map={x['attempt_id']:x for x in all_outcomes}
    cross=defaultdict(list)
    for feat in all_attempts:
        outc=outcome_map[feat['attempt_id']]
        touch_n=feat['prior_independent_touches']
        groups={
            'PRIOR_TOUCHES': str(touch_n) if touch_n<5 else '5+',
            'PRIOR_REJECTIONS': str(feat['prior_confirmed_rejections']) if feat['prior_confirmed_rejections']<3 else '3+',
            'PRIOR_FAILED_BREAKOUTS': '0' if feat['prior_failed_breakouts']==0 else '1+',
            'PRESSURE_BUILDUP': str(feat['pressure_build_up']),
            'APPROACH_ORIGIN': feat['prior_relation'],
            'ZONE_WIDTH_PCT': '<=0.5' if feat['zone_width_pct']<=.5 else ('0.5-1' if feat['zone_width_pct']<=1 else '>1'),
            'AGE_BARS': '<=32' if feat['level_age_bars']<=32 else ('33-80' if feat['level_age_bars']<=80 else '81+'),
        }
        vol=_safe_float(feat['volume_ratio_pre20'])
        groups['VOLUME_RATIO_PRE20']=('UNAVAILABLE' if vol is None else
                '<1' if vol<1 else '1-2' if vol<2 else '>=2')
        for dim,label in groups.items():cross[(dim,label)].append((feat,outc))
    cross_rows=[]
    for (dimension,label),pairs in sorted(cross.items()):
        n=len(pairs)
        c=Counter(out['status'] for _,out in pairs)
        accepted=sum(bool(out['accepted_closed_ts']) for _,out in pairs)
        retest=sum(bool(out['retest_closed_ts']) for _,out in pairs)
        flipped=sum(bool(out['defense_closed_ts']) for _,out in pairs)
        sustained=sum(bool(out['flip_sustained_closed_ts']) for _,out in pairs)
        censored=sum(out['status'].startswith('CENSORED_') or
                     out['status'].endswith('_CENSORED') for _,out in pairs)
        cross_rows.append(dict(dimension=dimension,label=label,n=n,
             symbols=len(set(f['symbol'] for f,_ in pairs)),
             accepted=accepted,accepted_pct=_round(100*accepted/n,3),
             retest=retest,retest_pct=_round(100*retest/n,3),
             flip_confirmed=flipped,flip_confirmed_pct=_round(100*flipped/n,3),
             flip_sustained=sustained,flip_sustained_pct=_round(100*sustained/n,3),
             early_failed=c.get('FAILED_BREAKOUT',0),
             direct_no_retest=c.get('DIRECT_NO_RETEST_24B',0),
             retest_failed=c.get('RETEST_FAILED',0),
             censored=censored,
             note='EXPLORATORY RETROSPECTIVE, NOT AN ENTRY-TIME FEATURE OR PROFITABILITY TEST'))
    output=a.output_dir
    for name,rows in [('levels_features.csv',all_levels),('touches.csv',all_touches),
                      ('breakout_attempts.csv',all_attempts),('structure_transitions.csv',all_tx),
                      ('attempt_outcomes.csv',all_outcomes),('structure_summary.csv',summ),
                      ('structure_by_prior_features.csv',cross_rows)]:
        _atomic_csv(output/name,rows,_cols(rows))
    stats=Counter(x['status'] for x in all_outcomes)
    manifest=dict(version=VERSION,generated_at_utc=datetime.now(timezone.utc).isoformat(),
         symbols=symbols,levels=len(all_levels),touches=len(all_touches),
         attempts=len(all_attempts),source_signal_sha256=freeze['signals_sha256'],
         frozen_atlas_sha256=_sha_file(a.atlas_run/'run_config.json'),
         frozen_signals_sha256=_sha_file(a.signal_run/'signals.csv'),
         rules=asdict(Rules()),statuses=dict(stats),
         interpretation='Retrospective structural states are NOT entry-known features. No PnL, no live orders, no future-based trade selection.',
         limitations=['Only V2.1 confirmed HIGH levels, max age 160 bars',
          'CROSS_UP attempt episodes may overlap and share a price path',
          'Atlas formation pivot raw prices are not archived; formation spread blank',
          'Contact means physical wick overlap, independent TEST is an extra property',
          'No causal significance of touch counts or pressure has been established'])
    _atomic_json(output/'manifest.json',manifest)
    print('HLL_V26_DONE',json.dumps(dict(output=str(output),levels=len(all_levels),
             touches=len(all_touches),attempts=len(all_attempts),statuses=dict(stats))),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
