#!/usr/bin/env python3
"""Optional prospective USD-M liquidation-snapshot feed; no trading.

Official Binance !forceOrder@arr is a SNAPSHOT stream (at most one per
symbol/second), NOT an exhaustive record of all forced liquidations. Values
are observed USD notionals only, never treated as complete exchange totals.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import time

WS='wss://fstream.binance.com/market/ws/!forceOrder@arr'
EXTRA='''
CREATE TABLE IF NOT EXISTS liquidations(
 symbol TEXT NOT NULL,event_ms INTEGER NOT NULL,received_ms INTEGER NOT NULL,
 side TEXT,notional_usdt REAL NOT NULL,trade_id TEXT NOT NULL,
 PRIMARY KEY(symbol,event_ms,trade_id));
CREATE INDEX IF NOT EXISTS idx_liq_symbol_time ON liquidations(symbol,received_ms);
CREATE TABLE IF NOT EXISTS liquidation_heartbeats(received_ms INTEGER PRIMARY KEY);
'''

def now():return int(time.time()*1000)

def parse(msg,received_ms):
    obj=json.loads(msg) if isinstance(msg,(str,bytes)) else msg
    if isinstance(obj,dict) and 'data' in obj:obj=obj['data']
    batch=obj if isinstance(obj,list) else [obj]
    out=[]
    for item in batch:
        if not isinstance(item,dict) or item.get('e')!='forceOrder':continue
        if str(item.get('st','1'))!='1':continue  # USD-M only in merged streams
        order=item.get('o') or {}
        try:
            symbol=str(order['s']).upper();event_ms=int(item['E'])
            qty=float(order.get('z') or order.get('l') or order.get('q'))
            price=float(order.get('ap') or order.get('p'))
            if not symbol or qty<=0 or price<=0 or event_ms>received_ms+5000:continue
            ident=str(order.get('T') or event_ms)+'/'+str(order.get('S','?'))+'/'+str(qty)
            out.append((symbol,event_ms,received_ms,str(order.get('S')),qty*price,ident))
        except (KeyError,ValueError,TypeError):continue
    return out

def open_db(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=30000')
    db.executescript(EXTRA)
    return db

def persist_message(db,message,moment):
    rows=parse(message,moment)
    with db:
        db.executemany('INSERT OR IGNORE INTO liquidations VALUES (?,?,?,?,?,?)',rows)
        db.execute('INSERT OR IGNORE INTO liquidation_heartbeats VALUES (?)',(moment,))
    return len(rows)

async def loop(project,db_path=None):
    try:import websockets
    except ImportError as exc:raise RuntimeError('Install websockets for liquidation observer') from exc
    dbpath=Path(db_path) if db_path else Path(project)/'reports/reaction_lab/telemetry_v1.sqlite3'
    with open_db(dbpath) as db:
        while True:
            try:
                async with websockets.connect(WS,ping_interval=20,ping_timeout=10) as ws:
                    print(json.dumps({'liquidation_stream':'CONNECTED','at':now()}),flush=True)
                    with db:
                        db.execute('INSERT OR IGNORE INTO liquidation_heartbeats VALUES (?)',(now(),))
                    while True:
                        try:
                            msg=await asyncio.wait_for(ws.recv(),timeout=30)
                            added=persist_message(db,msg,now())
                            if added:print(json.dumps({'liquidation_snapshots':added}),flush=True)
                        except asyncio.TimeoutError:
                            pong=await ws.ping()
                            await asyncio.wait_for(pong,timeout=8)
                            with db:
                                db.execute('INSERT OR IGNORE INTO liquidation_heartbeats VALUES (?)',(now(),))
                        # Bounded disk usage; causal per-event aggregate stays frozen in deep DB.
                        with db:
                            cutoff=now()-3*3600_000
                            db.execute('DELETE FROM liquidations WHERE received_ms<?',(cutoff,))
                            db.execute('DELETE FROM liquidation_heartbeats WHERE received_ms<?',(cutoff,))
            except Exception as exc:
                print(json.dumps({'liquidation_stream':'RECONNECT','reason':repr(exc)[:200]}),flush=True)
                await asyncio.sleep(15)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--project',default='.')
    p.add_argument('--db')
    p.add_argument('--check',action='store_true')
    p.add_argument('--loop',action='store_true')
    args=p.parse_args()
    if args.check:
        print(json.dumps({'stream':WS,'mode':'OPTIONAL','network':'OFF (check only)','accurate_total_liquidations':False}));return
    if not args.loop:p.error('Use --check or --loop')
    asyncio.run(loop(args.project,args.db))

if __name__=='__main__':main()
