#!/usr/bin/env python3
"""V2.4B | Multi-Swing BTC 60D: causal landmarks + uncensored-by-revisit paths.

OFFLINE and descriptive only. Reads the hash-verified frozen minute feed/catalog
and the immutable V2.4A census. Does not import the live engine, HLL, Redis,
Streamlit, network clients, or trading logic.

For EACH region there is a birth snapshot. For EACH V2.3 visit there are two
independent, timestamped feature snapshots:
  FIRST_TOUCH_CONFIRMED: first 1m intersecting candle has CLOSED. No visit
    duration, exit, eventual members, or later completed visit is accessible.
  VISIT_CLOSED: gap+1 non-contact closed bars have completed and the V2.3
    visit end is now known. Observed visit descriptors may be used from here.
An open visit never gets a VISIT_CLOSED landmark.

Static geometry is always the birth-stage band, never the final V2 band.
Stage extrema are read as-of each landmark; member confirmations are filtered
by availability; market context contains only closed bars up to the landmark.
Features and POST outcomes are exported in separate files. The outcome windows
are genuinely FIXED: revisiting the band does NOT terminate measurement. A
cutoff-short window is CUTOFF_CENSORED with NULL performance, not zero return.

No rankings, trading labels, hit rates, future-assisted classifications, or
claims of independence of visits in overlapping regions.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, asof_rows, digest, iso, load_snapshot
from btc_multiswing_information_universe_v24 import (
    DEFAULT_REPORT as V24A_REPORT, VERSION as V24A_VERSION,
    birth_stage_universe,
)
from btc_multiswing_structural_confluence_v2 import construct_regions

VERSION = 'btc-multiswing-causal-features-fixed-outcomes-v2.4b'
DEFAULT_OUTPUT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_causal_features_v24b')
HORIZONS = (60, 240, 720, 1440, 2880)
LANDMARKS = ('BIRTH_CONFIRMED', 'FIRST_TOUCH_CONFIRMED', 'VISIT_CLOSED')
NECESSARY_V24A = ('regions_v24.csv','visits_v24.csv','post_horizons_v24.csv','overlaps_v24.csv',
                  'summary_v24.json','manifest.json')
FEATURE_COLUMNS = [
    'region_id','side','visit_number','landmark','known_ts','known_utc',
    'region_birth_ts','region_age_minutes','band_low_birth','band_high_birth',
    'center_birth','band_width_pct_birth','first_touch_ts',
    'stage_extrema_asof','stage_last_confirm_ts','stage_age_minutes',
    'physical_member_count_asof','member_tf_rep_count_asof',
    'member_distinct_tf_count_asof','confirmed_member_first_ts',
    'confirmed_member_last_ts','member_confirmation_span_minutes',
    'inter_pivot_reversal_mean_pct','inter_pivot_reversal_max_pct',
    'close_at_landmark','close_position','distance_close_center_pct',
    'return_60m_pct','return_240m_pct','mean_true_range_60m_pct',
    'near_other_birth_bands_asof','near_same_side_birth_bands_asof',
    'prior_closed_visits_asof','prior_known_touch_minutes',
    'prior_known_two_closes_above','prior_known_two_closes_below',
    'arrival_side','visit_status_known','visit_exit_side_known',
    'visit_span_minutes_known','visit_touch_minutes_known',
    'visit_contact_episodes_known','visit_bridged_minutes_known',
    'visit_two_closes_above_known','visit_two_closes_below_known',
]
OUTCOME_COLUMNS = [
    'region_id','side','visit_number','landmark','evaluation_from_ts',
    'horizon_minutes','observed_minutes','status','last_known_ts',
    'reference_close','max_above_birth_band_pct','max_below_birth_band_pct',
    'favorable_excursion_pct','adverse_excursion_pct',
    'side_adjusted_close_return_pct','next_structural_visit_within_horizon',
    'v23_stopped_on_revisit_status',
]
DEPENDENCY_COLUMNS = ['region_id','retrospective_dependency_component',
                      'component_members','component_same_side_members',
                      'component_min_birth_ts','component_max_birth_ts',
                      'causal_feature_no_component_id']


def _valid_bars(full: pd.DataFrame, cutoff: int) -> pd.DataFrame:
    required = {'timestamp','open','high','low','close'}
    if full.empty or not required.issubset(full.columns) or cutoff % M:
        raise ValueError('V24B_INVALID_1M_INPUT')
    f = full.loc[(full.timestamp + M <= cutoff), list(('timestamp','open','high','low','close'))].copy()
    if f.empty:
        raise ValueError('V24B_NO_CLOSED_1M')
    for c in ('timestamp','open','high','low','close'):
        f[c] = pd.to_numeric(f[c],errors='coerce')
    if f.isna().any().any() or not np.isfinite(f.to_numpy(dtype=float)).all():
        raise ValueError('V24B_BAD_1M_NUMBERS')
    f = f.sort_values('timestamp').reset_index(drop=True)
    stamps = f.timestamp.to_numpy(dtype=np.int64)
    if (len(stamps)>1 and np.any(np.diff(stamps)!=M)) or np.any(stamps % M) or stamps[-1]+M!=cutoff:
        raise ValueError('V24B_INCOMPLETE_1M_PATH')
    o,h,l,c = (f[k].to_numpy(dtype=float) for k in ('open','high','low','close'))
    if (np.any(np.minimum.reduce([o,h,l,c])<=0) or
        np.any(h < np.maximum.reduce([o,l,c])) or
        np.any(l > np.minimum.reduce([o,h,c]))):
        raise ValueError('V24B_INVALID_OHLC')
    return f


def _load_v24a(run: Path):
    run = Path(run)
    if not run.is_dir():
        raise FileNotFoundError(f'V24A_RUN_MISSING: {run}')
    manifest = json.loads((run/'manifest.json').read_text(encoding='utf-8'))
    summary = json.loads((run/'summary_v24.json').read_text(encoding='utf-8'))
    if (manifest.get('version') != V24A_VERSION or summary.get('version') != V24A_VERSION
            or manifest.get('immutable_run') is not True):
        raise ValueError('V24B_INVALID_PARENT_RUN')
    for name in NECESSARY_V24A:
        if name == 'manifest.json':
            continue
        if not (run/name).is_file() or digest(run/name)!=manifest.get('sha256',{}).get(name):
            raise ValueError('V24B_PARENT_HASH_MISMATCH: '+name)
    if manifest.get('config',{}).get('cutoff')!=summary.get('cutoff'):
        raise ValueError('V24B_PARENT_CUTOFF_MISMATCH')
    if manifest.get('config',{}).get('timeframes')!=summary.get('timeframes'):
        raise ValueError('V24B_PARENT_TF_MISMATCH')
    if manifest.get('source_provenance')!=summary.get('source_provenance'):
        raise ValueError('V24B_PARENT_SOURCE_MISMATCH')
    regions=pd.read_csv(run/'regions_v24.csv')
    visits=pd.read_csv(run/'visits_v24.csv')
    overlaps=pd.read_csv(run/'overlaps_v24.csv')
    parent_post=pd.read_csv(run/'post_horizons_v24.csv')
    return regions,visits,parent_post,overlaps,summary,manifest


def _v2_asof_universe(catalog, cutoff, config):
    tfs=tuple(config['timeframes'])
    geometry=config['geometry']
    known=asof_rows(catalog,cutoff,tfs,config['side'])
    regions,stages,stats=construct_regions(
        known,cutoff,tolerance_pct=float(geometry['tolerance_pct']),
        lookback_days=int(geometry['lookback_days']),
        min_separation_minutes=int(geometry['separation_minutes']),min_extrema=2)
    births=birth_stage_universe(regions,stages,cutoff)
    return known,regions,stages,births,stats


def _validate_parent_census(base:pd.DataFrame, visits:pd.DataFrame,
                            births:pd.DataFrame, cutoff:int, gap:int):
    if (base.region_id.duplicated().any() or visits.duplicated(['region_id','visit_number']).any()
            or len(base)!=len(births) or set(base.region_id)!=set(births.region_id)):
        raise ValueError('V24B_CENSUS_NOT_MATCHING_V2')
    a=base.set_index('region_id')
    b=births.set_index('region_id')
    for rid in b.index:
        br=b.loc[rid]; ar=a.loc[rid]
        if (str(br.side)!=str(ar.side)
                or int(br.birth_known_ts)!=int(ar.birth_known_ts)
                or abs(float(br.band_low_birth)-float(ar.band_low_birth))>1e-7
                or abs(float(br.band_high_birth)-float(ar.band_high_birth))>1e-7):
            raise ValueError('V24B_BIRTH_STAGE_CONFLICT: '+rid)
    if (not visits.empty and
        (not visits.region_id.isin(base.region_id).all()
         or not (pd.to_numeric(visits.gap_minutes)==gap).all()
         or (pd.to_numeric(visits.first_known_ts)>cutoff).any())):
        raise ValueError('V24B_INVALID_PARENT_VISITS')
    if len(visits)!=int(base.visits.sum()):
        raise ValueError('V24B_PARENT_VISIT_TOTAL_CONFLICT')


def _member_index(known: pd.DataFrame, regions: pd.DataFrame):
    """Representations known by the frozen cutoff; filter AGAIN per landmark.

    `regions.members` is used only for lookup. It is never a count or a feature.
    No representation whose available_ts is later than the landmark is used.
    """
    verified=known.loc[known.physical_identity_state.eq('VERIFIED')].copy()
    all_ids=set()
    region_member_ids={}
    for r in regions.itertuples(index=False):
        ids=str(r.members).split(',')
        if not ids or len(ids)!=len(set(ids)):
            raise ValueError('V24B_INVALID_REGION_MEMBER_IDS')
        region_member_ids[str(r.region_id)]=ids
        all_ids.update(ids)
    verified=verified[verified.physical_id.isin(all_ids)]
    idx={}
    for pid,g in verified.groupby('physical_id'):
        if (g.side.nunique()!=1 or g.price.nunique()!=1 or
            g.physical_minute_ts.nunique()!=1):
            raise ValueError('V24B_PHYSICAL_ID_CONFLICT')
        idx[str(pid)]={
            'side':str(g.side.iloc[0]),'price':float(g.price.iloc[0]),
            'pivot_ts':int(g.physical_minute_ts.iloc[0]),
            'confirmations':sorted((int(x.available_ts),str(x.timeframe))
                                   for x in g.itertuples(index=False)),
        }
    if any(pid not in idx for ids in region_member_ids.values() for pid in ids):
        raise ValueError('V24B_MISSING_REGION_MEMBER')
    return region_member_ids,idx


def _region_dependency_components(base:pd.DataFrame, overlaps:pd.DataFrame):
    """Retrospective grouping for robust evaluation, NEVER an as-of feature."""
    ids=base.region_id.astype(str).tolist()
    parent={rid:rid for rid in ids}
    def find(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]]
            x=parent[x]
        return x
    for edge in overlaps.itertuples(index=False):
        a,b=str(edge.region_id),str(edge.preexisting_region_id)
        if a not in parent or b not in parent:
            raise ValueError('V24B_FOREIGN_DEPENDENCY_EDGE')
        # Only actual band intersections or nearby centers (V24A scope).
        if bool(edge.within_center_tolerance) or bool(edge.band_intersects):
            pa,pb=find(a),find(b)
            if pa!=pb: parent[max(pa,pb)]=min(pa,pb)
    comps=defaultdict(list)
    for rid in ids: comps[find(rid)].append(rid)
    meta=base.set_index('region_id')
    out=[]
    for rid in ids:
        members=sorted(comps[find(rid)])
        times=[int(meta.loc[m,'birth_known_ts']) for m in members]
        out.append({'region_id':rid,
            'retrospective_dependency_component':members[0],
            'component_members':len(members),
            'component_same_side_members':sum(str(meta.loc[m,'side'])==str(meta.loc[rid,'side']) for m in members),
            'component_min_birth_ts':min(times), 'component_max_birth_ts':max(times),
            'causal_feature_no_component_id':True})
    return pd.DataFrame(out,columns=DEPENDENCY_COLUMNS)


def _reversal_features(members, timestamps, lows, highs, offset):
    rec=[]
    for a,b in zip(members,members[1:]):
        left=int(np.searchsorted(timestamps,int(a['pivot_ts'])+M))
        right=int(np.searchsorted(timestamps,int(b['pivot_ts'])))
        if right<=left:
            raise ValueError('V24B_NO_INTERMEDIATE_BARS')
        if a['side']=='HIGH':
            trough=float(np.min(lows[left:right]))
            pct=max(0.0,(min(a['price'],b['price'])-trough)/offset*100.)
        else:
            peak=float(np.max(highs[left:right]))
            pct=max(0.0,(peak-max(a['price'],b['price']))/offset*100.)
        rec.append(pct)
    return (sum(rec)/len(rec),max(rec)) if rec else (None,None)


def build_v24b(full:pd.DataFrame, catalog:pd.DataFrame, regions_v24:pd.DataFrame,
               visits_v24:pd.DataFrame, overlaps_v24:pd.DataFrame, cutoff:int,
               *, config:dict, parent_post_v24:pd.DataFrame|None=None, progress=None):
    """Pure full-census calculation; caller owns hash verification and writes.

    Returns: (causal_features, fixed_horizon_outcomes, dependency_groups, summary).
    Future OHLC is accessed exclusively in the outcome loop, not in features.
    """
    cutoff=int(cutoff)
    frame=_valid_bars(full,cutoff)
    known,regions,stages,births,_=_v2_asof_universe(catalog,cutoff,config)
    gap=int(config['gap_minutes'])
    horizons=tuple(int(v) for v in config['horizons_minutes'])
    if not horizons or len(set(horizons))!=len(horizons) or any(h not in HORIZONS for h in horizons):
        raise ValueError('V24B_INVALID_HORIZON_CONFIG')
    _validate_parent_census(regions_v24,visits_v24,births,cutoff,gap)
    birthmap={str(r.region_id):r for r in births.itertuples(index=False)}
    by_stage={k:g.sort_values(['known_from','independent_extrema'],kind='mergesort').to_dict('records')
              for k,g in stages.groupby('region_id')}
    member_map, member_idx=_member_index(known,regions)
    dep=_region_dependency_components(regions_v24,overlaps_v24)
    all_birth= [(str(r.region_id),int(r.birth_known_ts),str(r.side),
                 float(r.band_low_birth),float(r.band_high_birth))
                for r in births.itertuples(index=False)]
    stamps=frame.timestamp.to_numpy(dtype=np.int64)
    lows=frame.low.to_numpy(dtype=float)
    highs=frame.high.to_numpy(dtype=float)
    closes=frame.close.to_numpy(dtype=float)
    opens=frame.open.to_numpy(dtype=float)
    index0=int(stamps[0])
    def ix(closed_end):
        """Index of last candle whose CLOSE is known at closed_end."""
        end=int(closed_end)
        pos=(end-index0)//M-1
        if end%M or pos<0 or pos>=len(stamps) or stamps[pos]+M!=end:
            raise ValueError('V24B_LANDMARK_OUTSIDE_CONTIGUOUS_FEED')
        return pos

    parent_post_index={}
    if parent_post_v24 is not None:
        if (parent_post_v24.duplicated(['region_id','visit_number','horizon_minutes']).any()
                or len(parent_post_v24)!=len(visits_v24)*len(horizons)):
            raise ValueError('V24B_INVALID_V23_POST_JOIN')
        for q in parent_post_v24.itertuples(index=False):
            key=(str(q.region_id),int(q.visit_number),int(q.horizon_minutes))
            parent_post_index[key]=(str(q.status),q.evaluation_from_ts)
    features, outcomes=[],[]
    visit_groups={str(k):g.sort_values('visit_number',kind='mergesort') for k,g in visits_v24.groupby('region_id')}
    for n,birth in enumerate(births.itertuples(index=False),1):
        rid=str(birth.region_id)
        side=str(birth.side)
        low,high=float(birth.band_low_birth),float(birth.band_high_birth)
        mid=(low+high)/2
        mem_ids=member_map[rid]
        region_visits=visit_groups.get(rid)
        vr=(region_visits.to_dict('records') if region_visits is not None else [])
        # BIRTH_CONFIRMED has no visit_number, touch, arrival side, or visit POST.
        # It covers the six regions without any subsequent V2.3 visits as well.
        evaluations=[(-1,None,None,None,[('BIRTH_CONFIRMED',int(birth.birth_known_ts))])]
        for vi,v in enumerate(vr):
            vnum=int(v['visit_number'])
            first=int(v['first_known_ts'])
            first_touch=int(v['first_touch_ts'])
            if first_touch < int(birth.birth_known_ts) or first!=first_touch+M or first>cutoff:
                raise ValueError('V24B_INVALID_FIRST_TOUCH_LANDMARK')
            last=v['end_known_ts']
            closing = None if pd.isna(last) else int(last)
            if closing is not None and (closing<first or closing>cutoff):
                raise ValueError('V24B_INVALID_VISIT_CLOSE_LANDMARK')
            if (closing is None) != (str(v['status'])=='OPEN_AT_CUTOFF'):
                raise ValueError('V24B_VISIT_STATUS_CONFLICT')
            snaps=[('FIRST_TOUCH_CONFIRMED',first)]
            if closing is not None:
                snaps.append(('VISIT_CLOSED',closing))
            evaluations.append((vi,v,vnum,first_touch,snaps))
        for vi,v,vnum,first_touch,snaps in evaluations:
            for label,t in snaps:
                bar_i=ix(t)
                # AS-OF membership counts: only confirmations available now.
                members=[]
                tf_reps=0
                tf_set=set()
                for pid in mem_ids:
                    m=member_idx[pid]
                    confirmations=[tf for at,tf in m['confirmations'] if at<=t]
                    if confirmations:
                        if m['side']!=side or m['pivot_ts']>=t:
                            raise ValueError('V24B_PHYSICAL_MEMBER_AFTER_LANDMARK')
                        members.append(m)
                        tf_reps+=len(confirmations)
                        tf_set.update(confirmations)
                members.sort(key=lambda m:(m['pivot_ts'],m['price']))
                candidate=[st for st in by_stage[rid] if int(st['known_from'])<=t]
                if not candidate:
                    raise ValueError('V24B_STAGE_NOT_BORN_ASOF')
                selected=max(candidate,key=lambda st:(int(st['known_from']),int(st['independent_extrema'])))
                indep=int(selected['independent_extrema'])
                if indep!=len(members):
                    raise ValueError(f'V24B_STAGE_MEMBER_MISMATCH: {rid} t={t} stage={indep} actual={len(members)}')
                r_mean,r_max=_reversal_features(members,stamps,lows,highs,mid)
                # Changes to frozen bars AFTER this landmark cannot enter any feature.
                v60=(float(closes[bar_i]/closes[bar_i-60]-1)*100 if bar_i>=60 else None)
                v240=(float(closes[bar_i]/closes[bar_i-240]-1)*100 if bar_i>=240 else None)
                if bar_i>=60:
                    left=bar_i-59
                    preceding=np.concatenate(([closes[left-1]],closes[left:bar_i]))
                    true_ranges=np.maximum.reduce((highs[left:bar_i+1]-lows[left:bar_i+1],
                        abs(highs[left:bar_i+1]-preceding),
                        abs(lows[left:bar_i+1]-preceding)))
                    tr=float(np.mean(true_ranges)/closes[bar_i]*100)
                else:
                    tr=None
                close=float(closes[bar_i])
                position=('ABOVE' if close>high else 'BELOW' if close<low else 'INSIDE')
                near=near_side=0
                for other_id,other_born,other_side,ol,oh in all_birth:
                    if other_id==rid or other_born>t:
                        continue
                    other_mid=(ol+oh)/2
                    distance=abs(other_mid-mid)/((other_mid+mid)/2)*100
                    if (distance<=float(config['geometry']['tolerance_pct'])+1e-12
                            or max(low,ol)<=min(high,oh)):
                        near+=1
                        near_side+=int(other_side==side)
                previous=[q for q in vr[:max(0,vi)] if not pd.isna(q['end_known_ts'])
                          and int(q['end_known_ts'])<=t]
                closed_here=(label=='VISIT_CLOSED')
                birth_here=(label=='BIRTH_CONFIRMED')
                features.append({
                    'region_id':rid,'side':side,'visit_number':vnum,
                    'landmark':label,'known_ts':t,'known_utc':iso(t),
                    'region_birth_ts':int(birth.birth_known_ts),
                    'region_age_minutes':(t-int(birth.birth_known_ts))//M,
                    'band_low_birth':low,'band_high_birth':high,'center_birth':mid,
                    'band_width_pct_birth':(high-low)/mid*100,
                    'first_touch_ts':first_touch,
                    'stage_extrema_asof':indep,
                    'stage_last_confirm_ts':int(selected['known_from']),
                    'stage_age_minutes':(t-int(selected['known_from']))//M,
                    'physical_member_count_asof':len(members),
                    'member_tf_rep_count_asof':tf_reps,
                    'member_distinct_tf_count_asof':len(tf_set),
                    'confirmed_member_first_ts':min(m['pivot_ts'] for m in members),
                    'confirmed_member_last_ts':max(m['pivot_ts'] for m in members),
                    'member_confirmation_span_minutes':
                        (max(m['pivot_ts'] for m in members)-min(m['pivot_ts'] for m in members))//M,
                    'inter_pivot_reversal_mean_pct':r_mean,
                    'inter_pivot_reversal_max_pct':r_max,
                    'close_at_landmark':close,'close_position':position,
                    'distance_close_center_pct':(close-mid)/mid*100,
                    'return_60m_pct':v60,'return_240m_pct':v240,
                    'mean_true_range_60m_pct':tr,
                    'near_other_birth_bands_asof':near,
                    'near_same_side_birth_bands_asof':near_side,
                    'prior_closed_visits_asof':len(previous),
                    'prior_known_touch_minutes':sum(int(q['touch_minutes']) for q in previous),
                    'prior_known_two_closes_above':sum(int(q['two_closes_above']) for q in previous),
                    'prior_known_two_closes_below':sum(int(q['two_closes_below']) for q in previous),
                    'arrival_side':None if birth_here else str(v['arrival_side']),
                    'visit_status_known':('NOT_APPLICABLE_AT_BIRTH' if birth_here else
                        'ENDED_BY_INACTIVITY' if closed_here else 'NOT_YET_CLOSED'),
                    'visit_exit_side_known':str(v['exit_side']) if closed_here else None,
                    'visit_span_minutes_known':int(v['span_minutes']) if closed_here else None,
                    'visit_touch_minutes_known':int(v['touch_minutes']) if closed_here else None,
                    'visit_contact_episodes_known':int(v['contact_episodes']) if closed_here else None,
                    'visit_bridged_minutes_known':int(v['bridged_noncontact_minutes']) if closed_here else None,
                    'visit_two_closes_above_known':int(v['two_closes_above']) if closed_here else None,
                    'visit_two_closes_below_known':int(v['two_closes_below']) if closed_here else None,
                })
                next_touch=(int(vr[0]['first_touch_ts']) if birth_here and vr else
                     int(vr[vi+1]['first_touch_ts']) if not birth_here and vi+1<len(vr) else None)
                for h in horizons:
                    observed=max(0,min(h,(cutoff-t)//M))
                    complete=(observed==h)
                    if complete:
                        sl=slice(bar_i+1,bar_i+1+h)
                        # Disjoint: outcome uses ONLY future 1m candles.
                        ph=float(np.max(highs[sl]))
                        pl=float(np.min(lows[sl]))
                        last_close=float(closes[bar_i+h])
                        above=max(0.,(ph-high)/high*100)
                        below=max(0.,(low-pl)/low*100)
                        favorable=max(0., ((close-pl) if side=='HIGH' else (ph-close))/close*100)
                        adverse=max(0., ((ph-close) if side=='HIGH' else (close-pl))/close*100)
                        direction=(-1 if side=='HIGH' else 1)
                        net=direction*(last_close/close-1)*100
                    else:
                        above=below=favorable=adverse=net=None
                    parent_status=None
                    if closed_here and parent_post_v24 is not None:
                        pair=parent_post_index.get((rid,vnum,int(h)))
                        if pair is None or pd.isna(pair[1]) or int(pair[1])!=t:
                            raise ValueError('V24B_V23_CLOSE_ALIGNMENT_CONFLICT')
                        parent_status=pair[0]
                    outcomes.append({
                        'region_id':rid,'side':side,'visit_number':vnum,
                        'landmark':label,'evaluation_from_ts':t,
                        'horizon_minutes':h,'observed_minutes':observed,
                        'status':'FULL_HORIZON' if complete else 'CUTOFF_CENSORED',
                        'last_known_ts':t+observed*M,
                        'reference_close':close,
                        'max_above_birth_band_pct':above,'max_below_birth_band_pct':below,
                        'favorable_excursion_pct':favorable,'adverse_excursion_pct':adverse,
                        'side_adjusted_close_return_pct':net,
                        'next_structural_visit_within_horizon':(next_touch is not None
                            and next_touch>=t and next_touch<t+observed*M),
                        'v23_stopped_on_revisit_status':parent_status,
                    })
        if progress and (n==1 or n%10==0 or n==len(births)):
            progress(n,len(births),rid,len(vr))
    ff=pd.DataFrame(features,columns=FEATURE_COLUMNS)
    oo=pd.DataFrame(outcomes,columns=OUTCOME_COLUMNS)
    if (ff.duplicated(['region_id','visit_number','landmark']).any() or
         oo.duplicated(['region_id','visit_number','landmark','horizon_minutes']).any()):
        raise ValueError('V24B_DUPLICATE_LANDMARK_OR_HORIZON')
    if len(ff)!=(len(regions_v24)+len(visits_v24)+(int(visits_v24.status.eq('ENDED_BY_INACTIVITY').sum()) if 'status' in visits_v24.columns else 0)):
        raise ValueError('V24B_LANDMARK_COUNT_MISMATCH')
    if len(oo)!=len(ff)*len(horizons):
        raise ValueError('V24B_FIXED_HORIZON_COUNT_MISMATCH')
    if not oo.empty:
        bad=oo.status.ne('FULL_HORIZON')
        if oo.loc[bad,['favorable_excursion_pct','adverse_excursion_pct',
                        'side_adjusted_close_return_pct']].notna().any().any():
            raise ValueError('V24B_CENSORED_OUTCOME_HAS_METRICS')
    counts={
        'regions':len(regions_v24),'visits':len(visits_v24),
        'landmark_birth':int(ff.landmark.eq('BIRTH_CONFIRMED').sum()) if not ff.empty else 0,
        'landmark_first_touch':int(ff.landmark.eq('FIRST_TOUCH_CONFIRMED').sum()) if not ff.empty else 0,
        'landmark_closed_visit':int(ff.landmark.eq('VISIT_CLOSED').sum()) if not ff.empty else 0,
        'landmarks':len(ff),'outcome_rows':len(oo),
        'fixed_full':int(oo.status.eq('FULL_HORIZON').sum()) if not oo.empty else 0,
        'fixed_cutoff_censored':int(oo.status.eq('CUTOFF_CENSORED').sum()) if not oo.empty else 0,
        'fixed_full_by_horizon':{str(h):int(((oo.horizon_minutes==h)&
                          oo.status.eq('FULL_HORIZON')).sum()) if not oo.empty else 0 for h in horizons},
        'closed_visit_fixed_full_vs_v23_censored':int(((oo.landmark=='VISIT_CLOSED') &
                        (oo.status=='FULL_HORIZON') & (oo.v23_stopped_on_revisit_status=='NEXT_VISIT_CENSORED')).sum()) if not oo.empty else 0,
        'fixed_full_with_next_structural_visit':int(((oo.status=='FULL_HORIZON')&
                                      (oo.next_structural_visit_within_horizon==True)).sum()) if not oo.empty else 0,
        'retrospective_dependency_components':int(dep.retrospective_dependency_component.nunique()) if not dep.empty else 0,
    }
    metadata={'version':VERSION,'cutoff':cutoff,'cutoff_utc':iso(cutoff),
        'parent_census_version':V24A_VERSION,
        'parent_region_geometry':'BIRTH_STAGE_FIXED',
        'config':config,'counts':counts,
        'limitations':[
            'Three different causal landmarks: BIRTH_CONFIRMED, FIRST_TOUCH_CONFIRMED and VISIT_CLOSED. Do not mix.',
            'Labels are descriptive price excursions, NOT trading PnL or a quality score.',
            'Future visits never truncate fixed horizons; cutoff-short horizons have NULL performance.',
            'Visit events and overlapping regions are dependent. Dependence components are RETROSPECTIVE, not PRE features.',
            'Inter-pivot reversal is a physical path descriptor, not a verified rejection or defense.',
            'All six-TF source identities are precomputed in a frozen catalog; confirmations are filtered as-of.',
            'BTC-only 60d is not an independent out-of-sample validation.',
        ]}
    return ff,oo,dep,metadata


def _write_run(dest:Path, frames, meta, *, provenance:dict, parent:dict):
    dest=Path(dest).resolve()
    if dest.exists():
        raise FileExistsError(f'V24B_RUN_ALREADY_EXISTS: {dest}')
    dest.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.v24b_stage_',dir=str(dest.parent)))
    try:
        names=('features_v24b.csv','fixed_horizon_outcomes_v24b.csv','dependency_groups_v24b.csv')
        for name,frame in zip(names,frames):
            frame.to_csv(stage/name,index=False)
        report={**meta,'source_provenance':provenance,'parent_v24a':parent,
                'created_utc':datetime.now(timezone.utc).isoformat()}
        (stage/'summary_v24b.json').write_text(json.dumps(report,indent=2,sort_keys=True,
            ensure_ascii=False)+'\n',encoding='utf-8')
        hashes={p.name:digest(p) for p in stage.iterdir() if p.is_file()}
        manifest={'version':VERSION,'sha256':hashes,'immutable_run':True,
                  'no_live_or_network':True,'source_provenance':provenance,
                  'parent_v24a':parent}
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True,
            ensure_ascii=False)+'\n',encoding='utf-8')
        if dest.exists():
            raise FileExistsError(f'V24B_RUN_ALREADY_EXISTS: {dest}')
        os.rename(stage,dest)
    except BaseException:
        shutil.rmtree(stage,ignore_errors=True)
        raise
    return dest


def main(argv=None):
    parser=argparse.ArgumentParser(description='Offline BTC multi-swing V2.4B causal feature/outcome audit')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    parser.add_argument('--project-root',default='.')
    parser.add_argument('--v24a-run',default=str(V24A_REPORT))
    parser.add_argument('--output-dir',default=str(DEFAULT_OUTPUT))
    args=parser.parse_args(argv)
    root=Path(args.project_root).resolve()
    parent=Path(args.v24a_run)
    if not parent.is_absolute(): parent=root/parent
    dest=Path(args.output_dir)
    if not dest.is_absolute(): dest=root/dest
    print('V24B_PLAN '+json.dumps({'mode':'PLAN' if args.plan else 'RUN',
        'project_root':str(root),'v24a_run':str(parent),'output_dir':str(dest),
        'supported_fixed_horizons_minutes':list(HORIZONS),
        'run_horizons_source':'V24A_PARENT_MANIFEST',
        'landmarks':list(LANDMARKS),'no_live':True},ensure_ascii=False),flush=True)
    if args.plan: return 0
    if dest.exists():
        raise FileExistsError(f'V24B_RUN_ALREADY_EXISTS: {dest}')
    regs,visits,parent_post,overlaps,meta,manifest=_load_v24a(parent)
    full,catalog,provenance=load_snapshot(root)
    if provenance!=meta['source_provenance'] or provenance!=manifest['source_provenance']:
        raise ValueError('V24B_PARENT_FROZEN_PROVENANCE_CHANGED')
    cutoff=int(meta['cutoff'])
    if cutoff!=int(provenance['end']):
        raise ValueError('V24B_PARENT_NONFINAL_CUTOFF')
    config=manifest['config']
    def progress(i,n,rid,nvisits):
        print(f'V24B_PROGRESS {i}/{n} region={rid} visits={nvisits}',flush=True)
    ff,oo,dep,summary=build_v24b(full,catalog,regs,visits,overlaps,cutoff,
        config=config,parent_post_v24=parent_post,progress=progress)
    parent_info={'path':str(parent.resolve()),
        'manifest_sha256':digest(parent/'manifest.json'),
        'regions_sha256':digest(parent/'regions_v24.csv'),
        'visits_sha256':digest(parent/'visits_v24.csv'),
        'overlaps_sha256':digest(parent/'overlaps_v24.csv'),
        'post_sha256':digest(parent/'post_horizons_v24.csv')}
    target=_write_run(dest,(ff,oo,dep),summary,
        provenance=provenance,parent=parent_info)
    print('V24B_FINISHED '+json.dumps({'output':str(target),
        'counts':summary['counts']},ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
