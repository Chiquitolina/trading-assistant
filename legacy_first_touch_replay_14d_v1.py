#!/usr/bin/env python3
"""Offline, resumable 14-day Legacy first-touch replay, 15m/3x3.

All three first-touch states are captured. Existing Legacy's classification and
Fail Touch Analyzer V1.2 are imported, not rewritten. LIVE, Redis and scanner DB
are never modified. Network is used ONLY in the explicit --download mode.

Usage: --plan / --download / --run / --report / --audit.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import traceback

from pre_reaction_opportunity_lab import (
    ONE, FIFTEEN, iso_ms, utc, load_native_1m, contiguous_15m_from_1m, sha,
)
from pre_reaction_1m_backfill_v0 import (
    read_symbol as read_backfill, write_symbol as write_backfill,
    download_symbol, check_disk, target_files,
)
from reaction_compare_swing_detectors_v1 import (
    find_variant_opportunities, assert_full_minute_coverage,
)
from dashboard.views.legacy_fail_touch_analyzer_view import (
    attach_fail_touch_metrics, _get_entry_outcome, _outcome_from_first_hits, DEPTH_LEVELS, MODES,
    TP_LEVELS, SL_LEVELS, HORIZONS,
)

VERSION='legacy-first-touch-historical-replay-v1.0'
DEFAULT_START='2026-09-17T00:00:00Z'
DEFAULT_END='2026-10-01T00:00:00Z'  # 14 complete UTC days
DEFAULT_ORIGINAL=Path('reports/candidate_long_horizon/candle_cache/1m')
DEFAULT_UNIVERSE=Path('reports/candidate_long_horizon/candle_cache/15m')
DEFAULT_BASE=Path('reports/reaction_lab/legacy_first_touch_historical_v1')
THREE=('REACTION','TOUCH_INDECISIVE','TOUCH_FAILED')
OTHER=('ARMED_NO_RETEST','NO_DEPARTURE','UNKNOWN_GAP')
EVENT_COLUMNS=('event_key','symbol','side','detector','status','pivot_ts','actionable_ts',
    'departure_ts','first_touch_ts','pivot_price','departure_price','retest_open',
    'retest_high','retest_low','retest_close','touch_penetration_pct',
    'touch_wick_pct_range','canonical_check','data_origin')
OPPORTUNITY_COLUMNS=('opportunity_key','symbol','side','pivot_ts','actionable_ts',
    'pivot_price','status','departure_ts','first_touch_ts','canonical_check')
GEOMETRY_COLUMNS=('event_key','symbol','side','first_touch_ts','pivot_ts',
    'ftr_touch_penetration_pct','ftr_touch_wick_share','ftr_touch_body_share',
    'ftr_fail_close_depth_pct','ftr_max_depth_180m_pct','ftr_complete_180m',
    'ftr_observed_minutes','ftr_reclaim_min','ftr_minutes_to_max_depth_180m')
ENTRY_COLUMNS=('event_key','symbol','side','first_touch_ts','depth_pct','mode',
    'triggered','signal_ts','entry_ts','entry_price','entry_failure_status',
    'tp2_sl1_180_status','tp2_sl1_180_bars','barrier_first_hits_json')
FILES=('first_touches.csv','all_actionable_opportunities.csv',
       'failed_geometry.csv','failed_entries.csv')


def write_csv(path,rows,columns):
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(encoding='utf-8',newline='') as f: return list(csv.DictReader(f))


def jsonable(value):
    if isinstance(value,dict):return {str(k):jsonable(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [jsonable(x) for x in value]
    if isinstance(value,float) and not math.isfinite(value):return None
    if hasattr(value,'item'):return jsonable(value.item())
    return value


def make_paths(root,base,start,end):
    base = base if base.is_absolute() else root/base
    tag=datetime.fromtimestamp(start/1000,timezone.utc).strftime('%Y%m%d')+'_'+datetime.fromtimestamp(end/1000,timezone.utc).strftime('%Y%m%d')
    folder=base/('run_'+tag)
    return folder,folder/'native_1m_PARTIAL',folder/'results'


def normalized_symbols(root,args):
    if args.symbols:
        vals=[x.strip().upper() for x in args.symbols.split(',') if x.strip()]
    elif args.symbols_file:
        file=Path(args.symbols_file)
        if not file.is_absolute():file=root/file
        vals=[]
        for line in file.read_text(encoding='utf-8').splitlines():
            line=line.split('#',1)[0].strip().upper()
            if line:vals.append(line)
    else:
        d=root/DEFAULT_UNIVERSE
        if not d.is_dir():
            raise FileNotFoundError(f'Cannot locate symbol universe: {d}. Use --symbols-file')
        vals=[p.name.upper() for p in d.iterdir() if p.is_dir()]
    if not vals:raise ValueError('Empty symbol universe')
    if len(vals)!=len(set(vals)):raise ValueError('Duplicate symbols in universe')
    # Binance USD-M also lists symbols with CJK/Han characters. Keep those
    # original symbols while rejecting path separators, punctuation and traversal.
    # Preserve the existing ASCII validation for all other symbols.
    if any(re.fullmatch(r'[A-Z0-9\u3400-\u9FFF]{2,40}',s) is None for s in vals):
        raise ValueError('Invalid symbol in universe')
    vals=sorted(vals)
    if not args.symbols and not args.symbols_file and len(vals)!=290:
        raise RuntimeError(f'Detected {len(vals)} symbols (not 290) in {root/DEFAULT_UNIVERSE}; supply explicit --symbols-file to freeze your 290-symbol universe')
    return vals


def bounds(start,end):
    if end<=start or (end-start)%ONE or start%ONE or end%ONE:
        raise ValueError('Dates must be aligned full UTC minutes and start < end')
    if end-start>31*24*60*ONE:raise ValueError('Maximum 31 days per replay')
    # 3h swing warmup; max 360m to retest, 360m to a late FTR entry,
    # and 360m after entry: all three horizons can be observed fully.
    return start-180*ONE,end+1081*ONE


def source_info(root,original,cache,sym,a,b,read=False):
    """Reuse ORIGINAL monthly cache only when it has exact continuous coverage.

    Partial historical monthly pickle data never passes as a complete source.
    """
    original=original if original.is_absolute() else root/original
    try:
        native,paths=load_native_1m(root,original if original.is_absolute() else root/original,sym,a,b)
        assert_full_minute_coverage(native,a,b,sym)
        return ('ORIGINAL_FULL',native if read else None,paths)
    except (FileNotFoundError,ValueError,RuntimeError,KeyError,IndexError):
        pass
    manifest=cache/'manifests'/f'{sym}.json'
    if manifest.is_file():
        if read:
            frame,m=read_backfill(cache,sym,a,b)
            return ('ISOLATED_VERIFIED',frame,target_files(cache,sym,a,b))
        try:
            frame,m=read_backfill(cache,sym,a,b)
            return ('ISOLATED_VERIFIED',None,target_files(cache,sym,a,b))
        except (FileNotFoundError,ValueError,RuntimeError,KeyError):
            return ('INVALID_ISOLATED',None,[])
    return ('MISSING',None,[])


def lock(folder):
    folder.mkdir(parents=True,exist_ok=True)
    f=(folder/'.replay.lock').open('a+')
    try:fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        f.close();raise RuntimeError('Another replay or download is already running')
    return f


def forward_cache(one):
    """Build Legacy's arrays and no-gap segment ends from chronological native 1m."""
    import numpy as np
    ts=one.timestamp.astype('int64').to_numpy()
    ends=np.empty(len(ts),dtype='int64')
    j=len(ts)-1
    if j>=0:
        ends[j]=j
        for i in range(j-1,-1,-1):
            ends[i]=ends[i+1] if ts[i+1]-ts[i]==ONE else i
    return dict(timestamps=ts,opens=one.open.to_numpy(),highs=one.high.to_numpy(),
        lows=one.low.to_numpy(),closes=one.close.to_numpy(),segment_end=ends)


