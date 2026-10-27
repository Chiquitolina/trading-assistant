"""Read-only LONG research: audited entry coverage and causal 1m first-touch grid.

Neither a CROSS_UP-only reference nor an absent OPEN is an entry.
No orders; no TP/SL parameters are changed in historical detectors.
"""
from __future__ import annotations

from pathlib import Path
import math
import numpy as np
import pandas as pd

MINUTE_MS = 60_000
TF30_MS = 1_800_000
ROUTES = ('FLIP_1M', 'V29_CONTINUATION_30M', 'V29_RECLAIM_30M')
LABELS = {'FLIP_1M':'Flip', 'V29_CONTINUATION_30M':'Continuación',
          'V29_RECLAIM_30M':'Reclaim'}


def _positive(value):
    try:
        x = float(value)
        return math.isfinite(x) and x > 0
    except (TypeError, ValueError):
        return False


def _time(value):
    try:
        if value is None or pd.isna(value):
            return None
        v = int(value)
        return v if v > 0 else None
    except (OverflowError, TypeError, ValueError):
        return None


def _read_observed_opens(path: Path, timestamps: set[int]):
    """One linear scan per symbol for all requested exact 1m entry opens."""
    if not timestamps or not path.is_file():
        return {}
    found = {}
    previous = None
    for chunk in pd.read_csv(path, usecols=['timestamp','open','high','low','close'], chunksize=180_000):
        t = pd.to_numeric(chunk.timestamp, errors='raise').astype('int64')
        if t.duplicated().any() or (len(t)>1 and not t.is_monotonic_increasing):
            raise ValueError(f'Archivo 1m no ordenado o duplicado: {path}')
        if previous is not None and len(t) and int(t.iloc[0]) <= previous:
            raise ValueError(f'Archivo 1m no monótono: {path}')
        if len(t): previous=int(t.iloc[-1])
        target = chunk.loc[t.isin(timestamps)]
        for rec in target.itertuples(index=False):
            stamp=int(rec.timestamp)
            if stamp in found:
                raise ValueError(f'OPEN 1m duplicado en {path}: {stamp}')
            op,hi,lo,cl=[float(getattr(rec,k)) for k in ('open','high','low','close')]
            if not all(_positive(x) for x in (op,hi,lo,cl)) or lo>min(op,cl,hi) or hi<max(op,cl,lo):
                raise ValueError(f'OHLC 1m inválido: {path}, timestamp={stamp}')
            found[stamp]=op
    return found


