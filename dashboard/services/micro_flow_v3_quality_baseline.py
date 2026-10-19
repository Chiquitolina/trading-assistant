"""Research-only, causal matched controls and immutable provenance snapshots.

Raw AggTrade SQLite remains the source of truth. This layer only reads CLOSED
sources and already verified Replay V3; it never sends orders or changes PM2.
Controls are selected using data *published strictly before* each decision.
Outcome values and baseline matches are observational proxies, not fills.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
import sqlite3
from pathlib import Path
import shutil
from statistics import mean
import tempfile

from research.micro_flow_v3.tape import replay_digest
from dashboard.services.micro_flow_v3_multi_session import hash_replay

SCHEMA = 'mfv3-quality-matched-v1'
HORIZONS = (30, 60, 180, 300)
SOURCE_PATH = Path('reports/micro_flow_v3/research_runs/quality_baseline')


class QualityError(ValueError):
    pass


def _json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':'))


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _published_history(rows, at_ms, *, history_s=120, max_age_s=3):
    """Only observation-ready buckets strictly before at_ms; no future leakage.

    A bucket's nominal second timestamp is NOT an availability timestamp.
    Requires a full enough historical span to be meaningfully matched.
    """
    eligible = [r for r in rows if at_ms - history_s * 1000 <=
                int(r['available_ms']) < at_ms]
    if len(eligible) < 10 or at_ms - int(eligible[-1]['available_ms']) > max_age_s * 1000:
        return None
    if int(eligible[-1]['available_ms']) - int(eligible[0]['available_ms']) < 45_000:
        return None
    prices = [float(r['close']) for r in eligible]
    if min(prices) <= 0:
        raise QualityError('nonpositive price')
    changes = [100 * math.log(b / a) for a, b in zip(prices, prices[1:])]
    return {
        'trend_120s_pct': 100 * math.log(prices[-1] / prices[0]),
        'vol_120s_pct': math.sqrt(sum(x*x for x in changes)),
        'history_buckets': len(eligible),
        'last_known_ms': int(eligible[-1]['available_ms']),
    }


def _by_segment(buckets):
    groups = defaultdict(list)
    for r in buckets:
        groups[int(r['segment_id'])].append(r)
    for segment, data in groups.items():
        data.sort(key=lambda r: (int(r['second_start_ms']), int(r['available_ms'])))
        times = [int(r['second_start_ms']) for r in data]
        published = [int(r['available_ms']) for r in data]
        if len(times) != len(set(times)) or any(b < a for a,b in zip(published,published[1:])):
            raise QualityError(f'invalid or nonmonotonic segment {segment}')
    return groups


def _proxy_outcomes(rows, decision_ms, direction, *, tolerance_ms=3000):
    """Use same subsequent observed bucket-OPEN proxy convention as V3.

    A proxy is not an executable fill. Never cross a causal segment boundary,
    interpolate unavailable seconds, or use later data for a decision.
    """
    starts = [int(r['second_start_ms']) for r in rows]
    entry_idx = bisect_left(starts, int(decision_ms))
    report = {'entry_ms': None, 'entry_proxy': None, 'entry_state': 'CENSORED_NO_ENTRY'}
    for h in HORIZONS:
        report[f'outcome_state_{h}s'] = 'CENSORED_NO_ENTRY'
        report[f'return_{h}s_pct'] = None
        report[f'directional_mfe_{h}s_pct'] = None
        report[f'directional_mae_{h}s_pct'] = None
        report[f'exit_proxy_{h}s'] = None
        report[f'exit_observed_ms_{h}s'] = None
    if (entry_idx >= len(rows) or starts[entry_idx] > int(decision_ms) + tolerance_ms):
        return report
    price = float(rows[entry_idx]['open'])
    if price <= 0:
        raise QualityError('invalid entry proxy')
    report.update(entry_ms=starts[entry_idx], entry_proxy=price, entry_state='PROXY_AVAILABLE')
    for h in HORIZONS:
        k = f'{h}s'
        target = starts[entry_idx] + h*1000
        exit_idx = bisect_left(starts, target, lo=entry_idx+1)
        if exit_idx >= len(rows):
            report[f'outcome_state_{k}'] = 'CENSORED_END_OF_SEGMENT'
            continue
        if starts[exit_idx] > target + tolerance_ms:
            report[f'outcome_state_{k}'] = 'CENSORED_HORIZON_GAP'
            continue
        window = rows[entry_idx:exit_idx]
        high = max(float(r['high']) for r in window)
        low = min(float(r['low']) for r in window)
        exit_price = float(rows[exit_idx]['open'])
        hi = 100*(high/price-1)
        lo = 100*(low/price-1)
        report[f'outcome_state_{k}'] = 'OBSERVED'
        report[f'return_{k}_pct'] = 100*direction*(exit_price/price-1)
        report[f'directional_mfe_{k}_pct'] = max(0.,hi) if direction == 1 else max(0.,-lo)
        report[f'directional_mae_{k}_pct'] = min(0.,lo) if direction == 1 else min(0.,-hi)
        report[f'exit_proxy_{k}'] = exit_price
        report[f'exit_observed_ms_{k}'] = starts[exit_idx]
    return report


def audit_episodes(result):
    """One row per episode, including unresolved/censored and decision evidence."""
    ep_by_id = {e['episode_id']:e for e in result['episodes']}
    by_event = {e['event_id']:e for e in result['anchors']}
    findings = {e['episode_id']:e for e in result['failed']}
    if len(ep_by_id) != len(result['episodes']) or len(findings) != len(ep_by_id):
        raise QualityError('episode/failed cardinality mismatch')
    records = []
    for episode in result['episodes']:
        eid = episode['episode_id']
        f = findings[eid]
        anchor = by_event[episode['anchor_event_id']]
        if (f['event_id'] != anchor['event_id'] or
                f['source_session'] != episode['source_session']):
            raise QualityError('episode provenance mismatch')
        item = dict(episode)
        item.update({'state':f['state'], 'confirmation_decision_ms':f['decision_ms'],
                     'evidence_last_known_ms':f['observed_asof_ms'],
                     'directional_progress_pct':f['directional_progress_pct'],
                     'peak_original_favorable_pct':f['peak_original_favorable_pct'],
                     'confirmation_net_delta_qty':f['confirmation_net_delta_qty'],
                     'candidate_side':f['candidate_side'],
                     'candidate_entry_state':f['entry_state'],
                     'candidate_entry_ms':f['entry_ms'],
                     'candidate_entry_proxy':f['entry_proxy'],
                     'signal_delta_qty':anchor['delta_qty'],
                     'signal_relative_volume':anchor['relative_volume'],
                     'signal_imbalance':anchor['imbalance']})
        for h in HORIZONS:
            item[f'candidate_outcome_state_{h}s'] = f.get(f'outcome_state_{h}s')
            item[f'candidate_return_{h}s_pct'] = f.get(f'return_{h}s_pct')
        records.append(item)
    return records


def make_matched_controls(result, *, control_spacing_seconds=30,
                          signal_quiet_seconds=30, match_window_minutes=90,
                          max_trend_difference_pct=.20, max_vol_difference_pct=.20,
                          estimated_round_trip_cost_bps=12):
    """One deterministic same-session, same-direction *matched* control per case.

    Control population is generated without future outcomes or knowledge of
    future pressure signals. Matching uses trailing published-only trend and
    volatility; prospective controls selected without replacement. Missing
    controls and censored outcomes stay explicit (no winning-only selection).
    """
    if (int(control_spacing_seconds) <= 0 or int(signal_quiet_seconds) < 0 or
            int(match_window_minutes) <= 0 or float(max_trend_difference_pct) < 0 or
            float(max_vol_difference_pct) < 0 or float(estimated_round_trip_cost_bps) < 0):
        raise QualityError('Invalid control-matching configuration')
    groups = _by_segment(result['buckets'])
    signals = defaultdict(list)
    for e in result['events']:
        signals[int(e['segment_id'])].append(int(e['ready_ms']))
    for s in signals:
        signals[s].sort()
    by_episode = {e['episode_id']:e for e in result['episodes']}
    candidates = []
    for f in result['failed']:
        if f['state'] not in ('FAILED_AGGRESSION','PRESSURE_CONTINUED'):
            continue
        if f['candidate_direction'] not in (-1,1):
            raise QualityError('bad candidate direction')
        segment = int(f['segment_id'])
        context = _published_history(groups[segment],int(f['decision_ms']))
        candidates.append((f,context))

    pools = defaultdict(list)
    for segment, rows in groups.items():
        last_slot = None
        event_times = signals[segment]
        for row in rows:
            at = int(row['available_ms'])
            slot = at // (control_spacing_seconds*1000)
            if slot == last_slot:
                continue
            last_slot = slot
            # Only exclude signals *already published* in trailing quiet window.
            # Do NOT filter out controls due to signals yet to happen.
            at_idx = bisect_right(event_times,at)
            if at_idx and at - event_times[at_idx-1] < signal_quiet_seconds*1000:
                continue
            context = _published_history(rows,at)
            if context is not None:
                pools[segment].append((at,context))

    used = defaultdict(set)
    matched = []
    for f,feature in sorted(candidates,key=lambda x:(x[0]['source_session'],x[0]['decision_ms'],x[0]['event_id'])):
        segment = int(f['segment_id'])
        ep = by_episode[f['episode_id']]
        row = {'episode_id':f['episode_id'], 'event_id':f['event_id'],
               'source_session':f['source_session'], 'segment_id':segment,
               'classification':f['state'], 'original_side':f['original_side'],
               'candidate_side':f['candidate_side'],
               'candidate_direction':int(f['candidate_direction']),
               'candidate_decision_ms':int(f['decision_ms']),
               'candidate_entry_ms':f['entry_ms'],
               'candidate_entry_proxy':f['entry_proxy'],
               'episode_mixed_sides':ep['mixed_sides'],
               'match_state':'UNMATCHED_NO_CANDIDATE_HISTORY' if feature is None else 'UNMATCHED_NO_COMPARABLE_CONTROL',
               'control_decision_ms':None, 'control_entry_ms':None,
               'trend_abs_diff_pct':None,'vol_abs_diff_pct':None,
               'candidate_trend_120s_pct':feature['trend_120s_pct'] if feature else None,
               'candidate_vol_120s_pct':feature['vol_120s_pct'] if feature else None,
               'control_trend_120s_pct':None,'control_vol_120s_pct':None,
               'estimated_round_trip_cost_bps':float(estimated_round_trip_cost_bps)}
        for h in HORIZONS:
            k=f'{h}s'
            row[f'candidate_state_{k}']=f[f'outcome_state_{k}']
            row[f'candidate_return_{k}_pct']=f[f'return_{k}_pct']
            row[f'control_state_{k}']='UNMATCHED'
            row[f'control_return_{k}_pct']=None
            row[f'matched_difference_{k}_pct']=None
            row[f'candidate_net_estimate_{k}_pct']=(None if f[f'return_{k}_pct'] is None else
                float(f[f'return_{k}_pct'])-float(estimated_round_trip_cost_bps)/100)
            row[f'control_net_estimate_{k}_pct']=None
        if feature is not None:
            viable = []
            for t,context in pools[segment]:
                # Controls must finish even their longest (300s) horizon BEFORE
                # the candidate decision: no future comparator data whatsoever.
                if (t in used[segment] or t + max(HORIZONS)*1000 + 3000 > int(f['decision_ms'])
                        or int(f['decision_ms']) - t > match_window_minutes*60000):
                    continue
                td=abs(context['trend_120s_pct']-feature['trend_120s_pct'])
                vd=abs(context['vol_120s_pct']-feature['vol_120s_pct'])
                if td>max_trend_difference_pct or vd>max_vol_difference_pct:
                    continue
                distance=td/max(.0001,max_trend_difference_pct)+vd/max(.0001,max_vol_difference_pct)
                distance+=abs(t-int(f['decision_ms']))/(match_window_minutes*60000)*.10
                viable.append((distance,t,context,td,vd))
            if viable:
                _,control_ms,context,td,vd=min(viable,key=lambda r:(r[0],r[1]))
                used[segment].add(control_ms)
                control=_proxy_outcomes(groups[segment],control_ms,int(f['candidate_direction']))
                row.update(match_state='MATCHED',control_decision_ms=control_ms,
                           control_entry_ms=control['entry_ms'],
                           control_entry_proxy=control['entry_proxy'],
                           control_trend_120s_pct=context['trend_120s_pct'],
                           control_vol_120s_pct=context['vol_120s_pct'],
                           trend_abs_diff_pct=td,vol_abs_diff_pct=vd)
                for h in HORIZONS:
                    k=f'{h}s'
                    row[f'control_state_{k}']=control[f'outcome_state_{k}']
                    row[f'control_return_{k}_pct']=control[f'return_{k}_pct']
                    if row[f'control_return_{k}_pct'] is not None and row[f'candidate_return_{k}_pct'] is not None:
                        row[f'matched_difference_{k}_pct']=row[f'candidate_return_{k}_pct']-row[f'control_return_{k}_pct']
                    if row[f'control_return_{k}_pct'] is not None:
                        row[f'control_net_estimate_{k}_pct']=row[f'control_return_{k}_pct']-float(estimated_round_trip_cost_bps)/100
        matched.append(row)
    return matched


def summarize_quality(episodes, controls, sessions):
    reports=[]
    for s in sessions:
        name=s['tape']
        ee=[e for e in episodes if e['source_session']==name]
        cc=[c for c in controls if c['source_session']==name]
        reports.append({'source_session':name, 'episodes':len(ee),
                        'mixed_episodes':sum(e['mixed_sides'] for e in ee),
                        'failed':sum(e['state']=='FAILED_AGGRESSION' for e in ee),
                        'continued':sum(e['state']=='PRESSURE_CONTINUED' for e in ee),
                        'unresolved':sum(e['state']=='UNRESOLVED' for e in ee),
                        'censored':sum(e['state'].startswith('CENSORED') for e in ee),
                        'matched':sum(c['match_state']=='MATCHED' for c in cc),
                        'unmatched':sum(c['match_state']!='MATCHED' for c in cc)})
    comparison=[]
    for state in ('FAILED_AGGRESSION','PRESSURE_CONTINUED'):
        for side in ('BUY_PRESSURE','SELL_PRESSURE'):
            sample=[c for c in controls if c['classification']==state and c['original_side']==side]
            for h in HORIZONS:
                suffix=f'{h}s'
                observed=[c for c in sample if c[f'matched_difference_{suffix}_pct'] is not None]
                comparison.append({'state':state,'original_side':side,'horizon_s':h,
                                   'candidates':len(sample),'controls_matched':sum(c['match_state']=='MATCHED' for c in sample),
                                   'pairs_observed':len(observed),
                                   'pairs_censored_or_unmatched':len(sample)-len(observed),
                                   'candidate_mean_pct':mean(c[f'candidate_return_{suffix}_pct'] for c in observed) if observed else None,
                                   'control_mean_pct':mean(c[f'control_return_{suffix}_pct'] for c in observed) if observed else None,
                                   'paired_difference_mean_pct':mean(c[f'matched_difference_{suffix}_pct'] for c in observed) if observed else None})
    return {'per_session':reports,'comparison':comparison,
            'episodes_total':len(episodes),
            'matched_cases':sum(c['match_state']=='MATCHED' for c in controls),
            'eligible_cases':len(controls)}


def _software_hashes():
    base=Path(__file__).resolve().parents[2]
    files = ['dashboard/services/micro_flow_v3_quality_baseline.py',
             'dashboard/services/micro_flow_v3_multi_session.py',
             'dashboard/services/micro_flow_v3_dashboard_service.py',
             'dashboard/micro_flow_v3_multi_session_tab.py',
             'dashboard/micro_flow_v3_quality_tab.py',
             'dashboard/services/micro_flow_v3_research.py',
             'dashboard/services/micro_flow_v3_failed_aggression.py',
             'research/micro_flow_v3/tape.py',
             'research/micro_flow_v3/replay.py']
    return {name: _sha(base / name) for name in files}


def _source_records(sessions, root, *, verify_sources=True):
    project = Path(root).resolve()
    records=[]
    for s in sessions:
        summary=s['summary']
        if (summary.get('closed_source_required') is not True or
            not s['info']['closed'] or
            s['info']['provenance']!='LIVE_OBSERVED_NOT_HISTORICAL_BACKFILL'):
            raise QualityError(f'Fuente sin cierre/procedencia V3: {s["tape"]}')
        if hash_replay(s['replay_path'])!=summary['replay_jsonl_sha256']:
            raise QualityError('Replay modificado: '+s['tape'])
        if json.loads(Path(s['summary_path']).read_text(encoding='utf-8')) != summary:
            raise QualityError('Replay summary changed since session analysis: '+s['tape'])
        if verify_sources:
            count,digest=replay_digest(s['tape_path'])
            if count!=summary['source_tape_rows'] or digest!=summary['source_tape_digest_sha256']:
                raise QualityError('El hash del tape no coincide: '+s['tape'])
        try:
            tape_relative=str(Path(s['tape_path']).resolve().relative_to(project))
            replay_relative=str(Path(s['replay_path']).resolve().relative_to(project))
            summary_relative=str(Path(s['summary_path']).resolve().relative_to(project))
        except ValueError as exc:
            raise QualityError('Sources must live within the project root') from exc
        records.append({'tape':s['tape'],
                        'tape_path':tape_relative,
                        'replay_path':replay_relative,
                        'summary_path':summary_relative,
                        'tape_rows':summary['source_tape_rows'],
                        'tape_digest_sha256':summary['source_tape_digest_sha256'],
                        'replay_sha256':summary['replay_jsonl_sha256'],
                        'replay_summary_sha256':_sha(s['summary_path']),
                        'buckets':summary['buckets_emitted'],
                        'segments':len(summary['segments'])})
    return records


def verify_saved_run(run_path, *, verify_sources=False, project_root=None):
    folder=Path(run_path)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest['schema'] != SCHEMA:
        raise QualityError('Unknown persisted research schema')
    expected=hashlib.sha256(_json(manifest['identity']).encode('utf-8')).hexdigest()[:20]
    if manifest['run_id'] != expected or folder.name != expected:
        raise QualityError('Run ID does not match its immutable identity')
    for filename,hash_value in manifest['artifact_sha256'].items():
        if Path(filename).name != filename or _sha(folder/filename)!=hash_value:
            raise QualityError('Artifact hash mismatch: '+filename)
    if verify_sources:
        if project_root is None:
            raise QualityError('project_root required to verify original sources')
        base=Path(project_root).resolve()
        for source in manifest['identity']['sources']:
            tape=base/source['tape_path']
            replay=base/source['replay_path']
            rows,digest=replay_digest(tape)
            if rows != source['tape_rows'] or digest != source['tape_digest_sha256']:
                raise QualityError('Persisted tape digest mismatch: '+source['tape'])
            if _sha(replay) != source['replay_sha256']:
                raise QualityError('Persisted Replay hash mismatch: '+source['tape'])
            if _sha(base/source['summary_path']) != source['replay_summary_sha256']:
                raise QualityError('Persisted Replay summary hash mismatch: '+source['tape'])
    return manifest


def persist_quality_run(root, sessions, result, *, parameters,
                        control_settings=None):
    """Atomic append-only, verifiable snapshot; no source mutations or overwrites.

    A new run ID is content-addressed by source+Replay hashes, configs and code.
    Replicating the same run returns its already verified manifest. Changed
    configs or changed code => new run; corrupted runs block rather than replace.
    """
    control_settings=dict(control_settings or {})
    if not 1 <= len(sessions) <= 4:
        raise QualityError('Expected 1..4 closed sessions')
    sources=_source_records(sessions,root,verify_sources=True)
    software=_software_hashes()
    identity={'schema':SCHEMA, 'sources':sources,'parameters':dict(parameters),
              'control_settings':control_settings,'software_sha256':software,
              'runtime':{'python':platform.python_version(),
                         'sqlite':sqlite3.sqlite_version}}
    # No file mtime or current timestamp in deterministic identity.
    run_id=hashlib.sha256(_json(identity).encode('utf-8')).hexdigest()[:20]
    destination=Path(root)/SOURCE_PATH/run_id
    if destination.exists():
        existing=verify_saved_run(destination)
        if existing['identity'] != identity:
            raise QualityError('Existing run ID collision; refuse overwrite')
        return destination,existing
    episodes=audit_episodes(result)
    control=make_matched_controls(result,**control_settings)
    summary=summarize_quality(episodes,control,sessions)
    base=destination.parent
    base.mkdir(parents=True, exist_ok=True)
    staged=Path(tempfile.mkdtemp(prefix='.pending-',dir=base))
    try:
        content={'episodes.jsonl':episodes,'signals.jsonl':result['events'],
                 'failed_aggression.jsonl':result['failed'],
                 'matched_controls.jsonl':control,
                 'quality_per_session.json':summary['per_session'],
                 'quality_comparison.json':summary['comparison'],
                 'quality_summary.json':{key:value for key,value in summary.items() if key not in ('per_session','comparison')}}
        hashes={}
        for filename,value in content.items():
            file=staged/filename
            if filename.endswith('.jsonl'):
                file.write_text(''.join(_json(row)+'\n' for row in value), encoding='utf-8')
            else:
                file.write_text(_json(value)+'\n',encoding='utf-8')
            hashes[filename]=_sha(file)
        manifest={'schema':SCHEMA,'run_id':run_id,
                  'created_utc':datetime.now(timezone.utc).isoformat(),
                  'identity':identity,'artifact_sha256':hashes,
                  'research_contract':{
                      'raw_source':'closed immutable V3 aggTrade SQLite',
                      'clock':'observed available_ms; strict < decision for matched covariates',
                      'source_verification':'full tape replay_digest and Replay JSONL SHA256',
                      'session_policy':'no state carried over session or segment',
                      'control_policy':'same segment/side; control 300s horizon finished before candidate decision; asof covariates; no future-event exclusions; without replacement',
                      'price_model':'later bucket OPEN proxy, NOT executable fill',
                      'estimated_costs':'user-supplied round-trip bps scenario, not actual commission/slippage',
                      'study_role':'exploratory; not an out-of-sample validation'}}
        (staged/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True,ensure_ascii=False)+'\n',encoding='utf-8')
        if destination.exists():
            return destination,verify_saved_run(destination)
        os.rename(staged,destination)
        return destination,verify_saved_run(destination)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


if __name__ == '__main__':
    import argparse
    ap=argparse.ArgumentParser(description='Verify an immutable Micro Flow V3 quality run')
    ap.add_argument('--verify',type=Path,required=True,help='Path to run folder')
    ap.add_argument('--root',type=Path,required=True,help='Project root containing original tapes/Replay')
    args=ap.parse_args()
    checked=verify_saved_run(args.verify,verify_sources=True,project_root=args.root)
    print('VERIFIED',checked['run_id'],'sources:',len(checked['identity']['sources']),
          'artifacts:',len(checked['artifact_sha256']))