def canonical_check(opportunity,one,canonical_find):
    """Call actual Legacy first-touch function, even for no-touch opportunities."""
    action=int(opportunity['actionable_ts']);end=action+360*ONE
    # A copied 361-minute window is all Legacy requires; no future before action.
    work=one.loc[(one.timestamp>=action)&(one.timestamp<=end),
        ['timestamp','open','high','low','close','volume']]
    p={
        'pivot_timestamp':int(opportunity['pivot_ts']),
        'actionable_timestamp':action,
        'pivot_price':float(opportunity['pivot_price']),
        'signal':opportunity['side'], 'timeframe':'15m','detector':'3x3',
        'entry_price':float(opportunity['pivot_price']),
    }
    can=canonical_find(one_minute=work,swing_row=p,retest_tolerance_pct=0.10,
        min_departure_pct=0.20,max_age_minutes=360)
    touched=opportunity['status'] in THREE
    if can is None and not touched:return 'NO_TOUCH_BOTH' ,None
    if can is None and touched: return 'MISSING_CANONICAL',None
    if can is not None and not touched:return 'PILOT_MISSED_CANONICAL',can
    if int(can['retest_timestamp'])!=int(opportunity['first_touch_ts']):return 'TS_MISMATCH',can
    if can['status']!=opportunity['status']:return 'STATUS_MISMATCH',can
    return 'EXACT_TOUCH',can


