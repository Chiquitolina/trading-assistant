#!/usr/bin/env python3
"""Runner Precursors Pilot V1: audited, isolated, read-only-source research.

200 events balanced among +3% MFE/180m vs <3% controls, not a trade simulation.
Reads scanner & auto_paths via mode=ro; optional episode-members CSV.
Writes ONLY reports/reaction_lab/runner_precursor_pilot_v1.sqlite3 + outputs.
REST Binance USD-M GET, no Redis, no collectors, no trading.

python3 runner_precursor_pilot.py --plan
python3 runner_precursor_pilot.py --run
python3 runner_precursor_pilot.py --audit
python3 runner_precursor_pilot.py --export
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import sqlite3
import statistics
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zlib

MIN = 60_000
TFS = {'1m': MIN, '15m': 15*MIN}
API = 'https://fapi.binance.com/fapi/v1/klines'
BAR = struct.Struct('<qdddd')
VERSION = 'runner-precursors-pilot-v1.0'
DEFAULT_DIR = 'reports/reaction_lab'
DB_NAME = 'runner_precursor_pilot_v1.sqlite3'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
 event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
 retest_ts INTEGER NOT NULL, known_ts INTEGER NOT NULL, first_seen_ms INTEGER NOT NULL,
 observation_type TEXT NOT NULL, episode_id TEXT, wave_minute INTEGER NOT NULL,
 entry_open REAL NOT NULL, class_label TEXT NOT NULL,
 mfe_180_pct REAL NOT NULL, mae_180_pct REAL NOT NULL,
 first_2m INTEGER, first_3m INTEGER, first_5m INTEGER
);
CREATE TABLE IF NOT EXISTS candles (
 symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_ts INTEGER NOT NULL,
 open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
 volume REAL NOT NULL, quote_volume REAL NOT NULL, trades INTEGER NOT NULL,
 taker_buy_base REAL NOT NULL, taker_buy_quote REAL NOT NULL,
 source TEXT NOT NULL, retrieved_ms INTEGER NOT NULL,
 PRIMARY KEY(symbol,timeframe,open_ts)
);
CREATE TABLE IF NOT EXISTS conflicts (
 symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_ts INTEGER NOT NULL,
 original_json TEXT NOT NULL, received_json TEXT NOT NULL,
 detected_ms INTEGER NOT NULL,
 PRIMARY KEY(symbol,timeframe,open_ts)
);
CREATE TABLE IF NOT EXISTS fetch_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
 begin_ts INTEGER NOT NULL,end_ts INTEGER NOT NULL, received INTEGER NOT NULL,
 sha256 TEXT,status TEXT NOT NULL,details TEXT,retrieved_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fetch_log_symbol ON fetch_log(symbol,timeframe,begin_ts);
'''


def now_ms(): return int(time.time()*1000)

def utc(ms): return datetime.fromtimestamp(ms/1000, timezone.utc).strftime('%Y-%m-%d %H:%M')

def day(ms): return datetime.fromtimestamp(ms/1000, timezone.utc).strftime('%Y-%m-%d')

def ro(path):
    if not path.is_file(): raise FileNotFoundError(f'Missing source: {path}')
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
    con.execute('PRAGMA query_only=ON')
    return con


def quality_bar(row,tf):
    ms=TFS[tf]
    if len(row)!=11:return False
    try:
        ts=int(row[0]); o,h,l,c,v,q=float(row[1]),float(row[2]),float(row[3]),float(row[4]),float(row[5]),float(row[6]);
        trades=int(row[7]); tb,tq=float(row[8]),float(row[9])
        # row[10] intentionally unused, Binance close_ts is checked during parse.
    except (ValueError,TypeError,OverflowError):return False
    basic=(ts%ms==0 and all(math.isfinite(x) for x in (o,h,l,c,v,q,tb,tq))
            and min(o,h,l,c)>0 and h>=max(o,l,c) and l<=min(o,h,c)
            and min(v,q,tb,tq)>=0 and trades>=0
            and tb<=v+max(v*1e-7,1e-8) and tq<=q+max(q*1e-7,1e-5))
    if not basic:return False
    # Quote volume must be consistent with OHLC price envelope; give modest
    # absolute and relative tolerance for Binance volume rounding.
    tol=max(1e-5, q*1e-5)
    if not (v*l-tol<=q<=v*h+tol):return False
    if not (tb*l-tol<=tq<=tb*h+tol):return False
    return True


def decode(blob, entry, retest, count):
    if not blob or int(count)!=360 or int(entry)!=int(retest)+MIN:return None
    try:
        raw=zlib.decompress(blob)
        if len(raw)!=360*BAR.size:return None
        rows=list(BAR.iter_unpack(raw))
    except (zlib.error,struct.error,TypeError):return None
    for i,(ts,o,h,l,c) in enumerate(rows):
        if ts!=entry+i*MIN or not all(math.isfinite(x) and x>0 for x in (o,h,l,c)) or h<max(o,l,c) or l>min(o,h,c):
            return None
    return rows


def outcomes(rows,side):
    """Structural *ideal reference* = next 1m OPEN, not a fill/TP simulation.

    Future label uses only 180 subsequent complete OHLCs.
    """
    base=rows[0][1]; sign=1 if side=='LONG' else -1
    fav=[]; adv=[]; hits={2:None,3:None,5:None}
    for i,(_ts,_o,h,l,_c) in enumerate(rows[:180],1):
        if sign==1:
            f=max(0,(h/base-1)*100); a=max(0,(1-l/base)*100)
        else:
            f=max(0,(1-l/base)*100); a=max(0,(h/base-1)*100)
        fav.append(f); adv.append(a)
        for threshold in hits:
            if hits[threshold] is None and f>=threshold:hits[threshold]=i
    return base,max(fav),max(adv),hits


def load_episode_csv(path):
    if not path:return {}
    output={}
    with open(path,encoding='utf-8-sig',newline='') as fh:
        for row in csv.DictReader(fh):
            key=row.get('event_key'); episode=row.get('episode_id')
            if key and episode:
                try: pos=int(row.get('position_in_episode') or 1)
                except ValueError:pos=1
                output[key]=(episode,pos)
    return output


def source_candidates(root,episode_path=None):
    base=root/DEFAULT_DIR
    if episode_path is None:
        candidate=root/DEFAULT_DIR/'movement_episodes_v1'/'events.csv'
        episode_path=candidate if candidate.is_file() else None
    ep=load_episode_csv(episode_path)
    with closing(ro(base/'scanner_events_v1.sqlite3')) as sc, closing(ro(base/'auto_paths_v1.sqlite3')) as ap:
        events={row[0]:row for row in sc.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,
           first_seen_ms,observation_type FROM events''')}
        paths=ap.execute('''SELECT event_key,entry_ts,state,count,path_blob,retest_ts FROM events
                           WHERE state='COMPLETE' AND count=360 AND path_blob IS NOT NULL''')
        candidates=[]; excluded=Counter()
        for key,entry,state,count,blob,path_retest in paths:
            e=events.get(key)
            if not e:excluded['not_in_scanner']+=1;continue
            _,sym,side,retest,known,seen,observation=e
            if (side not in ('LONG','SHORT') or observation not in ('LIVE_OBSERVED','RECOVERED_WINDOW')
                or key!=f'{sym}|{side}|{retest}' or known!=retest+MIN
                or seen<known or path_retest!=retest):
                excluded['invalid_scanner_metadata']+=1;continue
            bars=decode(blob,entry,retest,count)
            if bars is None:excluded['invalid_or_gapped_paths']+=1;continue
            opening,mfe,mae,hits=outcomes(bars,side)
            if isinstance(ep.get(key),tuple) and ep[key][1] != 1:
                excluded['later_member_of_known_episode']+=1;continue
            episode=ep.get(key)
            if isinstance(episode,tuple):episode=episode[0]
            candidates.append({'event_key':key,'symbol':sym,'side':side,'retest_ts':retest,
                  'known_ts':known,'first_seen_ms':seen,'observation_type':observation,
                  'episode_id':episode, 'wave_minute':known//MIN,
                  'entry_open':opening,'class_label':'RUNNER_3' if mfe>=3 else 'CONTROL_LT3',
                  'mfe_180_pct':mfe,'mae_180_pct':mae,'first_2m':hits[2],
                  'first_3m':hits[3],'first_5m':hits[5]})
    return candidates, excluded


def _round_robin(pool,limit,seed,selected=(),max_sym=3,max_wave=2,sep_min=180):
    """Spread across dates and avoid selecting duplicated same-symbol movement.
    Not a claim that same-minute symbols are causally the same wave.
    """
    rng=random.Random(seed)
    buckets=defaultdict(list)
    for c in pool:buckets[day(c['retest_ts'])].append(c)
    for values in buckets.values():
        values.sort(key=lambda c:c['event_key']);rng.shuffle(values)
    selected=list(selected); result=[]
    counts_sym=Counter(x['symbol'] for x in selected)
    counts_wave=Counter(x['wave_minute'] for x in selected)
    episodes={x['episode_id'] for x in selected if x['episode_id']}
    by_sym=defaultdict(list)
    for x in selected:by_sym[(x['symbol'],x['side'])].append(x['retest_ts'])
    days=sorted(buckets)
    while len(result)<limit:
        progressed=False
        for d in days:
            while buckets[d]:
                c=buckets[d].pop()
                k=(c['symbol'],c['side'])
                if (counts_sym[c['symbol']]>=max_sym or counts_wave[c['wave_minute']]>=max_wave
                    or (c['episode_id'] and c['episode_id'] in episodes)
                    or any(abs(c['retest_ts']-t)<sep_min*MIN for t in by_sym[k])):
                    continue
                result.append(c);selected.append(c);counts_sym[c['symbol']]+=1
                counts_wave[c['wave_minute']]+=1
                if c['episode_id']:episodes.add(c['episode_id'])
                by_sym[k].append(c['retest_ts'])
                progressed=True;break
            if len(result)>=limit:break
        if not progressed:break
    return result


def select_sample(candidates, per_class=100, seed=20261021):
    runners=[c for c in candidates if c['class_label']=='RUNNER_3']
    controls=[c for c in candidates if c['class_label']=='CONTROL_LT3']
    chosen_r=_round_robin(runners,per_class,seed)
    if len(chosen_r)!=per_class:raise ValueError(f'Only {len(chosen_r)}/{per_class} independent runners qualify')
    # Controls matched by UTC day + source when possible; then round-robin remainder.
    ctrl_by_bucket=defaultdict(list)
    rng=random.Random(seed+8)
    for c in controls:ctrl_by_bucket[(day(c['retest_ts']),c['observation_type'])].append(c)
    for values in ctrl_by_bucket.values():values.sort(key=lambda c:c['event_key']);rng.shuffle(values)
    chosen_c=[]
    # For simple, robust matching use greedy over requested strata and verify same global constraints.
    for r in chosen_r:
        pool=ctrl_by_bucket[(day(r['retest_ts']),r['observation_type'])]
        for idx in range(len(pool)-1,-1,-1):
            c=pool[idx]
            joined=chosen_r+chosen_c
            if (sum(x['symbol']==c['symbol'] for x in joined)>=3
                or sum(x['wave_minute']==c['wave_minute'] for x in joined)>=2
                or (c['episode_id'] and any(x['episode_id']==c['episode_id'] for x in joined))
                or any(x['symbol']==c['symbol'] and x['side']==c['side'] and
                       abs(x['retest_ts']-c['retest_ts'])<180*MIN for x in joined)):
                continue
            chosen_c.append(c);pool.pop(idx);break
    if len(chosen_c)<per_class:
        remaining_keys={c['event_key'] for c in chosen_c}
        additional=_round_robin([c for c in controls if c['event_key'] not in remaining_keys],
                per_class-len(chosen_c),seed+71,selected=chosen_r+chosen_c)
        chosen_c.extend(additional)
    if len(chosen_c)!=per_class:raise ValueError(f'Only {len(chosen_c)}/{per_class} controls selected')
    return sorted(chosen_r+chosen_c,key=lambda x:(x['retest_ts'],x['event_key']))


def windows(samples,pre1=120,pre15=96,post_audit=15):
    """Store only pre-known data as features, plus separate 1m audit-only POST bars."""
    spans=defaultdict(list)
    for e in samples:
        for symbol in (e['symbol'],'BTCUSDT'):
            for tf,n in (('1m',pre1),('15m',pre15)):
                tfms=TFS[tf]
                # Both causal anchors: before the retest candle, and before the
                # theoretical known_ts. Even if the retest straddles a 15m boundary.
                for anchor in (e['retest_ts'],e['known_ts']):
                    end=(anchor//tfms)*tfms  # EXCLUSIVE
                    spans[(symbol,tf)].append((end-n*tfms,end-tfms))
        # Only future candles for source parity; NEVER exported as input features.
        if post_audit:
            spans[(e['symbol'],'1m')].append((e['retest_ts']+MIN,e['retest_ts']+post_audit*MIN))
    return {key:merge_ranges(xs,TFS[key[1]]) for key,xs in spans.items()}


def merge_ranges(xs,step):
    out=[]
    for a,b in sorted(xs):
        if not out or a>out[-1][1]+step:out.append([a,b])
        else:out[-1][1]=max(out[-1][1],b)
    return [(a,b) for a,b in out]


def missing(a,b,existing,step):
    prev=a-step
    for t in existing:
        if t<a or t>b:continue
        if t-prev>step:yield prev+step,t-step
        prev=t
    if b-prev>=step:yield prev+step,b


def chunks(start,end,step,limit=1000):
    while start<=end:
        stop=min(end,start+(limit-1)*step)
        yield start,stop
        start=stop+step


def connect(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.executescript(SCHEMA)
    with con:
        con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('version',VERSION))
        assert con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]==VERSION
    return con


def sample_hash(samples):
    return hashlib.sha256('\n'.join(json.dumps(s,sort_keys=True) for s in samples).encode()).hexdigest()


def save_samples(con,selected,args):
    existing=con.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
    if existing:
        count=con.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        if count!=len(selected):
            # May have newly arrived auto path data, preserve original fixed cohort.
            print(f'COHORT_FROZEN rows={count}; ignoring newly selected cohort',flush=True)
        return load_samples(con)
    with con:
        for e in selected:
            con.execute('''INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              tuple(e[k] for k in ('event_key','symbol','side','retest_ts','known_ts','first_seen_ms',
                   'observation_type','episode_id','wave_minute','entry_open','class_label',
                   'mfe_180_pct','mae_180_pct','first_2m','first_3m','first_5m')))
        for k,v in {'sample_sha256':sample_hash(selected),'created_ms':str(now_ms()),
             'seed':str(args.seed),'pre1':str(args.pre1),'pre15':str(args.pre15),
             'post_audit':str(args.post_audit),'purpose':'BALANCED_EXPLORATORY_NOT_PREVALENCE',
             'sample_count':str(len(selected))}.items():
            con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(k,v))
    return selected


def load_samples(con):
    cols=[r[1] for r in con.execute('PRAGMA table_info(samples)')]
    return [dict(zip(cols,row)) for row in con.execute('SELECT * FROM samples ORDER BY retest_ts,event_key')]


def parse_klines(raw,symbol,tf,start,end):
    obj=json.loads(raw)
    if not isinstance(obj,list) or len(obj)>1000:raise ValueError('invalid Binance response shape')
    ms=TFS[tf];bars=[];prev=None
    for x in obj:
        if not isinstance(x,list) or len(x)<11:raise ValueError('short Binance kline row')
        ts=int(x[0]); close_ts=int(x[6]);
        if not (start<=ts<=end and ts%ms==0 and close_ts==ts+ms-1 and (prev is None or ts>prev)):
            raise ValueError('timestamp, closeTime, order or alignment mismatch')
        nums=[float(x[i]) for i in (1,2,3,4,5,7,9,10)]
        trades=int(x[8])
        bar=(ts,*nums[:5],nums[5],trades,nums[6],nums[7],None)
        if not quality_bar(bar,tf):raise ValueError(f'invalid OHLC/volume/trades/taker {symbol} {ts}')
        bars.append(bar[:-1]);prev=ts
    return bars


def fetch(symbol,tf,start,end,timeout=25,retries=5,opener=urlopen):
    query=urlencode({'symbol':symbol,'interval':tf,'startTime':start,'endTime':end+TFS[tf]-1,'limit':1000})
    req=Request(API+'?'+query,headers={'User-Agent':'runner-precursors-pilot/1.0'})
    for attempt in range(retries):
        try:
            with opener(req,timeout=timeout) as response:
                body=response.read(4_000_001)
            if len(body)>4_000_000:raise ValueError('response too large')
            return parse_klines(body,symbol,tf,start,end),hashlib.sha256(body).hexdigest()
        except HTTPError as exc:
            if exc.code not in (418,429,500,502,503,504):raise
            if exc.code in (418,429):raise RuntimeError(f'Binance rate-limited HTTP {exc.code}; stop batch') from exc
            if attempt==retries-1:raise
        except (URLError,TimeoutError,ConnectionError):
            if attempt==retries-1:raise
        time.sleep(min(20,2**attempt))
    raise RuntimeError('exhausted retries')


def equal(a,b,rel=1e-8,abs_=1e-8):
    return all(math.isclose(float(x),float(y),rel_tol=rel,abs_tol=abs_) for x,y in zip(a,b))


def ingest(con,symbol,tf,a,b,bars,sha):
    written=0; conflicted=0; stamp=now_ms()
    with con:
        for row in bars:
            ts,*data=row
            old=con.execute('''SELECT open,high,low,close,volume,quote_volume,trades,
                 taker_buy_base,taker_buy_quote FROM candles
                 WHERE symbol=? AND timeframe=? AND open_ts=?''',(symbol,tf,ts)).fetchone()
            if old is not None:
                if not equal(old,data):
                    conflicted+=1
                    con.execute('''INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,?,?)''',
                        (symbol,tf,ts,json.dumps(old),json.dumps(data),stamp))
            else:
                con.execute('''INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (symbol,tf,ts,*data,'BINANCE_USDM_REST',stamp))
                written+=1
        expected=(b-a)//TFS[tf]+1
        status='CONFLICT' if conflicted else 'COMPLETE' if len(bars)==expected else 'PARTIAL'
        con.execute('''INSERT INTO fetch_log(symbol,timeframe,begin_ts,end_ts,received,sha256,status,details,retrieved_ms)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (symbol,tf,a,b,len(bars),sha,status,f'new={written} conflicts={conflicted}',stamp))
    return written,conflicted


def run(root,db_path,args,selected=None,fetch_fn=fetch):
    if shutil.disk_usage(root).free<args.min_free_gb*1024**3:
        raise RuntimeError('Low free disk, refusing to download')
    lock=db_path.with_suffix('.lock');lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a+') as lockfile:
        try:fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Pilot download already running') from exc
        with closing(connect(db_path)) as con:
            current=load_samples(con)
            if current:
                selected=current
                if len(current)!=args.cases+args.controls:
                    print(f'NOTE Using FROZEN sample of {len(current)} rows')
                pre1=int(con.execute("SELECT value FROM meta WHERE key='pre1'").fetchone()[0]);
                pre15=int(con.execute("SELECT value FROM meta WHERE key='pre15'").fetchone()[0]);
                post=int(con.execute("SELECT value FROM meta WHERE key='post_audit'").fetchone()[0]);
            else:
                if selected is None:
                    sample,excluded=source_candidates(root,args.episodes_csv)
                    selected=select_sample(sample,args.cases,args.seed) if args.controls==args.cases else select_two(sample,args)
                selected=save_samples(con,selected,args)
                pre1=args.pre1;pre15=args.pre15;post=args.post_audit
            spans=windows(selected,pre1,pre15,post)
            nrequests=0;total=Counter();failures=0
            for (sym,tf),parts in sorted(spans.items()):
                step=TFS[tf]
                for a,b in parts:
                    existing=[r[0] for r in con.execute('''SELECT open_ts FROM candles
                         WHERE symbol=? AND timeframe=? AND open_ts BETWEEN ? AND ? ORDER BY open_ts''',
                         (sym,tf,a,b))]
                    for missing_a,missing_b in missing(a,b,existing,step):
                        for x,y in chunks(missing_a,missing_b,step):
                            if nrequests>=args.max_requests:
                                print('REQUEST_CAP_REACHED safe pause; rerun --run');return total
                            nrequests+=1
                            try:
                                bars,sha=fetch_fn(sym,tf,x,y)
                                new,conf=ingest(con,sym,tf,x,y,bars,sha)
                                total['requests']+=1;total['new_bars']+=new;total['conflicts']+=conf
                                if conf:raise RuntimeError(f'Conflict {sym} {tf} {x}')
                                if len(bars)!=(y-x)//step+1:
                                    total['partial_requests']+=1
                            except Exception as exc:
                                failures+=1;total['errors']+=1
                                with con:
                                    con.execute('''INSERT INTO fetch_log(symbol,timeframe,begin_ts,end_ts,received,
                                          sha256,status,details,retrieved_ms) VALUES (?,?,?,?,?,?,?,?,?)''',
                                        (sym,tf,x,y,0,None,'ERROR',f'{type(exc).__name__}: {str(exc)[:200]}',now_ms()))
                                print(f'FETCH_ERROR {sym} {tf} {utc(x)} {type(exc).__name__}: {exc}',flush=True)
                                if failures>=args.max_failures or 'rate-limited' in str(exc) or 'Conflict' in str(exc):
                                    raise RuntimeError('Stopped on repeated/provider/data errors; safe to resume') from exc
                            if nrequests%20==0:
                                print('PROGRESS',json.dumps(dict(total)), 'last=',sym,tf,utc(y),flush=True)
                                if shutil.disk_usage(root).free<args.min_free_gb*1024**3:
                                    raise RuntimeError('Low disk during download; safe to resume')
                            time.sleep(args.pause)
            print('DOWNLOAD_DONE',json.dumps(dict(total)),flush=True)
            return total


def select_two(candidates,args):
    r=_round_robin([c for c in candidates if c['class_label']=='RUNNER_3'],args.cases,args.seed)
    c=_round_robin([c for c in candidates if c['class_label']=='CONTROL_LT3'],args.controls,args.seed+1,selected=r)
    if len(r)!=args.cases or len(c)!=args.controls:raise ValueError('Cannot select requested independent cases/controls')
    return sorted(r+c,key=lambda x:(x['retest_ts'],x['event_key']))


def window_rows(con,sym,tf,end_excl,n):
    step=TFS[tf]; a=end_excl-n*step
    rows=con.execute('''SELECT open_ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base,taker_buy_quote
                  FROM candles WHERE symbol=? AND timeframe=? AND open_ts>=? AND open_ts<? ORDER BY open_ts''',
                  (sym,tf,a,end_excl)).fetchall()
    conflict=con.execute('''SELECT COUNT(*) FROM conflicts WHERE symbol=? AND timeframe=? AND open_ts>=? AND open_ts<?''',
                         (sym,tf,a,end_excl)).fetchone()[0]
    if conflict:return None,'CONFLICT'
    if len(rows)!=n or any(row[0]!=a+i*step for i,row in enumerate(rows)):
        return None,f'INCOMPLETE_{len(rows)}_OF_{n}'
    return rows,'COMPLETE'


def validate_aggregation(con, sample,pre1,pre15):
    """Check full 15-minute bars for which all 15 1m historical bars are available."""
    issues=[]; matched=0
    symbols={s['symbol'] for s in sample}|{'BTCUSDT'}
    # compare only candle timestamps present for that sample, not global unrelated rows
    for sym in sorted(symbols):
        for ts,o,h,l,c,v,q,trades,tb,tq in con.execute('''SELECT open_ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base,taker_buy_quote
                        FROM candles WHERE symbol=? AND timeframe='15m' ORDER BY open_ts''',(sym,)):
            rows=con.execute('''SELECT open_ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base,taker_buy_quote
                  FROM candles WHERE symbol=? AND timeframe='1m' AND open_ts>=? AND open_ts<? ORDER BY open_ts''',
                  (sym,ts,ts+15*MIN)).fetchall()
            if len(rows)!=15 or any(r[0]!=ts+i*MIN for i,r in enumerate(rows)):continue
            agg=(rows[0][1],max(r[2] for r in rows),min(r[3] for r in rows),rows[-1][4],
                 sum(r[5] for r in rows),sum(r[6] for r in rows),sum(r[7] for r in rows),
                 sum(r[8] for r in rows),sum(r[9] for r in rows))
            expected=(o,h,l,c,v,q,trades,tb,tq)
            # Aggregated volume/quote/taker rounded by Binance: tolerance modest and explicit.
            if not (equal(agg[:4],expected[:4],rel=1e-8,abs_=1e-10)
                    and equal(agg[4:6],expected[4:6],rel=1e-6,abs_=1e-5)
                    and agg[6]==expected[6]
                    and equal(agg[7:],expected[7:],rel=1e-6,abs_=1e-5)):
                if len(issues)<25:issues.append({'type':'AGGREGATION_MISMATCH','symbol':sym,'utc':utc(ts),'got':agg,'expected':expected})
            else:matched+=1
    return matched,issues


def autopaths_overlap(root,con,samples,post_audit):
    if not post_audit:return {'compared':0,'matched':0,'different':0,'bad_paths':0,'samples':[]}
    with closing(ro(root/DEFAULT_DIR/'auto_paths_v1.sqlite3')) as ap:
        ret={'compared':0,'matched':0,'different':0,'bad_paths':0,'samples':[]}
        for s in samples:
            record=ap.execute('SELECT entry_ts,retest_ts,count,path_blob FROM events WHERE event_key=?',
                               (s['event_key'],)).fetchone()
            if not record:ret['bad_paths']+=1;continue
            entry,retest,count,blob=record
            rows=decode(blob,entry,retest,count)
            if rows is None:ret['bad_paths']+=1;continue
            for ts,o,h,l,c in rows[:post_audit]:
                other=con.execute('''SELECT open,high,low,close FROM candles WHERE symbol=? AND timeframe='1m' AND open_ts=?''',
                     (s['symbol'],ts)).fetchone()
                if other is None:continue
                ret['compared']+=1
                if equal(other,(o,h,l,c),rel=1e-9,abs_=1e-11):ret['matched']+=1
                else:
                    ret['different']+=1
                    if len(ret['samples'])<10:ret['samples'].append([s['event_key'],utc(ts),other,[o,h,l,c]])
        return ret


def audit(root,db_path,output=None):
    with closing(ro(db_path)) as con:
        samples=load_samples(con)
        if not samples:raise ValueError('No frozen samples')
        meta=dict(con.execute('SELECT key,value FROM meta'))
        pre1=int(meta['pre1']);pre15=int(meta['pre15']);post=int(meta['post_audit'])
        report={'sample_count':len(samples),'counts':dict(Counter(s['class_label'] for s in samples)),
            'observation_types':dict(Counter(s['observation_type'] for s in samples)),
            'days':len({day(s['retest_ts']) for s in samples}),
            'symbols':len({s['symbol'] for s in samples}),
            'frozen_sample_hash':meta['sample_sha256'],'timeframe_coverage':Counter(),
            'incomplete':[],'conflicts':con.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0]}
        for s in samples:
            for sym_type,sym in (('ASSET',s['symbol']),('BTC','BTCUSDT')):
                for tf,n in (('1m',pre1),('15m',pre15)):
                    for anchor_name,anchor_ts in (('PRE_RETEST',s['retest_ts']),('PRE_KNOWN',s['known_ts'])):
                        end=(anchor_ts//TFS[tf])*TFS[tf]
                        _,state=window_rows(con,sym,tf,end,n)
                        report['timeframe_coverage'][f'{sym_type}:{anchor_name}:{tf}:{state}']+=1
                        if state!='COMPLETE' and len(report['incomplete'])<30:
                            report['incomplete'].append({'event':s['event_key'],'kind':sym_type,
                                                        'anchor':anchor_name,'tf':tf,'status':state})
        report['timeframe_coverage']=dict(report['timeframe_coverage'])
        report['aggregate_match'],report['aggregate_issues']=validate_aggregation(con,samples,pre1,pre15)
        report['auto_paths_parity']=autopaths_overlap(root,con,samples,post)
        report['success']=(not report['incomplete'] and report['conflicts']==0 and not report['aggregate_issues']
                           and report['auto_paths_parity']['different']==0 and
                           report['auto_paths_parity']['bad_paths']==0 and
                           report['auto_paths_parity']['compared']==len(samples)*post)
        if output:
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str)+'\n',encoding='utf-8')
        return report


def signed_ret(rows,side,n):
    if len(rows)<n+1:return None
    return (rows[-1][4]/rows[-n-1][4]-1)*100*(1 if side=='LONG' else -1)


def segment_features(rows,side,prefix):
    if not rows:return {}
    out={};volumes=[r[6] for r in rows];moves=[abs(rows[i][4]/rows[i-1][4]-1) for i in range(1,len(rows))]
    for n in (5,15,30,60):
        if len(rows)>=n+1:
            tail=rows[-n:]
            out[f'{prefix}_ret_{n}_pct']=round(signed_ret(rows,side,n),7)
            out[f'{prefix}_range_{n}_pct']=round(100*(max(x[2] for x in tail)/min(x[3] for x in tail)-1),7)
            out[f'{prefix}_quote_usdt_{n}']=round(sum(x[6] for x in tail),3)
            out[f'{prefix}_taker_share_{n}']=round(sum(x[9] for x in tail)/max(sum(x[6] for x in tail),1e-12),6)
            out[f'{prefix}_trades_{n}']=sum(int(x[7]) for x in tail)
            denom=sum(abs(math.log(rows[i][4]/rows[i-1][4])) for i in range(len(rows)-n,len(rows)))
            out[f'{prefix}_efficiency_{n}']=round(abs(math.log(rows[-1][4]/rows[-n-1][4]))/denom,6) if denom else None
    if len(rows)>=60:
        q5=sum(x[6] for x in rows[-5:])/5
        q60=sum(x[6] for x in rows[-60:])/60
        out[f'{prefix}_quote_rate_5_over_60']=round(q5/q60,6) if q60 else None
        t5=sum(x[7] for x in rows[-5:])/5
        t60=sum(x[7] for x in rows[-60:])/60
        out[f'{prefix}_trades_rate_5_over_60']=round(t5/t60,6) if t60 else None
    if len(moves)>=60:
        out[f'{prefix}_realized_vol_60_pct']=round(math.sqrt(sum(x*x for x in moves[-60:]))*100,7)
    return out


def features_from_sample(con,s,pre1,pre15):
    out=dict(s)
    for anchor_name,anchor_ts in (('prer',s['retest_ts']),('known',s['known_ts'])):
        for typ,sym in (('asset',s['symbol']),('btc','BTCUSDT')):
            for tf,n in (('1m',pre1),('15m',pre15)):
                end=(anchor_ts//TFS[tf])*TFS[tf]
                rows,state=window_rows(con,sym,tf,end,n)
                if state!='COMPLETE':return None
                prefix=f'{typ}_{anchor_name}_{tf}'
                out.update(segment_features(rows,s['side'],prefix))
                # Explicit 120m or 24h gross directional move.
                out[f'{prefix}_full_window_pct']=round(
                    (rows[-1][4]/rows[0][1]-1)*100*(1 if s['side']=='LONG' else -1),7)
                # 15m timeframe: use intuitive 1h/4h windows, strictly closed.
                if tf=='15m':
                    for bars,label in ((4,'1h'),(16,'4h'),(96,'24h')):
                        if len(rows)>=bars:
                            tail=rows[-bars:]
                            out[f'{prefix}_move_{label}_pct']=round(
                                (tail[-1][4]/tail[0][1]-1)*100*(1 if s['side']=='LONG' else -1),7)
                            out[f'{prefix}_quote_{label}']=round(sum(r[6] for r in tail),3)
    for anchor_name in ('prer','known'):
        for tf in ('1m','15m'):
            for n in (5,15,30,60):
                a=out.get(f'asset_{anchor_name}_{tf}_ret_{n}_pct')
                b=out.get(f'btc_{anchor_name}_{tf}_ret_{n}_pct')
                if a is not None and b is not None:
                    out[f'excess_vs_btc_{anchor_name}_{tf}_{n}_pct']=round(a-b,7)
    out['primary_context']='PRE_RETEST_CLOSED_ONLY'
    out['known_context']='PRE_KNOWN_CLOSED_ONLY'
    out['is_contemporaneously_observed']=int(s['observation_type']=='LIVE_OBSERVED')
    return out


CAUSAL_SWING_FIELDS = (
    'prominence_pct','pivot_to_confirmation_pct','max_departure_pct',
    'retest_distance_pct','penetration_pct','retest_close_location',
    'confirmed_to_retest_min','pivot_to_retest_min','retest_age_min',
    'relative_volume','retest_volume','triggering_swing_count',
)


def load_causal_swing_features(root, keys):
    """Use only the scanner's frozen causal_json, never recompute with later candles."""
    path=root/DEFAULT_DIR/'scanner_events_v1.sqlite3'
    if not path.exists():return {}
    result={}
    with closing(ro(path)) as sc:
        for event_key,raw in sc.execute('SELECT event_key,causal_json FROM events'):
            if event_key not in keys:continue
            try: data=json.loads(raw or '{}')
            except (ValueError,TypeError):continue
            if not isinstance(data,dict):continue
            extracted={}
            for key in CAUSAL_SWING_FIELDS:
                value=data.get(key)
                try:
                    number=float(value)
                    if math.isfinite(number):extracted['known_swing_'+key]=number
                except (ValueError,TypeError,OverflowError):pass
            result[event_key]=extracted
    return result


