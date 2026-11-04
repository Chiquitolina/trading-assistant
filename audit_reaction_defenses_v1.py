#!/usr/bin/env python3
"""Read-only, offline audit of original Reaction Lab FIRST TOUCH labels.

- Replays original app.py swing confirmation/first-touch functions through the
  HLL research adapter on hash-verified 1m tapes. Does NOT modify live scanner.
- Reuses the original app.py forward MFE/MAE attachment (all touch statuses).
- Separately measures future swing invalidation and chronological races.
- Does NOT claim parity with the root online scanner (requires audit A).
- All outcomes are RETROSPECTIVE, never causal PRE features.
"""
from __future__ import annotations
import argparse
import ast
import copy
import hashlib
import importlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TF1 = 60_000
HORIZONS = (15, 30, 60)
FAVORABLE = (0.3, 0.5, 1.0)
STATES = ('REACTION', 'TOUCH_FAILED', 'TOUCH_INDECISIVE')


def utc_ms(s: str) -> int:
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        raise ValueError('UTC cutoff must include timezone; example 2026-09-17T14:00:00Z')
    return int(ts.tz_convert('UTC').timestamp() * 1000)


def file_sha256(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for c in iter(lambda:f.read(1024*1024),b''):
            h.update(c)
    return h.hexdigest()


def compile_original_forward(app_path: Path):
    """Load exact forward excursion helpers by AST, no Streamlit app execution."""
    tree=ast.parse(app_path.read_text(encoding='utf-8-sig'),filename=str(app_path))
    found={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
    names=['_prepare_confirmed_swing_retest_candles',
           '_build_confirmed_swing_retest_forward_cache',
           '_attach_confirmed_swing_reaction_run']
    missing=set(names)-set(found)
    if missing:raise ValueError('Original app.py missing forward functions: '+str(sorted(missing)))
    nodes=[]
    for n in names:
        node=copy.deepcopy(found[n]);node.decorator_list=[];nodes.append(node)
    module=ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[]))
    env={'np':np,'pd':pd}
    exec(compile(module,str(app_path),'exec'),env)
    return env['_build_confirmed_swing_retest_forward_cache'],env['_attach_confirmed_swing_reaction_run']


def chronological_races(bars, side, baseline, swing, threshold_pct):
    """First favorable target vs first WICK-beyond-swing; no intra-1m guessing."""
    favorable_level=baseline*(1+threshold_pct/100 if side=='LONG' else 1-threshold_pct/100)
    for offset,bar in enumerate(bars,1):
        high=float(bar['high']); low=float(bar['low'])
        favorable=(high>=favorable_level if side=='LONG' else low<=favorable_level)
        invalid=(low<swing if side=='LONG' else high>swing)
        if favorable and invalid:return 'AMBIGUOUS_1M',offset
        if favorable:return 'FAVORABLE_FIRST',offset
        if invalid:return 'INVALIDATION_FIRST',offset
    return 'NEITHER',None


