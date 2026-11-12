#!/usr/bin/env python3
"""Arrival Lab V1 · BTC Multi-Swing 60D, OFFLINE, immutable, causal.

Uses the hash-verified 60D 1m feed/catalog, the immutable V2.4A complete
birth-band visit census, and the unchanged V2 geometric stages.

Critical distinction:
  - Events are RETROSPECTIVELY LOCATED first touches of the FIXED BIRTH band.
  - PRE facts are evaluated at the OPEN of that first touching 1m candle,
    BEFORE its OHLC or subsequent outcomes are known. A retrospective event
    index does NOT imply an advance prediction of a future touch.
  - The V2 region may have a DIFFERENT stage band at PRE, or no presently
    drawable stage. V2.4A fixed-band visits are not silently relabeled
    executable contacts of the then-current stage.
  - POST measures research excursions, NOT executable fills, TP/SL or PnL.

No imports of the live engine, Streamlit, Redis, HLL or external APIs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import DAY, M, asof_rows, digest, iso, load_snapshot
from btc_multiswing_structural_confluence_v2 import construct_regions

VERSION = 'btc-multiswing-arrival-lab-causal-v1'
PARENT_VERSION = 'btc-multiswing-information-universe-v2.4a'
DEFAULT_PARENT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_information_universe_v24a')
DEFAULT_OUTPUT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_arrival_lab_v1')
HORIZONS = (5, 15, 30, 60, 180)
PARENT_FILES = ('regions_v24.csv', 'visits_v24.csv', 'post_horizons_v24.csv',
                'overlaps_v24.csv', 'summary_v24.json')
# Every exported column is assigned a side of the PRE/POST boundary.
PRE_COLUMNS = [
    'event_id','region_id','visit_number','side','event_source',
    'pre_cutoff_ts','pre_cutoff_utc','source_contact_open_ts',
    'birth_confirmed_ts','region_age_minutes',
    'birth_band_low','birth_band_high','birth_center','birth_width_pct',
    'birth_extrema','stage_present_at_pre','current_stage_confirmed_ts',
    'current_stage_extrema','current_stage_low','current_stage_high',
    'current_stage_width_pct','minutes_since_stage_confirmation',
    'reference_mismatch_possible','pre_close','pre_position_vs_birth_band',
    'pre_distance_to_birth_band_pct','pre_distance_signed_center_pct',
    'pre_position_vs_current_stage','pre_distance_to_current_stage_pct',
    'pre_return_5m_pct','pre_return_15m_pct','pre_return_30m_pct',
    'pre_return_60m_pct','pre_range_15m_pct','pre_range_60m_pct',
    'pre_mean_true_range_60m_pct','pre_volume_ratio_5m_vs_60m',
    'prior_visits_closed_asof','prior_touch_minutes_known_asof',
    'other_stage_regions_known_asof','opposite_stage_regions_known_asof',
    'pre_data_status','execution_eligibility','causal_note',
]
POST_COLUMNS = [
    'event_id','region_id','visit_number','side','source_contact_open_ts',
    'horizon_minutes','evaluation_from_ts','observed_minutes','status',
    'reference_pre_close','post_last_close','post_close_return_pct',
    'max_up_from_preclose_pct','max_down_from_preclose_pct',
    'side_proxy_favorable_excursion_pct','side_proxy_adverse_excursion_pct',
    'first_touch_intersects_pre_stage','last_known_ts',
]


def _required_columns(df: pd.DataFrame, fields: set[str], label: str) -> None:
    missing = fields - set(df.columns)
    if missing:
        raise ValueError(f'{label}_SCHEMA_MISSING: {sorted(missing)}')


def _parent_run(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict, dict]:
    """Reject stale/mutated parent inputs; do not silently recompute a sample."""
    if not path.is_dir():
        raise FileNotFoundError(f'V24A_PARENT_NOT_FOUND: {path}')
    manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    summary = json.loads((path/'summary_v24.json').read_text(encoding='utf-8'))
    if (manifest.get('version') != PARENT_VERSION or
            summary.get('version') != PARENT_VERSION or
            manifest.get('immutable_run') is not True or
            manifest.get('no_live_imports_or_network') is not True):
        raise ValueError('ARRIVAL_PARENT_VERSION_OR_INTEGRITY_MISMATCH')
    for name in PARENT_FILES:
        if digest(path/name) != manifest.get('sha256', {}).get(name):
            raise ValueError('ARRIVAL_PARENT_FILE_SHA256_MISMATCH: '+name)
    cfg=manifest.get('config',{})
    if (manifest.get('source_provenance') != summary.get('source_provenance') or
            any(cfg.get(key)!=summary.get(key) for key in
                ('cutoff','timeframes','side','geometry','gap_minutes','horizons_minutes'))):
        raise ValueError('ARRIVAL_PARENT_MANIFEST_SUMMARY_MISMATCH')
    regions = pd.read_csv(path/'regions_v24.csv')
    visits = pd.read_csv(path/'visits_v24.csv')
    _required_columns(regions, {'region_id','side','birth_known_ts','band_low_birth',
        'band_high_birth','extrema_at_birth','visits'},'REGIONS_V24')
    _required_columns(visits, {'region_id','visit_number','first_touch_ts',
        'first_known_ts','end_known_ts','touch_minutes','gap_minutes'}, 'VISITS_V24')
    if regions.region_id.duplicated().any() or visits.duplicated(['region_id','visit_number']).any():
        raise ValueError('ARRIVAL_DUPLICATED_PARENT_ID')
    if not visits.region_id.isin(regions.region_id).all():
        raise ValueError('ARRIVAL_UNKNOWN_PARENT_REGION_ID')
    if int(regions.visits.sum()) != len(visits):
        raise ValueError('ARRIVAL_PARENT_TOTALS_MISMATCH')
    return regions, visits, manifest, summary


def _validate_and_reconstruct(catalog, source, regions_v24, visits_v24, parent):
    cfg = parent['config']
    prov = parent['source_provenance']
    if (prov.get('source_sha256') != source['source_sha256'] or
            prov.get('catalog_sha256') != source['catalog_sha256'] or
            prov.get('source_hll_parity') != source['source_hll_parity'] or
            int(prov.get('start',-1)) != source['start'] or
            int(prov.get('end',-1)) != source['end'] or
            int(prov.get('warmup_start',-1)) != source['warmup_start'] or
            int(cfg.get('cutoff',-1)) != source['end']):
        raise ValueError('ARRIVAL_FREEZE_PARENT_PROVENANCE_MISMATCH')
    g = cfg['geometry']
    known = asof_rows(catalog, source['end'], tuple(cfg['timeframes']), cfg['side'])
    regions, stages, _ = construct_regions(
        known, source['end'], tolerance_pct=float(g['tolerance_pct']),
        lookback_days=int(g['lookback_days']),
        min_separation_minutes=int(g['separation_minutes']), min_extrema=2)
    if (len(regions) != len(regions_v24) or
            set(regions.region_id) != set(regions_v24.region_id)):
        raise ValueError('ARRIVAL_V2_REGION_CENSUS_DRIFT')
    # Simultaneous confirmations: choose highest count at first known minute,
    # just like the V2.4A birth_stage_universe parent.
    earliest = (stages.sort_values(['known_from','independent_extrema','region_id'],
               ascending=[True,False,True],kind='mergesort')
                .drop_duplicates('region_id',keep='first').set_index('region_id'))
    pr = regions_v24.set_index('region_id')
    for rid in pr.index:
        actual = earliest.loc[rid]
        r = pr.loc[rid]
        if (int(actual.known_from) != int(r.birth_known_ts) or
            str(actual.side) != str(r.side) or
            abs(float(actual.band_low)-float(r.band_low_birth))>1e-7 or
            abs(float(actual.band_high)-float(r.band_high_birth))>1e-7 or
            int(actual.independent_extrema)!=int(r.extrema_at_birth)):
            raise ValueError('ARRIVAL_BIRTH_GEOMETRY_DRIFT: '+rid)
    if ((visits_v24.first_touch_ts % M != 0).any() or
        (visits_v24.first_known_ts != visits_v24.first_touch_ts + M).any() or
        (visits_v24.gap_minutes != cfg['gap_minutes']).any()):
        raise ValueError('ARRIVAL_PARENT_TOUCH_TIMELINE_INVALID')
    birth_by_visit = visits_v24.merge(regions_v24[['region_id','birth_known_ts']],
                                     on='region_id',validate='many_to_one')
    if ((birth_by_visit.first_touch_ts < birth_by_visit.birth_known_ts).any() or
            (birth_by_visit.first_touch_ts >= source['end']).any()):
        raise ValueError('ARRIVAL_TOUCH_BEFORE_BIRTH_OR_AFTER_FREEZE')
    return stages


def _position(value: float, low: float, high: float) -> tuple[str,float]:
    mid = (low+high)/2
    if value > high:
        return 'ABOVE', (value-high)/mid*100
    if value < low:
        return 'BELOW', (low-value)/mid*100
    return 'INSIDE',0.0


def _last_confirmed_stage(group: pd.DataFrame, cutoff: int, *, max_age_minutes: int):
    """A stage is drawable ONLY while its known_from <= PRE < known_until."""
    # V2's stage intervals may run until a future confirmation. Enforce the
    # chosen visual TTL separately: future confirmation cannot extend PRE age.
    subset = group.loc[(group.known_from <= cutoff) &
                       (group.known_until > cutoff) &
                       (group.known_from + max_age_minutes*M > cutoff)]
    if subset.empty:
        return None
    # For simultaneous timestamps, latest known independent extrema wins.
    return subset.sort_values(['known_from','independent_extrema'],
                              ascending=[False,False],kind='mergesort').iloc[0]


def _pre_features(closes, highs, lows, volumes, i: int, low: float, high: float):
    """Exactly bars with open time strictly < contact_open_ts."""
    if i < 1:
        raise ValueError('ARRIVAL_NO_PRE_CLOSED_MINUTE')
    close = float(closes[i-1])
    feat = {'pre_close':close}
    state, distance = _position(close,low,high)
    feat['pre_position_vs_birth_band'] = state
    feat['pre_distance_to_birth_band_pct'] = distance
    feat['pre_distance_signed_center_pct'] = (close-(low+high)/2)/((low+high)/2)*100
    for minutes in (5,15,30,60):
        feat[f'pre_return_{minutes}m_pct'] = (
            (close/float(closes[i-1-minutes])-1)*100 if i >= minutes+1 else None)
    for minutes in (15,60):
        feat[f'pre_range_{minutes}m_pct'] = (
            (float(np.max(highs[i-minutes:i]))-float(np.min(lows[i-minutes:i])))
            /close*100 if i >= minutes else None)
    if i >= 61:
        prev_close=closes[i-61:i-1]
        hi=highs[i-60:i]
        lo=lows[i-60:i]
        tr=np.maximum(hi-lo,np.maximum(abs(hi-prev_close),abs(lo-prev_close)))
        feat['pre_mean_true_range_60m_pct']=float(np.mean(tr))/close*100
    else:
        feat['pre_mean_true_range_60m_pct']=None
    if volumes is None or i < 60:
        feat['pre_volume_ratio_5m_vs_60m']=None
    else:
        baseline=float(np.mean(volumes[i-60:i]))
        recent=float(np.mean(volumes[i-5:i]))
        feat['pre_volume_ratio_5m_vs_60m']=(recent/baseline if baseline>0 else None)
    return feat


def _post_horizon(closes, highs, lows, i:int, horizon:int, cutoff_i:int,
                  pre_close:float, side:str):
    available = max(0, min(horizon, cutoff_i-i))
    full = available == horizon
    if not full:
        # No performance is exported on cutoff-censored observations.
        return available, 'CUTOFF_CENSORED', None, None, None, None, None, None
    hi = float(np.max(highs[i:i+horizon]))
    lo = float(np.min(lows[i:i+horizon]))
    last = float(closes[i+horizon-1])
    up = max(0., (hi-pre_close)/pre_close*100)
    down = max(0., (pre_close-lo)/pre_close*100)
    fav = up if side == 'LOW' else down
    adv = down if side == 'LOW' else up
    return available,'FULL_HORIZON',last,(last/pre_close-1)*100,up,down,fav,adv


def build_arrival_dataset(full:pd.DataFrame, regions:pd.DataFrame,
                          visits:pd.DataFrame, stages:pd.DataFrame,
                          source_start:int, source_end:int,
                          *, horizons:tuple[int,...]=HORIZONS,
                          max_stage_age_minutes:int=7*1440):
    """Pure deterministic builder; no file IO, no network, no detector mutation.

    Parent visits are a RETROSPECTIVE index; the current candle is exclusively
    consulted in POST outcomes and never in PRE features.
    """
    if (not horizons or any(h not in HORIZONS for h in horizons) or
            len(set(horizons))!=len(horizons) or
            type(max_stage_age_minutes) is not int or max_stage_age_minutes<=0):
        raise ValueError('ARRIVAL_UNSUPPORTED_HORIZON')
    _required_columns(full, {'timestamp','open','high','low','close'},'FREEZE_1M')
    _required_columns(regions, {'region_id','side','birth_known_ts','band_low_birth',
                               'band_high_birth','extrema_at_birth'},'REGIONS_V24')
    _required_columns(visits, {'region_id','visit_number','first_touch_ts',
                    'first_known_ts','end_known_ts','touch_minutes'},'VISITS_V24')
    _required_columns(stages, {'region_id','side','known_from','known_until',
                      'independent_extrema','band_low','band_high'},'STAGES_V2')
    if source_start%M or source_end%M or source_start>=source_end:
        raise ValueError('ARRIVAL_INVALID_FREEZE_LIMITS')
    frame=full.sort_values('timestamp',kind='mergesort').reset_index(drop=True)
    stamps=frame.timestamp.to_numpy(dtype=np.int64)
    if (stamps.size<2 or (np.diff(stamps)!=M).any() or
            stamps[-1]+M!=source_end or stamps[0]>source_start-M):
        raise ValueError('ARRIVAL_INCOMPLETE_FROZEN_1M')
    opens=frame.open.to_numpy(dtype=float)
    highs=frame.high.to_numpy(dtype=float)
    lows=frame.low.to_numpy(dtype=float)
    closes=frame.close.to_numpy(dtype=float)
    if (not np.isfinite(np.column_stack([opens,highs,lows,closes])).all() or
        (np.minimum.reduce([opens,highs,lows,closes])<=0).any() or
        (highs < np.maximum.reduce([opens,lows,closes])).any() or
        (lows > np.minimum.reduce([opens,highs,closes])).any()):
        raise ValueError('ARRIVAL_INVALID_FROZEN_OHLC')
    volumes=None
    if 'volume' in frame.columns:
        volumes=pd.to_numeric(frame.volume,errors='coerce').to_numpy(dtype=float)
        if not np.isfinite(volumes).all() or (volumes<0).any():
            raise ValueError('ARRIVAL_INVALID_VOLUME')
    start_of_bars=int(stamps[0])
    def bar_index(ts:int):
        idx=(int(ts)-start_of_bars)//M
        if (ts-start_of_bars)%M or idx<1 or idx>=len(frame):
            raise ValueError('ARRIVAL_INVALID_CONTACT_BAR_LOCATION')
        return idx
    cutoff_i=bar_index(source_end-M)+1
    regions_idx=regions.set_index('region_id')
    if regions_idx.index.duplicated().any():
        raise ValueError('ARRIVAL_DUPLICATE_REGION_ID')
    stage_groups={str(rid):g.sort_values(['known_from','independent_extrema'],
                   kind='mergesort') for rid,g in stages.groupby('region_id')}
    if set(regions_idx.index)!=set(stage_groups):
        raise ValueError('ARRIVAL_STAGE_CENSUS_NOT_MATCHING_REGIONS')
    by_visit={str(rid):g.sort_values(['first_touch_ts','visit_number'],kind='mergesort')
              for rid,g in visits.groupby('region_id')}
    if set(by_visit)-set(regions_idx.index):
        raise ValueError('ARRIVAL_VISIT_WITHOUT_REGION')
    results_pre=[]
    results_post=[]
    for region in regions.itertuples(index=False):
        rid=str(region.region_id)
        low,high=float(region.band_low_birth),float(region.band_high_birth)
        side=str(region.side)
        birth=int(region.birth_known_ts)
        if low<=0 or high<low or side not in ('LOW','HIGH') or birth%M:
            raise ValueError('ARRIVAL_INVALID_BIRTH_GEOMETRY')
        if rid not in by_visit:
            continue
        g=by_visit[rid]
        if g.duplicated(['visit_number']).any() or (g.first_touch_ts.diff().dropna()<=0).any():
            raise ValueError('ARRIVAL_DUPLICATE_OR_UNORDERED_VISITS')
        previous=[]
        for visit in g.itertuples(index=False):
            t=int(visit.first_touch_ts)
            if t<birth or t<source_start or t>=source_end or t%M or int(visit.first_known_ts)!=t+M:
                raise ValueError('ARRIVAL_INVALID_FIRST_TOUCH_TIMELINE')
            i=bar_index(t)
            # Retrospective consistency guard ONLY. This bar's OHLC is never
            # copied into PRE; its existence is used to reject corrupt census.
            if highs[i] < low or lows[i] > high:
                raise ValueError('ARRIVAL_PARENT_FIRST_TOUCH_NOT_IN_BIRTH_BAND')
            pre=_pre_features(closes,highs,lows,volumes,i,low,high)
            current=_last_confirmed_stage(stage_groups[rid],t,
                          max_age_minutes=max_stage_age_minutes)
            pre_usable=current is not None
            if pre_usable:
                clo,chi=float(current.band_low),float(current.band_high)
                pos,distance=_position(pre['pre_close'],clo,chi)
                current_known=int(current.known_from)
                current_extrema=int(current.independent_extrema)
            else:
                clo=chi=pos=distance=current_known=current_extrema=None
            active_other=stages.loc[(stages.region_id!=rid) &
                  (stages.known_from<=t) & (stages.known_until>t) &
                  (stages.known_from + max_stage_age_minutes*M>t)]
            active_other=active_other.sort_values(['known_from','independent_extrema'],
                  ascending=[False,False],kind='mergesort').drop_duplicates('region_id')
            known_prior=[p for p in previous if p['end_known_ts'] is not None and
                         p['end_known_ts']<=t]
            event_id=f'{rid}|V{int(visit.visit_number)}|{t}'
            if pre_usable and int(current_known)>t:
                raise ValueError('ARRIVAL_STAGE_LOOKAHEAD')
            results_pre.append({
                'event_id':event_id,'region_id':rid,'visit_number':int(visit.visit_number),
                'side':side,'event_source':'RETROSPECTIVE_V24A_BIRTH_FIXED_VISIT',
                'pre_cutoff_ts':t,'pre_cutoff_utc':iso(t),
                'source_contact_open_ts':t,
                'birth_confirmed_ts':birth,'region_age_minutes':(t-birth)//M,
                'birth_band_low':low,'birth_band_high':high,
                'birth_center':(low+high)/2,
                'birth_width_pct':(high-low)/((low+high)/2)*100,
                'birth_extrema':int(region.extrema_at_birth),
                'stage_present_at_pre':bool(pre_usable),
                'current_stage_confirmed_ts':current_known,
                'current_stage_extrema':current_extrema,
                'current_stage_low':clo,'current_stage_high':chi,
                'current_stage_width_pct':((chi-clo)/((chi+clo)/2)*100
                                               if pre_usable else None),
                'minutes_since_stage_confirmation':((t-current_known)//M
                                               if pre_usable else None),
                'reference_mismatch_possible':(not pre_usable or
                            abs(low-clo)>1e-7 or abs(high-chi)>1e-7),
                **pre,
                'pre_position_vs_current_stage':pos,
                'pre_distance_to_current_stage_pct':distance,
                'prior_visits_closed_asof':len(known_prior),
                'prior_touch_minutes_known_asof':sum(x['touch_minutes'] for x in known_prior),
                'other_stage_regions_known_asof':len(active_other),
                'opposite_stage_regions_known_asof':int(active_other.side.ne(side).sum()),
                'pre_data_status':'CLOSED_1M_CONTIGUOUS',
                'execution_eligibility':'STRUCTURAL_STAGE_PRESENT_NOT_TRADE_SIGNAL'
                    if pre_usable else 'RETROSPECTIVE_BIRTH_BAND_NO_STAGE_AT_PRE',
                'causal_note':'Event located retrospectively; no touch OHLC/visit outcome in PRE',
            })
            for h in horizons:
                observed,status,last,ret,up,down,fav,adv=_post_horizon(
                     closes,highs,lows,i,h,cutoff_i,float(pre['pre_close']),side)
                current_touch=(bool(highs[i]>=clo and lows[i]<=chi)
                               if pre_usable and status=='FULL_HORIZON' else None)
                results_post.append({
                    'event_id':event_id,'region_id':rid,'visit_number':int(visit.visit_number),
                    'side':side,'source_contact_open_ts':t,
                    'horizon_minutes':h,'evaluation_from_ts':t,
                    'observed_minutes':observed,'status':status,
                    'reference_pre_close':float(pre['pre_close']),
                    'post_last_close':last,'post_close_return_pct':ret,
                    'max_up_from_preclose_pct':up,'max_down_from_preclose_pct':down,
                    'side_proxy_favorable_excursion_pct':fav,
                    'side_proxy_adverse_excursion_pct':adv,
                    'first_touch_intersects_pre_stage':current_touch,
                    'last_known_ts':t+observed*M,
                })
            end_known = None if pd.isna(visit.end_known_ts) else int(visit.end_known_ts)
            if end_known is not None and end_known<=t:
                raise ValueError('ARRIVAL_PARENT_VISIT_END_BEFORE_BEGIN')
            previous.append({'end_known_ts':end_known,
                             'touch_minutes':int(visit.touch_minutes)})
    pre_df=pd.DataFrame(results_pre,columns=PRE_COLUMNS)
    post_df=pd.DataFrame(results_post,columns=POST_COLUMNS)
    if (len(pre_df)!=len(visits) or
        len(post_df)!=len(visits)*len(horizons) or
        pre_df.event_id.duplicated().any() or
        post_df.duplicated(['event_id','horizon_minutes']).any()):
        raise ValueError('ARRIVAL_CENSUS_NOT_COMPLETE')
    if (not pre_df.empty and
            (pre_df.pre_cutoff_ts!=pre_df.source_contact_open_ts).any()):
        raise ValueError('ARRIVAL_PRE_OPEN_MISMATCH')
    censored=post_df.status.eq('CUTOFF_CENSORED')
    if censored.any() and post_df.loc[censored,[
        'post_close_return_pct','side_proxy_favorable_excursion_pct',
        'side_proxy_adverse_excursion_pct','first_touch_intersects_pre_stage']].notna().any().any():
        raise ValueError('ARRIVAL_CENSORED_PERFORMANCE_NOT_NULL')
    pre_df=pre_df.sort_values(['pre_cutoff_ts','region_id','visit_number'],
                              kind='mergesort').reset_index(drop=True)
    post_df=post_df.sort_values(['evaluation_from_ts','region_id','visit_number',
                                'horizon_minutes'],kind='mergesort').reset_index(drop=True)
    summary={
        'version':VERSION,
        'freeze_start':source_start,'freeze_end_exclusive':source_end,
        'birth_regions':len(regions),'visits_in_parent':len(visits),
        'arrival_pre_rows':len(pre_df),'post_outcome_rows':len(post_df),
        'structural_stage_present_pre':int(pre_df.stage_present_at_pre.sum()),
        'retrospective_no_current_stage_pre':int((~pre_df.stage_present_at_pre).sum()),
        'reference_mismatch_possible':int(pre_df.reference_mismatch_possible.sum()),
        'visits_more_than_7d_from_birth':int((pre_df.region_age_minutes>7*1440).sum()),
        'post_full':int(post_df.status.eq('FULL_HORIZON').sum()),
        'post_cutoff_censored':int(censored.sum()),
        'full_by_horizon':{str(h):int(((post_df.horizon_minutes==h)&
                   post_df.status.eq('FULL_HORIZON')).sum()) for h in horizons},
        'horizons_minutes':list(horizons),'has_volume_column':volumes is not None,
        'visual_stage_ttl_minutes':max_stage_age_minutes,
        'research_only':True,'no_live':True,
        'limitations':[
          'Arrival events are located retrospectively from V2.4A; future detection is NOT a PRE feature.',
          'V2.4A visits touch FIXED BIRTH BANDS, not necessarily the evolving current V2 stage band.',
          'Stage present is geometric/reference status, NOT validated active support/resistance.',
          'Touching OHLC and favorable/adverse excursions are in POST only; no trades or fills.',
          'HIGH->short and LOW->long excursions are UNEXECUTED DIRECTIONAL PROXIES only.',
          'Overlapping visits and bands are dependent; no rates or significance claimed.',
          'All data are BTC-only in-sample; HLL/REACTION/volatility-adjusted execution not yet joined.',
        ],
    }
    return pre_df,post_df,summary


def _write_immutable(dest:Path, pre:pd.DataFrame, post:pd.DataFrame, summary:dict,
                     parent_manifest:dict, parent_path:Path, source:dict):
    if dest.exists():
        raise FileExistsError(f'ARRIVAL_OUTPUT_EXISTS_IMMUTABLE: {dest}')
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.arrival_v1_tmp_',dir=dest.parent))
    try:
        pre.to_csv(tmp/'arrival_pre_v1.csv',index=False,float_format='%.12g')
        post.to_csv(tmp/'arrival_post_outcomes_v1.csv',index=False,float_format='%.12g')
        summary_obj={**summary,'source_provenance':source,
             'parent_manifest_sha256':digest(parent_path/'manifest.json')}
        (tmp/'summary_v1.json').write_text(json.dumps(summary_obj,indent=2,
                                    ensure_ascii=False,sort_keys=True)+'\n',encoding='utf-8')
        payloads=('arrival_pre_v1.csv','arrival_post_outcomes_v1.csv','summary_v1.json')
        manifest={'version':VERSION,'immutable_run':True,
            'research_only':True,'no_live':True,
            'parent_v24a':str(parent_path),'parent_version':parent_manifest['version'],
            'parent_manifest_sha256':digest(parent_path/'manifest.json'),
            'source_provenance':source,'counts':{
                'arrivals':len(pre),'post_outcomes':len(post)},
            'sha256':{p:digest(tmp/p) for p in payloads}}
        (tmp/'manifest.json').write_text(json.dumps(manifest,indent=2,
                                  ensure_ascii=False,sort_keys=True)+'\n',encoding='utf-8')
        os.rename(tmp,dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    parser.add_argument('--project-root',type=Path,default=Path('.'))
    parser.add_argument('--parent',type=Path,default=DEFAULT_PARENT)
    parser.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args(argv)
    root=args.project_root.resolve()
    parent=(args.parent if args.parent.is_absolute() else root/args.parent)
    dest=(args.output_dir if args.output_dir.is_absolute() else root/args.output_dir)
    print('ARRIVAL_V1_PLAN',json.dumps({'mode':'RUN' if args.run else 'PLAN',
        'project_root':str(root),'parent_v24a':str(parent),
        'output_dir':str(dest),'horizons_minutes':list(HORIZONS),
        'read_only_freeze':True,'no_live':True},sort_keys=True),flush=True)
    if args.plan:
        return 0
    if dest.exists():
        raise FileExistsError(f'ARRIVAL_OUTPUT_EXISTS_IMMUTABLE: {dest}')
    full,catalog,source=load_snapshot(root)
    regions_v24,visits_v24,parent_manifest,_=_parent_run(parent)
    stages=_validate_and_reconstruct(catalog,source,regions_v24,visits_v24,parent_manifest)
    pre,post,summary=build_arrival_dataset(full,regions_v24,visits_v24,stages,
                   source['start'],source['end'],
                   max_stage_age_minutes=int(parent_manifest['config']['geometry']['lookback_days'])*1440)
    _write_immutable(dest,pre,post,summary,parent_manifest,parent,source)
    print('ARRIVAL_V1_FINISHED',json.dumps({'output':str(dest),
           'counts':{k:v for k,v in summary.items() if k in (
               'birth_regions','arrival_pre_rows','post_outcome_rows',
               'structural_stage_present_pre','retrospective_no_current_stage_pre',
               'reference_mismatch_possible','post_full','post_cutoff_censored')}},
           sort_keys=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
