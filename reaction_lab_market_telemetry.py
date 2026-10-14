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
from urllib.error import HTTPError

API='https://fapi.binance.com'
SCHEMA='''CREATE TABLE IF NOT EXISTS snapshots(
 symbol TEXT NOT NULL,observed_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL,
 features_json TEXT NOT NULL,quality_json TEXT NOT NULL,
 PRIMARY KEY(symbol,observed_ms));
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON snapshots(symbol,received_ms);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS priority_watches(symbol TEXT PRIMARY KEY,score REAL NOT NULL,
 detail_json TEXT NOT NULL,last_seen_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_watches_seen ON priority_watches(last_seen_ms);
'''


def now():return int(time.time()*1000)


class BinanceRateLimited(RuntimeError):
    def __init__(self, code, retry_seconds=600):
        super().__init__(f'Binance HTTP {code} rate-limited; cooldown requested')
        self.retry_seconds=retry_seconds


def client_get(path,params):
    url=API+path+'?'+urlencode(params)
    try:
        with urlopen(Request(url,headers={'User-Agent':'reaction-lab-research/1.0'}),timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (418,429):
            try:delay=int(exc.headers.get('Retry-After','600'))
            except (ValueError,TypeError):delay=600
            raise BinanceRateLimited(exc.code,max(600,delay)) from exc
        raise


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
            metric_keys={'oi':('oi_contracts',),'funding':('funding_rate',),
                         'depth':('orderbook_spread_bps','orderbook_imbalance_5'),
                         'taker':('taker_buy_ratio_5m','taker_buy_ratio_15m')}
            available=all(data.get(k) is not None for k in metric_keys[name])
            quality[name]={'observed_ms':moment,'available':available}
            if not available:quality[name]['reason']='missing_or_invalid_fields'
        except BinanceRateLimited:
            raise
        except Exception as e:
            quality[name]={'available':False,'error':repr(e)[:160]}
    return data,quality


def open_store(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=25)
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=25000')
    db.executescript(SCHEMA)
    return db


def _meta(db,key,default='0'):
    row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    return row[0] if row else default


def _put_meta(db,key,value):
    db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',(key,str(value)))


def select_symbols(db, symbols, max_symbols, now_ms, *, max_priority=10,
                   watch_valid_ms=5*60_000, revisit_ms=90_000):
    """Fair rotation reserves >=50% of budget for whole universe."""
    if not symbols:return [],0,0
    reserve=min(max_priority,max_symbols//2)
    seen=dict(db.execute('SELECT symbol,MAX(received_ms) FROM snapshots GROUP BY symbol'))
    candidates=[]
    for symbol,score,when in db.execute(
        'SELECT symbol,score,last_seen_ms FROM priority_watches WHERE last_seen_ms>=? ORDER BY score DESC',
        (now_ms-watch_valid_ms,)):
        if symbol not in symbols:continue
        if now_ms-seen.get(symbol,0)<revisit_ms:continue
        candidates.append((symbol,score,seen.get(symbol,0)))
    selected=[s for s,score,previous in sorted(candidates,key=lambda r:(-r[1],r[2],r[0]))[:reserve]]
    priority_n=len(selected)
    index=int(_meta(db,'next_cursor'))%len(symbols)
    walked=0
    while len(selected)<max_symbols and walked<len(symbols):
        sym=symbols[(index+walked)%len(symbols)]
        walked+=1
        if sym not in selected:selected.append(sym)
    next_cursor=(index+walked)%len(symbols)
    return selected,next_cursor,priority_n


def refresh_watches(db,project,symbols, *, budget=60,probe=None,now_ms=None):
    moment=now() if now_ms is None else int(now_ms)
    if not symbols:return {'scanned':0,'watches':0,'error':None}
    offset=int(_meta(db,'priority_cursor'))%len(symbols)
    chosen=[symbols[(offset+i)%len(symbols)] for i in range(min(budget,len(symbols)))]
    try:
        if probe is None:
            from reaction_lab_structural_priority import refresh_priority
            watches=refresh_priority(project,chosen,moment,budget=len(chosen))
        else:watches=probe(project,chosen,moment)
        with db:
            db.executemany('DELETE FROM priority_watches WHERE symbol=?',[(s,) for s in chosen])
            for symbol,detail in watches.items():
                if symbol in chosen:
                    db.execute('INSERT OR REPLACE INTO priority_watches VALUES (?,?,?,?)',
                        (symbol,float(detail['score']),json.dumps(detail,allow_nan=False),moment))
            _put_meta(db,'priority_cursor',(offset+len(chosen))%len(symbols))
            db.execute('DELETE FROM priority_watches WHERE last_seen_ms<?',(moment-5*60_000,))
        return {'scanned':len(chosen),'watches':len(watches),'error':None}
    except Exception as exc:
        return {'scanned':0,'watches':0,'error':repr(exc)[:180]}


def poll_once(project,db_path=None,max_symbols=20,get=client_get,*,priority_enabled=False,
              priority_scan_symbols=60,max_priority=10,priority_probe=None):
    from reaction_lab_continuous_scanner import get_symbols
    root=Path(project);symbols=get_symbols(root)
    dest=Path(db_path) if db_path else root/'reports/reaction_lab/telemetry_v1.sqlite3'
    attempted=completed=failed=0;limited=False
    with open_store(dest) as db:
        cooldown=int(_meta(db,'cooldown_until_ms'))
        if cooldown>now():
            return {'cooldown_remaining_s':int((cooldown-now())/1000),
                    'symbols_attempted':0,'snapshots':0,'missing':0,'universe':len(symbols)}
        probe_report={'scanned':0,'watches':0,'error':None}
        if priority_enabled:
            probe_report=refresh_watches(db,root,symbols,budget=priority_scan_symbols,probe=priority_probe)
        chosen,next_cursor,priority_n=select_symbols(db,symbols,min(max_symbols,len(symbols)),now(),
                                                     max_priority=max_priority if priority_enabled else 0)
        for sym in chosen:
            try:data,quality=poll_symbol(sym,get)
            except BinanceRateLimited as exc:
                with db:_put_meta(db,'cooldown_until_ms',now()+exc.retry_seconds*1000)
                limited=True;break
            attempted+=1
            timestamp=now()
            if data and any(q.get('available') is True for q in quality.values()):
                with db:
                    db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)',
                        (sym,timestamp,timestamp,json.dumps(data,allow_nan=False),json.dumps(quality)))
                completed+=1
            else:failed+=1
        if not limited:
            with db:_put_meta(db,'next_cursor',next_cursor)
    return {'symbols_attempted':attempted,'snapshots':completed,'missing':failed,
            'next_cursor':next_cursor if not limited else None,'universe':len(symbols),
            'priority_selected':priority_n,'priority_scan':probe_report,'rate_limited':limited}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.')
    p.add_argument('--db')
    p.add_argument('--check',action='store_true')
    p.add_argument('--once',action='store_true')
    p.add_argument('--loop',action='store_true')
    p.add_argument('--max-symbols',type=int,default=20)
    p.add_argument('--poll-seconds',type=int,default=45)
    p.add_argument('--priority-enabled',action='store_true')
    p.add_argument('--priority-scan-symbols',type=int,default=60)
    p.add_argument('--max-priority',type=int,default=10)
    a=p.parse_args()
    if a.check:
        from reaction_lab_continuous_scanner import get_symbols
        n=len(get_symbols(Path(a.project)))
        print(json.dumps({'universe':n,'calls_per_full_universe':n*4,
            'max_requests_per_cycle':a.max_symbols*4,'priority_enabled':a.priority_enabled,
            'network':'OFF (check only)','storage':'separate existing telemetry SQLite'}));return
    if not (a.once or a.loop):p.error('Use --check, --once or --loop')
    if not(1<=a.max_symbols<=25 and a.poll_seconds>=30 and
           1<=a.priority_scan_symbols<=100 and 0<=a.max_priority<=a.max_symbols//2):
        p.error('Out-of-range safe API / priority budget (max 25 symbols, >=30s interval)')
    while True:
        result=poll_once(a.project,a.db,a.max_symbols,priority_enabled=a.priority_enabled,
                         priority_scan_symbols=a.priority_scan_symbols,max_priority=a.max_priority)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if not a.loop:break
        time.sleep(a.poll_seconds)

if __name__=='__main__':main()
