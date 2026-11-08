"""Read-only, frozen HLL V2.1 visualization adapter for Runner Discovery.

Reads only previously audited offline CSV/JSON. Does NOT run HLL scans, query
Redis/Binance, modify LIVE data, or recalculate outcomes. Only one selected
First Touch is inspected/visualized at a time. PRE 30m candles and HLL level
features must be strictly known before the original retest 1m OPEN.
"""
from __future__ import annotations

from collections import deque
import csv
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

HLL_RUNS = Path('reports/reaction_lab/universal_runner_hll_lifecycle_join_v1/runs')
TF30_MS = 30 * 60 * 1000
EXPECTED_VERSION = 'universal-runner-hll-lifecycle-join-v1.0'
MARKER_VERSION = 'universal-runner-hll-pre-markers-v1.0'
MARKER_RUNS = Path('reports/reaction_lab/universal_runner_hll_pre_markers_v1/runs')
MARKER_TYPES = ('TEST_FROM_ABOVE', 'TEST_FROM_BELOW', 'DEFENSE_CONFIRMED', 'LOSS_CONFIRMED')


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _inside_project(project, path):
    project = Path(project).resolve()
    path = Path(path).resolve()
    if path != project and project not in path.parents:
        raise ValueError('La fuente HLL debe estar dentro del proyecto')
    return path


def _snapshot_stamp(run):
    return tuple((run / name).stat().st_mtime_ns for name in
                 ('audit.json', 'event_hll_labels.csv', 'hll_candidates_near_1atr.csv'))


@lru_cache(maxsize=16)
def _verify_run_cached(run_str, stamp):
    run = Path(run_str)
    audit = json.loads((run / 'audit.json').read_text(encoding='utf-8'))
    if audit.get('version') != EXPECTED_VERSION:
        raise ValueError('Versión HLL desconocida')
    if audit.get('no_post_outcomes_read') is not True or audit.get('read_only_to_project') is not True:
        raise ValueError('El informe HLL no acredita PRE causal y solo lectura')
    hashes = audit.get('source_hashes', {})
    for filename in ('event_hll_labels.csv', 'hll_candidates_near_1atr.csv'):
        if _sha(run / filename) != hashes.get(filename):
            raise ValueError('Integridad HLL inválida: ' + filename)
    return audit


def verified_run(project, run_dir):
    run = _inside_project(project, run_dir)
    required = (Path(project) / HLL_RUNS).resolve()
    if run.parent != required:
        raise ValueError('Directorio de unión HLL no permitido')
    return _verify_run_cached(str(run), _snapshot_stamp(run))


def available_runs(project):
    root = (Path(project) / HLL_RUNS).resolve()
    if not root.is_dir():
        return []
    found = []
    for p in root.iterdir():
        if not p.is_dir() or not all((p / name).is_file() for name in
            ('audit.json', 'event_hll_labels.csv', 'hll_candidates_near_1atr.csv')):
            continue
        try:
            a = verified_run(project, p)
            unknown = int(a.get('coverage', {}).get('UNKNOWN_HLL_WARMUP', 0))
            found.append((p, int(a['events']), unknown))
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return sorted(found, key=lambda v: (-v[1], v[2], v[0].name))


@lru_cache(maxsize=12)
def _metadata_cached(run_str, stamp):
    with (Path(run_str) / 'event_hll_labels.csv').open(newline='', encoding='utf-8-sig') as f:
        return {r['touch_key']: (r['symbol'], r['status'], r['hll_persistent_group'],
                                  r['hll_coverage']) for r in csv.DictReader(f)}


def audited_metadata(project, run_dir):
    verified_run(project, run_dir)
    return _metadata_cached(str(Path(run_dir).resolve()), _snapshot_stamp(Path(run_dir).resolve()))


def audited_keys(project, run_dir):
    return frozenset(audited_metadata(project, run_dir))


def _load_key(path, key):
    with path.open(newline='', encoding='utf-8-sig') as f:
        return [r for r in csv.DictReader(f) if r.get('touch_key') == key]


