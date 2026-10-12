#!/usr/bin/env python3
"""Append-only causal research context for Reaction Lab's observed 15m REACTIONs.

Uses the original dashboard's pure RSI, relative-volume and HTF Room helpers
compiled from source, WITHOUT importing Streamlit.  All candles are filtered
as-of event.  Market breadth is calculated from the existing market universe,
using the previous COMPLETED 1h / 4h bars.  No trading, REST or legacy writes.

IMPORTANT: This snapshot is an independently versioned research data layer,
NOT an assertion of parity with legacy market_regime.py / V2 portfolio.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time

TIMES = {'1m':60000, '5m':300000, '15m':900000, '30m':1800000,
         '1h':3600000, '4h':14400000}
LIMITS = {'1m':400,'5m':400,'15m':288,'30m':240,'1h':168,'4h':150}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS contexts (
  event_key TEXT PRIMARY KEY, known_ts INTEGER NOT NULL,
  captured_at_ms INTEGER NOT NULL, asof_complete INTEGER NOT NULL,
  status TEXT NOT NULL, source_hash TEXT NOT NULL,
  context_json TEXT NOT NULL, candles_json TEXT NOT NULL,
  notes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breadth (
  timeframe TEXT NOT NULL, boundary_ts INTEGER NOT NULL,
  computed_at_ms INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
  PRIMARY KEY(timeframe,boundary_ts)
);
CREATE TABLE IF NOT EXISTS followups (
  event_key TEXT PRIMARY KEY, path_state TEXT NOT NULL, path_bars INTEGER NOT NULL,
  last_bar_ts INTEGER, updated_ms INTEGER NOT NULL, followup_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, at_ms INTEGER NOT NULL,
  checked INTEGER NOT NULL, inserted INTEGER NOT NULL,
  ready INTEGER NOT NULL, partial INTEGER NOT NULL,
  error TEXT
);
'''
HELPERS = ('_lh_interval_ms','_lh_closed_tail',
           '_reaction_driver_rsi_from_candles',
           '_reaction_driver_relative_volume_from_candles',
           '_reaction_driver_swing_distances')


def now_ms(): return int(time.time()*1000)


def safe_json(obj):
    import numpy as np
    import pandas as pd
    def clean(v):
        if isinstance(v, dict): return {str(k):clean(x) for k,x in v.items()}
        if isinstance(v, (list,tuple)):return [clean(x) for x in v]
        if isinstance(v, np.generic):return clean(v.item())
        if isinstance(v, pd.Timestamp):return v.isoformat()
        if v is None:return None
        if isinstance(v,float) and not math.isfinite(v):return None
        if isinstance(v, (int,float,bool,str)):return v
        try:
            if pd.isna(v):return None
        except (ValueError,TypeError):pass
        return str(v)
    return json.dumps(clean(obj),ensure_ascii=False,separators=(',',':'),allow_nan=False)


def load_canonical(app_path):
    """Extract unmodified dashboard analytical functions with exact AST signature."""
    import pandas as pd
    import numpy as np
    from engine.live.research.swing_detector import SwingDetector
    source=app_path.read_text(encoding='utf-8-sig')
    defs={n.name:n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef)}
    missing=[name for name in HELPERS if name not in defs]
    if missing:raise RuntimeError(f'Missing canonical dashboard helpers: {missing}')
    body=[]
    for name in HELPERS:
        node=defs[name]
        node.decorator_list=[]
        body.append(node)
    compiled=ast.fix_missing_locations(ast.Module(body=body,type_ignores=[]))
    scope={'pd':pd,'np':np,'SwingDetector':SwingDetector}
    exec(compile(compiled,str(app_path),'exec'),scope)
    digest=hashlib.sha256(ast.dump(compiled,include_attributes=False).encode()).hexdigest()[:20]
    return scope,digest


def get_database(db,write=False):
    if not write:
        return sqlite3.connect(f'file:{db.resolve().as_posix()}?mode=ro',uri=True,timeout=20)
    db.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(db,timeout=20)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=20000')
    con.executescript(SCHEMA)
    return con


def closed_at(df, tf, known):
    import pandas as pd
    if df is None or df.empty:return pd.DataFrame()
    data=df.copy()
    data['timestamp']=pd.to_numeric(data['timestamp'],errors='coerce')
    data=data.dropna(subset=['timestamp']).sort_values('timestamp',kind='stable')
    data=data.drop_duplicates('timestamp',keep='last')
    # open_ts + interval <= known_timestamp, absolutely no look-ahead
    return data[data['timestamp'] + TIMES[tf] <= known].copy()


def nearest_return(data, boundary, tf):
    """Return bar close / preceding completed bar close - 1, in percent."""
    if data is None or data.empty:return None
    closes=dict(zip(data['timestamp'].astype(int),data['close'].astype(float)))
    p=closes.get(int(boundary));q=closes.get(int(boundary)-TIMES[tf]);
    return (float(p)/float(q)-1)*100 if p is not None and q not in (None,0) else None


def last_boundary(known, tf):
    interval=TIMES[tf]
    return (int(known)//interval-1)*interval


def market_snapshot(con, service, symbols, tf, boundary, known):
    """Frozen cross-sectional market breadth, one SQLite row per closed boundary."""
    old=con.execute('SELECT snapshot_json FROM breadth WHERE timeframe=? AND boundary_ts=?',
                    (tf,boundary)).fetchone()
    if old:return json.loads(old[0])
    returns=[];btc=None; returns_by_symbol={}; volume_by_symbol={}; relative_volume_by_symbol={}
    for sym in symbols:
        candles=service.get_closed_candles(symbol=sym,timeframe=tf,limit=LIMITS[tf])
        candles=closed_at(candles,tf,known)
        val=nearest_return(candles,boundary,tf)
        if val is not None and math.isfinite(val):
            returns.append(val)
            returns_by_symbol[sym]=val
            if sym=='BTCUSDT':btc=val
        if tf=='4h' and not candles.empty and int(boundary) in candles['timestamp'].astype(int).values:
            series=candles.set_index('timestamp')
            volume=float(series.loc[boundary,'volume'])
            volume_by_symbol[sym]=volume
            last=series.loc[series.index < boundary,'volume'].tail(42)
            if len(last)>0 and float(last.median())>0:
                relative_volume_by_symbol[sym]=volume/float(last.median())
    import statistics
    count=len(returns);pos=sum(x>0 for x in returns)
    result={'timeframe':tf,'boundary_timestamp':boundary,
            'market_breadth_pct':100*pos/count if count else None,
            'positive_symbols':pos,'valid_symbols':count,
            'market_coverage_pct':100*count/max(1,len(symbols)),
            'market_median_return_pct':statistics.median(returns) if count else None,
            'btc_return_pct':btc,'universe_count':len(symbols),
            'returns_by_symbol':returns_by_symbol,'volume_by_symbol':volume_by_symbol,
            'relative_volume_by_symbol':relative_volume_by_symbol}
    con.execute('INSERT OR IGNORE INTO breadth VALUES (?,?,?,?)',
                (tf,boundary,now_ms(),safe_json(result)))
    return result


def infer_market_alignment(side,breadth,btc):
    if breadth is None or btc is None:return 'UNKNOWN'
    if (side=='LONG' and breadth>=50 and btc>=0) or (side=='SHORT' and breadth<50 and btc<0):return 'TAILWIND'
    if (side=='LONG' and breadth<50 and btc<0) or (side=='SHORT' and breadth>=50 and btc>=0):return 'HEADWIND'
    return 'MIXED'


def transition_label(delta):
    if delta is None:return 'UNKNOWN'
    return 'EXPANDING' if delta>=5 else 'CONTRACTING' if delta<=-5 else 'STABLE'


def context_for_event(event, service, methods, fingerprint, con, universe, sector_map=None, max_age_minutes=400):
    """Compute once, never rewrite: as-of data or explicit missing markers."""
    import pandas as pd
    import numpy as np
    event_key,symbol,side,retest,known,first_seen,observation,causal_json=event
    raw=json.loads(causal_json)
    context={**raw, 'event_key':event_key,'symbol':symbol,'signal':side,
             'known_ts':known,'first_seen_ms':first_seen,
             'reaction_scanner_observation_type':observation,
             'candidate_v1_event_key':event_key,'candidate_v2_event_key':event_key,
             'context_schema_version':1,'context_helper_fingerprint':fingerprint}
    missing=[]; candle_archive={}
    sector_map=sector_map or {}
    context['sector']=sector_map.get(symbol)
    now=now_ms();age=now-known
    if age>max_age_minutes*60000:missing.append('LATE_CAPTURE: event older than 1m retention margin')
    asof_input={}
    for tf in TIMES:
        df=service.get_closed_candles(symbol=symbol,timeframe=tf,limit=LIMITS[tf])
        asof=closed_at(df,tf,known)
        asof_input[tf]=asof
        if asof.empty:
            missing.append(f'{tf}: historical as-of candles missing')
            candle_archive[tf]=[]
            continue
        columns=['timestamp','open','high','low','close','volume']
        candle_archive[tf]=asof[[c for c in columns if c in asof.columns]].to_dict('records')
        if len(asof)>1:
            gaps=asof['timestamp'].diff().dropna()
            if gaps.ne(TIMES[tf]).any():
                missing.append(f'{tf}: noncontiguous closed candle history')
        # Data availability diagnostic: full last closed boundary is represented
        latest_available=int(asof['timestamp'].iloc[-1])+TIMES[tf]
        latest_expected=int(known//TIMES[tf])*TIMES[tf]
        if latest_available < latest_expected:
            missing.append(f'{tf}: missing latest completed candle at known_ts')
    for tf in ('1m','5m','15m','1h'):
        val=methods['_reaction_driver_rsi_from_candles'](asof_input[tf],known,TIMES[tf],period=14)
        context[f'rsi14_{tf}']=float(val) if pd.notna(val) else None
        context[f'aligned_rsi_extreme_{tf}']=bool(
            pd.notna(val) and ((side=='LONG' and val<=30) or (side=='SHORT' and val>=70)))
    context['aligned_rsi_extreme_count']=sum(int(context[f'aligned_rsi_extreme_{tf}']) for tf in ('1m','5m','15m','1h'))
    for tf in ('1m','5m','15m','1h'):
        if context[f'rsi14_{tf}'] is None:missing.append(f'{tf}: RSI14 unavailable or incomplete')
    for tf in ('1m','5m','15m','1h'):
        val=methods['_reaction_driver_relative_volume_from_candles'](asof_input[tf],known,TIMES[tf])
        context[f'rel_volume_{tf}']=float(val) if pd.notna(val) else None
    price=raw.get('retest_close') or raw.get('retest_price')
    candidates=[];same=[];confluence=0
    for tf in ('30m','1h','4h'):
        df=asof_input[tf]
        points=[]
        if not df.empty:
            from engine.live.research.swing_detector import SwingDetector
            points=SwingDetector(left_bars=5,right_bars=5,min_prominence_pct=0.0).detect_all(df.to_dict('records'))
        if price is not None and float(price)>0:
            d=methods['_reaction_driver_swing_distances'](points,known,TIMES[tf],float(price),side)
        else:
            d={'same_side_distance_pct':np.nan,'opposing_room_pct':np.nan}
            missing.append('retest_price unavailable: room cannot be computed')
        room=d.get('opposing_room_pct'); dist=d.get('same_side_distance_pct')
        context[f'opposing_room_{tf}_pct']=float(room) if pd.notna(room) else None
        context[f'same_swing_dist_{tf}_pct']=float(dist) if pd.notna(dist) else None
        for field in ('opposing_swing_price','opposing_swing_pivot_timestamp',
                      'opposing_swing_confirmed_timestamp','opposing_swing_actionable_timestamp'):
            val=d.get(field)
            context[f'{field}_{tf}']=float(val) if pd.notna(val) else None
        if pd.notna(dist):
            same.append((float(dist),tf));confluence+=int(float(dist)<=0.5)
        if pd.notna(room):candidates.append((float(room),tf,d))
    context['htf_confluence_count_0_50']=confluence
    context['nearest_htf_same_swing_pct']=min(same)[0] if same else None
    context['nearest_htf_same_swing_tf']=min(same)[1] if same else None
    if candidates:
        best=min(candidates,key=lambda x:x[0]); context['nearest_opposing_room_pct']=best[0]
        context['nearest_opposing_swing_tf']=best[1]
        d=best[2]
        for field in ('opposing_swing_price','opposing_swing_pivot_timestamp','opposing_swing_confirmed_timestamp','opposing_swing_actionable_timestamp'):
            context['nearest_'+field]=d.get(field)
    else:
        context['nearest_opposing_room_pct']=None
        context['nearest_opposing_swing_tf']=None
    for tf in ('1h','4h'):
        boundary=last_boundary(known,tf)
        # Cache once per completed market boundary. This also freezes market-universe coverage.
        current=market_snapshot(con,service,universe,tf,boundary,known)
        # Fetch previous state only when it can still be reconstructed; do not rewrite archived rows.
        prev=market_snapshot(con,service,universe,tf,boundary-TIMES[tf],known)
        sym_ret=nearest_return(asof_input[tf],boundary,tf)
        btc=current.get('btc_return_pct')
        context[f'return_pct_{tf}']=sym_ret
        context[f'btc_return_pct_{tf}']=btc
        if sym_ret is not None and btc is not None:
            residual=sym_ret-btc
            context[f'symbol_strength_vs_btc_{tf}']=residual
            context[f'side_adjusted_strength_vs_btc_{tf}']=residual*(1 if side=='LONG' else -1)
        else:
            context[f'symbol_strength_vs_btc_{tf}']=None
            context[f'side_adjusted_strength_vs_btc_{tf}']=None
        breadth=current.get('market_breadth_pct')
        context[f'market_breadth_{tf}']=breadth
        context[f'positive_symbols_{tf}']=current.get('positive_symbols')
        context[f'market_coverage_{tf}']=current.get('market_coverage_pct')
        context[f'market_median_return_pct_{tf}']=current.get('market_median_return_pct')
        if tf=='4h':
            # Freeze raw inputs for eventual exact V1 market regime parity.
            returns=current.get('returns_by_symbol',{})
            context['return_pct_4h_from_breadth']=returns.get(symbol)
            context['relative_volume_4h_snapshot']=current.get('relative_volume_by_symbol',{}).get(symbol)
            context['volume_4h_snapshot']=current.get('volume_by_symbol',{}).get(symbol)
            if sector_map and symbol in sector_map:
                sector_returns=[v for s,v in returns.items() if sector_map.get(s)==sector_map[symbol]]
                if sector_returns:
                    import statistics
                    context['sector_median_return_4h_research']=statistics.median(sector_returns)
                    context['sector_relative_btc_4h_research']=(statistics.median(sector_returns)-btc) if btc is not None else None
                    context['sector_covered_symbols_4h']=len(sector_returns)
        prior=prev.get('market_breadth_pct')
        delta=breadth-prior if breadth is not None and prior is not None else None
        context[f'breadth_delta_{tf}']=delta
        context[f'breadth_direction_{tf}']=transition_label(delta)
        previous_btc=prev.get('btc_return_pct')
        context[f'btc_return_delta_{tf}']=btc-previous_btc if btc is not None and previous_btc is not None else None
        context[f'Market alignment {tf}']=infer_market_alignment(side,breadth,btc)
        context[f'flow_{tf}_candle_timestamp']=boundary
        if current.get('market_coverage_pct',0)<95:
            missing.append(f'{tf} breadth: incomplete market coverage ({current.get("valid_symbols")}/{len(universe)})')
    context['Market alignment']=context.get('Market alignment 4h')
    context['flow_candle_timestamp']=context.get('flow_4h_candle_timestamp')
    context['market_context_available']=bool(context.get('side_adjusted_strength_vs_btc_4h') is not None and
                                             context.get('market_breadth_4h') is not None)
    # Independent sector normalizations need the canonical v1_market_regime module.
    # They are NOT fabricated here; the complete OHLCV as-of evidence is archived.
    for field in ('sector_strength_vs_btc_4h','relative_volume_4h',
                  'return_rank_pct_4h','volume_rank_pct_4h'):
        context[field]=None
    missing.append('Sector ranks/relative volume 4h: require canonical market regime adapter')
    complete=(not missing and context['market_context_available'])
    return {'context':context,'candles':candle_archive,'notes':missing,
            'complete':complete,'status':'READY' if complete else 'PARTIAL'}


def outcomes_from_bars(bars, side):
    """Post-entry outcomes ONLY, separated from immutable causal context."""
    if not bars: return {'path_bars':0,'path_available':False}
    entry=float(bars[0][1]); direction=1 if side=='LONG' else -1
    result={'path_bars':len(bars),'path_available':True,'entry_price':entry,
            'entry_timestamp':int(bars[0][0])}
    for horizon in (1,3,5,15,30,60,120,180,360):
        if len(bars)<horizon:
            result[f'mfe_{horizon}m_pct']=None
            result[f'mae_{horizon}m_pct']=None
            continue
        portion=bars[:horizon]
        favorable=[((float(b[2])/entry-1)*100 if direction==1 else (1-float(b[3])/entry)*100) for b in portion]
        adverse=[((1-float(b[3])/entry)*100 if direction==1 else (float(b[2])/entry-1)*100) for b in portion]
        result[f'mfe_{horizon}m_pct']=max(favorable)
        result[f'mae_{horizon}m_pct']=max(adverse)
    # A reference TP0.5/SL3 benchmark; never infer same-minute ordering.
    outcome='PENDING';exit_ts=None;ambiguous=False
    for b in bars[:180]:
        high=float(b[2]);low=float(b[3]);ts=int(b[0])
        tp=(high>=entry*1.005) if direction==1 else (low<=entry*0.995)
        sl=(low<=entry*0.97) if direction==1 else (high>=entry*1.03)
        if tp and sl: outcome='AMBIGUOUS';ambiguous=True;exit_ts=ts;break
        if tp or sl:outcome='TP' if tp else 'SL';exit_ts=ts;break
    if outcome=='PENDING' and len(bars)>=180:
        outcome='TIME_EXIT';exit_ts=int(bars[179][0])
    result['tp_0_5_sl_3_h180_outcome']=outcome
    result['tp_0_5_sl_3_h180_exit_ts']=exit_ts
    result['tp_sl_same_bar_ambiguous']=ambiguous
    return result


def followup_once(con,paths_db,limit=2000):
    from reaction_lab_auto_paths import decode_path
    if not paths_db.is_file():return {'updated':0,'available':False}
    n=0
    with closing(get_database(paths_db)) as src:
        # Process latest rolling events; avoid 5k stale historical paths on each cycle.
        records=src.execute("""SELECT e.event_key,e.side,e.state,e.count,e.last_ts,e.path_blob
            FROM events e JOIN event_sources s ON e.event_key=s.event_key
            WHERE s.source='continuous_scan' ORDER BY e.first_seen_utc_ms DESC LIMIT ?""",
            (limit,)).fetchall()
    for key,side,state,count,last_ts,blob in records:
        old=con.execute('SELECT path_bars,last_bar_ts FROM followups WHERE event_key=?',(key,)).fetchone()
        if old is not None and int(old[0])==int(count) and old[1]==last_ts:continue
        try:bars=decode_path(blob)
        except (ValueError,TypeError):continue
        payload=outcomes_from_bars(bars,side)
        with con:
            con.execute("""INSERT INTO followups VALUES (?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET path_state=excluded.path_state,
                 path_bars=excluded.path_bars,last_bar_ts=excluded.last_bar_ts,
                 updated_ms=excluded.updated_ms,followup_json=excluded.followup_json""",
                (key,state,int(count),last_ts,now_ms(),safe_json(payload)))
        n+=1
    return {'updated':n,'available':True,'events_checked':len(records)}


def load_events(con,db,limit,project):
    """Only post-cutover; newest unprocessed first, never starve fresh data."""
    if not db.is_file():return []
    with closing(get_database(db)) as scan:
        cycle=scan.execute('SELECT MIN(started_ms) FROM cycles WHERE error IS NULL').fetchone()
        cutoff=int(cycle[0]) if cycle and cycle[0] is not None else None
        if cutoff is None:return []
        manifest=project/'reports/reaction_lab/continuous_v1_cohort.json'
        if manifest.is_file():
            try:
                saved=int(json.loads(manifest.read_text(encoding='utf-8'))['start_ms'])
                if saved>=cutoff:cutoff=saved
            except (OSError,KeyError,ValueError,TypeError):pass
        events=scan.execute("""SELECT event_key,symbol,side,retest_ts,known_ts,
          first_seen_ms,observation_type,causal_json FROM events
          WHERE known_ts>=? ORDER BY known_ts DESC""",(cutoff,)).fetchall()
    have={x[0] for x in con.execute('SELECT event_key FROM contexts')}
    return [r for r in events if r[0] not in have][:limit]


def process_once(project,db,scanner_db,limit=100,force=False,min_free_mb=1024):
    import redis
    from dashboard.services.geometry_scanner_data_service import GeometryScannerDataService
    from reaction_lab_continuous_scanner import get_symbols
    if shutil.disk_usage(project).free < min_free_mb*1024*1024:
        raise RuntimeError('Low disk capacity: refusing context writes')
    methods,digest=load_canonical(project/'dashboard/app.py')
    symbols=get_symbols(project)
    client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),port=int(os.getenv('REDIS_PORT','6379')),
                       db=int(os.getenv('REDIS_DB','0')),socket_timeout=10)
    client.ping()
    service=GeometryScannerDataService(redis_client=client)
    con=get_database(db,write=True)
    checked=ready=partial=inserted=0
    sector_map={}
    try:
        from engine.live.data.market_sector_catalog import MarketSectorCatalog
        sector_catalog=MarketSectorCatalog(required_symbols=symbols)
        for sector,group in sector_catalog.symbols_by_primary_sector().items():
            for symbol in group:sector_map[str(symbol).upper()]=str(sector)
    except Exception:
        pass  # Unknown sector is preserved as missing, never guessed.
    try:
        events=load_events(con,scanner_db,limit,project)
        for ev in sorted(events,key=lambda row: row[4],reverse=True):
            checked+=1
            if con.execute('SELECT 1 FROM contexts WHERE event_key=?',(ev[0],)).fetchone() and not force:continue
            # We refuse to overwrite first successful causal capture; separate schema migration is required.
            if force and con.execute('SELECT 1 FROM contexts WHERE event_key=?',(ev[0],)).fetchone():continue
            try:
                bundle=context_for_event(ev,service,methods,digest,con,symbols,sector_map)
                with con:
                    con.execute('''INSERT OR IGNORE INTO contexts VALUES(?,?,?,?,?,?,?,?,?)''',(
                        ev[0],int(ev[4]),now_ms(),int(bundle['complete']),bundle['status'],digest,
                        safe_json(bundle['context']),safe_json(bundle['candles']),safe_json(bundle['notes'])))
                inserted+=1;ready+=int(bundle['complete']);partial+=int(not bundle['complete'])
            except Exception as exc:
                # Transient errors remain pending for retry; no fabricated snapshot.
                print(safe_json({'event_key':ev[0], 'context_error':f'{type(exc).__name__}: {exc}'}),flush=True)
                partial+=1
        with con:
            con.execute('INSERT INTO runs(at_ms,checked,inserted,ready,partial,error) VALUES(?,?,?,?,?,NULL)',
                        (now_ms(),checked,inserted,ready,partial))
        outcomes=followup_once(con,project/'reports/reaction_lab/auto_paths_v1.sqlite3')
        statuses=dict(con.execute('SELECT status,COUNT(*) FROM contexts GROUP BY status'))
        return {'followups':outcomes,'checked':checked,'inserted':inserted,'ready_now':ready,'partial_now':partial,
                'total_contexts':sum(statuses.values()),'states':statuses,'source_hash':digest}
    finally:con.close()


def status(db):
    if not db.is_file():return {'database_exists':False}
    with closing(get_database(db)) as con:
        states=dict(con.execute('SELECT status,COUNT(*) FROM contexts GROUP BY status'))
        latest=con.execute('SELECT at_ms,checked,inserted,ready,partial FROM runs ORDER BY id DESC LIMIT 1').fetchone()
    return {'database_exists':True,'states':states,'last_run':latest}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--db',type=Path)
    parser.add_argument('--scanner-db',type=Path)
    parser.add_argument('--limit',type=int,default=250)
    parser.add_argument('--poll-seconds',type=int,default=60)
    parser.add_argument('--min-free-mb',type=int,default=1024)
    mode=parser.add_mutually_exclusive_group(required=True)
    for option in ('check','once','loop','status'):mode.add_argument('--'+option,action='store_true')
    args=parser.parse_args(argv)
    project=args.project.resolve();db=(args.db or project/'reports/reaction_lab/causal_context_v1.sqlite3').resolve()
    scanner=(args.scanner_db or project/'reports/reaction_lab/scanner_events_v1.sqlite3').resolve()
    if args.status:
        print(json.dumps(status(db),ensure_ascii=False,indent=2));return 0
    sys.path.insert(0,str(project))
    try:
        from dotenv import load_dotenv
        load_dotenv(project/'.env')
    except ImportError:pass
    scope,digest=load_canonical(project/'dashboard/app.py')
    print('REACTION CONTEXT:',digest,'SCANNER:',scanner,'DB:',db,flush=True)
    if args.check:
        print('CHECK PASS: canonical helpers available; no DB mutations');return 0
    db.parent.mkdir(parents=True,exist_ok=True)
    with db.with_suffix(db.suffix+'.lock').open('a+') as fh:
        fcntl.flock(fh,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stopped=False
        def stop(_signal,_frame):
            nonlocal stopped
            stopped=True
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        while not stopped:
            begin=time.monotonic()
            try:
                report=process_once(project,db,scanner,limit=args.limit,min_free_mb=args.min_free_mb)
                print(safe_json(report),flush=True)
            except Exception as ex:
                print(safe_json({'error':f'{type(ex).__name__}: {ex}'}),flush=True)
                if args.once:return 1
            if args.once:break
            while not stopped and time.monotonic()<begin+max(1,args.poll_seconds):time.sleep(0.25)
    return 0

if __name__=='__main__':raise SystemExit(main())
