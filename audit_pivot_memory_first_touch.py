#!/usr/bin/env python3
"""Pivot Historical Memory PRE -> First Touch read-only research report.

Run only AFTER pivot memory --audit. Does not alter any PRE files, historical
caches, LIVE systems, or the First Touch source. No criteria are optimized.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path

import pandas as pd

VERSION = 'pivot-memory-first-touch-report-v1.0'
PIVOT_DIR = Path('reports/reaction_lab/pivot_historical_memory_pre_v1/all_live')
V1_DIR = Path('reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe')
FT_PATH = Path('reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv')
OUTPUT_DIR = 'outcome_report_v1'
GROUPS = ('PRIOR_RESISTANCE','PRIOR_SUPPORT','PRIOR_MIXED_SR','NO_PRIOR_LEVEL','UNKNOWN_HISTORY','UNKNOWN_PIVOT')
ELIGIBLE = ('PRIOR_RESISTANCE','PRIOR_SUPPORT','PRIOR_MIXED_SR','NO_PRIOR_LEVEL')
SIDES = ('LONG','SHORT')
STATUSES = ('TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS','NOT_ENTERED')
PRIMARY = {'LONG':'PRIOR_SUPPORT', 'SHORT':'PRIOR_RESISTANCE'}


def sha(path:Path) -> str:
    dig=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):dig.update(chunk)
    return dig.hexdigest()


def require_cols(df,cols,where):
    missing=set(cols)-set(df.columns)
    if missing:raise ValueError(f'{where}: missing columns {sorted(missing)}')


def wilson(tp,n):
    if not n:return None
    z=1.959963984540054; p=tp/n; z2=z*z; denom=1+z2/n
    center=(p+z2/(2*n))/denom
    radius=z*math.sqrt((p*(1-p)+z2/(4*n))/n)/denom
    return [round(100*(center-radius),2),round(100*(center+radius),2)]


def summary(x,horizon):
    col=f'result_{horizon}m'
    counts=Counter(x[col].fillna('MISSING').astype(str))
    n=len(x); tp=int(counts.get('TP_FIRST',0));sl=int(counts.get('SL_FIRST',0))
    resolved=tp+sl
    return dict(events=int(n),TP=tp,SL=sl,NO_HIT=int(counts.get('NO_HIT',0)),
                AMBIGUOUS=int(counts.get('AMBIGUOUS',0)),NOT_ENTERED=int(counts.get('NOT_ENTERED',0)),
                OTHER=int(n-sum(counts.get(k,0) for k in STATUSES)),resolved=resolved,
                tp_rate_resolved=round(100*tp/resolved,2) if resolved else None,
                tp_rate_all=round(100*tp/n,2) if n else None,
                wilson95=wilson(tp,resolved) if resolved>=30 else None,
                note='LOW_N_LT_30' if resolved<30 else '')


def load_verified(root:Path,ft_path:Path):
    pre_dir=root/PIVOT_DIR
    path=pre_dir/'pivot_memory_labels_pre.csv'
    apath=pre_dir/'audit.json'
    if not path.is_file() or not apath.is_file():
        raise FileNotFoundError('Missing PRE pivot labels/audit: run pivot memory --audit (all-live)')
    au=json.loads(apath.read_text(encoding='utf-8'))
    if not (au.get('success') is True and au.get('no_outcomes_used') is True and
            au.get('version')=='pivot-historical-memory-pre-v1.0' and au.get('events')==6423):
        raise ValueError('Pivot memory PRE audit not complete or unexpected cohort')
    if au.get('labels_sha')!=sha(path):raise ValueError('Pivot PRE labels SHA256 differs from audit')
    frozen=root/V1_DIR
    provenance=au.get('source_hashes',{})
    for key, file in (('v1_events_sha','event_labels.csv'),('v1_zones_sha','candidate_levels.csv')):
        if provenance.get(key)!=sha(frozen/file):
            raise ValueError(f'Frozen V1 {file} hash differs from pivot PRE provenance')
    if provenance.get('cohort_events')!=6423 or provenance.get('selected_events')!=6423:
        raise ValueError('Pivot report must cover all 6423 LIVE_OBSERVED')
    pre=pd.read_csv(path,low_memory=False,dtype={'event_key':str,'symbol':str,'side':str})
    require_cols(pre,['event_key','symbol','side','date_utc','observation_type','memory_group_pre'],path)
    if len(pre)!=6423 or pre.event_key.isna().any() or pre.event_key.duplicated().any():
        raise ValueError('Missing or duplicated PRE event keys')
    if not pre.observation_type.eq('LIVE_OBSERVED').all() or not pre.side.isin(SIDES).all():
        raise ValueError('Non-LIVE_OBSERVED or invalid side in pivot PRE labels')
    if not pre.memory_group_pre.isin(GROUPS).all():raise ValueError('Unrecognized PRE memory group')
    expected={str(k):int(v) for k,v in au.get('groups',{}).items()}
    actual={str(k):int(v) for k,v in pre.memory_group_pre.value_counts().items()}
    if expected!=actual:raise ValueError('Pivot PRE group counts differ from audit')
    if not ft_path.is_file():raise FileNotFoundError(f'First Touch not found: {ft_path}')
    fields=['event_key','symbol','side','observation_type','entry_mode','tp_pct','sl_pct',
            'result_180m','result_360m']
    header=pd.read_csv(ft_path,nrows=0)
    require_cols(header,fields,ft_path)
    ft=pd.read_csv(ft_path,usecols=fields,dtype={'event_key':str},low_memory=False)
    ft=ft.loc[(ft.observation_type=='LIVE_OBSERVED') & (ft.entry_mode=='OBSERVED_OPEN') &
       (pd.to_numeric(ft.tp_pct,errors='coerce').sub(1.0).abs()<1e-9) &
       (pd.to_numeric(ft.sl_pct,errors='coerce').sub(1.0).abs()<1e-9)].copy()
    if ft.event_key.isna().any() or ft.event_key.duplicated().any():
        raise ValueError('Duplicate/empty First Touch scenario event key')
    for col in ('result_180m','result_360m'):
        actual=set(ft[col].dropna().astype(str))-set(STATUSES)
        if actual:raise ValueError(f'Unexpected First Touch {col} values: {sorted(actual)}')
    m=pre.merge(ft,on='event_key',how='left',indicator=True,
                validate='one_to_one',suffixes=('_pre','_ft'))
    matched=m['_merge'].eq('both')
    if ((m.loc[matched,'symbol_pre']!=m.loc[matched,'symbol_ft']) |
        (m.loc[matched,'side_pre']!=m.loc[matched,'side_ft'])).any():
        raise ValueError('First Touch symbol/side mismatch for matching event_key')
    return au,m,ft,sha(ft_path)


def analyze(root:Path,ft_path:Path):
    au,m,ft,ft_sha=load_verified(root,ft_path)
    matched=m['_merge'].eq('both')
    known=m[matched].copy()
    pre_counts={k:int(v) for k,v in m.memory_group_pre.value_counts().items()}
    missing={k:int(v) for k,v in m.loc[~matched,'memory_group_pre'].value_counts().items()}
    result={'version':VERSION,'pre_labels_sha':au['labels_sha'],'first_touch_sha':ft_sha,
            'first_touch_filter':'LIVE_OBSERVED|OBSERVED_OPEN|TP1|SL1',
            'counts':{'pre_total':len(m),'first_touch_matching_scenario_rows':len(ft),
                      'scenario_matched':int(matched.sum()),'scenario_missing':int((~matched).sum()),
                      'eligible_pre':int(m.memory_group_pre.isin(ELIGIBLE).sum()),
                      'eligible_matched':int((matched & m.memory_group_pre.isin(ELIGIBLE)).sum()),
                      'unknown_pre':int((~m.memory_group_pre.isin(ELIGIBLE)).sum()),
                      'pre_groups':pre_counts,'missing_by_group':missing},
            'by_side':{}, 'primary_comparison':{}, 'by_day':{}}
    for h in (180,360):
        hs=str(h);result['by_side'][hs]=[];result['primary_comparison'][hs]={};result['by_day'][hs]=[]
        for side in SIDES:
            for group in ELIGIBLE:
                x=known[(known.side_pre==side)&(known.memory_group_pre==group)]
                result['by_side'][hs].append({'side':side,'group':group,**summary(x,h)})
            current=[r for r in result['by_side'][hs] if r['side']==side]
            focus=next(r for r in current if r['group']==PRIMARY[side])
            control=next(r for r in current if r['group']=='NO_PRIOR_LEVEL')
            delta=None if focus['tp_rate_resolved'] is None or control['tp_rate_resolved'] is None else round(
                focus['tp_rate_resolved']-control['tp_rate_resolved'],2)
            result['primary_comparison'][hs][side]={
                'hypothesis_group':PRIMARY[side],'control_group':'NO_PRIOR_LEVEL',
                'hypothesis':focus,'control':control,'delta_tp_first_pp':delta}
        for (date,side,group),x in known[known.memory_group_pre.isin(ELIGIBLE)].groupby(
                ['date_utc','side_pre','memory_group_pre'],dropna=False,sort=True):
            result['by_day'][hs].append({'date':str(date),'side':str(side),'group':str(group),
                                         **summary(x,h)})
    return result


def write_json(path:Path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.pivot_ft_',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,indent=2,ensure_ascii=False,sort_keys=True,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def print_summary(r):
    print('PIVOT_MEMORY_FIRST_TOUCH_JOIN',json.dumps(r['counts'],ensure_ascii=False))
    for h in ('180','360'):
        print(f'\n=== {h} min / TP 1% / SL 1% / OBSERVED_OPEN ===')
        for side in SIDES:
            print(f'-- {side} --')
            for row in r['by_side'][h]:
                if row['side']==side:print(json.dumps(row,ensure_ascii=False))
            print('PRIMARY_COMPARE',json.dumps(r['primary_comparison'][h][side],ensure_ascii=False))
    print('CAUTION: retrospective non-independent data; hypothesis comparisons exploratory; TP-first != profitability.')
    print('UNKNOWN_HISTORY / UNKNOWN_PIVOT and unmatched First Touch are excluded from controls.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--first-touch',type=Path,default=None)
    p.add_argument('--no-write',action='store_true',help='print only; do not write outcome report')
    a=p.parse_args()
    root=a.root.resolve()
    ft=a.first_touch or root/FT_PATH
    r=analyze(root,ft)
    print_summary(r)
    if not a.no_write:
        dest=root/PIVOT_DIR/OUTPUT_DIR/'pivot_memory_first_touch_report.json'
        write_json(dest,r)
        print('PIVOT_MEMORY_REPORT',dest)

if __name__=='__main__':main()
