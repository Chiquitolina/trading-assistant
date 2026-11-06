#!/usr/bin/env python3
"""First Touch historical 1m recovery V1.1 (OFFLINE, limited batches).

Reads a frozen First Touch Recovery Audit V1 events.csv and the current First
Touch V2 SQLite (read-only). Downloads public, CLOSED Binance USD-M Futures 1m
candles for explicitly selected historical INDECISIVE/FAILED events, with
per-symbol merged intervals and strict request limits. Stores recovered market
paths + Runner Discovery-compatible outcomes in a NEW, separate output folder.
Never updates FIRST_TOUCH_V2, AUTO_PATHS, PM2, Redis, scanners or LIVE.

Plan (no download/write):
  python3 reaction_lab_first_touch_historical_recovery_v11.py --project . \
    --audit-events reports/reaction_lab/first_touch_recovery_audit_v1/run_.../events.csv \
    --symbols BTCUSDT,AAVEUSDT,1000PEPEUSDT --plan

Run (network + NEW offline result only):
  python3 reaction_lab_first_touch_historical_recovery_v11.py --project . \
    --audit-events reports/reaction_lab/first_touch_recovery_audit_v1/run_.../events.csv \
    --symbols BTCUSDT,AAVEUSDT,1000PEPEUSDT --max-pages 60 \
    --out reports/reaction_lab/first_touch_historical_recovery_v11/pilot_01 --run
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
import shutil
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BAR = struct.Struct('<qdddd')
MINUTE = 60_000
MAX_BARS = 360
PAGE_LIMIT = 1000  # intentionally conservative /fapi/v1/klines limit
ENDPOINT = 'https://fapi.binance.com/fapi/v1/klines'
SCHEMA_ID = 'FIRST_TOUCH_HISTORICAL_RECOVERY_V11'
ORIGIN = 'BINANCE_USDM_PUBLIC_REST_1M_OFFLINE'
ALLOWED_STATUSES = ('TOUCH_INDECISIVE', 'TOUCH_FAILED')
ALLOWED_STATES = ('STALE_BEFORE_COLLECTOR', 'GAP_EXPIRED', 'MISSING_ENTRY')
ROOT = Path('reports/reaction_lab')
SOURCE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  provider TEXT NOT NULL, PRIMARY KEY(symbol,ts)
);
CREATE TABLE IF NOT EXISTS recovered_paths(
  touch_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
  original_status TEXT NOT NULL, retest_ts INTEGER NOT NULL, entry_ts INTEGER NOT NULL,
  source_ft_state TEXT NOT NULL, source_ft_count INTEGER NOT NULL,
  path_state TEXT NOT NULL, count INTEGER NOT NULL, first_missing_ts INTEGER,
  source_parity_bars INTEGER NOT NULL, path_blob BLOB, provider TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovered_status ON recovered_paths(original_status,path_state);
'''


def utc(ms):
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).isoformat()


def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):
            h.update(part)
    return h.hexdigest()


def ro(path):
    path=Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=30)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db


def normalize_bar(symbol,k,low_ts,high_ts,max_closed_ts):
    if not isinstance(k,list) or len(k)<5:
        raise ValueError(f'{symbol}: malformed Binance kline')
    ts=int(k[0])
    if ts%MINUTE or not low_ts<=ts<=high_ts or ts>max_closed_ts:
        raise ValueError(f'{symbol}: unclosed/out of interval kline {ts}')
    values=tuple(float(a) for a in k[1:5])
    op,hi,lo,cl=values
    if not all(math.isfinite(x) and x>0 for x in values) or hi<max(op,lo,cl) or lo>min(op,hi,cl):
        raise ValueError(f'{symbol}: invalid OHLC {ts}')
    return (ts,op,hi,lo,cl)


def verify_prefix(blob,count,entry):
    n=int(count)
    if not 0<=n<=MAX_BARS:
        raise ValueError('invalid FT path count')
    if n==0:
        if blob:raise ValueError('FT path has blob but zero count')
        return []
    if not blob:raise ValueError('FT prefix is missing')
    raw=zlib.decompress(blob)
    if len(raw)!=n*BAR.size:
        raise ValueError('FT prefix blob/count mismatch')
    out=[]
    for i in range(n):
        b=BAR.unpack_from(raw,i*BAR.size)
        t,op,hi,lo,cl=b
        if t!=int(entry)+i*MINUTE or not all(math.isfinite(x) and x>0 for x in b[1:]) or hi<max(op,lo,cl) or lo>min(op,hi,cl):
            raise ValueError('noncontinuous/bad FT prefix')
        out.append(b)
    return out


def bars_equal(a,b):
    return a[0]==b[0] and all(math.isclose(a[i],b[i],rel_tol=1e-10,abs_tol=1e-12) for i in range(1,5))


def load_selected(audit_csv,ft_path,symbols,asof_ms):
    """Trust only matching original identities in the CURRENT read-only DB."""
    allowed={s.strip().upper() for s in symbols if s.strip()}
    if not allowed:raise ValueError('At least one symbol is required')
    if not Path(audit_csv).is_file():raise FileNotFoundError(audit_csv)
    with Path(audit_csv).open(encoding='utf-8',newline='') as f:
        reader=csv.DictReader(f)
        needed={'touch_key','symbol','side','status','retest_ts','entry_ts','ft_state','audit_state'}
        if not needed.issubset(reader.fieldnames or []):
            raise ValueError('Invalid Recovery Audit V1 CSV columns')
        frozen=[r for r in reader if r['symbol'].upper() in allowed and r['status'] in ALLOWED_STATUSES
                and r['ft_state'] in ALLOWED_STATES and r['audit_state']=='NO_AUTO_MATCH']
    if len({r['touch_key'] for r in frozen})!=len(frozen):
        raise ValueError('Duplicate touch_key in frozen audit CSV')
    selected=[]
    rejected=Counter()
    with closing(ro(ft_path)) as ft:
        required={'touch_key','symbol','side','status','retest_ts','entry_ts','state','count','path_blob','retest_close'}
        cols={r['name'] for r in ft.execute('PRAGMA table_info(paths)')}
        if not required.issubset(cols):raise ValueError('First Touch SQLite schema incompatible')
        for old in frozen:
            row=ft.execute('''SELECT touch_key,symbol,side,status,retest_ts,entry_ts,
                           state,count,path_blob,retest_close FROM paths WHERE touch_key=?''',
                           (old['touch_key'],)).fetchone()
            if row is None:
                rejected['NOT_FOUND']+=1;continue
            row=dict(row)
            if any(str(row[key])!=str(old[key]) for key in ('symbol','side','status','retest_ts','entry_ts')):
                rejected['IDENTITY_CHANGED']+=1;continue
            if row['state'] not in ALLOWED_STATES:
                rejected['STATE_NO_LONGER_PENDING']+=1;continue
            if (row['side'] not in ('LONG','SHORT') or int(row['entry_ts'])!=int(row['retest_ts'])+MINUTE
                    or int(row['entry_ts']) % MINUTE):
                rejected['INVALID_ENTRY_IDENTITY']+=1;continue
            if int(row['entry_ts']) + MAX_BARS*MINUTE > int(asof_ms)//MINUTE*MINUTE:
                rejected['NOT_YET_MATURE']+=1;continue
            try:
                row['source_bars']=verify_prefix(row['path_blob'],row['count'],row['entry_ts'])
            except (ValueError,zlib.error,struct.error):
                rejected['BAD_FT_PREFIX']+=1;continue
            # Do not carry blobs in output metadata; source_bars used only for parity.
            del row['path_blob']
            selected.append(row)
    return selected,dict(sorted(rejected.items())),len(frozen)


def merge_spans(spans):
    result=[]
    for symbol,parts in sorted(spans.items()):
        if not parts:continue
        parts=sorted(parts)
        a,b=parts[0]
        for x,y in parts[1:]:
            if x<=b:
                b=max(b,y)
            else:
                result.append((symbol,a,b))
                a,b=x,y
        result.append((symbol,a,b))
    return result


def plan_requests(rows):
    """Download full forward paths to verify existing prefix independently."""
    spans=defaultdict(list)
    for r in rows:
        start=int(r['entry_ts'])
        spans[r['symbol']].append((start,start+MAX_BARS*MINUTE))
    merged=merge_spans(spans)
    pages=[]
    for symbol,start,end in merged:
        a=start
        while a<end:
            b=min(end,a+PAGE_LIMIT*MINUTE)
            pages.append((symbol,a,b))
            a=b
    return merged,pages


def http_fetch(symbol,start,end_exclusive,timeout=30,endpoint=ENDPOINT):
    q=urllib.parse.urlencode({'symbol':symbol,'interval':'1m','startTime':start,
                             'endTime':end_exclusive-MINUTE,'limit':PAGE_LIMIT})
    req=urllib.request.Request(endpoint+'?'+q,headers={'User-Agent':'FirstTouchOfflineHistoricalRecovery/1.1'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            data=json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code} from Binance for {symbol} at {utc(start)}. Stopped; no retry storm.') from exc
    except (urllib.error.URLError,TimeoutError) as exc:
        raise RuntimeError(f'Connectivity error for {symbol} {utc(start)}: {exc}') from exc
    if not isinstance(data,list):
        raise ValueError(f'Unexpected Binance response for {symbol} at {utc(start)}: {str(data)[:140]}')
    return data


def fetch_bars(pages,*,fetcher=http_fetch,pause=0.3,timeout=30,asof_ms=None):
    if asof_ms is None:asof_ms=int(time.time()*1000)
    # Last closed bar starts at previous minute UTC, never the current forming candle.
    max_closed_ts=(int(asof_ms)//MINUTE-1)*MINUTE
    data=defaultdict(dict)
    details=[]
    for idx,(symbol,start,end) in enumerate(pages,1):
        if end-MINUTE>max_closed_ts:
            raise ValueError('Planned request includes a candle that is not closed')
        payload=fetcher(symbol,start,end,timeout=timeout)
        if len(payload)>PAGE_LIMIT:
            raise ValueError(f'{symbol}: provider returned >{PAGE_LIMIT} bars')
        seen=set()
        for raw in payload:
            bar=normalize_bar(symbol,raw,start,end-MINUTE,max_closed_ts)
            if bar[0] in seen:
                raise ValueError(f'{symbol}: duplicate candle in one API response')
            seen.add(bar[0])
            old=data[symbol].get(bar[0])
            if old is not None and not bars_equal(old,bar):
                raise ValueError(f'{symbol}: conflicting Binance candles at {bar[0]}')
            data[symbol][bar[0]]=bar
        details.append({'symbol':symbol,'start_utc':utc(start),'end_exclusive_utc':utc(end),
                        'expected_bars':(end-start)//MINUTE,'received_bars':len(payload)})
        if idx==1 or idx%10==0 or idx==len(pages):
            print(f'FT_HIST_FETCH progress={idx}/{len(pages)} symbol={symbol} unique_symbol_minutes={sum(map(len,data.values()))}',flush=True)
        if pause>0 and idx<len(pages):time.sleep(pause)
    return data,details


def reconstruct(event,market):
    entry=int(event['entry_ts'])
    prefix=event['source_bars']
    collection=market.get(event['symbol'],{})
    parity=0
    for existing in prefix:
        other=collection.get(existing[0])
        if other is None:
            return [],'PREFIX_NOT_RETRIEVED',0,existing[0]
        if not bars_equal(existing,other):
            return [],'OHLC_CONFLICT',parity,existing[0]
        parity+=1
    bars=[]
    for i in range(MAX_BARS):
        ts=entry+i*MINUTE
        value=collection.get(ts)
        if value is None:
            return bars,'PARTIAL' if bars else 'NO_START_BAR',parity,ts
        bars.append(value)
    return bars,'FULL_360',parity,None


def runner_outcome(event,bars,state):
    """Use exact same outcome engine/column naming as Runner Discovery All Touches V1."""
    from reaction_lab_runner_discovery_all_touches_v1 import FIELDS, TARGETS, as_positive, outcomes_for_path
    row=dict.fromkeys(FIELDS)
    row.update(touch_key=event['touch_key'],symbol=event['symbol'],side=event['side'],
               first_touch_status=event['status'],retest_ts=event['retest_ts'],
               date_utc=utc(event['retest_ts'])[:10],entry_ts=event['entry_ts'],
               collector_state=event['state'],collector_count=event['count'],
               path_source=ORIGIN if bars else 'NO_HISTORICAL_PATH',
               path_count=len(bars),path_quality='CONTIGUOUS_VALID' if bars and state in ('FULL_360','PARTIAL')
               else state,
               retest_close=event['retest_close'])
    # This pilot reconstructs 360m; partials stay censored, not 'not runner'.
    if bars:
        row['next_open']=bars[0][1]
        for basis,price in (('retest',as_positive(event['retest_close'])),('open',as_positive(bars[0][1]))):
            if price is None:continue
            hits,decisions,mfe180,mae180=outcomes_for_path(bars,event['side'],price)
            row[f'{basis}_mfe_180']=mfe180
            row[f'{basis}_mae_180']=mae180
            for tp in TARGETS:
                row[f'{basis}_time_to_run_{tp}pct']=hits[tp]
                row[f'{basis}_tp{tp}_result']=decisions[tp][0]
                row[f'{basis}_tp{tp}_minute']=decisions[tp][1]
    return row


def make_reports(events,market,out_dir,fetch_details,audit_path,rejected,frozen_count,merged,pages):
    """All writes isolated under a brand-new report directory."""
    from reaction_lab_runner_discovery_all_touches_v1 import FIELDS
    out_dir=Path(out_dir)
    out_dir.mkdir(parents=True,exist_ok=False)
    newdb=out_dir/'recovered_paths.sqlite3'
    stats=Counter()
    gaps=[]
    with closing(sqlite3.connect(newdb)) as dst:
        dst.executescript(SOURCE_SCHEMA)
        for sym,ts_map in sorted(market.items()):
            dst.executemany('''INSERT INTO candles(symbol,ts,open,high,low,close,provider)
                  VALUES (?,?,?,?,?,?,?)''',[(sym,*b,ORIGIN) for _,b in sorted(ts_map.items())])
        with (out_dir/'events_outcomes.csv').open('x',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS)
            writer.writeheader()
            for e in events:
                bars,state,parity,first_missing=reconstruct(e,market)
                if state=='OHLC_CONFLICT':
                    stats['OHLC_CONFLICT']+=1
                stats['events']+=1
                stats[f'state_{state}']+=1
                stats[f'status_{e["status"]}']+=1
                if state=='FULL_360':stats[f'full_{e["status"]}']+=1
                if parity:stats['events_with_source_prefix_parity']+=1;stats['verified_source_prefix_bars']+=parity
                blob=zlib.compress(b''.join(BAR.pack(*b) for b in bars),6) if bars else None
                dst.execute('''INSERT INTO recovered_paths(touch_key,symbol,side,original_status,retest_ts,
                     entry_ts,source_ft_state,source_ft_count,path_state,count,first_missing_ts,
                     source_parity_bars,path_blob,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (e['touch_key'],e['symbol'],e['side'],e['status'],e['retest_ts'],e['entry_ts'],
                      e['state'],e['count'],state,len(bars),first_missing,parity,blob,ORIGIN))
                writer.writerow(runner_outcome(e,bars,state))
                if first_missing is not None:
                    gaps.append({'touch_key':e['touch_key'],'symbol':e['symbol'],
                                 'status':e['status'],'state':state,'first_missing_utc':utc(first_missing)})
        dst.commit()
    with (out_dir/'missing_events.csv').open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['touch_key','symbol','status','state','first_missing_utc'])
        w.writeheader();w.writerows(gaps)
    with (out_dir/'requests.json').open('x',encoding='utf-8') as f:
        json.dump(fetch_details,f,ensure_ascii=False,indent=2)
    manifest={'schema':SCHEMA_ID,'provider':ORIGIN,'source_audit_events':str(Path(audit_path).resolve()),
              'audit_events_sha256':sha256_file(audit_path),'created_utc':utc(int(time.time()*1000)),
              'first_touch_db_mutated':False,'collector_interrupted':False,
              'selected_from_frozen_audit':frozen_count,'excluded_from_live_db':rejected,
              'selected_events':len(events),'selected_symbols':sorted({e['symbol'] for e in events}),
              'merged_windows':len(merged),'planned_pages':len(pages),
              'received_pages':len(fetch_details),'distinct_1m_bars':sum(map(len,market.values())),
              'counts':dict(sorted(stats.items())),
              'source':{
                'outcome_logic':'reaction_lab_runner_discovery_all_touches_v1.outcomes_for_path',
                'timestamp_start':'retest_ts + 60000 (next candle)',
                'full_horizon_requirement':'360 consecutive closed 1m',
                'initial_status':'unchanged from First Touch',
                'OHLC_conflict':'rejected/censored, never force-filled',
                'TP_vs_SL_same_minute':'AMBIGUOUS',
                'provenance':'Binance Futures public USD-M 1m REST'},
              'files_sha256':{}}
    for n in ('events_outcomes.csv','missing_events.csv','requests.json','recovered_paths.sqlite3'):
        manifest['files_sha256'][n]=sha256_file(out_dir/n)
    (out_dir/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--audit-events',type=Path,required=True)
    p.add_argument('--symbols',required=True,help='Comma separated; explicit pilot scope required')
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true',help='Read only: no network, no writes')
    mode.add_argument('--run',action='store_true',help='Fetch public Binance 1m and produce NEW isolated files')
    p.add_argument('--out',type=Path,help='NEW destination directory, required with --run')
    p.add_argument('--max-pages',type=int,default=60)
    p.add_argument('--max-events',type=int,default=300)
    p.add_argument('--max-symbol-minutes',type=int,default=70000)
    p.add_argument('--pause',type=float,default=0.3)
    p.add_argument('--timeout',type=float,default=30)
    args=p.parse_args(argv)
    if args.run and not args.out:p.error('--run requires --out')
    if args.plan and args.out:p.error('--out only with --run')
    if any(x<=0 for x in (args.max_pages,args.max_events,args.max_symbol_minutes,args.timeout)) or args.pause<0:
        p.error('Invalid safety bounds')
    project=args.project.resolve()
    audit=args.audit_events if args.audit_events.is_absolute() else project/args.audit_events
    output=args.out if args.out is None or args.out.is_absolute() else project/args.out
    if output is not None and output.exists():p.error(f'Refusing overwrite: {output}')
    now=int(time.time()*1000)
    events,rejected,frozen=load_selected(audit,project/ROOT/'first_touch_paths_v2.sqlite3',
                                         args.symbols.split(','),now)
    merged,pages=plan_requests(events)
    symbol_minutes=sum((e-s)//MINUTE for _,s,e in merged)
    info={'schema':SCHEMA_ID,'selected_events':len(events),'eligible_in_audit':frozen,
          'rejected_current_state':rejected,'symbols':sorted({e['symbol'] for e in events}),
          'merged_windows':len(merged),'unique_symbol_minutes':symbol_minutes,
          'estimated_pages_1000':len(pages),'source_db_read_only':True}
    print('FT_HIST_RECOVERY_PLAN '+json.dumps(info,ensure_ascii=False,sort_keys=True),flush=True)
    if not events:return 0
    if len(events)>args.max_events or len(pages)>args.max_pages or symbol_minutes>args.max_symbol_minutes:
        raise SystemExit('SAFETY_LIMIT: reduce --symbols or batch; no requests made. '+json.dumps(info))
    if args.plan:return 0
    assert output is not None
    if shutil.disk_usage(project).free<4*1024**3:
        raise SystemExit('Insufficient free disk (<4 GB); refused historical download')
    if not (project/'reaction_lab_runner_discovery_all_touches_v1.py').is_file():
        raise SystemExit('Missing current Runner Discovery all touches outcome module')
    data,requests=fetch_bars(pages,pause=args.pause,timeout=args.timeout,asof_ms=now)
    manifest=make_reports(events,data,output,requests,audit,rejected,frozen,merged,pages)
    print('FT_HIST_RECOVERY_RESULT '+json.dumps({
        'out_dir':str(output),'counts':manifest['counts'],
        'distinct_1m_bars':manifest['distinct_1m_bars'],
        'planned_pages':manifest['planned_pages']},ensure_ascii=False,sort_keys=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
