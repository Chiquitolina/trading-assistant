#!/usr/bin/env python3
"""Export frozen, verified HLL V2.1 PRE markers for the existing Runner Discovery overlay.

Scans each symbol's verified frozen 30m candles ONCE, filters the original
HLL event stream to levels appearing in a frozen Reaction x HLL join, and
publishes only TEST_FROM_*, DEFENSE_CONFIRMED and LOSS_CONFIRMED observations.
The UI additionally filters strictly observed_closed_ts < each touch's retest_ts.

No exchanges, Redis, LIVE actions, outcomes, or alteration of existing frozen runs.
Output is an independent new immutable run, under
reports/reaction_lab/universal_runner_hll_pre_markers_v1/runs/<join-run-name>.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile

import pandas as pd

from dashboard.views.reaction_hll_confluence_overlay_v1 import verified_run

PROJECT = Path('/home/multiservices-1m')
DEFAULT_JOIN = Path('reports/reaction_lab/universal_runner_hll_lifecycle_join_v1/runs/pilot_5_hll_extended_20261108')
OUT_BASE = Path('reports/reaction_lab/universal_runner_hll_pre_markers_v1/runs')
VERSION = 'universal-runner-hll-pre-markers-v1.0'
TF30 = 1_800_000
EVENT_TYPES = frozenset({'TEST_FROM_ABOVE','TEST_FROM_BELOW','DEFENSE_CONFIRMED','LOSS_CONFIRMED'})
COLUMNS = ('event_id','symbol','level_id','origin','event_type','observed_closed_ts',
           'bar_open_ts','high','low','close','test_id','test_from')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def resolve_inside(project, requested):
    project = Path(project).resolve()
    p = Path(requested)
    result = (p if p.is_absolute() else project/p).resolve()
    if result != project and project not in result.parents:
        raise ValueError('Fuente fuera del proyecto')
    return result


def read_csv(path):
    with Path(path).open(newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def candidate_limits(join_labels, candidates):
    """A line is eligible only if a frozen, complete event references it causally."""
    indexed = {}
    for r in join_labels:
        key = r['touch_key']
        if key in indexed:
            raise ValueError('Duplicate frozen touch key')
        indexed[key] = r
    result = defaultdict(dict)
    for c in candidates:
        r = indexed.get(c['touch_key'])
        if r is None or r['hll_coverage'] != 'COMPLETE':
            raise ValueError('Candidate not tied to complete frozen event')
        if c['symbol'] != r['symbol'] or int(c['retest_ts']) != int(r['retest_ts']):
            raise ValueError('Candidate event identity mismatch')
        cutoff = int(r['retest_ts'])
        if int(c['formation_closed_ts']) >= cutoff:
            raise ValueError('HLL candidate formed in future')
        key = c['level_id']
        current = result[c['symbol']].get(key)
        if current:
            if any(current[k] != c[k] for k in
                   ('origin','center','zone_low','zone_high','formation_closed_ts')):
                raise ValueError('Inconsistent HLL candidate geometry')
            current['max_cutoff'] = max(current['max_cutoff'], cutoff)
        else:
            result[c['symbol']][key] = {
                **{k:c[k] for k in ('origin','center','zone_low','zone_high','formation_closed_ts')},
                'max_cutoff':cutoff,
            }
    return dict(result)


def extract_markers(symbol, limits, formations, observations):
    """Validate source geometry/identities and export only source-confirmed PRE marks."""
    formation_map={x['level_id']: x for x in formations}
    if len(formation_map) != len(formations):
        raise ValueError('Repeated HLL formation id')
    for level_id, ref in limits.items():
        found = formation_map.get(level_id)
        if found is None or found['symbol'] != symbol or found['origin'] != ref['origin']:
            raise ValueError('Frozen candidate HLL level not reproduced')
        if int(found['formation_closed_ts']) != int(ref['formation_closed_ts']):
            raise ValueError('HLL confirmation time changed')
        for k in ('center','zone_low','zone_high'):
            if not math.isclose(float(found[k]),float(ref[k]),rel_tol=1e-10,abs_tol=1e-10):
                raise ValueError('HLL band geometry changed')
    seen=set()
    kept=[]
    for e in observations:
        level_id=e['level_id']
        if level_id not in limits or e['event_type'] not in EVENT_TYPES:
            continue
        cutoff=limits[level_id]['max_cutoff']
        ts=int(e['observed_closed_ts']); bar=int(e['bar_open_ts'])
        if ts >= cutoff:
            continue
        if ts != bar+TF30 or ts <= int(limits[level_id]['formation_closed_ts']):
            raise ValueError('Noncausal or unclosed HLL observation')
        if e['symbol'] != symbol or e['origin'] != limits[level_id]['origin']:
            raise ValueError('HLL observation identity mismatch')
        event_id=e['event_id']
        if event_id in seen:
            raise ValueError('Duplicate HLL observation')
        seen.add(event_id)
        high,low,close=(float(e[k]) for k in ('high','low','close'))
        if not (all(math.isfinite(v) and v>0 for v in (high,low,close)) and low<=close<=high):
            raise ValueError('Invalid HLL marker OHLC')
        kept.append({k:e.get(k,'') for k in COLUMNS})
    kept.sort(key=lambda e: (int(e['observed_closed_ts']), e['level_id'],e['event_id']))
    return kept


def run(project, join_dir, *, plan=False,min_free_gb=10):
    root=Path(project).resolve()
    join=resolve_inside(root,join_dir)
    audit=verified_run(root,join)
    if audit.get('coverage',{}).get('UNKNOWN_HLL_WARMUP',0):
        raise ValueError('Join HLL has unknown warmup, use extended verified history')
    source=resolve_inside(root,audit['offline_history_dir'])
    labels=read_csv(join/'event_hll_labels.csv')
    candidates=read_csv(join/'hll_candidates_near_1atr.csv')
    limits=candidate_limits(labels,candidates)
    by_symbol=sorted({x['symbol'] for x in labels})
    if len(by_symbol)!=int(audit['symbols']) or len(labels)!=int(audit['events']):
        raise ValueError('Frozen join audit counts changed')
    dst=root/OUT_BASE/join.name
    info={'schema':VERSION,'join_dir':str(join),'output_dir':str(dst),
          'symbols':len(by_symbol),'candidate_levels':sum(map(len,limits.values())),
          'mode':'PLAN_READ_ONLY' if plan else 'EXPORT_OFFLINE'}
    print('HLL_PRE_MARKERS_PLAN',json.dumps(info,ensure_ascii=False),flush=True)
    if plan:
        return info
    if dst.exists():
        raise FileExistsError('An immutable marker export already exists: '+str(dst))
    if shutil.disk_usage(root).free/1024**3 < min_free_gb:
        raise RuntimeError('Espacio libre insuficiente')

    import horizontal_level_lifecycle_v21 as hll
    import universal_runner_horizontal_history_v1 as history
    if hll.VERSION != audit.get('hll_scanner'):
        raise RuntimeError('HLL scanner version differs from frozen join')
    dst.parent.mkdir(parents=True,exist_ok=True)
    counts=Counter()
    symbol_hashes={}
    with tempfile.TemporaryDirectory(prefix='.markers_build_',dir=dst.parent) as td:
        tmp=Path(td)
        (tmp/'symbols').mkdir()
        for i,symbol in enumerate(by_symbol,1):
            manifest=source/'symbols'/symbol/'manifest.json'
            if sha(manifest) != audit['offline_manifest_sha256'].get(symbol):
                raise ValueError('Historical manifest changed: '+symbol)
            meta=json.loads(manifest.read_text(encoding='utf-8'))
            if meta['symbol'] != symbol:
                raise ValueError('Historical symbol mismatch')
            validated=history.verify_symbol(source,symbol,int(meta['start']),int(meta['end']))
            if validated is None or any(validated['timeframes'][tf]['validation']['status'] != 'FULL'
                                        for tf in ('15m','30m')):
                raise ValueError('Historical OHLC not verified: '+symbol)
            candles=pd.read_csv(source/'symbols'/symbol/'30m.csv',encoding='utf-8-sig')
            formations,observations,ends,diag=hll.scan_symbol(
                symbol,candles,hll.Config(),source='UNIVERSAL_OFFLINE_VERIFIED_30M')
            if diag['status']!='COMPLETE' or diag['gaps30']!=0:
                raise ValueError('HLL rescan failed: '+symbol)
            markers=extract_markers(symbol,limits.get(symbol,{}),formations,observations)
            filename=tmp/'symbols'/f'{symbol}.csv'
            with filename.open('w',newline='',encoding='utf-8') as f:
                writer=csv.DictWriter(f,fieldnames=COLUMNS)
                writer.writeheader();writer.writerows(markers)
            symbol_hashes[symbol]=sha(filename)
            counts.update(x['event_type'] for x in markers)
            if i%5==0 or i==len(by_symbol):
                print('HLL_PRE_MARKERS_PROGRESS',json.dumps({
                    'symbols_done':i,'total_symbols':len(by_symbol),
                    'markers_done':sum(counts.values())}),flush=True)
        manifest={
            'schema':VERSION,
            'source_join_dir':str(join),
            'source_join_audit_sha256':sha(join/'audit.json'),
            'source_join_labels_sha256':sha(join/'event_hll_labels.csv'),
            'source_join_candidates_sha256':sha(join/'hll_candidates_near_1atr.csv'),
            'source_offline_history':str(source),
            'hll_scanner_version':hll.VERSION,
            'symbols':len(by_symbol),
            'event_type_counts':dict(counts),
            'file_sha256':symbol_hashes,
            'strictly_pre_to_max_retest':True,
            'individual_touch_requires_asof_filter':True,
            'reads_outcomes':False,
            'modifies_existing_join':False,
        }
        (tmp/'audit.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        # Rename prepared snapshot atomically; original HLL audit and sources untouched.
        tmp.rename(dst)
    final={'schema':VERSION,'output_dir':str(dst),'symbols':len(by_symbol),
           'event_type_counts':dict(counts),'source_join_audit_sha256':manifest['source_join_audit_sha256']}
    print('HLL_PRE_MARKERS_RESULT',json.dumps(final,ensure_ascii=False),flush=True)
    return final


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    p.add_argument('--project',type=Path,default=PROJECT)
    p.add_argument('--hll-run-dir',type=Path,default=DEFAULT_JOIN)
    p.add_argument('--min-free-gb',type=float,default=10.0)
    args=p.parse_args(argv)
    run(args.project,args.hll_run_dir,plan=args.plan,min_free_gb=args.min_free_gb)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