def replay_symbol(root,original,cache,out,symbol,start,end,a,b,app,scanner):
    import pandas as pd
    source,one,paths=source_info(root,original,cache,symbol,a,b,read=True)
    if one is None:raise RuntimeError('Insufficient continuous native 1m for '+symbol)
    assert_full_minute_coverage(one,a,b,symbol)
    fifteen=contiguous_15m_from_1m(one)
    # compile AST once per symbol, never import dashboard.app.py.
    from reaction_lab_continuous_scanner import compile_canonical_scanner
    def loader(symbol,timeframe,limit):
        return (one if timeframe=='1m' else fifteen).tail(int(limit)).copy()
    scan,ast_fingerprint=compile_canonical_scanner(app,loader)
    exact_find=scan.__globals__['_find_confirmed_swing_retest']
    opportunities=find_variant_opportunities(symbol,one,fifteen,start,end,3)
    events=[];geometry=[];entry_rows=[];out_opps=[];parity=Counter()
    ahead=forward_cache(one)
    for opp in opportunities:
        category,legacy=canonical_check(opp,one,exact_find)
        parity[category]+=1
        if category not in ('EXACT_TOUCH','NO_TOUCH_BOTH'):
            raise RuntimeError('Canonical classification mismatch at '+opp['opportunity_key']+': '+category)
        opp_key=opp['opportunity_key']
        out_opps.append(dict(opportunity_key=opp_key,symbol=symbol,side=opp['side'],
            pivot_ts=opp['pivot_ts'],actionable_ts=opp['actionable_ts'],
            pivot_price=opp['pivot_price'],status=opp['status'],
            departure_ts=opp['departure_ts'],first_touch_ts=opp['first_touch_ts'],
            canonical_check=category))
        if not legacy:continue
        event_key='|'.join((symbol,'15m','3x3',str(opp['side']),str(opp['pivot_ts']),str(opp['actionable_ts']),str(opp['first_touch_ts'])))
        c=dict(legacy);c['symbol']=symbol
        result=dict(event_key=event_key,symbol=symbol,side=opp['side'],detector='3x3',
            status=c['status'],pivot_ts=opp['pivot_ts'],actionable_ts=opp['actionable_ts'],
            departure_ts=c['departure_timestamp'],first_touch_ts=c['retest_timestamp'],
            pivot_price=c['swing_price'],departure_price=c['departure_price'],
            retest_open=c['retest_open'],retest_high=c['retest_high'],
            retest_low=c['retest_low'],retest_close=c['retest_close'],
            canonical_check=category,data_origin=source)
        if c['status']=='TOUCH_FAILED':
            enriched=attach_fail_touch_metrics(c,ahead)
            result['touch_penetration_pct']=enriched.get('ftr_touch_penetration_pct')
            result['touch_wick_pct_range']=enriched.get('ftr_touch_wick_share')
            geo={k:enriched.get(k) for k in GEOMETRY_COLUMNS if k.startswith('ftr_')}
            geo.update(event_key=event_key,symbol=symbol,side=opp['side'],
                first_touch_ts=c['retest_timestamp'],pivot_ts=opp['pivot_ts'])
            geometry.append(geo)
            for detail in enriched.get('ftr_grid',{}).values():
                status,bars=_get_entry_outcome(detail,2.0,1.0,180)
                entry_rows.append(dict(event_key=event_key,symbol=symbol,side=opp['side'],
                    first_touch_ts=c['retest_timestamp'],depth_pct=detail['depth_pct'],
                    mode=detail['mode'],triggered=detail['triggered'],
                    signal_ts=detail['signal_ts'],entry_ts=detail['entry_ts'],
                    entry_price=detail['entry_price'],entry_failure_status=detail.get('entry_failure_status'),
                    tp2_sl1_180_status=status,tp2_sl1_180_bars=bars,
                    barrier_first_hits_json=json.dumps(jsonable(detail.get('barrier_first_hits')),separators=(',',':'))))
        events.append(result)
    if len({x['event_key'] for x in events})!=len(events):raise RuntimeError('Duplicate first-touch event keys')
    # Separate files for each symbol ensure interrupted runs resume safely.
    path=out/symbol
    if path.exists():raise RuntimeError('Output for symbol exists; never overwrite '+str(path))
    path.mkdir(parents=True)
    rows=[(events,EVENT_COLUMNS),(out_opps,OPPORTUNITY_COLUMNS),
        (geometry,GEOMETRY_COLUMNS),(entry_rows,ENTRY_COLUMNS)]
    try:
        for fn,(data,cols) in zip(FILES,rows):write_csv(path/fn,data,cols)
        report={'version':VERSION,'symbol':symbol,'start':utc(start),'end_exclusive':utc(end),
            'data_start':utc(a),'data_end_exclusive':utc(b),'data_source':source,
            'source_files_sha256':{str(p):sha(p) for p in paths},
            'source_candles':len(one),'15m_complete_bars':len(fifteen),
            'app_sha256':sha(app),'scanner_sha256':sha(scanner),
            'canonical_AST_fingerprint':ast_fingerprint,
            'status_counts':dict(Counter(r['status'] for r in out_opps)),
            'parity':dict(parity),'first_touches':len(events),'failed_geometry':len(geometry),
            'entry_rows':len(entry_rows),
            'hashes':{f:sha(path/f) for f in FILES}}
        (path/'manifest.json').write_text(json.dumps(jsonable(report),indent=2),encoding='utf-8')
        return report
    except Exception:
        # Intentionally leave partial folder for operator inspection: no false DONE.
        raise


