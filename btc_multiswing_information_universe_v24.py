#!/usr/bin/env python3
"""Multi-Swing V2.4A: universal BTC 60D information *coverage* audit (OFFLINE).

This is NOT a structural-quality score, predictive evaluation or LIVE feed.
Every V2 region is evaluated using its *birth-stage* immutable band, NOT the
retrospectively final band. A birth-stage is knowable at its known_from time;
later 1m interactions and post-visit outcomes are kept in separate tables.

Reads only the hash-verified frozen V1.1 six-TF catalog / BTC 1m dataset.
Reuses the unchanged V2, V2.2 and V2.3 semantics, including their censoring.
Never imports HLL, LIVE engine, Redis or a network client.

Outputs, created only with --run in a new directory:
  regions_v24.csv        one row per V2 birth; descriptive PRE features + coverage
  visits_v24.csv         all V2.3 visits, not a sample of winners
  post_horizons_v24.csv  all requested post-visit horizons with censor state
  overlaps_v24.csv      older/equal-birth geometrical overlap, no posterior bands
  summary_v24.json      universe counts, denominators and data provenance
  manifest.json         file SHA256 and run configuration
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from btc_multiswing_60d_explorer_v1 import (
    M, TF_MINUTES, asof_rows, digest, iso, load_snapshot,
)
from btc_multiswing_structural_confluence_v2 import construct_regions
from btc_multiswing_structural_visits_v23 import (
    ALLOWED_GAPS, ALLOWED_HORIZONS, audit_structural_visits,
)

VERSION = 'btc-multiswing-information-universe-v2.4a'
DEFAULT_TFS = ('15m', '30m', '1h', '4h')
DEFAULT_HORIZONS = (60, 240, 720, 1440, 2880)
DEFAULT_REPORT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_information_universe_v24a')
MIN_LONG_EXPOSURE = 2880  # 48 hours observation for a NO-VISIT descriptor
REGION_COLUMNS = [
    'region_id', 'side', 'birth_known_ts', 'birth_utc', 'band_low_birth',
    'band_high_birth', 'center_birth', 'band_width_pct_birth',
    'extrema_at_birth', 'birth_stage_is_fixed', 'source_region_born_ts',
    'followup_closed_minutes', 'followup_days', 'observation_status',
    'contact_episodes', 'visits', 'visits_closed', 'visits_open',
    'touch_minutes', 'visit_arrived_above', 'visit_arrived_below',
    'visit_arrived_inside_or_unknown', 'visit_exited_above',
    'visit_exited_below', 'visit_exited_inside_or_unknown',
    'full_outcomes_60m', 'full_outcomes_240m', 'full_outcomes_720m',
    'full_outcomes_1440m', 'full_outcomes_2880m',
    'censored_outcomes', 'prior_near_regions_at_birth',
    'prior_overlap_regions_at_birth', 'prior_near_same_side_at_birth',
    'evidence_status',
]
OVERLAP_COLUMNS = [
    'region_id', 'preexisting_region_id', 'region_side', 'preexisting_side',
    'region_birth_ts', 'preexisting_birth_ts', 'center_distance_pct',
    'intersection_width', 'iou', 'within_center_tolerance',
    'band_intersects',
]


def _guard_settings(timeframes: tuple[str, ...], side: str, tolerance_pct: float,
                    lookback_days: int, separation_minutes: int,
                    gap_minutes: int, horizons: tuple[int, ...]) -> None:
    if (not timeframes or len(set(timeframes)) != len(timeframes)
            or any(tf not in TF_MINUTES for tf in timeframes)):
        raise ValueError('INVALID_V24_TIMEFRAMES')
    if side not in ('BOTH', 'HIGH', 'LOW'):
        raise ValueError('INVALID_V24_SIDE')
    if (not 0 < tolerance_pct <= 2 or not 1 <= lookback_days <= 60
            or not 1 <= separation_minutes <= 1440):
        raise ValueError('INVALID_V24_GEOMETRY')
    if gap_minutes not in ALLOWED_GAPS:
        raise ValueError('INVALID_V24_VISIT_GAP')
    if not horizons or len(set(horizons)) != len(horizons) or any(
            horizon not in ALLOWED_HORIZONS for horizon in horizons):
        raise ValueError('INVALID_V24_HORIZONS')


def birth_stage_universe(regions: pd.DataFrame, stages: pd.DataFrame,
                         cutoff: int) -> pd.DataFrame:
    """Extract strictly the first known stage per V2 region.

    For simultaneous confirmations within the same closed minute, the last
    same-minute stage (maximum extrema) is already knowable at that minute.
    No `region.center`, final `region.members` or final TF set is copied.
    """
    cols = ['region_id', 'side', 'birth_known_ts', 'band_low_birth',
            'band_high_birth', 'extrema_at_birth', 'source_region_born_ts']
    if regions.empty:
        if not stages.empty:
            raise ValueError('ORPHAN_STAGE_WITHOUT_REGION')
        return pd.DataFrame(columns=cols)
    required_r = {'region_id', 'side', 'born_ts'}
    required_s = {'region_id', 'side', 'known_from', 'band_low',
                  'band_high', 'independent_extrema'}
    if not required_r.issubset(regions.columns) or not required_s.issubset(stages.columns):
        raise ValueError('V24_MISSING_V2_STAGE_COLUMNS')
    if regions.region_id.duplicated().any() or stages.empty:
        raise ValueError('V24_INVALID_V2_UNIVERSE')
    if (stages['known_from'].isna().any() or
            (pd.to_numeric(stages.known_from) > cutoff).any()):
        raise ValueError('V24_FUTURE_REGION_STAGE')
    first = (stages.sort_values(['known_from', 'independent_extrema', 'region_id'],
                               ascending=[True, False, True], kind='mergesort')
             .drop_duplicates('region_id', keep='first').copy())
    if set(first.region_id) != set(regions.region_id):
        raise ValueError('V24_MISSING_BIRTH_STAGE')
    meta = regions[['region_id', 'side', 'born_ts']].rename(
        columns={'side':'region_side', 'born_ts':'source_region_born_ts'})
    born = first.merge(meta, on='region_id', validate='one_to_one')
    if ((born.side != born.region_side).any()
            or (born.known_from != born.source_region_born_ts).any()):
        raise ValueError('V24_CAUSAL_BIRTH_CONFLICT')
    if ((born.band_low <= 0).any() or (born.band_high < born.band_low).any()
            or (born.independent_extrema < 2).any()):
        raise ValueError('V24_INVALID_BIRTH_GEOMETRY')
    born = born.rename(columns={
        'known_from':'birth_known_ts', 'band_low':'band_low_birth',
        'band_high':'band_high_birth',
        'independent_extrema':'extrema_at_birth',
    })
    return (born[cols].sort_values(['birth_known_ts','region_id'],
                                  kind='mergesort').reset_index(drop=True))


def _birth_overlap(births: pd.DataFrame, tolerance_pct: float):
    """Compare only bands that were already born at the inspected region's birth.

    Both sides are retained because overlapping HIGH/LOW is a dependency,
    not an independent repeated observation. Prices are birth-stage snapshots
    (not previous region's later-finalized geometry).
    """
    rows = []
    summary = {}
    prior = []
    for r in births.itertuples(index=False):
        near_count = overlap_count = near_same_side = 0
        low, high = float(r.band_low_birth), float(r.band_high_birth)
        center = (low + high) / 2
        for p in prior:
            p_low, p_high, p_center = p['low'], p['high'], p['center']
            dist = abs(center - p_center) / ((center + p_center)/2)*100
            intersect = max(low, p_low) <= min(high, p_high)
            near = dist <= tolerance_pct + 1e-12
            if not (near or intersect):
                continue
            overlap = max(0., min(high,p_high)-max(low,p_low))
            union = max(high,p_high)-min(low,p_low)
            iou = overlap/union if union > 0 else (1.0 if low == p_low else 0.0)
            near_count += int(near)
            overlap_count += int(intersect)
            near_same_side += int(near and r.side == p['side'])
            rows.append({
                'region_id':r.region_id, 'preexisting_region_id':p['id'],
                'region_side':r.side, 'preexisting_side':p['side'],
                'region_birth_ts':int(r.birth_known_ts),
                'preexisting_birth_ts':p['born'],
                'center_distance_pct':dist, 'intersection_width':overlap,
                'iou':iou, 'within_center_tolerance':near,
                'band_intersects':intersect,
            })
        summary[str(r.region_id)] = (near_count, overlap_count, near_same_side)
        prior.append({'id':str(r.region_id), 'side':str(r.side),
                      'born':int(r.birth_known_ts), 'low':low,
                      'high':high,'center':center})
    return pd.DataFrame(rows, columns=OVERLAP_COLUMNS), summary


def _classified_followup(followup: int, visits: pd.DataFrame) -> str:
    if followup <= 0:
        return 'NO_CLOSED_1M_FOLLOWUP'
    if visits.empty:
        return ('NO_CONTACT_WITH_48H_EXPOSURE' if followup >= MIN_LONG_EXPOSURE
                else 'NO_CONTACT_SHORT_EXPOSURE')
    if visits['status'].eq('OPEN_AT_CUTOFF').all():
        return 'ONLY_OPEN_VISITS'
    return 'HAS_CLOSED_VISIT'


def audit_universe(full: pd.DataFrame, catalog: pd.DataFrame, cutoff: int,
                   *, timeframes: tuple[str, ...] = DEFAULT_TFS,
                   side: str = 'BOTH', tolerance_pct: float = 0.20,
                   lookback_days: int = 7, separation_minutes: int = 60,
                   gap_minutes: int = 30,
                   horizons: tuple[int, ...] = DEFAULT_HORIZONS,
                   progress=None):
    """Build one deterministic census. Expects a verified frozen full/catalog.

    Each selected region is kept even if it has no contacts or too little
    observable followup. All V2.3 outcomes are POST data, not PRE features.
    The final region's retrospective `independent_extrema` is never a feature.
    """
    _guard_settings(timeframes,side,tolerance_pct,lookback_days,
                    separation_minutes,gap_minutes,horizons)
    cutoff = int(cutoff)
    if cutoff % M or full.empty or catalog.empty:
        raise ValueError('V24_INVALID_SNAPSHOT_OR_CUTOFF')
    if not {'timestamp','open','high','low','close'}.issubset(full.columns):
        raise ValueError('V24_MISSING_1M_SCHEMA')
    if cutoff < int(full.timestamp.min()) + M or cutoff > int(full.timestamp.max()) + M:
        raise ValueError('V24_CUTOFF_OUTSIDE_FREEZE')
    known = asof_rows(catalog,cutoff,timeframes,side)
    regions, stages, v2_stats = construct_regions(
        known,cutoff,tolerance_pct=tolerance_pct,
        lookback_days=lookback_days,
        min_separation_minutes=separation_minutes, min_extrema=2,
    )
    births = birth_stage_universe(regions,stages,cutoff)
    overlaps, near_by_id = _birth_overlap(births,tolerance_pct)
    region_rows, all_visits, all_outcomes = [], [], []
    for index, r in enumerate(births.itertuples(index=False),1):
        anchor = int(r.birth_known_ts)
        followup = (cutoff - anchor)//M
        stage = {'region_id':r.region_id,'side':r.side,'known_from':anchor,
                 'band_low':float(r.band_low_birth),
                 'band_high':float(r.band_high_birth)}
        if followup > 0:
            visits, outcomes, vsummary = audit_structural_visits(
                full,stage,cutoff,gap_minutes=gap_minutes,horizons=horizons)
        else:
            # Not omitted: right-censored at birth, before the first closed bar.
            visits = pd.DataFrame()
            outcomes = pd.DataFrame()
            vsummary = {'contact_episodes_v22':0,'visits_closed':0,'visits_open':0}
        if not visits.empty:
            all_visits.append(visits)
        if not outcomes.empty:
            all_outcomes.append(outcomes)
        if not outcomes.empty and not set(outcomes.horizon_minutes).issubset(horizons):
            raise ValueError('V24_OUTCOME_HORIZON_CONFLICT')
        full_horizons = {int(h):int(((outcomes.horizon_minutes == h) &
                            outcomes.status.eq('FULL_HORIZON')).sum())
                         if not outcomes.empty else 0
                         for h in DEFAULT_HORIZONS}
        near, inter, near_side = near_by_id[str(r.region_id)]
        contact_count = int(vsummary['contact_episodes_v22'])
        closed = int(vsummary['visits_closed'])
        open_count = int(vsummary['visits_open'])
        if closed + open_count != len(visits):
            raise ValueError('V24_VISIT_TALLY_CONFLICT')
        def nside(column: str, expected: str) -> int:
            return int(visits[column].eq(expected).sum()) if not visits.empty else 0
        if not outcomes.empty:
            censored = int(outcomes.status.ne('FULL_HORIZON').sum())
        else:
            censored = 0
        evidence = ('NO_VISITS' if visits.empty else
                    'VISITS_ONLY_CENSORED' if sum(full_horizons.values()) == 0 else
                    'HAS_FULL_POST_OUTCOME')
        low, high = float(r.band_low_birth),float(r.band_high_birth)
        center = (low+high)/2
        region_rows.append({
            'region_id':r.region_id, 'side':r.side,
            'birth_known_ts':anchor, 'birth_utc':iso(anchor),
            'band_low_birth':low,'band_high_birth':high,'center_birth':center,
            'band_width_pct_birth':(high-low)/center*100,
            'extrema_at_birth':int(r.extrema_at_birth),
            'birth_stage_is_fixed':True,
            'source_region_born_ts':int(r.source_region_born_ts),
            'followup_closed_minutes':followup,'followup_days':followup/1440,
            'observation_status':_classified_followup(followup,visits),
            'contact_episodes':contact_count,
            'visits':len(visits),'visits_closed':closed,
            'visits_open':open_count,
            'touch_minutes':int(visits.touch_minutes.sum()) if not visits.empty else 0,
            'visit_arrived_above':nside('arrival_side','ABOVE'),
            'visit_arrived_below':nside('arrival_side','BELOW'),
            'visit_arrived_inside_or_unknown':len(visits)-nside('arrival_side','ABOVE')-nside('arrival_side','BELOW'),
            'visit_exited_above':nside('exit_side','ABOVE'),
            'visit_exited_below':nside('exit_side','BELOW'),
            'visit_exited_inside_or_unknown':len(visits)-nside('exit_side','ABOVE')-nside('exit_side','BELOW'),
            **{f'full_outcomes_{h}m':full_horizons[h] for h in DEFAULT_HORIZONS},
            'censored_outcomes':censored,
            'prior_near_regions_at_birth':near,
            'prior_overlap_regions_at_birth':inter,
            'prior_near_same_side_at_birth':near_side,
            'evidence_status':evidence,
        })
        if progress:
            progress(index,len(births),str(r.region_id),len(visits),followup)
    summary_frame = pd.DataFrame(region_rows,columns=REGION_COLUMNS)
    visit_frame = (pd.concat(all_visits,ignore_index=True) if all_visits
                   else pd.DataFrame(columns=['region_id','gap_minutes','visit_number']))
    post_frame = (pd.concat(all_outcomes,ignore_index=True) if all_outcomes
                  else pd.DataFrame(columns=['region_id','gap_minutes','visit_number','horizon_minutes','status']))
    if len(summary_frame) != len(regions) or summary_frame.region_id.duplicated().any():
        raise ValueError('V24_INCOMPLETE_UNIVERSAL_CENSUS')
    if int(summary_frame.visits.sum()) != len(visit_frame):
        raise ValueError('V24_VISITS_NOT_ACCOUNTED')
    if len(post_frame) != len(visit_frame)*len(horizons):
        raise ValueError('V24_HORIZONS_NOT_ACCOUNTED')
    if not post_frame.empty and (post_frame.horizon_minutes.isna().any()
                                 or set(post_frame.horizon_minutes) != set(horizons)):
        raise ValueError('V24_OUTCOME_SCOPE_CONFLICT')
    counts = {
        'regions_census':len(summary_frame),
        'regions_HIGH':int(summary_frame.side.eq('HIGH').sum()),
        'regions_LOW':int(summary_frame.side.eq('LOW').sum()),
        'regions_with_visits':int(summary_frame.visits.gt(0).sum()),
        'regions_without_visits':int(summary_frame.visits.eq(0).sum()),
        'regions_without_visits_48h_observed':int(summary_frame.observation_status.eq('NO_CONTACT_WITH_48H_EXPOSURE').sum()),
        'regions_only_open_visits':int(summary_frame.observation_status.eq('ONLY_OPEN_VISITS').sum()),
        'regions_with_closed_visits':int(summary_frame.visits_closed.gt(0).sum()),
        'regions_with_any_full_outcome':int(summary_frame.evidence_status.eq('HAS_FULL_POST_OUTCOME').sum()),
        'regions_with_prior_near_birth':int(summary_frame.prior_near_regions_at_birth.gt(0).sum()),
        'total_contact_episodes':int(summary_frame.contact_episodes.sum()),
        'total_visits':len(visit_frame),
        'total_visits_closed':int(summary_frame.visits_closed.sum()),
        'total_visits_open':int(summary_frame.visits_open.sum()),
        'total_post_horizon_rows':len(post_frame),
        'total_full_horizon_rows':int(post_frame.status.eq('FULL_HORIZON').sum()) if not post_frame.empty else 0,
        'total_censored_horizon_rows':int(post_frame.status.ne('FULL_HORIZON').sum()) if not post_frame.empty else 0,
        'prior_geometric_near_pairs':len(overlaps),
        'horizon_full_counts':{
            str(h): int(((post_frame.horizon_minutes == h) & post_frame.status.eq('FULL_HORIZON')).sum())
            if not post_frame.empty else 0 for h in horizons
        },
    }
    meta = {'version':VERSION,'cutoff':cutoff,'cutoff_utc':iso(cutoff),
            'timeframes':list(timeframes),'side':side,
            'geometry':{'tolerance_pct':tolerance_pct,
                        'lookback_days':lookback_days,
                        'separation_minutes':separation_minutes},
            'gap_minutes':gap_minutes,'horizons_minutes':list(horizons),
            'region_geometry':'BIRTH_STAGE_FIXED_NOT_RETROSPECTIVE_FINAL',
            'universe_census_method':'ALL_V2_REGION_BIRTHS_KNOWN_ASOF_CUTOFF',
            'v2_original_stats':v2_stats,
            'counts':counts,
            'limitations':[
                'Descriptive census only. No structural quality/edge/defense/rupture label.',
                'Visits of overlapping regions are not independent observations.',
                'RIGHT_CENSORED/NO_CONTACT are not negative examples.',
                'Outcomes are known after visit closure; never features at birth.',
                'Birth stage may differ from the mature band manually inspected in V2.3.',
                'Bands created before the catalog start cannot be measured here (left truncation).',
                'A full 1m followup is not a valid independent out-of-sample result.',
            ]}
    return summary_frame,visit_frame,post_frame,overlaps,meta


def _write_run(destination: Path, frames: tuple[pd.DataFrame,...],
               meta: dict, source: dict) -> Path:
    """Atomically publish a NEW offline run; never modify existing/frozen files."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f'V24_RUN_ALREADY_EXISTS: {destination}')
    destination.parent.mkdir(parents=True,exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='.v24a_stage_',dir=str(destination.parent)))
    try:
        names = ('regions_v24.csv','visits_v24.csv','post_horizons_v24.csv','overlaps_v24.csv')
        for name, frame in zip(names,frames):
            frame.to_csv(tmp/name,index=False)
        summary = {**meta,'source_provenance':source,
                   'created_utc':datetime.now(timezone.utc).isoformat()}
        (tmp/'summary_v24.json').write_text(json.dumps(summary,sort_keys=True,
            indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        shas = {p.name:digest(p) for p in sorted(tmp.iterdir()) if p.is_file()}
        manifest = {'version':VERSION,'source_provenance':source,
                    'config':{k:meta[k] for k in ('timeframes','side','geometry',
                                                  'gap_minutes','horizons_minutes','cutoff')},
                    'sha256':shas,'immutable_run':True,
                    'no_live_imports_or_network':True}
        (tmp/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,
            indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        if destination.exists():
            raise FileExistsError(f'V24_RUN_ALREADY_EXISTS: {destination}')
        os.rename(tmp,destination)
        return destination
    except BaseException:
        shutil.rmtree(tmp,ignore_errors=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='OFFLINE BTC 60D V2.4A structural coverage audit')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true',help='Show sources and settings, no reads/writes')
    mode.add_argument('--run',action='store_true',help='Run verified frozen offline census')
    parser.add_argument('--project-root',default='.')
    parser.add_argument('--output-dir',default=str(DEFAULT_REPORT))
    parser.add_argument('--timeframes',default=','.join(DEFAULT_TFS))
    parser.add_argument('--side',default='BOTH',choices=('BOTH','HIGH','LOW'))
    parser.add_argument('--tolerance-pct',type=float,default=0.2)
    parser.add_argument('--lookback-days',type=int,default=7)
    parser.add_argument('--separation-minutes',type=int,default=60)
    parser.add_argument('--gap-minutes',type=int,default=30)
    parser.add_argument('--horizons',default=','.join(map(str,DEFAULT_HORIZONS)))
    args = parser.parse_args(argv)
    tfs = tuple(args.timeframes.split(','))
    horizons = tuple(int(x) for x in args.horizons.split(','))
    _guard_settings(tfs,args.side,args.tolerance_pct,args.lookback_days,
                    args.separation_minutes,args.gap_minutes,horizons)
    root = Path(args.project_root).resolve()
    output = Path(args.output_dir)
    if not output.is_absolute():
        output = root/output
    settings={'version':VERSION,'mode':'PLAN' if args.plan else 'RUN',
        'project_root':str(root),'output_dir':str(output),
        'timeframes':tfs,'side':args.side,'tolerance_pct':args.tolerance_pct,
        'lookback_days':args.lookback_days,
        'separation_minutes':args.separation_minutes,
        'gap_minutes':args.gap_minutes,'horizons':horizons}
    print('V24_PLAN '+json.dumps(settings,ensure_ascii=False,default=list),flush=True)
    if args.plan:
        return 0
    if output.exists():
        raise FileExistsError(f'V24_RUN_ALREADY_EXISTS: {output}')
    full,catalog,provenance = load_snapshot(root)
    cutoff=int(provenance['end'])
    def progress(i,n,rid,visits,followup):
        if i==1 or i%10==0 or i==n:
            print(f'V24_PROGRESS {i}/{n} region={rid} visits={visits} '
                  f'followup_minutes={followup}',flush=True)
    outputs=audit_universe(full,catalog,cutoff,timeframes=tfs,side=args.side,
        tolerance_pct=args.tolerance_pct,lookback_days=args.lookback_days,
        separation_minutes=args.separation_minutes,gap_minutes=args.gap_minutes,
        horizons=horizons,progress=progress)
    regions,visits,outcomes,overlaps,meta=outputs
    target=_write_run(output,(regions,visits,outcomes,overlaps),meta,provenance)
    print('V24_FINISHED '+json.dumps({'output':str(target),
        'counts':meta['counts'],'source_sha256':provenance.get('source_sha256'),
        'catalog_sha256':provenance.get('catalog_sha256')},ensure_ascii=False),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
