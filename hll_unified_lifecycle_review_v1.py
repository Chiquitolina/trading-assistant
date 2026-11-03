#!/usr/bin/env python3
"""Read-only report/review of HLL unified lab. Prints data; never trades or writes."""
from __future__ import annotations
import argparse
import json
from collections import Counter,defaultdict
from pathlib import Path

from hll_unified_structure_lifecycle_v1 import DEFAULTS,digest,rows,read_manifest


def verify_report(directory:Path):
    manifest=read_manifest(directory/'manifest.json')
    if manifest.get('version')!='HLL_UNIFIED_STRUCTURE_LIFECYCLE_V1':
        raise ValueError('Wrong HLL unified report version')
    for name,sha in manifest.get('outputs_sha256',{}).items():
        path=directory/name
        if not path.is_file() or digest(path)!=sha:
            raise ValueError('Unified report damaged: '+name)
    return manifest


def review(directory:Path, *, symbol=None,attempt=None,max_rows=20):
    verify_report(directory)
    if attempt and not symbol:
        raise ValueError('Specify --symbol along with --attempt')
    summary=read_manifest(directory/'summary.json')
    attempts=[r for r in rows(directory/'unified_attempts.csv')
               if (not symbol or r['symbol']==symbol) and (not attempt or r['attempt_id']==attempt)]
    if (symbol or attempt) and not attempts:
        raise ValueError('No matching attempts in pilot: '+str(symbol)+' / '+str(attempt))
    ids={r['attempt_id'] for r in attempts}
    entry=[r for r in rows(directory/'unified_entries.csv') if r['attempt_id'] in ids]
    entry_ids={r['entry_id'] for r in entry}
    out=[r for r in rows(directory/'entry_outcomes_1m.csv') if r['entry_id'] in entry_ids]
    ctx=[r for r in rows(directory/'entry_process_context_RETROSPECTIVE.csv') if r['entry_id'] in entry_ids]
    per_route=defaultdict(Counter)
    for r in out:
        if r['scenario']=='TP3_SL1P5_48H':per_route[r['route']][r['state']]+=1
    view={'version':'HLL_UNIFIED_REVIEW_V1',
       'filter':{'symbol':symbol,'attempt':attempt},
       'attempts':len(attempts),'episodes':len({a['episode_id'] for a in attempts}),
       'verified_entries':len(entry),
       'entry_routes':dict(Counter(e['route'] for e in entry)),
       'reference_tp3_sl1p5_48h_by_route':{k:dict(c) for k,c in per_route.items()},
       'physical_event_links':'RETROSPECTIVE_ONLY',
       'process_context_rows':len(ctx),
       'attempt_preview':[{k:a[k] for k in ('attempt_id','symbol','episode_id','episode_ordinal',
              'pre_pattern_96','fingerprint_status','coverage_FLIP_1M',
              'coverage_V29_CONTINUATION_30M','coverage_V29_RECLAIM_30M',
              'linked_physical_processes_retrospective')} for a in attempts[:max_rows]],
       'entry_preview':[{'entry_id':e['entry_id'],'attempt_id':e['attempt_id'],
                          'route':e['route'],'entry_ts':e['entry_ts'],
                          'entry_price':e['entry_price']} for e in entry[:max_rows]],
       'outcome_preview':[{'entry_id':o['entry_id'],'scenario':o['scenario'],
                           'state':o['state'],'mfe_pct':o['mfe_pct'],
                           'mae_pct':o['mae_pct'],'net_pct':o['net_pct']}
                          for o in out[:max_rows]],
       'source_limitation':'Retrospective OHLC, not executed trades or OOS evidence',
    }
    return view


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',type=Path,default=Path(DEFAULTS['output']))
    p.add_argument('--symbol',default=None)
    p.add_argument('--attempt',default=None)
    p.add_argument('--max-rows',type=int,default=12)
    args=p.parse_args(argv)
    if args.max_rows<1 or args.max_rows>500:raise ValueError('max-rows must be between 1 and 500')
    result=review(args.output_dir,symbol=args.symbol,attempt=args.attempt,max_rows=args.max_rows)
    print('HLL_UNIFIED_REVIEW',json.dumps(result,indent=2,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
