#!/usr/bin/env python3
"""Frozen 50-symbol subsample from the 257 untouched historical-replay symbols.

No live trading, no network, no copying of the 33-symbol development records.
Uses the original, previously audited 1m candle snapshots and the exact feature/
label generation implementation from failed_touch_causal_2pct_v1.py.

The validation cohort and the two *preselected* rules are locked before any of
its outcomes are read. The other 207 symbols remain sealed and available. Running --report early is descriptive only, not final.
"""
from __future__ import annotations
import argparse
import hashlib
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from shutil import disk_usage
import statistics

import failed_touch_causal_2pct_v1 as core

VERSION = 'failed-touch-causal-validation-50-v1.0'
SELECTION_SEED = 'failed-touch-causal-holdout50-seed-v1-20261023'
N_HOLDOUT_POOL = 257
N_TOTAL = 290
N_DEVELOPMENT = 33
N_VALIDATION = 50
# Values frozen from the prior exploratory analysis, NOT selected on holdout data.
RULES = {
    'A_WICK50_T0': {'checkpoint_min': 0, 'feature': 'favorable_wick_share', 'operator': '>=', 'threshold': 0.5},
    'B_RETURN5_POS_T15': {'checkpoint_min': 15, 'feature': 'favorable_return_5m_pct', 'operator': '>', 'threshold': 0.0},
}
BASE_RULE = {'depth_pct': 2.0, 'max_minutes_to_depth': 180, 'max_extension_pct': 3.0,
             'structural_horizon_min': 180, 'trade_tp_pct': 2.0, 'trade_sl_pct': 1.5,
             'trade_horizon_min': 180, 'trade_entry': 'next_consecutive_1m_open',
             'checkpoint_must_remain_active': True,
             'fee_slippage_roundtrip_scenarios_pct': [0.10, 0.20]}
FIELDNAMES = ('rule','direction','dedup','group','n','symbols','clusters','recovered','extended','ambiguous',
              'no_decision','censored','decided','recovery_decided_pct',
              'recovery_wilson95_low_pct','recovery_wilson95_high_pct',
              'tp','sl','sl_ambiguous','timeout','trade_censored','pnl_covered',
              'gross_mean_pct','net_mean_cost_0_10_pct','net_mean_cost_0_20_pct')


def get_paths(root):
    folder, cache, oldout, develop = core.selected_paths(root)
    return folder, cache, oldout, develop, folder/'causal_depth2_validation_50_v1', (root/'reports/candidate_long_horizon/candle_cache/15m')


def symbol_is_safe(sym):
    # Binance Futures also admits CJK names: don't silently delete those symbols.
    return sym.endswith('USDT') and len(sym)>4 and all(c.isalnum() for c in sym) and '/' not in sym and '\\' not in sym


def discovery(root):
    _,_,oldout,develop,study,source = get_paths(root)
    frozen_dev = develop/'frozen_development_universe.json'
    if not frozen_dev.is_file():
        raise RuntimeError('Missing frozen development manifest; do not create validation before 33-symbol development study')
    dev = json.loads(frozen_dev.read_text(encoding='utf-8'))
    if dev.get('version')!=core.VERSION or len(dev.get('symbols',[])) != N_DEVELOPMENT:
        raise RuntimeError('Development cohort is not the expected 33 symbols')
    devsyms=sorted(dev['symbols'])
    if len(set(devsyms))!=N_DEVELOPMENT or not core.PILOT.issubset(set(devsyms)):
        raise RuntimeError('Duplicate or missing pilot member in frozen development cohort')
    if not source.is_dir():raise RuntimeError('Missing 290-symbol original 15m universe: '+str(source))
    full=sorted(p.name for p in source.iterdir() if p.is_dir())
    if len(full)!=N_TOTAL or len(set(full))!=N_TOTAL or any(not symbol_is_safe(x) for x in full):
        raise RuntimeError('Original 290-symbol universe changed or has unsafe symbol names')
    if not set(devsyms).issubset(set(full)):
        raise RuntimeError('Development includes symbol not in frozen original universe')
    holdout=sorted(set(full)-set(devsyms))
    if len(holdout)!=N_HOLDOUT_POOL:raise RuntimeError('Holdout candidate pool is not exactly 257 symbols')
    # Random-looking, deterministic, unchangeable assignment from symbol IDs only.
    # Neither candle coverage nor feature/outcome values are used in selection.
    chosen=sorted(sorted(holdout,key=lambda s:(
        hashlib.sha256(f'{SELECTION_SEED}|{s}'.encode('utf-8')).hexdigest(),s))[:N_VALIDATION])
    if len(chosen)!=N_VALIDATION or (set(chosen)&set(devsyms)):
        raise RuntimeError('Invalid frozen 50-symbol selection')
    return devsyms,chosen,core.sha(frozen_dev),full