def completed(out,symbol):
    return (out/symbol/'manifest.json').is_file()


def verify_symbol(root,out,symbol,app,scanner,a,b):
    folder=out/symbol
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if m['version']!=VERSION or m['symbol']!=symbol or m['data_start']!=utc(a) or m['data_end_exclusive']!=utc(b):
        raise RuntimeError('Manifest identity or window mismatch: '+symbol)
    if m['app_sha256']!=sha(app) or m['scanner_sha256']!=sha(scanner):
        raise RuntimeError('Scanner or dashboard changed since replay: '+symbol)
    for path,digest in m['source_files_sha256'].items():
        if sha(Path(path))!=digest:raise RuntimeError('Source changed: '+path)
    for fn,digest in m['hashes'].items():
        if sha(folder/fn)!=digest:raise RuntimeError('Corrupt output: '+str(folder/fn))
    ev=read_csv(folder/FILES[0]);op=read_csv(folder/FILES[1]);geos=read_csv(folder/FILES[2]);grid=read_csv(folder/FILES[3]);
    if len(ev)!=m['first_touches'] or len(geos)!=m['failed_geometry'] or len(grid)!=m['entry_rows']:
        raise RuntimeError('Row count mismatch: '+symbol)
    if len({r['event_key'] for r in ev})!=len(ev):raise RuntimeError('Event ID collision '+symbol)
    if any(r['status'] not in THREE for r in ev):raise RuntimeError('Invalid status '+symbol)
    if dict(Counter(r['status'] for r in op))!=m['status_counts']:
        raise RuntimeError('Status summary mismatch: '+symbol)
    if any(r['canonical_check']!='EXACT_TOUCH' for r in ev):
        raise RuntimeError('Canonical parity failure: '+symbol)
    if any(int(e['entry_ts'])<=int(e['signal_ts']) or int(e['entry_ts'])-int(e['signal_ts'])!=ONE
           for e in grid if e['entry_ts'] and e['signal_ts']):
        raise RuntimeError('Non-causal entry: '+symbol)
    return m


