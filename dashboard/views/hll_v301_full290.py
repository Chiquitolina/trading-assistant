#!/usr/bin/env python3
"""HLL V3.0.1: frozen full 290-symbol offline research, resumable by symbol.

Stages:
  --plan | --structure | --minutes | --mechanisms | --finalize-mechanisms |
  --entries | --finalize-entries | --report

NO live or private account code. Never overwrite historical analyses.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

PRE = Path('reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv')
CANDLES = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')
TOUCHES = Path('reports/horizontal_level_structure_lab_v26/runs/sep290_frozen_v26_roundtrip/touches.csv')
OUT = Path('reports/horizontal_level_full290_v301')
V291 = Path('reports/horizontal_level_structure_integrity_v291/runs/sep290_full_v301')
V29 = Path('reports/horizontal_level_continuation_v29/runs/sep290_full_v301')
MECH = Path('reports/horizontal_level_mechanisms_v292/runs/sep290_full_v301')
START = 1787184000000  # 20 Aug 2026 00 UTC
END = 1791244800000    # 06 Oct 2026 00 UTC; preserves 48h after Oct 03.
MINUTE = 60000


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def source_state():
    required=[PRE,TOUCHES]
    missing=[str(x) for x in required if not x.is_file()]
    if missing:raise FileNotFoundError('Missing source: '+', '.join(missing))
    cohort=pd.read_csv(PRE,usecols=['attempt_id','symbol','level_id','breakout_closed_ts'])
    if cohort.attempt_id.isna().any() or cohort.attempt_id.duplicated().any():raise ValueError('PRE duplicate IDs')
    syms=sorted(cohort.symbol.astype(str).unique())
    if len(cohort)!=55717 or len(syms)!=290:raise ValueError(f'Frozen source mismatch: {len(cohort)} attempts/{len(syms)} symbols')
    missing_30=[s for s in syms if not (CANDLES/f'{s}_30m.csv').is_file()]
    if missing_30:raise FileNotFoundError('Missing 30m candles '+str(missing_30[:5]))
    return syms,{'pre_sha256':digest(PRE),'touch_sha256':digest(TOUCHES),'n_attempts':len(cohort),'n_symbols':len(syms)}


def check_space(min_gb=4,min_memory_gb=1.5):
    free=shutil.disk_usage(Path.cwd()).free/(1024**3)
    if free<min_gb:raise RuntimeError(f'DISK_GUARD: only {free:.2f}GiB free; below {min_gb}GiB')
    try:
        info=Path('/proc/meminfo').read_text()
        avail=int(next(x.split()[1] for x in info.splitlines() if x.startswith('MemAvailable:')))*1024/(1024**3)
        if avail<min_memory_gb:raise RuntimeError(f'MEMORY_GUARD: only {avail:.2f}GiB available')
    except FileNotFoundError:pass
    return round(free,2)


def call_module(module,args):
    cmd=[sys.executable,'-u','-m',module]+list(map(str,args))
    print('HLL_V301_COMMAND',json.dumps(cmd),flush=True)
    subprocess.run(cmd,check=True)


def structure():
    if not (V291/'integrity_audit.csv').is_file():
        call_module('dashboard.views.hll_v291_structure_integrity',
                    ['--run','--cohort',PRE,'--candles-dir',CANDLES,'--output-dir',V291])
    if not (V29/'continuation_events.csv').is_file():
        call_module('dashboard.views.hll_v29_continuation_lab',
                    ['--run','--cohort',PRE,'--candles-dir',CANDLES,'--output-dir',V29])
    validate_stage_files()
    print('HLL_V301_STRUCTURE_DONE',json.dumps({'integrity':str(V291),'continuations':str(V29)}),flush=True)


def validate_stage_files():
    for src in (V291/'integrity_audit.csv',V29/'continuation_events.csv'):
        if not src.is_file():raise FileNotFoundError(src)
    v291m=json.loads((V291/'manifest.json').read_text())
    v29m=json.loads((V29/'manifest.json').read_text())
    if v291m.get('original_sha256')!=digest(PRE) or v29m.get('source_sha256')!=digest(PRE):
        raise ValueError('V291/V29 source hash mismatch; no mixing cohorts')
    if len(pd.read_csv(V291/'integrity_audit.csv',usecols=['attempt_id']))!=55717:raise ValueError('V291 cardinality')
    if len(pd.read_csv(V29/'continuation_events.csv',usecols=['attempt_id']))!=111434:raise ValueError('V29 cardinality')


def minute_location(symbol):return OUT/'minutes'/f'{symbol}_1m.csv'
def minute_manifest(symbol):return OUT/'minutes_manifest'/f'{symbol}.json'


def minute_status(symbol,source):
    path=minute_location(symbol);met=minute_manifest(symbol)
    if not path.is_file() or not met.is_file():return 'MISSING'
    try:
        m=json.loads(met.read_text())
        if m.get('source_sha256')!=source['pre_sha256'] or m.get('start_ts')!=START or m.get('end_ts')!=END:
            return 'WRONG_SOURCE'
        if m.get('sha256')!=digest(path):return 'HASH_MISMATCH'
        if m.get('bars')!=(END-START)//MINUTE:return 'BAD_LENGTH'
        # Complete range validated at first write; hash check on resume.
    except (ValueError,KeyError,OSError):return 'BAD_METADATA'
    return 'FULL'


def fetch_symbol(symbol,source,sleep_s=.25):
    """Atomic fetch, strict 1m continuity, no blind forward-fill, no private API."""
    from dashboard.views.hll_v300_clean_flip_290 import fetch_page, validate_row, FIELDS
    old=minute_status(symbol,source)
    if old=='FULL':return {'symbol':symbol,'status':'FULL_REUSED'}
    if old!='MISSING':raise RuntimeError(f'Existing minute source inconsistent for {symbol}: {old}; inspect manually')
    path=minute_location(symbol); path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=f'.{symbol}.',suffix='.part',dir=str(path.parent))
    pages=0;bars=0
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            writer=csv.writer(f);writer.writerow(FIELDS)
            t=START
            while t<END:
                packet=fetch_page(symbol,t,END,sleep_s=sleep_s)
                if not packet:raise ValueError(f'Empty 1m source for {symbol} at {t}')
                pages+=1
                for k in packet:
                    if len(k)<8:raise ValueError('Short public kline payload')
                    r=dict(zip(FIELDS,(int(k[0]),k[1],k[2],k[3],k[4],k[5],k[7])))
                    writer.writerow(validate_row(r,expected_ts=t))
                    t+=MINUTE;bars+=1
                    if t>END:raise ValueError('Beyond end')
        if bars!=(END-START)//MINUTE:raise ValueError('Incomplete public minute range')
        # O_EXCL: never overwrite an old archive.
        if path.exists():raise FileExistsError(path)
        os.rename(temp,path)
        meta={'symbol':symbol,'sha256':digest(path),'source_sha256':source['pre_sha256'],
              'start_ts':START,'end_ts':END,'bars':bars,'pages':pages,'source':'BINANCE_FAPI_PUBLIC'}
        mp=minute_manifest(symbol);mp.parent.mkdir(parents=True,exist_ok=True)
        with mp.open('x',encoding='utf-8') as f:json.dump(meta,f,ensure_ascii=False,indent=2)
        return {'symbol':symbol,'status':'FULL','bars':bars,'pages':pages}
    finally:
        if os.path.exists(temp):os.unlink(temp)


def batch_symbols(symbols,start,size):
    if start<0 or size<1 or start>=len(symbols):raise ValueError('Batch outside 290 universe')
    return symbols[start:start+size]


def assert_minute_ready(symbols,source):
    counts=Counter(minute_status(s,source) for s in symbols)
    if counts!={'FULL':len(symbols)}:
        raise RuntimeError(f'Incomplete 1m: {dict(counts)}; finish downloading before evaluating')
    return counts


def dataset_batch_folder(kind,start,group,scope=None):
    name=f'{start:03d}_{start+len(group):03d}'
    return OUT/f'{kind}_{scope}'/name if scope else OUT/kind/name


def reusable_batch(folder,group,source,scope=None):
    if not folder.exists():return False
    m=folder/'metadata.json'
    if not m.is_file():raise ValueError(f'Incomplete batch folder: {folder}')
    meta=json.loads(m.read_text())
    if meta.get('status')!='COMPLETE' or meta.get('symbols')!=group or meta.get('source')!=source:
        raise ValueError(f'Existing batch belongs to a different source or symbol set: {folder}')
    if scope is not None and meta.get('scope')!=scope:raise ValueError('Batch scope mismatch')
    names=['mechanism_attempts.csv','pre_contacts.csv','flip_survival_1m.csv'] if scope is None else [
           'original_attempts.csv','attempt_route_coverage.csv','entry_events.csv','entry_scenarios_25.csv']
    if any(not (folder/n).is_file() for n in names):raise ValueError(f'Missing batch output: {folder}')
    print('HLL_V301_BATCH_REUSED',json.dumps({'folder':str(folder),'symbols':len(group),'scope':scope}),flush=True)
    return True


def protected_write_batch(dest,parts,metadata):
    if dest.exists():
        raise FileExistsError(f'Batch exists, never overwrite: {dest}')
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(dir=dest.parent,prefix='.hll-v301-'))
    try:
        for name,df in parts.items():df.to_csv(tmp/name,index=False)
        (tmp/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.rename(dest)
    finally:
        if tmp.exists():shutil.rmtree(tmp)


def mechanism_batch(group,start,source):
    validate_stage_files();assert_minute_ready(group,source)
    dest=dataset_batch_folder('mechanisms_batches',start,group)
    if reusable_batch(dest,group,source):return
    from dashboard.views.hll_v292_mechanism_audit import load_inputs,run_audit
    cohort,touches=load_inputs(PRE,V291,TOUCHES)
    c=cohort[cohort.symbol.isin(group)].copy()
    t=touches[touches.symbol.isin(group)].copy()
    attempt,contact,surv=run_audit(c,t,CANDLES,OUT/'minutes')
    if len(attempt)!=len(c):raise ValueError('Mechanism source length mismatch')
    if attempt.attempt_id.duplicated().any():raise ValueError('Repeated mechanism attempt_id')
    dest=dataset_batch_folder('mechanisms_batches',start,group)
    protected_write_batch(dest,{'mechanism_attempts.csv':attempt,'pre_contacts.csv':contact,
                                'flip_survival_1m.csv':surv},
              {'symbols':group,'source':source,'integrity_hash':digest(V291/'integrity_audit.csv'),
               'n_attempts':len(attempt),'n_contacts':len(contact),'n_survival':len(surv),'status':'COMPLETE'})
    print('HLL_V301_MECHANISM_BATCH_DONE',json.dumps({'folder':str(dest),'symbols':len(group),'attempts':len(attempt),
                                                       'touches':len(contact),'survival_rows':len(surv)},ensure_ascii=False),flush=True)


def collect_batches(parent,expected,source,kind):
    if not parent.is_dir():raise FileNotFoundError(parent)
    ordered=[];covered=set()
    for p in sorted(x for x in parent.iterdir() if x.is_dir()):
        met=json.loads((p/'metadata.json').read_text())
        if met['source']!=source or met['status']!='COMPLETE':raise ValueError(f'Stale batch {p}')
        group=set(met['symbols'])
        if not group or covered.intersection(group):raise ValueError(f'Overlapping/empty symbol batch: {p}')
        covered.update(group);ordered.append(p)
    if covered!=set(expected):raise ValueError(f'{kind} incomplete: {len(covered)}/{len(expected)} symbols, missing {sorted(set(expected)-covered)[:6]}')
    return ordered


def combine_csvs(inputs,output):
    """Memory bounded concat preserving CSV headers."""
    with Path(output).open('w',encoding='utf-8',newline='') as target:
        header=None
        for p in inputs:
            with p.open('r',encoding='utf-8',newline='') as source:
                current=source.readline()
                if not current or not current.strip():
                    continue  # A symbol batch can have zero verified route entries.
                if header is None:header=current;target.write(current)
                elif current!=header:raise ValueError('Batch schema conflict: '+str(p))
                shutil.copyfileobj(source,target)


def finalize_mechanisms(symbols,source):
    validate_stage_files()
    batches=collect_batches(OUT/'mechanisms_batches',symbols,source,'mechanism')
    expected_hash=digest(V291/'integrity_audit.csv')
    if any(json.loads((b/'metadata.json').read_text()).get('integrity_hash')!=expected_hash for b in batches):
        raise ValueError('V291 integrity changed across mechanism batches')
    if MECH.exists():raise FileExistsError(MECH)
    from dashboard.views.hll_v292_mechanism_audit import (
        summarize,monthly_summary,survival_summary,blind_sample,matched_controls)
    MECH.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(dir=MECH.parent,prefix='.hll-v301-mech-'))
    try:
        for name in ('mechanism_attempts.csv','pre_contacts.csv','flip_survival_1m.csv'):
            combine_csvs([x/name for x in batches],tmp/name)
        a=pd.read_csv(tmp/'mechanism_attempts.csv',low_memory=False)
        p=pd.read_csv(tmp/'flip_survival_1m.csv',low_memory=False)
        if len(a)!=source['n_attempts'] or a.attempt_id.duplicated().any():raise ValueError('Mechanism duplicate/missing attempt')
        if p.duplicated(['attempt_id','horizon_min']).any():raise ValueError('Survival duplicates')
        summarize(a).to_csv(tmp/'mechanism_summary.csv',index=False)
        monthly_summary(a).to_csv(tmp/'mechanism_monthly_48.csv',index=False)
        survival_summary(a,p).to_csv(tmp/'survival_by_mechanism_48.csv',index=False)
        blind_sample(a).to_csv(tmp/'blind_review_sample.csv',index=False)
        matched,unmatched=matched_controls(a)
        matched.to_csv(tmp/'matched_controls_post.csv',index=False)
        unmatched.to_csv(tmp/'unmatched_cases.csv',index=False)
        m={'version':'HLL_V301_FULL290_MECHANISMS','source_prehash':digest(PRE),
           'source_v291hash':digest(V291/'integrity_audit.csv'),'source_touches_hash':digest(TOUCHES),
           'symbols':len(symbols),'attempts':len(a),'touches':sum(int(x['n_contacts']) for x in [json.loads((b/'metadata.json').read_text()) for b in batches]),
           'survival_coverage':{str(k):int(v) for k,v in p.path_state.value_counts().items()},
           'limitations':'Descriptive only; 1m OHLC first touch theoretical; no trading.'}
        (tmp/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.rename(MECH)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    print('HLL_V301_MECHANISMS_DONE',json.dumps({'attempts':len(a),'survival_rows':len(p),'output':str(MECH)},ensure_ascii=False),flush=True)


def entry_batch(group,start,source,scope):
    validate_stage_files();assert_minute_ready(group,source)
    dest=dataset_batch_folder('entries_batches',start,group,scope)
    if reusable_batch(dest,group,source,scope):return
    if not (MECH/'mechanism_attempts.csv').is_file():raise FileNotFoundError('Finish --finalize-mechanisms first')
    from dashboard.views.hll_v296_entry_audit import build_entries,detail_for_entries,status_table
    from dashboard.views.hll_v295_net_matrix import first_attempt_ids
    pre=pd.read_csv(PRE,low_memory=False)
    mech=pd.read_csv(MECH/'mechanism_attempts.csv',low_memory=False)
    v29=pd.read_csv(V29/'continuation_events.csv',low_memory=False)
    if scope=='FIRST_PER_LEVEL':pre=pre[pre.attempt_id.isin(first_attempt_ids(pre))]
    pre=pre[pre.symbol.isin(group)].copy()
    mech=mech[mech.symbol.isin(group)].copy()
    v29=v29[v29.origin_symbol.isin(group)].copy()
    selected,coverage,entries=build_entries(pre,mech,v29,CANDLES,OUT/'minutes',scope='ALL_ATTEMPTS')
    # Signals remain verified against frozen 30m sources. For simulation only,
    # reconcile exact-timestamp execution OPEN to the observed 1m archive and
    # retain every source mismatch in a separate audit artifact.
    from dashboard.views.hll_v301_entry_open_reconcile import reconcile_verified_opens
    entries,open_audit=reconcile_verified_opens(entries,OUT/'minutes')
    if entries.empty:
        detail=pd.DataFrame()
    else:
        detail=detail_for_entries(entries,OUT/'minutes',[120,720,2880],.10,0.0)
        extra=[c for c in ['attempt_id','pre_pattern_24','pre_pattern_96'] if c in selected]
        if len(extra)>1:detail=detail.merge(selected[extra],on='attempt_id',how='left',validate='many_to_one')
        original_pre=Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
        if not original_pre.is_file():raise FileNotFoundError('Missing original 15 cohort for group comparison')
        orig=set(pd.read_csv(original_pre,usecols=['symbol']).symbol.astype(str))
        if len(orig)!=15:raise ValueError('Original cohort is not 15 symbols')
        entries['universe_group']=entries.symbol.map(lambda x:'ORIGINAL_15' if x in orig else 'ADDITIONAL_275')
        detail['universe_group']=detail.symbol.map(lambda x:'ORIGINAL_15' if x in orig else 'ADDITIONAL_275')
    if len(detail)!=len(entries)*75 or (not detail.empty and detail.duplicated(['entry_id','horizon_min','tp_pct','sl_pct']).any()):
        raise ValueError('Entry scenario cardinality or IDs invalid')
    folder=dataset_batch_folder('entries_batches',start,group,scope)
    protected_write_batch(folder,{'original_attempts.csv':selected,'attempt_route_coverage.csv':coverage,
                                  'entry_events.csv':entries,'entry_scenarios_25.csv':detail,
                                  'entry_open_discrepancies.csv':open_audit},
                          {'symbols':group,'scope':scope,'source':source,'n_attempts':len(selected),'n_entries':len(entries),
                           'n_scenarios':len(detail),'v29_hash':digest(V29/'continuation_events.csv'),
                           'mechanism_hash':digest(MECH/'mechanism_attempts.csv'),
                           'entry_open_policy':'30m_signal_exact_1m_execution',
                           'n_open_discrepancies':len(open_audit),'status':'COMPLETE'})
    print('HLL_V301_ENTRIES_BATCH_DONE',json.dumps({'folder':str(folder),'scope':scope,'attempts':len(selected),
        'entries':len(entries),'scenarios':len(detail),'open_discrepancies':len(open_audit)},ensure_ascii=False),flush=True)


def finalize_entries(symbols,source,scope):
    parent=OUT/f'entries_batches_{scope}'
    batches=collect_batches(parent,symbols,source,'entries')
    v29hash=digest(V29/'continuation_events.csv');mechhash=digest(MECH/'mechanism_attempts.csv')
    if any((lambda m:m.get('v29_hash')!=v29hash or m.get('mechanism_hash')!=mechhash)(json.loads((b/'metadata.json').read_text())) for b in batches):
        raise ValueError('V29 or mechanisms changed across entry batches')
    if any((lambda m:m.get('n_scenarios')!=75*m.get('n_entries'))(json.loads((b/'metadata.json').read_text())) for b in batches):
        raise ValueError('Incomplete 25 x 3 scenarios in a batch')
    # Old completed batches were produced with a stricter equality assertion
    # and have no sidecar. This allows an exact resume without rewriting them.
    from dashboard.views.hll_v301_entry_open_reconcile import AUDIT_COLUMNS
    for b in batches:
        md=json.loads((b/'metadata.json').read_text())
        sidecar=b/'entry_open_discrepancies.csv'
        if sidecar.is_file():
            count=len(pd.read_csv(sidecar,usecols=['entry_id']))
            if count != md.get('n_open_discrepancies'):
                raise ValueError(f'Invalid OPEN source audit count: {b}')
        elif md.get('entry_open_policy')=='30m_signal_exact_1m_execution':
            raise ValueError(f'Missing OPEN source audit: {b}')
    dest=OUT/f'entries_{scope}'
    if dest.exists():raise FileExistsError(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(dir=dest.parent,prefix='.hll-v301-entries-'))
    try:
        for name in ['original_attempts.csv','attempt_route_coverage.csv','entry_events.csv','entry_scenarios_25.csv']:
            combine_csvs([b/name for b in batches],tmp/name)
        audit_paths=[b/'entry_open_discrepancies.csv' for b in batches
                     if (b/'entry_open_discrepancies.csv').is_file()]
        if audit_paths:
            combine_csvs(audit_paths,tmp/'entry_open_discrepancies.csv')
        else:
            pd.DataFrame(columns=AUDIT_COLUMNS).to_csv(tmp/'entry_open_discrepancies.csv',index=False)
        # Keep detail as a file, not millions of DataFrame rows in memory.
        acc=defaultdict(lambda:[0,0,0,0,0,0,0.0,0.0])
        dimensions=['route','pre_pattern','mechanism_pre_48','universe_group','horizon_min','tp_pct','sl_pct','entry_month']
        for chunk in pd.read_csv(tmp/'entry_scenarios_25.csv',chunksize=80_000,low_memory=False):
            for w in (24,48,96):
                frame=chunk.copy()
                frame['pre_pattern']=frame[f'pre_pattern_{w}']
                for keys,g in frame.groupby(dimensions,dropna=False,sort=False):
                    known=g[g.result.isin(['TP_FIRST','SL_FIRST','TIMEOUT'])]
                    k=(w,)+tuple(keys);s=acc[k]
                    s[0]+=len(g);s[1]+=len(known);s[2]+=int(g.result.eq('TP_FIRST').sum());s[3]+=int(g.result.eq('SL_FIRST').sum())
                    s[4]+=int(g.result.eq('TIMEOUT').sum());s[5]+=int(g.result.eq('AMBIGUOUS_1M').sum())
                    s[6]+=float(pd.to_numeric(known.net_pct,errors='raise').sum());s[7]+=int(len(g)-len(known)-int(g.result.eq('AMBIGUOUS_1M').sum()))
        rows=[]
        for k,v in acc.items():
            rows.append(dict(zip(['pre_window']+dimensions,k)) |
              {'entries':v[0],'known':v[1],'tp_first':v[2],'sl_first':v[3],'timeout':v[4],
               'ambiguous':v[5],'censored':int(v[7]),'mean_net_known_pct':v[6]/v[1] if v[1] else np.nan,
               'coverage_pct':100*v[1]/v[0] if v[0] else 0})
        pd.DataFrame(rows).to_csv(tmp/'matrix_by_route_pattern_month.csv',index=False)
        n_entries=sum(json.loads((b/'metadata.json').read_text())['n_entries'] for b in batches)
        n_scenarios=sum(json.loads((b/'metadata.json').read_text())['n_scenarios'] for b in batches)
        (tmp/'manifest.json').write_text(json.dumps({'version':'HLL_V301_FULL290_ENTRIES','scope':scope,'source':source,
         'symbols':len(symbols),'entries':n_entries,'scenario_rows':n_scenarios,
         'horizons_min':[120,720,2880],'commission_roundtrip_pct':.10,'slippage_pct':0,
         'entry_open_policy':'30m_signal_exact_1m_execution',
         'entry_open_discrepancies':sum(int(json.loads((b/'metadata.json').read_text()).get('n_open_discrepancies',0)) for b in batches),
         'entry_open_audit':'entry_open_discrepancies.csv',
         'limitations':['Threshold fills theoretical, no funding or order book costs',
                        'Multiple routes/levels dependent, no simultaneous portfolio',
                        'Historical cross-sectional replication, not prospective validation']},indent=2,ensure_ascii=False),encoding='utf-8')
        tmp.rename(dest)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    print('HLL_V301_ENTRIES_DONE',json.dumps({'scope':scope,'entries':n_entries,
          'scenario_rows':n_scenarios,'output':str(dest)},ensure_ascii=False),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    exclusive=p.add_mutually_exclusive_group(required=True)
    for action in ['plan','structure','minutes','mechanisms','finalize-mechanisms','entries','finalize-entries','report']:
        exclusive.add_argument('--'+action,action='store_true')
    p.add_argument('--batch-start',type=int,default=0)
    p.add_argument('--batch-size',type=int,default=10)
    p.add_argument('--scope',choices=['FIRST_PER_LEVEL','ALL'],default='FIRST_PER_LEVEL')
    p.add_argument('--sleep-s',type=float,default=.25)
    p.add_argument('--min-free-gb',type=float,default=4)
    a=p.parse_args(argv)
    if a.batch_size<1 or a.batch_size>290 or a.batch_start<0 or a.sleep_s<.1 or a.min_free_gb<2:
        p.error('Invalid batch, rate limit, or free-space guard')
    symbols,source=source_state()
    minutes=Counter(minute_status(s,source) for s in symbols)
    plan={'version':'HLL_V301_FULL290','symbols':len(symbols),'attempts':source['n_attempts'],
          'V291_ready':(V291/'integrity_audit.csv').is_file(),'V29_ready':(V29/'continuation_events.csv').is_file(),
          'V292_ready':(MECH/'mechanism_attempts.csv').is_file(),'minutes':dict(minutes),
          'entries_first_ready':(OUT/'entries_FIRST_PER_LEVEL'/'manifest.json').is_file(),
          'entries_all_ready':(OUT/'entries_ALL'/'manifest.json').is_file(),
          'output':str(OUT),'mode':'OFFLINE_NO_LIVE'}
    print('HLL_V301_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if a.plan or a.report:return 0
    if a.structure:
        check_space(a.min_free_gb);structure();return 0
    group=batch_symbols(symbols,a.batch_start,a.batch_size)
    if a.minutes:
        check_space(a.min_free_gb);counts=Counter()
        for i,s in enumerate(group,a.batch_start+1):
            check_space(a.min_free_gb)
            try:
                result=fetch_symbol(s,source,a.sleep_s)
                counts[result['status']]+=1
                print('HLL_V301_MINUTE_SYMBOL',json.dumps({'index':i,'of':len(symbols),**result},ensure_ascii=False),flush=True)
            except (RuntimeError,ValueError,OSError) as e:
                print('HLL_V301_MINUTE_ERROR',json.dumps({'symbol':s,'error':str(e)[:350]},ensure_ascii=False),flush=True)
                return 2
        print('HLL_V301_MINUTES_BATCH_DONE',json.dumps(dict(counts)|{'start':a.batch_start,'total':len(group)}),flush=True)
    elif a.mechanisms:check_space(a.min_free_gb);mechanism_batch(group,a.batch_start,source)
    elif a.finalize_mechanisms:check_space(a.min_free_gb);finalize_mechanisms(symbols,source)
    elif a.entries:check_space(a.min_free_gb);entry_batch(group,a.batch_start,source,a.scope)
    elif a.finalize_entries:check_space(a.min_free_gb);finalize_entries(symbols,source,a.scope)
    return 0

if __name__=='__main__':raise SystemExit(main())
