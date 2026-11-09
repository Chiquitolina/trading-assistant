#!/usr/bin/env python3
"""HLL historical succession candidates — offline/read-only and as-of bounded.

Candidate proximity/overlap is NOT reactivation, rejection, or trading evidence.
A pair becomes observable no earlier than the successor's formation timestamp.
"""
from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path

import pandas as pd

MS_DAY = 86_400_000
REQUIRED = {'level_id','center','zone_low','zone_high','formation_closed_ts',
            'last_observed_closed_ts','state_at_cutoff'}


def successor_candidates(levels: pd.DataFrame, *, cutoff_ms: int, max_gap_days: float=14,
                         min_overlap_iou: float=.25, max_center_distance_pct: float=.5,
                         max_age_ids=None) -> pd.DataFrame:
    """Find forward-born band-overlapping pairs among expired old levels.

    Old levels must have ended *strictly* before cutoff. If max_age_ids is
    supplied, allow only levels whose verified ends.csv reason was MAX_AGE.
    Never use future formation data (birth >= cutoff).
    """
    missing = REQUIRED - set(levels.columns)
    if missing:
        raise ValueError(f'Missing columns: {sorted(missing)}')
    if not (0 < min_overlap_iou <= 1 and max_gap_days > 0 and max_center_distance_pct > 0):
        raise ValueError('Invalid thresholds')
    if levels.level_id.astype(str).duplicated().any():
        raise ValueError('Duplicate level ID')
    cutoff_ms = int(cutoff_ms)
    df = levels.copy()
    for col in ('formation_closed_ts','last_observed_closed_ts','center','zone_low','zone_high'):
        df[col] = pd.to_numeric(df[col],errors='raise')
    df = df.loc[(df.formation_closed_ts < cutoff_ms) &
                (df.formation_closed_ts <= df.last_observed_closed_ts) &
                (df.zone_high > df.zone_low) & (df.center > 0)].copy()
    df['level_id'] = df.level_id.astype(str)
    old = df.loc[(df.last_observed_closed_ts < cutoff_ms) &
                 (~df.state_at_cutoff.eq('ACTIVE'))].copy()
    if max_age_ids is not None:
        old = old.loc[old.level_id.isin(set(map(str,max_age_ids)))]
    newborns = df.sort_values(['formation_closed_ts','level_id'],kind='stable')
    born = newborns.formation_closed_ts.astype('int64').tolist()
    rows=[]
    max_gap = int(max_gap_days*MS_DAY)
    for r in old.itertuples(index=False):
        end=int(r.last_observed_closed_ts)
        start_idx=bisect.bisect_right(born,end)
        stop_idx=bisect.bisect_right(born,min(cutoff_ms-1,end+max_gap))
        for s in newborns.iloc[start_idx:stop_idx].itertuples(index=False):
            if s.level_id == r.level_id:
                continue
            gap = abs(float(s.center)-float(r.center))/float(r.center)*100
            if gap > max_center_distance_pct:
                continue
            overlap = max(0.0,min(float(r.zone_high),float(s.zone_high)) -
                          max(float(r.zone_low),float(s.zone_low)))
            if overlap <= 0:
                continue
            union = (max(float(r.zone_high),float(s.zone_high)) -
                     min(float(r.zone_low),float(s.zone_low)))
            iou = overlap/union
            if iou < min_overlap_iou:
                continue
            rows.append({'old_level_id':r.level_id,'successor_level_id':s.level_id,
                'old_center':float(r.center),'successor_center':float(s.center),
                'old_end_ms':end,'successor_birth_ms':int(s.formation_closed_ts),
                'hours_after_expiry':(int(s.formation_closed_ts)-end)/3_600_000,
                'center_distance_pct':gap,'overlap_iou':iou,
                'old_band_low':float(r.zone_low),'old_band_high':float(r.zone_high),
                'new_band_low':float(s.zone_low),'new_band_high':float(s.zone_high),
                'candidate_state_at_cutoff':str(s.state_at_cutoff),
                'classification':'SUCCESSOR_CANDIDATE_NOT_PROVEN'})
    cols=['old_level_id','successor_level_id','old_center','successor_center',
          'old_end_ms','successor_birth_ms','hours_after_expiry',
          'center_distance_pct','overlap_iou','old_band_low','old_band_high',
          'new_band_low','new_band_high','candidate_state_at_cutoff','classification']
    return (pd.DataFrame(rows,columns=cols)
            .sort_values(['old_level_id','overlap_iou','hours_after_expiry','successor_level_id'],
                         ascending=[True,False,True,True],kind='stable').reset_index(drop=True))