def export(root,db_path,out_dir,require_audit=True):
    report=audit(root,db_path,out_dir/'audit.json')
    if require_audit and not report['success']:
        raise RuntimeError('Audit failed. No feature export; inspect audit.json')
    with closing(ro(db_path)) as con:
        meta=dict(con.execute('SELECT key,value FROM meta'))
        rows=[]
        sample_list=load_samples(con)
        swing_data=load_causal_swing_features(root,{s['event_key'] for s in sample_list})
        for s in sample_list:
            x=features_from_sample(con,s,int(meta['pre1']),int(meta['pre15']))
            if x is not None:
                x.update(swing_data.get(s['event_key'],{}))
                rows.append(x)
        if not rows:raise RuntimeError('No complete feature rows')
        # label/outcomes remain separated in their own file to prevent accidental leakage.
        out_dir.mkdir(parents=True,exist_ok=True)
        source_only={'class_label','mfe_180_pct','mae_180_pct','first_2m','first_3m','first_5m','entry_open'}
        identifiers={'event_key','symbol','side','retest_ts','known_ts','first_seen_ms',
                     'observation_type','episode_id','wave_minute'}
        all_keys=set().union(*(r.keys() for r in rows))
        prer_keys=sorted(k for k in all_keys if
                  k in identifiers or k=='primary_context' or k.startswith((
                      'asset_prer_', 'btc_prer_', 'excess_vs_btc_prer_')))
        known_keys=sorted(k for k in all_keys if
                  k in identifiers or k=='known_context' or k.startswith((
                      'asset_known_', 'btc_known_', 'excess_vs_btc_known_', 'known_swing_')))
        feature_keys=sorted((set(prer_keys)|set(known_keys))-identifiers)
        outcome_keys=['event_key',*sorted(source_only)]
        for filename,keys in [('features_before_retest.csv',prer_keys),
                              ('features_at_known.csv',known_keys),
                              ('outcomes_future.csv',outcome_keys)]:
            with (out_dir/filename).open('w',newline='',encoding='utf-8') as fh:
                writer=csv.DictWriter(fh,fieldnames=keys,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
        # descriptive comparison in sampled 1:1 classes; not base-rate, performance or trading claims.
        stats=[]
        feature_numeric=[k for k in feature_keys if k not in identifiers and k not in ('primary_context','known_context','is_contemporaneously_observed')]
        for k in feature_numeric:
            run=[r[k] for r in rows if r['class_label']=='RUNNER_3' and isinstance(r.get(k),(int,float))]
            ctrl=[r[k] for r in rows if r['class_label']=='CONTROL_LT3' and isinstance(r.get(k),(int,float))]
            if len(run)>=5 and len(ctrl)>=5:
                stats.append({'feature':k,'runner_n':len(run),'control_n':len(ctrl),
                       'median_runner':statistics.median(run),'median_control':statistics.median(ctrl),
                       'median_difference':statistics.median(run)-statistics.median(ctrl)})
        with (out_dir/'descriptive_comparison.csv').open('w',newline='',encoding='utf-8') as fh:
            writer=csv.DictWriter(fh,fieldnames=['feature','runner_n','control_n','median_runner','median_control','median_difference'])
            writer.writeheader();writer.writerows(stats)
        summary={'exported':len(rows),'runner_count':sum(r['class_label']=='RUNNER_3' for r in rows),
                 'control_count':sum(r['class_label']=='CONTROL_LT3' for r in rows),
                 'features':len(feature_keys),'sampled_balanced':True,
                 'warning':'Do not estimate true population probabilities from balanced sample. Structural retest-time labels are NOT executable LIVE fills. PRE_RETEST is the primary precursor anchor.',
                 'artifacts':['features_before_retest.csv','features_at_known.csv','outcomes_future.csv','descriptive_comparison.csv','audit.json']}
        (out_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
        return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    for x in ('plan','run','audit','export'):mode.add_argument('--'+x,action='store_true')
    p.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--episodes-csv',type=Path,default=None,help='Optional episode members exported CSV')
    p.add_argument('--cases',type=int,default=100)
    p.add_argument('--controls',type=int,default=100)
    p.add_argument('--seed',type=int,default=20261021)
    p.add_argument('--pre1',type=int,default=120)
    p.add_argument('--pre15',type=int,default=96)
    p.add_argument('--post-audit',type=int,default=15)
    p.add_argument('--pause',type=float,default=0.45)
    p.add_argument('--max-requests',type=int,default=2000)
    p.add_argument('--max-failures',type=int,default=10)
    p.add_argument('--min-free-gb',type=float,default=1.0)
    args=p.parse_args(argv)
    root=args.project.resolve(); db=root/DEFAULT_DIR/DB_NAME
    if args.cases<1 or args.controls<1 or args.pre1<60 or args.pre15<96 or not(0<=args.post_audit<=60):
        p.error('invalid sample/window sizes')
    if args.plan:
        if db.exists():
            with closing(ro(db)) as con:selected=load_samples(con)
            print('PLAN FROZEN EXISTING PILOT',len(selected))
            with closing(ro(db)) as con:
                meta=dict(con.execute('SELECT key,value FROM meta'))
                spans=windows(selected,int(meta['pre1']),int(meta['pre15']),int(meta['post_audit']))
        else:
            candidates,excluded=source_candidates(root,args.episodes_csv)
            print('SOURCE_ELIGIBLE',len(candidates),'BY_CLASS',dict(Counter(x['class_label'] for x in candidates)))
            print('SOURCE_EXCLUDED',dict(excluded),'EPISODE_LINKED',sum(bool(x['episode_id']) for x in candidates))
            selected=select_sample(candidates,args.cases,args.seed) if args.cases==args.controls else select_two(candidates,args)
            spans=windows(selected,args.pre1,args.pre15,args.post_audit)
        required=sum((b-a)//TFS[tf]+1 for (_,tf),parts in spans.items() for a,b in parts)
        estimated=sum(((b-a)//TFS[tf]+1000)//1000 for (_,tf),parts in spans.items() for a,b in parts)
        print('SAMPLE',len(selected),'RUNNER',sum(s['class_label']=='RUNNER_3' for s in selected),
              'CONTROLS',sum(s['class_label']=='CONTROL_LT3' for s in selected))
        print('DAYS',len({day(s['retest_ts']) for s in selected}),
              'SYMBOLS',len({s['symbol'] for s in selected}),
              'OBSERVATIONS',dict(Counter(s['observation_type'] for s in selected)))
        print('UNIQUE_REQUIRED_BARS',required,'APPROX_REQUESTS',estimated,'STORE',db)
        print('SAMPLE_HASH',sample_hash(selected),'NO_FILES_OR_NETWORK_MODIFIED')
    elif args.run:
        res=run(root,db,args)
        print('RESULT',json.dumps(dict(res)))
    elif args.audit:
        print(json.dumps(audit(root,db),indent=2,ensure_ascii=False))
    else:
        print(json.dumps(export(root,db,root/DEFAULT_DIR/'runner_precursor_pilot'),indent=2))

if __name__=='__main__':main()
