#!/usr/bin/env python3
"""Optional PUBLIC Binance USD-M microstructure observer (opt-in, research only).

Polls a bounded rotating symbol batch. Snapshots are recorded at the time the
HTTP response is RECEIVED, not retrospectively assigned to a REACTION. This
service MUST have been running before a signal to supply causal data. No API
keys, exchange orders, stream duplication, or backfill of order books.

Liquidations and per-trade tick CVD are NOT fabricated; 1m taker-buy kline
volume supplies an explicitly labeled aggregate taker delta approximation.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import Request,urlopen

API='https://fapi.binance.com'
SCHEMA='''CREATE TABLE IF NOT EXISTS snapshots(
 symbol TEXT NOT NULL,observed_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL,
 features_json TEXT NOT NULL,quality_json TEXT NOT NULL,
 PRIMARY KEY(symbol,observed_ms));
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON snapshots(symbol,received_ms);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''


def now():return int(time.time()*1000)


def client_get(path,params):
    url=API+path+'?'+urlencode(params)
    with urlopen(Request(url,headers={'User-Agent':'reaction-lab-research/1.0'}),timeout=8) as response:
        return json.load(response)


def valid_float(v):
    try:
        f=float(v)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError,OverflowError):return None


def derive_taker(klines, captured_ms):
    """All source candles must be CLOSED before snapshot observation."""
    bars=[]
    for row in klines:
        try:
            op=int(row[0]); end=int(row[6]);total=float(row[5]);buy=float(row[9])
            if end>=captured_ms or not (0<=buy<=total) or not math.isfinite(total):continue
            bars.append((op,total,buy))
        except (ValueError,TypeError,IndexError):continue
    bars.sort()
    if not bars:return {}
    data={}
    for window in (5,15):
        last=bars[-window:]
        if len(last)!=window or any(last[i][0]-last[i-1][0]!=60_000 for i in range(1,len(last))):continue
        buy=sum(t[2] for t in last);volume=sum(t[1] for t in last)
        sell=volume-buy
        data[f'taker_buy_volume_{window}m']=buy
        data[f'taker_sell_volume_{window}m']=sell
        data[f'taker_delta_{window}m']=buy-sell
        data[f'taker_buy_ratio_{window}m']=buy/volume if volume>0 else None
        if window==15:data['cvd_15m']=buy-sell
    data['taker_source']='binance_futures_closed_1m_klines_taker_buy_base'
    return data


def derive_book(book):
    bids=[(valid_float(x[0]),valid_float(x[1])) for x in book.get('bids',[])[:5]]
    asks=[(valid_float(x[0]),valid_float(x[1])) for x in book.get('asks',[])[:5]]
    if not bids or not asks or any(p is None or q is None or p<=0 or q<0 for p,q in bids+asks):return {}
    bid=sum(p*q for p,q in bids);ask=sum(p*q for p,q in asks)
    spread=asks[0][0]-bids[0][0]
    mid=(asks[0][0]+bids[0][0])/2
    return {'orderbook_spread_bps':spread/mid*10000 if mid>0 else None,
            'orderbook_imbalance_5':(bid-ask)/(bid+ask) if bid+ask>0 else None,
            'orderbook_bid_depth_usdt':bid,'orderbook_ask_depth_usdt':ask,
            'orderbook_top5_source':'binance_futures_rest_depth_snapshot'}


def poll_symbol(symbol,get=client_get):
    """Bounded calls; each field records actual receipt time in SQLite."""
    data={};quality={}
    specs=[('oi','/fapi/v1/openInterest',{'symbol':symbol}),
           ('funding','/fapi/v1/premiumIndex',{'symbol':symbol}),
           ('depth','/fapi/v1/depth',{'symbol':symbol,'limit':5}),
           ('taker','/fapi/v1/klines',{'symbol':symbol,'interval':'1m','limit':18})]
    for name,path,params in specs:
        try:
            response=get(path,params)
            moment=now()
            if name=='oi':data['oi_contracts']=valid_float(response.get('openInterest'))
            elif name=='funding':data['funding_rate']=valid_float(response.get('lastFundingRate'))
            elif name=='depth':data.update(derive_book(response))
            elif name=='taker':data.update(derive_taker(response,moment))
            quality[name]={'observed_ms':moment,'available':True}
        except Exception as e:
            quality[name]={'available':False,'error':repr(e)[:160]}
    return data,quality


def open_store(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=25)
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=25000')
    db.executescript(SCHEMA)
    return db


def poll_once(project,db_path=None,max_symbols=25,get=client_get):
    from reaction_lab_continuous_scanner import get_symbols
    root=Path(project)
    symbols=get_symbols(root)
    dest=Path(db_path) if db_path else root/'reports/reaction_lab/telemetry_v1.sqlite3'
    completed=0;failed=0
    with open_store(dest) as db:
        cursor=db.execute("SELECT value FROM meta WHERE key='next_cursor'").fetchone()
        index=int(cursor[0]) if cursor else 0
        chosen=[symbols[(index+i)%len(symbols)] for i in range(min(max_symbols,len(symbols)))]
        for sym in chosen:
            data,quality=poll_symbol(sym,get)
            timestamp=now()  # never pretend fields existed earlier than response
            if data:
                with db:
                    db.execute('''INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)''',
                        (sym,timestamp,timestamp,json.dumps(data,allow_nan=False),json.dumps(quality)))
                completed+=1
            else:failed+=1
        with db:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('next_cursor',str((index+len(chosen))%len(symbols))))
    return {'symbols_attempted':len(chosen),'snapshots':completed,'missing':failed,
            'next_cursor':(index+len(chosen))%len(symbols),'universe':len(symbols)}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--project',default='.')
    p.add_argument('--db')
    p.add_argument('--check',action='store_true')
    p.add_argument('--once',action='store_true')
    p.add_argument('--loop',action='store_true')
    p.add_argument('--max-symbols',type=int,default=25)
    p.add_argument('--poll-seconds',type=int,default=120)
    a=p.parse_args()
    if a.check:
        from reaction_lab_continuous_scanner import get_symbols
        print(json.dumps({'universe':len(get_symbols(Path(a.project))),
            'calls_per_full_universe':len(get_symbols(Path(a.project)))*4,
            'network':'OFF (check only)','storage':'opt-in separate SQLite'}));return
    if not (a.once or a.loop):p.error('Use --check, --once or --loop')
    if a.max_symbols<1 or a.poll_seconds<30:p.error('Invalid polling options')
    while True:
        print(json.dumps(poll_once(a.project,a.db,a.max_symbols)),flush=True)
        if not a.loop:break
        time.sleep(a.poll_seconds)

if __name__=='__main__':main()
