#!/usr/bin/env python3
"""Dataset offline V1.1: confluencia V1 + reconstruccion PRE-TOUCH.

Sólo lee las dos bases SQLite originales en modo read-only. No hace llamadas de
red, no modifica scanner, LIVE, ni dashboard. Nunca ejecuta labels POST.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
from typing import Mapping

from dashboard.views.reaction_continuous_multiswing_view import (
    confirmed_points, normalized_snapshot,
)
from reaction_multi_swing_quality_dataset_v1 import (
    open_ro, snapshot_for_event, _touch_rows, make_feature_row,
    FIRST_TOUCH_DB, CONTEXT_DB, VALID_STATES, _price,
)
from multi_swing_structural_quality_v1 import ResearchInputError
from multi_swing_reconstruction_v11 import compute_reconstruction, SCHEMA

DATASET_SCHEMA = 'REACTION_MULTI_SWING_QUALITY_DATASET_V11'
TF_BARS = {'1m':5,'5m':5,'15m':3}
TF_STEP = {'1m':60_000,'5m':300_000,'15m':900_000}


def make_v11_row(touch: Mapping, snapshots: Mapping, *, proximity_pct=0.10,
                 windows=(30,60,120,240)) -> dict:
    """Usa el detector y los snapshots existentes; no inventa nuevos pivotes."""
    if touch.get('timeframe') != '15m' or touch.get('detector') != '3x3':
        raise ResearchInputError('Retest 15m 3x3 obligatorio')
    known=int(touch['known_ts'])
    cutoff=int(touch['retest_ts'])
    raw=json.loads(touch['causal_json']) if isinstance(touch.get('causal_json'),str) else touch.get('causal_json',{})
    if not isinstance(raw,dict):
        raise ResearchInputError('causal_json invalido')
    price,_=_price(raw)
    event={'symbol':touch['symbol'],'side':touch['side'],'status':touch['status'],
           'pivot_ts':int(touch['pivot_ts']),'actionable_ts':int(touch['actionable_ts']),
           'retest_ts':cutoff,'known_ts':known,'swing_price':price,
           'event_key':touch['legacy_event_key']}
    direction='LOW' if event['side']=='LONG' else 'HIGH'

    # OBS: el 15m del scanner original sigue siendo canonico, aunque
    # su pivot quede fuera del warmup del snapshot del visor.
    safe_anchor_ts=max(event['actionable_ts'],event['pivot_ts']+4*15*60_000)
    pivot_rows=[{'timeframe':'15m','side':direction,
                 'pivot_ts':event['pivot_ts'],'available_ts':safe_anchor_ts,
                 'price':price,'source':'SCANNER_ORIGINAL'}]
    timestamps={}
    anchor_snapshot_check='NOT_REDETECTED'
    for tf,bars in TF_BARS.items():
        frame,points=confirmed_points(snapshots.get(tf,[]),tf,cutoff,bars,0.0)
        timestamps[tf]=tuple(int(x) for x in frame.timestamp.tolist())
        for p in points:
            item={'timeframe':tf,'side':p.side,'pivot_ts':int(p.pivot_timestamp),
                  'available_ts':int(p.confirmed_timestamp + TF_STEP[tf]),
                  'price':float(p.price),'prominence_pct':p.prominence_pct,
                  'source':'FROZEN_SNAPSHOT'}
            if (item['timeframe'],item['pivot_ts'],item['side']) == ('15m',event['pivot_ts'],direction):
                # Fuentes repetidas: el original es autoridad. Si la detectada
                # contradice al original, reportar en lugar de tapar.
                if abs(item['price']/price-1)>1e-10:
                    raise ResearchInputError('Precio original 15m difiere del snapshot 15m')
                # La fuente original puede guardar el OPEN de ultima barra
                # derecha mientras el visor expone su CIERRE. Reportar,
                # no sobrescribir la identidad original ni usar el OPEN.
                anchor_snapshot_check=('MATCH' if item['available_ts']==event['actionable_ts']
                                       else 'KNOWN_TS_CONVENTION_DIFFERENCE')
                continue
            pivot_rows.append(item)
    frame_1m=normalized_snapshot(snapshots.get('1m',[]),'1m',cutoff)
    result=compute_reconstruction(event,pivot_rows,frame_1m.to_dict('records'),
                                  timestamps,windows=windows,proximity_pct=proximity_pct,
                                  detector_bars=TF_BARS)
    result['anchor_snapshot_check']=anchor_snapshot_check
    return result


def run_cohort_v11(project: Path, *, symbols=(),limit=100,status='ALL',
                   proximity_pct=0.10, windows=(30,60,120,240)):
    """Ambas fases independientes: V1 L0 y V1.1 PRE-TOUCH."""
    base=Path(project)
    output=[]; original_status=Counter(); state=Counter()
    with closing(open_ro(base/FIRST_TOUCH_DB)) as journal, \
         closing(open_ro(base/CONTEXT_DB)) as contexts:
        for touch in _touch_rows(journal,symbols=symbols,limit=limit,status=status):
            row={'touch_key':touch['touch_key'],
                 'legacy_event_key':touch['legacy_event_key'],
                 'symbol':touch['symbol'],'side':touch['side'],
                 'original_status':touch['status'],
                 'observation_type':touch['observation_type'],
                 'pivot_ts':touch['pivot_ts'],'retest_ts':touch['retest_ts'],
                 'known_ts':touch['known_ts'],'dataset_version':DATASET_SCHEMA}
            snapshots,context_status=snapshot_for_event(contexts,touch['legacy_event_key'],
                                                        touch['known_ts'])
            row['frozen_context_status']=context_status
            if snapshots is None:
                row.update(v1_state='NOT_COVERED',v11_state='NOT_COVERED',
                           failure_reason=context_status)
            else:
                try:
                    base_v1=make_feature_row(touch,snapshots)
                    row['v1_state']=base_v1.pop('quality_state')
                    row.update(base_v1)
                    # Version del dataset V1 del estado ORIGINAL no se sobreescribe.
                    row['dataset_version']=DATASET_SCHEMA
                except (ResearchInputError,KeyError,ValueError,TypeError,OverflowError) as ex:
                    row.update(v1_state='FEATURE_ERROR',v1_failure_reason=str(ex))
                try:
                    update=make_v11_row(touch,snapshots,proximity_pct=proximity_pct,
                                        windows=windows)
                    row.update(update)
                    ncovered=sum(row.get(f'w{w}_coverage_state')=='COVERED' for w in windows)
                    row['v11_state']=('V11_OK' if ncovered==len(windows)
                                      else 'V11_PARTIAL' if ncovered else 'V11_NOT_COVERED')
                except (ResearchInputError,KeyError,ValueError,TypeError,OverflowError) as ex:
                    row.update(v11_state='FEATURE_ERROR',failure_reason=str(ex))
            output.append(row)
            original_status[row['original_status']]+=1
            state[row['v11_state']]+=1
    return output,{'dataset_version':DATASET_SCHEMA,
                    'sampled_touches':len(output),
                    'original_statuses':dict(original_status),
                    'v11_states':dict(state),
                    'symbols':list(symbols),'limit':limit,'status_filter':status,
                    'structural_cutoff':'retest_ts (PRE_TOUCH)',
                    'original_classification_known_at':'retest_ts+60000',
                    'windows_min':list(windows),
                    'zone_proximity_pct':proximity_pct,
                    'detector_bars':TF_BARS,
                    'sources':{'first_touches':str(base/FIRST_TOUCH_DB),
                               'frozen_context':str(base/CONTEXT_DB)},
                    'warning':('Las ventanas sin cobertura no equivalen a cero swings. '
                               'La base causal congelada puede estar sesgada hacia REACTION. '
                               'No se incluyen outcomes ni existe entrenamiento de estrategia.')}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_run_v11(rows, meta, dest: Path, *, code_root: Path):
    dest=Path(dest)
    dest.mkdir(parents=True,exist_ok=False)
    columns=list(dict.fromkeys(k for row in rows for k in row)) or ['touch_key','v11_state']
    file=dest/'features.csv'
    with file.open('w',newline='',encoding='utf-8') as out:
        writer=csv.DictWriter(out,fieldnames=columns)
        writer.writeheader();writer.writerows(rows)
    sources={}
    for path in ('reaction_multi_swing_quality_dataset_v11.py',
                 'multi_swing_reconstruction_v11.py',
                 'reaction_multi_swing_quality_dataset_v1.py',
                 'multi_swing_structural_quality_v1.py',
                 'dashboard/views/reaction_continuous_multiswing_view.py',
                 'engine/live/research/swing_detector.py'):
        f=Path(code_root)/path
        sources[path]=_sha(f) if f.is_file() else 'MISSING'
    manifest=dict(meta,source_sha256=sources,output_sha256=_sha(file))
    (dest/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),
                                      encoding='utf-8')
    return file


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path.cwd())
    ap.add_argument('--symbol',action='append',default=[])
    ap.add_argument('--limit',type=int,default=100)
    ap.add_argument('--status',choices=('ALL',*VALID_STATES),default='REACTION')
    ap.add_argument('--proximity-pct',type=float,default=0.10)
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args(argv)
    if not 1<=args.limit<=10000:ap.error('limit fuera de 1..10000')
    if not 0<=args.proximity_pct<=5:ap.error('proximity-pct fuera de 0..5')
    rows,meta=run_cohort_v11(args.root,symbols=tuple(args.symbol),limit=args.limit,
                             status=args.status,proximity_pct=args.proximity_pct)
    file=write_run_v11(rows,meta,args.out,code_root=Path(__file__).parent)
    print(json.dumps({'csv':str(file),'manifest':str(args.out/'manifest.json'),
                      'states':meta['v11_states'],'events':len(rows)},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