def load_case(project, run_dir, touch_key, *, max_candidates=10):
    audit = verified_run(project, run_dir)
    run = Path(run_dir).resolve()
    matched = _load_key(run / 'event_hll_labels.csv', touch_key)
    if len(matched) > 1:
        raise ValueError('Identidad touch_key duplicada')
    if not matched:
        return None
    r = matched[0]
    candidates = _load_key(run / 'hll_candidates_near_1atr.csv', touch_key)
    cutoff = int(r['asof_cutoff_exclusive'])
    if cutoff != int(r['retest_ts']):
        raise ValueError('HLL cutoff y First Touch no coinciden')
    if r.get('hll_coverage') != 'COMPLETE':
        return audit, r, []
    if any(int(c['formation_closed_ts']) >= cutoff or int(c['retest_ts']) != cutoff
           for c in candidates):
        raise ValueError('La línea HLL todavía no estaba confirmada en PRE')
    candidates.sort(key=lambda c: (float(c['gap_atr']), c['level_id']))
    return audit, r, candidates[:max_candidates]


def read_pre_30m(project, audit, symbol, cutoff, *, max_bars=120):
    """Read only verified, strictly PRE 30m bars from the frozen source."""
    if max_bars not in (40, 80, 120, 160, 240):
        raise ValueError('Cantidad de velas PRE fuera de opciones')
    history = _inside_project(project, audit['offline_history_dir'])
    syms = audit.get('offline_manifest_sha256', {})
    if symbol not in syms:
        raise ValueError('El símbolo no figura en la fuente HLL congelada')
    folder = history / 'symbols' / symbol
    manifest_file = _inside_project(project, folder / 'manifest.json')
    if _sha(manifest_file) != syms[symbol]:
        raise ValueError('El manifest histórico cambió desde la auditoría')
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    if manifest.get('symbol') != symbol:
        raise ValueError('Identidad de símbolo incorrecta en histórico')
    tf = manifest.get('timeframes', {}).get('30m', {})
    if tf.get('validation', {}).get('status') != 'FULL':
        raise ValueError('El histórico PRE 30m no está íntegramente verificado')
    candles = _inside_project(project, folder / '30m.csv')
    if _sha(candles) != tf.get('sha256'):
        raise ValueError('El histórico 30m cambió desde la auditoría')
    res = deque(maxlen=max_bars)
    with candles.open(newline='', encoding='utf-8-sig') as f:
        for raw in csv.DictReader(f):
            ts = int(raw['timestamp'])
            if ts + TF30_MS >= cutoff:
                break  # exclude the candle closing exactly at the retest OPEN
            o, h, l, c = (float(raw[x]) for x in ('open', 'high', 'low', 'close'))
            if not (all(math.isfinite(x) and x > 0 for x in (o, h, l, c))
                    and h >= max(o, c, l) and l <= min(o, c, h)):
                raise ValueError('Vela PRE inválida')
            res.append((ts, o, h, l, c))
    return list(res)


def candidate_options(candidates):
    """Streamlit-friendly stable ids; the default focuses the nearest HLL line."""
    choices = [('NEAREST', 'Más cercana'), ('ALL', 'Todas las líneas HLL')]
    choices.extend((c['level_id'],
                    f"{c['origin']} · {c['level_id'][:12]} · {float(c['gap_atr']):.2f} ATR "
                    f"· tests {c.get('prior_tests', '0')} · defensas {c.get('prior_defenses', '0')}")
                   for c in candidates)
    return choices


def selected_candidates(candidates, selection='NEAREST'):
    if selection == 'ALL':
        return list(candidates)
    if selection == 'NEAREST':
        return list(candidates[:1])
    matched = [c for c in candidates if c['level_id'] == selection]
    if len(matched) != 1:
        raise ValueError('La línea HLL seleccionada no pertenece al episodio auditado')
    return matched


def marker_run(project, hll_run_dir):
    """Locate optional immutable marker export linked to the selected HLL join."""
    join = _inside_project(project, hll_run_dir)
    verified_run(project, join)
    marker = (Path(project).resolve() / MARKER_RUNS / join.name).resolve()
    root = (Path(project).resolve() / MARKER_RUNS).resolve()
    if marker.parent != root or not (marker / 'audit.json').is_file():
        return None
    return marker