def build_entry_catalogue(cohort: pd.DataFrame, v29: pd.DataFrame,
                          candles: Path, minute_dir: Path) -> pd.DataFrame:
    """One row per attempt/route; preserve unverified signals, never guess OPEN."""
    if cohort.attempt_id.isna().any() or cohort.attempt_id.duplicated().any():
        raise ValueError('Cohorte con IDs ausentes o duplicados')
    if not v29.empty and v29.duplicated(['attempt_id','branch']).any():
        raise ValueError('V2.9 duplicó branch para el mismo intento')
    branches={
        'BREAKOUT_CONTINUATION': 'V29_CONTINUATION_30M',
        'RECLAIM_CONTINUATION': 'V29_RECLAIM_30M',
    }
    events={(str(e.attempt_id),str(e.branch)):e for e in v29.itertuples(index=False)} if not v29.empty else {}
    symbols=cohort.symbol.astype(str).unique()
    result=[]
    for sym in symbols:
        part=cohort.loc[cohort.symbol.eq(sym)]
        path=Path(candles)/f'{sym}_30m.csv'
        if path.is_file():
            bars=pd.read_csv(path, usecols=['timestamp','open','high','low','close'])
            if bars.timestamp.duplicated().any():
                raise ValueError(f'Timestamp 30m duplicado: {path}')
            opens={int(t):float(op) for t,op in zip(bars.timestamp,bars.open)}
            closes={int(t):float(cl) for t,cl in zip(bars.timestamp,bars.close)}
        else:
            opens={}; closes={}
        flip_targets={int(t)+MINUTE_MS for t in part.flip_confirmed_closed_ts.dropna()}
        flip_opens=_read_observed_opens(Path(minute_dir)/f'{sym}_1m.csv',flip_targets)
        for row in part.itertuples(index=False):
            attempt_id=str(row.attempt_id)
            flip=_time(row.flip_confirmed_closed_ts)
            if flip is not None:
                ts=flip+MINUTE_MS
                price=flip_opens.get(ts)
                result.append(dict(attempt_id=attempt_id,symbol=sym,route='FLIP_1M',
                                   status='ENTRY_VERIFIED' if _positive(price) else 'SIGNAL_OPEN_MISSING',
                                   entry_ts=ts,entry_price=price,signal_ts=flip))
            for branch,route in branches.items():
                ev=events.get((attempt_id,branch))
                if ev is None or str(ev.status).strip()!='CONFIRMED':
                    continue
                ts=_time(ev.confirmed_ts)
                if ts is None:
                    reason='SIGNAL_CLOCK_INVALID'; price=None
                else:
                    price=opens.get(ts)
                    signal_open=_time(ev.signal_open_ts)
                    close=closes.get(signal_open) if signal_open is not None else None
                    if signal_open is None or signal_open+TF30_MS!=ts or not _positive(close) or not _positive(ev.signal_close):
                        reason='SIGNAL_OHLC_MISMATCH'
                    elif abs(close-float(ev.signal_close))>max(1e-10,abs(close)*1e-7):
                        reason='SIGNAL_OHLC_MISMATCH'
                    elif not _positive(price):
                        reason='SIGNAL_OPEN_MISSING'
                    elif _time(getattr(ev,'next_open_ts',None)) not in (None,ts):
                        reason='NEXT_OPEN_CLOCK_MISMATCH'
                    elif _positive(getattr(ev,'next_open',None)) and abs(price-float(ev.next_open))>max(1e-10,abs(price)*1e-7):
                        reason='NEXT_OPEN_PRICE_MISMATCH'
                    else:
                        reason='ENTRY_VERIFIED'
                result.append(dict(attempt_id=attempt_id,symbol=sym,route=route,
                                   status=reason,entry_ts=ts,
                                   entry_price=price if reason=='ENTRY_VERIFIED' else None,
                                   signal_ts=ts))
    return pd.DataFrame(result,columns=['attempt_id','symbol','route','status','entry_ts','entry_price','signal_ts'])


def summarize_coverage(selection: pd.DataFrame, catalogue: pd.DataFrame):
    """Count attempts once; multiple entry routes on an attempt remain separate."""
    ids=set(selection.attempt_id.astype(str))
    subset=catalogue[catalogue.attempt_id.isin(ids)]
    with_entry=set(subset.loc[subset.status.eq('ENTRY_VERIFIED'),'attempt_id'])
    with_signal=set(subset.attempt_id)
    n=len(ids)
    summary={'total':n, 'with_entry':len(with_entry),
             'without_entry':n-len(with_entry),
             'no_signal':n-len(with_signal),
             'confirmed_unverified':len(with_signal-with_entry)}
    for route in ROUTES:
        s=subset[subset.route.eq(route)]
        summary[route]={'signal':len(s),'verified':int(s.status.eq('ENTRY_VERIFIED').sum()),
                        'unverified':int(s.status.ne('ENTRY_VERIFIED').sum())}
    return summary


