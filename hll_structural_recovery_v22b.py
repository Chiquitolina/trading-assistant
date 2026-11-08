#!/usr/bin/env python3
"""HLL V2.2-B pilot: descriptive 30m recovery after original LOSS_CONFIRMED.

Read-only, frozen run. Not a signal, flip detector, or proof of an executable reclaim.
Current-cutoff zone identities only; post-loss horizons are descriptive. The OHLC
30m bar cannot order intrabar touches. Requires verified V1/V2/V2.2A manifests.
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from hll_hierarchical_levels_v1 import TF, sha
from hll_structural_zones_v2 import verified_source, load_v2, snapshot_zones
from hll_structural_behavior_inspector_v22 import verified_load

VERSION='hll-structural-recovery-v2.2b-pilot-30m'
COLS=['episode_id','zone_id','side','level_id','test_id','loss_closed_ts',
      'level_low','level_high','zone_low','zone_high','zone_created_at',
      'horizon_bars','bars_observed','complete_horizon','first_zone_recovery_ts',
      'first_level_recovery_ts','zone_recovery_2close_ts','level_recovery_2close_ts',
      'zone_hold_bars_after_2close','level_hold_bars_after_2close',
      'zone_close_above_count','zone_close_below_count','zone_close_inside_count',
      'max_opposite_excursion_pct','last_zone_relation','ambiguity_note']

def _first_true_time(mask, times):
    idx=np.flatnonzero(np.asarray(mask,dtype=bool))
    return int(times[idx[0]]) if len(idx) else None

def _two_close(mask,times):
    arr=np.asarray(mask,dtype=bool)
    idx=np.flatnonzero(arr[1:] & arr[:-1])+1
    return int(times[idx[0]]) if len(idx) else None

def _hold(mask,times,trigger):
    if trigger is None:return None
    idx=np.flatnonzero(times==trigger)
    if len(idx)!=1:raise ValueError('Trigger timestamp missing')
    i=int(idx[0]);n=0
    for ok in mask[i+1:]:
        if not ok:break
        n+=1
    return n

def analyze_loss(bars, loss_ts, side, level_band, zone_band, *,horizon_bars=24,cutoff=None):
    """Only closed 30m bars strictly AFTER a confirmed loss; no future beyond cutoff.

    Recovery means close crosses to original approach side. Two closes are
    descriptive confirmation timestamps; no trade is entered at those closes.
    """
    if side not in ('ABOVE','BELOW') or not 1<=horizon_bars<=240:
        raise ValueError('Invalid direction or horizon')
    ll,lh=map(float,level_band);zl,zh=map(float,zone_band)
    if not (0<ll<=lh and 0<zl<=zh):raise ValueError('Invalid bands')
    if loss_ts%TF:raise ValueError('Loss not aligned to close')
    t=bars.timestamp.to_numpy(dtype=np.int64)+TF
    if len(t)<2 or np.any(np.diff(t)!=TF):raise ValueError('Discontinuous candles')
    if cutoff is None:cutoff=int(t[-1])
    if cutoff%TF or cutoff>int(t[-1]):raise ValueError('Invalid cutoff')
    if not np.isin(loss_ts,t):raise ValueError('Loss candle not in history')
    mask=(t>loss_ts)&(t<=cutoff)&(t<=loss_ts+horizon_bars*TF)
    times=t[mask];close=bars.close.to_numpy(dtype=float)[mask]
    high=bars.high.to_numpy(dtype=float)[mask];low=bars.low.to_numpy(dtype=float)[mask]
    if not len(times):return dict(bars_observed=0,complete_horizon=False,
        first_zone_recovery_ts=None,first_level_recovery_ts=None,
        zone_recovery_2close_ts=None,level_recovery_2close_ts=None,
        zone_hold_bars_after_2close=None,level_hold_bars_after_2close=None,
        zone_close_above_count=0,zone_close_below_count=0,zone_close_inside_count=0,
        max_opposite_excursion_pct=None,last_zone_relation='NO_FOLLOWUP',
        ambiguity_note='NO_CLOSED_FOLLOWUP')
    if side=='ABOVE':
        zone_ok=close>zh;level_ok=close>lh
        adverse=np.maximum(0,(zl-low)/zl*100)
    else:
        zone_ok=close<zl;level_ok=close<ll
        adverse=np.maximum(0,(high-zh)/zh*100)
    z2=_two_close(zone_ok,times);l2=_two_close(level_ok,times)
    last='ABOVE' if close[-1]>zh else 'BELOW' if close[-1]<zl else 'INSIDE'
    complete=bool(times[-1]>=loss_ts+horizon_bars*TF)
    return dict(bars_observed=len(times),complete_horizon=complete,
        first_zone_recovery_ts=_first_true_time(zone_ok,times),
        first_level_recovery_ts=_first_true_time(level_ok,times),
        zone_recovery_2close_ts=z2,level_recovery_2close_ts=l2,
        zone_hold_bars_after_2close=_hold(zone_ok,times,z2),
        level_hold_bars_after_2close=_hold(level_ok,times,l2),
        zone_close_above_count=int((close>zh).sum()),
        zone_close_below_count=int((close<zl).sum()),
        zone_close_inside_count=int(((close>=zl)&(close<=zh)).sum()),
        max_opposite_excursion_pct=round(float(adverse.max()),6),
        last_zone_relation=last,
        ambiguity_note='30M_OHLC_NO_INTRABAR_ORDER' + (';RIGHT_CENSORED' if not complete else ''))

def build(root, v1, v2, audit, tag, *,run=False,horizon_bars=24,min_free_gb=10):
    root=Path(root).resolve()
    def absolute(p):
        p=Path(p);return p.resolve() if p.is_absolute() else (root/p).resolve()
    v1,v2,audit=map(absolute,(v1,v2,audit))
    vm,f,e,ends,bars=verified_source(v1)
    cutoff=int(vm['cutoff_ms'])
    v2manifest,links=load_v2(v2,v1/'manifest.json')
    _,ep,tr=verified_load(audit,v1,v2,cutoff)
    zones,_=snapshot_zones(f,e,ends,bars,links,cutoff)
    zd={str(r.zone_id):r for r in zones.itertuples(index=False)}
    fd={str(r.level_id):r for r in f.itertuples(index=False)}
    records=[]
    loss=tr.loc[(tr.transition_role=='RESOLUTION') & (tr.event_type=='LOSS_CONFIRMED')].copy()
    for r in loss.itertuples(index=False):
        zone=zd.get(str(r.zone_id));level=fd.get(str(r.level_id))
        if zone is None or level is None:raise ValueError('Missing frozen identity')
        t=int(r.observed_closed_ts)
        if t<int(zone.created_at):raise ValueError('Loss before zone birth')
        result=analyze_loss(bars,t,str(r.side),(level.zone_low,level.zone_high),
                            (zone.low,zone.high),horizon_bars=horizon_bars,cutoff=cutoff)
        records.append(dict(episode_id=str(r.episode_id),zone_id=str(r.zone_id),
            side=str(r.side),level_id=str(r.level_id),test_id=str(r.test_id),
            loss_closed_ts=t,level_low=level.zone_low,level_high=level.zone_high,
            zone_low=zone.low,zone_high=zone.high,zone_created_at=int(zone.created_at),
            horizon_bars=horizon_bars,**result))
    df=pd.DataFrame(records,columns=COLS)
    if not re.fullmatch(r'[A-Za-z0-9_.-]{2,80}',tag):raise ValueError('Invalid tag')
    dest=root/'reports/horizontal_level_structural_behavior_v22/runs'/tag
    plan=dict(version=VERSION,mode='RUN_OFFLINE' if run else 'PLAN_READ_ONLY',
        symbol=vm['symbol'],cutoff_ms=cutoff,source_v1_manifest_sha256=sha(v1/'manifest.json'),
        source_v2_manifest_sha256=sha(v2/'manifest.json'),
        source_v22a_manifest_sha256=sha(audit/'manifest.json'),
        horizon_bars=horizon_bars,loss_events=len(df),
        recovered_zone_2close=int(df.zone_recovery_2close_ts.notna().sum()),
        recovered_level_2close=int(df.level_recovery_2close_ts.notna().sum()),
        right_censored=int((~df.complete_horizon).sum()),output_dir=str(dest),
        limitations=['Current-cutoff zone identities only','Frozen 30m candles, no intrabar sequence',
                     'Recovery observational only, not an executable entry or a flip',
                     'Recovery horizon after LOSS, not after episode first test'])
    if not run:return plan
    if shutil.disk_usage(root).free/(1024**3)<min_free_gb:raise RuntimeError('DISK_GUARD')
    if dest.exists():raise FileExistsError(str(dest))
    dest.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent,prefix='.behavior-v22b-') as temp:
        p=Path(temp);df.to_csv(p/'loss_recovery_30m.csv',index=False)
        plan['files_sha256']={'loss_recovery_30m.csv':sha(p/'loss_recovery_30m.csv')}
        (p/'manifest.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        p.rename(dest)
    return plan

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.')
    p.add_argument('--v1-run',required=True);p.add_argument('--v2-run',required=True)
    p.add_argument('--audit-run',required=True);p.add_argument('--tag',required=True)
    p.add_argument('--horizon-bars',type=int,default=24)
    p.add_argument('--min-free-gb',type=float,default=10)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    a=p.parse_args(argv)
    print('HLL_BEHAVIOR_V22B',json.dumps(build(a.project,a.v1_run,a.v2_run,a.audit_run,a.tag,
        run=a.run,horizon_bars=a.horizon_bars,min_free_gb=a.min_free_gb),ensure_ascii=False))
if __name__=='__main__':main()