def report_all(folder,out,symbols,depth=0.5,mode='DEPTH_NEXT_OPEN',horizon=180):
    """Offline matrix using compact first-hit timings (not only selected winners)."""
    if depth not in DEPTH_LEVELS or mode not in MODES or horizon not in HORIZONS:
        raise ValueError('Unsupported matrix depth, mode or horizon')
    completed_symbols=[];totals=Counter();parity=Counter();failures=0;entries=Counter()
    grid=defaultdict(Counter)
    selected_key=f'{depth:.1f}'
    for s in symbols:
        if not completed(out,s):continue
        m=json.loads((out/s/'manifest.json').read_text())
        completed_symbols.append(s)
        totals.update(m['status_counts']);parity.update(m['parity']);failures+=m['failed_geometry']
        for row in read_csv(out/s/'failed_entries.csv'):
            if row['depth_pct']!=selected_key or row['mode']!=mode:continue
            if depth==0.5 and mode=='DEPTH_NEXT_OPEN' and horizon==180:
                entries[row['tp2_sl1_180_status']]+=1
            hits=json.loads(row['barrier_first_hits_json']) if row.get('barrier_first_hits_json') not in (None,'','null') else None
            for side in ('ALL',row['side']):
                for tp in TP_LEVELS:
                    for sl in SL_LEVELS:
                        cat=grid[(side,tp,sl)]
                        if hits is not None:
                            status,_=_outcome_from_first_hits(hits,tp,sl,horizon)
                        else:
                            status=row['entry_failure_status'] or 'DATA_UNAVAILABLE'
                        cat[status]+=1
    matrix=[]
    for side in ('ALL','LONG','SHORT'):
        for tp in TP_LEVELS:
            for sl in SL_LEVELS:
                c=grid[(side,tp,sl)]
                total=sum(c.values())
                resolved=c['TP_FIRST']+c['SL_FIRST']+c['SL_AMBIGUOUS']
                activated=c['TP_FIRST']+c['SL_FIRST']+c['SL_AMBIGUOUS']+c['NO_HIT']+c['CENSORED']
                matrix.append({'side':side,'depth_pct':depth,'mode':mode,'horizon_min':horizon,
                    'tp_pct':tp,'sl_pct':sl,'N_failed':total,'N_activated':activated,
                    'TP_FIRST':c['TP_FIRST'],'SL_FIRST':c['SL_FIRST'],
                    'SL_AMBIGUOUS':c['SL_AMBIGUOUS'],'NO_HIT':c['NO_HIT'],
                    'CENSORED':c['CENSORED'],
                    'NO_TRIGGER':c['NO_TRIGGER'],'TRIGGER_CENSORED':c['TRIGGER_CENSORED'],
                    'ENTRY_CENSORED':c['ENTRY_CENSORED'],'DATA_UNAVAILABLE':c['DATA_UNAVAILABLE'],
                    'resolved':resolved,
                    'TP_rate_resolved_pct':round(c['TP_FIRST']/resolved*100,3) if resolved else '',
                    'gross_resolved_pct':round((c['TP_FIRST']*tp-(c['SL_FIRST']+c['SL_AMBIGUOUS'])*sl)/resolved,4) if resolved else ''})
    folder.mkdir(parents=True,exist_ok=True)
    matrix_name=f'tp_sl_matrix_depth_{depth:g}_{mode}_{horizon}m.csv'
    write_csv(folder/matrix_name,matrix,list(matrix[0]))
    summary={'version':VERSION,'symbols_requested':len(symbols),'symbols_complete':len(completed_symbols),
        'symbols_missing':[s for s in symbols if s not in completed_symbols],
        'status_counts':dict(totals),'parity':dict(parity),
        'failed_events':failures,'tp2_sl1_180_depth0_5_next_open':dict(entries),
        'matrix_file':str(folder/matrix_name),'matrix_depth':depth,
        'matrix_mode':mode,'matrix_horizon':horizon,
        'outcomes_do_not_include_fees':True,'all_periods_causal_first_touch':True}
    (folder/'progress_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print('LEGACY_FIRST_TOUCH_REPLAY_RESULT',json.dumps(summary,sort_keys=True),flush=True)
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    for flag in ('plan','download','run','report','audit'):g.add_argument('--'+flag,action='store_true')
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--start',default=DEFAULT_START)
    p.add_argument('--end',default=DEFAULT_END)
    p.add_argument('--symbols',default='')
    p.add_argument('--symbols-file',type=Path)
    p.add_argument('--original-cache',type=Path,default=DEFAULT_ORIGINAL)
    p.add_argument('--base',type=Path,default=DEFAULT_BASE)
    p.add_argument('--batch-size',type=int,default=10)
    p.add_argument('--min-free-gb',type=float,default=4.0)
    p.add_argument('--matrix-depth',type=float,default=0.5)
    p.add_argument('--matrix-mode',choices=MODES,default='DEPTH_NEXT_OPEN')
    p.add_argument('--matrix-horizon',type=int,choices=HORIZONS,default=180)
    args=p.parse_args(argv)
    root=args.project.resolve();start=iso_ms(args.start);end=iso_ms(args.end)
    a,b=bounds(start,end)
    symbols=normalized_symbols(root,args)
    folder,cache,out=make_paths(root,args.base,start,end)
    app=root/'dashboard/app.py';scanner=root/'reaction_lab_continuous_scanner.py'
    if args.batch_size<1:raise ValueError('--batch-size must be >=1')
    if args.plan:
        available=Counter(); missing=[]
        for s in symbols:
            source,_,_=source_info(root,args.original_cache,cache,s,a,b,read=False)
            available[source]+=1
            if source not in ('ORIGINAL_FULL','ISOLATED_VERIFIED'):missing.append(s)
        plan={'version':VERSION,'symbols':len(symbols),'symbols_first':symbols[:10],
            'start':utc(start),'end_exclusive':utc(end),
            'required_1m_start':utc(a),'required_1m_end_exclusive':utc(b),
            'bars_per_symbol':(b-a)//ONE,'source_status_counts':dict(available),
            'symbols_need_backfill':len(missing),'examples_missing':missing[:10],
            'symbols_completed':sum(completed(out,s) for s in symbols),
            'cache':str(cache),'output':str(out),'network':False,
            'read_only':True,'disk_gb_free':check_disk(cache,0),
            'legacy_detector':'15m/3x3', 'nontrading':True}
        print('LEGACY_FIRST_TOUCH_REPLAY_PLAN',json.dumps(plan,indent=2));return
    if args.audit:
        ok=[];bad=[]
        for s in symbols:
            if not completed(out,s):continue
            try:verify_symbol(root,out,s,app,scanner,a,b);ok.append(s)
            except Exception as e:bad.append({'symbol':s,'error':str(e)})
        payload={'verified_symbols':len(ok),'requested_symbols':len(symbols),
            'missing_symbols':[s for s in symbols if s not in ok],
            'issues':bad,'success':not bad and len(ok)==len(symbols),'partial':len(ok)<len(symbols)}
        print('LEGACY_FIRST_TOUCH_REPLAY_AUDIT',json.dumps(payload,sort_keys=True))
        if bad:raise RuntimeError('Replay audit had corrupted symbols')
        return
    if args.report:
        report_all(folder,out,symbols,args.matrix_depth,args.matrix_mode,args.matrix_horizon);return
    if not app.is_file() or not scanner.is_file():raise FileNotFoundError('Missing canonical scanner code')
    with lock(folder):
        remaining=args.batch_size
        for s in symbols:
            if remaining<=0:break
            if args.download:
                origin,_,_=source_info(root,args.original_cache,cache,s,a,b,read=False)
                if origin in ('ORIGINAL_FULL','ISOLATED_VERIFIED'):continue
                if origin=='INVALID_ISOLATED':
                    raise RuntimeError(f'Invalid isolated source for {s}; refusing overwrite')
                check_disk(cache,args.min_free_gb)
                print('LEGACY_FIRST_TOUCH_DOWNLOADING',s,'bars',(b-a)//ONE,flush=True)
                try:
                    bars=download_symbol(s,a,b)
                    check_disk(cache,args.min_free_gb)
                    result=write_backfill(cache,s,a,b,bars)
                    print('LEGACY_FIRST_TOUCH_DOWNLOADED',json.dumps(result,sort_keys=True),flush=True)
                    remaining-=1
                except Exception as exc:
                    print('LEGACY_FIRST_TOUCH_DOWNLOAD_ERROR',json.dumps({'symbol':s,'error':str(exc)}),flush=True)
                    if 'rate limit' in str(exc).lower() or 'http 418' in str(exc).lower() or 'http 429' in str(exc).lower():
                        raise RuntimeError('Binance rate limit detected; STOP backfill and retry later') from exc
            else:
                if completed(out,s):
                    verify_symbol(root,out,s,app,scanner,a,b)
                    continue
                origin,_,_=source_info(root,args.original_cache,cache,s,a,b,read=False)
                if origin not in ('ORIGINAL_FULL','ISOLATED_VERIFIED'):
                    print('LEGACY_FIRST_TOUCH_NEEDS_HISTORY',s,origin,flush=True);continue
                check_disk(out,args.min_free_gb)
                try:
                    info=replay_symbol(root,args.original_cache,cache,out,s,start,end,a,b,app,scanner)
                    remaining-=1
                    print('LEGACY_FIRST_TOUCH_SYMBOL',json.dumps({'symbol':s,
                        'opportunities':sum(info['status_counts'].values()),
                        'first_touches':info['first_touches'],'status_counts':info['status_counts'],
                        'failed':info['failed_geometry'],'canonical_parity':info['parity']},sort_keys=True),flush=True)
                except Exception as exc:
                    print('LEGACY_FIRST_TOUCH_RUN_ERROR',json.dumps({'symbol':s,'error':str(exc)}),flush=True)
                    if (out/s).exists():
                        print('LEGACY_FIRST_TOUCH_MANUAL_INSPECTION',str(out/s),flush=True)
        if args.run:report_all(folder,out,symbols,args.matrix_depth,args.matrix_mode,args.matrix_horizon)


if __name__=='__main__':main()