def read_minute_window(path: Path, entry_ts: int, horizon_min: int):
    """Return contiguous 1m observations through first gap or tail; no filling."""
    if horizon_min not in (120,720,2880):
        raise ValueError('Horizonte no admitido')
    if not path.is_file():
        return pd.DataFrame(), 'NO_1M_FILE'
    until=entry_ts+horizon_min*MINUTE_MS
    chunks=[]
    for raw in pd.read_csv(path, usecols=['timestamp','open','high','low','close'], chunksize=180_000):
        ts=pd.to_numeric(raw.timestamp,errors='raise').astype('int64')
        segment=raw.loc[ts.ge(entry_ts)&ts.lt(until)]
        if not segment.empty:chunks.append(segment)
        if len(ts) and int(ts.iloc[-1])>=until: break
    if not chunks:
        return pd.DataFrame(), 'NO_ENTRY_MINUTE'
    data=pd.concat(chunks,ignore_index=True)
    # Only the contiguous prefix is admissible for first-touch.
    stamps=pd.to_numeric(data.timestamp,errors='raise').astype('int64').to_numpy()
    if len(stamps)>1 and (np.diff(stamps)<=0).any():
        raise ValueError('Timestamp 1m duplicado o fuera de orden en el segmento')
    if stamps[0]!=entry_ts:
        return pd.DataFrame(), 'NO_ENTRY_MINUTE'
    expected=entry_ts+np.arange(len(stamps),dtype=np.int64)*MINUTE_MS
    mismatch=np.flatnonzero(stamps!=expected)
    cut=int(mismatch[0]) if len(mismatch) else len(stamps)
    valid=data.iloc[:cut].copy()
    vals=valid[['open','high','low','close']].apply(pd.to_numeric,errors='coerce').to_numpy(dtype=float)
    if not np.isfinite(vals).all() or (vals<=0).any() or (vals[:,2]>np.minimum.reduce([vals[:,0],vals[:,1],vals[:,3]])).any() or (vals[:,1]<np.maximum.reduce([vals[:,0],vals[:,2],vals[:,3]])).any():
        raise ValueError('OHLC 1m inválido durante el horizonte')
    if len(mismatch):return valid,'CENSORED_GAP'
    if len(stamps)<horizon_min:return valid,'CENSORED_TAIL'
    return valid,'COMPLETE_1M'


def evaluate_grid(minute_bars:pd.DataFrame, entry_price:float, path_state:str):
    """25 first-touch outcomes; same-minute high+low touch is AMBIGUOUS_1M."""
    if not _positive(entry_price):
        raise ValueError('No existe precio ejecutable de entrada')
    if path_state not in ('COMPLETE_1M','CENSORED_GAP','CENSORED_TAIL','NO_1M_FILE','NO_ENTRY_MINUTE'):
        raise ValueError('Estado de trayectoria desconocido')
    if minute_bars.empty: hi=np.array([]);lo=np.array([])
    else:
        hi=minute_bars.high.to_numpy(dtype=float)
        lo=minute_bars.low.to_numpy(dtype=float)
    rows=[]
    for sl in range(1,6):
        for tp in range(1,6):
            tpi=np.flatnonzero(hi>=float(entry_price)*(1+tp/100))
            sli=np.flatnonzero(lo<=float(entry_price)*(1-sl/100))
            it=int(tpi[0]) if len(tpi) else None
            il=int(sli[0]) if len(sli) else None
            if it is not None and il is not None and it==il:
                outcome='AMBIGUOUS_1M'; first=it
            elif it is not None and (il is None or it<il):
                outcome='TP_FIRST';first=it
            elif il is not None:
                outcome='SL_FIRST';first=il
            else:
                outcome='TIMEOUT' if path_state=='COMPLETE_1M' else path_state
                first=None
            rows.append(dict(tp_pct=tp,sl_pct=sl,rr=round(tp/sl,3),
                             result=outcome,first_hit_min=first))
    return pd.DataFrame(rows)


def matrix_table(results:pd.DataFrame):
    abbreviations={'TP_FIRST':'TP','SL_FIRST':'SL','AMBIGUOUS_1M':'AMB',
                   'TIMEOUT':'TIME','CENSORED_GAP':'GAP','CENSORED_TAIL':'TAIL',
                   'NO_1M_FILE':'SIN 1m','NO_ENTRY_MINUTE':'SIN OPEN'}
    r=results.copy()
    r['display']=r.result.map(abbreviations).fillna(r.result)
    return r.pivot(index='sl_pct',columns='tp_pct',values='display').reindex(index=range(1,6),columns=range(1,6))


