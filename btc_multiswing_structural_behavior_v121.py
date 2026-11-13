#!/usr/bin/env python3
"""Multi-Swing BTC Structural Behavior V1.2.1 — read-only causal PRE repair.

Consumes immutable V1.2 and original immutable V2.4A; does not recalculate
transitions, rewrite historical audits, score trades, or change Streamlit/LIVE.

- Masks fixed episode-reference geometry and ALL derived positions when that
  specific reference stage was unknown at the PRE candle's OPEN.
- Reconstructs separately the latest CONFIRMED as-of geometric stage from the
  original V2 swing catalog (15m/30m/1h/4h), never the eventual mature band.
- Separates pre-first-contact snapshots from snapshots after a closed 1m touch;
  even the 1m candle OPEN is not enough to establish a touch.
- Keeps cross-band, same-clock events as descriptive dependency groups only.
- Creates a NEW immutable V1.2.1 output. No V1/V1.1/V1.2 overwrite.

Known geometry is NOT a validated ACTIVE or tradeable state. This is not a
backtest. Event identifiers and post-hoc groups are joining keys, not PRE signals.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, asof_rows, digest, iso, load_snapshot
from btc_multiswing_structural_confluence_v2 import construct_regions

VERSION = 'btc-multiswing-structural-behavior-pre-safety-v1.2.1'
DEFAULT_ROOT = Path('reports/reaction_lab/btc_multiswing_60d_v1')
DEFAULT_V12 = DEFAULT_ROOT / 'btc_sep09_nov08_structural_behavior_15m_1h_v12'
DEFAULT_V24A = DEFAULT_ROOT / 'btc_sep09_nov08_information_universe_v24a'
DEFAULT_OUTPUT = DEFAULT_ROOT / 'btc_sep09_nov08_structural_behavior_pre_safety_v121'
PARENT_REQUIRED = (
    'episode_index_v12.csv', 'visit_pre_v12.csv',
    'transition_pre_v12.csv', 'transition_observations_v12.csv',
    'physical_transitions_v12.csv', 'behavior_summary_v12.json',
)
V24A_REQUIRED = ('regions_v24.csv',)
OUTPUT_TABLES = ('transition_pre_safe_v121.csv', 'visit_pre_stage_v121.csv',
                 'geometric_stage_history_v121.csv', 'clock_dependency_v121.csv')


def _checked_parent(folder: Path, *, version: str, required: tuple[str, ...]) -> dict:
    m = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if (m.get('version') != version or m.get('immutable_run') is not True
            or m.get('no_live_imports_or_network') is not True):
        raise ValueError('V121_PARENT_MANIFEST_INVALID: ' + str(folder))
    hashes = m.get('files_sha256', m.get('sha256', {}))
    for name in required:
        if name not in hashes or digest(folder / name) != hashes[name]:
            raise ValueError('V121_PARENT_HASH_MISMATCH: ' + name)
    return m


def verify_sources(root: Path, v12: Path, v24a: Path):
    m12 = _checked_parent(v12, version='btc-multiswing-structural-behavior-15m-1h-4h-v1.2',
                          required=PARENT_REQUIRED)
    m24 = _checked_parent(v24a, version='btc-multiswing-information-universe-v2.4a',
                          required=V24A_REQUIRED)
    for k in ('start', 'end', 'source_sha256', 'catalog_sha256'):
        if m12['source_provenance'].get(k) != m24['source_provenance'].get(k):
            raise ValueError('V121_PARENT_SOURCE_DISAGREEMENT: ' + k)
    config = m24.get('config', {})
    geometry = config.get('geometry', {})
    if (not config.get('timeframes') or config.get('side') != 'BOTH'
            or not all(x in geometry for x in ('tolerance_pct', 'lookback_days', 'separation_minutes'))):
        raise ValueError('V121_UNVERIFIED_V24A_GEOMETRY_POLICY')
    _, catalog, source = load_snapshot(root)
    for k in ('start', 'end', 'source_sha256', 'catalog_sha256'):
        if source.get(k) != m12['source_provenance'].get(k):
            raise ValueError('V121_FREEZE_SOURCE_DRIFT: ' + k)
    if int(config.get('cutoff', -1)) != int(source['end']):
        raise ValueError('V121_V24A_CUTOFF_MUST_EQUAL_FREEZE_END')
    files = {name: pd.read_csv(v12 / name, low_memory=False)
             for name in PARENT_REQUIRED if name.endswith('.csv')}
    if (len(files['transition_pre_v12.csv']) != len(files['transition_observations_v12.csv'])
            or files['transition_pre_v12.csv'].transition_id.duplicated().any()
            or not files['transition_pre_v12.csv'].transition_id.isin(
                files['transition_observations_v12.csv'].transition_id).all()
            or files['visit_pre_v12.csv'].event_id.duplicated().any()):
        raise ValueError('V121_PARENT_KEYS_INVALID')
    births = pd.read_csv(v24a / 'regions_v24.csv', low_memory=False)
    return files, births, catalog, source, config, m12, m24


def reconstruct_stage_history(catalog: pd.DataFrame, *, source_end: int,
                              config: dict, original_births: pd.DataFrame) -> pd.DataFrame:
    geom = config['geometry']
    known = asof_rows(catalog, source_end, tuple(config['timeframes']), config['side'])
    regions, stages, _ = construct_regions(
        known, source_end, tolerance_pct=float(geom['tolerance_pct']),
        lookback_days=int(geom['lookback_days']),
        min_separation_minutes=int(geom['separation_minutes']), min_extrema=2)
    # Match every V2.4A band's ORIGINAL physical identity and birth geometry;
    # do not silently apply a different V2 configuration or TF set.
    if (regions.region_id.duplicated().any() or stages.empty or
            set(regions.region_id) != set(original_births.region_id)):
        raise ValueError('V121_REGION_IDENTITY_NOT_REPRODUCED')
    first = (stages.sort_values(['known_from','independent_extrema'],kind='mergesort')
             .drop_duplicates('region_id', keep='first').set_index('region_id'))
    for r in original_births.itertuples(index=False):
        s = first.loc[str(r.region_id)]
        if (int(s.known_from) != int(r.birth_known_ts) or
            abs(float(s.band_low)-float(r.band_low_birth)) > 1e-6 or
            abs(float(s.band_high)-float(r.band_high_birth)) > 1e-6 or
            str(s.side) != str(r.side)):
            raise ValueError('V121_BIRTH_GEOMETRY_RECONSTRUCTION_DRIFT: '+str(r.region_id))
    stages = stages.copy().sort_values(['region_id','known_from','independent_extrema'],
                                      kind='mergesort').reset_index(drop=True)
    stages['audit_role'] = 'FULL_HISTORY_STAGE_INDEX_FILTER_BY_KNOWN_FROM_BEFORE_USING'
    stages['not_active'] = True
    return stages


def lookup_asof(stages: pd.DataFrame, identifiers: pd.DataFrame, *,
                key_col: str, time_col: str, lookback_days: int) -> pd.DataFrame:
    """One row per PRE, latest stage whose confirmation is <= PRE cutoff.

    Returning a future stage, even as a fallback, is forbidden. Age is a visual
    projection marker, not proof the band is invalid or ACTIVE.
    """
    required = {'region_id', 'known_from', 'band_low', 'band_high', 'independent_extrema'}
    if not required.issubset(stages.columns):
        raise ValueError('V121_STAGE_SCHEMA_INVALID')
    if identifiers[key_col].duplicated().any():
        raise ValueError('V121_NONUNIQUE_PRE_ID')
    region_index = {}
    for region, group in stages.groupby('region_id', sort=False):
        s = group.sort_values(['known_from','independent_extrema'], kind='mergesort')
        region_index[str(region)] = (s.known_from.to_numpy(dtype='int64'), s.reset_index(drop=True))
    result = []
    for r in identifiers[['region_id', key_col, time_col]].itertuples(index=False, name=None):
        region, ident, t = str(r[0]), str(r[1]), int(r[2])
        times, group = region_index.get(region, (np.array([], dtype='int64'), None))
        pos = int(np.searchsorted(times, t, side='right')) - 1
        record = {key_col: ident, 'asof_stage_known': pos >= 0,
                  'asof_stage_low': np.nan, 'asof_stage_high': np.nan,
                  'asof_stage_known_from': pd.NA, 'asof_stage_extrema': pd.NA,
                  'asof_stage_age_minutes': pd.NA,
                  'asof_visual_projection_within_lookback': False}
        if pos >= 0:
            s = group.iloc[pos]
            age = t - int(s.known_from)
            if age < 0:
                raise ValueError('V121_FUTURE_STAGE_LEAK')
            record.update(asof_stage_low=float(s.band_low),
                          asof_stage_high=float(s.band_high),
                          asof_stage_known_from=int(s.known_from),
                          asof_stage_extrema=int(s.independent_extrema),
                          asof_stage_age_minutes=age // M,
                          asof_visual_projection_within_lookback=(age <= lookback_days*1440*M))
        result.append(record)
    return pd.DataFrame(result)


def _reference_position(price: float, low: float, high: float):
    if not (pd.notna(price) and pd.notna(low) and pd.notna(high)):
        return None
    if float(price) < float(low): return 'BELOW'
    if float(price) > float(high): return 'ABOVE'
    return 'INSIDE'


def _visit_latest_closed(pre: pd.DataFrame, visits: pd.DataFrame) -> pd.DataFrame:
    """Last 1m contact *already closed* at PRE cutoff; no touch at bar open."""
    pv = visits[['episode_id','event_id','pre_cutoff_ts']].copy()
    pv['touch_known_ts'] = pv.pre_cutoff_ts.astype('int64') + M
    groups = {}
    for ep, g in pv.groupby('episode_id', sort=False):
        g = g.sort_values(['touch_known_ts','event_id'], kind='mergesort')
        groups[str(ep)] = (g.touch_known_ts.to_numpy(dtype='int64'),
                           g.event_id.to_numpy(dtype=str))
    records = []
    for r in pre[['transition_id','episode_id','pre_cutoff_ts']].itertuples(index=False):
        a, ids = groups.get(str(r.episode_id),(np.array([],dtype='int64'), np.array([],dtype=str)))
        i = int(np.searchsorted(a,int(r.pre_cutoff_ts),side='right')) - 1
        records.append({'transition_id':str(r.transition_id),
           'pre_contact_phase': 'AFTER_CLOSED_TOUCH_1M' if i>=0 else 'BEFORE_FIRST_CLOSED_TOUCH_1M',
           'pre_episode_touch_known': i>=0,
           'pre_last_known_visit_event_id':str(ids[i]) if i>=0 else None,
           'pre_closed_visits_observed':i+1})
    return pd.DataFrame(records)


def build_safe_tables(pre: pd.DataFrame, visits: pd.DataFrame,
                      observations: pd.DataFrame, physical: pd.DataFrame,
                      stages: pd.DataFrame, *, lookback_days: int) -> tuple[dict, dict]:
    p = pre.copy()
    if (p.transition_id.duplicated().any() or len(p) != len(observations) or
            set(p.transition_id) != set(observations.transition_id)):
        raise ValueError('V121_PRE_OBSERVATION_MISMATCH')
    # The original V1.2 PRE reference is a FROZEN episode stage. It is NOT
    # necessarily the stage that existed at the timestamp of a later event.
    p = p.merge(lookup_asof(stages,p,key_col='transition_id',time_col='pre_cutoff_ts',
                           lookback_days=lookback_days),on='transition_id',validate='one_to_one')
    p = p.merge(_visit_latest_closed(p, visits),on='transition_id',validate='one_to_one')
    old_touch_flag = p.pre_episode_touch_occurred.fillna(False).astype(bool)
    p['v12_touch_flag_corrected'] = old_touch_flag.ne(p.pre_episode_touch_known)
    p['pre_episode_touch_occurred'] = p.pre_episode_touch_known
    missing = ~p.pre_reference_stage_known.fillna(False).astype(bool)
    # Every column whose value was DERIVED from the unavailable EPISODE reference
    # must be nulled. Raw closed prices, timestamps and independent returns stay.
    sensitive = ('pre_stage_reference_low','pre_stage_reference_high',
                 'pre_1m_position','pre_15m_position','pre_1h_position','pre_4h_position')
    # If the independent catalog denies any stage at PRE, fail closed even if
    # an old parent table claims its fixed episode reference was available.
    mask_any = missing | ~p.asof_stage_known
    for col in sensitive:
        if col not in p.columns:
            raise ValueError('V121_REQUIRED_UNSAFE_COLUMN_MISSING: '+col)
        p.loc[mask_any,col] = np.nan
    p['reference_sanitized'] = mask_any
    p['reference_sanitization_reason'] = np.select(
        [missing, ~p.asof_stage_known],
        ['EPISODE_REFERENCE_NOT_CONFIRMED', 'CATALOG_HAS_NO_CONFIRMED_STAGE'],
        default='REFERENCE_CONFIRMED')
    p['asof_stage_matches_reference'] = (
        p.asof_stage_known & ~missing &
        np.isclose(p.asof_stage_low,p.pre_stage_reference_low,equal_nan=False,atol=1e-6) &
        np.isclose(p.asof_stage_high,p.pre_stage_reference_high,equal_nan=False,atol=1e-6))
    for stem in ('1m','15m','1h','4h'):
        src = 'pre_'+stem+'_close'
        if src not in p: raise ValueError('V121_MISSING_CLOSE_FOR_ASOF: '+src)
        p['pre_'+stem+'_position_asof'] = [
            _reference_position(px,lo,hi) for px,lo,hi in zip(
                p[src], p.asof_stage_low, p.asof_stage_high)]
    p['stage_policy'] = 'EPISODE_REFERENCE_MASKED_PLUS_CONFIRMED_STAGE_ASOF_NOT_ACTIVE'
    p['pre_model_eligible_structure'] = (p.asof_stage_known & p.pre_episode_touch_known)
    p['pre_model_exclusion_reason'] = np.select(
        [~p.asof_stage_known, ~p.pre_episode_touch_known],
        ['NO_CONFIRMED_STAGE_ASOF','NO_CLOSED_TOUCH_YET'], default='ELIGIBLE_STRUCTURAL_CONTEXT_ONLY')
    p['event_label_is_post_only'] = True
    p['not_a_trade_signal'] = True
    # PRE identifiers still link to retrospective labels outside this CSV.
    # Never include POST event labels, h1-m15 links, or future-confirmation data.
    if {'event_type','confirmation_close','h1_m15_link_status'} & set(p):
        raise ValueError('V121_RETROSPECTIVE_COLUMNS_IN_PRE')
    v = visits.copy()
    v = v.merge(lookup_asof(stages,v,key_col='event_id',time_col='pre_cutoff_ts',
                           lookback_days=lookback_days),on='event_id',validate='one_to_one')
    v['asof_stage_matches_visit_reference'] = (
        v.asof_stage_known & np.isclose(v.stage_low,v.asof_stage_low,equal_nan=False,atol=1e-6) &
        np.isclose(v.stage_high,v.asof_stage_high,equal_nan=False,atol=1e-6))
    v['visit_touch_known_at_pre'] = False  # Precedes the first contact minute
    v['pre_contact_role'] = 'BEFORE_VISIT_FIRST_TOUCH_1M_OPEN'
    v['not_active'] = True
    # A mismatch does not auto-correct the visit's anchor: mark it as an
    # inconsistency and keep its raw source traceable for later investigation.
    v['visit_stage_audit_status'] = np.select(
        [~v.asof_stage_known,~v.asof_stage_matches_visit_reference],
        ['STAGE_NOT_RECONSTRUCTIBLE','VISIT_REFERENCE_DRIFT'],default='CONFIRMED_MATCH')
    # Treat the V1 anchored reference as an audit-only input when it cannot be
    # reconciled to the catalog. The original remains in immutable V1.2.
    v['visit_original_reference_masked'] = v.visit_stage_audit_status.ne('CONFIRMED_MATCH')
    for col in ('stage_low','stage_high','pre_1h_position','pre_4h_position'):
        if col in v.columns:
            v.loc[v.visit_original_reference_masked,col] = np.nan
    shared = physical.copy()
    required = {'confirmation_ts','source_tf','event_type','direction','region_id'}
    if not required.issubset(shared):
        raise ValueError('V121_PHYSICAL_SCHEMA_MISSING')
    group_cols=['confirmation_ts','source_tf','event_type','direction']
    shared['concurrent_bands'] = shared.groupby(group_cols)['region_id'].transform('nunique')
    shared = (shared[group_cols + ['physical_transition_id','region_id','concurrent_bands']]
              .loc[lambda x:x.concurrent_bands.gt(1)].copy())
    shared['audit_role'] = 'RETROSPECTIVE_CLOCK_DEPENDENCY_NOT_PRE_FEATURE'
    counts = {
       'transition_pre_rows':len(p), 'visits':len(v),
       'fixed_reference_masked_rows':int(missing.sum()),
       'total_reference_masked_rows':int(mask_any.sum()),
       'pre_without_closed_first_touch':int((~p.pre_episode_touch_known).sum()),
       'v12_touch_flag_corrected_rows':int(p.v12_touch_flag_corrected.sum()),
       'pre_asof_stage_unavailable':int((~p.asof_stage_known).sum()),
       'pre_asof_stage_reference_changed':int((~missing & p.asof_stage_known &
                                            ~p.asof_stage_matches_reference).sum()),
       'visit_stage_reference_drift':int((v.visit_stage_audit_status!='CONFIRMED_MATCH').sum()),
       'events_cross_band_clock_collisions':len(shared),
       'pre_model_eligible_structural_contexts':int(p.pre_model_eligible_structure.sum())}
    if (p.loc[mask_any,list(sensitive)].notna().any().any() or
            p.loc[~p.asof_stage_known,['asof_stage_low','asof_stage_high']].notna().any().any()):
        raise ValueError('V121_PRE_GEOMETRY_FUTURE_LEAK')
    if ((p.loc[p.asof_stage_known,'asof_stage_known_from'].astype('int64') >
           p.loc[p.asof_stage_known,'pre_cutoff_ts']).any() or
            (v.loc[v.asof_stage_known,'asof_stage_known_from'].astype('int64') >
             v.loc[v.asof_stage_known,'pre_cutoff_ts']).any()):
        raise ValueError('V121_STAGE_KNOWN_AFTER_PRE')
    return ({'transition_pre_safe_v121.csv':p,
             'visit_pre_stage_v121.csv':v,
             'clock_dependency_v121.csv':shared}, counts)


def write_immutable(output:Path,tables:dict,summary:dict, *,root:Path,v12:Path,v24a:Path,
                    m12:dict,m24:dict,source:dict):
    if output.exists():
        raise FileExistsError('V121_IMMUTABLE_RESULT_ALREADY_EXISTS: '+str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ms_v121_',dir=output.parent) as temp:
        working=Path(temp)
        for name, df in tables.items():
            df.to_csv(working/name,index=False,float_format='%.12g')
        (working/'pre_safety_summary_v121.json').write_text(json.dumps(summary,sort_keys=True,
               indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        file_names=list(tables)+['pre_safety_summary_v121.json']
        manifest={'version':VERSION,'immutable_run':True,'read_only':True,
            'no_live_imports_or_network':True,
            'parent_v12':str(v12),'parent_v24a':str(v24a),
            'parent_v12_manifest_sha256':digest(v12/'manifest.json'),
            'parent_v24a_manifest_sha256':digest(v24a/'manifest.json'),
            'source_provenance':{k:source.get(k) for k in ('start','end','source_sha256','catalog_sha256')},
            'files_sha256':{name:digest(working/name) for name in file_names}}
        (working/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2,
                         ensure_ascii=False)+'\n',encoding='utf-8')
        working.rename(output)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    m=parser.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true')
    m.add_argument('--run',action='store_true')
    parser.add_argument('--project-root',type=Path,default=Path('.'))
    parser.add_argument('--parent-v12',type=Path,default=DEFAULT_V12)
    parser.add_argument('--parent-v24a',type=Path,default=DEFAULT_V24A)
    parser.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args(argv)
    root=args.project_root.resolve()
    v12=args.parent_v12 if args.parent_v12.is_absolute() else root/args.parent_v12
    v24a=args.parent_v24a if args.parent_v24a.is_absolute() else root/args.parent_v24a
    output=args.output_dir if args.output_dir.is_absolute() else root/args.output_dir
    print('MS_BEHAVIOR_V121_PLAN',json.dumps({'version':VERSION,'mode':'RUN' if args.run else 'PLAN',
            'read_only':True,'no_live':True,'original_v12_unchanged':True,
            'parent_v12':str(v12),'parent_v24a':str(v24a),'output_dir':str(output)},
           sort_keys=True),flush=True)
    if args.plan:return 0
    if output.exists():
        raise FileExistsError('V121_IMMUTABLE_RESULT_ALREADY_EXISTS: '+str(output))
    files,births,catalog,source,config,m12,m24=verify_sources(root,v12,v24a)
    stage_history = reconstruct_stage_history(catalog,source_end=int(source['end']),
                          config=config,original_births=births)
    tables,counts = build_safe_tables(files['transition_pre_v12.csv'],files['visit_pre_v12.csv'],
                         files['transition_observations_v12.csv'], files['physical_transitions_v12.csv'],
                         stage_history,lookback_days=int(config['geometry']['lookback_days']))
    tables['geometric_stage_history_v121.csv']=stage_history
    totals={**counts,'version':VERSION,'source_immutable':True,
       'stage_history_rows':len(stage_history),
       'warnings':['Retrospective event labels/clock groups and episode IDs are joining references, NEVER model PRE features.',
          'Stage geometry is confirmed as-of for each cutoff; visual projection is NOT ACTIVE or execution eligibility.',
          'A first 1m touch is known only AFTER its minute has fully closed.',
          'Cross-band duplicate clocks remain dependent; no PnL, model, TP/SL or trading signals.',
          'All parent V1/V1.1/V1.2 and V2.4A datasets are preserved.']}
    write_immutable(output,tables,totals,root=root,v12=v12,v24a=v24a,
                    m12=m12,m24=m24,source=source)
    print('MS_BEHAVIOR_V121_FINISHED',json.dumps({'counts':counts,
         'output':str(output)},sort_keys=True),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
