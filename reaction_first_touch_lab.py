#!/usr/bin/env python3
"""First-touch REACTION study, read-only Auto Paths + frozen Full Context.

No Binance, PM2, Redis, scanner writes or live execution. Paths are required to
be COMPLETE 360x1m contiguous; missing paths are not labeled as losses.
Horizon is always measured from retest, even for delayed observable entries.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

MIN = 60000
HORIZONS = (5,15,30,60,180)
SCENARIOS = ((0.5,0.5),(1.0,0.5),(1.0,1.0),(1.5,0.75),(2.0,1.0))
MODES = ('STRUCTURAL_OPEN','OBSERVED_OPEN')
REL = Path('reports/reaction_lab')
VERSION = 'reaction-first-touch-lab-v1.0'
INPUT_EVENT_COLS = ('event_key','symbol','side','retest_ts','known_ts','first_seen_ms',
                   'observation_type','source_hash','causal_json','quality')
EVENT_FIELDS = ('event_key','symbol','side','date_utc','retest_ts','known_ts','first_seen_ms',
                'observation_type','path_status','path_count','path_quality',
                'first_seen_delay_seconds','pre_retest_range15_pct','pre_retest_range30_pct',
                'pre_retest_volatility60_pct','pre_retest_trades5','pre_retest_quote5',
                'pre_retest_taker_share5','btc_pre_retest_return60_pct','asset_pre_retest_return60_pct',
                'pre_retest_side_adjusted_vs_btc60_pct')
ROW_FIELDS = ('event_key','symbol','side','date_utc','observation_type','entry_mode',
              'entry_ts','entry_price','entry_delay_min','tp_pct','sl_pct',
              *[f'result_{h}m' for h in HORIZONS],
              *[f'minute_{h}m' for h in HORIZONS],
              'first_touch_result_180m','first_touch_minute_180m',
              'mfe_180_pct','mae_180_pct')


def ro(path):
    if not path.is_file(): raise FileNotFoundError(str(path))
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=20)
    con.execute('PRAGMA query_only=ON')
    return con


def frozen(con):
    col_str=','.join(INPUT_EVENT_COLS)
    rows=[dict(zip(INPUT_EVENT_COLS,r)) for r in con.execute(
        f'SELECT {col_str} FROM events ORDER BY retest_ts,event_key')]
    if not rows: raise RuntimeError('Empty frozen full-context catalog')
    serial='\n'.join(json.dumps(e,sort_keys=True,ensure_ascii=False,separators=(',',':')) for e in rows)
    sha=hashlib.sha256(serial.encode('utf-8')).hexdigest()
    meta=dict(con.execute('SELECT key,value FROM meta'))
    if meta.get('cohort_sha256')!=sha or meta.get('event_count')!=str(len(rows)):
        raise RuntimeError('Full Context frozen cohort hash/count mismatch')
    return rows,sha


def require_full_context_audit(root,sha,events_count):
    path=root/REL/'reaction_full_context_v1'/'audit.json'
    if not path.is_file():
        raise RuntimeError(f'Full-context audit report missing: {path}. Export full context first.')
    report=json.loads(path.read_text(encoding='utf-8'))
    if (report.get('success') is not True or
        report.get('frozen_cohort_sha256')!=sha or
        report.get('events')!=events_count or
        report.get('ready')!=events_count or
        report.get('incomplete_windows_total')!=0 or
        report.get('conflicts')!=0 or
        report.get('aggregate',{}).get('unverified_mismatches_total')!=0):
        raise RuntimeError('Full-context audit is not valid for this frozen cohort')
    return report


def check_schema(con,table,fields):
    found={x[1] for x in con.execute(f'PRAGMA table_info({table})')}
    if not set(fields)<=found:
        raise RuntimeError(f'Unexpected {table} schema: missing {set(fields)-found}')


def path_index(con):
    check_schema(con,'events',('event_key','entry_ts','state','count','path_blob'))
    return {k:(ts, state, count) for k,ts,state,count in
            con.execute('SELECT event_key,entry_ts,state,count FROM events')}


def plan(root):
    with closing(ro(root/REL/'reaction_full_context_v1.sqlite3')) as ctx, closing(ro(root/REL/'auto_paths_v1.sqlite3')) as ap:
        events,sha=frozen(ctx)
        require_full_context_audit(root,sha,len(events))
        paths=path_index(ap)
        check_schema(ctx,'candles',('symbol','timeframe','open_ts','open','high','low','close',
                                   'quote_volume','trades','taker_buy_quote'))
        state_counts=Counter()
        for e in events:
            rec=paths.get(e['event_key'])
            state_counts['PATH_MISSING' if rec is None else str(rec[1])]+=1
        return {'version':VERSION,'frozen_events':len(events),'cohort_sha256':sha,
                'observations':dict(Counter(e['observation_type'] for e in events)),
                'path_states':dict(state_counts),'candidate_complete_360':sum(
                    rec is not None and rec[1]=='COMPLETE' and rec[2]==360
                    for e in events if (rec:=paths.get(e['event_key'])) is not None),
                'scenarios_per_eligible_event':len(SCENARIOS)*len(MODES),
                'horizons_min':list(HORIZONS),'read_only':True,'writes_or_network_calls':False}


def validate_path(bars,rec,e):
    if rec is None:return 'PATH_MISSING'
    ts,state,n=rec
    if state!='COMPLETE':return 'PATH_'+str(state)
    if int(n)!=360 or ts != e['retest_ts']+MIN or len(bars)!=360:
        return 'INVALID_LENGTH_OR_ENTRY'
    for i,r in enumerate(bars):
        t,o,h,l,c=r
        if t!=ts+i*MIN:return 'INVALID_CONTINUITY'
        if not all(math.isfinite(v) and v>0 for v in (o,h,l,c)) or h<max(o,l,c) or l>min(o,h,c):
            return 'INVALID_OHLC'
    return 'COMPLETE_VALID'


def observed_index(bars,e):
    if e['observation_type']!='LIVE_OBSERVED':return None
    ready=max(e['known_ts'],e['first_seen_ms'])
    for i,(ts,*_) in enumerate(bars[:180]):
        if ts>=ready:return i
    return None


def first_touch(bars,start_idx,horizon,side,entry,tp,sl):
    """Absolute horizon from retest; intrabar ties are not wins."""
    if start_idx is None or start_idx>=horizon:return 'NOT_ENTERED',None
    tpp=entry*(1+tp/100) if side=='LONG' else entry*(1-tp/100)
    slp=entry*(1-sl/100) if side=='LONG' else entry*(1+sl/100)
    for i in range(start_idx,horizon):
        _,o,hi,lo,_=bars[i]
        tp_hit=hi>=tpp if side=='LONG' else lo<=tpp
        sl_hit=lo<=slp if side=='LONG' else hi>=slp
        if tp_hit and sl_hit:return 'AMBIGUOUS',i+1
        if tp_hit:return 'TP_FIRST',i+1
        if sl_hit:return 'SL_FIRST',i+1
    return 'NO_HIT',None


def excursions(bars,start_idx,side,entry):
    if start_idx is None:return None,None
    used=bars[start_idx:180]
    if not used:return None,None
    hi=max(b[2] for b in used);lo=min(b[3] for b in used)
    if side=='LONG':return max(0,(hi/entry-1)*100),max(0,(1-lo/entry)*100)
    return max(0,(1-lo/entry)*100),max(0,(hi/entry-1)*100)


def get_pre_context(con,sym,anchor):
    # All windows end strictly before anchor. End exclusive even on minute boundary.
    end=(int(anchor)//MIN)*MIN
    rows=con.execute('''SELECT open_ts,high,low,close,quote_volume,trades,taker_buy_quote
       FROM candles WHERE symbol=? AND timeframe='1m' AND open_ts>=? AND open_ts<?
       ORDER BY open_ts''',(sym,end-180*MIN,end)).fetchall()
    if len(rows)!=180 or any(int(r[0])!=end-(180-i)*MIN for i,r in enumerate(rows)):
        return None
    def range_(n):
        a=rows[-n:]; return (max(x[1] for x in a)-min(x[2] for x in a))/a[-1][3]*100
    # 60 CLOSED candle-to-candle returns (61 closes).
    closes=[r[3] for r in rows[-61:]]
    vol=math.sqrt(sum(math.log(b/a)**2 for a,b in zip(closes,closes[1:]))) *100
    recent=rows[-5:]
    quote=sum(r[4] for r in recent)
    taker=sum(r[6] for r in recent)
    return {'range15':range_(15),'range30':range_(30),'vol60':vol,
            'trades5':sum(int(r[5]) for r in recent),'quote5':quote,
            'taker5':(taker/quote if quote>0 else None),
            'return60':(closes[-1]/closes[0]-1)*100}


def make_event_row(con,e):
    a=get_pre_context(con,e['symbol'],e['retest_ts'])
    btc=get_pre_context(con,'BTCUSDT',e['retest_ts'])
    direction=1 if e['side']=='LONG' else -1
    return {'event_key':e['event_key'],'symbol':e['symbol'],'side':e['side'],
            'date_utc':datetime.fromtimestamp(e['retest_ts']/1000,timezone.utc).date().isoformat(),
            'retest_ts':e['retest_ts'],'known_ts':e['known_ts'],'first_seen_ms':e['first_seen_ms'],
            'observation_type':e['observation_type'],
            'first_seen_delay_seconds':round((e['first_seen_ms']-e['known_ts'])/1000,3),
            'pre_retest_range15_pct':a['range15'] if a else None,
            'pre_retest_range30_pct':a['range30'] if a else None,
            'pre_retest_volatility60_pct':a['vol60'] if a else None,
            'pre_retest_trades5':a['trades5'] if a else None,
            'pre_retest_quote5':a['quote5'] if a else None,
            'pre_retest_taker_share5':a['taker5'] if a else None,
            'asset_pre_retest_return60_pct':a['return60'] if a else None,
            'btc_pre_retest_return60_pct':btc['return60'] if btc else None,
            'pre_retest_side_adjusted_vs_btc60_pct':
                direction*(a['return60']-btc['return60']) if a and btc else None}


def row_for(e,bars,mode,start_idx,tp,sl):
    entry=bars[start_idx][1] if start_idx is not None else None
    out={'event_key':e['event_key'],'symbol':e['symbol'],'side':e['side'],
         'date_utc':datetime.fromtimestamp(e['retest_ts']/1000,timezone.utc).date().isoformat(),
         'observation_type':e['observation_type'],'entry_mode':mode,
         'entry_ts':bars[start_idx][0] if start_idx is not None else None,
         'entry_price':entry,'entry_delay_min':start_idx if start_idx is not None else None,
         'tp_pct':tp,'sl_pct':sl}
    for h in HORIZONS:
        state,idx=first_touch(bars,start_idx,h,e['side'],entry,tp,sl)
        out[f'result_{h}m']=state
        out[f'minute_{h}m']=idx
    out['first_touch_result_180m']=out['result_180m']
    out['first_touch_minute_180m']=out['minute_180m']
    out['mfe_180_pct'],out['mae_180_pct']=excursions(bars,start_idx,e['side'],entry)
    return out


def write_csv_atomic(path,fields,rows):
    fd,tmp=tempfile.mkstemp(prefix='.first_touch_',dir=path.parent,suffix='.csv')
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def sha_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(2**20),b''):h.update(block)
    return h.hexdigest()


def run(root):
    from reaction_lab_auto_paths import decode_path
    outdir=root/REL/'reaction_first_touch_lab_v1'
    events_rows=[];outcomes=[];path_counts=Counter();coverage=Counter();groups=defaultdict(Counter)
    with closing(ro(root/REL/'reaction_full_context_v1.sqlite3')) as ctx, closing(ro(root/REL/'auto_paths_v1.sqlite3')) as ap:
        frozen_events,sha=frozen(ctx)
        qa=require_full_context_audit(root,sha,len(frozen_events))
        for e in frozen_events:
            eventrow=make_event_row(ctx,e)
            path=ap.execute('SELECT entry_ts,state,count,path_blob FROM events WHERE event_key=?',(e['event_key'],)).fetchone()
            rec=None if path is None else path[:3]
            if rec is None:quality='PATH_MISSING';bars=[]
            elif rec[1]!='COMPLETE':quality='PATH_'+str(rec[1]);bars=[]
            else:
                try:
                    bars=decode_path(path[3]);quality=validate_path(bars,rec,e)
                except (ValueError,TypeError,OverflowError,MemoryError,IndexError,UnicodeError) as exc:
                    bars=[];quality='DECODE_'+type(exc).__name__
            eventrow['path_status']='MISSING' if rec is None else str(rec[1])
            eventrow['path_count']=None if rec is None else rec[2]
            eventrow['path_quality']=quality
            events_rows.append(eventrow)
            path_counts[quality]+=1
            if quality!='COMPLETE_VALID':continue
            coverage[e['observation_type']]+=1
            for mode in MODES:
                idx=0 if mode=='STRUCTURAL_OPEN' else observed_index(bars,e)
                for tp,sl in SCENARIOS:
                    r=row_for(e,bars,mode,idx,tp,sl)
                    outcomes.append(r)
                    groups[(mode,tp,sl,e['observation_type'])][r['result_180m']]+=1
    outdir.mkdir(parents=True,exist_ok=True)
    events_csv=outdir/'events_coverage_features.csv'
    outcomes_csv=outdir/'first_touch_scenarios.csv'
    write_csv_atomic(events_csv,EVENT_FIELDS,events_rows)
    write_csv_atomic(outcomes_csv,ROW_FIELDS,outcomes)
    report={'version':VERSION,'frozen_cohort_sha256':sha,'source_events':len(events_rows),'upstream_audit_verified_exceptions':len(qa['aggregate'].get('verified_provider_exceptions',[])),
            'path_quality':dict(path_counts),'complete_by_observation':dict(coverage),
            'scenario_rows':len(outcomes),'scenarios':len(SCENARIOS),'modes':list(MODES),
            'horizons_from_retest_minutes':list(HORIZONS),
            'scenario_counts':[
              {'entry_mode':mode,'tp_pct':tp,'sl_pct':sl,'observation_type':obs,'counts':dict(count)}
              for (mode,tp,sl,obs),count in sorted(groups.items())],
            'file_sha256':{events_csv.name:sha_file(events_csv),outcomes_csv.name:sha_file(outcomes_csv)},
            'notes':[
              'All 7631 frozen scanner events remain in events_coverage_features.csv, including missing paths',
              'Only COMPLETE, valid, contiguous 360-minute paths yield scenarios',
              'STRUCTURAL_OPEN is an idealized historical first open, not executable live',
              'OBSERVED_OPEN only for LIVE_OBSERVED and open >= max(known_ts,first_seen_ms)',
              'All horizons measured from retest, not entry; delayed entries may be NOT_ENTERED',
              'AMBIGUOUS simultaneous TP/SL in same 1m candle is never a win',
              'NO_HIT at horizon is not classified as win/loss or marked-to-market PnL',
              '1m volatility proxy sqrt(sum log returns squared) in percent; no V1 threshold assumed identical',
              'No fills, fee, slippage or return assumptions: purely first-touch frequency audit',
              'Discovery research, NOT independent prospective validation']}
    (outdir/'summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return report


def audit(root):
    out=root/REL/'reaction_first_touch_lab_v1'
    meta=json.loads((out/'summary.json').read_text(encoding='utf-8'))
    with closing(ro(root/REL/'reaction_full_context_v1.sqlite3')) as c:
        ev,sha=frozen(c)
        require_full_context_audit(root,sha,len(ev))
    rows=[]
    for f,expected in meta['file_sha256'].items():
        actual=sha_file(out/f)
        rows.append({'file':f,'match':actual==expected})
    with (out/'events_coverage_features.csv').open(encoding='utf-8',newline='') as file:
        actual_rows=sum(1 for _ in csv.DictReader(file))
    with (out/'first_touch_scenarios.csv').open(encoding='utf-8',newline='') as file:
        actual_outcomes=sum(1 for _ in csv.DictReader(file))
    ok=(sha==meta['frozen_cohort_sha256'] and len(ev)==actual_rows==meta['source_events']
        and actual_outcomes==meta['scenario_rows'] and all(r['match'] for r in rows))
    return {'success':ok,'frozen_hash_matches':sha==meta['frozen_cohort_sha256'],
            'event_rows':actual_rows,'outcome_rows':actual_outcomes,'checksums':rows}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    modes=p.add_mutually_exclusive_group(required=True)
    for m in ('plan','run','audit'):modes.add_argument('--'+m,action='store_true')
    p.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    a=p.parse_args(argv);root=a.project.resolve()
    if a.plan:print('PLAN',json.dumps(plan(root),ensure_ascii=False))
    elif a.run:print('FIRST_TOUCH_DONE',json.dumps(run(root),ensure_ascii=False))
    else:print(json.dumps(audit(root),indent=2,ensure_ascii=False))

if __name__=='__main__':main()
