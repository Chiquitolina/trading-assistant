#!/usr/bin/env python3
"""Research-only PRE-RETEST telemetry prioritization from already closed Redis candles.

NOT the canonical REACTION detector. Heuristically identifies confirmed 15m 3x3
pivots with a 0.20% departure and no observed first return within 0.10%.
Used solely for API polling order; cannot create/filter REACTION events.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

MINUTE=60_000


def normalized_bars(frame, *, timeframe_ms, now_ms):
    if frame is None or getattr(frame,'empty',True): return []
    result=[]
    for r in frame.to_dict('records'):
        try:
            ts=int(r['timestamp'])
            if ts+timeframe_ms>now_ms: continue
            o,h,l,c=(float(r[k]) for k in ('open','high','low','close'))
            if not all(map(math.isfinite,(o,h,l,c))) or not (0<l<=min(o,c)<=max(o,c)<=h):continue
            result.append({'timestamp':ts,'open':o,'high':h,'low':l,'close':c})
        except (KeyError,ValueError,TypeError,OverflowError):continue
    return list({r['timestamp']:r for r in sorted(result,key=lambda b:b['timestamp'])}.values())


def priority_watches(bars_15m, bars_1m, now_ms, *, max_age_minutes=360, max_distance_pct=4.0):
    """Return open WATCHes; timestamps only from already CLOSED candles.

    Strict 3-left/3-right pivots are a priority heuristic, not canonical swing IDs.
    Incomplete paths are ignored, not assumed to have missed their first retest.
    """
    candles15=normalized_bars(bars_15m,timeframe_ms=15*MINUTE,now_ms=now_ms)
    candles1=normalized_bars(bars_1m,timeframe_ms=MINUTE,now_ms=now_ms)
    if len(candles15)<7 or len(candles1)<10:return []
    last1=candles1[-1]
    results=[]
    for i in range(3,len(candles15)-3):
        pivot=candles15[i]
        confirm_at=candles15[i+3]['timestamp']+15*MINUTE
        if confirm_at>last1['timestamp'] or now_ms-confirm_at>max_age_minutes*MINUTE:continue
        # Require contiguous HTF bars spanning the pivot confirmation.
        if any(candles15[j]['timestamp']-candles15[j-1]['timestamp']!=15*MINUTE for j in range(i-2,i+4)):continue
        left=candles15[i-3:i]; right=candles15[i+1:i+4]
        sides=[]
        if all(pivot['low']<x['low'] for x in left+right): sides.append(('LONG',pivot['low']))
        if all(pivot['high']>x['high'] for x in left+right): sides.append(('SHORT',pivot['high']))
        if not sides:continue
        path=[r for r in candles1 if r['timestamp']>=confirm_at]
        # We cannot prove there was no earlier retest if Redis 1m is truncated.
        if not path or path[0]['timestamp']!=confirm_at:continue
        if any(b['timestamp']-a['timestamp']!=MINUTE for a,b in zip(path,path[1:])):continue
        for side,level in sides:
            departure=None
            for k,b in enumerate(path):
                if (side=='LONG' and b['high']>=level*1.002) or (side=='SHORT' and b['low']<=level*.998):
                    departure=k;break
            if departure is None:continue
            after=path[departure+1:]
            # Once returned to the swing level, that watch is no longer prior to its first retest.
            touched=any(b['low']<=level*1.001 for b in after) if side=='LONG' else any(b['high']>=level*.999 for b in after)
            if touched:continue
            close=last1['close']
            distance=abs(close/level-1)*100
            if distance>max_distance_pct:continue
            age=(now_ms-confirm_at)/MINUTE
            score=round(100-20*distance-.02*age,4)
            results.append({'side':side,'level':level,'confirm_ms':confirm_at,
                            'distance_pct':round(distance,4),'score':score})
    results.sort(key=lambda r:(-r['score'],-r['confirm_ms']))
    return results


def refresh_priority(project, symbols, now_ms, *, budget=60):
    """Redis read-only. Exceptions propagate to caller for safe rotation fallback."""
    import redis
    from pathlib import Path
    from dashboard.services.geometry_scanner_data_service import GeometryScannerDataService
    # Same Redis connection parameters as the existing scanner.
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(project)/'.env')
    except ImportError:pass
    client=redis.Redis(host=os.getenv('REDIS_HOST','127.0.0.1'),
        port=int(os.getenv('REDIS_PORT','6379')),
        db=int(os.getenv('REDIS_DB','0')),socket_timeout=5)
    service=GeometryScannerDataService(redis_client=client)
    result={}
    try:
        client.ping()
        for symbol in symbols[:budget]:
            try:
                m15=service.get_closed_candles(symbol=symbol,timeframe='15m',limit=40)
                m1=service.get_closed_candles(symbol=symbol,timeframe='1m',limit=400)
                watches=priority_watches(m15,m1,now_ms)
                if watches:result[symbol]=watches[0]
            except Exception:
                continue  # budgeted priority probe must not interrupt REST rotation
    finally:
        try:client.close()
        except Exception:pass
    return result
