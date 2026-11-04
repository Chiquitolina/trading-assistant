#!/usr/bin/env python3
"""READ-ONLY experimental direct swing 15m / 30m / 1h & causal 15m HTF context.

Same confirmed 3x3 swing detector and first-retest classification from dashboard/app.py,
with 1m departure/retest; original 15m events must exactly match HLL overlay.
All signals/context have a known timestamp <= PRE cutoff. POST is only for
retrospective outcome evaluation (30/60m). No HLL strategy or LIVE changes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

import compare_reaction_confirmations_v1 as v1
import audit_reaction_bidirectional_breakout_v2 as bi
from audit_reaction_defenses_v1 import file_sha256

VERSION = 'REACTION_MULTITF_SWING_AUDIT_V1'
TF_MIN = {'15m': 15, '30m': 30, '1h': 60}
SWING_LEFT = SWING_RIGHT = 3
CONTEXT_MAX_AGE_MIN = 1440
# At least context age + (3 left + pivot + 3 right) 1h candles + safety.
# Configured dynamically, 32h at 24h context; avoids silent truncation of HTF pivots.
CONTEXT_BAND_PCT = 0.10
ALIGNMENT_BANDS_PCT = (0.10, 0.20)
STATUS_ORDER = ('REACTION', 'TOUCH_INDECISIVE', 'TOUCH_FAILED')
SIDES = ('LONG', 'SHORT')


def aggregate_closed_1m(prefix: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Exact UTC-aligned OHLC, fail on partial/incomplete candle intervals."""
    if tf not in TF_MIN:
        raise ValueError('Unsupported timeframe')
    minutes = TF_MIN[tf]
    frame_ms = minutes * v1.TF1
    if prefix.empty or len(prefix) % minutes or int(prefix.timestamp.iloc[0]) % frame_ms:
        raise ValueError('Incomplete/nonaligned 1m prefix for '+tf)
    ts = prefix.timestamp.to_numpy(dtype=np.int64)
    if (np.diff(ts) != v1.TF1).any() or ((ts % v1.TF1) != 0).any():
        raise ValueError('Discontinuous 1m candles')
    grouped = prefix.assign(group=(prefix.timestamp // frame_ms) * frame_ms).groupby('group', sort=True)
    out = grouped.agg(timestamp=('timestamp', 'min'),open=('open','first'),
                      high=('high','max'),low=('low','min'),close=('close','last'),
                      count=('timestamp','size')).reset_index(drop=True)
    if out['count'].ne(minutes).any() or (np.diff(out.timestamp.to_numpy(dtype=np.int64)) != frame_ms).any():
        raise ValueError('Partial HTF candle or timeframe gap: '+tf)
    return out.drop(columns='count')


def build_confirmed_points(candles, tf, env, swing_class, pre_ms):
    """Use original SwingDetector + original confirmation helper, not a new detector."""
    if tf not in TF_MIN:
        raise ValueError(tf)
    detector = swing_class(left_bars=3,right_bars=3,min_prominence_pct=0.0)
    points = detector.detect_all(candles.to_dict('records'))
    get_conf = env['get_volume_exhaustion_swing_confirmation_info']
    result = []
    for point in points:
        info = get_conf(point=point, timeframe_candles=candles, swing_timeframe=tf)
        if info is None:
            continue
        actionable = int(info['actionable_timestamp'])
        # Strictly prior to cutoff for a swing that could have produced a PRE retest.
        if actionable >= pre_ms:
            continue
        side = 'LONG' if point.side == 'LOW' else 'SHORT' if point.side == 'HIGH' else None
        if side is None:
            continue
        if actionable != int(point.confirmed_timestamp) + TF_MIN[tf]*v1.TF1:
            raise AssertionError('Canonical actionable timestamp mismatch')
        result.append({'timeframe':tf,'detector':'3x3','side':side,
                       'pivot_ts':int(point.pivot_timestamp),'confirmed_candle_ts':int(point.confirmed_timestamp),
                       'actionable_ts':actionable,'swing_price':float(point.price),
                       'confirmation_close':float(info['confirmation_close']),
                       'pivot_to_confirmation_pct':float(info['move_pct']),
                       'prominence_pct':float(point.prominence_pct)})
    return sorted(result,key=lambda p:(p['actionable_ts'],p['side'],p['pivot_ts']))


def direct_first_retests(prefix, first_visible_ms, pre_ms, symbol, tf, points, env):
    """Canonical first retest per swing, no manual reclassification or future candle."""
    scan = env['_find_confirmed_swing_retest']
    events=[]
    for point in points:
        row={'timeframe':tf,'detector':'3x3','signal':point['side'],
             'pivot_timestamp':point['pivot_ts'], 'actionable_timestamp':point['actionable_ts'],
             'pivot_price':point['swing_price'], 'entry_price':point['confirmation_close'],
             'pivot_to_confirmation_pct':point['pivot_to_confirmation_pct'],
             'prominence_pct':point['prominence_pct']}
        touch=scan(prefix,row,0.10,0.20,360)
        if touch is None:
            continue
        known=int(touch['retest_timestamp'])+v1.TF1
        if not first_visible_ms <= known <= pre_ms:
            continue
        side=point['side'];retest=int(touch['retest_timestamp'])
        canonical_key=f'{symbol}|{side}|{retest}'
        unique_key=f'{symbol}|{tf}|{side}|{point["pivot_ts"]}|{retest}'
        events.append({'symbol':symbol,'swing_timeframe':tf,'swing_shape':'3x3',
                       'event_key':canonical_key if tf=='15m' else unique_key,
                       'pivot_touch_id':unique_key,'canonical_event_key':canonical_key,
                       'side':side,'status':str(touch['status']),
                       'pivot_ts':point['pivot_ts'],'swing_price':point['swing_price'],
                       'actionable_ts':point['actionable_ts'],'departure_ts':int(touch['departure_timestamp']),
                       'retest_ts':retest,'known_ts':known,'retest_close':float(touch['retest_close']),
                       'retest_price':float(touch['retest_price']),
                       'max_departure_pct':float(touch['max_departure_pct']),
                       'penetration_pct':float(touch['penetration_pct']),
                       'prominence_pct':point['prominence_pct'],
                       'source':'CANONICAL_FUNCTIONS_REPLAY_RESEARCH_'+tf})
    keys=[e['pivot_touch_id'] for e in events]
    if len(keys)!=len(set(keys)):
        raise AssertionError('Duplicate timeframe + pivot + first retest identity')
    return sorted(events,key=lambda e:(e['known_ts'],e['side'],e['pivot_ts']))


def assert_15m_parity(tape, first_visible_ms, pre_ms, symbol, env, swing_class, events, overlay):
    """Hard fail: 15m direct results must match EXACT original overlay event rows."""
    expected = overlay.replay_canonical_pre(tape,pre_ms,first_visible_ms,
             env=env,swing_detector_class=swing_class,symbol=symbol,
             left_bars=3,right_bars=3,min_prominence_pct=0.0,include_first_retests=True)
    fields=('side','pivot_ts','retest_ts','known_ts','status','swing_price','actionable_ts','departure_ts')
    def keyset(rows):
        return Counter(tuple(round(float(r[f]),12) if f=='swing_price' else r[f] for f in fields) for r in rows)
    a,b=keyset(expected),keyset(events)
    if a!=b:
        extra=list((b-a).elements())[:3];missing=list((a-b).elements())[:3]
        raise RuntimeError(f'15m baseline parity FAIL expected={len(expected)}, got={len(events)}; extra={extra}, missing={missing}')
    return len(expected),sum(r['status']=='REACTION' for r in expected)


def known_htf_context(tape, event, points, tf, max_age_min=CONTEXT_MAX_AGE_MIN):
    """Only HTF levels actionable by event's 1m CLOSE; no future-confirmed pivots.

    A supporting swing has the SAME side and has not closed through its own
    +/-0.10% band between its actionable timestamp and the 15m event known_ts.
    Returns unknown when contiguous coverage cannot establish the level state.
    """
    if tf not in ('30m','1h'):
        raise ValueError(tf)
    known=int(event['known_ts']);swing=float(event['swing_price']);side=event['side']
    candidates=[p for p in points if p['side']==side and
                int(p['actionable_ts']) <= known and
                0 <= known-int(p['actionable_ts']) <= max_age_min*v1.TF1]
    result={f'{tf}_context_status':'NO_CONFIRMED_CANDIDATE',
            f'{tf}_confirmed_candidates_n':len(candidates),
            f'{tf}_active_candidates_n':0,
            f'{tf}_nearest_active_swing_price':None,
            f'{tf}_nearest_distance_pct':None,
            f'{tf}_nearest_pivot_ts':None,
            f'{tf}_nearest_actionable_ts':None,
            f'{tf}_near_0_10_pct':None,
            f'{tf}_near_0_20_pct':None}
    if not candidates:return result
    active=[];incomplete=0
    for p in candidates:
        n=(known-int(p['actionable_ts']))//v1.TF1
        if (known-int(p['actionable_ts']))%v1.TF1:
            raise ValueError('Unaligned HTF actionable/15m event')
        if n:
            path,coverage=v1.observed_segment(tape,int(p['actionable_ts']),n)
            if coverage!='COMPLETE':
                incomplete+=1;continue
            threshold=float(p['swing_price'])*(1-CONTEXT_BAND_PCT/100 if side=='LONG'
                                             else 1+CONTEXT_BAND_PCT/100)
            broken=bool((path.close < threshold).any() if side=='LONG' else (path.close > threshold).any())
        else:broken=False
        if not broken:active.append(p)
    result[f'{tf}_active_candidates_n']=len(active)
    if incomplete:
        result[f'{tf}_context_status']='UNKNOWN_HISTORY'
        return result
    if not active:
        result[f'{tf}_context_status']='ALL_CANDIDATES_INVALIDATED'
        return result
    nearest=min(active,key=lambda p:(abs(float(p['swing_price'])/swing-1),-int(p['actionable_ts'])))
    dist=abs(float(nearest['swing_price'])/swing-1)*100
    result.update({f'{tf}_context_status':'ACTIVE',
            f'{tf}_nearest_active_swing_price':float(nearest['swing_price']),
            f'{tf}_nearest_distance_pct':dist,
            f'{tf}_nearest_pivot_ts':int(nearest['pivot_ts']),
            f'{tf}_nearest_actionable_ts':int(nearest['actionable_ts']),
            f'{tf}_near_0_10_pct':bool(dist<=0.10),
            f'{tf}_near_0_20_pct':bool(dist<=0.20)})
    return result


def summarize_direct(df: pd.DataFrame):
    records=[]
    for (tf,side,status),g in df.groupby(['swing_timeframe','original_side','original_status'],sort=True):
        row={'timeframe':tf,'side':side,'status':status,'first_touches_n':len(g),
             'unique_retest_minutes_n':int(g.retest_ts.nunique()),
             'observed_window_hours':float(g.observation_hours.iloc[0]),
             'retests_per_24h':round(len(g)*24/float(g.observation_hours.iloc[0]),3)}
        for h in (30,60):
            avail=g.loc[g[f'original_{h}m_coverage']=='COMPLETE']
            wins=int((avail[f'original_{h}m_race_0_5']=='FAVORABLE_FIRST').sum())
            wins_close=int((avail[f'original_{h}m_race_close_band_0_5']=='FAVORABLE_FIRST').sum())
            row.update({f'{h}m_complete_n':len(avail),f'{h}m_wick_wins_n':wins,
                        f'{h}m_wick_wins_pct':round(wins*100/len(avail),2) if len(avail) else None,
                        f'{h}m_close_band_wins_n':wins_close,
                        f'{h}m_close_band_wins_pct':round(wins_close*100/len(avail),2) if len(avail) else None})
        scan=g.loc[g.opposite_breakout_scan_coverage=='COMPLETE']
        confirmed=scan.loc[scan.opposite_breakout_confirmed==True]
        row.update({'opposite_scan_complete_n':len(scan),'opposite_breaks_n':len(confirmed),
                    'opposite_breaks_pct':round(len(confirmed)*100/len(scan),2) if len(scan) else None,
                    'opposite_30m_complete_n':int((confirmed.opposite_30m_coverage=='COMPLETE').sum())})
        records.append(row)
    return pd.DataFrame(records)


def summarize_htf(df: pd.DataFrame):
    out=[]
    if df.empty:return pd.DataFrame()
    for tf in ('30m','1h'):
        for (side,status),g in df.groupby(['original_side','original_status']):
            labels=[]
            for _,r in g.iterrows():
                x=r[f'{tf}_context_status']
                labels.append(('NEAR_ACTIVE_0.20' if bool(r[f'{tf}_near_0_20_pct']) else 'FAR_ACTIVE')
                              if x=='ACTIVE' else x)
            z=g.assign(htf_group=labels)
            for cls,s in z.groupby('htf_group'):
                avail=s.loc[s.original_30m_coverage=='COMPLETE']
                wins=int((avail.original_30m_race_0_5=='FAVORABLE_FIRST').sum())
                row={'context_timeframe':tf,'side':side,'status':status,'context_group':cls,
                     'first_touches_n':len(s),'30m_complete_n':len(avail),
                     '30m_wick_wins_n':wins,
                     '30m_wick_wins_pct':round(100*wins/len(avail),2) if len(avail) else None}
                out.append(row)
    return pd.DataFrame(out)


def run_audit(tape, pre_ms, first_visible_ms, symbol, env, swing_class, overlay,
              context_max_age_min=CONTEXT_MAX_AGE_MIN):
    if pre_ms% (60*v1.TF1) or first_visible_ms%(60*v1.TF1):
        raise ValueError('PRE/observation window must be UTC-aligned to 1h')
    if context_max_age_min not in (360,720,1440):
        raise ValueError('Unsupported HTF context maximum age')
    warmup_minutes=max(context_max_age_min,360)+7*60+60
    start=first_visible_ms-warmup_minutes*v1.TF1
    prefix=overlay.closed_prefix(tape,start,pre_ms)
    frames={tf:aggregate_closed_1m(prefix,tf) for tf in TF_MIN}
    points={tf:build_confirmed_points(frames[tf],tf,env,swing_class,pre_ms) for tf in TF_MIN}
    direct={tf:direct_first_retests(prefix,first_visible_ms,pre_ms,symbol,tf,points[tf],env) for tf in TF_MIN}
    parity=assert_15m_parity(tape,first_visible_ms,pre_ms,symbol,env,swing_class,direct['15m'],overlay)
    rows=[]
    for tf,events in direct.items():
        for e in events:
            r=bi.analyze_touch(tape,e,pre_ms)
            r['swing_timeframe']=tf
            r['observation_hours']=(pre_ms-first_visible_ms)/(60*v1.TF1)
            r['pivot_touch_id']=e['pivot_touch_id']
            r['canonical_event_key']=e['canonical_event_key']
            rows.append(r)
    if rows:
        df=pd.DataFrame(rows)
        if df.pivot_touch_id.duplicated().any():
            raise AssertionError('Duplicate event identity across timeframes')
        direct_summary=summarize_direct(df)
    else:
        df=pd.DataFrame();direct_summary=pd.DataFrame()
    context=[]
    for e in direct['15m']:
        r=next((x for x in rows if x['pivot_touch_id']==e['pivot_touch_id']),None)
        if r is None:raise AssertionError('Missing baseline 15m event')
        r=dict(r)
        for tf in ('30m','1h'):
            r.update(known_htf_context(tape,e,points[tf],tf,max_age_min=context_max_age_min))
        context.append(r)
    context_df=pd.DataFrame(context)
    context_summary=summarize_htf(context_df)
    return {'events':df,'summary':direct_summary,'context':context_df,
            'context_summary':context_summary,'parity':parity,
            'points':{tf:len(points[tf]) for tf in TF_MIN},
            'counts':{tf:Counter(e['status'] for e in direct[tf]) for tf in TF_MIN}}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True)
    p.add_argument('--symbol',required=True)
    p.add_argument('--pre-cutoff-utc',required=True)
    p.add_argument('--pre-candles',type=int,default=96,choices=(48,96,144,192,288,384))
    p.add_argument('--htf-context-max-age-min',type=int,default=1440,choices=(360,720,1440))
    p.add_argument('--expected-15m-first-touches',type=int)
    p.add_argument('--expected-15m-reactions',type=int)
    p.add_argument('--out',required=True)
    args=p.parse_args(argv)
    root=Path(args.root).resolve();symbol=''.join(c for c in args.symbol.upper() if c.isalnum())
    if not symbol or symbol!=args.symbol.upper():raise ValueError('Invalid symbol')
    out=Path(args.out).resolve();base=root/'reports/reaction_multitimeframe_audit_v1'
    if not out.is_relative_to(base) or out==base:
        raise ValueError('--out must be a separate directory under '+str(base))
    files={name:out/(symbol+'_'+suffix) for name,suffix in (
        ('events','multitf_events.csv'),('summary','multitf_summary.csv'),
        ('context','htf_context_15m_events.csv'),('context_summary','htf_context_summary.csv'),
        ('manifest','multitf_manifest.json'))}
    if any(path.exists() for path in files.values()):
        raise FileExistsError('Refusing to overwrite historical audit artifacts')
    if str(root) not in sys.path:sys.path.insert(0,str(root))
    overlay=importlib.import_module('dashboard.views.hll_reaction_pre_overlay')
    app=root/'dashboard/app.py';minute=root/overlay.MINUTE_DIR/(symbol+'_1m.csv')
    historical_manifest=root/overlay.MANIFEST_DIR/(symbol+'.json')
    tape=overlay.load_verified_minute(minute,historical_manifest)
    pre_ms=v1.parse_utc(args.pre_cutoff_utc)
    first_visible=pre_ms-args.pre_candles*overlay.TF30
    env,swing_class,app_hash=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
    result=run_audit(tape,pre_ms,first_visible,symbol,env,swing_class,overlay,
                     context_max_age_min=args.htf_context_max_age_min)
    n,reactions=result['parity']
    if (args.expected_15m_first_touches is not None and n!=args.expected_15m_first_touches) or (args.expected_15m_reactions is not None and reactions!=args.expected_15m_reactions):
        raise RuntimeError(f'15m expected baseline mismatch n={n} REACTIONs={reactions}; outputs not written')
    out.mkdir(parents=True,exist_ok=True)
    for name in ('events','summary','context','context_summary'):
        result[name].to_csv(files[name],index=False)
    dependencies=[minute,historical_manifest,app,root/'engine/live/research/swing_detector.py',
                  Path(overlay.__file__),root/'audit_reaction_bidirectional_breakout_v2.py',
                  root/'compare_reaction_confirmations_v1.py',root/'compare_first_touch_confirmations_v2.py']
    report={'version':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),
            'symbol':symbol,'pre_cutoff_utc':args.pre_cutoff_utc,'pre_candles_30m':args.pre_candles,
            'observation_hours':(pre_ms-first_visible)/(60*v1.TF1),
            'htf_context_max_age_min':args.htf_context_max_age_min,
            'tf_shapes':{tf:'3x3, prominence 0%' for tf in TF_MIN},
            'confirmed_points_available':result['points'],
            'first_touches_by_timeframe_status':{tf:dict(c) for tf,c in result['counts'].items()},
            '15m_overlay_exact_parity':{'first_touches':n,'reactions':reactions},
            'source_sha256':{str(f.relative_to(root)) if f.is_relative_to(root) else str(f):file_sha256(f) for f in dependencies},
            'auditor_sha256':file_sha256(Path(__file__).resolve()),
            'canonical_app_functions_sha256':app_hash,
            'git_head':subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True).stdout.strip(),
            'direct':'Canonical 3x3 15m/30m/1h confirmed swings; first retest 1m, departure .20%, tolerance .10%, max age 360m, labels untouched.',
            'context':'Only 30m/1h same-side pivots actionable by 15m event known_ts, age <= configured context max age, not closed beyond 0.10% from confirmation; distance to 15m swing (0.10/0.20%).',
            'outcomes':'Retrospective 30/60m from next OPEN 1m; +0.5% favorable before wick/close-band invalidation; opposite 2 closed 1m bars swing +/-0.10%; POST allowed only for outcome.',
            'caveats':'Research only, no statistical conclusion, no online scanner parity, no HLL-level parity; repeated events may share movement; no backtest of executable orders.',
            'outputs':{name:str(path) for name,path in files.items()},
            'output_sha256':{name:file_sha256(files[name]) for name in ('events','summary','context','context_summary')}}
    files['manifest'].write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf8')
    print('REACTION_MULTITF_AUDIT '+json.dumps({'symbol':symbol,'15m_overlay_parity':{'first_touches':n,'reactions':reactions},
        'confirmed_points':result['points'],'by_tf':{tf:dict(c) for tf,c in result['counts'].items()}}))
    if not result['summary'].empty:
        print(result['summary'][['timeframe','side','status','first_touches_n','30m_complete_n','30m_wick_wins_n','opposite_breaks_n']].to_string(index=False))
    print('OUTPUT',*[str(f) for f in files.values()])

if __name__=='__main__':main()