def _verified_marker_audit(project, join, marker):
    verified_run(project, join)
    marker = _inside_project(project, marker)
    expected_root = (Path(project).resolve() / MARKER_RUNS).resolve()
    if marker.parent != expected_root or marker.name != Path(join).name:
        raise ValueError('Exportación de marcas no asociada a esta cohorte HLL')
    meta = json.loads((marker / 'audit.json').read_text(encoding='utf-8'))
    if (meta.get('schema') != MARKER_VERSION or
        Path(meta.get('source_join_dir','')).resolve() != Path(join).resolve() or
        meta.get('reads_outcomes') is not False or
        meta.get('individual_touch_requires_asof_filter') is not True or
        meta.get('source_join_audit_sha256') != _sha(Path(join)/'audit.json') or
        meta.get('source_join_labels_sha256') != _sha(Path(join)/'event_hll_labels.csv') or
        meta.get('source_join_candidates_sha256') != _sha(Path(join)/'hll_candidates_near_1atr.csv')):
        raise ValueError('Integridad o procedencia de marcas HLL inválida')
    return meta


def load_pre_markers(project, hll_run_dir, row, candidates, *, verify_counts=True):
    """Return only PRE-confirmed observations for the exact audited First Touch.

    The marker file may contain subsequent observations for other retests.
    Nothing at or after THIS retest is returned (fail closed).
    """
    run=marker_run(project, hll_run_dir)
    if run is None:
        return None  # marker export not yet installed; never invent points
    audit=_verified_marker_audit(project,Path(hll_run_dir).resolve(),run)
    if row['hll_coverage'] != 'COMPLETE':
        return []
    symbol=row['symbol']
    file=(run/'symbols'/f'{symbol}.csv').resolve()
    if file.parent != (run/'symbols').resolve() or not file.is_file():
        raise ValueError('Faltan marcas HLL auditadas del símbolo')
    if _sha(file) != audit.get('file_sha256',{}).get(symbol):
        raise ValueError('El archivo de marcas HLL fue modificado')
    cutoff=int(row['retest_ts'])
    level_ids={c['level_id'] for c in candidates}
    by_level={c['level_id']:c for c in candidates}
    filtered=[]
    seen=set()
    with file.open(newline='',encoding='utf-8-sig') as f:
        for raw in csv.DictReader(f):
            if raw.get('level_id') not in level_ids:
                continue
            if raw.get('symbol') != symbol or raw.get('event_type') not in MARKER_TYPES:
                raise ValueError('Marca HLL con identidad o tipo inválido')
            event_id=raw['event_id']
            if event_id in seen:
                raise ValueError('Observación HLL duplicada')
            seen.add(event_id)
            when=int(raw['observed_closed_ts']); opening=int(raw['bar_open_ts'])
            if when >= cutoff:
                continue
            source=by_level[raw['level_id']]
            if (when != opening+TF30_MS or
                when <= int(source['formation_closed_ts']) or
                when >= int(row['asof_cutoff_exclusive'])):
                raise ValueError('Marca HLL no causal')
            hi,lo,close=(float(raw[x]) for x in ('high','low','close'))
            if not (all(math.isfinite(x) and x>0 for x in (hi,lo,close)) and lo<=close<=hi):
                raise ValueError('Precio de observación HLL inválido')
            kind=raw['event_type']
            price=lo if kind=='TEST_FROM_ABOVE' else hi if kind=='TEST_FROM_BELOW' else close
            filtered.append({**raw,'marker_price':price})
    filtered.sort(key=lambda x:(int(x['observed_closed_ts']),x['level_id'],x['event_id']))
    if verify_counts:
        from collections import Counter
        for c in candidates:
            counts=Counter(x['event_type'] for x in filtered if x['level_id']==c['level_id'])
            for label,field in (('prior_tests',None),('prior_defenses','DEFENSE_CONFIRMED'),
                                ('prior_losses','LOSS_CONFIRMED')):
                value=(counts['TEST_FROM_ABOVE']+counts['TEST_FROM_BELOW']
                       if field is None else counts[field])
                if value != int(c.get(label,0) or 0):
                    raise ValueError('Las marcas PRE no coinciden con el recuento congelado HLL: '+label)
    return filtered


