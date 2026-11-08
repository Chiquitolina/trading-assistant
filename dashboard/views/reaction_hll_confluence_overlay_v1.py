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


def decorate_chart(fig, *, pre_bars, row, candidates, show_hll=True,
                   show_persistent=True, show_pre=True):
    """Mutate a Plotly figure in memory; returns metadata for a caption."""
    import pandas as pd
    import plotly.graph_objects as go
    cutoff = int(row['retest_ts'])
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
    if show_hll and row.get('hll_coverage') == 'COMPLETE':
        for i, c in enumerate(candidates[:8]):
            lo, hi, mid = (float(c[k]) for k in ('zone_low', 'zone_high', 'center'))
            if not (0 < lo <= mid <= hi):
                raise ValueError('Banda HLL inválida')
            main = float(c['gap_atr']) <= 0.5
            color = '#1e9dc8' if c['origin'] == 'HIGH' else '#a477d2'
            fig.add_hrect(y0=lo, y1=hi, fillcolor=color, opacity=0.10 if main else 0.05,
                          line_width=0)
            fig.add_hline(y=mid, line_color=color, line_width=1.8 if main else 1,
                          line_dash='solid' if main else 'dot',
                          annotation_text=(f"HLL {c['origin']} {c['level_id'][:6]}" if i < 4 else None),
                          annotation_position='top left')
    if show_persistent and row.get('persistent_group_pre') == 'PERSISTENT_LEVEL_NEAR_PRE':
        lo = float(row['persistent_zone_low'])
        hi = float(row['persistent_zone_high'])
        if not (0 < lo <= hi):
            raise ValueError('Banda persistente inválida')
        fig.add_hrect(y0=lo, y1=hi, fillcolor='#e9ad39', opacity=0.12, line_width=0)
        fig.add_hline(y=(lo + hi) / 2, line_color='#e9ad39',
                      line_dash='dash', annotation_text='Persistente 15m/30m',
                      annotation_position='bottom left')
    fig.update_layout(legend=dict(orientation='h', y=-0.18), height=555,
                      xaxis_rangeslider_visible=False)
    return fig
