"""Horizontal Level Research: verified 1m candle/zone intersections.

Atlas V2 provides *PRE* causal zones, but its ``known_touch`` predicate is
one-sided and must never be used to assert a 1m candle actually touched a
zone. Here, KNOWN contact is explicitly recomputed from the archived 1m OHLC.
No First Touch outcome influences class assignment.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

MODEL_VERSION = 'horizontal_levels_verified_ohlc_contact_v2'
MIN_CONFIRMED_PIVOTS = 2
MIN_PRE_DEFENSE_EPISODES = 1
NEAR_ATR = 0.5

A = 'A_CONTACTO_CONOCIDO'
B = 'B_CERCA_SIN_CONTACTO'
C = 'C_SIN_NIVEL_IMPORTANTE_CERCA'
UNKNOWN = 'UNKNOWN_HISTORY_O_CONTACTO'
GROUPS = {A:'A · Tocó nivel importante', B:'B · Cerca, sin tocar',
          C:'C · Sin nivel importante cercano', UNKNOWN:'No evaluable'}


def finite(value):
    try:
        f=float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def flag(value):
    if isinstance(value,bool): return value
    if value is None or pd.isna(value): return None
    if isinstance(value,str):
        if value.strip().upper() in ('TRUE','1','YES'): return True
        if value.strip().upper() in ('FALSE','0','NO'): return False
        return None
    n=finite(value)
    return None if n is None else bool(n)


def outcome(value):
    if value is None or pd.isna(value): return 'SIN_RESULTADO'
    v=str(value).strip().upper()
    return v if v in ('TP_FIRST','SL_FIRST','NO_HIT','AMBIGUOUS') else 'SIN_RESULTADO'


def important(zone):
    """PRE-only Atlas qualification; outcome and KNOWN fields are ignored."""
    p=finite(zone['pivot_count'])
    episodes=finite(zone['defense_episodes_pre'])
    formed=finite(zone['formation_closed_ts'])
    retest=finite(zone['retest_ts'])
    return bool(p is not None and p>=MIN_CONFIRMED_PIVOTS and episodes is not None
                and episodes>=MIN_PRE_DEFENSE_EPISODES and formed is not None
                and retest is not None and formed<retest)


def zone_rank(zone):
    """PRE-only rank; used for choosing among equally classified contacts."""
    eps=finite(zone.get('defense_episodes_pre')) or 0
    pivots=finite(zone.get('pivot_count')) or 0
    dist=finite(zone.get('gap_to_zone_atr'))
    return (-eps,-pivots,float('inf') if dist is None else dist,
            str(zone.get('timeframe')),str(zone.get('zone_id')))


def _placebo_close(zone):
    price=finite(zone.get('pre_last_close'))
    atr=finite(zone.get('atr_pre'))
    low=finite(zone.get('zone_low')); high=finite(zone.get('zone_high'))
    if None in (price,atr,low,high) or atr<=0:return None
    token=f"{zone.get('event_key')}|{zone.get('zone_id')}".encode()
    sign=1 if int(hashlib.sha256(token).hexdigest(),16)&1 else -1
    offset=sign*3*atr
    gap=max(low+offset-price,0,price-(high+offset))
    return gap/atr<=NEAR_ATR


def candle_zone_gap(zone,candle_low,candle_high):
    """Absolute gap between *closed* 1m candle's interval and frozen zone.

    A zero gap means actual OHLC intersection (including boundary touch).
    In contrast, a single directional inequality is not a valid touch test.
    """
    lo=finite(zone.get('zone_low'));hi=finite(zone.get('zone_high'))
    if None in (lo,hi,candle_low,candle_high) or lo>hi or candle_low>candle_high:
        return None
    return max(lo-candle_high,candle_low-hi,0.0)


def valid_retest_candle(event):
    """Read-only 1m OHLC must be valid, without manufactured missing bars."""
    o,h,l,c=[finite(event.get(f'known_{key}')) for key in ('open','high','low','close')]
    if None in (o,h,l,c) or min(o,h,l,c)<=0 or l>h or not(l<=o<=h and l<=c<=h):
        return None
    return (l,h)


def load_retest_candles(project,events):
    """Read ONLY the exact retest 1m candles from Full Context SQLite.

    Rows missing from SQLite remain NaN and are classified UNKNOWN, never C.
    SQL uses exact (symbol, open_ts) rather than a nearest/future candle.
    """
    import reaction_structural_level_atlas_v2 as atlas
    root=Path(project)/atlas.v1.lineage.ROOT_REL
    path=root/atlas.v1.lineage.CTX_NAME
    if not path.is_file():
        raise FileNotFoundError(f'Falta Full Context para validar contacto 1m: {path}')
    rows=[]
    uri=path.resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        sql=("SELECT open,high,low,close FROM candles "
             "WHERE symbol=? AND timeframe='1m' AND open_ts=?")
        for e in events.itertuples(index=False):
            hit=connection.execute(sql,(str(e.symbol),int(e.retest_ts))).fetchone()
            rows.append(dict(event_key=e.event_key,
                             known_open=hit[0] if hit else float('nan'),
                             known_high=hit[1] if hit else float('nan'),
                             known_low=hit[2] if hit else float('nan'),
                             known_close=hit[3] if hit else float('nan')))
    fresh=events.drop(columns=['known_open','known_high','known_low','known_close'],errors='ignore')
    return fresh.merge(pd.DataFrame(rows),on='event_key',how='left',validate='one_to_one')


def classify(event,candidates):
    """One group per REACTION, with verified 1m interval intersections.

    PRE proximity uses the previous closed HTF candle as before; A/B/C use
    the complete KNOWN 1m candle, which cannot be used at that candle's open.
    """
    selected=[r for r in candidates if important(r)]
    selected.sort(key=zone_rank)
    atlas_claimed_touch=any(flag(z.get('known_touch')) is True for z in selected)
    complete=flag(event.get('all_covered')) is True
    known=valid_retest_candle(event)
    pre_near=any(finite(z.get('gap_to_zone_atr')) is not None and
                 float(z['gap_to_zone_atr'])<=NEAR_ATR for z in selected)
    pre_bucket=('NO_EVALUABLE' if not complete else
                'PRE_CERCA' if pre_near else 'PRE_LEJOS_O_SIN_NIVEL')
    results=[]
    if complete and known is not None:
        candle_low,candle_high=known
        for z in selected:
            gap=candle_zone_gap(z,candle_low,candle_high)
            atr=finite(z.get('atr_pre'))
            gap_atr=gap/atr if gap is not None and atr is not None and atr>0 else None
            results.append((z,gap,gap_atr))
    if not complete or known is None:
        group=UNKNOWN;chosen=None;chosen_gap_atr=None
    else:
        touching=[r for r in results if r[1] is not None and r[1]<=0]
        nearby=[r for r in results if r[2] is not None and r[2]<=NEAR_ATR]
        invalid=[r for r in results if r[2] is None]
        if touching:
            group=A
            chosen,_,chosen_gap_atr=sorted(touching,key=lambda r:zone_rank(r[0]))[0]
        elif nearby:
            group=B
            chosen,_,chosen_gap_atr=sorted(nearby,key=lambda r:(r[2],zone_rank(r[0])))[0]
        elif invalid:
            group=UNKNOWN;chosen=None;chosen_gap_atr=None
        else:
            group=C;chosen=None;chosen_gap_atr=None
    placebo_near=any(_placebo_close(z) is True for z in selected) if selected and complete else None
    d=dict(event)
    d.update(level_group=group,pre_bucket=pre_bucket,
             important_levels=len(selected),
             atlas_claimed_touch=atlas_claimed_touch,
             atlas_claimed_touch_not_verified=(atlas_claimed_touch and group in (B,C)),
             relevant_zone_id=chosen.get('zone_id') if chosen else None,
             relevant_timeframe=chosen.get('timeframe') if chosen else None,
             relevant_zone_low=chosen.get('zone_low') if chosen else None,
             relevant_zone_high=chosen.get('zone_high') if chosen else None,
             relevant_gap_atr=chosen.get('gap_to_zone_atr') if chosen else None,
             relevant_retest_gap_atr=chosen_gap_atr,
             relevant_episodes=chosen.get('defense_episodes_pre') if chosen else None,
             relevant_pivots=chosen.get('pivot_count') if chosen else None,
             relevant_state=chosen.get('level_state_pre') if chosen else None,
             known_touch_verified=True if group==A else False if group in (B,C) else None,
             placebo_near_pre=placebo_near)
    return d


def build(events,levels,timeframe='TODOS'):
    if timeframe not in ('TODOS','15m','30m'):raise ValueError('Timeframe invalid')
    req_event={'event_key','all_covered','retest_ts','symbol','side','date_utc',
               'known_open','known_high','known_low','known_close'}
    req_level={'event_key','zone_id','pivot_count','defense_episodes_pre','formation_closed_ts',
               'retest_ts','timeframe','gap_to_zone_atr','zone_low','zone_high','atr_pre'}
    missing_e=req_event-set(events.columns)
    missing_l=req_level-set(levels.columns)
    if missing_e or missing_l:
        raise ValueError(f'Atlas V2/contacto 1m columns missing: events={sorted(missing_e)}, levels={sorted(missing_l)}')
    if events.event_key.duplicated().any():raise ValueError('Duplicate event in Atlas V2')
    for z in levels.to_dict('records'):
        a,b=finite(z.get('formation_closed_ts')),finite(z.get('retest_ts'))
        if a is not None and b is not None and a>=b:
            raise ValueError('Future/non-confirmed structural level in Atlas V2')
    chosen=levels if timeframe=='TODOS' else levels.loc[levels.timeframe.eq(timeframe)]
    grouped={k:g.to_dict('records') for k,g in chosen.groupby('event_key',sort=False)}
    records=[classify(e,grouped.get(e['event_key'],[])) for e in events.to_dict('records')]
    return pd.DataFrame(records),chosen.loc[chosen.apply(important,axis=1)].copy()


def wilson(tp,n):
    if n==0:return (None,None)
    z=1.96;p=tp/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return round(100*(center-radius),1),round(100*(center+radius),1)


def group_summary(df,col='level_group',min_resolved=30):
    rows=[]
    for category,g in df.groupby(col,dropna=False):
        counts=g.outcome_norm.value_counts()
        tp=int(counts.get('TP_FIRST',0));sl=int(counts.get('SL_FIRST',0));n=tp+sl
        lo,hi=wilson(tp,n) if n>=min_resolved else (None,None)
        rows.append({'Grupo':GROUPS.get(category,category),'REACTIONs':len(g),
            'TP':tp,'SL':sl,'NO_HIT':int(counts.get('NO_HIT',0)),
            'AMBIGUOUS':int(counts.get('AMBIGUOUS',0)),
            'SIN_RESULTADO':int(counts.get('SIN_RESULTADO',0)),
            'N resueltos':n,'TP resueltos (%)':round(100*tp/n,1) if n>=min_resolved else None,
            'IC Wilson 95% inferior':lo,'IC Wilson 95% superior':hi,
            'Muestra suficiente':bool(n>=min_resolved)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# INDEPENDENT PRE-ONLY EXPERIMENT: zone's role vs direction of REACTION.
# No 1m retest candle, touch label, First Touch or post-retest path is read.
# ---------------------------------------------------------------------------
ROLE_VERSION = 'horizontal_role_pre_nearest_v1_frozen'
ROLE_NEAR_ATR = NEAR_ATR  # existing 0.5 ATR, not fitted to results
ROLE_REBOUND = 'PRE_REBOTE_COMPATIBLE'
ROLE_BREAK = 'PRE_NECESITA_ROMPER'
ROLE_IN_ZONE = 'PRE_DENTRO_O_EQUIDISTANTE'
ROLE_NO_NEAR = 'PRE_SIN_NIVEL_CERCANO'
ROLE_UNKNOWN = 'PRE_HISTORICO_NO_EVALUABLE'
ROLE_LABELS = {
    ROLE_REBOUND: 'Rebote compatible · LONG/soporte o SHORT/resistencia',
    ROLE_BREAK: 'Necesita ruptura · LONG/resistencia o SHORT/soporte',
    ROLE_IN_ZONE: 'Posición ambigua · dentro de zona o niveles equidistantes',
    ROLE_NO_NEAR: 'Sin nivel importante cercano PRE',
    ROLE_UNKNOWN: 'PRE no evaluable',
}


def _role_near_zones(event, candidates):
    """Nearest qualified levels using *only* their frozen PRE attributes.

    The normalized gap is recomputed from each zone's PRE closed price and ATR.
    This never uses the actual retest 1m high/low, Atlas's known_touch or TP/SL.
    """
    eligible = []
    incomplete = False
    for z in candidates:
        if not important(z):
            continue
        pre = finite(z.get('pre_last_close'))
        atr = finite(z.get('atr_pre'))
        low = finite(z.get('zone_low'))
        high = finite(z.get('zone_high'))
        if (None in (pre, atr, low, high) or pre <= 0 or atr <= 0
                or low <= 0 or high < low):
            incomplete = True
            continue
        gap = candle_zone_gap(z, pre, pre)
        if gap is None:
            incomplete = True
            continue
        distance_atr = gap / atr
        if distance_atr <= ROLE_NEAR_ATR + 1e-10:
            if low <= pre <= high:
                position = 'INSIDE_ZONE'
            elif pre > high:
                position = 'SUPPORT_BELOW'
            else:
                position = 'RESISTANCE_ABOVE'
            eligible.append((distance_atr, z, position, pre))
    eligible.sort(key=lambda e:(e[0], -(finite(e[1].get('defense_episodes_pre')) or 0),
                                -(finite(e[1].get('pivot_count')) or 0),
                                str(e[1].get('timeframe')), str(e[1].get('zone_id'))))
    return eligible, incomplete


def classify_role_pre(event, candidates):
    """Causal PRE hypothesis about one nearest important horizontal level.

    'Rebound' is **not** a claim that the level will hold. 'Needs break' does
    **not** mean the break happened. The last close of the HTF is earlier
    than retest and role is based only on its side relative to frozen zones.
    """
    side = str(event.get('side', '')).upper()
    complete = flag(event.get('all_covered')) is True
    eligible, incomplete = _role_near_zones(event, candidates)
    selected = None
    if not complete or side not in ('LONG','SHORT'):
        role = ROLE_UNKNOWN
    elif not eligible:
        role = ROLE_UNKNOWN if incomplete else ROLE_NO_NEAR
    else:
        best = eligible[0]
        # If levels from different historical maps are equally near but lie
        # on opposite sides of price, the directional meaning is ambiguous.
        tied = [e for e in eligible if abs(e[0]-best[0]) <= 1e-9]
        sides = {e[2] for e in tied}
        selected = best
        if best[2] == 'INSIDE_ZONE' or len(sides) > 1:
            role = ROLE_IN_ZONE
        elif ((side == 'LONG' and best[2] == 'SUPPORT_BELOW') or
              (side == 'SHORT' and best[2] == 'RESISTANCE_ABOVE')):
            role = ROLE_REBOUND
        else:
            role = ROLE_BREAK
    z = selected[1] if selected is not None else None
    return dict(event_key=event.get('event_key'),pre_role=role,
                role_zone_id=z.get('zone_id') if z else None,
                role_timeframe=z.get('timeframe') if z else None,
                role_zone_low=finite(z.get('zone_low')) if z else None,
                role_zone_high=finite(z.get('zone_high')) if z else None,
                role_pre_close=selected[3] if selected else None,
                role_distance_atr=selected[0] if selected else None,
                role_position=selected[2] if selected else None,
                role_defenses_pre=finite(z.get('defense_episodes_pre')) if z else None,
                role_pivots_pre=finite(z.get('pivot_count')) if z else None,
                role_origin=z.get('level_origin') if z else None,
                role_near_count=len(eligible),
                role_incomplete_candidates=bool(incomplete))


def build_pre_roles(events, levels, timeframe='TODOS'):
    """One PRE-only role for each REACTION; works without retest 1m OHLC.

    ``events`` are Atlas V2 event_map records (not outcomes). ``levels``
    are causal candidate_levels. The output is safe for a left one-to-one join
    to the existing verified A/B/C and First Touch analytics.
    """
    if timeframe not in ('TODOS','15m','30m'):
        raise ValueError('Timeframe invalid')
    event_fields={'event_key','all_covered','side'}
    level_fields={'event_key','zone_id','timeframe','pivot_count',
                  'defense_episodes_pre','formation_closed_ts','retest_ts',
                  'pre_last_close','atr_pre','zone_low','zone_high'}
    missing_e=event_fields-set(events.columns)
    missing_l=level_fields-set(levels.columns)
    if missing_e or missing_l:
        raise ValueError(f'PRE role columns missing: events={sorted(missing_e)}, levels={sorted(missing_l)}')
    if events.event_key.duplicated().any():
        raise ValueError('Duplicate event_key in PRE role input')
    chosen=levels if timeframe=='TODOS' else levels.loc[levels.timeframe.eq(timeframe)]
    grouped={k:g.to_dict('records') for k,g in chosen.groupby('event_key',sort=False)}
    records=[classify_role_pre(e,grouped.get(e['event_key'],[]))
             for e in events.to_dict('records')]
    return pd.DataFrame.from_records(records)
