#!/usr/bin/env python3
"""HLL V301 read-only diagnostic for disagreements between 30m and 1m entry OPEN.

Does not modify snapshots, batches, entry detectors, or LIVE.
Use: python3 -m dashboard.views.hll_v301_open_mismatch_diagnostic --batch-start 60 --batch-size 5
"""
from __future__ import annotations
import argparse
import json
from collections import Counter

import numpy as np
import pandas as pd

from dashboard.views.hll_v301_full290 import PRE, MECH, V29, CANDLES, OUT, source_state
from dashboard.views.hll_v295_net_matrix import first_attempt_ids
from dashboard.views.hll_v296_entry_audit import build_entries


def mismatch(open_1m, verified_price):
    if not np.isfinite(open_1m) or not np.isfinite(verified_price):
        return False
    return abs(open_1m-verified_price) > max(1e-10,abs(verified_price)*1e-7)


def load_subset_csv(path, symbols, column, wanted=None):
    header=pd.read_csv(path,nrows=0).columns
    cols=[c for c in (wanted or header) if c in header]
    data=pd.read_csv(path,usecols=cols,low_memory=False)
    return data[data[column].astype(str).isin(symbols)].copy()


def run(start, size, target):
    symbols,_=source_state()
    if start < 0 or size < 1 or start >= len(symbols):
        raise ValueError('Rango fuera de los 290 símbolos')
    group=symbols[start:start+size]
    print('HLL_OPEN_AUDIT_GROUP',json.dumps({'start':start,'size':len(group),'symbols':group},ensure_ascii=False),flush=True)
    full_pre=pd.read_csv(PRE,low_memory=False)
    ids=first_attempt_ids(full_pre)
    pre=full_pre[full_pre.attempt_id.isin(ids)&full_pre.symbol.isin(group)].copy()
    del full_pre
    mech_cols=['symbol','attempt_id','mechanism_48','touch_v26_independent_48',
               'physical_overlap_clusters_48','confirmed_pivots_48','close_efficiency_48',
               'pre_30m_coverage_48']
    mech=load_subset_csv(MECH/'mechanism_attempts.csv',group,'symbol',mech_cols)
    v29_cols=['origin_symbol','attempt_id','branch','status','confirmed_ts',
              'signal_open_ts','signal_close','next_open_ts','next_open']
    v29=load_subset_csv(V29/'continuation_events.csv',group,'origin_symbol',v29_cols)
    selected,coverage,entries=build_entries(pre,mech,v29,CANDLES,OUT/'minutes',scope='ALL_ATTEMPTS')
    print('HLL_OPEN_AUDIT_ENTRIES',json.dumps({'attempts':len(selected),'verified_entries':len(entries),
          'by_route':{str(k):int(v) for k,v in entries.route.value_counts().items()}},ensure_ascii=False),flush=True)
    if entries.empty:
        print('HLL_OPEN_AUDIT_DONE',json.dumps({'compared':0,'mismatches':0}));return
    discrepancies=[]
    ncompared=0
    for symbol,grp in entries.groupby('symbol',sort=True):
        path=OUT/'minutes'/f'{symbol}_1m.csv'
        raw=pd.read_csv(path,usecols=['timestamp','open'])
        raw.timestamp=pd.to_numeric(raw.timestamp,errors='raise').astype('int64')
        if raw.timestamp.duplicated().any() or not raw.timestamp.is_monotonic_increasing:
            raise ValueError('1m duplicados/no ordenados: '+str(path))
        opens=raw.set_index('timestamp')['open']
        for e in grp.itertuples(index=False):
            ts=int(e.entry_ts)
            one=opens.get(ts,np.nan)
            price=float(e.entry_price)
            if not np.isfinite(one):
                print('HLL_OPEN_AUDIT_MISSING_MINUTE',json.dumps({'entry_id':e.entry_id,'symbol':symbol,
                      'route':e.route,'timestamp':ts},ensure_ascii=False))
                continue
            ncompared+=1
            if mismatch(float(one),price):
                discrepancies.append({'entry_id':e.entry_id,'attempt_id':e.attempt_id,
                  'symbol':symbol,'route':e.route,
                  'entry_ts':ts,'entry_utc':pd.to_datetime(ts,unit='ms',utc=True).isoformat(),
                  'verified_open':price,'minute_open':float(one),
                  'difference_pct':round(100*(float(one)/price-1),9)})
    print('HLL_OPEN_AUDIT_SUMMARY',json.dumps({'compared':ncompared,'mismatches':len(discrepancies),
       'mismatches_by_route':dict(Counter(r['route'] for r in discrepancies)),
       'largest_abs_difference_pct':round(max([abs(r['difference_pct']) for r in discrepancies],default=0),9)},ensure_ascii=False),flush=True)
    for row in discrepancies[:30]:
        print('HLL_OPEN_AUDIT_MISMATCH',json.dumps(row,ensure_ascii=False),flush=True)
    found=next((r for r in discrepancies if r['entry_id']==target),None)
    if found:
        print('HLL_OPEN_AUDIT_TARGET',json.dumps(found,ensure_ascii=False),flush=True)
    elif target:
        x=entries.loc[entries.entry_id.eq(target)]
        print('HLL_OPEN_AUDIT_TARGET_NOT_MISMATCH',json.dumps({'entry_id':target,
                'in_catalogue':not x.empty},ensure_ascii=False),flush=True)


def main():
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--batch-start',type=int,default=60)
    a.add_argument('--batch-size',type=int,default=5)
    a.add_argument('--entry-id',default='edeaf86d17b40186bd685b11')
    v=a.parse_args()
    run(v.batch_start,v.batch_size,v.entry_id)

if __name__=='__main__':main()