def expected_symbol_file(study,selected):
    return '\n'.join(selected)+'\n'


def write_symbol_file(study,selected):
    file=study/'holdout50_symbols.txt'
    content=expected_symbol_file(study,selected)
    if file.exists():
        if file.read_text(encoding='utf-8')!=content:
            raise RuntimeError('Frozen holdout50_symbols.txt changed; refusing to alter selection')
    else:
        file.parent.mkdir(parents=True,exist_ok=True)
        tmp=file.with_suffix('.tmp')
        tmp.write_text(content,encoding='utf-8')
        tmp.replace(file)
    return file


def freeze(root):
    _,_,_,_,study,_=get_paths(root)
    devsyms,holdout,source_sha,full=discovery(root)
    dest=study/'frozen_validation_plan.json'
    spec=dict(version=VERSION,cohort='HOLDOUT_50_FROM_257_NOT_USED_FOR_RULE_SELECTION',
              development_symbols=devsyms,validation_symbols=holdout,
              original_symbols=full,selection_seed=SELECTION_SEED,
              reserved_unselected_symbols=sorted(set(full)-set(devsyms)-set(holdout)),
              dev_frozen_sha256=source_sha,
              core_implementation_sha256=core.sha(Path(core.__file__)),
              base=BASE_RULE, hypotheses=RULES,
              note='Frozen prior to inspecting any holdout feature/outcome')
    if dest.exists():
        current=json.loads(dest.read_text(encoding='utf-8'))
        if current!=spec:raise RuntimeError('Frozen validation plan differs; refuse overwrite')
        write_symbol_file(study,current['validation_symbols'])
        return current,False
    dest.parent.mkdir(parents=True,exist_ok=True)
    core.atomic_json(dest,spec)
    write_symbol_file(study,spec['validation_symbols'])
    return spec,True


def locked(root):
    _,_,_,_,study,_=get_paths(root)
    path=study/'frozen_validation_plan.json'
    if not path.is_file():raise RuntimeError('First execute --freeze BEFORE holdout analysis')
    spec=json.loads(path.read_text(encoding='utf-8'))
    devsyms,holdout,source_sha,full=discovery(root)
    expected=dict(version=VERSION,cohort='HOLDOUT_50_FROM_257_NOT_USED_FOR_RULE_SELECTION',
                  development_symbols=devsyms,validation_symbols=holdout,
                  original_symbols=full,selection_seed=SELECTION_SEED,
                  reserved_unselected_symbols=sorted(set(full)-set(devsyms)-set(holdout)),
                  dev_frozen_sha256=source_sha,
                  core_implementation_sha256=core.sha(Path(core.__file__)),
                  base=BASE_RULE,hypotheses=RULES,
                  note='Frozen prior to inspecting any holdout feature/outcome')
    if spec!=expected:raise RuntimeError('Frozen plan, data universe or causal core code has changed')
    symbol_file=study/'holdout50_symbols.txt'
    if not symbol_file.is_file() or symbol_file.read_text(encoding='utf-8')!=expected_symbol_file(study,spec['validation_symbols']):
        raise RuntimeError('Frozen holdout50_symbols.txt missing or changed')
    return spec


def eligible(row, rule):
    if row.get('checkpoint_status')!='ACTIVE':return False
    if int(row['checkpoint_min'])!=rule['checkpoint_min']:return False
    value=core.fnum(row.get(rule['feature']))
    if value is None:return False
    return value>=rule['threshold'] if rule['operator']=='>=' else value>rule['threshold']


def deduplicate(rows):
    """Select first stable level by event ID, before knowing which signal it passed."""
    by_cluster={}
    for r in sorted(rows,key=lambda x:(x['cluster_key'],x['event_key'])):
        by_cluster.setdefault(r['cluster_key'],r)
    return list(by_cluster.values())


def stat(rows,rule,side,dedup,group):
    selected=[r for r in rows if side=='ALL' or r['side']==side]
    if group=='SELECTED':selected=[r for r in selected if eligible(r,rule)]
    elif group=='REJECTED':selected=[r for r in selected if not eligible(r,rule)]
    # 'BASELINE' includes all ACTIVE rows for the same checkpoint.
    outcomes=Counter(r['structural_outcome'] for r in selected)
    trades=Counter(r['trade_outcome'] for r in selected)
    good=outcomes['RECOVERY_FIRST'];bad=outcomes['CONTINUATION_FIRST'];d=good+bad
    gross=[core.fnum(r.get('trade_gross_pct')) for r in selected
           if r['trade_outcome'] in ('TP_FIRST','SL_FIRST','TIMEOUT')]
    gross=[g for g in gross if g is not None]
    wl,wh=core.wilson(good,d)
    mean=statistics.mean(gross) if gross else None
    return dict(rule=rule['_name'],direction=side,dedup=dedup,group=group,n=len(selected),
                symbols=len(set(r['symbol'] for r in selected)),
                clusters=len(set(r['cluster_key'] for r in selected)),
                recovered=good,extended=bad,ambiguous=outcomes['AMBIGUOUS'],
                no_decision=outcomes['NO_DECISION'],censored=outcomes['CENSORED'],
                decided=d,recovery_decided_pct=round(good*100/d,3) if d else '',
                recovery_wilson95_low_pct=wl,recovery_wilson95_high_pct=wh,
                tp=trades['TP_FIRST'],sl=trades['SL_FIRST'],
                sl_ambiguous=trades['SL_AMBIGUOUS'],timeout=trades['TIMEOUT'],
                trade_censored=trades['CENSORED'],pnl_covered=len(gross),
                gross_mean_pct=round(mean,5) if mean is not None else '',
                net_mean_cost_0_10_pct=round(mean-0.1,5) if mean is not None else '',
                net_mean_cost_0_20_pct=round(mean-0.2,5) if mean is not None else '')


