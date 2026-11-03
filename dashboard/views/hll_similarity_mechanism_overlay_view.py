"""HLL | read-only historical POST inspector inside the existing Structural Discovery PRE.

Only consumes the prebuilt, audited V1 overlay by exact Attempt ID. PRE selection,
ranking and fingerprints remain wholly independent from this module.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

import hll_fingerprint_mechanism_join_audit_v1 as audit
from hll_similarity_mechanism_adapter_v1 import (
    DEFAULT_OUTPUT, ROUTES, VERSION, load_attempt,
)

CANDLE_MS = 30 * 60 * 1000
ROUTE_LABELS = {
    'FLIP_1M': 'Flip V2.6',
    'V29_CONTINUATION_30M': 'Continuación V2.9',
    'V29_RECLAIM_30M': 'Reclaim V2.9',
}
ENTRY_LABELS = {
    'FLIP_1M': ('ENTRADA FLIP', '#21c98d', 'diamond'),
    'V29_CONTINUATION_30M': ('ENTRADA CONT.', '#4cbfee', 'circle'),
    'V29_RECLAIM_30M': ('ENTRADA RECLAIM', '#c191f5', 'star'),
}
CONFIRMATION_COLORS = {
    'FLIP_1M': '#21c98d',
    'V29_CONTINUATION_30M': '#4cbfee',
    'V29_RECLAIM_30M': '#c191f5',
}
ENTRY_STATES = {'NOT_COVERED', 'NO_SIGNAL', 'SIGNAL_OPEN_MISSING', 'ENTRY_VERIFIED'}


def _check(condition, message):
    if not condition:
        raise ValueError('Overlay HLL incompatible: ' + message)


def _clock(value):
    if value is None or str(value).strip() == '':
        return None
    stable = audit.stable_ts(str(value))
    return int(stable) if stable != '' else None


def _utc(value):
    ts = _clock(value)
    return pd.Timestamp(ts, unit='ms', tz='UTC') if ts is not None else None


def overlay_paths(root):
    folder = Path(root) / DEFAULT_OUTPUT
    return folder / 'candidate_mechanism_overlay.sqlite3', folder / 'manifest.json'


def read_attempt(root, attempt_id, symbol, level_id, pre_closed_ts, episode_id=None):
    """Validate immutable provenance and exact attempt identity before exposing POST.

    Returns None only if overlay is not installed. A present but invalid overlay
    MUST fail closed: do not silently replace records with inferred signals.
    """
    root = Path(root)
    db, manifest_file = overlay_paths(root)
    if not db.is_file() and not manifest_file.is_file():
        return None
    _check(db.is_file() and manifest_file.is_file(), 'faltan archivos del adaptador')
    manifest = json.loads(manifest_file.read_text(encoding='utf8'))
    _check(manifest.get('version') == VERSION and manifest.get('status') == 'DONE',
           'versión/estado de adaptador')
    _check(manifest.get('scope') == 'ALL_ATTEMPTS_BY_EXACT_ATTEMPT_ID' and
           manifest.get('entry_scope') == 'FIRST_PER_LEVEL_ONLY' and
           manifest.get('ranker_changed') is False and manifest.get('pre_features_changed') is False,
           'alcance o contrato PRE')
    _check(int(manifest.get('routes_per_attempt', 0)) == len(ROUTES), 'número de rutas')
    # The source manifests are already independently verified by the PRE loaders.
    similarity_manifest = root / 'reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1/manifest.json'
    overlap_manifest = root / 'reports/horizontal_level_structural_overlap_audit_v1/full290_v1/manifest.json'
    _check(similarity_manifest.is_file() and overlap_manifest.is_file(), 'faltan manifiestos de procedencia')
    sim = json.loads(similarity_manifest.read_text(encoding='utf8'))
    eps = json.loads(overlap_manifest.read_text(encoding='utf8'))
    _check(manifest.get('source_fingerprints_sha256') == sim.get('fingerprints_sha256'),
           'huella PRE distinta de la fuente de overlay')
    _check(manifest.get('source_episode_map_sha256') == eps.get('outputs_sha256', {}).get('attempt_episode_map.csv'),
           'mapa de episodios distinto de la fuente de overlay')
    _check(manifest.get('output_db_sha256') == audit.digest(db),
           'SQLite no coincide con el manifiesto del adaptador')

    records = load_attempt(db, str(attempt_id))
    _check(len(records) == len(ROUTES), 'faltan rutas para Attempt ID ' + str(attempt_id))
    _check({r['route'] for r in records} == set(ROUTES), 'rutas duplicadas/desconocidas')
    pre_ms = _clock(pre_closed_ts)
    _check(pre_ms is not None, 'corte PRE inválido')
    for r in records:
        _check(r['audit_match'] == 'MATCHED' and r['post_is_retrospective'] == 'true',
               'registro no auditado')
        _check(r['attempt_id'] == str(attempt_id) and r['symbol'] == str(symbol) and
               r['level_id'] == str(level_id) and _clock(r['pre_closed_ts']) == pre_ms,
               'identidad de intento/fecha/símbolo/nivel')
        if episode_id is not None:
            _check(r['episode_id'] == str(episode_id), 'episodio diferente')
        status = r['entry_status']
        _check(status in ENTRY_STATES, 'estado de cobertura desconocido')
        confirmed = _clock(r['confirmed_closed_ts'])
        if confirmed is not None:
            _check(confirmed >= pre_ms, 'confirmación antes del corte PRE')
        if status == 'ENTRY_VERIFIED':
            _check(bool(r['entry_id']) and bool(r['entry_open_execution_1m']),
                   'entrada verificada sin identidad/precio')
            price = float(r['entry_open_execution_1m'])
            _check(math.isfinite(price) and price > 0, 'precio OPEN 1m inválido')
            entry_ts = _clock(r['entry_ts'])
            signal_ts = _clock(r['signal_ts'])
            _check(entry_ts is not None and signal_ts is not None and
                   pre_ms <= signal_ts <= entry_ts and
                   (confirmed is None or confirmed <= entry_ts),
                   'entrada sin reloj causal verificable')
            if r['route'] == 'FLIP_1M':
                _check(confirmed is not None and entry_ts == confirmed + 60_000,
                       'flip no corresponde al siguiente OPEN 1m')
            else:
                _check(confirmed is not None and entry_ts == confirmed,
                       'continuación/reclaim no corresponde a OPEN 30m siguiente')
        else:
            _check(not r['entry_id'] and not r['entry_open_execution_1m'],
                   'entrada inventada en ruta sin OPEN verificado')
        if r['open_source_discrepancy'] == 'true':
            _check(status == 'ENTRY_VERIFIED', 'discrepancia OPEN sin entrada verificada')
        else:
            _check(r['open_source_discrepancy'] == 'false', 'flag de OPEN desconocido')
    return records


def mechanism_table(records):
    """Expose original statuses, clearly separating confirmation from executable OPEN."""
    result = []
    for r in records:
        entry = r['entry_status'] == 'ENTRY_VERIFIED'
        result.append({
            'Ruta': ROUTE_LABELS[r['route']],
            'Estado mecanismo (original)': r['mechanism_status'],
            'Confirmación UTC': _utc(r['confirmed_closed_ts']),
            'Cobertura FIRST_PER_LEVEL': r['entry_status'],
            'Entrada UTC': _utc(r['entry_ts']) if entry else None,
            'OPEN ejecutable 1m': float(r['entry_open_execution_1m']) if entry else None,
            'Referencia señal': (float(r['entry_open_signal_reference'])
                if r['entry_open_signal_reference'] and entry else None),
            'TF referencia': r['signal_reference_timeframe'] if entry else None,
            'Diferencia OPEN': r['open_source_discrepancy'] == 'true',
        })
    return pd.DataFrame(result)


def add_post_markers(fig, records, pre_closed_ts, visible_post_bars, *, reveal=False):
    """Plot only revealed, actually timestamped POST confirmations/verified entries.

    The 30m chart may contain a 1m entry timestamp between 30m candle opens.
    Never extrapolate or draw a marker outside the visible 30m POST window.
    """
    if not reveal or visible_post_bars < 1 or not records:
        return fig
    import plotly.graph_objects as go
    pre = _clock(pre_closed_ts)
    end = pre + int(visible_post_bars) * CANDLE_MS
    for r in records:
        route = r['route']
        state = r['mechanism_status']
        is_confirmed = (state.startswith('FLIP_CONFIRMED') if route == 'FLIP_1M'
                        else state == 'CONFIRMED')
        confirmed = _clock(r['confirmed_closed_ts'])
        if is_confirmed and confirmed is not None and pre <= confirmed < end:
            when = pd.Timestamp(confirmed, unit='ms', tz='UTC')
            fig.add_vline(x=when, line_color=CONFIRMATION_COLORS[route],
                          line_dash='dot', opacity=0.8)
            fig.add_annotation(x=when, y=1, xref='x', yref='paper', yanchor='bottom',
                               showarrow=False, text=ROUTE_LABELS[route] + ' confirm.',
                               font=dict(color=CONFIRMATION_COLORS[route], size=9))
        if r['entry_status'] != 'ENTRY_VERIFIED':
            continue
        entry_ms = _clock(r['entry_ts'])
        if not (pre <= entry_ms < end):
            continue
        label, color, symbol = ENTRY_LABELS[route]
        price = float(r['entry_open_execution_1m'])
        when = pd.Timestamp(entry_ms, unit='ms', tz='UTC')
        fig.add_trace(go.Scatter(x=[when], y=[price], mode='markers+text',
            text=[label], textposition='top center',
            marker=dict(color=color, symbol=symbol, size=13,
                        line=dict(color='#152231', width=1.5)),
            textfont=dict(color=color, size=10), name=ROUTE_LABELS[route] + ' · OPEN 1m verificado',
            customdata=[[str(when), r['route']]],
            hovertemplate=('<b>' + label + '</b><br>UTC: %{customdata[0]}'
                           '<br>OPEN ejecutable 1m: %{y:.8g}'
                           '<br>Simulación histórica, no ejecución real<extra></extra>')))
    return fig
