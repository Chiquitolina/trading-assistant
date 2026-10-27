"""Offline HLL V2.9.5: separate LONG route matrices with first-touch net estimates.

Never trades, changes causal detectors, or transforms censored/ambiguous into outcomes.
All times are 1-minute *buckets*, not exact intraminute fill timestamps.
"""
from __future__ import annotations

from pathlib import Path
import math
import numpy as np
import pandas as pd

from dashboard.views.hll_v294_entry_matrix import (
    MINUTE_MS, ROUTES, _window_from_loaded, evaluate_grid, _positive,
)

KNOWN = ('TP_FIRST', 'SL_FIRST', 'TIMEOUT')


def first_attempt_ids(cohort: pd.DataFrame) -> set[str]:
    """Original earliest CROSS_UP per (symbol, level), before any POST filtering."""
    required = {'attempt_id', 'symbol', 'level_id', 'breakout_closed_ts'}
    if missing := required - set(cohort.columns):
        raise ValueError(f'Faltan columnas para primer intento por nivel: {sorted(missing)}')
    if cohort.attempt_id.isna().any() or cohort.attempt_id.duplicated().any():
        raise ValueError('ID original ausente o duplicado')
    if cohort.level_id.isna().any():
        raise ValueError('Sin level_id, no se puede deduplicar con precisión')
    if pd.to_numeric(cohort.breakout_closed_ts, errors='coerce').isna().any():
        raise ValueError('Faltan fechas originales del breakout')
    chosen = (cohort.sort_values(['breakout_closed_ts','attempt_id'],kind='stable')
              .drop_duplicates(['symbol','level_id'],keep='first'))
    return set(chosen.attempt_id.astype(str))


def first_touch_net(bars: pd.DataFrame, entry_price: float, path_state: str,
                    entry_ts: int, commission_roundtrip_pct:float=0.1,
                    slippage_roundtrip_pct:float=0.0) -> pd.DataFrame:
    """25 independent scenarios per route entry; net estimate in return %.

    TP/SL barrier fills are modeled at threshold (NOT exchange executions),
    with configurable round-trip commission and slippage in percentage points.
    TIMEOUT uses final 1m close only for a fully observed horizon.
    UNKNOWN never receives an imputed PnL.
    """
    for name, x in (('Comisión total',commission_roundtrip_pct),
                    ('Deslizamiento total',slippage_roundtrip_pct)):
        if not math.isfinite(float(x)) or float(x)<0 or float(x)>5:
            raise ValueError(name+' debe ser un porcentaje entre 0 y 5')
    if not _positive(entry_price):
        raise ValueError('Entrada sin precio verificado')
    if not isinstance(entry_ts, (int,np.integer)) or entry_ts<=0:
        raise ValueError('Entrada sin reloj verificado')
    grid = evaluate_grid(bars, entry_price, path_state)
    cost = float(commission_roundtrip_pct)+float(slippage_roundtrip_pct)
    last_close = float(bars.close.iloc[-1]) if not bars.empty else None
    results=[]
    for a in grid.itertuples(index=False):
        known = a.result in KNOWN
        gross = (float(a.tp_pct) if a.result=='TP_FIRST' else
                 -float(a.sl_pct) if a.result=='SL_FIRST' else
                 (100*(last_close/entry_price-1) if a.result=='TIMEOUT' and last_close is not None else np.nan))
        net = gross-cost if known else np.nan
        minute_index = None if pd.isna(a.first_hit_min) else int(a.first_hit_min)
        minute_ts = (int(entry_ts)+minute_index*MINUTE_MS) if minute_index is not None else None
        rows=dict(tp_pct=int(a.tp_pct), sl_pct=int(a.sl_pct), rr=float(a.rr),
                  result=a.result, first_hit_min=minute_index,
                  first_hit_minute_utc=(pd.Timestamp(minute_ts,unit='ms',tz='UTC').isoformat()
                                       if minute_ts is not None else None),
                  gross_pct=(round(float(gross),8) if known else np.nan),
                  net_pct=(round(float(net),8) if known else np.nan),
                  cost_roundtrip_pct=cost)
        results.append(rows)
    return pd.DataFrame(results)


def _validate_ohlc(bars:pd.DataFrame, symbol:str):
    if bars.empty:
        return
    ts=pd.to_numeric(bars.timestamp,errors='raise').to_numpy(dtype=np.int64)
    if len(ts)>1 and (np.diff(ts)<=0).any():
        raise ValueError(f'Archivo 1m no ordenado o con timestamps duplicados: {symbol}')
    vals=bars[['open','high','low','close']].apply(pd.to_numeric,errors='coerce').to_numpy(dtype=float)
    if (not np.isfinite(vals).all() or (vals<=0).any() or
        (vals[:,2]>np.minimum.reduce([vals[:,0],vals[:,1],vals[:,3]])).any() or
        (vals[:,1]<np.maximum.reduce([vals[:,0],vals[:,2],vals[:,3]])).any()):
        raise ValueError(f'OHLC 1m inválidos para {symbol}')


