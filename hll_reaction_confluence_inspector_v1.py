"""Verified PRE confluence and bounded OHLC window for one frozen First Touch.

All context rows are taken from the same frozen HLL join. The historical
structural-zone rank at a later dashboard cutoff is intentionally not used.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

MINUTE=60_000
SIZES={'1m':1,'3m':3,'5m':5,'15m':15,'30m':30,'1h':60,'4h':240}
NEEDED={'touch_key','symbol','level_id','formation_closed_ts','zone_low','zone_high',
        'center','gap_atr','prior_tests','prior_defenses','prior_losses','prior_returns'}

def sha(path:Path)->str:
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def load_candidates(folder:Path,*,symbol='BTCUSDT')->pd.DataFrame:
    folder=Path(folder)
    manifest=json.loads((folder/'audit.json').read_text(encoding='utf-8'))
    name='hll_candidates_near_1atr.csv'
    if (manifest.get('version')!='universal-runner-hll-lifecycle-join-v1.0'
            or manifest.get('no_post_outcomes_read') is not True
            or manifest.get('read_only_to_project') is not True
            or sha(folder/name)!=manifest.get('source_hashes',{}).get(name)):
        raise ValueError('Candidatos HLL PRE sin verificación de integridad')
    all_rows=pd.read_csv(folder/name,low_memory=False)
    if not NEEDED.issubset(all_rows.columns):
        raise ValueError('Campos HLL PRE ausentes')
    for field in ('formation_closed_ts','zone_low','zone_high','center','gap_atr',
                  'prior_tests','prior_defenses','prior_losses','prior_returns'):
        all_rows[field]=pd.to_numeric(all_rows[field],errors='raise')
    if all_rows[['touch_key','level_id']].isna().any().any():
        raise ValueError('Identidad HLL incompleta')
    if all_rows.duplicated(['touch_key','level_id']).any():
        raise ValueError('Candidatos HLL duplicados por touch_key/level_id')
    if ((all_rows.zone_low<=0)|(all_rows.zone_high<all_rows.zone_low)|
        (all_rows.gap_atr<0)).any():
        raise ValueError('Geometría PRE inválida')
    return all_rows.loc[all_rows.symbol.eq(symbol)].copy()

def event_context(event:pd.Series,candidates:pd.DataFrame,*,near_atr=.5):
    """Return all eligible PRE candidates and close-by subset, never future members."""
    known=int(event['known_ts'])
    rows=candidates.loc[candidates.touch_key.eq(str(event['touch_key']))].copy()
    if not rows.empty and (rows.formation_closed_ts>=known).any():
        raise ValueError('Horizontal HLL formada después de la REACTION')
    rows=rows.sort_values(['gap_atr','level_id']).reset_index(drop=True)
    close=rows.loc[rows.gap_atr<=near_atr].copy()
    if int(event['hll_near_05atr_count'])!=len(close):
        raise ValueError('Conteo PRE y candidatos a 0,5 ATR no coinciden')
    return rows,close

def focus_interval(event_ms:int,*,pre_hours:int,post_hours:int,reveal_post:bool,
                   cutoff_ms:int,available_start:int,available_end:int):
    if pre_hours not in (2,4,6,12,24) or post_hours not in (2,4,6,12,24):
        raise ValueError('Horas fuera de la configuración de inspección')
    if event_ms>cutoff_ms or event_ms<available_start or event_ms>=available_end:
        raise ValueError('REACTION fuera de la fuente verificada o del corte')
    first=event_ms-pre_hours*3_600_000
    end=min(cutoff_ms,available_end,event_ms+post_hours*3_600_000) if reveal_post else event_ms
    if first<available_start:
        raise ValueError('No hay PRE completo para la ventana seleccionada')
    if end<=first:raise ValueError('Ventana inválida')
    return first,end

def interval_candles(bars:pd.DataFrame,*,first:int,end:int,timeframe:str,
                     source_minute:int,max_bars=3200)->pd.DataFrame:
    """Return complete, UTC-aligned OHLC buckets closed by end.

    Input can be true continuous 1m bars or the verified HLL continuous 30m series.
    """
    if timeframe not in SIZES or SIZES[timeframe]<source_minute or SIZES[timeframe]%source_minute:
        raise ValueError('Temporalidad incompatible con la fuente')
    width=SIZES[timeframe]*MINUTE
    src=source_minute*MINUTE
    # The event time is usually not aligned to bucket boundaries; only full
    # UTC buckets are plotted, and none may close after the PRE boundary.
    candidate=bars.loc[(bars.timestamp>=first-width)&(bars.timestamp+src<=end)].copy()
    if candidate.empty:raise ValueError('Sin velas disponibles')
    key=(candidate.timestamp.astype('int64')//width)*width
    g=candidate.assign(bucket=key).groupby('bucket',sort=True)
    result=g.agg(timestamp=('timestamp','min'),open=('open','first'),high=('high','max'),
                 low=('low','min'),close=('close','last'),count=('timestamp','size'))
    count=SIZES[timeframe]//source_minute
    result=result.loc[(result['count']==count)&
                      (result.timestamp.astype('int64')==result.index.astype('int64'))&
                      (result.index.astype('int64')+width<=end)&
                      (result.index.astype('int64')+width>first)]
    out=result[['timestamp','open','high','low','close']].reset_index(drop=True)
    if out.empty or len(out)>max_bars:raise ValueError('Sin velas completas o demasiadas velas')
    return out
