#!/usr/bin/env python3
"""Offline causal census: all level crosses, approaches, attacks, volume and 1m outcomes.

Never alters the frozen V2.1 atlas or LIVE processes. Every factor is known at
30m bar close; outcomes use the opening of a *later* 1m candle (1m delay).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from horizontal_breakout_lab import Archive, ROOT, TF, M
from horizontal_level_lifecycle_v21 import load_30m, _segment_bounds, _atr

VERSION = 'horizontal-level-cross-research-v2.2'
DEST = Path('reports/horizontal_level_cross_research_v22/runs')
SIGNALS = ('event_id','symbol','level_id','origin','stage','direction','known_closed_ts','known_utc',
           'bar_open_ts','center','zone_low','zone_high','zone_width_pct','zone_quality_flag','formation_closed_ts','level_age_bars',
           'prior_cross_up','prior_cross_down','prior_tests','prior_retests_directional',
           'prior_returns_inside','prior_failed_cross_up','prior_failed_cross_down',
           'prior_reconfirmations','prev_relation','close_relation','previous_close','close','high','low',
           'distance_pre_atr','distance_now_atr','atr_pre','vol_current','vol_pre20_median',
           'vol_ratio','vol_pre5_20_ratio','vol_rank_prev20_pct','body_atr','range_atr',
           'bar_close_position','volume_source','feature_state','event_source',
           'cluster_symbol_closed','source_30m')
OUTCOMES=('event_id','symbol','stage','direction','known_closed_ts','entry_ts','entry_price',
          'horizon_min','tp_pct','sl_pct','fee_roundtrip_pct','path_state','outcome',
          'return_gross_pct','return_after_fee_pct','mfe_pct','mae_pct','bars_available')


def ts_iso(ts):
    return datetime.fromtimestamp(int(ts)/1000,timezone.utc).isoformat()


def digest(*parts):
    return hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()[:24]


def sha_file(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def save_csv(p, rows, cols):
    p.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.hllcross-',suffix='.csv',dir=p.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(rows)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def save_json(p,d):
    p.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.hllcross-',suffix='.json',dir=p.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(d,f,indent=2,ensure_ascii=False,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def relation(x,lo,hi):
    return 'ABOVE' if x>hi else 'BELOW' if x<lo else 'INSIDE'


def stage_for_cross(origin, typ):
    if origin=='HIGH' and typ=='CROSS_UP':return 'RESISTANCE_CROSS_UP','LONG'
    if origin=='LOW' and typ=='CROSS_DOWN':return 'SUPPORT_CROSS_DOWN','SHORT'
    if origin=='LOW' and typ=='CROSS_UP':return 'CONTEXT_SUPPORT_CROSS_UP','LONG'
    if origin=='HIGH' and typ=='CROSS_DOWN':return 'CONTEXT_RESISTANCE_CROSS_DOWN','SHORT'
    return None


def pre_volume(frame, i):
    """Current *closed* volume relative only to preceding 20 CLOSED candles."""
    if i<20:return dict(vol_current=np.nan,vol_pre20_median=np.nan,
                        vol_ratio=np.nan,vol_pre5_20_ratio=np.nan,
                        vol_rank_prev20_pct=np.nan,volume_source='INSUFFICIENT_PRE_20')
    # Quote-volume is preferred only when full 21-bar window has finite positive data.
    q=frame.quoteVolume.iloc[i-20:i+1].to_numpy(dtype=float)
    use_quote=np.all(np.isfinite(q)) and np.all(q>0)
    v=q if use_quote else frame.volume.iloc[i-20:i+1].to_numpy(dtype=float)
    if not np.all(np.isfinite(v)) or np.any(v<0) or float(np.median(v[:-1]))<=0:
        return dict(vol_current=np.nan,vol_pre20_median=np.nan,vol_ratio=np.nan,
                    vol_pre5_20_ratio=np.nan,vol_rank_prev20_pct=np.nan,volume_source='INVALID_VOLUME')
    base=float(np.median(v[:-1]));current=float(v[-1]);
    return dict(vol_current=current,vol_pre20_median=base,vol_ratio=current/base,
                vol_pre5_20_ratio=float(np.median(v[-6:-1])/base),
                vol_rank_prev20_pct=100*float(np.mean(v[:-1]<=current)),
                volume_source='QUOTE_VOLUME' if use_quote else 'BASE_VOLUME')


def prior_counters(evs, cutoff):
    """Only events from this level STRICTLY EARLIER than candidate time."""
    old=evs[evs.observed_closed_ts<cutoff].sort_values('observed_closed_ts')
    kinds=old.event_type.tolist()
    # Return/opp-side close after a CROSS, before the candidate, counts as prior failure.
    pending_up=pending_down=False;fail_up=fail_down=0
    for kind in kinds:
        if kind=='CROSS_UP':pending_up=True
        elif kind=='CROSS_DOWN':
            if pending_up:fail_up+=1;pending_up=False
            pending_down=True
        elif kind=='RETURN_INSIDE':
            if pending_up:fail_up+=1;pending_up=False
            if pending_down:fail_down+=1;pending_down=False
        if kind=='CROSS_UP' and pending_down:
            fail_down+=1;pending_down=False
    return dict(prior_cross_up=kinds.count('CROSS_UP'),prior_cross_down=kinds.count('CROSS_DOWN'),
        prior_tests=sum(k.startswith('TEST_FROM_') for k in kinds),
        prior_retests_directional=sum(k=='TEST_FROM_BELOW' for k in kinds),
        prior_returns_inside=kinds.count('RETURN_INSIDE'),
        prior_failed_cross_up=fail_up,prior_failed_cross_down=fail_down,
        prior_reconfirmations=kinds.count('LEVEL_RECONFIRMED'))


def causal_signals_for_symbol(symbol, frame, forms, events, ends):
    """Only levels active at this historical CLOSE; never select by outcomes."""
    if frame.empty or forms.empty:return []
    frame=frame.reset_index(drop=True)
    end_map=ends.set_index('level_id').end_closed_ts.to_dict()
    forms=forms.set_index('level_id',drop=False)
    per_level={str(k):v.sort_values('observed_closed_ts') for k,v in events.groupby('level_id')}
    times={int(v):i for i,v in enumerate(frame.timestamp)}
    # Validate closed bars and skip lookback crossing archive gaps.
    contiguous=np.zeros(len(frame),dtype=int)
    for i in range(1,len(frame)):
        contiguous[i]=contiguous[i-1]+1 if int(frame.timestamp.iloc[i])-int(frame.timestamp.iloc[i-1])==TF else 0
    candidates=[]
    recorded=set()
    for level_id, lv in forms.iterrows():
        lid=str(level_id);origin=str(lv.origin)
        if origin not in ('HIGH','LOW'):continue
        lo=float(lv.zone_low);hi=float(lv.zone_high)
        formed=int(lv.formation_closed_ts)
        end=int(end_map.get(lid,0))
        le=per_level.get(lid,pd.DataFrame(columns=events.columns))
        # Always preserve every CROSS_UP, including repeated and adverse cases.
        crosses=le[le.event_type.isin(['CROSS_UP','CROSS_DOWN'])]
        for ce in crosses.itertuples():
            cls=stage_for_cross(origin,ce.event_type)
            if cls is None:continue
            k=int(ce.observed_closed_ts);i=times.get(k-TF)
            if i is None:continue
            if not (formed<k<=end):continue
            candidates.append((lv,le,i,cls[0],cls[1],str(ce.event_type)))
            recorded.add((lid,k,cls[0]))
        # Pre-break attack and approach only when a new 30m close advances from
        # a previous bar OUTSIDE the band toward that band's relevant side.
        start=max(1,int(np.searchsorted(frame.timestamp.to_numpy(),formed)))
        for i in range(start,len(frame)):
            k=int(frame.timestamp.iloc[i])+TF
            if not formed<k<=end:continue
            if contiguous[i]<20:continue
            pre=float(frame.close.iloc[i-1]);now=float(frame.close.iloc[i])
            atr=_atr(frame,i-1)
            if atr is None:continue
            b=frame.iloc[i]
            stage=None;direction=None
            if origin=='HIGH' and pre<lo and now<=hi:
                if float(b.high)>=lo:
                    stage='RESISTANCE_ATTACK_BELOW'
                elif (lo-pre)>0.5*atr and (0<=lo-now<=0.5*atr):
                    stage='RESISTANCE_APPROACH_BELOW'
                direction='LONG'
            elif origin=='LOW' and pre>hi and now>=lo:
                if float(b.low)<=hi:
                    stage='SUPPORT_ATTACK_ABOVE'
                elif (pre-hi)>0.5*atr and (0<=now-hi<=0.5*atr):
                    stage='SUPPORT_APPROACH_ABOVE'
                direction='SHORT'
            if stage and (lid,k,stage) not in recorded:
                candidates.append((lv,le,i,stage,direction,'DERIVED_APPROACH_OR_ATTACK'))
                recorded.add((lid,k,stage))
    result=[]
    for lv,le,i,stage,side,source_event in candidates:
        k=int(frame.timestamp.iloc[i])+TF
        if i<1:continue
        before=float(frame.close.iloc[i-1]);now=float(frame.close.iloc[i]);b=frame.iloc[i]
        atr=_atr(frame,i-1) if contiguous[i]>=20 else None
        v=pre_volume(frame,i) if contiguous[i]>=20 else pre_volume(frame,0)
        counters=prior_counters(le,k)
        # Tests from ABOVE are directional retests for a LOW/support; otherwise BELOW.
        if str(lv.origin)=='LOW':
            past=le[le.observed_closed_ts<k]
            counters['prior_retests_directional']=int((past.event_type=='TEST_FROM_ABOVE').sum())
        close_rel=relation(now,float(lv.zone_low),float(lv.zone_high))
        pre_rel=relation(before,float(lv.zone_low),float(lv.zone_high))
        v_atr=atr if atr is not None and atr>0 else np.nan
        dist_pre=(max(float(lv.zone_low)-before,0) if str(lv.origin)=='HIGH'
                  else max(before-float(lv.zone_high),0))
        dist_now=(max(float(lv.zone_low)-now,0) if str(lv.origin)=='HIGH'
                  else max(now-float(lv.zone_high),0))
        bar_rng=float(b.high)-float(b.low)
        result.append(dict(event_id=digest(VERSION,lv.level_id,k,stage),symbol=symbol,
           level_id=str(lv.level_id),origin=str(lv.origin),stage=stage,direction=side,
           known_closed_ts=k,known_utc=ts_iso(k),bar_open_ts=int(b.timestamp),
           center=float(lv.center),zone_low=float(lv.zone_low),zone_high=float(lv.zone_high),
           zone_width_pct=100*(float(lv.zone_high)-float(lv.zone_low))/float(lv.center),
           zone_quality_flag=str(lv.get('quality_flag','UNKNOWN_QUALITY')),formation_closed_ts=int(lv.formation_closed_ts),
           level_age_bars=int((k-int(lv.formation_closed_ts))//TF),**counters,
           prev_relation=pre_rel,close_relation=close_rel,previous_close=before,close=now,
           high=float(b.high),low=float(b.low),
           distance_pre_atr=dist_pre/v_atr,distance_now_atr=dist_now/v_atr,
           atr_pre=v_atr,**v,body_atr=abs(now-float(b.open))/v_atr,
           range_atr=bar_rng/v_atr,bar_close_position=(now-float(b.low))/bar_rng if bar_rng>0 else np.nan,
           feature_state='COMPLETE_PRE' if contiguous[i]>=20 and np.isfinite(v['vol_ratio']) else 'UNKNOWN_PRE',
           event_source=source_event,cluster_symbol_closed=f'{symbol}|{k}',
           source_30m=str(lv.source_30m)))
    result.sort(key=lambda r:(r['known_closed_ts'],r['level_id'],r['stage']))
    assert len({x['event_id'] for x in result})==len(result)
    return result


def simulate_minute_path(minute, signal, horizon, tp, sl, fee):
    """Conservative 1m delay: entry at minute OPEN one minute AFTER 30m signal close.

    If both TP and SL touch in the same 1m bar, report AMBIGUOUS, never TP.
    Gaps and missing coverage are censored, never SL or TIME_EXIT.
    """
    start=int(signal['known_closed_ts'])+M
    entry={'event_id':signal['event_id'],'symbol':signal['symbol'],'stage':signal['stage'],
           'direction':signal['direction'],'known_closed_ts':signal['known_closed_ts'],
           'entry_ts':start,'entry_price':'','horizon_min':horizon,'tp_pct':tp,
           'sl_pct':sl,'fee_roundtrip_pct':fee,'path_state':'','outcome':'',
           'return_gross_pct':'','return_after_fee_pct':'','mfe_pct':'','mae_pct':'','bars_available':0}
    if minute.empty:
        entry['path_state']='NO_1M_ARCHIVE';return entry
    t=minute.timestamp.to_numpy(dtype='int64')
    ix=int(np.searchsorted(t,start))
    if ix>=len(t) or int(t[ix])!=start:
        entry['path_state']='NO_ENTRY_1M';return entry
    path=minute.iloc[ix:ix+horizon]
    n=len(path)
    entry['bars_available']=n
    if n<horizon:
        entry['path_state']='CENSORED_1M_TAIL';return entry
    if np.any(np.diff(path.timestamp.to_numpy(dtype='int64'))!=M):
        entry['path_state']='GAP_1M';return entry
    p=float(path.open.iloc[0]);long=signal['direction']=='LONG'
    upper=p*(1+tp/100) if long else p*(1+sl/100)
    lower=p*(1-sl/100) if long else p*(1-tp/100)
    mf=float(path.high.max()/p-1)*100 if long else float(1-path.low.min()/p)*100
    ma=float(1-path.low.min()/p)*100 if long else float(path.high.max()/p-1)*100
    outcome='TIME_EXIT';gross=100*((float(path.close.iloc[-1])/p-1) if long else (1-float(path.close.iloc[-1])/p))
    for b in path.itertuples():
        hit_high=float(b.high)>=upper;hit_low=float(b.low)<=lower
        if hit_high and hit_low:
            outcome='BOTH_SAME_1M';gross=None;break
        if hit_high or hit_low:
            win=(hit_high if long else hit_low)
            outcome='TP_FIRST' if win else 'SL_FIRST'
            gross=tp if win else -sl
            break
    entry.update(entry_price=p,path_state='COMPLETE_1M',outcome=outcome,
                 return_gross_pct='' if gross is None else gross,
                 return_after_fee_pct='' if gross is None else gross-fee,
                 mfe_pct=mf,mae_pct=ma)
    return entry


def bins(s):
    if pd.isna(s):return 'UNKNOWN'
    s=float(s)
    if s<0.8:return '<0.8'
    if s<1.2:return '0.8–1.2'
    if s<2:return '1.2–2'
    return '>=2'


def distance_bin(x):
    if pd.isna(x):return 'UNKNOWN'
    x=float(x)
    if x<=0:return 'AT_OR_INSIDE_ZONE'
    if x<=0.5:return '0–0.5 ATR'
    if x<=1:return '0.5–1 ATR'
    return '>1 ATR'


def range_bin(x):
    if pd.isna(x):return 'UNKNOWN'
    x=float(x)
    if x<0.75:return '<0.75 ATR'
    if x<1.5:return '0.75–1.5 ATR'
    return '>=1.5 ATR'


def age_bin(x):
    if pd.isna(x):return 'UNKNOWN'
    x=float(x)
    if x<12:return '<12 bars'
    if x<48:return '12–47 bars'
    return '48+ bars'


def count_bin(x):
    try:x=int(x)
    except (ValueError,TypeError):return 'UNKNOWN'
    return '0' if x==0 else '1' if x==1 else '2+'


def make_comparison(signals, outcomes):
    x=signals.copy()
    if x.empty:return []
    x['vol_bin']=x.vol_ratio.map(bins)
    x['distance_pre_atr_bin']=x.distance_pre_atr.map(distance_bin)
    x['range_atr_bin']=x.range_atr.map(range_bin)
    x['level_age_bin']=x.level_age_bars.map(age_bin)
    for f in ('prior_cross_up','prior_tests','prior_retests_directional','prior_failed_cross_up'):
        x[f+'_bin']=x[f].map(count_bin)
    rows=[]
    # Volume and prior attempts are DESCRIPTIVE strata, not tuned filters.
    for factor in ('ALL','zone_quality_flag','vol_bin','distance_pre_atr_bin','range_atr_bin','level_age_bin',
                   'prior_cross_up_bin','prior_tests_bin','prior_retests_directional_bin','prior_failed_cross_up_bin'):
        group_cols=['origin','stage','direction']+([] if factor=='ALL' else [factor])
        for keys,g in x.groupby(group_cols,dropna=False):
            keys=(keys,) if isinstance(keys,str) else tuple(keys)
            base=dict(origin=keys[0],stage=keys[1],direction=keys[2],factor=factor,
                      factor_value='ALL' if factor=='ALL' else str(keys[-1]),
                      n_events=len(g),n_symbols=g.symbol.nunique(),
                      n_symbol_days=g.assign(day=pd.to_datetime(g.known_closed_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%d')).groupby(['symbol','day']).ngroups,
                      n_unique_levels=g.level_id.nunique(),
                      median_volume_ratio=pd.to_numeric(g.vol_ratio,errors='coerce').median(),
                      median_dist_pre_atr=pd.to_numeric(g.distance_pre_atr,errors='coerce').median())
            if outcomes.empty:
                rows.append({**base,'horizon_min':'','tp_pct':'','n_complete_1m':0,'n_tp':0,
                             'n_sl':0,'n_ambiguous':0,'n_time':0,'n_censored':len(g),
                             'tp_pct_full_denominator':'','tp_pct_resolved':'',
                             'median_mfe_pct':'','median_mae_pct':'','median_net_pct':''})
                continue
            o=outcomes[outcomes.event_id.isin(g.event_id)]
            for (h,tp),z in o.groupby(['horizon_min','tp_pct']):
                complete=z[z.path_state=='COMPLETE_1M']
                resol=complete[complete.outcome.isin(['TP_FIRST','SL_FIRST'])]
                rows.append({**base,'horizon_min':h,'tp_pct':tp,
                    'n_complete_1m':len(complete),'n_tp':int((complete.outcome=='TP_FIRST').sum()),
                    'n_sl':int((complete.outcome=='SL_FIRST').sum()),
                    'n_ambiguous':int((complete.outcome=='BOTH_SAME_1M').sum()),
                    'n_time':int((complete.outcome=='TIME_EXIT').sum()),
                    'n_censored':len(g)-len(complete),
                    'tp_pct_full_denominator':100*(complete.outcome=='TP_FIRST').mean() if len(complete) else np.nan,
                    'tp_pct_resolved':100*(resol.outcome=='TP_FIRST').mean() if len(resol) else np.nan,
                    'median_mfe_pct':pd.to_numeric(complete.mfe_pct,errors='coerce').median(),
                    'median_mae_pct':pd.to_numeric(complete.mae_pct,errors='coerce').median(),
                    'median_net_pct':pd.to_numeric(complete.return_after_fee_pct,errors='coerce').median()})
    return rows


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--scan',action='store_true')
    mode.add_argument('--evaluate',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--atlas-run',type=Path)
    p.add_argument('--run-dir',type=Path)
    p.add_argument('--run-tag',default='')
    p.add_argument('--eval-tag',default='')
    p.add_argument('--minute-dir',type=Path)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--horizons',default='60,180,360')
    p.add_argument('--tps',default='1,2,3')
    p.add_argument('--sl',type=float,default=1.5)
    p.add_argument('--fee',type=float,default=0.10)
    p.add_argument('--min-free-gb',type=float,default=0.5)
    return p.parse_args(argv)


def main(argv=None):
    a=cli(argv);root=a.root.resolve()
    if a.plan or a.scan:
        if not a.atlas_run:raise SystemExit('--atlas-run required')
        src=(root/a.atlas_run).resolve();manifest_file=src/'run_config.json'
        if not manifest_file.is_file():raise SystemExit(f'No frozen atlas run: {src}')
        manifest=json.loads(manifest_file.read_text())
        if not str(manifest.get('version','')).startswith('horizontal-level-lifecycle-v2.1'):
            raise SystemExit('Requires frozen V2.1 atlas run')
        syms=manifest['symbols'][:a.max_symbols] if a.max_symbols>0 else manifest['symbols']
        print('HLC_V22_PLAN',json.dumps(dict(source=str(src),symbols=len(syms),
              frozen_source=manifest['version'],volume='current closed 30m vs PRE20',
              scope='All CROSS_UP/DOWN plus attack and approach; NO outcome selection')),
              flush=True)
        if a.plan:return 0
        if not a.run_tag or not all(c.isalnum() or c in '._-' for c in a.run_tag):
            raise SystemExit('Supply --run-tag [A-Za-z0-9_.-]')
        output=root/DEST/a.run_tag
        if output.exists():raise SystemExit('RUN_TAG_EXISTS: never overwrite research freezes')
        if shutil.disk_usage(root).free/1024**3<a.min_free_gb:raise SystemExit('Insufficient disk space')
        candles_dir=Path(manifest['candles_dir']) if manifest.get('candles_dir') else None
        audit_hashes={};stage_counts=Counter();errors=[];total_signals=0
        # Stream each symbol to disk. Do not retain 290 symbols of signals in RAM.
        output.mkdir(parents=True,exist_ok=False)
        partial=output/'signals.csv.partial'
        with open(partial,'w',newline='',encoding='utf-8') as fp:
            writer=csv.DictWriter(fp,fieldnames=SIGNALS,extrasaction='ignore')
            writer.writeheader()
            with Archive(root,candles_dir) as arc:
                for n,sym in enumerate(syms,1):
                    files={name:src/'shards'/f'{sym}.{name}.csv' for name in ('formation','events','ends')}
                    try:
                        for p in files.values():
                            if not p.is_file():raise FileNotFoundError(p)
                        audit_hashes[sym]={name:sha_file(p) for name,p in files.items()}
                        fs=pd.read_csv(files['formation']);es=pd.read_csv(files['events']);en=pd.read_csv(files['ends'])
                        bars,source=load_30m(arc,sym)
                        r=causal_signals_for_symbol(sym,bars,fs,es,en)
                        writer.writerows(r)
                        total_signals+=len(r)
                        stage_counts.update(x['stage'] for x in r)
                        print('HLC_V22_PROGRESS',json.dumps(dict(symbol=sym,index=n,total=len(syms),events=len(r),
                              crosses=sum('CROSS_' in x['stage'] for x in r),
                              attacks=sum('ATTACK_' in x['stage'] for x in r))),flush=True)
                    except Exception as exc:
                        errors.append(dict(symbol=sym,error=f'{type(exc).__name__}: {exc}'))
                        print('HLC_V22_ERROR',json.dumps(errors[-1]),flush=True)
            fp.flush();os.fsync(fp.fileno())
        if errors:
            (output/'errors.json').write_text(json.dumps(errors,indent=2))
            raise SystemExit('SOURCE_ERRORS: partial run, no research freeze published; '+json.dumps(errors[:5]))
        os.replace(partial,output/'signals.csv')
        snapshot=dict(version=VERSION,atlas_run=str(src),atlas_manifest_sha256=sha_file(manifest_file),
                      atlas_files_sha256=audit_hashes,symbols=syms,
                      known_time_semantics='30m CLOSED bar only; factors PRE20 and current closed',
                      statuses=dict(stage_counts))
        snapshot['signals_sha256']=sha_file(output/'signals.csv')
        save_json(output/'freeze.json',snapshot)
        print('HLC_V22_RESULT',json.dumps(dict(run_dir=str(output),n_signals=total_signals,
              n_symbols=len(syms),stages=snapshot['statuses'],source_errors=0)),flush=True)
        return 0
    if a.evaluate:
        if not a.run_dir:raise SystemExit('--run-dir required')
        run=(root/a.run_dir).resolve();freeze=run/'freeze.json'
        if not freeze.is_file():raise SystemExit('No frozen V2.2 scan')
        cfg=json.loads(freeze.read_text());signals_path=run/'signals.csv'
        if sha_file(signals_path)!=cfg['signals_sha256']:raise SystemExit('FROZEN_SIGNALS_HASH_CHANGED')
        if not a.eval_tag or not all(c.isalnum() or c in '._-' for c in a.eval_tag):
            raise SystemExit('Supply --eval-tag')
        output=run/'evaluations'/a.eval_tag
        if output.exists():raise SystemExit('EVAL_TAG_EXISTS')
        h=sorted({int(x) for x in a.horizons.split(',')});tps=sorted({float(x) for x in a.tps.split(',')})
        if min(h)<=0 or max(h)>2880 or min(tps)<=0 or a.sl<=0 or a.fee<0:raise SystemExit('Invalid risk parameters')
        ss=pd.read_csv(signals_path)
        if a.max_symbols > 0 and not ss.empty:
            subset=sorted(ss.symbol.unique())[:a.max_symbols]
            ss=ss[ss.symbol.isin(subset)].copy()
        if ss.empty:
            raise SystemExit('No signals eligible for outcome evaluation')
        minute_dir=a.minute_dir
        if minute_dir and not minute_dir.is_absolute():minute_dir=(root/minute_dir).resolve()
        # A FILE_CSV atlas may have no SQLite candle archive at all. Use the
        # same candle directory and mark 1m MISSING rather than crashing.
        if minute_dir is None:
            atlas_manifest=json.loads((Path(cfg['atlas_run'])/'run_config.json').read_text())
            if atlas_manifest.get('candles_dir'):
                minute_dir=Path(atlas_manifest['candles_dir'])
        rows=[];sources=Counter();sym_status=[]
        with Archive(root,minute_dir) as arc:
            for sym,group in ss.groupby('symbol',sort=True):
                one,source=arc._bars(sym,'1m');sources[source]+=1
                for sig in group.to_dict('records'):
                    for hor in h:
                        for tp in tps:
                            rows.append(simulate_minute_path(one,sig,hor,tp,a.sl,a.fee))
                sym_status.append(dict(symbol=sym,source=source,bars1m=len(one)))
                print('HLC_V22_PATH',json.dumps(dict(symbol=sym,bars1m=len(one),events=len(group))),flush=True)
        save_csv(output/'outcomes.csv',rows,OUTCOMES)
        save_csv(output/'minute_coverage.csv',sym_status,('symbol','source','bars1m'))
        joined=make_comparison(ss,pd.DataFrame(rows))
        cols=(list(joined[0]) if joined else ['stage','n_events'])
        save_csv(output/'comparison.csv',joined,cols)
        audit=dict(version=VERSION,freeze=str(run),freeze_sha256=sha_file(freeze),
            signals_sha256=cfg['signals_sha256'],outcomes=len(rows),
            n_events=len(ss),evaluated_symbol_count=ss.symbol.nunique(),
            max_symbols_requested=a.max_symbols,n_complete_1m=sum(r['path_state']=='COMPLETE_1M' for r in rows),
            path_states=dict(Counter(r['path_state'] for r in rows)),
            source_count=dict(sources),horizons=h,tps=tps,sl_pct=a.sl,fee_roundtrip_pct=a.fee,
            caveat='No funding, slippage or overlapping portfolio positions; multiple levels and timestamps are dependent')
        save_json(output/'audit.json',audit)
        print('HLC_V22_EVALUATED',json.dumps({**audit,'output':str(output)}),flush=True)
        return 0


if __name__=='__main__':
    raise SystemExit(main())
