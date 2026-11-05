#!/usr/bin/env python3
"""Dataset PRE/L0 de calidad multi-swing — solo investigación OFFLINE.

Fuente de eventos: scanner_first_touches_v1.sqlite3 (solo lectura).
Fuente de velas: causal_context_v1.sqlite3 / candles_json congelado (solo lectura).
Confluencia: las MISMAS funciones de Multi-Swing Confluence Continuous V1.
Identidad física: OHLC 1m verificado, mediante multi_swing_structural_quality_v1.

No usa Redis/Binance, no modifica el scanner, el dashboard, LIVE ni el journal.
No produce labels de resultados ni puntajes predictivos. Si falta contexto se
registra como NO_FROZEN_CONTEXT, no como NO_CONFLUENCE.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from dashboard.views.reaction_continuous_multiswing_view import (
    confirmed_points, confluence_matches, normalized_snapshot,
    FIRST_TOUCH_DB, CONTEXT_DB,
)
from multi_swing_structural_quality_v1 import compute_features, ResearchInputError

SCHEMA = 'REACTION_MULTI_SWING_QUALITY_DATASET_V1'
M = 60_000
TF = ('1m', '5m')
VALID_STATES = ('REACTION', 'TOUCH_INDECISIVE', 'TOUCH_FAILED')
EVENT_COLS = ('touch_key','legacy_event_key','symbol','side','status','timeframe',
              'detector','pivot_ts','actionable_ts','retest_ts','known_ts',
              'observation_type','causal_json')


def open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f'No existe la fuente SQLite: {path}')
    # URI de SQLite read-only, sin journal ni CREATE; no PRAGMAs que escriban.
    con = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=20)
    con.execute('PRAGMA query_only=ON')
    return con


def snapshot_for_event(con: sqlite3.Connection, key: str, known_ts: int):
    row = con.execute('SELECT known_ts, status, candles_json FROM contexts WHERE event_key=?',
                      (key,)).fetchone()
    if row is None:
        return None, 'NO_FROZEN_CONTEXT'
    if int(row[0]) != int(known_ts):
        return None, 'FROZEN_KNOWN_TS_MISMATCH'
    try:
        snapshots = json.loads(row[2])
    except (ValueError, TypeError):
        return None, 'INVALID_CONTEXT_JSON'
    if not isinstance(snapshots, dict):
        return None, 'INVALID_CONTEXT_JSON'
    # status of context is not the label of the first-touch event.
    return snapshots, str(row[1])


def _price(original: Mapping):
    for col in ('swing_price','pivot_price'):
        try:
            val = float(original.get(col))
            if 0 < val < float('inf'):
                return val, col
        except (TypeError, ValueError):
            pass
    raise ResearchInputError('swing_price y pivot_price no disponibles en causal_json')


def make_feature_row(touch: Mapping, snapshots: Mapping, *, tolerance_pct=0.10,
                     bars=5, prominence_pct=0.0) -> dict:
    """Una identidad touch_key -> una fila; mismas coincidencias del visor 15m.

    Solo el estado original del journal define REACTION/FAILED/INDECISIVE.
    Los snapshots se cierran as-of known_ts. El 15m original es autoridad
    canónica, aun si el snapshot corto no permite redetectarlo.
    """
    if touch.get('timeframe') != '15m' or touch.get('detector') != '3x3':
        raise ResearchInputError('Este estudio requiere primer retest original 15m/3x3')
    known = int(touch['known_ts'])
    event = {k: touch[k] for k in ('symbol','side','status')}
    event.update(pivot_ts=int(touch['pivot_ts']),
                 actionable_ts=int(touch['actionable_ts']),
                 retest_ts=int(touch['retest_ts']),known_ts=known,
                 event_key=str(touch['legacy_event_key']))
    raw = json.loads(touch['causal_json']) if isinstance(touch.get('causal_json'), str) else touch.get('causal_json', {})
    if not isinstance(raw, dict):
        raise ResearchInputError('causal_json inválido')
    price, price_source = _price(raw)
    event['swing_price'] = price
    side = 'LOW' if event['side'] == 'LONG' else 'HIGH'
    if not isinstance(snapshots, Mapping):
        raise ResearchInputError('snapshots faltantes o inválidos')

    # Selector real del visor: 1 swing elegido POR TIMEFRAME y bucket de 15m.
    points_by_tf = {}
    coverage = {}
    for tf in TF:
        frame, points = confirmed_points(snapshots.get(tf, []), tf, known,
                                         int(bars), float(prominence_pct))
        points_by_tf[tf] = points
        coverage[f'closed_{tf}_bars'] = len(frame)
    matches = confluence_matches(points_by_tf, pivot_ts=event['pivot_ts'],
                                 pivot_price=price, side=side, known_ts=known,
                                 tolerance_pct=tolerance_pct, time_bucket='15m')
    pivots = [dict(timeframe='15m',side=side,pivot_ts=event['pivot_ts'],
                   available_ts=event['actionable_ts'],price=price,
                   source='SCANNER_ORIGINAL')]
    for m in matches:
        pivots.append(dict(timeframe=m['timeframe'],side=m['side'],
                           pivot_ts=m['pivot_ts'],available_ts=m['confirmed_available_ts'],
                           price=m['price'],prominence_pct=m['prominence_pct'],
                           source='FROZEN_SNAPSHOT'))
    # Physical identity is derived ONLY from closed 1m frozen OHLC.
    frozen_1m = normalized_snapshot(snapshots.get('1m',[]),'1m',known)
    features = compute_features(event, pivots, frozen_1m.to_dict('records'),
                                proximity_pct=float(tolerance_pct))
    # Fail rather than record a dataset that disagrees with the current UI.
    if features['coincident_tf_count'] != 1 + len(matches):
        raise ResearchInputError('Paridad de conteos con Multi-Swing UI fallida')
    result = dict(features)
    result.update(coverage)
    result['closed_1m_bars'] = len(frozen_1m)
    result['touch_key'] = str(touch['touch_key'])
    result['legacy_event_key'] = str(touch['legacy_event_key'])
    result['observation_type'] = str(touch['observation_type'])
    result['price_source'] = price_source
    result['matched_timeframes'] = ','.join(['15m']+[m['timeframe'] for m in matches])
    result['quality_state'] = ('FEATURES_OK' if features['physical_identity_complete']
                               else 'PHYSICAL_IDENTITY_UNKNOWN')
    result['dataset_version'] = SCHEMA
    return result


def _touch_rows(con: sqlite3.Connection, *, symbols=(), limit=100, status='ALL'):
    where = ["timeframe='15m'", "detector='3x3'"]
    args = []
    if symbols:
        where.append('symbol IN (' + ','.join('?' for _ in symbols) + ')')
        args += [s.upper() for s in symbols]
    if status != 'ALL':
        where.append('status=?')
        args.append(status)
    sql = 'SELECT '+', '.join(EVENT_COLS)+' FROM first_touches WHERE '+' AND '.join(where)
    # Fixed ordering; stable across reruns. Avoid unbounded scans by default.
    sql += ' ORDER BY retest_ts DESC,touch_key ASC LIMIT ?'
    for row in con.execute(sql, [*args,int(limit)]):
        yield dict(zip(EVENT_COLS,row))


def run_cohort(project: Path, *, symbols=(), limit=100, status='ALL',
               tolerance_pct=0.10, bars=5, prominence_pct=0.0):
    """Return per-touch rows and summary, without writing anything."""
    project = Path(project)
    db = project/FIRST_TOUCH_DB
    ctx = project/CONTEXT_DB
    output=[]; statuses=Counter();reasons=Counter()
    with closing(open_ro(db)) as events, closing(open_ro(ctx)) as contexts:
        for touch in _touch_rows(events,symbols=symbols,limit=limit,status=status):
            outcome={'touch_key':touch['touch_key'],
                     'legacy_event_key':touch['legacy_event_key'],
                     'symbol':touch['symbol'],'side':touch['side'],
                     'original_status':touch['status'],
                     'observation_type':touch['observation_type'],
                     'pivot_ts':touch['pivot_ts'],'retest_ts':touch['retest_ts'],
                     'known_ts':touch['known_ts'],
                     'quality_state':'','failure_reason':'',
                     'dataset_version':SCHEMA}
            snapshots,context_state=snapshot_for_event(contexts, touch['legacy_event_key'],
                                                       touch['known_ts'])
            outcome['frozen_context_status'] = context_state
            if snapshots is None:
                outcome.update(quality_state='NOT_COVERED', failure_reason=context_state)
            else:
                try:
                    feature=make_feature_row(touch,snapshots,tolerance_pct=tolerance_pct,
                                             bars=bars,prominence_pct=prominence_pct)
                    outcome.update(feature)
                    if outcome['quality_state']=='PHYSICAL_IDENTITY_UNKNOWN':
                        outcome['failure_reason']='INCOMPLETE_OR_AMBIGUOUS_1M_OHLC'
                except (ResearchInputError,KeyError,ValueError,TypeError,OverflowError) as exc:
                    outcome.update(quality_state='FEATURE_ERROR',failure_reason=str(exc))
            output.append(outcome)
            statuses[outcome['original_status']]+=1
            reasons[outcome['quality_state']]+=1
    summary = {'dataset_version':SCHEMA, 'sampled_touches':len(output),
               'event_statuses':dict(statuses),'feature_states':dict(reasons),
               'symbols':list(symbols),'limit':limit,'status_filter':status,
               'tolerance_pct':tolerance_pct,'detector_bars_1m5m':bars,
               'prominence_pct':prominence_pct,
               'sources':{'journal':str(db),'frozen_context':str(ctx)},
               'warning':('NOT_COVERED is not NO_CONFLUENCE. Original causal_context '
                          'was built primarily for REACTIONs. This export is NOT '
                          'a balanced supervised training cohort.')}
    return output,summary


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_run(rows, meta, dest: Path, *, code_root: Path):
    """Create new output directory; refuse overwrites."""
    dest=Path(dest)
    dest.mkdir(parents=True,exist_ok=False)
    columns=list(dict.fromkeys(k for row in rows for k in row))
    if not columns:
        columns=['touch_key','quality_state','failure_reason','dataset_version']
    csv_path=dest/'features.csv'
    with csv_path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        w.writeheader()
        for row in rows:w.writerow(row)
    fingerprints={}
    for rel in ('multi_swing_structural_quality_v1.py',
                'reaction_multi_swing_quality_dataset_v1.py',
                'dashboard/views/reaction_continuous_multiswing_view.py',
                'engine/live/research/swing_detector.py'):
        p=code_root/rel
        fingerprints[rel]=_sha(p) if p.is_file() else 'MISSING'
    meta=dict(meta,source_sha256=fingerprints,output_sha256=_sha(csv_path))
    (dest/'manifest.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    return csv_path


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path.cwd(),
                    help='Raíz actual del repo para localizar el journal')
    ap.add_argument('--symbol',action='append',default=[],
                    help='Filtro de símbolo; repetible, ej --symbol BTCUSDT')
    ap.add_argument('--limit',type=int,default=100)
    ap.add_argument('--status',choices=('ALL',*VALID_STATES),default='ALL')
    ap.add_argument('--tolerance-pct',type=float,default=0.10)
    ap.add_argument('--bars',type=int,default=5)
    ap.add_argument('--prominence-pct',type=float,default=0.0)
    ap.add_argument('--out',type=Path,required=True,
                    help='Directorio nuevo de salida: NO reemplaza archivos previos')
    args=ap.parse_args(argv)
    if not 1 <= args.limit <= 10000:ap.error('--limit debe estar entre 1 y 10000')
    if args.bars not in (2,3,5):ap.error('--bars debe ser 2, 3 o 5')
    if not 0<=args.tolerance_pct<=5:ap.error('tolerance-pct inválido')
    if not 0<=args.prominence_pct<=5:ap.error('prominence-pct inválido')
    rows,meta=run_cohort(args.root,symbols=tuple(args.symbol),limit=args.limit,
                         status=args.status,tolerance_pct=args.tolerance_pct,
                         bars=args.bars,prominence_pct=args.prominence_pct)
    csv_path=write_run(rows,meta,args.out,code_root=Path(__file__).resolve().parent)
    print(json.dumps({'csv':str(csv_path),'manifest':str(args.out/'manifest.json'),
                      'states':meta['feature_states'],'events':len(rows)},ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