def max_age_ids_from_ends(ends:pd.DataFrame, *, cutoff_ms:int):
    needed={'level_id','end_state','end_closed_ts'}
    if not needed.issubset(ends.columns):
        raise ValueError(f'Missing ends columns: {sorted(needed-set(ends.columns))}')
    completed=ends.loc[(ends.end_state.astype(str)=='MAX_AGE') &
                       (pd.to_numeric(ends.end_closed_ts,errors='raise') < int(cutoff_ms))]
    return set(completed.level_id.astype(str))


def audit(base:Path, out:Path, *, max_gap_days=14., min_overlap_iou=.25):
    base=Path(base);out=Path(out)
    levels=pd.read_csv(base/'snapshot_asof.csv',low_memory=False)
    ends=pd.read_csv(base/'ends.csv',low_memory=False)
    bars=pd.read_csv(base/'candles_30m.csv')
    if bars.empty or 'timestamp' not in bars:
        raise ValueError('Missing timestamps')
    end_of_data=int(pd.to_numeric(bars.timestamp,errors='raise').max())+30*60*1000
    if 'state_at_cutoff' not in levels:
        raise ValueError('Missing state_at_cutoff')
    ids=max_age_ids_from_ends(ends,cutoff_ms=end_of_data)
    pairs=successor_candidates(levels,cutoff_ms=end_of_data,
                               max_gap_days=max_gap_days,min_overlap_iou=min_overlap_iou,
                               max_age_ids=ids)
    expired=levels.loc[levels.level_id.astype(str).isin(ids)].copy()
    counts=pairs.groupby('old_level_id').size().rename('candidate_count') if not pairs.empty else pd.Series(dtype='int64',name='candidate_count')
    summary=expired[['level_id','center','formation_closed_ts','last_observed_closed_ts','state_at_cutoff']].copy()
    summary=summary.join(counts,on='level_id').fillna({'candidate_count':0})
    summary['candidate_count']=summary.candidate_count.astype('int64')
    summary['has_candidate']=summary.candidate_count.gt(0)
    out.mkdir(parents=True,exist_ok=True)
    pairs.to_csv(out/'succession_candidates_all.csv',index=False)
    summary.to_csv(out/'expired_levels_coverage.csv',index=False)
    manifest={'mode':'OFFLINE_READ_ONLY','cutoff_ms':end_of_data,
              'old_level_filter':'ends.csv MAX_AGE before cutoff',
              'new_level_filter':'formed strictly after expiry and before cutoff',
              'candidate_not_proof':True,'no_live_changes':True,
              'max_gap_days':max_gap_days,'min_overlap_iou':min_overlap_iou,
              'expired_levels':int(len(summary)),'expired_with_candidates':int(summary.has_candidate.sum()),
              'candidate_pairs':int(len(pairs))}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print('HLL SUCCESSION V1C',json.dumps(manifest,ensure_ascii=False))
    for lid in ('ce7c566bfe13084ba002577b','3450c0079d4e691cd7144642',
                'd6eee9bf1b3640986ca5ca3b'):
        picked=pairs.loc[pairs.old_level_id.eq(lid)].head(5)
        print('\nORIGINAL',lid,'candidates:',len(pairs.loc[pairs.old_level_id.eq(lid)]))
        if not picked.empty:
            print(picked[['successor_level_id','successor_center','overlap_iou','hours_after_expiry']].to_string(index=False))
    print('FILES',out)
    return pairs,summary,manifest

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base',type=Path,default=Path('reports/horizontal_level_hierarchy_v1/runs/btc_60d_hierarchy_v1'))
    p.add_argument('--out',type=Path,default=Path('reports/horizontal_level_succession_v1c/btc_60d'))
    p.add_argument('--max-gap-days',type=float,default=14)
    p.add_argument('--min-overlap-iou',type=float,default=.25)
    args=p.parse_args()
    audit(args.base,args.out,max_gap_days=args.max_gap_days,min_overlap_iou=args.min_overlap_iou)
