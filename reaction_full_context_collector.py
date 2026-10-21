#!/usr/bin/env python3
"""Frozen, all-REACTION, causal historical context archive (research-only).

Reads scanner_events_v1.sqlite3 READ ONLY, freezes EVERY scanner event without
using outcome labels or Auto Paths; imports reusable audited 1m/15m OHLCV from
frozen V1/V2 research DBs, then fetches only missing Binance USD-M REST bars.

Never connects to Redis or execution. Never writes to scanner, Auto Paths,
V1, V2, or LIVE. Output: reports/reaction_lab/reaction_full_context_v1.sqlite3.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
import fcntl
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time

import runner_precursor_pilot as p

REL = Path('reports/reaction_lab')
DB = 'reaction_full_context_v1.sqlite3'
OUT = 'reaction_full_context_v1'
VERSION = 'reaction-full-context-v1.0'
COLUMNS = 'event_key,symbol,side,retest_ts,known_ts,first_seen_ms,observation_type,source_hash,causal_json'
BAR_COLS = 'open_ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base,taker_buy_quote'
SCHEMA = p.SCHEMA + '''
CREATE TABLE IF NOT EXISTS events (
 event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
 retest_ts INTEGER NOT NULL, known_ts INTEGER NOT NULL, first_seen_ms INTEGER NOT NULL,
 observation_type TEXT NOT NULL, source_hash TEXT NOT NULL, causal_json TEXT NOT NULL,
 quality TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_full_context_events_retest ON events(retest_ts);
CREATE TABLE IF NOT EXISTS import_log (
 source TEXT PRIMARY KEY, observed_rows INTEGER NOT NULL, inserted_rows INTEGER NOT NULL,
 conflicts INTEGER NOT NULL, completed_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS independently_verified_discrepancies (
 symbol TEXT NOT NULL, open_ts INTEGER NOT NULL,
 aggregated_json TEXT NOT NULL, direct_15m_json TEXT NOT NULL,
 one_min_response_sha256 TEXT NOT NULL, fifteen_min_response_sha256 TEXT NOT NULL,
 checked_ms INTEGER NOT NULL, method TEXT NOT NULL,
 PRIMARY KEY(symbol,open_ts)
);
'''


def source_events(root):
    with closing(p.ro(root/REL/'scanner_events_v1.sqlite3')) as con:
        data=con.execute('SELECT '+COLUMNS+' FROM events ORDER BY retest_ts,event_key').fetchall()
    return [dict(zip(COLUMNS.split(','),v)) for v in data]


def event_quality(e):
    sym=e['symbol']; side=e['side']; r=e['retest_ts']; k=e['known_ts']; seen=e['first_seen_ms']
    if not isinstance(sym,str) or not sym or side not in ('LONG','SHORT'):
        return 'INVALID_SIDE_OR_SYMBOL'
    if e['event_key']!=f'{sym}|{side}|{r}':return 'INVALID_EVENT_KEY'
    if not all(isinstance(t,int) and t>0 for t in (r,k,seen)):
        return 'INVALID_TIMESTAMPS'
    if r%p.MIN or k!=r+p.MIN or seen<k:
        return 'INVALID_CAUSAL_TIMING'
    if e['observation_type'] not in ('LIVE_OBSERVED','RECOVERED_WINDOW'):
        return 'INVALID_OBSERVATION_TYPE'
    return 'READY'


def normalize(events):
    return [dict(e,quality=event_quality(e)) for e in events]


def cohort_sha(events):
    # The raw causal_json and source_hash are frozen too: source provenance is immutable.
    canon='\n'.join(json.dumps(e,sort_keys=True,ensure_ascii=False,separators=(',',':'))
                    for e in events)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()


def own_connect(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=30)
    con.execute('PRAGMA busy_timeout=30000')
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    with con:
        con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('version',VERSION))
        version=con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        if version!=VERSION:raise RuntimeError(f'DB version mismatch: {version}')
    return con


def frozen_events(con):
    keys=[x.strip() for x in COLUMNS.split(',')]+['quality']
    rows=con.execute('SELECT '+COLUMNS+',quality FROM events ORDER BY retest_ts,event_key').fetchall()
    return [dict(zip(keys,r)) for r in rows]


def frozen_config(con):
    meta=dict(con.execute('SELECT key,value FROM meta'))
    events=frozen_events(con)
    if not events or meta.get('cohort_sha256')!=cohort_sha(events):
        raise RuntimeError('Frozen scanner cohort integrity FAILED: DB contents or SHA do not match')
    return events,int(meta['pre1']),int(meta['pre15']),meta


def freeze(con,root,pre1,pre15):
    if con.execute('SELECT COUNT(*) FROM events').fetchone()[0]:
        events,a,b,_=frozen_config(con)
        if (a,b)!=(pre1,pre15):
            raise RuntimeError(f'Frozen windows 1m={a} 15m={b}, requested {pre1}/{pre15}; use original parameters')
        return events
    events=normalize(source_events(root))
    if not events:raise RuntimeError('Scanner journal has no events to freeze')
    hash_=cohort_sha(events)
    with con:
        con.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',[
            tuple(e[k] for k in COLUMNS.split(','))+ (e['quality'],) for e in events])
        con.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',[
            ('cohort_sha256',hash_),('event_count',str(len(events))),
            ('pre1',str(pre1)),('pre15',str(pre15)),
            ('source','scanner_events_v1.sqlite3'),('frozen_ms',str(p.now_ms())),
            ('policy','ALL_SCANNER_EVENTS_NO_OUTCOME_SELECTION'),
            ('anchors','PRE_RETEST,PRE_KNOWN,PRE_OBSERVED'),
        ])
    return events


def spans_for(events,pre1,pre15):
    """Only CLOSED candles at THREE causal times; no future/outcomes."""
    spans=defaultdict(list)
    for e in events:
        if e['quality']!='READY':continue
        for sym in {e['symbol'],'BTCUSDT'}:
            for tf,n in (('1m',pre1),('15m',pre15)):
                step=p.TFS[tf]
                for anchor in (e['retest_ts'],e['known_ts'],e['first_seen_ms']):
                    end=(anchor//step)*step  # EXCLUSIVE; forming candle prohibited
                    spans[sym,tf].append((end-n*step,end-step))
    return {key:p.merge_ranges(rs,p.TFS[key[1]]) for key,rs in spans.items()}


def required_count(spans):
    return sum((b-a)//p.TFS[tf]+1 for (_,tf),parts in spans.items() for a,b in parts)


def request_estimate(spans):
    return sum(((b-a)//p.TFS[tf])//1000+1 for (_,tf),parts in spans.items() for a,b in parts)


def load_plan(root,db,pre1,pre15):
    if db.is_file():
        with closing(p.ro(db)) as con:
            events,a,b,meta=frozen_config(con)
        frozen=True
    else:
        events=normalize(source_events(root));a=pre1;b=pre15;frozen=False
    spans=spans_for(events,a,b)
    return {'frozen':frozen,'events':len(events),
            'eligible_context':sum(e['quality']=='READY' for e in events),
            'excluded_by_metadata':dict(Counter(e['quality'] for e in events if e['quality']!='READY')),
            'observation_types':dict(Counter(e['observation_type'] for e in events)),
            'days':len({p.day(e['retest_ts']) for e in events}),
            'symbols':len({e['symbol'] for e in events}),
            'anchors':['PRE_RETEST','PRE_KNOWN','PRE_OBSERVED'],
            'timeframes':['1m','15m'],'asset_and_btc':True,
            'pre1':a,'pre15':b,
            'unique_required_bars':required_count(spans),
            'approx_requests_before_cache_reuse':request_estimate(spans),
            'cohort_sha256':cohort_sha(events),
            'db':str(db),'writes_or_network_calls':False}


def field_match(a,b):
    # Strict price, exact trade count, explicit float tolerances for volume.
    return (p.equal(a[:4],b[:4],rel=1e-11,abs_=1e-11)
        and p.equal(a[4:6],b[4:6],rel=1e-9,abs_=1e-7)
        and a[6]==b[6]
        and p.equal(a[7:],b[7:],rel=1e-9,abs_=1e-7))


def add_bars(con,sym,tf,bars,source):
    new=bad=0;ts_now=p.now_ms()
    # Raw bar layout: (ts,o,h,l,c,vol,quote,trades,takerBase,takerQuote).
    with con:
        for bar in bars:
            ts=int(bar[0]);data=tuple(bar[1:])
            old=con.execute('''SELECT open,high,low,close,volume,quote_volume,trades,
                    taker_buy_base,taker_buy_quote FROM candles
                    WHERE symbol=? AND timeframe=? AND open_ts=?''',(sym,tf,ts)).fetchone()
            if old is not None:
                if not field_match(old,data):
                    bad+=1
                    con.execute('''INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,?,?)''',
                                (sym,tf,ts,json.dumps(old),json.dumps(data),ts_now))
            else:
                con.execute('''INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (sym,tf,ts,*data,source,ts_now));new+=1
    return new,bad


def reuse_caches(root,con,spans):
    """Read-only cache import; preserves source provenance and rejects mismatches."""
    allstats=Counter()
    for name in (p.DB_NAME,'runner_precursor_validation_v2.sqlite3'):
        file=root/REL/name
        if not file.is_file():continue
        marker=con.execute('SELECT 1 FROM import_log WHERE source=?',(name,)).fetchone()
        if marker:continue
        count=Counter()
        with closing(p.ro(file)) as existing:
            tables={r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'candles' not in tables:raise RuntimeError(f'Invalid reusable cache {file}')
            for (sym,tf),ranges in sorted(spans.items()):
                for a,b in ranges:
                    # Fetch aligned required bars only, never unneeded future bars.
                    cursor=existing.execute('''SELECT '''+BAR_COLS+''' FROM candles
                         WHERE symbol=? AND timeframe=? AND open_ts BETWEEN ? AND ?
                         ORDER BY open_ts''',(sym,tf,a,b))
                    while True:
                        batch=cursor.fetchmany(800)
                        if not batch:break
                        count['seen']+=len(batch)
                        new,bad=add_bars(con,sym,tf,batch,'CACHE:'+name)
                        count['inserted']+=new;count['conflicts']+=bad
                        if bad:raise RuntimeError(f'Cache discrepancy in {name} {sym} {tf}; inspect conflicts, not overwritten')
        with con:
            con.execute('INSERT INTO import_log VALUES (?,?,?,?,?)',
                 (name,count['seen'],count['inserted'],count['conflicts'],p.now_ms()))
        allstats.update(count)
        print('CACHE_IMPORTED',json.dumps({'source':name,**count},ensure_ascii=False),flush=True)
    return dict(allstats)


def download(root,db,pre1,pre15,max_requests=1500,pause=0.45,min_free_gb=3,
             fetch_fn=None, enable_cache=True):
    if fetch_fn is None:fetch_fn=p.fetch
    if shutil.disk_usage(root).free<min_free_gb*1024**3:
        raise RuntimeError('Free disk below safety limit')
    db.parent.mkdir(parents=True,exist_ok=True)
    lock=db.with_suffix('.lock')
    with lock.open('a+') as fh:
        try:fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Collector already running') from exc
        with closing(own_connect(db)) as con:
            events=freeze(con,root,pre1,pre15)
            spans=spans_for(events,pre1,pre15)
            if enable_cache:reuse_caches(root,con,spans)
            total=Counter();requests=0
            for (sym,tf),parts in sorted(spans.items()):
                step=p.TFS[tf]
                for a,b in parts:
                    existing=[t[0] for t in con.execute('''SELECT open_ts FROM candles
                         WHERE symbol=? AND timeframe=? AND open_ts BETWEEN ? AND ? ORDER BY open_ts''',
                         (sym,tf,a,b))]
                    for x,y in p.missing(a,b,existing,step):
                        for lo,hi in p.chunks(x,y,step):
                            if requests>=max_requests:
                                print('REQUEST_CAP_REACHED; resume by rerunning --run',json.dumps(dict(total)),flush=True)
                                return {'complete':False,**total}
                            requests+=1
                            try:
                                bars,sha=fetch_fn(sym,tf,lo,hi)
                                new,bad=p.ingest(con,sym,tf,lo,hi,bars,sha)
                                total['requests']+=1;total['new_bars']+=new;total['conflicts']+=bad
                                if bad:raise RuntimeError('conflicting historical bars; STOP')
                                if len(bars)!=(hi-lo)//step+1:total['partial_requests']+=1
                            except Exception as exc:
                                with con:
                                    con.execute('''INSERT INTO fetch_log(symbol,timeframe,begin_ts,end_ts,received,
                                         sha256,status,details,retrieved_ms) VALUES(?,?,?,?,?,?,?,?,?)''',
                                         (sym,tf,lo,hi,0,None,'ERROR',f'{type(exc).__name__}:{exc}'[:200],p.now_ms()))
                                raise RuntimeError(f'Collector stopped at {sym} {tf} {p.utc(lo)}; safely resumable') from exc
                            if requests%20==0:
                                print('PROGRESS',json.dumps(dict(total)), 'last=',sym,tf,p.utc(hi),flush=True)
                                if shutil.disk_usage(root).free<min_free_gb*1024**3:
                                    raise RuntimeError('Free disk below safety limit; safely resumable')
                            if pause:time.sleep(pause)
            print('DOWNLOAD_DONE',json.dumps(dict(total)),flush=True)
            return {'complete':True,**total}


def aggregate_audit(con,events,limit=25,collect_unverified=False):
    """Same strict OHLCV/trades/taker tolerance + exact pinned anomalies as V1/V2."""
    matched=0;verified=[];errors=[];unknown_total=0;pending=[]
    stored_proofs={(row[0],row[1]):row[2:] for row in con.execute(
        'SELECT symbol,open_ts,aggregated_json,direct_15m_json,one_min_response_sha256,fifteen_min_response_sha256,checked_ms,method FROM independently_verified_discrepancies')}

    syms={e['symbol'] for e in events if e['quality']=='READY'}|{'BTCUSDT'}
    for sym in sorted(syms):
        for ts,*vals in con.execute('''SELECT '''+BAR_COLS+''' FROM candles
                           WHERE symbol=? AND timeframe='15m' ORDER BY open_ts''',(sym,)):
            bars=con.execute('''SELECT '''+BAR_COLS+''' FROM candles
                    WHERE symbol=? AND timeframe='1m' AND open_ts>=? AND open_ts<?
                    ORDER BY open_ts''',(sym,ts,ts+15*p.MIN)).fetchall()
            if len(bars)!=15 or any(b[0]!=ts+i*p.MIN for i,b in enumerate(bars)):
                continue
            agg=(bars[0][1],max(b[2] for b in bars),min(b[3] for b in bars),bars[-1][4],
                 sum(b[5] for b in bars),sum(b[6] for b in bars),sum(b[7] for b in bars),
                 sum(b[8] for b in bars),sum(b[9] for b in bars))
            expected=tuple(vals)
            if (p.equal(agg[:4],expected[:4],rel=1e-8,abs_=1e-10)
                and p.equal(agg[4:6],expected[4:6],rel=1e-6,abs_=1e-5)
                and agg[6]==expected[6]
                and p.equal(agg[7:],expected[7:],rel=1e-6,abs_=1e-5)):
                matched+=1
            else:
                proof=p.exact_verified_provider_anomaly(sym,ts,agg,expected)
                saved=stored_proofs.get((sym,ts))
                if not proof and saved:
                    old_agg=json.loads(saved[0]);old_expected=json.loads(saved[1])
                    if field_match(agg,old_agg) and field_match(expected,old_expected):
                        proof={'symbol':sym,'utc':p.utc(ts),'open_ts':ts,
                               'type':'DIRECT_REFETCH_BOTH_TIMEFRAMES_PINNED',
                               'checked_ms':saved[4],'method':saved[5],
                               'one_min_sha256':saved[2],'15m_sha256':saved[3]}
                if proof:verified.append(proof)
                else:
                    unknown_total+=1
                    issue={'symbol':sym,'utc':p.utc(ts),'open_ts':ts,'got':agg,'expected':expected}
                    if len(errors)<limit:errors.append(issue)
                    if collect_unverified:pending.append(issue)
    result={'matched':matched,'verified_provider_exceptions':verified,
            'unverified_mismatches_total':unknown_total,'unverified_examples':errors}
    if collect_unverified:result['all_unverified']=pending
    return result



def verify_provider(root,db,max_verify=100,pause=0.45,fetch_fn=None):
    """Explicit REST verification of new exchange timeframe disagreements.

    Both ALL 15 1m bars and the direct 15m bar must match the stored values.
    Inconsistencies are exact-value pinned, never a universal tolerance.
    """
    if fetch_fn is None:fetch_fn=p.fetch
    lock=db.with_suffix('.lock')
    with lock.open('a+') as fh:
        try:fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Collector already running') from exc
        with closing(own_connect(db)) as con:
            events,_,_,_=frozen_config(con)
            pending=aggregate_audit(con,events,collect_unverified=True)['all_unverified']
            result=Counter()
            for mismatch in pending[:max_verify]:
                sym=mismatch['symbol'];ts=mismatch['open_ts']
                remote1,sha1=fetch_fn(sym,'1m',ts,ts+14*p.MIN)
                remote15,sha15=fetch_fn(sym,'15m',ts,ts)
                saved1=con.execute('SELECT '+BAR_COLS+''' FROM candles WHERE symbol=?
                    AND timeframe='1m' AND open_ts>=? AND open_ts<? ORDER BY open_ts''',
                    (sym,ts,ts+15*p.MIN)).fetchall()
                saved15=con.execute('SELECT '+BAR_COLS+''' FROM candles WHERE symbol=?
                    AND timeframe='15m' AND open_ts=?''',(sym,ts)).fetchall()
                valid=(len(remote1)==len(saved1)==15 and len(remote15)==len(saved15)==1
                       and all(remote1[i][0]==ts+i*p.MIN and saved1[i][0]==remote1[i][0]
                               and field_match(remote1[i][1:],saved1[i][1:]) for i in range(15))
                       and remote15[0][0]==ts and field_match(remote15[0][1:],saved15[0][1:]))
                if not valid:
                    result['NOT_VERIFIED']+=1
                    print('VERIFY_FAILED',sym,p.utc(ts),'stored values changed vs exchange; no whitelist',flush=True)
                    continue
                with con:
                    con.execute('''INSERT OR REPLACE INTO independently_verified_discrepancies
                        VALUES (?,?,?,?,?,?,?,?)''',(sym,ts,json.dumps(mismatch['got']),
                        json.dumps(mismatch['expected']),sha1,sha15,p.now_ms(),
                        'BINANCE_USDM_REST_ALL_15x1m_AND_1x15m_COMPARE_STORED'))
                result['verified']+=1
                if pause:time.sleep(pause)
            result['remaining_before_run']=len(pending)
            result['request_limited_to']=max_verify
            print('VERIFICATION_DONE',json.dumps(dict(result),ensure_ascii=False),flush=True)
            return dict(result)

def audit(root,db,out=None):
    with closing(p.ro(db)) as con:
        events,pre1,pre15,meta=frozen_config(con)
        counts=Counter();missing=[];incomplete_total=0
        for e in events:
            if e['quality']!='READY':continue
            for name,anchor in (('PRE_RETEST',e['retest_ts']),('PRE_KNOWN',e['known_ts']),
                                ('PRE_OBSERVED',e['first_seen_ms'])):
                for scope,sym in (('ASSET',e['symbol']),('BTC','BTCUSDT')):
                    for tf,n in (('1m',pre1),('15m',pre15)):
                        end=(anchor//p.TFS[tf])*p.TFS[tf]
                        _,state=p.window_rows(con,sym,tf,end,n)
                        counts[f'{scope}:{name}:{tf}:{state}']+=1
                        if state!='COMPLETE':
                            incomplete_total+=1
                            if len(missing)<25:missing.append({'event_key':e['event_key'],
                                'anchor':name,'scope':scope,'timeframe':tf,'status':state})
        conflicts=con.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0]
        agg=aggregate_audit(con,events)
        report={'version':VERSION,'frozen_cohort_sha256':meta['cohort_sha256'],
             'events':len(events),'ready':sum(e['quality']=='READY' for e in events),
             'invalid_by_reason':dict(Counter(e['quality'] for e in events if e['quality']!='READY')),
             'observations':dict(Counter(e['observation_type'] for e in events)),
             'windows_per_event':12,'coverage':dict(counts),
             'incomplete_windows_total':incomplete_total,'incomplete_examples':missing,
             'conflicts':conflicts,'aggregate':agg,
             'success':(incomplete_total==0 and conflicts==0
                        and agg['unverified_mismatches_total']==0
                        and all(e['quality']=='READY' for e in events))}
    if out:
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return report


def export(root,db):
    out=root/REL/OUT
    report=audit(root,db,out/'audit.json')
    if not report['success']:
        raise RuntimeError('Audit not successful; catalog export blocked, see audit.json')
    with closing(p.ro(db)) as con:
        events,pre1,pre15,meta=frozen_config(con)
        out.mkdir(parents=True,exist_ok=True)
        fields=[*COLUMNS.split(','),'quality']
        # Raw causal JSON is stored in full in SQLite; small CSV index references it.
        with (out/'events_catalog.csv').open('w',newline='',encoding='utf-8') as fh:
            cols=[f for f in fields if f!='causal_json']
            writer=csv.DictWriter(fh,fieldnames=cols)
            writer.writeheader()
            writer.writerows({k:e[k] for k in cols} for e in events)
        with (out/'candle_index.csv').open('w',newline='',encoding='utf-8') as fh:
            writer=csv.writer(fh)
            writer.writerow(['symbol','timeframe','candles','earliest_utc','latest_utc','source_types'])
            for sym,tf,count,lo,hi,sources in con.execute('''SELECT symbol,timeframe,COUNT(*),
                 MIN(open_ts),MAX(open_ts),GROUP_CONCAT(DISTINCT source) FROM candles
                 GROUP BY symbol,timeframe ORDER BY symbol,timeframe'''):
                writer.writerow([sym,tf,count,p.utc(lo),p.utc(hi),sources])
        summary={'events':len(events),'ready':report['ready'],'cohort_sha256':meta['cohort_sha256'],
                 'unique_candles':con.execute('SELECT COUNT(*) FROM candles').fetchone()[0],
                 'reused_from':dict(con.execute('SELECT source,inserted_rows FROM import_log')),
                 'verified_15m_source_discrepancies':len(report['aggregate']['verified_provider_exceptions']),
                 'pre1':pre1,'pre15':pre15,
                 'notes':['All scanner events frozen WITHOUT labels/outcomes/Auto Paths',
                          'Raw OHLCV/trades/taker/context remains queryable in isolated SQLite',
                          'PRE_RETEST, PRE_KNOWN, PRE_OBSERVED are closed-bar-only, do not imply execution fills',
                          'LIVE_OBSERVED and RECOVERED_WINDOW must remain separate',
                          'Historical dataset, NOT true forward validation']}
        (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return summary


def main(argv=None):
    pa=argparse.ArgumentParser(description=__doc__)
    m=pa.add_mutually_exclusive_group(required=True)
    for mode in ('plan','run','audit','export','verify_provider'):
        m.add_argument('--'+mode,action='store_true')
    pa.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    pa.add_argument('--pre1',type=int,default=180,help='Complete 1m CLOSED bars per anchor')
    pa.add_argument('--pre15',type=int,default=96,help='Complete 15m CLOSED bars per anchor')
    pa.add_argument('--max-requests',type=int,default=1500,help='Request cap PER --run execution')
    pa.add_argument('--pause',type=float,default=0.45)
    pa.add_argument('--max-verify',type=int,default=100)
    pa.add_argument('--min-free-gb',type=float,default=3)
    args=pa.parse_args(argv)
    if args.pre1<1 or args.pre15<1 or args.max_requests<1 or args.pause<0:
        pa.error('invalid parameters')
    root=args.project.resolve(); db=root/REL/DB
    if args.plan:
        print('PLAN',json.dumps(load_plan(root,db,args.pre1,args.pre15),ensure_ascii=False))
    elif args.run:
        print('RESULT',json.dumps(download(root,db,args.pre1,args.pre15,args.max_requests,
                args.pause,args.min_free_gb),ensure_ascii=False))
    elif args.audit:
        print(json.dumps(audit(root,db,root/REL/OUT/'audit.json'),indent=2,ensure_ascii=False))
    elif args.verify_provider:
        print(json.dumps(verify_provider(root,db,args.max_verify,args.pause),indent=2,ensure_ascii=False))
    else:
        print(json.dumps(export(root,db),indent=2,ensure_ascii=False))

if __name__=='__main__':main()