def compute_summary(all_snapshots):
    out=[]
    for name,raw in RULES.items():
        rule=dict(raw,_name=name)
        eligible_base=[r for r in all_snapshots if r['checkpoint_status']=='ACTIVE'
                       and int(r['checkpoint_min'])==rule['checkpoint_min']]
        # Sensitivity: use either all levels or one predetermined level/contact.
        for dedup in ('ALL_LEVELS','ONE_LEVEL_PER_TOUCH'):
            pool=eligible_base if dedup=='ALL_LEVELS' else deduplicate(eligible_base)
            for side in ('ALL','LONG','SHORT'):
                for group in ('BASELINE','SELECTED','REJECTED'):
                    if group=='BASELINE':
                        # The BASELINE cohort is the same exact checkpoint population.
                        part=[r for r in pool if side=='ALL' or r['side']==side]
                        baseline_rule=dict(rule)
                        z=stat(part,baseline_rule,'ALL',dedup,'BASELINE')
                        z['direction']=side
                        out.append(z)
                    else:out.append(stat(pool,rule,side,dedup,group))
    return out


def verified_completion(root):
    spec=locked(root)
    _,_,oldout,_,study,_=get_paths(root)
    completed=[];issues=[]
    for sym in spec['validation_symbols']:
        f=study/sym/'manifest.json'
        if not f.is_file():continue
        try:
            if not (oldout/sym/'manifest.json').is_file():raise RuntimeError('Missing upstream manifest')
            core.verify_output(study,sym,core.sha(oldout/sym/'manifest.json'))
            completed.append(sym)
        except Exception as exc:issues.append((sym,str(exc)))
    return spec,completed,issues