def decorate_chart(fig, *, pre_bars, row, candidates, show_hll=True,
                   show_persistent=True, show_pre=True,
                   selected_hll='NEAREST', pre_markers=None, show_markers=True):
    """Add selected PRE HLL bands and optionally exact confirmed PRE events."""
    import pandas as pd
    import plotly.graph_objects as go
    cutoff = int(row['retest_ts'])
    visible_candles={int(b[0]) for b in pre_bars}
    if show_pre and pre_bars:
        fig.add_trace(go.Candlestick(
            x=pd.to_datetime([b[0] for b in pre_bars], unit='ms', utc=True),
            open=[b[1] for b in pre_bars], high=[b[2] for b in pre_bars],
            low=[b[3] for b in pre_bars], close=[b[4] for b in pre_bars],
            name='PRE 30m · cerrado antes del retest', opacity=0.7,
            increasing_line_color='#7596ad', decreasing_line_color='#9a788a'))
        fig.add_shape(type='line', x0=pd.to_datetime(cutoff, unit='ms', utc=True),
                      x1=pd.to_datetime(cutoff, unit='ms', utc=True),
                      yref='paper', y0=0, y1=1, line=dict(color='#888', width=1, dash='dot'))
    chosen=selected_candidates(candidates,selected_hll)
    if show_hll and row.get('hll_coverage') == 'COMPLETE':
        for i,c in enumerate(chosen):
            lo, hi, mid = (float(c[k]) for k in ('zone_low', 'zone_high', 'center'))
            if not (0 < lo <= mid <= hi):
                raise ValueError('Banda HLL inválida')
            main = float(c['gap_atr']) <= 0.5
            color = '#1e9dc8' if c['origin'] == 'HIGH' else '#a477d2'
            fig.add_hrect(y0=lo, y1=hi, fillcolor=color, opacity=0.14 if main else 0.08,
                          line_width=0)
            fig.add_hline(y=mid, line_color=color, line_width=2 if selected_hll!='ALL' else 1.3,
                          line_dash='solid' if main else 'dot',
                          annotation_text=(f"HLL {c['origin']} {c['level_id'][:7]}" if i < 3 else None),
                          annotation_position='top left')
    if show_persistent and row.get('persistent_group_pre') == 'PERSISTENT_LEVEL_NEAR_PRE':
        lo = float(row['persistent_zone_low']); hi = float(row['persistent_zone_high'])
        if not (0 < lo <= hi):
            raise ValueError('Banda persistente inválida')
        fig.add_hrect(y0=lo, y1=hi, fillcolor='#e9ad39', opacity=0.12, line_width=0)
        fig.add_hline(y=(lo + hi) / 2, line_color='#e9ad39',
                      line_dash='dash', annotation_text='Persistente 15m/30m',
                      annotation_position='bottom left')
    if show_pre and show_hll and show_markers and pre_markers is not None:
        selected_ids={c['level_id'] for c in chosen}
        visible=[m for m in pre_markers if
                 m['level_id'] in selected_ids and
                 int(m['observed_closed_ts']) < cutoff and
                 int(m['bar_open_ts']) in visible_candles]
        classes=(
            (('TEST_FROM_ABOVE','TEST_FROM_BELOW'),'Contacto confirmado PRE','#d6a451','circle',9),
            (('DEFENSE_CONFIRMED',),'Defensa confirmada PRE','#24ad78','triangle-up',11),
            (('LOSS_CONFIRMED',),'Pérdida confirmada PRE','#dc6470','x',11),
        )
        for event_types,label,color,symbol,size in classes:
            hits=[m for m in visible if m['event_type'] in event_types]
            if not hits:
                continue
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([int(m['bar_open_ts']) for m in hits],unit='ms',utc=True),
                y=[float(m['marker_price']) for m in hits],
                mode='markers',name=label,
                marker=dict(color=color,symbol=symbol,size=size,line=dict(color='#151a21',width=1)),
                customdata=[[m['level_id'][:12],m['event_type'],m['test_id'],
                             pd.to_datetime(int(m['observed_closed_ts']),unit='ms',utc=True).strftime('%d/%m %H:%M UTC')]
                            for m in hits],
                hovertemplate=(f'<b>{label}</b><br>Precio: %{{y:.6g}}<br>'
                               'Línea: %{customdata[0]}<br>Evento: %{customdata[1]}<br>'
                               'Confirmado: %{customdata[3]}<br>Test ID: %{customdata[2]}<extra></extra>'),
            ))
    fig.update_layout(legend=dict(orientation='h', y=-0.18), height=555,
                      xaxis_rangeslider_visible=False)
    return fig
