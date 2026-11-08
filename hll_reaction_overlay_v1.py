"""Read-only overlay of frozen Reaction First Touch events on HLL BTC chart.

Markers use the causal *swing reference price*, not an execution or candle close.
No network access, scanner calls, database writes, or post-event outcome reads.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

RELATIVE_RUN = Path('reports/reaction_lab/universal_runner_hll_lifecycle_join_v1/runs/pilot_5_hll_extended_20261108')
KINDS = ('REACTION', 'TOUCH_FAILED', 'TOUCH_INDECISIVE')
PALETTE = {'REACTION':'#35c985', 'TOUCH_FAILED':'#f17f86', 'TOUCH_INDECISIVE':'#e8bd60'}
SYMBOLS = {'REACTION':'triangle-up', 'TOUCH_FAILED':'x', 'TOUCH_INDECISIVE':'circle'}
REQUIRED = ('touch_key','symbol','side','status','retest_ts','known_ts','swing_price','hll_coverage')


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(piece)
    return h.hexdigest()


def load_frozen(path: Path, *, symbol: str = 'BTCUSDT') -> tuple[pd.DataFrame, dict]:
    path = Path(path)
    audit = json.loads((path/'audit.json').read_text(encoding='utf-8'))
    if audit.get('version') != 'universal-runner-hll-lifecycle-join-v1.0':
        raise ValueError('Versión desconocida de etiquetas Reaction × HLL')
    if audit.get('no_post_outcomes_read') is not True or audit.get('read_only_to_project') is not True:
        raise ValueError('La fuente no declara lectura estrictamente PRE')
    if int(audit.get('events', -1)) <= 0:
        raise ValueError('Cardinalidad del manifiesto inválida')
    labels = path/'event_hll_labels.csv'
    if _sha256(labels) != audit['source_hashes']['event_hll_labels.csv']:
        raise ValueError('Hash de etiquetas Reaction × HLL incorrecto')
    data = pd.read_csv(labels, low_memory=False)
    if len(data) != int(audit['events']):
        raise ValueError('Cantidad de eventos diferente del manifiesto')
    if not set(REQUIRED).issubset(data.columns):
        raise ValueError('Campos obligatorios ausentes del overlay')
    if data.touch_key.isna().any() or data.touch_key.duplicated().any():
        raise ValueError('Identidades First Touch duplicadas o ausentes')
    if not data.status.isin(KINDS).all():
        raise ValueError('Estado First Touch no reconocido')
    if not data.hll_coverage.eq('COMPLETE').all():
        raise ValueError('Hay eventos sin cobertura HLL completa')
    for col in ('retest_ts','known_ts','swing_price'):
        data[col] = pd.to_numeric(data[col],errors='raise')
    if not ((data.known_ts - data.retest_ts) == 60_000).all():
        raise ValueError('Evento sin known_ts causal 1m')
    if not np.isfinite(data.swing_price.to_numpy(dtype=float)).all() or (data.swing_price<=0).any():
        raise ValueError('Precios de referencia inválidos')
    result = data.loc[data.symbol.eq(symbol)].copy()
    result.sort_values(['known_ts','touch_key'],inplace=True,kind='stable')
    return result, audit


def select_events(data: pd.DataFrame, *, cutoff: int, first: int,
                  kinds: tuple[str, ...]=KINDS, sides: tuple[str,...]=('LONG','SHORT'),
                  limit: int=240, chosen: str|None=None) -> tuple[pd.DataFrame,int]:
    if limit < 1: raise ValueError('Límite de marcadores inválido')
    # The event is known when the 1m retest closes; never reveal future events.
    m = data.loc[(data.known_ts.astype('int64')<=int(cutoff)) &
                 (data.known_ts.astype('int64')>=int(first)) &
                 data.status.isin(kinds) & data.side.isin(sides)].copy()
    n = len(m)
    if n > limit:
        # Deterministic thinning; preserve chronological distribution.
        ix = np.unique(np.linspace(0,n-1,limit,dtype=int))
        m = m.iloc[ix].copy()
        if chosen is not None and chosen in set(data.touch_key) and chosen not in set(m.touch_key):
            selected = data.loc[data.touch_key.eq(chosen)]
            if not selected.empty and selected.index[0] in data.index and bool(
                selected.known_ts.iloc[0]<=cutoff and selected.known_ts.iloc[0]>=first and
                selected.status.iloc[0] in kinds and selected.side.iloc[0] in sides):
                m=pd.concat([m,selected],ignore_index=True).drop_duplicates('touch_key')
    return m,n


def add_reaction_traces(fig: go.Figure, data: pd.DataFrame, *, selected: str|None=None) -> None:
    for kind in KINDS:
        part = data.loc[data.status.eq(kind)]
        if part.empty: continue
        hover = [f"{r.touch_key}<br>{r.side} · {r.status}<br>Known UTC: "
                 f"{pd.Timestamp(int(r.known_ts),unit='ms',tz='UTC')}"
                 f"<br>Swing referencia: {float(r.swing_price):,.2f}"
                 f"<br>HLL próximo 0.5 ATR: {int(r.hll_near_05atr_count)}"
                 f"<br>Grupo PRE: {r.hll_persistent_group}"
                 f"<br>Marcador en swing, NO precio de ejecución"
                 for r in part.itertuples(index=False)]
        is_selected=part.touch_key.eq(selected).to_numpy()
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(part.known_ts,unit='ms',utc=True),
            y=part.swing_price, mode='markers',
            name=f'{kind} · {len(part)}', text=hover,
            marker=dict(symbol=SYMBOLS[kind],size=np.where(is_selected,15,9).tolist(),
                        color=PALETTE[kind],opacity=.93,
                        line=dict(color='#101821',width=1)),
            hovertemplate='%{text}<extra></extra>'))
