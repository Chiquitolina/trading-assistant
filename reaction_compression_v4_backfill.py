#!/usr/bin/env python3
"""Targeted native Binance USDM 15m/30m history for the V4 event selection.

Only --fetch makes network requests. Writes the SEPARATE V3/V4 research history
cache, never Full Context, Market Data, exchange state, or a live strategy.
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import math

import reaction_compression_lineage as v3
import reaction_compression_breakout_retest_v4 as v4
from reaction_compression_history_backfill import own_db, fetch_batches


PRIOR_HOURS=180  # At least longest box+separate base + 48h search + 12h margin


def requests_for_selected(root,max_events=100,cohort='live-stratified',search_hours=48):
    required_prior=max((3*n*v3.TF[tf]/(60*v3.M) for tf,ns in v3.WINDOWS.items() for n in ns), default=120)
    margin=max(PRIOR_HOURS,math.ceil(required_prior+search_hours+12))
    with closing(v3.ro(root/v3.ROOT_REL/v3.CTX_NAME)) as con:
        events=v4.select_events(con,cohort,max_events)
    groups=defaultdict(list)
    for e in events:
        groups[e['symbol']].append((e['retest_ts']-margin*60*v3.M,e['retest_ts']))
    merged={}
    for sym,intervals in sorted(groups.items()):
        out=[]
        for start,end in sorted(intervals):
            if out and start<=out[-1][1]+v3.TF['15m']:
                out[-1]=(out[-1][0],max(end,out[-1][1]))
            else:out.append((start,end))
        merged[sym]=out
    return events,merged,margin


def backfill(root,max_events=100,cohort='live-stratified',search_hours=48,pause=.45,max_symbols=0):
    import requests
    _,intervals,_=requests_for_selected(root,max_events,cohort,search_hours)
    symbols=sorted(intervals)
    if max_symbols:symbols=symbols[:max_symbols]
    path=root/v3.ROOT_REL/v3.HISTORY_NAME
    newbars=0;skipped=0;requests_sent=0
    with closing(own_db(path)) as con,requests.Session() as session:
        for idx,symbol in enumerate(symbols,1):
            for start,end in intervals[symbol]:
                for tf in ('15m','30m'):
                    step=v3.TF[tf]
                    a=(start//step)*step;b=(end//step)*step
                    expected=(b-a)//step+1
                    covered=con.execute('SELECT COUNT(*) FROM candles WHERE symbol=? AND timeframe=? AND open_ts BETWEEN ? AND ?',
                        (symbol,tf,a,b)).fetchone()[0]
                    if covered==expected:
                        skipped+=1
                        continue
                    for batch in fetch_batches(session,symbol,tf,start,end,pause):
                        requests_sent+=1
                        with con:
                            for row in batch:
                                before=con.execute('SELECT open,high,low,close,volume,quote_volume FROM candles '
                                    'WHERE symbol=? AND timeframe=? AND open_ts=?',row[:3]).fetchone()
                                if before is not None:
                                    if any(not math.isclose(x,y,abs_tol=1e-10,rel_tol=1e-9) for x,y in zip(before,row[3:])):
                                        raise RuntimeError(f'Conflict in research cache: {symbol} {tf} {row[2]}')
                                else:
                                    con.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',row)
                                    newbars+=1
            print('V4_BACKFILL_PROGRESS',json.dumps({'symbol':symbol,'done':idx,'total':len(symbols),'new_bars':newbars}),flush=True)
    return {'symbols_processed':len(symbols),'new_bars':newbars,
            'batch_requests':requests_sent,'cached_full_intervals_skipped':skipped,
            'cache':str(path),'frozen_archive_modified':False}


def plan(root,max_events,cohort,search_hours,max_symbols):
    events,intervals,prior=requests_for_selected(root,max_events,cohort,search_hours)
    symbols=sorted(intervals)
    if max_symbols:symbols=symbols[:max_symbols]
    times=[p for s in symbols for p in intervals[s]]
    return dict(selected_events=len(events),symbols=len(symbols),intervals=sum(len(intervals[s]) for s in symbols),
        first_symbols=symbols[:15],cohort=cohort,max_events=max_events,
        prior_hours=prior,search_hours=search_hours,
        earliest_start_utc=datetime.fromtimestamp(min((a for a,_ in times),default=0)/1000,timezone.utc).isoformat() if times else None,
        latest_end_utc=datetime.fromtimestamp(max((b for _,b in times),default=0)/1000,timezone.utc).isoformat() if times else None,
        cache=str(root/v3.ROOT_REL/v3.HISTORY_NAME),network_calls=False,writes=False,
        note='Matches exact V4 sample. Use the SAME --max-events/--cohort/--search-hours on both commands.')


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    mode=ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--fetch',action='store_true')
    ap.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    ap.add_argument('--max-events',type=int,default=100)
    ap.add_argument('--cohort',choices=['live-stratified','all-live','all-frozen'],default='live-stratified')
    ap.add_argument('--search-hours',type=int,default=48)
    ap.add_argument('--max-symbols',type=int,default=0,help='0 = ALL selected symbols')
    ap.add_argument('--pause',type=float,default=.45)
    args=ap.parse_args(argv)
    if args.max_events<0 or args.max_symbols<0 or args.pause<0 or args.search_hours<6 or args.search_hours>168:
        ap.error('Invalid arguments')
    root=args.root.resolve()
    obj=plan(root,args.max_events,args.cohort,args.search_hours,args.max_symbols) if args.plan else backfill(
        root,args.max_events,args.cohort,args.search_hours,args.pause,args.max_symbols)
    print(json.dumps(obj,indent=2,ensure_ascii=False))
if __name__=='__main__':main()
