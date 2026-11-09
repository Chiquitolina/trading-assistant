#!/usr/bin/env python3
"""Offline, read-only audit of BTC HLL levels after MAX_AGE expiry.

No detector changes. Output is descriptive and never a PRE trading label.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import pandas as pd

LEVELS = {
    'VIOLETA': 'ce7c566bfe13084ba002577b',
    'NARANJA_INFERIOR': '3450c0079d4e691cd7144642',
    'NARANJA_INTERMEDIA': 'd6eee9bf1b3640986ca5ca3b',
}
STEP = 30 * 60 * 1000

def side(p, lo, hi):
    return 'ABOVE' if p > hi else ('BELOW' if p < lo else 'INSIDE')

def fmt_time(t):
    return pd.to_datetime(int(t), unit='ms', utc=True).isoformat()

def group_runs(bars, mask, after):
    """Group qualifying contiguous touches; non-touch bar separates runs."""
    groups, current = [], []
    for i in range(len(bars)):
        t = int(bars.iloc[i]['timestamp'])
        if t <= after:
            continue
        if bool(mask[i]):
            if current and (i != current[-1]+1 or t-int(bars.iloc[current[-1]]['timestamp']) != STEP):
                groups.append(current)
                current = []
            current.append(i)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups

def continuous_slice(bars, index, count):
    xs=[]
    initial=int(bars.iloc[index]['timestamp'])
    for j in range(index+1, min(len(bars), index+count+1)):
        if int(bars.iloc[j]['timestamp']) != initial+(j-index)*STEP:
            break
        xs.append(j)
    return xs

def analyze(base: Path, out: Path):
    lv = pd.read_csv(base/'snapshot_asof.csv')
    ends = pd.read_csv(base/'ends.csv')
    bars = pd.read_csv(base/'candles_30m.csv')
    needed_bars=['timestamp','open','high','low','close']
    needed_levels=['level_id','center','zone_low','zone_high','formation_closed_ts','last_observed_closed_ts','state_at_cutoff']
    for table, names in [(bars,needed_bars),(lv,needed_levels)]:
        absent=set(names)-set(table.columns)
        if absent: raise ValueError(f'Missing columns: {sorted(absent)}')
    bars=bars.sort_values('timestamp').reset_index(drop=True)
    if bars['timestamp'].duplicated().any(): raise ValueError('Duplicate bar timestamps')
    for col in needed_bars: bars[col]=pd.to_numeric(bars[col],errors='raise')
    for col in ['formation_closed_ts','last_observed_closed_ts','center','zone_low','zone_high']:
        lv[col]=pd.to_numeric(lv[col],errors='raise')
    if ((bars.high<bars.low) | (bars.high<bars.open) | (bars.high<bars.close) | (bars.low>bars.open) | (bars.low>bars.close)).any():
        raise ValueError('Invalid OHLC')
    output=[]; successors=[]; overview=[]
    for name,lid in LEVELS.items():
        old=lv.loc[lv.level_id.astype(str)==lid]
        if len(old)!=1: raise ValueError(f'Expected single old level {lid}, found {len(old)}')
        old=old.iloc[0]
        expiry=int(old.last_observed_closed_ts)
        lo,hi=float(old.zone_low),float(old.zone_high)
        center=float(old.center)
        end=ends.loc[ends.level_id.astype(str)==lid]
        if len(end)!=1: raise ValueError(f'Expected single end record for {lid}: {len(end)}')
        reason=str(end.iloc[0]['end_state'])
        if reason!='MAX_AGE': raise ValueError(f'Expected MAX_AGE for {lid}, saw {reason}')
        mask=(bars.high>=lo)&(bars.low<=hi)
        groups=group_runs(bars,mask,expiry)
        history=lv.loc[lv.level_id.astype(str)!=lid].copy()
        history=history[(history.formation_closed_ts>=expiry)&(history.formation_closed_ts<=int(bars.timestamp.max()))]
        for _, cand in history.iterrows():
            cl,ch=float(cand.zone_low),float(cand.zone_high)
            overlap=max(0.,min(hi,ch)-max(lo,cl))
            if overlap<=0: continue
            union=max(hi,ch)-min(lo,cl)
            successors.append({'region':name,'old_level_id':lid,'candidate_level_id':str(cand.level_id),
                               'old_center':center,'candidate_center':float(cand.center),
                               'center_gap_usd':abs(float(cand.center)-center),
                               'overlap_iou':overlap/union,'candidate_birth_utc':fmt_time(cand.formation_closed_ts),
                               'hours_after_expiry':(int(cand.formation_closed_ts)-expiry)/3600000,
                               'candidate_state_at_cutoff':str(cand.state_at_cutoff)})
        for k,run in enumerate(groups,1):
            i=run[0]
            first=bars.iloc[i]
            prev=bars.iloc[i-1] if i>0 and int(first.timestamp)-int(bars.iloc[i-1].timestamp)==STEP else None
            pre_side=side(float(prev.close),lo,hi) if prev is not None else 'UNKNOWN'
            first_side=side(float(first.close),lo,hi)
            last_i=run[-1]
            finish=int(bars.iloc[last_i].timestamp)
            # Outcome horizon is measured after the END of each touch run, not just its first bar.
            future=continuous_slice(bars,last_i,4)
            close30= float(bars.iloc[future[0]].close) if len(future)>=1 else None
            close60= float(bars.iloc[future[1]].close) if len(future)>=2 else None
            close120=float(bars.iloc[future[3]].close) if len(future)>=4 else None
            fside=[side(float(bars.iloc[j].close),lo,hi) for j in future]
            stable_up = len(fside)>=2 and fside[0]=='ABOVE' and fside[1]=='ABOVE'
            stable_down = len(fside)>=2 and fside[0]=='BELOW' and fside[1]=='BELOW'
            if stable_up: physical='TWO_CLOSES_ABOVE_POST_RUN'
            elif stable_down: physical='TWO_CLOSES_BELOW_POST_RUN'
            elif len(future)<2: physical='CENSORED_OR_GAP'
            else: physical='NO_TWO_CLOSE_CONFIRMATION'
            # descriptive excursion from center over contact run and four subsequent contiguous bars.
            ws = run + future
            hmax=max(float(bars.iloc[j].high) for j in ws)
            lmin=min(float(bars.iloc[j].low) for j in ws)
            output.append({'region':name,'old_level_id':lid,'run_index':k,'expiry_utc':fmt_time(expiry),
                           'start_utc':fmt_time(first.timestamp),'end_utc':fmt_time(finish),
                           'hours_after_expiry':(int(first.timestamp)-expiry)/3600000,
                           'touch_bars':len(run),'arrival_previous_close':float(prev.close) if prev is not None else None,
                           'arrival_side':pre_side,'first_close':float(first.close),'first_close_side':first_side,
                           'last_touch_close_side':side(float(bars.iloc[last_i].close),lo,hi),
                           'run_max_high':max(float(bars.iloc[j].high) for j in run),
                           'run_min_low':min(float(bars.iloc[j].low) for j in run),
                           'window_excursion_above_band_usd':max(0.,hmax-hi),
                           'window_excursion_below_band_usd':max(0.,lo-lmin),
                           'post_run_close_30m':close30,'post_run_close_60m':close60,'post_run_close_120m':close120,
                           'post_run_side_30m':fside[0] if len(fside)>=1 else 'UNKNOWN',
                           'post_run_side_60m':fside[1] if len(fside)>=2 else 'UNKNOWN',
                           'post_run_side_120m':fside[3] if len(fside)>=4 else 'UNKNOWN',
                           'two_close_outcome':physical,'max_up_from_center_pct':100*(hmax/center-1),
                           'max_down_from_center_pct':100*(lmin/center-1),
                           'valid_forward_bars':len(future),
                           'warning':'DESCRIPTIVE_POST_NOT_LIFECYCLE_DEFENSE'})
        overview.append({'region':name,'center':center,'expiry_utc':fmt_time(expiry),
                         'expiry_reason':reason,'post_expiry_touch_runs':len(groups),
                         'post_expiry_touch_bars':sum(len(x) for x in groups),
                         'last_available_bar_utc':fmt_time(bars.timestamp.max())})
    out.mkdir(parents=True,exist_ok=True)
    events=pd.DataFrame(output)
    candidates=pd.DataFrame(successors)
    summaries=pd.DataFrame(overview)
    events.to_csv(out/'post_expiry_runs.csv',index=False)
    candidates.to_csv(out/'successor_candidates.csv',index=False)
    summaries.to_csv(out/'region_summary.csv',index=False)
    with (out/'audit_manifest.json').open('w') as f:
        json.dump({'mode':'OFFLINE_READ_ONLY','classification':'DESCRIPTIVE_POST_ONLY',
                   'successor_definition':'forming after old expiry, overlapping band; not proof of identity',
                   'two_close_definition':'two consecutive closes after touch run exits; not the Lifecycle rule',
                   'no_detector_mutation':True,'run_counts':{r['region']:r['post_expiry_touch_runs'] for r in overview}},f,indent=2)
    print('\n=== ALL POST-EXPIRY RUNS ===')
    if events.empty: print('No runs')
    else:
        for name,g in events.groupby('region',sort=False):
            print('\n',name,'runs',len(g))
            print(g[['run_index','start_utc','touch_bars','arrival_side','last_touch_close_side','two_close_outcome','post_run_side_120m']].to_string(index=False))
    print('\n=== SUCCESSOR CANDIDATES (TOP BY OVERLAP; NOT PROVEN) ===')
    if candidates.empty: print('No candidates')
    else:
        for name,g in candidates.groupby('region',sort=False):
            print('\n',name)
            print(g.sort_values(['overlap_iou','hours_after_expiry'],ascending=[False,True]).head(10)[['candidate_level_id','candidate_center','overlap_iou','hours_after_expiry','candidate_state_at_cutoff']].to_string(index=False))
    print('\nFILES:',out)
    return events,candidates,summaries

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',type=Path,default=Path('reports/horizontal_level_hierarchy_v1/runs/btc_60d_hierarchy_v1'))
    parser.add_argument('--out',type=Path,default=Path('reports/horizontal_level_continuity_audit_v1/btc_3_regions'))
    a=parser.parse_args()
    analyze(a.base,a.out)
