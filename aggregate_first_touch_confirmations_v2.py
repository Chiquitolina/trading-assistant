#!/usr/bin/env python3
"""Pool V2 first-touch event rows from several symbols; never average percentages.

This is descriptive only. Before cross-period inference, verify matched source
hashes and reserve out-of-sample symbols/dates. Distinct pivots can share a path.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
from compare_first_touch_confirmations_v2 import summarize_all

REQUIRED=('symbol','pivot_touch_id','original_status','side','pre_eligible',
          'confirmation_min','policy','min_displacement_pct','horizon_min',
          'confirmation_coverage','outcome_coverage','confirmed','held_wick',
          'held_close','held_close_outside_band','race_0_3','race_0_5',
          'race_1_0','initial_close_wrong_side_but_later_accepted')


def pool(files):
    frames=[]
    for path in files:
        df=pd.read_csv(path,low_memory=False)
        if df.empty:raise ValueError(f'Empty events: {path}')
        missing=set(REQUIRED)-set(df.columns)
        if missing:raise ValueError(f'{path}: missing columns {sorted(missing)}')
        if df.symbol.nunique()!=1:
            raise ValueError('Each event input must contain precisely one symbol')
        frames.append(df)
    combined=pd.concat(frames,ignore_index=True)
    if combined.duplicated(['pivot_touch_id','confirmation_min','policy',
                            'min_displacement_pct','horizon_min']).any():
        raise ValueError('Duplicate pivot/rule/horizon; do not double count')
    if combined.symbol.nunique()!=len(frames):
        raise ValueError('Do not include the same symbol twice')
    group_count=combined.groupby(['pivot_touch_id']).size()
    from compare_reaction_confirmations_v1 import HORIZONS,rules
    expected=len(HORIZONS)*len(list(rules()))
    if not group_count.eq(expected).all():
        raise ValueError('Incomplete rule grid for one or more first-touch candidates')
    return combined,summarize_all(combined)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs',required=True,nargs='+')
    p.add_argument('--out',required=True)
    a=p.parse_args(argv)
    dest=Path(a.out).resolve()
    if dest.exists():raise FileExistsError('Output exists, choose a fresh file: '+str(dest))
    combined,summary=pool([Path(x).resolve() for x in a.inputs])
    dest.parent.mkdir(parents=True,exist_ok=True)
    summary.to_csv(dest,index=False)
    pivot_base=combined[['symbol','pivot_touch_id','original_status','retest_ts']].drop_duplicates()
    print('FIRST_TOUCH_V2_POOL',pivot_base.symbol.nunique(),'symbols',len(pivot_base),'pivot first touches')
    subset=summary[(summary.side_group=='ALL')&(summary.original_status_group=='ALL')&
                   (summary.horizon_min==30)]
    display=['confirmation_min','policy','min_displacement_pct',
             'eligible_complete_outcomes','selected_complete_outcomes',
             'selected_rescued_indecisive_n','selected_reclaimed_failed_n',
             'win_0_5_n','win_0_5_pct_selected','win_0_5_pct_all_eligible']
    print(subset[display].to_string(index=False))
    print('OUTPUT',dest)


if __name__=='__main__':main()
