#!/usr/bin/env python3
"""HBL V1.3: pre-registered SYMBOL holdout, unchanged V1.2 engine.

This orchestrator NEVER edits V1.2, LIVE, Redis, strategy files, or SQLite.
Download is delegated to the existing public-only native collector.
Run/audit is delegated to the exact pinned V1.2 detector.
Do not select symbols or thresholds after inspecting validation outcomes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = ROOT / 'reports/horizontal_breakout_lab_v1/frozen_v13/symbol_holdout_40_20261008_20261020'
ENGINE = ROOT / 'horizontal_breakout_lifecycle_v12.py'
CORE = ROOT / 'horizontal_breakout_lab.py'
COLLECTOR = ROOT / 'horizontal_breakout_native_candles_pilot_v1.py'
EXPECTED_ENGINE = 'cdea32676d0d0bc08ed060ba25d08b7e7b52df115cca9892948ff6464155d9c0'
EXPECTED_CORE = '9b772a362cc55c61c3e552ca313e910a36228cbdda1b5ee0dde7661684fb350f'
EXPECTED_COLLECTOR = '583d8896254c7c41d191e2fc8babf0dc1021e3df53d9e38f8664ce613bb07f7a'
VERSION = 'horizontal-breakout-frozen-validation-v1.3.0'
START = '2026-10-08'
END = '2026-10-20'
TAIL_HOURS = 48
BATCH_SIZE = 5
DISCOVERY = ('1000BONKUSDT','1000FLOKIUSDT','1000PEPEUSDT','1000XECUSDT','1INCHUSDT')
# Frozen BEFORE seeing validation outcomes; no replacements in a failed cohort.
COHORT = (
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'DOTUSDT',
    'LTCUSDT', 'BCHUSDT', 'ETCUSDT', 'UNIUSDT', 'AAVEUSDT',
    'ATOMUSDT', 'NEARUSDT', 'FILUSDT', 'TRXUSDT', 'RUNEUSDT',
    'SUIUSDT', 'INJUSDT', 'OPUSDT', 'ARBUSDT', 'SEIUSDT',
    'TIAUSDT', 'WIFUSDT', 'PEPEUSDT', '1000SHIBUSDT', 'GALAUSDT',
    'HBARUSDT', 'ICPUSDT', 'XLMUSDT', 'VETUSDT', 'LDOUSDT',
    'CRVUSDT', 'COMPUSDT', 'ZECUSDT', 'ENSUSDT', 'APTUSDT',
)
FROZEN_SETTINGS = {
    'windows': [12,24,36,48], 'min_touches': 2, 'tolerance_atr': 0.5,
    'margin_pct': 0.1, 'max_level_bars': 96, 'rearm_bars': 24,
    'invalidation_closes': 2, 'acceptance_closes': 2,
    'min_volume_ratio': 2.0, 'min_body_atr': 0.6,
    'min_close_position': 0.75, 'high_lookback': 12, 'post_bars': 8,
    'max_horizon_min': 2880, 'stop_pct': 1.5,
    'fee_roundtrip_pct': 0.1,
}
PRIMARY = {
    'universe': 'FIRST_ATTEMPT_PER_LEVEL',
    'case': 'STRONG_BREAKOUT + LEVEL_CROSS',
    'control': 'BREAKOUT_ATTEMPT + LEVEL_CROSS',
    'horizon_minutes': 360, 'take_profit_pct': 2.0,
    'stop_loss_pct': 1.5, 'roundtrip_fee_pct': 0.1,
    'entry': 'next available 1m open after observed 30m signal close',
    'primary_metrics': ['mean_theoretical_net_pct', 'TP_FIRST_per_all_attempts'],
}
KEY = ['attempt_id','level_id','symbol','signal_closed_ts']


def sha(p):
    with p.open('rb') as fh:
        h = hashlib.sha256()
        for block in iter(lambda: fh.read(1<<20), b''):
            h.update(block)
        return h.hexdigest()


def store(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    try:
        tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        tmp.replace(p)
    finally:
        if tmp.exists(): tmp.unlink()


def code_hashes():
    names = {'horizontal_breakout_lifecycle_v12.py':(ENGINE,EXPECTED_ENGINE),
             'horizontal_breakout_lab.py':(CORE,EXPECTED_CORE),
             'horizontal_breakout_native_candles_pilot_v1.py':(COLLECTOR,EXPECTED_COLLECTOR)}
    hashes={}
    for name,(p,expected) in names.items():
        if not p.is_file(): raise RuntimeError(f'MISSING_SOURCE_FILE: {p}')
        actual=sha(p)
        if actual != expected:
            raise RuntimeError(f'CODE_MISMATCH: {name}\n  Expected {expected}\n  Found    {actual}\n'
                               'Do NOT silently accept a different detector for validation.')
        hashes[name]=actual
    return hashes


def batches():
    return [COHORT[i:i+BATCH_SIZE] for i in range(0,len(COHORT),BATCH_SIZE)]


def batch_dir(index):
    return BASE / 'native' / f'batch_{index:02d}'


def run_tag(index):
    return f'frozen_v13_b{index:02d}'


def expected_manifest():
    return {'version':VERSION, 'holdout_type':'UNSEEN_SYMBOLS_SHARED_DATES',
            'note':'Same market dates as pilot; NOT independent temporal validation.',
            'discovery_symbols_excluded':list(DISCOVERY),
            'symbols':list(COHORT), 'batches':[list(b) for b in batches()],
            'start_utc_inclusive':START, 'end_utc_exclusive':END,
            'tail_hours':TAIL_HOURS, 'frozen_detector_settings':FROZEN_SETTINGS,
            'frozen_primary_comparison':PRIMARY, 'source_sha256':code_hashes()}


def freeze():
    if len(COHORT)!=40 or len(set(COHORT))!=len(COHORT) or set(COHORT)&set(DISCOVERY):
        raise RuntimeError('INVALID_PREDECLARED_COHORT')
    m=expected_manifest()
    path=BASE/'freeze_manifest.json'
    if path.exists():
        old=json.loads(path.read_text(encoding='utf-8'))
        if any(old.get(k)!=v for k,v in m.items()):
            raise RuntimeError('Existing freeze manifest differs. Will not overwrite.')
        print('FREEZE_ALREADY_EXISTS',path)
        return
    m['frozen_at_utc']=datetime.now(timezone.utc).isoformat()
    store(path,m)
    print('FROZEN',path,'cohort=40 batches=8 settings=V1.2 exact SHA256')


def locked():
    path=BASE/'freeze_manifest.json'
    if not path.is_file(): raise RuntimeError('First run --freeze; no downloads or analysis before freezing.')
    m=json.loads(path.read_text(encoding='utf-8'))
    for k,v in expected_manifest().items():
        if m.get(k)!=v: raise RuntimeError('FROZEN_MANIFEST_MISMATCH: '+k)
    return m


def call(pyfile,*args):
    cmd=[sys.executable,str(pyfile),*map(str,args)]
    print('EXEC', ' '.join(cmd),flush=True)
    subprocess.run(cmd,check=True,cwd=ROOT)


def collector_args(index, symbols):
    return ['--start',START,'--end',END,'--tail-hours',str(TAIL_HOURS),
            '--symbols',','.join(symbols),'--output',str(batch_dir(index))]


def seal_data(write=False):
    """Re-check every native file and compare to immutable SHA256 dataset seal."""
    hashes={}
    for idx,syms in enumerate(batches(),1):
        for symbol in syms:
            for tf in ('1m','30m'):
                f=batch_dir(idx)/f'{symbol}_{tf}.csv'
                meta=f.with_suffix('.meta.json')
                if not f.is_file() or not meta.is_file(): raise RuntimeError('MISSING_NATIVE_FILE: '+str(f))
                if json.loads(meta.read_text(encoding='utf-8')).get('sha256')!=sha(f):
                    raise RuntimeError('NATIVE_CHECKSUM_MISMATCH: '+str(f))
                hashes[str(f.relative_to(BASE))]=sha(f)
    p=BASE/'dataset_seal.json'
    if p.exists():
        if json.loads(p.read_text(encoding='utf-8'))!=hashes:
            raise RuntimeError('DATASET_CHANGED_AFTER_SEAL')
    elif write:
        store(p,hashes)
    else:
        raise RuntimeError('First execute --audit-data to seal the 80 verified candle files.')
    print('DATASET_VERIFIED',len(hashes),'native CSVs; sealed',p)
    return hashes


def batch_run_dir(index):
    # Same fingerprint function used by V1.2, without changing its logic.
    import horizontal_breakout_lifecycle_v12 as v12
    from argparse import Namespace
    a=Namespace(root=ROOT,candles_dir=batch_dir(index),max_symbols=0,
                symbols=','.join(batches()[index-1]),run_tag=run_tag(index))
    p,manifest=v12.fingerprint(a,v12.LifecycleConfig())
    if json.loads(json.dumps(manifest['settings']))!=FROZEN_SETTINGS:
        raise RuntimeError('V1.2 SETTINGS NO LONGER FROZEN')
    return p


def verify_audits():
    import horizontal_breakout_lifecycle_v12 as v12
    rows=[]
    for idx,syms in enumerate(batches(),1):
        out=batch_run_dir(idx)
        rep,_,_,_,_=v12.run_audit(out)
        if not rep['success'] or rep['expected_symbols']!=len(syms) or rep['completed_symbols']!=len(syms):
            raise RuntimeError(f'BATCH_{idx:02d}_AUDIT_FAILED: {rep}')
        source=json.loads((out/'source_snapshot.json').read_text(encoding='utf-8'))
        if len(source)!=len(syms)*2:raise RuntimeError(f'BAD_INPUT_SNAPSHOT_BATCH_{idx:02d}')
        rows.append((out,rep))
    return rows


def combine():
    import pandas as pd
    import numpy as np
    verified=verify_audits()
    joined={}
    for category in ('levels','attempts','outcomes'):
        frames=[]
        for out,rep in verified:
            src=out/(category+'.csv')
            if not src.is_file():raise RuntimeError('MISSING_AGGREGATED_CSV: '+str(src))
            try: frame=pd.read_csv(src)
            except pd.errors.EmptyDataError:frame=pd.DataFrame()
            if len(frame):frames.append(frame)
        joined[category]=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
    lv,att,oc=(joined[k] for k in ('levels','attempts','outcomes'))
    if att.empty or oc.empty: raise RuntimeError('NO_ATTEMPTS_OR_OUTCOMES; report is not interpretable')
    for col in KEY:
        if col not in att or col not in oc:raise RuntimeError('MISSING_JOIN_FIELD: '+col)
    if att.duplicated(KEY).any() or oc.duplicated(KEY).any():raise RuntimeError('DUPLICATE_JOIN_KEYS')
    d=att.merge(oc,on=KEY,how='outer',validate='one_to_one',indicator=True)
    if len(d)!=len(att) or len(d)!=len(oc) or (d['_merge']!='both').any():
        raise RuntimeError('ATTEMPT_OUTCOME_MISMATCH')
    d=d.drop(columns=['_merge'])
    if not set(d.symbol).issubset(COHORT) or set(d.symbol)&set(DISCOVERY):
        raise RuntimeError('CONTAMINATION_OF_HOLDOUT')
    d=d.sort_values(['level_id','signal_closed_ts','attempt_id']).copy()
    d['first_on_level']=~d.duplicated('level_id')
    d['validation_group']=np.select([
        (d.strength_class=='STRONG_BREAKOUT')&(d.trigger_kind=='LEVEL_CROSS'),
        (d.strength_class=='STRONG_BREAKOUT')&(d.trigger_kind=='EXPANSION_CONTINUATION'),
        (d.strength_class=='BREAKOUT_ATTEMPT')&(d.trigger_kind=='LEVEL_CROSS')],
        ['DIRECT_STRONG','CONTINUATION_STRONG','WEAK_CROSS'],default='OTHER')
    d['signal_day_utc']=pd.to_datetime(d.signal_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')
    return lv,d


def wilson(successes, total):
    if total==0:return (None,None)
    z=1.959963984540054
    p=successes/total
    denom=1+z*z/total
    center=(p+z*z/(2*total))/denom
    half=z*math.sqrt(p*(1-p)/total+z*z/(4*total**2))/denom
    return (round(100*(center-half),2),round(100*(center+half),2))


def analyze_group(frame,label):
    label_col='tp2_h360'; net_col='net_tp2_h360_pct'
    n=len(frame)
    hits=frame[label_col].value_counts().to_dict() if n else {}
    outcome={key:int(hits.get(key,0)) for key in ('TP_FIRST','SL_FIRST','NO_HIT','CENSORED','BOTH_SAME_1M')}
    net=frame[net_col].dropna()
    return {'group':label,'N':n,'unique_levels':int(frame.level_id.nunique()),
            'symbols_with_signal':int(frame.symbol.nunique()),
            **outcome,'tp_rate_all_pct':round(100*outcome['TP_FIRST']/n,2) if n else None,
            'tp_rate_wilson95_pct':wilson(outcome['TP_FIRST'],n),
            'mean_theoretical_net_pct':round(float(net.mean()),4) if len(net) else None,
            'net_count':int(len(net)), 'positive_symbols':int((frame.groupby('symbol')[net_col].mean()>0).sum()) if len(net) else 0}


def report():
    import pandas as pd
    seal_data(write=False)
    levels,d=combine()
    first=d[d.first_on_level].copy()
    # Primary comparison completely frozen before reading the held-out data.
    primary={name:analyze_group(first[first.validation_group==name],name)
             for name in ('DIRECT_STRONG','WEAK_CROSS')}
    secondary={name:analyze_group(d[d.validation_group==name],name)
               for name in ('DIRECT_STRONG','CONTINUATION_STRONG','WEAK_CROSS','OTHER')}
    per_symbol=[]
    for (sym,group),g in first.groupby(['symbol','validation_group']):
        per_symbol.append(analyze_group(g,group)|{'symbol':sym})
    result={'version':VERSION,'audit_success':True, 'study_type':'SYMBOL_HOLDOUT_ONLY',
            'shared_dates_with_discovery':True,'frozen_primary':PRIMARY,
            'expected_symbols':len(COHORT),'symbols_with_attempts':int(d.symbol.nunique()),
            'total_levels':len(levels),'total_attempts':len(d),'first_attempts':len(first),
            'primary_first_attempt_by_level':primary,
            'secondary_all_attempts':secondary,
            'per_symbol_first_attempt':per_symbol,
            'limits':['not temporal OOS','same macro regime as discovery','cross-symbol dependency',
                      'independent theoretical trades; simultaneous slots and slippage omitted',
                      'Wilson interval describes sampled hit rate, not strategy profitability']}
    store(BASE/'validation_report.json',result)
    d.to_csv(BASE/'validation_attempts_with_outcomes.csv',index=False)
    if per_symbol:pd.DataFrame(per_symbol).to_csv(BASE/'validation_by_symbol.csv',index=False)
    print('\nHBL_V13_FROZEN_VALIDATION',json.dumps({k:v for k,v in result.items() if k not in ('per_symbol_first_attempt',)},indent=2,ensure_ascii=False))
    print('OUTPUT',BASE/'validation_report.json')
    return result


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    exclusive=ap.add_mutually_exclusive_group(required=True)
    for option in ('freeze','plan','fetch','audit-data','run','audit'):
        exclusive.add_argument('--'+option,action='store_true')
    a=ap.parse_args(argv)
    if a.freeze:
        freeze();return 0
    locked()
    if a.plan:
        print(json.dumps({'mode':'PLAN','type':'SYMBOL_HOLDOUT_SHARED_DATES',
                          'symbols':len(COHORT),'batches':len(batches()),
                          'dates_utc':[START,END],'post_tail_hours':TAIL_HOURS,
                          'principal':PRIMARY,'destination':str(BASE)},indent=2,ensure_ascii=False))
        for i,syms in enumerate(batches(),1):
            print(f'BATCH {i:02d}: {", ".join(syms)}')
        print('DATA_VOLUME: 40 symbols x 20,160 1m bars + 576 30m bars; ~880 public API calls.')
        return 0
    if a.fetch:
        for i,syms in enumerate(batches(),1):
            call(COLLECTOR,'--run',*collector_args(i,syms))
        print('FETCH_COMPLETE: next use --audit-data')
        return 0
    if a.audit_data:
        for i,syms in enumerate(batches(),1):
            call(COLLECTOR,'--audit',*collector_args(i,syms))
        seal_data(write=True)
        return 0
    seal_data(write=False)
    if a.run:
        for i,syms in enumerate(batches(),1):
            run=['--candles-dir',str(batch_dir(i)), '--symbols',','.join(syms),
                 '--run-tag',run_tag(i)]
            call(ENGINE,'--run',*run)
            call(ENGINE,'--audit',*run)
        print('V13_RUN_COMPLETE: now execute --audit')
        return 0
    if a.audit:
        report()
        return 0
    raise AssertionError('unknown mode')


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError,subprocess.CalledProcessError) as exc:
        print('HBL_V13_ERROR:',str(exc),file=sys.stderr)
        raise SystemExit(2)
