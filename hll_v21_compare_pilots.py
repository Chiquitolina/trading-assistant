#!/usr/bin/env python3
"""Read-only comparison of frozen Phase A V2.0 and causal V2.1 pilot runs."""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd


def read(run,symbol,kind):
    path=run/'shards'/f'{symbol}.{kind}.csv'
    return pd.read_csv(path) if path.is_file() else pd.DataFrame()


def count_overlaps(form,ends):
    if form.empty:return (0,0)
    live=form.merge(ends[['level_id','end_closed_ts']],on='level_id',how='inner',validate='one_to_one')
    ids=set();pairs=0
    for _,group in live.groupby(['symbol','origin']):
        rows=group.to_dict('records')
        for idx,a in enumerate(rows):
            for b in rows[idx+1:]:
                if max(a['formation_closed_ts'],b['formation_closed_ts']) > min(a['end_closed_ts'],b['end_closed_ts']):
                    continue
                if max(a['zone_low'],b['zone_low']) <= min(a['zone_high'],b['zone_high']):
                    pairs+=1;ids.update((a['level_id'],b['level_id']))
    return pairs,len(ids)


def summary(run,symbols):
    forms=[];ends=[];events=[]
    for sym in symbols:
        for store,kind in [(forms,'formation'),(ends,'ends'),(events,'events')]:
            df=read(run,sym,kind)
            if not df.empty:store.append(df)
    f=pd.concat(forms,ignore_index=True) if forms else pd.DataFrame()
    en=pd.concat(ends,ignore_index=True) if ends else pd.DataFrame()
    ev=pd.concat(events,ignore_index=True) if events else pd.DataFrame()
    overlaps=count_overlaps(f,en)
    types=ev.event_type.value_counts().to_dict() if not ev.empty else {}
    return dict(levels=len(f),overlap_pairs=overlaps[0],overlap_levels=overlaps[1],
        crosses=types.get('CROSS_UP',0)+types.get('CROSS_DOWN',0),
        old_visits=types.get('VISIT',0),
        new_tests=types.get('TEST_FROM_ABOVE',0)+types.get('TEST_FROM_BELOW',0),
        reconfirmations=types.get('LEVEL_RECONFIRMED',0),
        defenses=types.get('DEFENSE_CONFIRMED',0),losses=types.get('LOSS_CONFIRMED',0),
        timeout=types.get('TEST_TIMEOUT',0))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--old-run',type=Path,required=True)
    p.add_argument('--new-run',type=Path,required=True)
    a=p.parse_args()
    old,new=a.old_run.resolve(),a.new_run.resolve()
    old_cov=pd.read_csv(old/'coverage.csv')
    new_cov=pd.read_csv(new/'coverage.csv')
    common=sorted(set(old_cov[old_cov.status=='COMPLETE'].symbol)&
                  set(new_cov[new_cov.status=='COMPLETE'].symbol))
    if not common:raise SystemExit('No common complete symbols')
    print('COMMON_COMPLETE_SYMBOLS',len(common),', '.join(common))
    r=pd.DataFrame([summary(old,common),summary(new,common)],index=['V2.0_ORIGINAL','V2.1_GEOMETRY'])
    print('\n=== SAME SYMBOLS · CAUSAL DETECTOR CHANGE ===')
    print(r.to_string())
    print('\nATTENTION: two runs may have different candle archives/time ranges; inspect source coverage before interpreting deltas.')
    print('TEST is an independently rearmed contact; VISIT used a weaker rule. Different meaning, not a performance improvement.')

if __name__=='__main__':main()
