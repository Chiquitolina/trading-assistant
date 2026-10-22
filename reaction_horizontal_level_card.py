"""Horizontal Level Rich Card V1 — causal interpretation of an EXISTING Atlas V2 zone.

No new level detection. The V2 primary zone is frozen before the reaction.
PRE: only closed 1m candles strictly before retest_ts and the frozen zone.
KNOWN: current 1m retest candle, only AFTER close; never a pre-signal.
POST: the existing First-Touch replay labels, joined by event_key in the UI.

Most importantly a SHORT at support is not automatically a defended level:
it is a possible bearish breakdown. A SHORT at resistance can be a defense.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

MIN = 60_000
ROOT = Path('reports/reaction_lab')
PRE_BARS = 15


def _num(x):
    try:
        y=float(x)
        return y if math.isfinite(y) else None
    except (ValueError,TypeError):
        return None


def _position(price,low,high):
    if price is None:return 'UNKNOWN'
    if price>high:return 'ABOVE'
    if price<low:return 'BELOW'
    return 'IN_ZONE'


def setup_from_prices(*,side,zone_low,zone_high,pre_bars):
    """Causal setup from 15 1m PRE candles and one frozen level.

    If final close sits in the zone, trace the most recent PRE close outside
    that zone; never claim a direction from an inside-zone close alone.
    """
    side=str(side).upper()
    lo,hi=_num(zone_low),_num(zone_high)
    if side not in ('LONG','SHORT') or lo is None or hi is None or lo<=0 or lo>hi:
        return {'setup':'DESCONOCIDO','origin':'UNKNOWN','pre_location':'UNKNOWN',
                'pre_close':None,'pre_bars_used':0}
    if len(pre_bars)<5:
        return {'setup':'DESCONOCIDO','origin':'UNKNOWN','pre_location':'UNKNOWN',
                'pre_close':None,'pre_bars_used':len(pre_bars)}
    closes=[_num(b[4]) for b in pre_bars]
    if any(x is None or x<=0 for x in closes):
        return {'setup':'DESCONOCIDO','origin':'UNKNOWN','pre_location':'UNKNOWN',
                'pre_close':None,'pre_bars_used':len(pre_bars)}
    pre_loc=_position(closes[-1],lo,hi)
    origin=pre_loc
    if origin=='IN_ZONE':
        for c in reversed(closes[:-1]):
            candidate=_position(c,lo,hi)
            if candidate!='IN_ZONE':
                origin=candidate
                break
    if origin=='IN_ZONE':
        setup='EN_ZONA_SIN_ORIGEN'
    elif (side=='LONG' and origin=='ABOVE') or (side=='SHORT' and origin=='BELOW'):
        setup='DEFENSA_POTENCIAL'
    else:
        setup='RUPTURA_POTENCIAL'
    return {'setup':setup,'origin':origin,'pre_location':pre_loc,
            'pre_close':closes[-1],'pre_bars_used':len(pre_bars)}


def reaction_candle_label(*,side,setup,zone_low,zone_high,retest_bar):
    """1m KNOWN outcome; NOT a verified multi-candle breakout or a future trade outcome."""
    lo,hi=_num(zone_low),_num(zone_high)
    if lo is None or hi is None or retest_bar is None or setup=='DESCONOCIDO':
        return 'SIN_VELA_CERRADA'
    _,op,h,l,c,*_=retest_bar
    h,l,c=map(_num,(h,l,c))
    if h is None or l is None or c is None:return 'SIN_VELA_CERRADA'
    touch=l<=hi and h>=lo
    if setup=='DEFENSA_POTENCIAL':
        if side=='LONG':
            if c<lo:return 'NIVEL_PERDIDO_EN_CIERRE'
            if touch and c>hi:return 'RECHAZO_FAVORABLE_1M'
        else:
            if c>hi:return 'NIVEL_PERDIDO_EN_CIERRE'
            if touch and c<lo:return 'RECHAZO_FAVORABLE_1M'
        return 'TOQUE_SIN_DEFENSA_CONFIRMADA' if touch else 'SIN_CONTACTO_1M'
    if setup=='RUPTURA_POTENCIAL':
        if (side=='LONG' and c>hi) or (side=='SHORT' and c<lo):
            return 'CRUCE_INICIAL_1M'  # NOT accepted breakout
        return 'PRUEBA_SIN_QUIEBRE' if touch else 'SIN_CONTACTO_1M'
    if side=='LONG' and c>hi:return 'SALE_POR_ARRIBA_1M'
    if side=='SHORT' and c<lo:return 'SALE_POR_ABAJO_1M'
    return 'DENTRO_O_INDEFINIDO_1M'


def classify_one(event,pre_bars,retest_bar):
    """No access to outcome, first_touch_minute, or POST; deterministic."""
    side=str(event['side']).upper()
    lo,hi=event.get('primary_zone_low'),event.get('primary_zone_high')
    if not bool(event.get('all_covered',0)):
        return {'setup':'DESCONOCIDO','origin':'UNKNOWN','pre_location':'UNKNOWN',
                'pre_close':None,'known':'SIN_HISTORICO','pre_bars_used':0,
                'pre_contacts_5':None,'pre_contacts_15':None,'pressure_pre':False}
    pre=setup_from_prices(side=side,zone_low=lo,zone_high=hi,pre_bars=pre_bars)
    known=reaction_candle_label(side=side,setup=pre['setup'],zone_low=lo,
                                   zone_high=hi,retest_bar=retest_bar)
    # Contacts are PRE-only and descriptive; 3 touches do NOT prove impending breakout.
    lo,hi=_num(lo),_num(hi)
    touch_flags=[]
    if lo is not None and hi is not None:
        for bar in pre_bars:
            hh,ll=_num(bar[2]),_num(bar[3])
            touch_flags.append(bool(hh is not None and ll is not None and ll<=hi and hh>=lo))
    pre_contacts_5=sum(touch_flags[-5:]) if len(touch_flags)>=5 else None
    pre_contacts_15=sum(touch_flags) if len(touch_flags)==PRE_BARS else None
    pressure_pre=(pre['setup']=='RUPTURA_POTENCIAL' and
                  pre_contacts_5 is not None and pre_contacts_5>=3)
    return {**pre,'known':known,'pre_contacts_5':pre_contacts_5,
            'pre_contacts_15':pre_contacts_15,'pressure_pre':pressure_pre}


def _read_event_bars(con,symbol,retest_ts):
    """Return bars only if contiguous and strictly prior; never fill gaps."""
    r=int(retest_ts)
    rows=con.execute('''SELECT open_ts,open,high,low,close FROM candles
             WHERE symbol=? AND timeframe='1m' AND open_ts>=? AND open_ts<=?
             ORDER BY open_ts''',(str(symbol),r-PRE_BARS*MIN,r)).fetchall()
    pre=[x for x in rows if int(x[0])<r]
    retest=next((x for x in rows if int(x[0])==r),None)
    if len(pre)!=PRE_BARS or any(int(b[0])!=r-(PRE_BARS-i)*MIN for i,b in enumerate(pre)):
        return [],retest
    return pre,retest


def enrich_events(project,events):
    """Attach PRE setup and KNOWN retest candle, reading frozen Context read-only.

    Atlas V2 must already have chosen the primary zone PRE. Outcomes are not
    consulted and may be joined independently by the dashboard.
    """
    required=('event_key','symbol','side','retest_ts','all_covered',
              'primary_zone_low','primary_zone_high')
    missing=[c for c in required if c not in events.columns]
    if missing:raise ValueError('Atlas V2 incompleto: '+', '.join(missing))
    if events.event_key.duplicated().any() or events.event_key.isna().any():
        raise ValueError('event_key repetido o nulo; se bloquea cruce')
    if events.empty:return events.copy()
    path=Path(project)/ROOT/'reaction_full_context_v1.sqlite3'
    if not path.is_file():raise FileNotFoundError(str(path))
    results=[]
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as con:
        for ev in events.to_dict('records'):
            pre,ret=_read_event_bars(con,ev['symbol'],ev['retest_ts'])
            out=classify_one(ev,pre,ret)
            results.append({'event_key':ev['event_key'],'setup_pre':out['setup'],
                'arrival_origin_pre':out['origin'],'last_close_location_pre':out['pre_location'],
                'last_pre_1m_close':out['pre_close'],'known_candle_label':out['known'],
                'pre_bars_used':out['pre_bars_used'],
                'pre_contacts_5':out['pre_contacts_5'],
                'pre_contacts_15':out['pre_contacts_15'],
                'pressure_pre':out['pressure_pre']})
    annotations=pd.DataFrame(results)
    return events.merge(annotations,on='event_key',validate='one_to_one')


def groups_summary(events):
    """One row per causal setup; NO_HIT and missing excluded only from win rate."""
    out=[]
    for setup,g in events.groupby('setup_pre',dropna=False):
        tp=int(g.outcome.eq('TP_FIRST').sum())
        sl=int(g.outcome.eq('SL_FIRST').sum())
        out.append({'Situación ANTES':setup,'REACTIONs':len(g),'TP':tp,'SL':sl,
                    'NO_HIT':int(g.outcome.eq('NO_HIT').sum()),
                    'Sin resultado':int(len(g)-g.outcome.isin(['TP_FIRST','SL_FIRST','NO_HIT']).sum()),
                    'TP resueltos (%)':round(100*tp/(tp+sl),1) if tp+sl else None,
                    'N resueltos':tp+sl})
    return pd.DataFrame(out)