def report(root):
    spec,completed,issues=verified_completion(root)
    if issues:raise RuntimeError('Failed validation output hash check: '+str(issues[:3]))
    _,_,_,_,study,_=get_paths(root)
    # No holdout outcomes or feature-selection tables before the entire locked
    # cohort is processed: prevents sequential peeking and post-hoc retuning.
    if len(completed)!=N_VALIDATION:
        progress=dict(version=VERSION,scope='HOLDOUT_50',
                      symbols_complete=len(completed),symbols_pending=N_VALIDATION-len(completed),
                      is_final=False,holdout_outcomes_sealed=True,
                      note='Do not inspect validation outcomes until 50/50 are complete')
        core.atomic_json(study/'holdout_progress.json',progress)
        print('FAILED_TOUCH_VALIDATION_PROGRESS',json.dumps(progress,ensure_ascii=False),flush=True)
        return progress
    feature_rows=[];census=[]
    for sym in completed:
        feature_rows+=core.read_csv(study/sym/'snapshot_features_outcomes.csv')
        census+=core.read_csv(study/sym/'event_census.csv')
    rows=compute_summary(feature_rows)
    core.atomic_csv(study/'holdout_frozen_rules_summary.csv',rows,FIELDNAMES)
    core.atomic_csv(study/'holdout_event_census.csv',census,core.EVENT_FIELDS)
    # No extra wide all-snapshot CSV: maintain immutable per-symbol sources.
    counts=Counter(x['depth2_status'] for x in census)
    summary=dict(version=VERSION,scope='VALIDATION_HOLDOUT_50',
                 frozen_universe_size=N_VALIDATION,completed=len(completed),
                 pending=N_VALIDATION-len(completed),is_final=len(completed)==N_VALIDATION,
                 completed_symbols=completed,failed_events=len(census),
                 reached_2pct_in_180min=counts['DEPTH_2_WITHIN_180'],
                 predefined_rules=RULES,predefined_base=BASE_RULE,
                 dedup_sensitivity='ONE_LEVEL_PER_TOUCH uses lowest event_key prior to filter, not portfolio execution',
                 notes=['All entries next 1m open; structural labels future-only',
                        'No fee/slippage measured; 0.10% and 0.20% are fixed assumed roundtrip costs',
                        'Costs subtracted only from resolved or TIMEOUT gross, not censored/ambiguous',
                        'Events correlated across symbols/dates; Wilson intervals are per-event and optimistic under clustering',
                        'Partial results are not a completed holdout validation; do not retune thresholds',
                        'Different checkpoints select survivor cohorts, not same opportunities',
                        'No 1-slot portfolio simulation or market capacity constraints'])
    core.atomic_json(study/'holdout_summary.json',summary)
    printed=[r for r in rows if r['direction']=='ALL' and r['dedup']=='ONE_LEVEL_PER_TOUCH'
             and r['group'] in ('BASELINE','SELECTED','REJECTED')]
    print('FAILED_TOUCH_VALIDATION_REPORT',json.dumps(dict(
        complete=summary['is_final'],symbols_complete=summary['completed'],
        symbols_pending=summary['pending'],failed_events=summary['failed_events'],
        reached_depth2=summary['reached_2pct_in_180min'],
        key_groups=printed,output=str(study)),ensure_ascii=False),flush=True)
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    actions=p.add_mutually_exclusive_group(required=True)
    for f in ('plan','freeze','run','report','audit'):actions.add_argument('--'+f,action='store_true')
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--batch-size',type=int,default=30)
    p.add_argument('--min-free-gb',type=float,default=4.0)
    args=p.parse_args(argv)
    if args.batch_size<1:raise ValueError('--batch-size must be positive')
    root=args.project.resolve()
    folder,cache,oldout,dev,study,source=get_paths(root)
    if args.freeze:
        spec,created=freeze(root)
        print('FAILED_TOUCH_VALIDATION_FROZEN',json.dumps(dict(created=created,
              development=len(spec['development_symbols']),holdout=len(spec['validation_symbols']),
              reserved=len(spec['reserved_unselected_symbols']),
              symbols_file=str(study/'holdout50_symbols.txt'),
              rules=RULES,folder=str(study)),ensure_ascii=False));return
    if args.plan:
        if (study/'frozen_validation_plan.json').is_file():
            spec=locked(root)
        else:
            devsyms,holdout,_,_=discovery(root)
            spec={'validation_symbols':holdout}
        completed=[s for s in spec['validation_symbols'] if (study/s/'manifest.json').is_file()]
        upstream=[s for s in spec['validation_symbols'] if (oldout/s/'manifest.json').is_file()]
        print('FAILED_TOUCH_VALIDATION_PLAN',json.dumps(dict(
             frozen=(study/'frozen_validation_plan.json').is_file(),
             holdout_symbols=N_VALIDATION,reserved_symbols=N_HOLDOUT_POOL-N_VALIDATION,
             symbols_file=str(study/'holdout50_symbols.txt'),
             original_replays_available=len(upstream),
             validation_completed=len(completed),
             next_pending_with_upstream=[s for s in upstream if s not in completed][:20],
             original_replays_missing=N_VALIDATION-len(upstream),
             free_gb=round(disk_usage(root).free/1024**3,2),
             output=str(study)),ensure_ascii=False,indent=2));return
    spec=locked(root)
    if args.audit:
        _,completed,issues=verified_completion(root)
        print('FAILED_TOUCH_VALIDATION_AUDIT',json.dumps(dict(
              valid=len(completed),pending=N_VALIDATION-len(completed),
              issues=[dict(symbol=s,error=e) for s,e in issues],
              success=not issues,all_holdout_completed=len(completed)==N_VALIDATION),ensure_ascii=False))
        if issues:raise RuntimeError('Validation audit issues')
        return
    if args.report:
        report(root);return
    # Run only if the upstream Legacy first-touch replay is already complete for a holdout symbol.
    import legacy_first_touch_replay_14d_v1 as replay
    from pre_reaction_1m_backfill_v0 import check_disk
    a,b=replay.bounds(replay.iso_ms(replay.DEFAULT_START),replay.iso_ms(replay.DEFAULT_END))
    completed=0
    for sym in spec['validation_symbols']:
        if (study/sym/'manifest.json').is_file():
            core.verify_output(study,sym,core.sha(oldout/sym/'manifest.json'))
            continue
        if not (oldout/sym/'manifest.json').is_file():continue
        if completed>=args.batch_size:break
        check_disk(study,args.min_free_gb)
        result=core.process_one(root,cache,oldout,study,sym,replay,a,b)
        completed+=1
        print('FAILED_TOUCH_VALIDATION_SYMBOL',json.dumps(result,ensure_ascii=False),flush=True)
    print('FAILED_TOUCH_VALIDATION_BATCH',json.dumps(dict(newly_completed=completed,
          study=str(study)),ensure_ascii=False),flush=True)
    report(root)


if __name__=='__main__':main()
