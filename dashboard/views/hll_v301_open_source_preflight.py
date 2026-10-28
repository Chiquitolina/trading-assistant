#!/usr/bin/env python3
"""READ-ONLY HLL V301 preflight of frozen 30m signals vs exact 1m entry opens.

No detector changes, no simulation, no LIVE. Does not overwrite existing outputs.
Runs FIRST_PER_LEVEL globally before selecting batch symbols, preserving membership.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from dashboard.views.hll_v301_full290 import PRE, MECH, V29, CANDLES, OUT, source_state
from dashboard.views.hll_v295_net_matrix import first_attempt_ids
from dashboard.views.hll_v296_entry_audit import build_entries
from dashboard.views.hll_v294_entry_matrix import _read_observed_opens

MIN30=1_800_000
GUARD_PCT=0.1
FIELDS=('entry_id','attempt_id','symbol','route','entry_ts','entry_utc',
        'signal_reference_open_30m','archive_open_30m','execution_open_1m',
        'source_open_difference_pct','reference_vs_archive_30m_pct','classification')


def classify(reference, observed, route, guard_pct=GUARD_PCT):
    """Return status and signed difference in percent (1m/reference)."""
    if not math.isfinite(float(reference)) or float(reference)<=0:
        return 'INVALID_REFERENCE',None
    if observed is None or not math.isfinite(float(observed)) or float(observed)<=0:
        return 'MISSING_EXACT_1M',None
    diff=100*(float(observed)/float(reference)-1)
    unequal=abs(float(observed)-float(reference))>max(1e-10,abs(float(reference))*1e-7)
    if route=='FLIP_1M' and unequal:
        return 'FLIP_ARCHIVE_CHANGED',diff
    if not unequal:
        return 'EXACT',diff
    if abs(diff)>guard_pct:
        return 'OVER_SOURCE_GUARD',diff
    return 'WITHIN_SOURCE_GUARD',diff


def _subset(frame, column, symbols):
    return frame.loc[frame[column].astype(str).isin(symbols)].copy()


def _get_30m_opens(symbol, entry_timestamps):
    # Only the exact 30m-bar start is permitted for reference checking.
    stamps={int(t) for t in entry_timestamps if int(t)%MIN30==0}
    if not stamps:
        return {}
    path=CANDLES/f'{symbol}_30m.csv'
    if not path.is_file():
        raise FileNotFoundError(path)
    df=pd.read_csv(path,usecols=['timestamp','open'])
    ts=pd.to_numeric(df.timestamp,errors='raise').astype('int64')
    if ts.duplicated().any() or not ts.is_monotonic_increasing:
        raise ValueError(f'30m timestamps duplicated/disordered: {symbol}')
    x=df.loc[ts.isin(stamps)]
    return {int(row.timestamp):float(row.open) for row in x.itertuples(index=False)}


def run(start, size, chunk_size, guard_pct):
    if not math.isfinite(guard_pct) or guard_pct<=0:
        raise ValueError('Invalid source guard')
    symbols, source=source_state()
    if not (MECH/'mechanism_attempts.csv').is_file():
        raise FileNotFoundError('V292 mechanisms not consolidated')
    if start < 0 or start >= len(symbols) or size < 1 or chunk_size < 1:
        raise ValueError('Invalid symbol range')
    stop=min(len(symbols),start+size)
    targeted=symbols[start:stop]
    output=OUT/'source_audit'/f'preflight_FIRST_PER_LEVEL_{start:03d}_{stop:03d}.csv'
    summary_path=output.with_suffix('.summary.json')
    if output.exists() or summary_path.exists():
        raise FileExistsError(f'Report already exists, never overwrite: {output}')

    # Compute FIRST_PER_LEVEL on the full frozen PRE universe before slicing.
    pre=pd.read_csv(PRE,low_memory=False)
    first_ids=first_attempt_ids(pre)
    first=pre.loc[pre.attempt_id.astype(str).isin(first_ids)].copy()
    del pre
    mech_cols=['symbol','attempt_id','mechanism_48','touch_v26_independent_48',
               'physical_overlap_clusters_48','confirmed_pivots_48',
               'close_efficiency_48','pre_30m_coverage_48']
    head=pd.read_csv(MECH/'mechanism_attempts.csv',nrows=0).columns
    mech=pd.read_csv(MECH/'mechanism_attempts.csv',usecols=[c for c in mech_cols if c in head],low_memory=False)
    v29_cols=['origin_symbol','attempt_id','branch','status','confirmed_ts',
              'signal_open_ts','signal_close','next_open_ts','next_open']
    v29=pd.read_csv(V29/'continuation_events.csv',usecols=v29_cols,low_memory=False)

    rows=[]; counter=Counter(); route_counter=Counter(); worst=[]
    for i in range(0,len(targeted),chunk_size):
        group=targeted[i:i+chunk_size]
        pre_g=_subset(first,'symbol',group)
        mech_g=_subset(mech,'symbol',group)
        v29_g=_subset(v29,'origin_symbol',group)
        selected,_,entries=build_entries(pre_g,mech_g,v29_g,CANDLES,OUT/'minutes',scope='ALL_ATTEMPTS')
        for symbol,subset in entries.groupby('symbol',sort=True) if not entries.empty else []:
            stamps={int(t) for t in subset.entry_ts}
            opens1=_read_observed_opens(OUT/'minutes'/f'{symbol}_1m.csv',stamps)
            opens30=_get_30m_opens(symbol,stamps)
            for e in subset.itertuples(index=False):
                t=int(e.entry_ts);ref=float(e.entry_price)
                one=opens1.get(t)
                archive=opens30.get(t) if e.route!='FLIP_1M' else None
                status,delta=classify(ref,one,e.route,guard_pct)
                archive_delta=(100*(archive/ref-1) if archive is not None and ref>0 else None)
                if e.route!='FLIP_1M':
                    if t%MIN30:status='30M_ENTRY_NOT_ALIGNED'
                    elif archive is None:status='MISSING_ARCHIVE_30M'
                    elif abs(archive-ref)>max(1e-10,abs(ref)*1e-7):status='REFERENCE_30M_CHANGED'
                row=dict(entry_id=str(e.entry_id),attempt_id=str(e.attempt_id),symbol=str(symbol),
                         route=str(e.route),entry_ts=t,entry_utc=str(e.entry_utc),
                         signal_reference_open_30m=ref,archive_open_30m=archive,
                         execution_open_1m=one,source_open_difference_pct=delta,
                         reference_vs_archive_30m_pct=archive_delta,classification=status)
                rows.append(row)
                counter[status]+=1
                route_counter[(e.route,status)]+=1
                if delta is not None and status in ('OVER_SOURCE_GUARD','FLIP_ARCHIVE_CHANGED','REFERENCE_30M_CHANGED'):
                    worst.append(row)
        print('HLL_OPEN_PREFLIGHT_PROGRESS',json.dumps({
          'symbols_done':min(i+chunk_size,len(targeted)), 'symbols_total':len(targeted),
          'selected_attempts_in_group':len(selected), 'entries_in_group':len(entries),
          'compared_entries':len(rows), 'over_guard':counter['OVER_SOURCE_GUARD'],
          'missing':counter['MISSING_EXACT_1M']},ensure_ascii=False),flush=True)

    out=pd.DataFrame(rows,columns=FIELDS)
    output.parent.mkdir(parents=True,exist_ok=True)
    tmp=output.with_suffix('.csv.part')
    out.to_csv(tmp,index=False)
    os.replace(tmp,output)
    top=sorted(worst,key=lambda r:-abs(float(r['source_open_difference_pct'] or 0)))[:20]
    summary={'scope':'FIRST_PER_LEVEL','symbol_start':start,'symbol_stop':stop,'symbols_scanned':len(targeted),
             'attempt_universe':len(first),'entries_compared':len(rows),
             'classification_counts':dict(sorted(counter.items())),
             'by_route_and_classification':[
               {'route':k[0],'classification':k[1],'count':n}
               for k,n in sorted(route_counter.items())],
             'guard_pct':guard_pct,'highest_disagreements':top,
             'note':'Read only. No entry or signal is changed. Raw 30m/1m archives are not overwritten.',
             'csv':str(output)}
    summary_path.write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_OPEN_PREFLIGHT_DONE',json.dumps({
        'symbols':len(targeted),'entries':len(rows),'counts':dict(counter),
        'csv':str(output),'summary':str(summary_path)},ensure_ascii=False),flush=True)
    for row in top:
        print('HLL_OPEN_PREFLIGHT_FLAG',json.dumps(row,ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch-start',type=int,default=100)
    p.add_argument('--batch-size',type=int,default=5)
    p.add_argument('--chunk-size',type=int,default=5)
    p.add_argument('--guard-pct',type=float,default=0.1)
    a=p.parse_args()
    run(a.batch_start,a.batch_size,a.chunk_size,a.guard_pct)

if __name__=='__main__':
    main()
