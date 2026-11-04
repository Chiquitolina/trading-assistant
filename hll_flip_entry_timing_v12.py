#!/usr/bin/env python3
"""HLL Flip Entry Timing V1.2: causal pre-OPEN metrics + frozen V1/V1.1 outcomes.

Offline, new output directory only. Reads 15-symbol Unified V1/V1.1 exact-entry
cohort and archived 1m bars. No source/view/detector/LIVE file is changed.
Physical-process retrospective membership is deliberately not used as a feature.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import hll_unified_structure_lifecycle_v1 as u

VERSION = 'HLL_FLIP_ENTRY_TIMING_V1_2'
REFERENCE = 'TP3_SL1P5_48H'
FLIP = 'FLIP_1M'
V1 = 'reports/horizontal_level_unified_lifecycle_v1/pilot15_seed20261103_v1'
V11 = 'reports/horizontal_level_unified_lifecycle_v11/pilot15_seed20261103_v11'
MINUTE = 'reports/horizontal_level_full290_v301/minutes'
MINUTE_MANIFEST = 'reports/horizontal_level_full290_v301/minutes_manifest'
OUTPUT = 'reports/horizontal_level_flip_entry_timing_v12/pilot15_seed20261103_v12'
REFERENCE_CASES = ('99bf0093139dabe8597d775b', 'a51689f8e697e0dc72519671')
TP_STATES = frozenset(('TP_FIRST', 'TP_GAP_OPEN'))
SL_STATES = frozenset(('SL_FIRST', 'SL_GAP_OPEN'))
KNOWN_OUTCOMES = TP_STATES | SL_STATES | frozenset((
    'TIMEOUT', 'AMBIGUOUS_1M', 'CENSORED_GAP_OR_TAIL',
    'NO_1M_FILE', 'NO_ENTRY_MINUTE'))
FLOAT_FIELDS = ('lag_from_breakout_min', 'signal_to_open_min',
                'entry_above_zone_high_pct', 'entry_above_zone_high_atr',
                'entry_vs_breakout_close_pct', 'preentry_peak_above_zone_pct',
                'entry_pullback_from_preentry_peak_pct', 'preentry_postcross_peak_pct',
                'preentry_postcross_min_pct', 'preentry_final_1m_close_pct',
                'preentry_last_5m_close_return_pct', 'preentry_last_15m_close_return_pct',
                'preentry_last_30m_close_return_pct',
                'postcross_closes_above', 'postcross_closes_inside', 'postcross_closes_below',
                'postcross_last_above_streak', 'preentry_low_vs_zone_low_pct',
                'preentry_peak_price', 'breakout_close_1m', 'last_closed_1m_price')


def sha(path):
    return u.digest(Path(path))


def json_read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def csv_read(path, required=()):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as file:
        rd = csv.DictReader(file)
        cols = rd.fieldnames or []
        if set(required) - set(cols):
            raise ValueError(f'Missing required CSV columns in {path}: {set(required)-set(cols)}')
        result = list(rd)
    return result, cols


def index(rows, key, label):
    output = {}
    for r in rows:
        value = r.get(key, '')
        if not value or value in output:
            raise ValueError(f'Duplicate/null {key} in {label}: {value!r}')
        output[value] = r
    return output


def anchored_paths(root, *, v1=V1, v11=V11, minute=MINUTE,
                   minute_manifest=MINUTE_MANIFEST, output=OUTPUT):
    root = Path(root).resolve()
    def p(value):
        path = Path(value)
        return path.resolve() if path.is_absolute() else (root / path).resolve()
    return dict(v1=p(v1), v11=p(v11), minute=p(minute),
                minute_manifest=p(minute_manifest), output=p(output))


def verify_report(folder, expected_version, hash_key):
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    manifest = json_read(folder / 'manifest.json')
    if manifest.get('version') != expected_version:
        raise ValueError(f'Unexpected {folder} version')
    declared = manifest.get(hash_key, {})
    for filename, value in declared.items():
        file = folder / filename
        if not file.is_file() or sha(file) != value:
            raise ValueError(f'Changed frozen report {file}')
    return manifest


def verify_inputs(paths):
    v1 = verify_report(paths['v1'], 'HLL_UNIFIED_STRUCTURE_LIFECYCLE_V1', 'outputs_sha256')
    v11 = verify_report(paths['v11'], 'HLL_UNIFIED_FAMILY_LIFECYCLE_V1_1', 'output_sha256')
    required_v1 = ('unified_entries.csv', 'entry_outcomes_1m.csv', 'summary.json')
    required_v11 = ('unified_entries_family_v11.csv', 'entry_outcomes_family_v11.csv',
                    'family_geometry_flip_review_pairs_v11.csv')
    if any(k not in v1.get('outputs_sha256',{}) for k in required_v1):
        raise ValueError('Missing V1 manifest output hash')
    if any(k not in v11.get('output_sha256',{}) for k in required_v11):
        raise ValueError('Missing V1.1 manifest output hash')
    sources = v11.get('sources_sha256', {})
    if (sources.get('v1_manifest') != sha(paths['v1']/'manifest.json')
        or sources.get('v1_entries') != sha(paths['v1']/'unified_entries.csv')
        or sources.get('v1_outcomes') != sha(paths['v1']/'entry_outcomes_1m.csv')):
        raise ValueError('V1.1 is not derived from exactly these V1 reports')
    if v11.get('outcomes_copied_no_recompute') is not True or v11.get('scope_first_per_level') is not True:
        raise ValueError('V1.1 lost frozen OPEN/outcome provenance')
    if v1.get('entry_scope') != 'FIRST_PER_LEVEL' or v1.get('fingerprint_scope') != 'EPISODE_FIRST_ONLY':
        raise ValueError('Unexpected V1 entry/fingerprint scope')
    return v1, v11


def validate_records(paths, v1_manifest):
    raw_entries, _ = csv_read(paths['v1']/'unified_entries.csv',
        ('entry_id','attempt_id','route','entry_ts','entry_price','symbol'))
    entries, columns = csv_read(paths['v11']/'unified_entries_family_v11.csv',
        ('entry_id','attempt_id','route','signal_ts','entry_ts','entry_price','symbol',
         'pre_closed_ts','breakout_closed_ts','zone_low','zone_high','atr_pre',
         'family_1p0','pre_pattern_96','split','episode_id','level_id','entry_1m_verified'))
    original_outcomes, _ = csv_read(paths['v1']/'entry_outcomes_1m.csv',
        ('entry_id','scenario','state','exit_ts','net_pct'))
    enriched_outcomes, _ = csv_read(paths['v11']/'entry_outcomes_family_v11.csv',
        ('entry_id','scenario','state','exit_ts','net_pct'))
    source_by_id = index(raw_entries, 'entry_id', 'V1 source entries')
    by_id = index(entries, 'entry_id', 'V1.1 enriched entries')
    if set(source_by_id) != set(by_id):
        raise ValueError('V1 and V1.1 entry inventories disagree')
    for eid, e in by_id.items():
        original = source_by_id[eid]
        for col in original:
            if col not in e or e[col] != original[col]:
                raise ValueError(f'V1.1 changed original entry {eid}, field {col}')
    def keyed_outcomes(values):
        output = {}
        for o in values:
            key = (o['entry_id'], o['scenario'])
            if key in output: raise ValueError(f'Duplicate scenario key {key}')
            output[key] = o
        return output
    original = keyed_outcomes(original_outcomes)
    enriched = keyed_outcomes(enriched_outcomes)
    if original.keys() != enriched.keys():
        raise ValueError('V1/V1.1 outcome universe changed')
    for key, o in original.items():
        for field in o:
            if field not in enriched[key] or o[field] != enriched[key][field]:
                raise ValueError(f'V1.1 changed a historical outcome: {key} {field}')
    for eid,e in by_id.items():
        if e['entry_1m_verified'] != 'YES_SOURCE_ENTRY_VERIFIED':
            raise ValueError('Non-verified OPEN found: '+eid)
        if e['route'] not in u.ROUTES:
            raise ValueError('Unknown route: '+eid)
        if (eid,REFERENCE) not in original:
            raise ValueError('No frozen reference outcome for '+eid)
        if e['route']==FLIP and e['coverage_FLIP_1M'] != 'ENTRY_VERIFIED':
            raise ValueError('Missing FIRST_PER_LEVEL Flip coverage: '+eid)
    summary=json_read(paths['v1']/'summary.json')
    if (len(entries)!=int(summary['verified_entry_events'])
        or len(enriched_outcomes)!=int(summary['outcome_scenarios'])):
        raise ValueError('Pilot cardinalities changed')
    flips = [e for e in entries if e['route']==FLIP]
    if len(flips) != int(summary['verified_entries_by_route'][FLIP]):
        raise ValueError('Flip entry count changed')
    return flips, original, columns, summary


def prefix_metrics(e, times, arrays):
    """Only 1m bars with open timestamp strictly before entry OPEN.

    PRE window starts with the 30m CROSS_UP candle at its open: pre_closed_ts.
    No AFTER-entry candle (including the entry candle's high/low/close) is used.
    """
    entry = u.clock(e['entry_ts'])
    pre_ts = u.clock(e['pre_closed_ts'],step=u.TF30)
    cross_ts = u.clock(e['breakout_closed_ts'],step=u.TF30)
    if cross_ts != pre_ts+u.TF30 or entry < cross_ts:
        raise ValueError('Noncausal PRE/cross/entry ordering')
    signal = u.clock(e['signal_ts']) if e.get('signal_ts','') else None
    if signal is not None and signal>entry:
        raise ValueError('Signal after entry OPEN')
    entry_px = u.price(e['entry_price'])
    lo, top, atr = (u.price(e[k]) for k in ('zone_low','zone_high','atr_pre'))
    if lo>top:
        raise ValueError('Inverted resistance zone')
    result={key:'' for key in FLOAT_FIELDS}
    result['last_closed_1m_relation_to_zone']=''
    result.update(dict(entry_ts=entry, breakout_closed_ts=cross_ts,
        pre_closed_ts=pre_ts, signal_ts=signal if signal is not None else '',
        entry_price=entry_px,
        lag_from_breakout_min=(entry-cross_ts)//u.TF1,
        signal_to_open_min=(entry-signal)//u.TF1 if signal is not None else '',
        entry_above_zone_high_pct=round(100*(entry_px/top-1),6),
        entry_above_zone_high_atr=round((entry_px-top)/atr,6),
        preentry_window_start_ts=pre_ts,
        preentry_bars_expected=(entry-pre_ts)//u.TF1,
        postcross_minutes_before_entry=(entry-cross_ts)//u.TF1,
        preentry_quality='NO_1M_FILE',
        has_preentry_full_1m_prefix=False))
    if times is None:
        return result
    idx_e=bisect.bisect_left(times,entry)
    if idx_e==len(times) or times[idx_e]!=entry:
        raise ValueError('Previously verified OPEN minute vanished')
    opens,highs,lows,closes=arrays
    if not u.open_equal(opens[idx_e],entry_px):
        raise ValueError('OPEN price differs from frozen verified entry')
    idx_p=bisect.bisect_left(times,pre_ts)
    required=(entry-pre_ts)//u.TF1
    if (idx_p>=len(times) or times[idx_p]!=pre_ts
        or idx_e-idx_p !=required
        or (required and times[idx_e-1]!=entry-u.TF1)):
        result['preentry_quality']='INCOMPLETE_1M_PREFIX'
        return result
    # Sorted unique 1m timestamps plus length and boundaries prove contiguity.
    # Missing interior timestamps would change the count.
    result['preentry_quality']='COMPLETE_PREOPEN_1M'
    result['has_preentry_full_1m_prefix']=True
    if required==0:
        return result
    peak=max(highs[idx_p:idx_e]);low=min(lows[idx_p:idx_e]);last=closes[idx_e-1]
    cross_index=bisect.bisect_left(times,cross_ts)
    if cross_index<idx_p or cross_index>idx_e or cross_index-1<idx_p:
        raise ValueError('No complete breakout candle available in PREOPEN interval')
    breakout_close=closes[cross_index-1]
    result.update(
        entry_vs_breakout_close_pct=round(100*(entry_px/breakout_close-1),6),
        preentry_peak_above_zone_pct=round(100*(peak/top-1),6),
        entry_pullback_from_preentry_peak_pct=round(max(0.,100*(peak-entry_px)/peak),6),
        preentry_final_1m_close_pct=round(100*(last/entry_px-1),6),
        preentry_low_vs_zone_low_pct=round(100*(low/lo-1),6),
        preentry_peak_price=peak,
        breakout_close_1m=breakout_close,
        last_closed_1m_price=last,
        last_closed_1m_relation_to_zone=u.label_zone(last,lo,top),
    )
    if cross_index<idx_e:
        # These are completed post-CROSS_UP 1m candles, but still before entry.
        cp=closes[cross_index:idx_e]
        ph=max(highs[cross_index:idx_e]);pl=min(lows[cross_index:idx_e])
        result.update(
            preentry_postcross_peak_pct=round(100*(ph/top-1),6),
            preentry_postcross_min_pct=round(100*(pl/top-1),6),
            postcross_closes_above=sum(x>top for x in cp),
            postcross_closes_inside=sum(lo<=x<=top for x in cp),
            postcross_closes_below=sum(x<lo for x in cp),
        )
        streak=0
        for value in reversed(cp):
            if value<=top:break
            streak+=1
        result['postcross_last_above_streak']=streak
    for horizon in (5,15,30):
        # Compare last closed candle to close exactly N minutes earlier.
        left=idx_e-1-horizon
        if left>=idx_p:
            result[f'preentry_last_{horizon}m_close_return_pct']=round(
                100*(last/closes[left]-1),6)
    return result


def outcome_label(state):
    if state in TP_STATES:return 'TP_FIRST'
    if state in SL_STATES:return 'SL_FIRST'
    return 'UNRESOLVED'


def fixed_bin(field, value):
    if value in ('', None):return 'NO_PREENTRY_COVERAGE'
    x=float(value)
    if field=='lag_from_breakout_min':
        return 'LAG_0_30' if x<=30 else ('LAG_31_90' if x<=90 else ('LAG_91_240' if x<=240 else 'LAG_GT240'))
    if field=='entry_above_zone_high_pct':
        return 'AT_OR_BELOW_ZONE' if x<=0 else ('ABOVE_0_0P5' if x<=.5 else ('ABOVE_0P5_1P5' if x<=1.5 else 'ABOVE_GT1P5'))
    if field=='entry_pullback_from_preentry_peak_pct':
        return 'PULLBACK_0_0P25' if x<=.25 else ('PULLBACK_0P25_0P75' if x<=.75 else ('PULLBACK_0P75_1P5' if x<=1.5 else 'PULLBACK_GT1P5'))
    raise ValueError(f'Unrecognized bin metric: {field}')


def median_or_blank(values):
    valid=[]
    for x in values:
        if x not in ('',None):
            y=float(x)
            if math.isfinite(y):valid.append(y)
    return round(statistics.median(valid),6) if valid else ''


def summary_group(rows, dimensions):
    groups=defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in dimensions)].append(r)
    out=[]
    for key,group in sorted(groups.items()):
        counts=Counter(x['outcome_class'] for x in group)
        resolved=counts['TP_FIRST']+counts['SL_FIRST']
        out.append({**dict(zip(dimensions,key)),'entries':len(group),
          'episodes':len({x['episode_id'] for x in group}),
          'symbols':len({x['symbol'] for x in group}),
          'tp_first':counts['TP_FIRST'],'sl_first':counts['SL_FIRST'],
          'unresolved':counts['UNRESOLVED'],'resolved':resolved,
          'tp_share_resolved_pct':round(100*counts['TP_FIRST']/resolved,3) if resolved else '',
          'median_lag_min':median_or_blank(x['lag_from_breakout_min'] for x in group),
          'median_entry_zone_dist_pct':median_or_blank(x['entry_above_zone_high_pct'] for x in group),
          'median_prior_peak_pullback_pct':median_or_blank(x['entry_pullback_from_preentry_peak_pct'] for x in group),
          'interpretation':'RETROSPECTIVE_DESCRIPTIVE_NOT_OOS_EPISODES_DEPENDENT'})
    return out


def construct_rows(paths, flips, outcomes, v1_manifest):
    entries_by_sym=defaultdict(list)
    for e in flips:entries_by_sym[e['symbol']].append(e)
    result=[]
    for sym in sorted(entries_by_sym):
        minute_path=paths['minute']/f'{sym}_1m.csv'
        meta_path=paths['minute_manifest']/f'{sym}.json'
        if not minute_path.is_file() or not meta_path.is_file():
            raise FileNotFoundError('Verified 1m archive/manifest missing: '+sym)
        minute_meta=json_read(meta_path)
        expected_sha=v1_manifest.get('minute_sha256',{}).get(sym)
        expected_meta_sha=v1_manifest.get('minute_manifest_sha256',{}).get(sym)
        if not expected_sha or sha(minute_path)!=expected_sha or minute_meta.get('sha256')!=expected_sha:
            raise ValueError('Changed original 1m archive: '+sym)
        if not expected_meta_sha or sha(meta_path)!=expected_meta_sha:
            raise ValueError('Changed original 1m manifest: '+sym)
        if minute_meta.get('source_sha256')!=v1_manifest.get('source_sha256',{}).get('pre'):
            raise ValueError('1m source PRE identity changed: '+sym)
        times,ohlc=u.read_minute_file(minute_path)
        if len(times)!=minute_meta.get('bars'):
            raise ValueError('1m bar count changed: '+sym)
        for e in sorted(entries_by_sym[sym],key=lambda x:(u.clock(x['entry_ts']),x['entry_id'])):
            ref=outcomes[(e['entry_id'],REFERENCE)]
            if ref['route']!=FLIP or ref['symbol']!=sym or ref['attempt_id']!=e['attempt_id']:
                raise ValueError('Outcome identity mismatch for entry: '+e['entry_id'])
            if ref['state'] not in KNOWN_OUTCOMES:
                raise ValueError('Unknown historical outcome state for '+e['entry_id'])
            metrics=prefix_metrics(e,times,ohlc)
            row={k:e[k] for k in ('entry_id','attempt_id','symbol','episode_id','level_id',
                  'split','episode_ordinal','pre_pattern_96','family_0p5','family_1p0',
                  'family_1p5','scale_agreement','fingerprint_status','zone_low','zone_high','atr_pre')}
            row.update(metrics)
            row.update(scenario=REFERENCE,original_result_state=ref['state'],
                outcome_class=outcome_label(ref['state']),
                outcome_net_pct=ref.get('net_pct',''),
                outcome_exit_ts_POST=ref.get('exit_ts',''),
                outcome_minutes_observed_POST=ref.get('minutes_observed',''),
                source_scope='FIRST_PER_LEVEL_VERIFIED_FLIP_ONLY',
                preentry_only='ALL_METRICS_STRICTLY_BEFORE_ENTRY_OPEN',
                outcome_post_only='RETROSPECTIVE_LABEL_NOT_A_PREDICTOR')
            for field, label in (('lag_from_breakout_min','lag_bucket'),
                ('entry_above_zone_high_pct','entry_zone_bucket'),
                ('entry_pullback_from_preentry_peak_pct','prior_peak_pullback_bucket')):
                row[label]=fixed_bin(field,row[field])
            result.append(row)
        print('HLL_FLIP_TIMING_SYMBOL '+json.dumps({'symbol':sym,'flip_entries':len(entries_by_sym[sym])},ensure_ascii=False),flush=True)
    return result


def create_artifacts(paths):
    v1_manifest,_=verify_inputs(paths)
    flips,outcomes,_,summary=validate_records(paths,v1_manifest)
    rows=construct_rows(paths,flips,outcomes,v1_manifest)
    if len(rows)!=int(summary['verified_entries_by_route'][FLIP]):
        raise ValueError('Missing frozen verified Flip entries')
    families=summary_group(rows,('split','pre_pattern_96','family_1p0'))
    agreements=summary_group(rows,('split','pre_pattern_96','family_1p0','scale_agreement'))
    comparisons=summary_group(rows,('split','outcome_class'))
    # V1.1 matched pairs were selected RETROSPECTIVELY. Compare their
    # entry-time features; never use these selections to fit thresholds.
    selected,_=csv_read(paths['v11']/'family_geometry_flip_review_pairs_v11.csv',
        ('winner_entry_id','loser_entry_id','pre_pattern_96','family_1p0'))
    by_entry=index(rows,'entry_id','Flip entry timing cohort')
    pair_timing=[]
    for pair in selected:
        win=by_entry.get(pair['winner_entry_id'])
        lose=by_entry.get(pair['loser_entry_id'])
        if not win or not lose:
            raise ValueError('V1.1 comparison pair references missing Flip entry')
        if (win['split']!='DISCOVERY_SYMBOL' or lose['split']!='DISCOVERY_SYMBOL'
            or win['episode_id']==lose['episode_id'] or win['symbol']==lose['symbol']
            or win['pre_pattern_96']!=lose['pre_pattern_96']
            or win['family_1p0']!=lose['family_1p0']
            or pair['pre_pattern_96']!=win['pre_pattern_96']
            or pair['family_1p0']!=win['family_1p0']
            or win['outcome_class']!='TP_FIRST' or lose['outcome_class']!='SL_FIRST'):
            raise ValueError('V1.1 paired comparison identity/outcome mismatch')
        pr={**pair,
            'winner_preentry_quality':win['preentry_quality'],
            'loser_preentry_quality':lose['preentry_quality'],
            'selection_warning':'PAIRS_CHOSEN_AFTER_OUTCOMES_NOT_INDEPENDENT_OOS'}
        for name in ('lag_from_breakout_min','entry_above_zone_high_pct',
                     'entry_pullback_from_preentry_peak_pct',
                     'preentry_peak_above_zone_pct','entry_vs_breakout_close_pct'):
            pr['winner_'+name]=win.get(name,'')
            pr['loser_'+name]=lose.get(name,'')
            pr['winner_minus_loser_'+name]=(round(float(win[name])-float(lose[name]),6)
                if win.get(name,'')!='' and lose.get(name,'')!='' else '')
        pair_timing.append(pr)
    timing=[]
    for field in ('lag_bucket','entry_zone_bucket','prior_peak_pullback_bucket'):
        for group in summary_group(rows,('split',field)):
            timing.append({'metric':field,'bucket':group[field],**{k:v for k,v in group.items() if k!=field}})
    by_attempt=index(rows,'attempt_id','Flip entry by attempt (FIRST_PER_LEVEL)')
    reference_cases=[by_attempt[aid] for aid in REFERENCE_CASES if aid in by_attempt]
    stats={'version':VERSION,'scope':'PILOT15_FIRST_PER_LEVEL_VERIFIED_FLIP_ONLY',
        'flip_verified':len(rows), 'splits':dict(Counter(r['split'] for r in rows)),
        'outcomes':dict(Counter(r['outcome_class'] for r in rows)),
        'preentry_quality':dict(Counter(r['preentry_quality'] for r in rows)),
        'reference_cases_found':[r['attempt_id'] for r in reference_cases],
        'matched_flip_timing_pairs':len(pair_timing),
        'no_live':True,'no_detector_changes':True,'no_source_overwrite':True,
        'outcomes_copied_from_unified_v1':True,
        'notes':['Only 1m candles closed before verified entry OPEN are used for timing features',
                 'Preentry window starts at PRE close, includes full breakout 30m candle',
                 'Signal timestamp semantics inherited from frozen Entry Audit; 0min may be exact boundary',
                 'No missing 1m prefix is silently filled: metrics become unavailable',
                 'Family and geometry known at each exact PRE cut; not inherited from episode first',
                 'Physical-process links and frozen POST status not used as predictors',
                 'Within one episode/level entries are dependent; descriptive not out-of-sample',
                 'Breakout lag and timing buckets were fixed before outcome comparisons',
                 'No ranking, filter selection, LIVE or strategy change',
                 'Reference cases are retrospective illustrations, not a fitted rule']}
    return {'flip_entry_timing_preopen.csv':rows,
            'by_geometry_family_timing.csv':families,
            'by_geometry_family_agreement.csv':agreements,
            'by_result_timing_descriptive.csv':comparisons,
            'by_timing_bucket.csv':timing,
            'dydx_cake_reference_cases.csv':reference_cases,
            'matched_flip_timing_pairs.csv':pair_timing},stats


def write_csv(path,records,columns):
    with path.open('x',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(columns),extrasaction='raise')
        writer.writeheader()
        writer.writerows(records)


def run(paths):
    output=paths['output']
    for source in ('v1','v11','minute','minute_manifest'):
        origin=paths[source]
        if origin==output or origin in output.parents or output in origin.parents:
            raise ValueError('Refusing to write into/above an input source')
    if output.exists():raise FileExistsError('Output exists; refuse overwrite: '+str(output))
    artifacts,stats=create_artifacts(paths)
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix='.hll-flip-timing-v12-',dir=output.parent))
    try:
        manifest={'version':VERSION,'sources_sha256':{
            'v1_manifest':sha(paths['v1']/'manifest.json'),
            'v11_manifest':sha(paths['v11']/'manifest.json')},
            'output_sha256':{},'no_source_changes':True}
        # Stable columns even if a test cohort has zero rows.
        allcols=list(artifacts['flip_entry_timing_preopen.csv'][0]) if artifacts['flip_entry_timing_preopen.csv'] else []
        stable = ['entry_id','attempt_id','symbol','episode_id','level_id','split',
                  'pre_pattern_96','family_1p0','preentry_quality','outcome_class','original_result_state']
        for name,entries in artifacts.items():
            if entries: cols=list(entries[0])
            elif name=='flip_entry_timing_preopen.csv':cols=allcols or stable
            elif name=='dydx_cake_reference_cases.csv':cols=allcols or stable
            elif name=='matched_flip_timing_pairs.csv':cols=['winner_entry_id','loser_entry_id',
                'pre_pattern_96','family_1p0','selection_warning']
            elif name=='by_timing_bucket.csv':cols=['metric','bucket','entries','episodes','symbols','tp_first','sl_first','unresolved','resolved','tp_share_resolved_pct']
            else:cols=['split','entries','episodes','symbols','tp_first','sl_first','unresolved','resolved']
            write_csv(temp/name,entries,cols)
            manifest['output_sha256'][name]=sha(temp/name)
        (temp/'summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        manifest['output_sha256']['summary.json']=sha(temp/'summary.json')
        (temp/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.rename(temp,output)
    except Exception:
        shutil.rmtree(temp,ignore_errors=True)
        raise
    final={'status':'DONE','version':VERSION,'output':str(output),
           'flip_verified':stats['flip_verified'],
           'outcomes':stats['outcomes'],
           'preentry_quality':stats['preentry_quality'],
           'reference_cases_found':stats['reference_cases_found'],
           'matched_flip_timing_pairs':stats['matched_flip_timing_pairs']}
    print('HLL_FLIP_TIMING_FINAL '+json.dumps(final,ensure_ascii=False),flush=True)
    return final


def inspect(paths,attempt_id):
    output=paths['output']
    manifest=verify_report(output,VERSION,'output_sha256')
    found=[]
    for row in u.rows(output/'flip_entry_timing_preopen.csv',('attempt_id','entry_id')):
        if row['attempt_id']==attempt_id:found.append(row)
    if not found:
        raise ValueError(f'No verified FLIP attempt {attempt_id} in this pilot')
    print('HLL_FLIP_TIMING_INSPECT '+json.dumps({'attempt_id':attempt_id,'entries':found},ensure_ascii=False,indent=2))
    return found


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    mode.add_argument('--inspect-attempt',metavar='ATTEMPT_ID')
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    for key,default in [('v1',V1),('v11',V11),('minute',MINUTE),
                        ('minute-manifest',MINUTE_MANIFEST),('output',OUTPUT)]:
        p.add_argument('--'+key,default=default)
    a=p.parse_args(argv)
    paths=anchored_paths(a.root,v1=a.v1,v11=a.v11,minute=a.minute,
                        minute_manifest=a.minute_manifest,output=a.output)
    if a.inspect_attempt:
        inspect(paths,a.inspect_attempt);return 0
    print('HLL_FLIP_TIMING_PLAN '+json.dumps({'version':VERSION,
        'input_presence':{key:paths[key].exists() for key in ('v1','v11','minute','minute_manifest')},
        'output':str(paths['output']),'output_exists':paths['output'].exists(),
        'writes_sources':False,'entry_scope':'FIRST_PER_LEVEL_VERIFIED_FLIP',
        'uses_1m_bars_before_entry_only':True,
        'post_outcomes_copied_no_resimulation':True},ensure_ascii=False),flush=True)
    if a.plan:return 0
    run(paths)
    return 0

if __name__=='__main__':raise SystemExit(main())
