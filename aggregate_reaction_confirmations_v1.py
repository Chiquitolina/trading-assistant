#!/usr/bin/env python3
"""Combine event-level confirmation audits WITHOUT averaging percentages across symbols.

Read-only: applies the same summary function on pooled, complete event rows.
Reports ALL, LONG, SHORT separately. Do not choose a winner from two cases.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
from compare_reaction_confirmations_v1 import summarize_records


def aggregate_files(files):
    frames=[]
    for f in files:
        x=pd.read_csv(f,low_memory=False)
        if x.empty:raise ValueError(f'Empty event-level input: {f}')
        required=['symbol','event_key','retest_ts','side','confirmation_min','policy',
                  'min_displacement_pct','horizon_min','confirmed','confirmation_coverage',
                  'outcome_coverage','held_wick','held_close','held_close_outside_band',
                  'mfe_pct','mae_pct','race_0_3','race_0_5','race_1_0',
                  'hit_0_3','hit_0_5','hit_1_0']
        missing=set(required)-set(x.columns)
        if missing:raise ValueError(f'Missing columns {missing} in {f}')
        frames.append(x)
    x=pd.concat(frames,ignore_index=True)
    identifier=['symbol','event_key','confirmation_min','policy','min_displacement_pct','horizon_min']
    if x.duplicated(identifier).any():
        raise ValueError('Duplicate rows across inputs; do not double count an event')
    summaries=[]
    for side,subset in (('ALL',x),('LONG',x[x.side=='LONG']),('SHORT',x[x.side=='SHORT'])):
        if subset.empty:continue
        ss=summarize_records(subset,'POOLED')
        ss.insert(0,'side_group',side)
        summaries.append(ss)
    return x,pd.concat(summaries,ignore_index=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',nargs='+',required=True)
    parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    files=[Path(f).resolve() for f in args.inputs]
    events,summary=aggregate_files(files)
    dest=Path(args.out).resolve()
    if dest.exists():raise FileExistsError('Output file already exists: '+str(dest))
    dest.parent.mkdir(parents=True,exist_ok=True)
    summary.to_csv(dest,index=False)
    print('POOLED',events.symbol.nunique(),'symbols',events.event_key.nunique(),'original reactions')
    cols=['side_group','confirmation_min','policy','min_displacement_pct','horizon_min',
          'eligible_complete','confirmed_n','selected_pct_of_eligible','wick_held_pct',
          'win_0_5_first_pct_selected','win_0_5_first_pct_all_eligible']
    print(summary.loc[(summary.side_group=='ALL')&(summary.horizon_min==30),cols].to_string(index=False))
    print('OUTPUT',dest)

if __name__=='__main__':main()
