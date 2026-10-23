#!/usr/bin/env python3
"""Read-only outcome cross-tab for frozen causal persistent horizontal levels.

Never modifies the persistent-level detector, historical SQLite, or first-touch output.
This is descriptive historical research, not an independent holdout or a live strategy.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import pandas as pd

NEAR = 'PERSISTENT_LEVEL_NEAR_PRE'
FAR = 'NO_PERSISTENT_LEVEL_NEAR_PRE'
UNKNOWN = 'UNKNOWN_HISTORY'
STATES = ('TP_FIRST', 'SL_FIRST', 'NO_HIT', 'AMBIGUOUS', 'NOT_ENTERED')

def sha256_file(path: Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def wilson(tp, n, z=1.959963984540054):
    if not n:return None
    p=tp/n;z2=z*z;d=1+z2/n
    center=(p+z2/(2*n))/d
    radius=z*math.sqrt((p*(1-p)+z2/(4*n))/n)/d
    return 100*(center-radius),100*(center+radius)

def require_cols(df, names, source):
    missing=set(names)-set(df.columns)
    if missing:raise ValueError(f'{source}: missing columns {sorted(missing)}')

def summarize(rows, horizon):
    col=f'result_{horizon}m'
    n=len(rows)
    counts=rows[col].fillna('SIN_RESULTADO').astype(str).value_counts().to_dict()
    tp=int(counts.get('TP_FIRST',0));sl=int(counts.get('SL_FIRST',0));hit=tp+sl
    rate=round(tp/hit*100,2) if hit else None
    interval=wilson(tp,hit) if hit>=30 else None
    return dict(events=n,TP=tp,SL=sl,NO_HIT=int(counts.get('NO_HIT',0)),
                AMBIGUOUS=int(counts.get('AMBIGUOUS',0)),NOT_ENTERED=int(counts.get('NOT_ENTERED',0)),
                OTHER=n-sum(int(counts.get(k,0)) for k in STATES),resolved=hit,
                tp_rate_resolved=rate,
                tp_rate_all_scenarios=round(tp/n*100,2) if n else None,
                wilson95=(tuple(round(x,1) for x in interval) if interval else None),
                note='LOW_N_LT_30' if hit<30 else '')

def analyze(run_folder:Path, ft_path:Path):
    event_file=run_folder/'event_labels.csv'
    audit_file=run_folder/'audit.json'
    if not event_file.is_file() or not audit_file.is_file():
        raise FileNotFoundError('Missing persistent level event_labels.csv or audit.json; run --audit first')
    audit=json.loads(audit_file.read_text())
    if audit.get('success') is not True or audit.get('audit_rule')!='strict_response_close_before_retest_v1.1':
        raise ValueError('Persistent level audit is missing, failed or not strict PRE v1.1')
    expected_hash=audit.get('outputs_sha256',{}).get('event_labels.csv')
    if not expected_hash or sha256_file(event_file)!=expected_hash:
        raise ValueError('Persistent event_labels.csv does not match audited SHA256')
    events=pd.read_csv(event_file,low_memory=False,dtype={'event_key':str,'symbol':str,'side':str})
    require_cols(events,['event_key','symbol','side','observation_type','group_pre','date_utc'],str(event_file))
    if len(events)!=audit['events'] or events.event_key.isna().any() or events.event_key.duplicated().any():
        raise ValueError('Persistent events missing or duplicated')
    if not events.observation_type.eq('LIVE_OBSERVED').all():
        raise ValueError('Expected all-live: found non-LIVE_OBSERVED events')
    if not events.group_pre.isin([NEAR,FAR,UNKNOWN]).all():
        raise ValueError('Unknown PRE group label')
    if not ft_path.is_file():raise FileNotFoundError(str(ft_path))
    # First-touch contains multiple scenario/entry rows per event. Filter BEFORE joining.
    header=pd.read_csv(ft_path,nrows=0)
    require_cols(header,['event_key','symbol','side','observation_type','entry_mode','tp_pct','sl_pct','result_180m','result_360m'],str(ft_path))
    ft=pd.read_csv(ft_path,low_memory=False,
                   usecols=['event_key','symbol','side','observation_type','entry_mode','tp_pct','sl_pct','result_180m','result_360m'])
    filtered=ft[(ft.observation_type=='LIVE_OBSERVED') & (ft.entry_mode=='OBSERVED_OPEN') &
                (pd.to_numeric(ft.tp_pct,errors='coerce').sub(1).abs()<1e-9) &
                (pd.to_numeric(ft.sl_pct,errors='coerce').sub(1).abs()<1e-9)].copy()
    if filtered.event_key.isna().any() or filtered.event_key.duplicated().any():
        raise ValueError('Duplicate or missing First Touch keys for 1%/1% OBSERVED_OPEN')
    merged=events.merge(filtered,on='event_key',how='left',validate='one_to_one',indicator=True,suffixes=('_pre','_ft'))
    match=merged['_merge']=='both'
    mismatched=merged.loc[match & ((merged.symbol_pre!=merged.symbol_ft) | (merged.side_pre!=merged.side_ft))]
    if len(mismatched):raise ValueError(f'{len(mismatched)} event keys have symbol/side mismatches')
    eligible=merged[merged.group_pre.isin([NEAR,FAR])].copy()
    known=eligible[eligible._merge=='both'].copy()
    result={'pre_audit':{'version':audit.get('version'),'audit_rule':audit.get('audit_rule'),
                         'events':len(events),'group_counts':events.group_pre.value_counts().to_dict()},
            'join':{'first_touch_rows_total':len(ft),'first_touch_matching_scenario_rows':len(filtered),
                    'pre_events_with_scenario':int(match.sum()),
                    'pre_events_without_scenario':int((~match).sum()),
                    'eligible_complete_history':len(eligible),
                    'eligible_with_scenario':len(known),
                    'eligible_without_scenario':len(eligible)-len(known),
                    'no_scenario_by_group':eligible[eligible._merge!='both'].group_pre.value_counts().to_dict(),
                    'unknown_history_events':int((events.group_pre==UNKNOWN).sum())},
            'comparison':{}}
    for h in (180,360):
        items=[]
        for group in (NEAR,FAR):
            x=known[known.group_pre==group]
            s=summarize(x,h);s.update(group=group,horizon_min=h)
            items.append(s)
        result['comparison'][str(h)]=items
    # Store direction/date splits only as diagnostics (do not use to tune thresholds).
    result['by_side']={str(h):[
        dict(group=g,side=side,**summarize(known[(known.group_pre==g)&(known.side_pre==side)],h))
        for g in (NEAR,FAR) for side in ('LONG','SHORT')]
        for h in (180,360)}
    result['by_day']={str(h):[
        dict(date_utc=str(day),group=g,**summarize(known[(known.date_utc.astype(str)==str(day))&
            (known.group_pre==g)],h))
        for day in sorted(known.date_utc.dropna().unique()) for g in (NEAR,FAR)]
        for h in (180,360)}
    return result

def print_summary(r,show_dates=False):
    print('\n=== AUDIT PERSISTENT PRE / FIRST TOUCH JOIN ===')
    print('PRE label totals:',r['pre_audit']['group_counts'])
    print('JOIN:',json.dumps(r['join'],ensure_ascii=False))
    for h, rows in r['comparison'].items():
        print(f'\n=== TP +1% / SL -1% / {h} min / OBSERVED_OPEN ===')
        for z in rows:print(json.dumps(z,ensure_ascii=False))
        print('BY_SIDE:')
        for z in r['by_side'][h]:print(json.dumps(z,ensure_ascii=False))
        if show_dates:
            print('BY_DATE:')
            for z in r['by_day'][h]:print(json.dumps(z,ensure_ascii=False))
    print('\nWARNINGS: historical, non-independent observations; TP-first is not P&L.')
    print('Missing First Touch scenarios are NOT counted as SL or NO_HIT.')
    print('No placebo or prospective validation has been implemented in this report.')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--run-folder',type=Path)
    p.add_argument('--first-touch',type=Path)
    p.add_argument('--show-dates',action='store_true')
    a=p.parse_args()
    base=a.root.resolve()
    run=a.run_folder or base/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe'
    ft=a.first_touch or base/'reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv'
    print_summary(analyze(run,ft),a.show_dates)
if __name__=='__main__':main()