def check_path(tape: pd.DataFrame, event: dict, horizon: int):
    """Audit ONLY closed 1m candles after touch; never consume the touch candle.

    Returns CENSORED_GAP / CENSORED_TAIL if the requested whole horizon is
    not observed contiguously. No unobserved event counts as failure or survival.
    """
    retest_ts=int(event['retest_ts'])
    start=retest_ts+TF1
    ts=tape['timestamp'].to_numpy(dtype=np.int64)
    i=int(np.searchsorted(ts,start))
    if i>=len(ts):return {'coverage':'CENSORED_TAIL','observed_bars':0}
    if int(ts[i])!=start:return {'coverage':'CENSORED_GAP','observed_bars':0}
    stop=min(i+horizon,len(ts))
    end=i
    while end<stop and int(ts[end])==start+(end-i)*TF1:
        end+=1
    bars=tape.iloc[i:end]
    full=(end-i)==horizon
    coverage='COMPLETE' if full else ('CENSORED_GAP' if end<len(ts) else 'CENSORED_TAIL')
    result={'coverage':coverage,'observed_bars':len(bars)}
    if not full:return result
    side=str(event['side']);swing=float(event['swing_price']);baseline=float(event['retest_close'])
    high=bars['high'].to_numpy(dtype=float);low=bars['low'].to_numpy(dtype=float);close=bars['close'].to_numpy(dtype=float)
    if side=='LONG':
        wick=np.flatnonzero(low<swing)
        crossed=np.flatnonzero(close<swing)
        crossed_band=np.flatnonzero(close<swing*(1-0.10/100))
    else:
        wick=np.flatnonzero(high>swing)
        crossed=np.flatnonzero(close>swing)
        crossed_band=np.flatnonzero(close>swing*(1+0.10/100))
    for name,idx in [('wick_beyond_swing',wick),('close_beyond_swing',crossed),('close_beyond_band_0_10pct',crossed_band)]:
        result[name]=bool(len(idx))
        result[name+'_first_bar']=int(idx[0])+1 if len(idx) else None
    for t in FAVORABLE:
        key=f'{t:.1f}'.replace('.','_')
        race,first_bar=chronological_races(bars.to_dict('records'),side,baseline,swing,t)
        result['race_'+key]=race
        result['race_'+key+'_first_bar']=first_bar
        result['favorable_plus_'+key]=bool(race in ('FAVORABLE_FIRST','AMBIGUOUS_1M') or (
           side=='LONG' and np.any(high>=baseline*(1+t/100)) or
           side=='SHORT' and np.any(low<=baseline*(1-t/100))))
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description='Offline HLL/Reaction first-touch post-outcome audit; READ ONLY')
    p.add_argument('--root',required=True,help='Project root containing dashboard/app.py and HLL minutes/manifests')
    p.add_argument('--symbol',required=True)
    p.add_argument('--pre-cutoff-utc',required=True,help='E.g. 2026-09-17T14:00:00Z')
    p.add_argument('--pre-candles',type=int,default=96,choices=(48,96,144,192,288,384))
    p.add_argument('--out',required=True,help='Output directory, NEVER inside existing live reports')
    p.add_argument('--expected-reactions',type=int,default=None)
    a=p.parse_args(argv)
    root=Path(a.root).resolve();out=Path(a.out).resolve();sym=a.symbol.upper()
    if not sym.replace('USDT','').isalnum() or '/' in sym or '..' in sym:
        raise ValueError('Unsafe symbol name')
    cutoff=utc_ms(a.pre_cutoff_utc)
    app=root/'dashboard/app.py'
    if not app.is_file():raise FileNotFoundError(app)
    sys.path.insert(0,str(root))
    overlay=importlib.import_module('dashboard.views.hll_reaction_pre_overlay')
    tape_file=root/overlay.MINUTE_DIR/(sym+'_1m.csv')
    manifest_file=root/overlay.MANIFEST_DIR/(sym+'.json')
    tape=overlay.load_verified_minute(tape_file,manifest_file)
    env,swing_class,source_hash=overlay.canonical_functions(str(app),app.stat().st_mtime_ns)
    events=overlay.replay_canonical_pre(
      tape,cutoff,cutoff-a.pre_candles*overlay.TF30,
      env=env,swing_detector_class=swing_class,symbol=sym,
      left_bars=3,right_bars=3,min_prominence_pct=0.0,include_first_retests=True)
    if not events:raise RuntimeError('No first touches; check coverage and exact PRE cutoff')
    seen_unique_reactions={r['event_key'] for r in events if r['status']=='REACTION'}
    # HLL overlay deduplicates REACTION by event_key; first-touch baselines
    # remain pivot-level candidates. Keep both denominators explicit.
    if a.expected_reactions is not None and len(seen_unique_reactions)!=a.expected_reactions:
        raise RuntimeError(f'BASELINE_MISMATCH: expected {a.expected_reactions} distinct REACTION event_keys, got {len(seen_unique_reactions)}. Stop; audit source/cutoff before interpreting outcomes.')
    original_cache,original_forward=compile_original_forward(app)
    forward_cache=original_cache(tape)
    if not forward_cache:raise RuntimeError('Original forward cache could not be built')
    rows=[]
    for e in events:
        if e['status'] not in STATES:raise ValueError('Unexpected first-touch classification: '+str(e['status']))
        r=dict(e)
        raw_original={'retest_timestamp':int(e['retest_ts']),
                      'retest_close':float(e['retest_close']),
                      'swing_price':float(e['swing_price']),
                      'signal':e['side'],'status':e['status'],
                      'reaction':e['status']=='REACTION'}
        forward=original_forward(raw_original,forward_cache)
        r['pivot_touch_id']=f"{sym}|{e['side']}|{e['pivot_ts']}|{e['retest_ts']}"
        r['original_forward_entry_baseline']='RETEST_CLOSE; NEXT_1M_BARS_ONLY'
        side=e['side']; swing=float(e['swing_price']); close=float(e['retest_close'])
        r['swing_held_at_t0_close']=(close>=swing if side=='LONG' else close<=swing)
        r['swing_margin_at_t0_pct']=round((close/swing-1)*100*(1 if side=='LONG' else -1),8)
        r['confirmation_to_touch_min']=(int(e['known_ts'])-int(e['actionable_ts']))/60_000.0
        retest_candle=tape[tape['timestamp']==int(e['retest_ts'])]
        if len(retest_candle)!=1:raise RuntimeError('Retest candle missing or duplicated')
        k=retest_candle.iloc[0]
        range_=float(k['high'])-float(k['low'])
        r['retest_close_strength_at_t0']=(0.5 if range_<=0 else
          (float(k['close'])-float(k['low']))/range_ if side=='LONG' else
          (float(k['high'])-float(k['close']))/range_)
        for horizon in HORIZONS:
            prefix=f'{horizon}m_'
            path=check_path(tape,e,horizon)
            r.update({prefix+k:v for k,v in path.items()})
            r[prefix+'orig_complete']=bool(forward[f'touch_complete_{horizon}m'])
            for name in ('mfe','mae','return'):
                val=forward[f'touch_{name}_{horizon}m_pct']
                r[prefix+name+'_pct']=float(val) if pd.notna(val) else None
            if r[prefix+'orig_complete'] != (path['coverage']=='COMPLETE'):
                raise AssertionError(f'ORIGINAL_COVERAGE_DISAGREEMENT {r["pivot_touch_id"]} {horizon}m')
        rows.append(r)
    out.mkdir(parents=True,exist_ok=True)
    df=pd.DataFrame(rows)
    event_file=out/f'{sym}_first_touches_defense_audit.csv'
    df.to_csv(event_file,index=False)
    summary=[]
    for status in STATES:
        sub=df[df.status==status]
        for horizon in HORIZONS:
            prefix=f'{horizon}m_';complete=sub[sub[prefix+'coverage']=='COMPLETE']
            row={'symbol':sym,'status':status,'horizon_min':horizon,
                 'pivot_touch_candidates':len(sub),'complete':len(complete),
                 'censored_gap':int((sub[prefix+'coverage']=='CENSORED_GAP').sum()),
                 'censored_tail':int((sub[prefix+'coverage']=='CENSORED_TAIL').sum()),
                 'coverage_pct':round(100*len(complete)/len(sub),2) if len(sub) else None}
            for field in ('wick_beyond_swing','close_beyond_swing','close_beyond_band_0_10pct',
                          'favorable_plus_0_3','favorable_plus_0_5','favorable_plus_1_0'):
                # Censored events are NOT used as negatives.
                row[field+'_pct']=round(100*complete[prefix+field].astype(float).mean(),2) if len(complete) else None
            for field in ('mfe_pct','mae_pct','return_pct'):
                row['median_'+field]=round(float(complete[prefix+field].median()),5) if len(complete) else None
            for t in FAVORABLE:
                key=f'{t:.1f}'.replace('.','_')
                for outcome in ('FAVORABLE_FIRST','INVALIDATION_FIRST','AMBIGUOUS_1M','NEITHER'):
                    row[f'first_{key}_{outcome.lower()}_pct']=round(100*float((complete[prefix+'race_'+key]==outcome).mean()),2) if len(complete) else None
            summary.append(row)
    summary_file=out/f'{sym}_summary.csv';pd.DataFrame(summary).to_csv(summary_file,index=False)
    # Exploratory 30m profiling: these features existed at the retest close.
    # This is DESCRIPTIVE, not a trained predictor. No features from future.
    profile=[]
    complete_30=df[(df.status=='REACTION') & (df['30m_coverage']=='COMPLETE')].copy()
    if not complete_30.empty:
        complete_30['defense_30m'] = np.where(complete_30['30m_wick_beyond_swing'],'LOST_WICK','HELD_WICK')
        known_features=('prominence_pct','max_departure_pct','penetration_pct',
                        'confirmation_to_touch_min','swing_margin_at_t0_pct',
                        'retest_close_strength_at_t0')
        for name,group in complete_30.groupby('defense_30m',sort=True):
            row={'symbol':sym,'group':name,'n':len(group)}
            for feature in known_features:
                row['median_'+feature]=round(float(group[feature].median()),6)
            profile.append(row)
    profile_file=out/f'{sym}_reaction_profile_30m.csv'
    pd.DataFrame(profile,columns=['symbol','group','n']+['median_'+x for x in
        ('prominence_pct','max_departure_pct','penetration_pct','confirmation_to_touch_min',
         'swing_margin_at_t0_pct','retest_close_strength_at_t0')]).to_csv(profile_file,index=False)
    metadata={'audit_version':'reaction_defenses_v1','symbol':sym,
       'pre_cutoff_utc':pd.Timestamp(cutoff,unit='ms',tz='UTC').isoformat(),
       'pre_candles_30m':a.pre_candles,
       'source_app_sha256':file_sha256(app),
       'source_overlay_sha256':file_sha256(Path(overlay.__file__)),
       'source_swing_sha256':file_sha256(root/'engine/live/research/swing_detector.py'),
       'one_minute_sha256':file_sha256(tape_file),'manifest_sha256':file_sha256(manifest_file),
       'historical_replay_note':'Original confirmation and first-touch functions through HLL overlay; NOT YET root scanner parity validated',
       'future_outcomes_note':'RETROSPECTIVE; NEVER feed results back as PRE causal features',
       'swing_shape':'3x3','prominence_pct':0.0,'min_departure_pct':0.20,
       'retest_tolerance_pct':0.10,'max_age_minutes':360,
       'pivot_first_touch_rows':len(df),
       'pivot_first_touch_by_status':dict(Counter(df.status)),
       'unique_reaction_event_keys':len(seen_unique_reactions),
       'duplicate_reaction_pivots':int(len(df[df.status=='REACTION'])-len(seen_unique_reactions)),
       'horizon_minutes':list(HORIZONS),'favorable_thresholds_pct':list(FAVORABLE),
       'denominator_warning':'All rates pivot-level with COMPLETE horizon; multiple swings can share a retest timestamp, not independent observations',
       'invalidations':'Wick strict beyond swing; 1m close strict beyond swing; 1m close outside +/-0.10% zone',
       'race_warning':'Wick-favorable + wick-invalidation same 1m candle => AMBIGUOUS_1M',
       'baseline_warning':'Use retest CLOSE for observational excursion, not an executable fill; original order research uses NEXT 1m OPEN',
       'generated_utc':datetime.now(timezone.utc).isoformat(),
       'git_head':subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True,check=False).stdout.strip() or None,
       'files':[str(event_file),str(summary_file),str(profile_file)]}
    (out/f'{sym}_manifest.json').write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding='utf-8')
    print('AUDIT_REACTION_DEFENSES '+json.dumps({k:metadata[k] for k in ('symbol','pivot_first_touch_rows','pivot_first_touch_by_status','unique_reaction_event_keys','duplicate_reaction_pivots')},ensure_ascii=False))
    print('OUTPUT',event_file,summary_file,profile_file)


if __name__=='__main__':main()