def aggregate_net_matrix(catalogue:pd.DataFrame, minute_dir:Path, horizon_min:int,
                         commission_roundtrip_pct:float=0.1,
                         slippage_roundtrip_pct:float=0.0) -> pd.DataFrame:
    """Per-route matrix of 25 scenarios; reads each symbol's 1m history once.

    A given attempt may have entries via multiple routes. It is counted once
    WITHIN each route, never pooled across routes in a single expectancy.
    """
    if horizon_min not in (120,720,2880):
        raise ValueError('Horizonte no permitido')
    if catalogue.duplicated(['attempt_id','route']).any():
        raise ValueError('Duplicados por intento/ruta')
    rows=[]
    selected=catalogue[catalogue.status.eq('ENTRY_VERIFIED')]
    if not set(selected.route).issubset(ROUTES):
        raise ValueError('Ruta desconocida')
    for sym, group in selected.groupby('symbol',sort=True):
        path=Path(minute_dir)/f'{sym}_1m.csv'
        if path.is_file():
            bars=pd.read_csv(path,usecols=['timestamp','open','high','low','close'])
            _validate_ohlc(bars,sym)
            bars.timestamp=pd.to_numeric(bars.timestamp,errors='raise').astype('int64')
        else:
            bars=None
        for e in group.itertuples(index=False):
            if not _positive(e.entry_price) or pd.isna(e.entry_ts):
                raise ValueError(f'Entrada verificada sin OPEN {e.attempt_id}')
            if bars is None:
                window=pd.DataFrame(); state='NO_1M_FILE'
            else:
                window,state=_window_from_loaded(bars,int(e.entry_ts),horizon_min)
                if not window.empty:
                    op=float(window.open.iloc[0])
                    if abs(op-float(e.entry_price)) > max(1e-10,abs(op)*1e-7):
                        raise ValueError(f'OPEN 1m difiere del catálogo: {e.attempt_id}/{e.route}')
            per=first_touch_net(window,float(e.entry_price),state,int(e.entry_ts),
                                commission_roundtrip_pct,slippage_roundtrip_pct)
            per['route']=e.route
            per['attempt_id']=str(e.attempt_id)
            per['symbol']=str(sym)
            per['path_state']=state
            rows.append(per)
    detail=(pd.concat(rows,ignore_index=True) if rows else pd.DataFrame(
        columns=['tp_pct','sl_pct','rr','result','first_hit_min','net_pct','route','attempt_id']))
    return summarize_net_matrix(detail)


def summarize_net_matrix(detail:pd.DataFrame) -> pd.DataFrame:
    """Per-route 25-cell scorecards. UNKNOWN/ambiguous excluded from net means."""
    if detail.empty:
        return pd.DataFrame(columns=['route','sl_pct','tp_pct','entries','known','TP_FIRST',
                                     'SL_FIRST','TIMEOUT','AMBIGUOUS_1M','CENSORED',
                                     'coverage_pct','tp_first_rate_pct','net_mean_pct',
                                     'net_median_pct','net_positive_pct',
                                     'median_tp_min','median_sl_min'])
    out=[]
    for (route,sl,tp),g in detail.groupby(['route','sl_pct','tp_pct'],sort=True):
        r=g.result.value_counts()
        observed=g[g.result.isin(KNOWN)]
        net=pd.to_numeric(observed.net_pct,errors='coerce')
        if net.isna().any():
            raise ValueError('Resultado conocido sin rentabilidad neta calculada')
        n=len(g); k=len(observed)
        wins=observed[observed.result.eq('TP_FIRST')].first_hit_min.dropna()
        losses=observed[observed.result.eq('SL_FIRST')].first_hit_min.dropna()
        out.append(dict(route=route,sl_pct=int(sl),tp_pct=int(tp),rr=round(float(tp)/float(sl),3),
                        entries=n,known=k,TP_FIRST=int(r.get('TP_FIRST',0)),
                        SL_FIRST=int(r.get('SL_FIRST',0)),TIMEOUT=int(r.get('TIMEOUT',0)),
                        AMBIGUOUS_1M=int(r.get('AMBIGUOUS_1M',0)),
                        CENSORED=int(n-k-r.get('AMBIGUOUS_1M',0)),
                        coverage_pct=round(100*k/n,2) if n else np.nan,
                        tp_first_rate_pct=round(100*r.get('TP_FIRST',0)/k,2) if k else np.nan,
                        net_mean_pct=round(float(net.mean()),5) if k else np.nan,
                        net_median_pct=round(float(net.median()),5) if k else np.nan,
                        net_positive_pct=round(float(100*(net>0).mean()),2) if k else np.nan,
                        median_tp_min=round(float(wins.median()),1) if len(wins) else np.nan,
                        median_sl_min=round(float(losses.median()),1) if len(losses) else np.nan))
    return pd.DataFrame(out).sort_values(['route','sl_pct','tp_pct']).reset_index(drop=True)
