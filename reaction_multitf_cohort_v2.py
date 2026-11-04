#!/usr/bin/env python3
"""Cohort runner/collector for READ-ONLY Reaction multitimeframe audit V1.

Plan before seeing outcomes; deterministic symbol ranking, fixed UTC time blocks.
Run only discovery until research hypotheses are frozen; holdout requires explicit gate.
Never changes scanner, dashboard, LIVE or trading behavior.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pandas as pd

VERSION = 'REACTION_MULTITF_COHORT_V2'
SEED = 'reaction-multitf-cohort-20261104-frozen-v1'
DISCOVERY_CUTS = ('2026-09-14T14:00:00Z', '2026-09-19T14:00:00Z')
HOLDOUT_CUTS = ('2026-09-25T14:00:00Z', '2026-09-30T14:00:00Z')
EXCLUDE = frozenset(('DYDXUSDT','CAKEUSDT'))  # Already used to develop hypotheses.
PRE_CANDLES = 96  # exactly 48h, nonoverlapping outcome windows per stage
SOURCE_FILES = (
    'dashboard/app.py', 'engine/live/research/swing_detector.py',
    'dashboard/views/hll_reaction_pre_overlay.py',
    'audit_reaction_multitf_v1.py', 'audit_reaction_bidirectional_breakout_v2.py',
    'audit_reaction_defenses_v1.py', 'compare_reaction_confirmations_v1.py',
    'compare_first_touch_confirmations_v2.py',
)
SUFFIXES = dict(events='multitf_events.csv',summary='multitf_summary.csv',
                context='htf_context_15m_events.csv',context_summary='htf_context_summary.csv',
                manifest='multitf_manifest.json')


def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for buf in iter(lambda:f.read(1024*1024),b''):
            h.update(buf)
    return h.hexdigest()


def fingerprint(root: Path) -> dict[str,str]:
    result={}
    for rel in SOURCE_FILES:
        f=root/rel
        if not f.is_file():
            raise FileNotFoundError(f'Required research source unavailable: {f}')
        result[rel]=sha256(f)
    return result


def inventory(root:Path) -> list[str]:
    minutes=root/'reports/horizontal_level_full290_v301/minutes'
    manifests=root/'reports/horizontal_level_full290_v301/minutes_manifest'
    if not minutes.is_dir() or not manifests.is_dir():
        raise FileNotFoundError('HLL 1m files + minutes_manifest not found; no invented coverage')
    result=[]
    for p in manifests.glob('*.json'):
        symbol=p.stem
        if not symbol or not symbol.replace('_','').isalnum() or symbol in EXCLUDE:
            continue
        if (minutes/(symbol+'_1m.csv')).is_file():
            result.append(symbol)
    return sorted(set(result))


def select_symbols(symbols:list[str],max_symbols:int) -> list[str]:
    if max_symbols<1:raise ValueError('max_symbols must be positive')
    # Stable under changes to file discovery order. Selection uses no outcomes or price data.
    return sorted(symbols,key=lambda s:(hashlib.sha256((SEED+'|'+s).encode()).hexdigest(),s))[:max_symbols]


def make_plan(root:Path,max_symbols:int=30):
    inv=inventory(root)
    chosen=select_symbols(inv,max_symbols)
    if not chosen:raise ValueError('No usable verified 1m file/manifest pairs were found')
    return {'version':VERSION,'created_utc':datetime.now(timezone.utc).isoformat(),
            'root':str(root.resolve()), 'seed':SEED,'selection_rule':'sha256(seed|symbol) ascending',
            'universe_n_with_files':len(inv),'max_symbols_requested':max_symbols,
            'selected_symbols':chosen,'excluded_development_symbols':sorted(EXCLUDE),
            'discovery_cutoffs_utc':list(DISCOVERY_CUTS),
            'holdout_cutoffs_utc':list(HOLDOUT_CUTS),
            'pre_candles_30m':PRE_CANDLES,'context_max_age_min':1440,
            'source_sha256':fingerprint(root),
            'caveats':['Inventory tests file existence only, not complete temporal coverage',
                       'Data gaps and tails are failures/censored, never inferred as negative signals',
                       'Symbols chosen without outcome knowledge; no replacement for failures',
                       'Holdout is not to be viewed until analysis and hypothesis are frozen',
                       'Historical replay parity with HLL overlay is NOT online scanner parity']}


def validate_plan(root:Path,plan:dict):
    if plan.get('version')!=VERSION or plan.get('seed')!=SEED or plan.get('pre_candles_30m')!=PRE_CANDLES:
        raise ValueError('Unsupported or modified cohort plan')
    if not plan.get('selected_symbols') or len(set(plan['selected_symbols']))!=len(plan['selected_symbols']):
        raise ValueError('Empty / duplicate symbol selection in plan')
    if list(plan.get('discovery_cutoffs_utc',[]))!=list(DISCOVERY_CUTS) or list(plan.get('holdout_cutoffs_utc',[]))!=list(HOLDOUT_CUTS):
        raise ValueError('Plan time cutoffs not matching predeclared experiment')
    if set(plan['selected_symbols']) & EXCLUDE:
        raise ValueError('Pilot symbols leaked into new unbiased cohort')
    if plan.get('source_sha256')!=fingerprint(root):
        raise RuntimeError('Source code changed since cohort freeze: do NOT mix experiments or overwrite results')


def case_paths(root:Path,stage:str,cutoff:str,symbol:str) -> dict[str,Path]:
    stamp=cutoff.replace('-','').replace(':','').replace('T','_').replace('Z','Z')
    base=root/'reports/reaction_multitimeframe_audit_v1/cohort_v2'/stage/stamp/symbol
    return {kind:base/(symbol+'_'+suffix) for kind,suffix in SUFFIXES.items()}


def cases(plan:dict,stage:str):
    if stage not in ('discovery','holdout'):raise ValueError('Unknown stage')
    for cutoff in plan[stage+'_cutoffs_utc']:
        for symbol in plan['selected_symbols']:
            yield stage,cutoff,symbol


def verify_case(paths:dict[str,Path],symbol:str,cutoff:str,src:dict[str,str]):
    meta_path=paths['manifest']
    if not meta_path.is_file():raise FileNotFoundError(meta_path)
    meta=json.loads(meta_path.read_text(encoding='utf-8'))
    if meta.get('version')!='REACTION_MULTITF_SWING_AUDIT_V1' or meta.get('symbol')!=symbol:
        raise ValueError('Manifest identity/version mismatch')
    if meta.get('pre_cutoff_utc')!=cutoff or meta.get('pre_candles_30m')!=PRE_CANDLES or meta.get('htf_context_max_age_min')!=1440:
        raise ValueError('Manifest dates/window/context do not match locked cohort')
    if meta.get('15m_overlay_exact_parity') is None:
        raise ValueError('Missing hard 15m replay parity')
    for source,expected in src.items():
        # V1 manifests record their own script as auditor_sha256, not source_sha256.
        # audit_reaction_defenses_v1 is a transitive import but isn't in the
        # original V1 manifest; runner's frozen source fingerprint guards it.
        if source=='audit_reaction_multitf_v1.py':
            actual=meta.get('auditor_sha256')
        elif source=='audit_reaction_defenses_v1.py':
            continue
        else:
            actual=meta.get('source_sha256',{}).get(source)
        if actual!=expected:
            raise ValueError('Source fingerprint does not match locked cohort: '+source)
    for name in ('events','summary','context','context_summary'):
        path=paths[name]
        if not path.is_file():raise FileNotFoundError(path)
        if sha256(path)!=meta.get('output_sha256',{}).get(name):
            raise ValueError('Output CSV hash mismatch: '+str(path))
    return meta


def run_stage(root:Path,plan:dict,stage:str,max_cases:int|None=None,allow_holdout=False,timeout_s:int=900):
    validate_plan(root,plan)
    if stage=='holdout' and not allow_holdout:
        raise PermissionError('Holdout locked. Freeze hypotheses first; rerun with --allow-holdout only after that.')
    if max_cases is not None and max_cases<1:raise ValueError('max-cases must be >0')
    done=skipped=failed=0
    for idx,(_,cutoff,symbol) in enumerate(cases(plan,stage)):
        paths=case_paths(root,stage,cutoff,symbol)
        if paths['manifest'].exists():
            verify_case(paths,symbol,cutoff,plan['source_sha256'])
            skipped+=1
            print('COHORT_V2_RESUME_OK',stage,cutoff,symbol,flush=True)
            continue
        # Select number of NEW cases to run; resumability remains deterministic.
        if max_cases is not None and done+failed>=max_cases:break
        if any(p.exists() for k,p in paths.items() if k!='manifest'):
            raise RuntimeError('Partial unverified outputs exist; inspect manually, refuse to overwrite: '+str(paths['events'].parent))
        cmd=[sys.executable,str(root/'audit_reaction_multitf_v1.py'),
             '--root',str(root),'--symbol',symbol,'--pre-cutoff-utc',cutoff,
             '--pre-candles',str(PRE_CANDLES),'--htf-context-max-age-min','1440',
             '--out',str(paths['events'].parent)]
        try:
            r=subprocess.run(cmd,cwd=root,capture_output=True,text=True,timeout=timeout_s)
            if r.returncode:
                raise RuntimeError(f'returncode={r.returncode}; stdout={r.stdout[-1000:]}; stderr={r.stderr[-1500:]}')
            verify_case(paths,symbol,cutoff,plan['source_sha256'])
            if fingerprint(root)!=plan['source_sha256']:
                raise RuntimeError('Canonical source files mutated during run')
            print('COHORT_V2_COMPLETE',stage,cutoff,symbol,flush=True)
            done+=1
        except Exception as exc:
            # If no output was written, record a permanent failure distinct from censored events.
            path=paths['events'].parent/'_cohort_failure.json'
            path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():
                path.write_text(json.dumps({'stage':stage,'cutoff':cutoff,'symbol':symbol,'error':str(exc)},indent=2),encoding='utf-8')
            failed+=1
            print('COHORT_V2_FAILED',stage,cutoff,symbol,str(exc)[:400],flush=True)
    print(json.dumps({'stage':stage,'new_complete':done,'new_failures':failed,'resumed_ok':skipped},ensure_ascii=False))
    return done,failed,skipped


def pct(n:int,d:int):return round(100*n/d,2) if d else None


def _count_complete_race(z:pd.DataFrame,horizon:int=30,close_band:bool=False):
    field=f'original_{horizon}m_race_close_band_0_5' if close_band else f'original_{horizon}m_race_0_5'
    complete=z.loc[z[f'original_{horizon}m_coverage']=='COMPLETE']
    return len(complete),int(complete[field].eq('FAVORABLE_FIRST').sum()),int(complete[field].eq('INVALIDATION_FIRST').sum()),int(complete[field].eq('AMBIGUOUS').sum())


def _group(z:pd.DataFrame,keys:list[str],direct:bool) -> pd.DataFrame:
    out=[]
    for combo,g in z.groupby(keys,dropna=False,sort=True):
        if not isinstance(combo,tuple):combo=(combo,)
        row={k:v for k,v in zip(keys,combo)}
        row.update(n=int(len(g)),symbols=int(g.symbol.nunique()),symbol_dates=int(g[['symbol','pre_cutoff_utc']].drop_duplicates().shape[0]))
        for h in (30,60):
            n,win,loss,ambig=_count_complete_race(g,h,False)
            nc,wc,lc,ac=_count_complete_race(g,h,True)
            row.update({f'{h}m_complete':n,f'{h}m_wins_wick':win,f'{h}m_wins_wick_pct':pct(win,n),
                        f'{h}m_losses_wick':loss,f'{h}m_ambiguous_wick':ambig,
                        f'{h}m_wins_close_band':wc,f'{h}m_wins_close_band_pct':pct(wc,nc)})
        scan=g.loc[g.opposite_breakout_scan_coverage=='COMPLETE']
        nopp=int(scan.opposite_breakout_confirmed.eq(True).sum())
        row.update(opposite_scan_complete=len(scan),opposite_confirmed=nopp,
                   opposite_confirmed_pct=pct(nopp,len(scan)))
        opp_cov=g['opposite_30m_coverage'] if 'opposite_30m_coverage' in g else pd.Series(None,index=g.index)
        winsopp=g.loc[(g.opposite_breakout_confirmed==True)&(opp_cov=='COMPLETE')]
        opp_wins=(int(winsopp.opposite_30m_race_0_5.eq('FAVORABLE_FIRST').sum())
                  if 'opposite_30m_race_0_5' in winsopp else 0)
        row.update(opposite_30m_complete=len(winsopp),opposite_30m_wins_wick=opp_wins)
        out.append(row)
    return pd.DataFrame(out)


def context_group(row,tf):
    val=row[tf+'_context_status']
    if val=='ACTIVE':
        flag=row[tf+'_near_0_20_pct']
        if pd.isna(flag):return 'UNKNOWN_HISTORY'
        if isinstance(flag,str):flag=flag.strip().lower()=='true'
        return 'NEAR_ACTIVE_0.20' if bool(flag) else 'FAR_ACTIVE'
    return val


def _load_checked(root:Path,plan:dict,stage:str,require_all:bool):
    coverage=[];direct=[];context=[];used=set();tape_hashes={}
    for _,cutoff,symbol in cases(plan,stage):
        paths=case_paths(root,stage,cutoff,symbol)
        r={'stage':stage,'symbol':symbol,'pre_cutoff_utc':cutoff,'state':None,'error':''}
        try:
            if not paths['manifest'].exists():
                fail=paths['events'].parent/'_cohort_failure.json'
                r.update(state='FAILED' if fail.exists() else 'NOT_RUN',error=fail.read_text(encoding='utf-8')[-400:] if fail.exists() else '')
            else:
                manifest=verify_case(paths,symbol,cutoff,plan['source_sha256'])
                minute_key=f'reports/horizontal_level_full290_v301/minutes/{symbol}_1m.csv'
                minute_sha=manifest.get('source_sha256',{}).get(minute_key)
                if not minute_sha:raise ValueError('Manifest missing historical minute SHA')
                if symbol in tape_hashes and tape_hashes[symbol]!=minute_sha:
                    raise ValueError('Different minute tape versions across same-symbol windows')
                tape_hashes[symbol]=minute_sha
                try:e=pd.read_csv(paths['events'])
                except pd.errors.EmptyDataError:e=pd.DataFrame()
                try:c=pd.read_csv(paths['context'])
                except pd.errors.EmptyDataError:c=pd.DataFrame()
                if len(e) and not e.symbol.astype(str).eq(symbol).all():raise ValueError('Mismatched direct symbol')
                if len(c) and not c.symbol.astype(str).eq(symbol).all():raise ValueError('Mismatched context symbol')
                e['pre_cutoff_utc']=cutoff;c['pre_cutoff_utc']=cutoff
                if len(e):direct.append(e)
                if len(c):context.append(c)
                r.update(state='COMPLETE',direct_rows=len(e),context_rows=len(c))
                used.add((symbol,cutoff))
        except Exception as exc:r.update(state='CORRUPT_OR_MISMATCH',error=str(exc))
        coverage.append(r)
    cd=pd.DataFrame(coverage)
    if require_all and not cd.state.eq('COMPLETE').all():
        raise RuntimeError('Incomplete cohort; inspect coverage before full report: '+str(Counter(cd.state)))
    d=pd.concat(direct,ignore_index=True) if direct else pd.DataFrame()
    c=pd.concat(context,ignore_index=True) if context else pd.DataFrame()
    if len(d) and d[['symbol','pre_cutoff_utc','pivot_touch_id']].duplicated().any():
        raise ValueError('Duplicate same-timeframe pivot first touch')
    if len(c) and c[['symbol','pre_cutoff_utc','pivot_touch_id']].duplicated().any():
        raise ValueError('Duplicate 15m context first touch')
    return cd,d,c


def pool_stage(root:Path,plan:dict,stage:str,out:Path,require_all=False,allow_holdout=False):
    validate_plan(root,plan)
    if stage=='holdout' and not allow_holdout:
        raise PermissionError('Holdout remains sealed until hypotheses are frozen')
    cover,d,c=_load_checked(root,plan,stage,require_all)
    if not out.is_relative_to(root/'reports/reaction_multitf_cohort_v2'):
        raise ValueError('Pooled output must stay under reports/reaction_multitf_cohort_v2')
    paths={k:Path(str(out)+'_'+suffix) for k,suffix in dict(coverage='coverage.csv',
              direct='direct.csv',direct_summary='direct_summary.csv',htf='htf_events.csv',
              htf_summary='htf_summary.csv',htf_pair='htf_pair_summary.csv',
              manifest='manifest.json').items()}
    if any(p.exists() for p in paths.values()):raise FileExistsError('Refusing to overwrite previous pooled results')
    if len(d):
        direct_summary=_group(d,['swing_timeframe','original_side','original_status'],True)
    else:direct_summary=pd.DataFrame()
    if len(c):
        for tf in ('30m','1h'):
            c[tf+'_group']=c.apply(lambda r:context_group(r,tf),axis=1)
        htf_parts=[]
        for tf in ('30m','1h'):
            q=c.copy();q['htf_tf']=tf;q['htf_group']=q[tf+'_group']
            htf_parts.append(q)
        htf_long=pd.concat(htf_parts,ignore_index=True)
        htf_summary=_group(htf_long,['htf_tf','original_side','original_status','htf_group'],False)
        reactions=c.loc[c.original_status=='REACTION'].copy()
        htf_pair=_group(reactions,['original_side','30m_group','1h_group'],False) if len(reactions) else pd.DataFrame()
    else:htf_summary=pd.DataFrame();htf_pair=pd.DataFrame()
    out.parent.mkdir(parents=True,exist_ok=True)
    for name,frame in [('coverage',cover),('direct',d),('direct_summary',direct_summary),
                       ('htf',c),('htf_summary',htf_summary),('htf_pair',htf_pair)]:
        frame.to_csv(paths[name],index=False)
    metadata={'version':VERSION,'stage':stage,'created_utc':datetime.now(timezone.utc).isoformat(),
              'cohort_plan_sha256':sha256(Path(plan['_plan_path'])),
              'planned_cases':len(cover),'completed_cases':int(cover.state.eq('COMPLETE').sum()),
              'failed_cases':int(cover.state.eq('FAILED').sum()),
              'not_run_cases':int(cover.state.eq('NOT_RUN').sum()),
              'corrupt_cases':int(cover.state.eq('CORRUPT_OR_MISMATCH').sum()),
              'direct_rows':len(d),'htf15m_rows':len(c),
              'complete_cohort':bool(cover.state.eq('COMPLETE').all()),
              'note':'Research only. CIs for individual events are not independent; same symbol and market paths are correlated.',
              'output_sha256':{name:sha256(paths[name]) for name in paths if name!='manifest'}}
    paths['manifest'].write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding='utf-8')
    print('COHORT_V2_POOL '+json.dumps({k:metadata[k] for k in ('stage','planned_cases','completed_cases','failed_cases','not_run_cases','direct_rows','htf15m_rows','complete_cohort')},ensure_ascii=False))
    if len(direct_summary):print(direct_summary[['swing_timeframe','original_side','original_status','n','30m_complete','30m_wins_wick','opposite_confirmed']].to_string(index=False))
    if len(htf_summary):print(htf_summary[['htf_tf','original_side','original_status','htf_group','n','30m_complete','30m_wins_wick','opposite_confirmed']].to_string(index=False))
    print('OUTPUT',*[str(p) for p in paths.values()])
    return metadata


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('plan');p.add_argument('--root',required=True);p.add_argument('--out',required=True);p.add_argument('--max-symbols',type=int,default=30)
    p=sub.add_parser('run');p.add_argument('--root',required=True);p.add_argument('--plan',required=True);p.add_argument('--stage',choices=('discovery','holdout'),default='discovery');p.add_argument('--max-cases',type=int);p.add_argument('--timeout-seconds',type=int,default=900);p.add_argument('--allow-holdout',action='store_true')
    p=sub.add_parser('pool');p.add_argument('--root',required=True);p.add_argument('--plan',required=True);p.add_argument('--stage',choices=('discovery','holdout'),default='discovery');p.add_argument('--out',required=True);p.add_argument('--require-all',action='store_true');p.add_argument('--allow-holdout',action='store_true')
    a=parser.parse_args(argv)
    root=Path(a.root).resolve()
    if a.cmd=='plan':
        out=Path(a.out).resolve()
        if not out.is_relative_to(root/'reports/reaction_multitf_cohort_v2'):
            raise ValueError('Plan must stay under reports/reaction_multitf_cohort_v2')
        if out.exists():raise FileExistsError('Plan already exists; never overwrite locked cohort')
        plan=make_plan(root,a.max_symbols)
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(plan,indent=2,ensure_ascii=False),encoding='utf-8')
        print('COHORT_V2_PLAN '+json.dumps({'available_file_pairs':plan['universe_n_with_files'],'selected_symbols':len(plan['selected_symbols']), 'symbols':plan['selected_symbols'], 'discovery_cutoffs':plan['discovery_cutoffs_utc'],'holdout_cutoffs':plan['holdout_cutoffs_utc']},ensure_ascii=False))
        print('OUTPUT',out)
        return
    plan_path=Path(a.plan).resolve()
    plan=json.loads(plan_path.read_text(encoding='utf-8'))
    plan['_plan_path']=str(plan_path)
    if a.cmd=='run':run_stage(root,plan,a.stage,a.max_cases,a.allow_holdout,a.timeout_seconds)
    elif a.cmd=='pool':pool_stage(root,plan,a.stage,Path(a.out).resolve(),a.require_all,a.allow_holdout)

if __name__=='__main__':main()
