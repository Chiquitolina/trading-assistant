#!/usr/bin/env python3
"""Structural Level Atlas V2: historical multi-zone map, independent of Compression V4.

Reads frozen Full Context and optional independently cached 15m/30m OHLCV.
Never looks at a retest candle, TP/SL or future path for PRE level discovery.
Records ALL confirmed 2-pivot frozen zones, nearest zones, favor-side and
opposing-side structural space; the retest close is a separate KNOWN label.
Only derived CSV/JSON under reaction_structural_level_atlas_v2 are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd

import reaction_structural_level_atlas as v1

VERSION = 'structural-level-atlas-v2.0'
OUTPUT = 'reaction_structural_level_atlas_v2'
# Freeze distance thresholds before reading outcomes; they are not optimized.
NEAR_ATR = 0.5
CONTEXT_ATR = 1.5
PLACEBO_SHIFT_ATR = 3.0

ZONE_COLS = [
    'event_key','symbol','side','date_utc','observation_type','retest_ts',
    'timeframe','history_source','coverage','zone_id','origin_kind','level_origin',
    'level_price','zone_low','zone_high','zone_atr_at_formation',
    'formation_closed_ts','pivot_count','halfwidth_pct',
    'level_state_pre','acceptance_last4','defenses_pre','defense_episodes_pre','breakout_closed_ts',
    'pre_last_close','atr_pre','gap_to_zone_pct','gap_to_zone_atr','signed_center_pct',
    'orientation','contact_band_pre','known_touch','known_hold','state_known'
]
EVENT_COLS = [
    'event_key','symbol','side','date_utc','observation_type','retest_ts',
    'coverage_15m','coverage_30m','all_covered','n_zones_total',
    'n_zones_15m','n_zones_30m','primary_zone_id','primary_timeframe',
    'primary_level_price','primary_zone_low','primary_zone_high','primary_state_pre',
    'primary_level_origin','primary_orientation','primary_distance_atr',
    'primary_distance_pct','primary_contact_band_pre','primary_defenses','primary_defense_episodes',
    'primary_acceptance_last4','primary_known_touch','primary_known_hold',
    'pre_family','known_family','support_zone_id','support_distance_atr',
    'opposing_zone_id','opposing_distance_pct','opposing_distance_atr',
    'space_to_opposition_pct','space_to_opposition_atr',
    'placebo_distance_atr','placebo_near_pre','reason'
]


def _valid_eligible(event, tf, df):
    step=v1.STEP[tf]
    r=int(event['retest_ts'])
    closed=df[(df.timestamp+step)<=r].tail(v1.LOOKBACK[tf]).copy().reset_index(drop=True)
    if len(closed)<v1.LOOKBACK[tf]:
        return closed,'INSUFFICIENT'
    stamps=closed.timestamp.to_numpy(dtype='int64')
    if not np.all(np.diff(stamps)==step):return closed,'GAPPED'
    numeric=closed[['open','high','low','close','volume']].to_numpy(dtype=float)
    if not np.isfinite(numeric).all() or (numeric[:,1]<numeric[:,2]).any() or (numeric[:,0:4]<=0).any():
        return closed,'INVALID'
    return closed,'COMPLETE'


def _gap(price, z):
    lo=float(z['zone_low']);hi=float(z['zone_high'])
    return max(lo-price,0.0,price-hi)


def _orientation(price,z,side):
    lo=float(z['zone_low']);hi=float(z['zone_high'])
    if lo <= price <= hi:return 'AT_ZONE'
    if side=='LONG':return 'SUPPORT_BELOW' if price>hi else 'RESISTANCE_ABOVE'
    return 'RESISTANCE_ABOVE' if price<lo else 'SUPPORT_BELOW'


def _contact_band(distance_atr):
    if distance_atr is None:return 'UNKNOWN_ATR'
    if distance_atr<=NEAR_ATR:return 'NEAR_0_0.5ATR'
    if distance_atr<=CONTEXT_ATR:return 'CONTEXT_0.5_1.5ATR'
    return 'DISTANT_OVER_1.5ATR'


def _id(e,tf,z):
    blob=f"{e['event_key']}|{tf}|{z['origin']}|{z['formation_closed_ts']}|{float(z['center']):.14g}"
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _defense_episodes(closed, zone, side, after_close_ts, step):
    """Count non-adjacent visits with favorable closes, not candles in same visit."""
    after=closed[(closed.timestamp+step)>int(after_close_ts)].copy().reset_index(drop=True)
    if after.empty:return 0
    if side=='LONG':
        eligible=(after.low<=zone['high']) & (after.close>=zone['center'])
    else:
        eligible=(after.high>=zone['low']) & (after.close<=zone['center'])
    indices=np.flatnonzero(eligible.to_numpy())
    if len(indices)==0:return 0
    return 1+int(np.count_nonzero(np.diff(indices)>2))


def levels_for_tf(event,tf,df,history_source,retest):
    """All zones selected from strictly pre-retest closed candles; KNOWN is separate."""
    closed,coverage=_valid_eligible(event,tf,df)
    if coverage!='COMPLETE':return [],coverage
    step=v1.STEP[tf];pre_price=float(closed.close.iloc[-1]);side=event['side']
    atr=v1.true_range_at(closed,len(closed)-1)
    if atr is None:return [],'INVALID_ATR'
    pivots=v1.pivots_confirmed(closed)
    options=[]
    for kind in ('HIGH','LOW'):
        for zone in v1.frozen_zones(closed,pivots,kind,step):
            # Future fields from event are never referenced during level creation.
            evaluated=v1.evaluate_zone(closed,zone,side,int(event['retest_ts']),step)
            info=evaluated or {}
            anchor=(info.get('breakout_closed_ts') or zone['formation_closed_ts'])
            episodes=_defense_episodes(closed,zone,side,anchor,step) if evaluated else None
            base={'level_price':float(zone['center']),'zone_low':float(zone['low']),
                  'zone_high':float(zone['high'])}
            classified=v1.classify_known(base,side,retest)
            gap=_gap(pre_price,base);gap_atr=gap/atr
            orientation=_orientation(pre_price,base,side)
            rec={k:None for k in ZONE_COLS}
            rec.update({k:event[k] for k in ('event_key','symbol','side','date_utc','observation_type','retest_ts')})
            rec.update(timeframe=tf,history_source=history_source,coverage=coverage,
                zone_id=_id(event,tf,zone),origin_kind=kind,
                level_origin=info.get('level_origin', ('PRIOR_HIGH' if kind=='HIGH' else 'PRIOR_LOW')),
                level_price=zone['center'],zone_low=zone['low'],zone_high=zone['high'],
                zone_atr_at_formation=zone['atr_at_formation'],
                formation_closed_ts=zone['formation_closed_ts'],pivot_count=zone['pivot_count'],
                halfwidth_pct=zone['halfwidth_pct'],
                level_state_pre=info.get('state_pre','UNCONFIRMED_OR_OPPOSING'),
                acceptance_last4=info.get('acceptance_favorable_last4'),
                defenses_pre=info.get('defenses_since_breakout'),
                defense_episodes_pre=episodes,
                breakout_closed_ts=info.get('breakout_closed_ts'),
                pre_last_close=pre_price,atr_pre=atr,gap_to_zone_pct=100*gap/pre_price,
                gap_to_zone_atr=gap_atr,
                signed_center_pct=v1.price_side_pct(pre_price,zone['center'],side),
                orientation=orientation,contact_band_pre=_contact_band(gap_atr),
                known_touch=classified['retest_touch'],known_hold=classified['retest_hold'],
                state_known=classified['state_known'])
            options.append(rec)
    # Deterministic ordering never refers to known labels/outcomes.
    options.sort(key=lambda z:(z['gap_to_zone_atr'],0 if z['level_state_pre'] in
                 ('ACCEPTED_DEFENDED','RECLAIM_ACCEPTED') else 1,
                 z['formation_closed_ts'],z['zone_id']))
    return options,coverage


def _support_candidate(z,side):
    return z['orientation']==('SUPPORT_BELOW' if side=='LONG' else 'RESISTANCE_ABOVE')


def _opposite_candidate(z,side):
    return z['orientation']==('RESISTANCE_ABOVE' if side=='LONG' else 'SUPPORT_BELOW')


def _pre_family(primary):
    if not primary:return 'NO_CONFIRMED_LEVEL'
    if primary['gap_to_zone_atr']>CONTEXT_ATR:return 'DISTANT_LEVEL_ONLY'
    st=primary['level_state_pre']
    if st=='LOST_LEVEL_PRE':return 'LOST_LEVEL_PRE'
    if primary['gap_to_zone_atr']<=NEAR_ATR:
        if st=='ACCEPTED_DEFENDED':return 'NEAR_ACCEPTED_DEFENDED'
        if st=='RECLAIM_ACCEPTED':return 'NEAR_RECLAIM_ACCEPTED'
        return 'NEAR_OTHER_CONFIRMED'
    if st=='RECLAIM_ACCEPTED':return 'RECLAIM_CONTINUATION_CONTEXT'
    if st=='ACCEPTED_DEFENDED':return 'DEFENSE_CONTEXT_NOT_DIRECT'
    return 'CONTEXT_NOT_CONFIRMED'


def summarize_event(event,tf_items,coverages):
    e={k:event[k] for k in ('event_key','symbol','side','date_utc','observation_type','retest_ts')}
    all_z=[z for vals in tf_items.values() for z in vals]
    complete=all(v=='COMPLETE' for v in coverages.values())
    def nearest(values):
        return min(values,key=lambda z:(z['gap_to_zone_atr'],
                   0 if z['level_state_pre'] in ('ACCEPTED_DEFENDED','RECLAIM_ACCEPTED') else 1,
                   z['formation_closed_ts'],z['zone_id'])) if values else None
    primary=nearest(all_z)
    support=nearest([z for z in all_z if _support_candidate(z,event['side'])])
    opposing=nearest([z for z in all_z if _opposite_candidate(z,event['side'])])
    def fromp(key):return primary.get(key) if primary else None
    near_gap=fromp('gap_to_zone_atr')
    known='NOT_EVALUABLE' if not primary else (
        'DIRECT_TOUCH_HELD_KNOWN' if primary['known_touch'] and primary['known_hold'] else
        'DIRECT_TOUCH_FAILED_KNOWN' if primary['known_touch'] else 'NO_TOUCH_KNOWN')
    if not complete: pre='UNKNOWN_HISTORY'
    else:pre=_pre_family(primary)
    # Structural space: a distance to the nearest PRE-confirmed opposing zone,
    # NOT a claim that its wall will hold as resistance/support.
    space_pct=opposing['gap_to_zone_pct'] if opposing else None
    space_atr=opposing['gap_to_zone_atr'] if opposing else None
    placebo_d=None
    if primary:
        # Negative control: deterministic signed shift, not an actual detected level.
        offset=PLACEBO_SHIFT_ATR*(1 if int(hashlib.sha256(event['event_key'].encode()).hexdigest(),16)%2 else -1)
        shift=offset*primary['atr_pre']
        displaced={'zone_low':primary['zone_low']+shift,'zone_high':primary['zone_high']+shift}
        placebo_d=_gap(primary['pre_last_close'],displaced)/primary['atr_pre']
    o={k:None for k in EVENT_COLS}
    o.update(e,coverage_15m=coverages.get('15m'),coverage_30m=coverages.get('30m'),
        all_covered=int(complete),n_zones_total=len(all_z),
        n_zones_15m=len(tf_items.get('15m',[])),n_zones_30m=len(tf_items.get('30m',[])),
        primary_zone_id=fromp('zone_id'),primary_timeframe=fromp('timeframe'),
        primary_level_price=fromp('level_price'),primary_zone_low=fromp('zone_low'),
        primary_zone_high=fromp('zone_high'),primary_state_pre=fromp('level_state_pre'),
        primary_level_origin=fromp('level_origin'),primary_orientation=fromp('orientation'),
        primary_distance_atr=near_gap,primary_distance_pct=fromp('gap_to_zone_pct'),
        primary_contact_band_pre=fromp('contact_band_pre'),
        primary_defenses=fromp('defenses_pre'),primary_defense_episodes=fromp('defense_episodes_pre'),primary_acceptance_last4=fromp('acceptance_last4'),
        primary_known_touch=fromp('known_touch'),primary_known_hold=fromp('known_hold'),
        pre_family=pre,known_family=known,
        support_zone_id=support['zone_id'] if support else None,
        support_distance_atr=support['gap_to_zone_atr'] if support else None,
        opposing_zone_id=opposing['zone_id'] if opposing else None,
        opposing_distance_pct=space_pct,opposing_distance_atr=space_atr,
        space_to_opposition_pct=space_pct,space_to_opposition_atr=space_atr,
        placebo_distance_atr=placebo_d,
        placebo_near_pre=(int(placebo_d<=NEAR_ATR) if placebo_d is not None else None),
        reason='PRE_ONLY_LEVEL_SELECTION;KNOWN_SEPARATE;OUTCOME_NEVER_READ')
    return o


def run(root,max_events=40,cohort='live-stratified'):
    integrity=v1.lineage.integrity(root)
    archive=root/v1.lineage.ROOT_REL/v1.lineage.CTX_NAME
    cache=root/v1.lineage.ROOT_REL/v1.lineage.HISTORY_NAME
    hist=v1.lineage.ro(cache) if cache.exists() else None
    all_levels=[];event_rows=[]
    try:
        with closing(v1.lineage.ro(archive)) as ctx:
            events=v1.v4.select_events(ctx,cohort,max_events)
            grouped=defaultdict(list)
            for ev in events:grouped[ev['symbol']].append(ev)
            for sym,items in grouped.items():
                df15,df30,source30=v1.v4._merge_tf(ctx,hist,sym)
                df15=df15.reset_index();df30=df30.reset_index()
                for ev in items:
                    _,retest=v1.lineage.one_min_pre_and_retest(ctx,sym,ev['retest_ts'])
                    z15,c15=levels_for_tf(ev,'15m',df15,'NATIVE_15M',retest)
                    z30,c30=levels_for_tf(ev,'30m',df30,source30,retest)
                    all_levels.extend(z15);all_levels.extend(z30)
                    event_rows.append(summarize_event(ev,{'15m':z15,'30m':z30},{'15m':c15,'30m':c30}))
    finally:
        if hist is not None:hist.close()
    if len(event_rows)!=len(events):raise RuntimeError('Faltan eventos')
    folder=root/v1.lineage.ROOT_REL/OUTPUT
    v1.write_csv_atomic(folder/'candidate_levels.csv',all_levels,ZONE_COLS)
    v1.write_csv_atomic(folder/'event_map.csv',event_rows,EVENT_COLS)
    summary=dict(version=VERSION,events=len(events),candidate_levels=len(all_levels),cohort=cohort,
        max_events=max_events,frozen_cohort_sha256=integrity['frozen_cohort_sha256'],
        coverage=dict(Counter(str(r['coverage_15m'])+'|'+str(r['coverage_30m']) for r in event_rows)),
        pre_families=dict(Counter(r['pre_family'] for r in event_rows)),
        files_sha256={name:v1.lineage.hash_file(folder/name) for name in ('candidate_levels.csv','event_map.csv')},
        assumptions=['All levels frozen from two confirmed pivots; original V1 pivot/ATR thresholds unchanged',
          'All HTF levels listed, proximity to pre-close selects a primary BEFORE current retest',
          '0.5/1.5 ATR distance buckets fixed a priori, not optimized on labels',
          'KNOWN current retest candle never selects primary or pre-family',
          'Opposing zone space is descriptive; may be absent or unverified barrier',
          'Three-ATR deterministic translated placebo is normalized by pre-retest ATR and is not randomized control events',
          'Original SQLite read-only; historical discovery, no execution edge established'])
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('LEVEL_ATLAS_V2_DONE',json.dumps({'events':len(events),'candidate_levels':len(all_levels),
        'coverage':summary['coverage'],'pre_families':summary['pre_families']}),flush=True)
    return summary


def plan(root,max_events,cohort):
    result=v1.plan(root,max_events,cohort)
    result.update(version=VERSION, output=OUTPUT,
        near_atr=NEAR_ATR,context_atr=CONTEXT_ATR,placebo_shift_atr=PLACEBO_SHIFT_ATR,
        notes='V1 pivot definitions unchanged; all levels preserved per event/timeframe')
    return result


def audit(root):
    folder=root/v1.lineage.ROOT_REL/OUTPUT
    meta=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    a=pd.read_csv(folder/'candidate_levels.csv',low_memory=False)
    b=pd.read_csv(folder/'event_map.csv',low_memory=False)
    hashes=all(v1.lineage.hash_file(folder/fn)==h for fn,h in meta['files_sha256'].items())
    ids=set(b.event_key)
    chronological=(a.empty or ((a.formation_closed_ts<a.retest_ts).all() and
        (a.breakout_closed_ts.dropna()<a.loc[a.breakout_closed_ts.notna(),'retest_ts']).all()))
    paired=b.groupby('event_key',dropna=False).size().eq(1).all() and not b.event_key.duplicated().any()
    counts=a.groupby('event_key').size() if len(a) else pd.Series(dtype='int64')
    total_match=all(int(row.n_zones_total)==int(counts.get(row.event_key,0)) for row in b.itertuples(index=False))
    coverage_ok=b.all_covered.isin([0,1]).all() and b.pre_family.notna().all()
    source_ok=meta['frozen_cohort_sha256']==v1.lineage.integrity(root)['frozen_cohort_sha256']
    sample_ok=set(a.event_key).issubset(ids)
    valid=bool(hashes and chronological and paired and total_match and coverage_ok and source_ok and sample_ok
               and len(b)==meta['events'] and len(a)==meta['candidate_levels'])
    return dict(success=valid,events=len(b),candidate_levels=len(a),hashes_ok=hashes,
      chronology_valid=bool(chronological),one_row_per_event=bool(paired),counts_valid=bool(total_match),
      source_cohort_matches=source_ok,pre_families=meta['pre_families'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    operations=parser.add_mutually_exclusive_group(required=True)
    for op in ('plan','run','audit'):operations.add_argument('--'+op,action='store_true')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--cohort',choices=['live-stratified','all-live','all-frozen'],default='live-stratified')
    parser.add_argument('--max-events',type=int,default=40,help='0 = all events in selected cohort')
    args=parser.parse_args(argv)
    if args.max_events<0:parser.error('max-events must be >=0')
    root=args.root.resolve()
    if args.plan:result=plan(root,args.max_events,args.cohort)
    elif args.run:result=run(root,args.max_events,args.cohort)
    else:result=audit(root)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