def _window_from_loaded(frame, entry_ts, horizon_min):
    """Take a sorted 1m symbol array, preserving first gap/tail semantics."""
    until=entry_ts+int(horizon_min)*MINUTE_MS
    ts=frame.timestamp.to_numpy(dtype=np.int64)
    start=int(np.searchsorted(ts, entry_ts, side='left'))
    end=int(np.searchsorted(ts, until, side='left'))
    if start>=len(ts) or ts[start]!=entry_ts:
        return frame.iloc[:0], 'NO_ENTRY_MINUTE'
    selected=frame.iloc[start:end]
    actual=selected.timestamp.to_numpy(dtype=np.int64)
    expected=entry_ts+np.arange(len(actual),dtype=np.int64)*MINUTE_MS
    invalid=np.flatnonzero(actual!=expected)
    if len(invalid):
        return selected.iloc[:int(invalid[0])], 'CENSORED_GAP'
    if len(selected)<horizon_min:
        return selected, 'CENSORED_TAIL'
    return selected,'COMPLETE_1M'


def aggregate_selection_matrix(verified_catalogue:pd.DataFrame, minute_dir: Path,
                               horizon_min:int):
    """Aggregate all selected observed entries, one per attempt per requested route.

    Valid: TP_FIRST, SL_FIRST, TIMEOUT. AMBIGUOUS_1M and censored are excluded
    from TP rate denominators, and independently enumerated in every cell.
    """
    if horizon_min not in (120,720,2880):raise ValueError('Horizonte no admitido')
    if verified_catalogue.duplicated(['attempt_id','route']).any():
        raise ValueError('La selección repite un intento/ruta')
    only=verified_catalogue[verified_catalogue.status.eq('ENTRY_VERIFIED')]
    counts={(sl,tp):{} for sl in range(1,6) for tp in range(1,6)}
    for sym,grp in only.groupby('symbol'):
        path=Path(minute_dir)/f'{sym}_1m.csv'
        if path.is_file():
            bars=pd.read_csv(path,usecols=['timestamp','open','high','low','close'])
            if bars.timestamp.duplicated().any() or not bars.timestamp.is_monotonic_increasing:
                raise ValueError(f'1m duplicado o desordenado en {sym}')
            bars=bars.reset_index(drop=True)
        else:
            bars=None
        for row in grp.itertuples(index=False):
            if bars is None:
                data=pd.DataFrame(); state='NO_1M_FILE'
            else:
                data,state=_window_from_loaded(bars,int(row.entry_ts),horizon_min)
                if not data.empty:
                    vals=data[['open','high','low','close']].apply(pd.to_numeric,errors='coerce').to_numpy(dtype=float)
                    if not np.isfinite(vals).all() or (vals<=0).any() or (vals[:,2]>np.minimum.reduce([vals[:,0],vals[:,1],vals[:,3]])).any() or (vals[:,1]<np.maximum.reduce([vals[:,0],vals[:,2],vals[:,3]])).any():
                        raise ValueError(f'OHLC 1m inválido cerca de {row.entry_ts} ({sym})')
                    if abs(float(vals[0,0])-float(row.entry_price))>max(1e-10,abs(float(row.entry_price))*1e-7):
                        raise ValueError(f'OPEN 1m no coincide con OPEN de ruta {row.route}, {sym}')
            outcomes=evaluate_grid(data,float(row.entry_price),state)
            for o in outcomes.itertuples(index=False):
                bucket=counts[(o.sl_pct,o.tp_pct)]
                bucket[o.result]=bucket.get(o.result,0)+1
    rows=[]
    for (sl,tp),c in counts.items():
        tp_count=c.get('TP_FIRST',0)
        sl_count=c.get('SL_FIRST',0)
        time_count=c.get('TIMEOUT',0)
        known=tp_count+sl_count+time_count
        rows.append(dict(sl_pct=sl,tp_pct=tp,rr=round(tp/sl,3),
                         entries=len(only),known=known,TP_FIRST=tp_count,SL_FIRST=sl_count,
                         TIMEOUT=time_count,AMBIGUOUS_1M=c.get('AMBIGUOUS_1M',0),
                         CENSORED=sum(v for k,v in c.items() if k not in
                                      ('TP_FIRST','SL_FIRST','TIMEOUT','AMBIGUOUS_1M')),
                         tp_first_rate_pct=round(100*tp_count/known,2) if known else None))
    return pd.DataFrame(rows)

