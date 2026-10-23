#!/usr/bin/env python3
"""Independent, read-only-source study of causal recovery after Legacy TOUCH_FAILED depth >=2%.

Inputs: VERIFIED historical replay outputs and their original 1m OHLCV cache.
Outputs: versioned isolated CSV/JSON under reports/reaction_lab/.../causal_depth2_v1.
No exchange API requests, LIVE writes, dashboard edits, or ML fitting.

Causal clock: the first CLOSED 1m candle whose wick reaches >=2% against the pivot,
within 180m after the original failed first touch. Checkpoints are +0/+5/+15m.
At each checkpoint, all features use only candles through its close. New entries
are possible only at the next consecutive 1m OPEN. A checkpoint is INELIGIBLE
if its path has already reclaimed the pivot by close or extended >=3% by wick.
Structural future outcome: first pivot reclaim on a CLOSED 1m candle versus first
additional excursion >=3% from pivot (wick), over next 180 consecutive 1m bars.
A same-candle collision is AMBIGUOUS. Trade benchmark: TP2/SL1.5, 180m from
next OPEN, intrabar both-hit = SL_AMBIGUOUS; timeout marked at final close.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys

VERSION='failed-touch-causal-depth2-v1.0'
DEPTH=2.0
EXTEND=3.0
MAX_DEPTH_WAIT=180
CHECKPOINTS=(0,5,15)
HORIZON=180
PILOT=frozenset(('ADAUSDT','1000PEPEUSDT','1000BONKUSDT'))
EVENT_FIELDS=('event_key','symbol','side','first_touch_ts','pivot_price','first_depth2_ts',
              'depth2_delay_min','depth2_status','cluster_key')
FEATURE_FIELDS=('event_key','symbol','side','first_touch_ts','pivot_price','cluster_key',
    'checkpoint_min','depth2_delay_min','signal_ts','entry_ts','entry_price',
    'checkpoint_status','close_depth_pct','max_depth_so_far_pct','favorable_wick_share',
    'close_location_favorable','favorable_return_5m_pct','favorable_return_15m_pct',
    'favorable_candles_5m','volume_last5_over_prior30','range_5m_pct',
    'original_touch_penetration_pct','structural_outcome','structural_bars',
    'trade_outcome','trade_bars','trade_gross_pct')


def read_csv(path):
    import csv
    with path.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))


def atomic_csv(path, rows, fields):
    import csv
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    os.replace(tmp,path)


def atomic_json(path, obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    os.replace(tmp,path)


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for buf in iter(lambda:f.read(1<<20),b''):h.update(buf)
    return h.hexdigest()


def fnum(x):
    try:
        val=float(x)
        return val if math.isfinite(val) else None
    except (ValueError,TypeError):return None


def as_output(x):return round(x,7) if x is not None and math.isfinite(float(x)) else ''


def adverse_depth(side,pivot,high,low):
    return max(0.,(pivot-low)*100/pivot) if side=='LONG' else max(0.,(high-pivot)*100/pivot)


def recovered(side,pivot,close):
    return close>=pivot if side=='LONG' else close<=pivot


def first_depth_hit(frame,start,side,pivot,max_wait=MAX_DEPTH_WAIT):
    """First wick threshold across the initial failed candle and next 180m inclusive."""
    end=min(len(frame)-1,start+max_wait)
    for i in range(start,end+1):
        r=frame.iloc[i]
        if adverse_depth(side,pivot,float(r.high),float(r.low))>=DEPTH-1e-9:
            return i
    return None


def past_state(frame,hit,snapshot,side,pivot):
    """Evaluate *already known* path through snapshot, including the hit candle."""
    saw_reclaim=False;saw_extension=False
    for i in range(hit,snapshot+1):
        r=frame.iloc[i]
        saw_reclaim |= recovered(side,pivot,float(r.close))
        saw_extension |= adverse_depth(side,pivot,float(r.high),float(r.low))>=EXTEND-1e-9
    if saw_reclaim and saw_extension:return 'BOTH_ALREADY_OCCURRED'
    if saw_reclaim:return 'ALREADY_RECLAIMED'
    if saw_extension:return 'ALREADY_EXTENDED_3'
    return 'ACTIVE'


def make_features(frame,hit,snap,side,pivot):
    """Every field is computed from data indexed <= snap (checkpoint close)."""
    r=frame.iloc[snap];o,h,l,c=(float(r[k]) for k in ('open','high','low','close'))
    width=h-l
    wick=max(0.,min(o,c)-l) if side=='LONG' else max(0.,h-max(o,c))
    loc=((c-l)/width if side=='LONG' else (h-c)/width) if width>0 else None
    sign=1 if side=='LONG' else -1
    close_depth=max(0.,(pivot-c)*100/pivot) if side=='LONG' else max(0.,(c-pivot)*100/pivot)
    mx=max(adverse_depth(side,pivot,float(frame.iloc[i].high),float(frame.iloc[i].low))
           for i in range(hit,snap+1))
    def ret(steps):
        if snap<steps:return None
        prior=float(frame.iloc[snap-steps].close)
        return sign*(c/prior-1)*100 if prior>0 else None
    def mean_vol(a,b):
        if a<0 or b>len(frame) or a>=b:return None
        return statistics.mean(float(x) for x in frame.volume.iloc[a:b])
    last5=mean_vol(snap-4,snap+1)
    previous30=mean_vol(snap-34,snap-4)
    volume_ratio=last5/previous30 if previous30 and previous30>0 else None
    candles=None
    if snap>=4:
        candles=sum((float(frame.iloc[j].close)>=float(frame.iloc[j].open)) if side=='LONG'
                    else (float(frame.iloc[j].close)<=float(frame.iloc[j].open))
                    for j in range(snap-4,snap+1))
    last=frame.iloc[max(0,snap-4):snap+1]
    range5=(float(last.high.max())-float(last.low.min()))*100/pivot
    return dict(close_depth_pct=as_output(close_depth),max_depth_so_far_pct=as_output(mx),
        favorable_wick_share=as_output(wick/width if width>0 else None),
        close_location_favorable=as_output(loc),
        favorable_return_5m_pct=as_output(ret(5)),
        favorable_return_15m_pct=as_output(ret(15)),
        favorable_candles_5m=candles if candles is not None else '',
        volume_last5_over_prior30=as_output(volume_ratio),range_5m_pct=as_output(range5))


def structural_path(frame,entry_i,side,pivot,horizon=HORIZON):
    """Future label, NEVER included as an input feature."""
    if entry_i>=len(frame):return 'CENSORED',0
    end=min(len(frame)-1,entry_i+horizon-1)
    for i in range(entry_i,end+1):
        r=frame.iloc[i]
        good=recovered(side,pivot,float(r.close))
        bad=adverse_depth(side,pivot,float(r.high),float(r.low))>=EXTEND-1e-9
        if good and bad:return 'AMBIGUOUS',i-entry_i+1
        if good:return 'RECOVERY_FIRST',i-entry_i+1
        if bad:return 'CONTINUATION_FIRST',i-entry_i+1
    return ('NO_DECISION' if end-entry_i+1>=horizon else 'CENSORED'),end-entry_i+1


def trade_path(frame,entry_i,side,entry,horizon=HORIZON,tp=2.,sl=1.5):
    """Next-open executable benchmark; no fantasy fills at 2% wick threshold."""
    if entry_i>=len(frame):return 'CENSORED',0,None
    goal=entry*(1+tp/100) if side=='LONG' else entry*(1-tp/100)
    stop=entry*(1-sl/100) if side=='LONG' else entry*(1+sl/100)
    end=min(len(frame)-1,entry_i+horizon-1)
    for i in range(entry_i,end+1):
        r=frame.iloc[i];hi=float(r.high);lo=float(r.low)
        good=hi>=goal if side=='LONG' else lo<=goal
        bad=lo<=stop if side=='LONG' else hi>=stop
        if good and bad:return 'SL_AMBIGUOUS',i-entry_i+1,None
        if good:return 'TP_FIRST',i-entry_i+1,tp
        if bad:return 'SL_FIRST',i-entry_i+1,-sl
    count=end-entry_i+1
    if count<horizon:return 'CENSORED',count,None
    close=float(frame.iloc[end].close)
    outcome=(close/entry-1)*100*(1 if side=='LONG' else -1)
    return 'TIMEOUT',count,outcome


def analyze_events(frame,events):
    """Frame must have verified contiguous 1m coverage; event source is canonical replay."""
    import numpy as np
    ts=frame.timestamp.astype('int64').to_numpy()
    if len(ts)<2 or (np.diff(ts)!=60000).any():raise ValueError('Non-contiguous 1m; refusing to label')
    census=[];snapshots=[]
    for ev in events:
        if ev['status']!='TOUCH_FAILED':continue
        side=ev['side'];sym=ev['symbol'];pivot=float(ev['pivot_price'])
        touch=int(ev['first_touch_ts']);i0=int(np.searchsorted(ts,touch))
        if i0>=len(ts) or ts[i0]!=touch:raise RuntimeError('Missing failed first touch '+ev['event_key'])
        if ev.get('canonical_check')!='EXACT_TOUCH':raise RuntimeError('Non-canonical input '+ev['event_key'])
        if side not in ('LONG','SHORT') or pivot<=0:raise RuntimeError('Bad source geometry')
        cluster=f'{sym}|{side}|{touch}'
        hit=first_depth_hit(frame,i0,side,pivot)
        entry=dict(event_key=ev['event_key'],symbol=sym,side=side,first_touch_ts=touch,
                   pivot_price=pivot,first_depth2_ts=int(ts[hit]) if hit is not None else '',
                   depth2_delay_min=hit-i0 if hit is not None else '',
                   depth2_status='DEPTH_2_WITHIN_180' if hit is not None else 'NO_DEPTH_2_WITHIN_180',
                   cluster_key=cluster)
        census.append(entry)
        if hit is None:continue
        for delta in CHECKPOINTS:
            snap=hit+delta
            if snap>=len(frame):raise RuntimeError('No snapshot coverage; source incomplete')
            status=past_state(frame,hit,snap,side,pivot)
            raw=make_features(frame,hit,snap,side,pivot)
            row=dict(event_key=ev['event_key'],symbol=sym,side=side,first_touch_ts=touch,
                 pivot_price=pivot,cluster_key=cluster,checkpoint_min=delta,depth2_delay_min=hit-i0,
                 signal_ts=int(ts[snap]),entry_ts='',entry_price='',checkpoint_status=status,
                 original_touch_penetration_pct=ev.get('touch_penetration_pct',''),
                 structural_outcome='NOT_ELIGIBLE',structural_bars='',trade_outcome='NOT_ELIGIBLE',
                 trade_bars='',trade_gross_pct='',**raw)
            if status=='ACTIVE':
                e=snap+1
                if e>=len(frame) or int(ts[e])-int(ts[snap])!=60000:
                    row['checkpoint_status']='ENTRY_CENSORED'
                else:
                    price=float(frame.iloc[e].open)
                    row.update(entry_ts=int(ts[e]),entry_price=price)
                    label,bars=structural_path(frame,e,side,pivot)
                    trade,tbars,gross=trade_path(frame,e,side,price)
                    row.update(structural_outcome=label,structural_bars=bars,
                        trade_outcome=trade,trade_bars=tbars,trade_gross_pct=as_output(gross))
            snapshots.append(row)
    return census,snapshots


def wilson(num,den,z=1.96):
    if not den:return ('','')
    p=num/den;var=z*math.sqrt((p*(1-p)+z*z/(4*den))/den)/(1+z*z/den)
    mid=(p+z*z/(2*den))/(1+z*z/den)
    return round(100*(mid-var),2),round(100*(mid+var),2)


def bins(row):
    wait=int(row['depth2_delay_min'])
    yield 'time_to_2pct',('0-15m' if wait<=15 else '16-60m' if wait<=60 else '61-180m')
    wick=fnum(row['favorable_wick_share'])
    if wick is not None:yield 'favorable_wick_share',('>=50%' if wick>=0.5 else '<50%')
    rev=fnum(row['favorable_return_5m_pct'])
    if rev is not None:yield 'favorable_return_5m',('>0%' if rev>0 else '<=0%')
    close=fnum(row['close_location_favorable'])
    if close is not None:yield 'favorable_close_location',('>=0.65' if close>=0.65 else '<0.65')
    vol=fnum(row['volume_last5_over_prior30'])
    if vol is not None:yield 'volume_5m_over_prior30',('>=1.5x' if vol>=1.5 else '<1.5x')


def summarize(snapshots,scope,check,side,feature,bucket):
    rr=[r for r in snapshots if r['checkpoint_status']=='ACTIVE' and
        int(r['checkpoint_min'])==check and (side=='ALL' or r['side']==side) and
        (scope=='ALL33' or r['symbol'] not in PILOT) and
        (feature=='BASELINE' or dict(bins(r)).get(feature)==bucket)]
    if not rr:return None
    outcomes=Counter(r['structural_outcome'] for r in rr)
    trades=Counter(r['trade_outcome'] for r in rr)
    decided=outcomes['RECOVERY_FIRST']+outcomes['CONTINUATION_FIRST']
    wil_lo,wil_hi=wilson(outcomes['RECOVERY_FIRST'],decided)
    gross=[fnum(r['trade_gross_pct']) for r in rr if r['trade_outcome'] in ('TP_FIRST','SL_FIRST','TIMEOUT')]
    gross=[x for x in gross if x is not None]
    clusters=len({r['cluster_key'] for r in rr})
    return dict(scope=scope,checkpoint_min=check,side=side,feature=feature,bucket=bucket,
        n=len(rr),unique_touch_clusters=clusters,rec_first=outcomes['RECOVERY_FIRST'],
        extend_first=outcomes['CONTINUATION_FIRST'],ambiguous=outcomes['AMBIGUOUS'],
        no_decision=outcomes['NO_DECISION'],censored=outcomes['CENSORED'],
        recovery_rate_decided_pct=round(outcomes['RECOVERY_FIRST']*100/decided,3) if decided else '',
        recovery_wilson95_low_pct=wil_lo,recovery_wilson95_high_pct=wil_hi,
        tp=trades['TP_FIRST'],sl=trades['SL_FIRST'],sl_ambiguous=trades['SL_AMBIGUOUS'],
        timeout=trades['TIMEOUT'],trade_censored=trades['CENSORED'],
        trade_gross_mean_including_timeout_pct=round(statistics.mean(gross),5) if gross else '',
        trade_pnl_coverage=len(gross))


def summary_rows(rows):
    out=[]
    for scope in ('NEW30','ALL33'):
        for check in CHECKPOINTS:
            for side in ('ALL','LONG','SHORT'):
                combinations=[('BASELINE','ALL')]
                combinations.extend((feat,bucket) for feat in ('time_to_2pct',
                    'favorable_wick_share','favorable_return_5m','favorable_close_location',
                    'volume_5m_over_prior30') for bucket in (('0-15m','16-60m','61-180m') if feat=='time_to_2pct' else
                     ('>=50%','<50%') if feat=='favorable_wick_share' else
                     ('>0%','<=0%') if feat=='favorable_return_5m' else
                     ('>=0.65','<0.65') if feat=='favorable_close_location' else ('>=1.5x','<1.5x')))
                for feature,bucket in combinations:
                    r=summarize(rows,scope,check,side,feature,bucket)
                    if r is not None:out.append(r)
    return out


def selected_paths(root):
    folder=root/'reports/reaction_lab/legacy_first_touch_historical_v1/run_20260917_20261001'
    return folder,folder/'native_1m_PARTIAL',folder/'results',folder/'causal_depth2_v1'


def eligible_symbols(input_dir,symbols_str):
    all_symbols=sorted(p.name for p in input_dir.iterdir() if p.is_dir() and (p/'manifest.json').exists()) if input_dir.is_dir() else []
    if symbols_str:
        selected=[x.strip().upper() for x in symbols_str.split(',') if x.strip()]
        if len(selected)!=len(set(selected)):raise ValueError('Duplicate --symbols')
        if any(x not in all_symbols for x in selected):raise ValueError('Selected symbol has no completed original replay')
        return sorted(selected)
    return all_symbols


def verify_output(folder,sym,old_manifest_sha=None):
    d=folder/sym
    meta=json.loads((d/'manifest.json').read_text(encoding='utf-8'))
    if meta.get('version')!=VERSION or meta['symbol']!=sym:raise RuntimeError('Study version/identity mismatch '+sym)
    if old_manifest_sha and meta['input_manifest_sha256']!=old_manifest_sha:
        raise RuntimeError('Upstream replay changed '+sym)
    for name,digest in meta['hashes'].items():
        if sha(d/name)!=digest:raise RuntimeError('Study output hash changed '+sym+'/'+name)
    return meta


def process_one(root,cache,oldout,study,sym,replay,a,b):
    in_manifest=oldout/sym/'manifest.json'
    original_sha=sha(in_manifest)
    if (study/sym/'manifest.json').exists():
        meta=verify_output(study,sym,original_sha)
        return dict(symbol=sym,status='EXISTING_VERIFIED',**meta['counts'])
    app=root/'dashboard/app.py';scanner=root/'reaction_lab_continuous_scanner.py'
    replay.verify_symbol(root,oldout,sym,app,scanner,a,b)
    source,frame,_=replay.source_info(root,replay.DEFAULT_ORIGINAL,cache,sym,a,b,read=True)
    if frame is None:raise RuntimeError('Missing verified 1m for '+sym)
    input_rows=read_csv(oldout/sym/'first_touches.csv')
    census,features=analyze_events(frame,input_rows)
    if len(census)!=sum(r['status']=='TOUCH_FAILED' for r in input_rows):raise RuntimeError('Bad failed census '+sym)
    # Exact first threshold signal parity vs original V1.2 grid, including hits >180m.
    old_grids={r['event_key']:r for r in read_csv(oldout/sym/'failed_entries.csv')
               if r['depth_pct']=='2.0' and r['mode']=='DEPTH_NEXT_OPEN'}
    if len(old_grids)!=len(census):raise RuntimeError('Missing original 2% grid '+sym)
    for r in census:
        original=old_grids[r['event_key']]['signal_ts']
        found=r['first_depth2_ts']
        if found!='' and str(found)!=original:
            raise RuntimeError('First depth crossing differs from original replay '+r['event_key'])
        if found=='' and original and int(original)-int(r['first_touch_ts'])<=180*60000:
            raise RuntimeError('Missed a depth crossing from original replay '+r['event_key'])
    d=study/sym
    if d.exists():raise RuntimeError('Partial study directory exists, inspect before retry: '+str(d))
    d.mkdir(parents=True)
    atomic_csv(d/'event_census.csv',census,EVENT_FIELDS)
    atomic_csv(d/'snapshot_features_outcomes.csv',features,FEATURE_FIELDS)
    counts=dict(failed=len(census),reached_depth2=sum(r['first_depth2_ts']!='' for r in census),
                snapshots=len(features),active_snapshots=sum(r['checkpoint_status']=='ACTIVE' for r in features))
    meta=dict(version=VERSION,symbol=sym,input_manifest_sha256=original_sha,
        source=source,counts=counts,hashes={name:sha(d/name) for name in ('event_census.csv','snapshot_features_outcomes.csv')})
    atomic_json(d/'manifest.json',meta)
    return dict(symbol=sym,status='COMPLETE',**counts)


def report(study,symbols):
    census=[];features=[];complete=[]
    for sym in symbols:
        if not (study/sym/'manifest.json').exists():continue
        verify_output(study,sym)
        census+=read_csv(study/sym/'event_census.csv')
        features+=read_csv(study/sym/'snapshot_features_outcomes.csv')
        complete.append(sym)
    if not complete:raise RuntimeError('No study symbols complete')
    atomic_csv(study/'all_event_census.csv',census,EVENT_FIELDS)
    atomic_csv(study/'all_snapshot_features_outcomes.csv',features,FEATURE_FIELDS)
    grouped=summary_rows(features)
    if grouped:atomic_csv(study/'causal_group_summary.csv',grouped,list(grouped[0]))
    tally=Counter(r['depth2_status'] for r in census)
    stage=Counter((int(r['checkpoint_min']),r['checkpoint_status']) for r in features)
    base=[r for r in grouped if r['feature']=='BASELINE' and r['side']=='ALL']
    summ=dict(version=VERSION,scope='historical completed symbols only',
        symbols_complete=len(complete),symbols=complete,failed_events=len(census),
        depth2_within_180=tally['DEPTH_2_WITHIN_180'],
        not_reached_180=tally['NO_DEPTH_2_WITHIN_180'],
        snapshot_status_counts={f'{k[0]}m_{k[1]}':v for k,v in sorted(stage.items())},
        baseline=base,signals_causal_at_candle_close=True,entry_next_1m_open=True,
        future_labels_not_input_features=True,
        notes=['Pilot symbols excluded from NEW30 scope; cohort is frozen on first run','Results are not portfolio performance',
               'Same-symbol simultaneous first-touch levels may be correlated',
               'No fees/slippage; ambiguity excluded from gross averages',
               'Snapshots +5/+15 are conditional on no prior reclaim/extension',
               'No ranking/selection of best feature in this exploration'])
    atomic_json(study/'summary.json',summ)
    print('FAILED_TOUCH_CAUSAL_REPORT',json.dumps(dict(symbols_complete=len(complete),
        failed_events=len(census),depth2_within_180=tally['DEPTH_2_WITHIN_180'],
        active_t0=stage[(0,'ACTIVE')],active_t5=stage[(5,'ACTIVE')],
        active_t15=stage[(15,'ACTIVE')],output=str(study),
        baseline=base),ensure_ascii=False),flush=True)
    return summ


def frozen_universe(study, oldout, symbols_str='', create=False):
    """Lock the original completed 33-symbol development cohort on first --run.

    New historical replays are NOT silently included later. Explicit --symbols
    only narrows the frozen cohort, and is never allowed to expand it.
    """
    current=eligible_symbols(oldout,'')
    frozen_file=study/'frozen_development_universe.json'
    if frozen_file.exists():
        obj=json.loads(frozen_file.read_text(encoding='utf-8'))
        if obj.get('version')!=VERSION or not obj.get('symbols'):
            raise RuntimeError('Bad frozen development universe')
        universe=obj['symbols']
        if len(universe)!=len(set(universe)) or any(x not in current for x in universe):
            raise RuntimeError('Frozen symbols missing or duplicated in original replay')
    else:
        universe=current
        if create:
            if not universe:raise RuntimeError('Cannot freeze empty cohort')
            atomic_json(frozen_file,dict(version=VERSION,symbols=universe,
                pilot_symbols=sorted(PILOT),study='development',
                depth=DEPTH,checkpoints=CHECKPOINTS,horizon=HORIZON,
                statement='Frozen before inspecting results of this study'))
    if symbols_str:
        selected=[x.strip().upper() for x in symbols_str.split(',') if x.strip()]
        if len(selected)!=len(set(selected)) or any(x not in universe for x in selected):
            raise ValueError('Requested symbol not present in frozen study universe')
        return sorted(selected),universe,current
    return universe,universe,current


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    actions=p.add_mutually_exclusive_group(required=True)
    for flag in ('plan','run','report','audit'):actions.add_argument('--'+flag,action='store_true')
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--symbols',default='',help='Optional subset of completed original replay symbols')
    p.add_argument('--batch-size',type=int,default=33)
    p.add_argument('--min-free-gb',type=float,default=4.)
    args=p.parse_args(argv)
    if args.batch_size<1:raise ValueError('batch-size must be positive')
    root=args.project.resolve();folder,cache,oldout,study=selected_paths(root)
    symbols,universe,all_original=frozen_universe(study,oldout,args.symbols,create=args.run)
    completed=[s for s in symbols if (study/s/'manifest.json').exists()]
    pending=[s for s in symbols if s not in completed]
    if args.plan:
        from shutil import disk_usage
        print('FAILED_TOUCH_CAUSAL_PLAN',json.dumps(dict(version=VERSION,
            original_completed=len(all_original),development_universe_frozen=(study/'frozen_development_universe.json').exists(),
            development_universe_size=len(universe),additional_original_outside_development=len(all_original)-len(universe),
            study_completed=len(completed),
            study_pending=len(pending),symbols_first=pending[:15],
            output=str(study),input_cache=str(cache),
            free_gb=round(disk_usage(root).free/1024**3,2),
            mode='READ_ONLY_INPUT_OFFLINE',depth_pct=DEPTH,
            first_threshold_max_min=MAX_DEPTH_WAIT,checkpoints=CHECKPOINTS,
            future_horizon=HORIZON),ensure_ascii=False,indent=2));return
    if not symbols:raise RuntimeError('No completed legacy replay symbols to analyze')
    if args.audit:
        checked=[];issues=[]
        for s in symbols:
            if s not in completed:continue
            try:verify_output(study,s,sha(oldout/s/'manifest.json'));checked.append(s)
            except Exception as e:issues.append(dict(symbol=s,error=str(e)))
        print('FAILED_TOUCH_CAUSAL_AUDIT',json.dumps(dict(checked=len(checked),
            selected=len(symbols),pending=pending,issues=issues,success=len(checked)==len(symbols) and not issues),ensure_ascii=False))
        if issues:raise RuntimeError('Audit failed')
        return
    if args.run:
        from pre_reaction_1m_backfill_v0 import check_disk
        import legacy_first_touch_replay_14d_v1 as replay
        a,b=replay.bounds(replay.iso_ms(replay.DEFAULT_START),replay.iso_ms(replay.DEFAULT_END))
        done=0
        for s in symbols:
            if s in completed:continue
            if done>=args.batch_size:break
            check_disk(study,args.min_free_gb)
            info=process_one(root,cache,oldout,study,s,replay,a,b)
            done+=1
            print('FAILED_TOUCH_CAUSAL_SYMBOL',json.dumps(info,ensure_ascii=False),flush=True)
    report(study,symbols)

if __name__=='__main__':main()
