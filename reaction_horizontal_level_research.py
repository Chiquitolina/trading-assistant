"""Horizontal Level Research V1 -- read-only, causal Atlas V2 zone triage.

Importance is fixed before observing First Touch:
- two confirmed same-kind pivot contacts (Atlas V2 freezes the level on pivot 2)
- >= 1 PRE independent defense episode (NOT successive 1m candles)
- the level has formed before the retest 1m candle

PRE_NEAR uses the last completed HTF close; A/B use the *closed retest*
1m candle, thus cannot be treated as an entry-at-open predictor.
"""
from __future__ import annotations

import hashlib
import math
import pandas as pd

MODEL_VERSION = 'horizontal_levels_importance_v1_frozen'
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
    """No KNOWN/outcome fields used here. Only PRE Atlas V2 evidence."""
    p=finite(zone['pivot_count'])
    episodes=finite(zone['defense_episodes_pre'])
    formed=finite(zone['formation_closed_ts'])
    retest=finite(zone['retest_ts'])
    return bool(p is not None and p>=MIN_CONFIRMED_PIVOTS and episodes is not None
                and episodes>=MIN_PRE_DEFENSE_EPISODES and formed is not None
                and retest is not None and formed<retest)


def zone_rank(zone):
    """No outcomes. First favor proven episodes then closeness then identity."""
    eps=finite(zone.get('defense_episodes_pre')) or 0
    pivots=finite(zone.get('pivot_count')) or 0
    dist=finite(zone.get('gap_to_zone_atr'))
    return (-eps, -pivots, float('inf') if dist is None else dist,
            str(zone.get('timeframe')), str(zone.get('zone_id')))


def _placebo_close(zone):
    """Fixed ±3 ATR shift, diagnostic PRE proximity only; NOT placebo trading."""
    price=finite(zone.get('pre_last_close'))
    atr=finite(zone.get('atr_pre'))
    low=finite(zone.get('zone_low')); high=finite(zone.get('zone_high'))
    if None in (price,atr,low,high) or atr<=0: return None
    token=f"{zone.get('event_key')}|{zone.get('zone_id')}".encode()
    sign=1 if int(hashlib.sha256(token).hexdigest(),16)&1 else -1
    offset=sign*3*atr
    gap=max(low+offset-price,0,price-(high+offset))
    return gap/atr<=NEAR_ATR


def classify(event, candidates):
    """Return one event row. A/B use KNOWN close, PRE near remains independent."""
    raw=list(candidates)
    selected=[r for r in raw if important(r)]
    selected.sort(key=zone_rank)
    complete=flag(event.get('all_covered')) is True
    # Reject incomplete source before treating lack of levels as 'no level'.
    if not complete:
        group=UNKNOWN; chosen=None
    else:
        touched=[r for r in selected if flag(r.get('known_touch')) is True]
        near=[r for r in selected if finite(r.get('gap_to_zone_atr')) is not None
              and float(r['gap_to_zone_atr'])<=NEAR_ATR]
        unknown_contact=any(flag(r.get('known_touch')) is None for r in selected)
        if touched:
            group=A; chosen=sorted(touched,key=zone_rank)[0]
        elif unknown_contact:
            group=UNKNOWN; chosen=None
        elif near:
            group=B; chosen=sorted(near,key=zone_rank)[0]
        else:
            group=C; chosen=selected[0] if selected else None
    pre_near= any(finite(z.get('gap_to_zone_atr')) is not None
                  and float(z['gap_to_zone_atr'])<=NEAR_ATR for z in selected)
    pre_bucket = ('NO_EVALUABLE' if not complete else
                  'PRE_CERCA' if pre_near else 'PRE_LEJOS_O_SIN_NIVEL')
    placebo_near = any(_placebo_close(z) is True for z in selected) if selected and complete else None
    d=dict(event)
    d.update(level_group=group,pre_bucket=pre_bucket,
             important_levels=len(selected),
             relevant_zone_id=chosen.get('zone_id') if chosen else None,
             relevant_timeframe=chosen.get('timeframe') if chosen else None,
             relevant_zone_low=chosen.get('zone_low') if chosen else None,
             relevant_zone_high=chosen.get('zone_high') if chosen else None,
             relevant_gap_atr=chosen.get('gap_to_zone_atr') if chosen else None,
             relevant_episodes=chosen.get('defense_episodes_pre') if chosen else None,
             relevant_pivots=chosen.get('pivot_count') if chosen else None,
             relevant_state=chosen.get('level_state_pre') if chosen else None,
             placebo_near_pre=placebo_near)
    return d


def build(events, levels, timeframe='TODOS'):
    """Source candidates are Atlas V2 only; no candle/replay recomputation."""
    if timeframe not in ('TODOS','15m','30m'): raise ValueError('Timeframe invalid')
    req_event={'event_key','all_covered','retest_ts','symbol','side','date_utc'}
    req_level={'event_key','zone_id','pivot_count','defense_episodes_pre','formation_closed_ts',
               'retest_ts','timeframe','gap_to_zone_atr','known_touch'}
    missing_e=req_event-set(events.columns)
    missing_l=req_level-set(levels.columns)
    if missing_e or missing_l:
        raise ValueError(f'Atlas V2 columns missing: events={sorted(missing_e)}, levels={sorted(missing_l)}')
    if events.event_key.duplicated().any(): raise ValueError('Duplicate event in Atlas V2')
    # Fail closed if Atlas provided levels formed after signal.
    for z in levels.to_dict('records'):
        a,b=finite(z.get('formation_closed_ts')),finite(z.get('retest_ts'))
        if a is not None and b is not None and a>=b:
            raise ValueError('Future/non-confirmed structural level in Atlas V2')
    chosen=levels if timeframe=='TODOS' else levels.loc[levels.timeframe.eq(timeframe)]
    grouped={k:g.to_dict('records') for k,g in chosen.groupby('event_key',sort=False)}
    records=[classify(e,grouped.get(e['event_key'],[])) for e in events.to_dict('records')]
    return pd.DataFrame(records), chosen.loc[chosen.apply(important,axis=1)].copy()


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
        rows.append({'Grupo':GROUPS.get(category,category), 'REACTIONs':len(g),
            'TP':tp,'SL':sl,'NO_HIT':int(counts.get('NO_HIT',0)),
            'AMBIGUOUS':int(counts.get('AMBIGUOUS',0)),
            'SIN_RESULTADO':int(counts.get('SIN_RESULTADO',0)),
            'N resueltos':n,'TP resueltos (%)':round(100*tp/n,1) if n>=min_resolved else None,
            'IC Wilson 95% inferior':lo,'IC Wilson 95% superior':hi,
            'Muestra suficiente':bool(n>=min_resolved)})
    return pd.DataFrame(rows)