from functools import lru_cache


def _fingerprint(path:Path):
    if not path.is_file():return (str(path),0,0)
    st=path.stat()
    return (str(path),int(st.st_size),int(st.st_mtime_ns))


@lru_cache(maxsize=3)
def _catalogue_cached(root_str, cohort_str, fingerprints):
    root=Path(root_str)
    cohort=pd.read_csv(cohort_str,low_memory=False)
    from dashboard.views.hll_v282_pre_followup_view import (
        V29_EVENTS,CANDLES,load_v29_events
    )
    p=root/V29_EVENTS
    v29=load_v29_events(p) if p.is_file() else pd.DataFrame()
    minutes=root/'reports/horizontal_level_cross_research_v22/extended_1m_core15_v25'
    return build_entry_catalogue(cohort,v29,root/CANDLES,minutes)


def load_catalogue_for_view(root:Path, folder:Path, symbols):
    """Cache the read-only catalogue until any relevant source file changes."""
    from dashboard.views.hll_v282_pre_followup_view import V29_EVENTS,CANDLES
    root=Path(root); folder=Path(folder)
    mins=root/'reports/horizontal_level_cross_research_v22/extended_1m_core15_v25'
    fingerprints=tuple([_fingerprint(folder/'pre_cohort.csv'),_fingerprint(root/V29_EVENTS)] +
        [_fingerprint(root/CANDLES/f'{s}_30m.csv') for s in sorted(symbols)] +
        [_fingerprint(mins/f'{s}_1m.csv') for s in sorted(symbols)])
    return _catalogue_cached(str(root),str(folder/'pre_cohort.csv'),fingerprints)

@lru_cache(maxsize=512)
def _episode_grid_cached(path_str:str,size:int,mtime_ns:int,entry_ts:int,price:float,horizon_min:int):
    bars,state=read_minute_window(Path(path_str),entry_ts,horizon_min)
    if not bars.empty:
        actual=float(bars.open.iloc[0])
        if abs(actual-price)>max(1e-10,abs(price)*1e-7):
            raise ValueError('OPEN 1m no coincide con entrada verificada')
    return evaluate_grid(bars,price,state),state


def episode_grid(path:Path,entry_ts:int,price:float,horizon_min:int):
    fingerprint=_fingerprint(Path(path))
    return _episode_grid_cached(str(path),fingerprint[1],fingerprint[2],
                                int(entry_ts),float(price),int(horizon_min))


def reconcile_entries_with_catalogue(existing:list[dict], catalogue_rows:pd.DataFrame):
    """Recover independently verified exact OPENs if an older plotting helper omitted them.

    This intentionally cannot recover BREAKOUT_30M (unconfirmed reference).
    Conflicts fail loudly; only verified observations can be added.
    """
    from dashboard.views.hll_v293_entry_markers import ENTRY_INFO
    by_key={e['key']:dict(e) for e in existing}
    for item in catalogue_rows.itertuples(index=False):
        if item.status != 'ENTRY_VERIFIED' or item.route not in ROUTES:
            continue
        key=item.route;clock=int(item.entry_ts);price=float(item.entry_price)
        if key in by_key:
            old=by_key[key]
            if int(old['entry_ts'])!=clock or abs(float(old['price'])-price)>max(1e-10,price*1e-7):
                raise ValueError(f'Entrada {key}: diferencia entre catálogo y marcador')
            continue
        short,color,marker,desc=ENTRY_INFO[key]
        known=clock-MINUTE_MS if key=='FLIP_1M' else clock
        by_key[key]=dict(key=key,short=short,color=color,marker=marker,
                         description=desc,price=price,entry_ts=clock,
                         known_ts=known,label=desc)
    return [by_key[k] for k in list(ROUTES)+['BREAKOUT_30M'] if k in by_key]
