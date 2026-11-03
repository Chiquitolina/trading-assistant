#!/usr/bin/env python3
"""HLL | Cohorte de procesos de interacción con resistencia V1.

Sólo lectura de históricos congelados. Concentra observaciones del Inspector Universal
V1 en procesos descriptivos, sin redefinir Flip/Continuation/Reclaim ni fabricar entradas.

Regla exploratoria INTERMITTENT_4: en las cuatro primeras velas cerradas del corte
existen >=2 cierres ABOVE, >=1 IN_ZONE, 0 BELOW y ninguna pareja de cierres ABOVE.
No es una señal: se conoce al cierre de la cuarta vela, no antes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from hll_resistance_universal_audit_v1 import price_behavior, TF
from hll_detector_miss_audit_v1 import (
    PRE, V29, CANDLES, EPISODES, read_rows, select_symbols, load_bars,
    millis, source_check, sha256,
)

VERSION = 'HLL_RESISTANCE_PROCESS_COHORT_V1'
LEVELS = Path('reports/horizontal_level_structure_lab_v26/runs/sep290_frozen_v26_roundtrip/levels_features.csv')
OUTPUT = Path('reports/horizontal_level_resistance_process_cohort_v1')


def early4(rows):
    """Only uses the four closed bars, never later outcomes."""
    if len(rows) < 4:
        return {'early4_state': 'INCOMPLETE', 'early4_pattern': '', 'early4_intermit': False,
                'early4_two_above': False, 'known_closed_ts': ''}
    first = rows[:4]
    relations = [r['relation'] for r in first]
    pair = any(relations[i-1] == relations[i] == 'ABOVE' for i in range(1,4))
    intermit = (relations.count('ABOVE') >= 2 and
                relations.count('IN_ZONE') >= 1 and
                relations.count('BELOW') == 0 and not pair)
    return {'early4_state':'KNOWN', 'early4_pattern':'>'.join(relations),
            'early4_intermit':intermit, 'early4_two_above':pair,
            'known_closed_ts':first[-1]['known_closed_ts']}


def consolidate_touch_processes(bars, *, max_gap_bars=2):
    """Cluster contact observations by offset with up to two empty intervening bars.

    A cluster is DESCRIPTIVE, not a complete resistance lifecycle or an entry.
    Events may belong to more than one attempt of the same historical episode.
    """
    if not isinstance(max_gap_bars,int) or max_gap_bars < 0:
        raise ValueError('Bad grouping gap')
    touches = [r for r in bars if r['physical_touch']]
    clusters = []
    for r in touches:
        if not clusters or r['bar_offset'] - clusters[-1]['last_offset'] - 1 > max_gap_bars:
            clusters.append({'start_offset':r['bar_offset'],'last_offset':r['bar_offset'],
                'first_known_closed_ts':r['known_closed_ts'],
                'last_known_closed_ts':r['known_closed_ts'],
                'physical_bars':0,'from_above_bars':0,'loss_crosses':0,'recovery_crosses':0})
        cluster = clusters[-1]
        cluster['last_offset'] = r['bar_offset']
        cluster['last_known_closed_ts'] = r['known_closed_ts']
        cluster['physical_bars'] += 1
        cluster['from_above_bars'] += int(r['touch_from_above'])
        cluster['loss_crosses'] += int(r['cross_down'])
        cluster['recovery_crosses'] += int(r['cross_up'])
    # Events outside touch candles remain in the global bar/event record; cluster
    # counts are intentionally NOT a count of all losses and recoveries.
    for c in clusters:
        c['span_bars'] = 1 + c['last_offset'] - c['start_offset']
    return clusters


def slice_candles(candles, pre, horizon=96):
    """Convert loaded 30m OHLC to precisely the small window inspected in the UI."""
    result = {}
    for offset in range(-1, horizon):
        t = pre + offset*TF
        bar = candles.get(t)
        if bar is None:
            continue
        o,h,l,c = bar
        result[t] = dict(open_ts=t, close_ts=t+TF, open=o,high=h,low=l,close=c)
    return result


def load_level_atr(root, selected):
    path = root/LEVELS
    if not path.is_file():
        return {}, 'FILE_MISSING'
    cols = {'symbol','level_id','zone_low','zone_high','atr_at_formation'}
    with path.open(encoding='utf-8-sig',newline='') as f:
        fieldnames = set(csv.DictReader(f).fieldnames or [])
    if not cols.issubset(fieldnames):
        return {}, 'SCHEMA_MISMATCH'
    levels={}
    for row in read_rows(path):
        if row['symbol'] not in selected:
            continue
        key = row['symbol'], row['level_id']
        if key in levels:
            levels[key] = None # refuse duplicates
        else:
            levels[key] = row
    return levels, 'AVAILABLE'


def formation_atr(levels, row):
    level = levels.get((row['symbol'],row['level_id']))
    if level is None:
        return None, 'MISSING_OR_DUPLICATE'
    try:
        lo, hi, formation = [float(level[k]) for k in ('zone_low','zone_high','atr_at_formation')]
        expected_lo, expected_hi = float(row['zone_low']),float(row['zone_high'])
    except (TypeError, KeyError, ValueError):
        return None, 'INVALID_LEVEL'
    if (formation <= 0 or abs(lo-expected_lo)>max(abs(lo),1e-12)*1e-10 or
            abs(hi-expected_hi)>max(abs(hi),1e-12)*1e-10):
        return None,'ZONE_MISMATCH'
    return formation,'VERIFIED'


def summarize_one(row, candles, levels, statuses, episode, *, horizon=96, max_gap=2):
    base = {k:row.get(k,'') for k in ('attempt_id','symbol','level_id',
           'pre_closed_ts','breakout_closed_ts','zone_low','zone_high','v26_status')}
    base['episode_id'] = episode
    base['continuation_status'] = statuses.get('BREAKOUT_CONTINUATION','MISSING')
    base['reclaim_status'] = statuses.get('RECLAIM_CONTINUATION','MISSING')
    atr, atr_state = formation_atr(levels,row)
    base['formation_atr_status'] = atr_state
    base['audit_state'] = 'OK'
    base['error'] = ''
    try:
        pre = millis(row['pre_closed_ts'])
        breakout = millis(row['breakout_closed_ts'])
        behavior = price_behavior(slice_candles(candles,pre,horizon),
                pre_closed_ts=pre, breakout_closed_ts=breakout,
                zone_low=row['zone_low'],zone_high=row['zone_high'],
                formation_atr=atr,horizon=horizon)
        s = behavior['summary']
        first = early4(behavior['bars'])
        groups = consolidate_touch_processes(behavior['bars'],max_gap_bars=max_gap)
        base.update(first)
        base.update({'contact_processes':len(groups),
            'from_above_processes':sum(bool(c['from_above_bars']) for c in groups),
            'raw_contacts_from_above':s['touches_from_above'],
            'raw_physical_contacts':s['physical_zone_contacts'],
            'loss_crosses':s['loss_crosses'], 'recovery_crosses':s['recovery_crosses'],
            'defenses_descriptive':s['descriptive_defenses'],
            'observed_post_bars':s['observed_post_bars'],
            'complete_window':s['complete_window'],
            'breakout_close_above_zone_high':s['breakout_close_above_zone_high']})
        if not s['complete_window']:
            base['audit_state'] = 'CENSORED'
        if atr is None:
            base['defenses_descriptive'] = ''
        return base, groups
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        base.update({'audit_state':'ERROR','error':str(exc),
                     'early4_state':'UNAVAILABLE','contact_processes':'',
                     'from_above_processes':'','defenses_descriptive':''})
        return base, []


def write_csv(path, rows, fields):
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def run(root, *, pilot_size=15, seed=20261103, visual_controls='WAXPUSDT,AAVEUSDT,BSVUSDT', full=False,
        horizon=96, max_gap=2, output_dir=None):
    paths, proof = source_check(root, check_hash=True)
    symbols = sorted({r['symbol'] for r in read_rows(paths['pre'])})
    if len(symbols)!=290:
        raise ValueError(f'Expected 290 symbols, got {len(symbols)}')
    selected = symbols if full else select_symbols(symbols,pilot_size,seed,visual_controls)[0]
    chosen=set(selected)
    missing_candles=[s for s in selected if not (root/CANDLES/f'{s}_30m.csv').is_file()]
    if missing_candles:
        raise FileNotFoundError(f'Missing candles: {missing_candles[:8]}')
    pre_rows=defaultdict(list)
    for r in read_rows(root/PRE):
        if r['symbol'] in chosen:
            pre_rows[r['symbol']].append(r)
    v29=defaultdict(dict)
    for r in read_rows(root/V29):
        if r['origin_symbol'] in chosen:
            aid=r['attempt_id']; branch=r['branch']
            if branch in v29[aid]:
                raise ValueError('Duplicate V2.9 branch: '+aid)
            v29[aid][branch]=r['status']
    episodes={}
    for r in read_rows(root/EPISODES):
        if r['symbol'] in chosen:
            if r['attempt_id'] in episodes:
                raise ValueError('Duplicate episode attempt: '+r['attempt_id'])
            episodes[r['attempt_id']] = r['episode_id']
    levels,level_source_state=load_level_atr(root,chosen)
    out=Path(output_dir) if output_dir else root/OUTPUT/('full290_v1' if full else f'pilot{len(selected)}_seed{seed}_v1')
    if out.exists():
        raise FileExistsError(f'Output already exists, refusing overwrite: {out}')
    out.mkdir(parents=True)
    attempt_rows=[]; process_rows=[]
    for i,sym in enumerate(selected,1):
        candles = load_bars(root/CANDLES/f'{sym}_30m.csv')
        for r in pre_rows[sym]:
            aid = r['attempt_id']
            if aid not in episodes or set(v29.get(aid,{})) != {'BREAKOUT_CONTINUATION','RECLAIM_CONTINUATION'}:
                raise ValueError(f'Incomplete identity or frozen V2.9 routes: {aid}')
            result, clusters=summarize_one(r,candles,levels,v29[aid],episodes[aid],
                                            horizon=horizon,max_gap=max_gap)
            attempt_rows.append(result)
            for n,c in enumerate(clusters,1):
                process_rows.append({'attempt_id':aid,'symbol':sym,'episode_id':episodes[aid],
                                     'contact_process_ordinal':n,**c})
        print('HLL_PROCESS_SYMBOL',json.dumps({'i':i,'total':len(selected),'symbol':sym,
            'attempts':len(pre_rows[sym])}),flush=True)
    if not attempt_rows:
        raise ValueError('Empty cohort')
    episode_groups=defaultdict(list)
    for r in attempt_rows:
        episode_groups[(r['symbol'],r['episode_id'])].append(r)
    episode_rows=[]
    for (sym,eid),members in sorted(episode_groups.items()):
        members=sorted(members,key=lambda x:int(millis(x['pre_closed_ts'])))
        candidates=[m for m in members if m.get('early4_intermit')]
        episode_rows.append({'symbol':sym,'episode_id':eid,
            'attempts':len(members),'first_attempt_id':members[0]['attempt_id'],
            'first_pre_closed_ts':members[0]['pre_closed_ts'],
            'has_intermit4_at_any_attempt':bool(candidates),
            'first_intermit4_attempt_id':candidates[0]['attempt_id'] if candidates else '',
            'first_intermit4_known_closed_ts':candidates[0]['known_closed_ts'] if candidates else '',
            'retrospective_episode_rollup':True})
    files = ('attempt_processes.csv','contact_processes.csv','episode_rollup.csv')
    write_csv(out/files[0],attempt_rows,list(dict.fromkeys(k for r in attempt_rows for k in r)))
    process_cols = ['attempt_id','symbol','episode_id','contact_process_ordinal',
            'start_offset','last_offset','first_known_closed_ts','last_known_closed_ts',
            'physical_bars','from_above_bars','loss_crosses','recovery_crosses','span_bars']
    write_csv(out/files[1],process_rows,process_cols)
    episode_cols=['symbol','episode_id','attempts','first_attempt_id','first_pre_closed_ts',
          'has_intermit4_at_any_attempt','first_intermit4_attempt_id',
          'first_intermit4_known_closed_ts','retrospective_episode_rollup']
    write_csv(out/files[2],episode_rows,episode_cols)
    summary={'version':VERSION,'mode':'FULL290' if full else f'PILOT{len(selected)}',
       'symbols':len(selected),'attempts':len(attempt_rows),
       'episodes':len(set(r['episode_id'] for r in attempt_rows)),
       'contact_process_rows':len(process_rows),
       'attempt_status_counts':dict(Counter(r['audit_state'] for r in attempt_rows)),
       'early4_patterns':dict(Counter(r.get('early4_pattern','') for r in attempt_rows)),
       'early4_evaluable_n':sum(r.get('early4_state')=='KNOWN' for r in attempt_rows),
       'early4_intermit_n':sum(bool(r.get('early4_intermit')) for r in attempt_rows),
       'early4_intermit_episodes':sum(bool(r['has_intermit4_at_any_attempt']) for r in episode_rows),
       'early4_intermit_flip_status':dict(Counter(r['v26_status'] for r in attempt_rows if r.get('early4_intermit'))),
       'formation_atr_counts':dict(Counter(r['formation_atr_status'] for r in attempt_rows)),
       'level_source_state':level_source_state,
       'selected_symbols':selected,'grouping_max_empty_bars':max_gap,'post_horizon':horizon,
       'warnings':[
          'Los procesos de contacto son agrupaciones descriptivas, NO retests ni señales.',
          'Intentos de un mismo episodio/nivel/símbolo NO son muestras independientes.',
          'La condición INTERMITTENT_4 es hipotética, no fue validada contra resultados POST.',
          'Los estados frozen son descriptivos; entrada verificada NO se infiere.',
          'No inferir ausencia de defensas si falta ATR de formación o velas.',
       ]}
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    manifest={'version':VERSION,'mode':'OFFLINE_NO_LIVE',
      'source_prehash_verified':True,'pre_sha256':proof['pre_sha256'],
      'v29_sha256':sha256(root/V29),'episodes_sha256':sha256(root/EPISODES),
      'engine_file':'hll_resistance_universal_audit_v1.py',
      'engine_sha256':sha256(Path(__import__('inspect').getfile(price_behavior))),
      'no_live':True,'no_detector_changes':True,'no_pre_rerank':True,
      'files_sha256':{name:sha256(out/name) for name in (*files,'summary.json')}}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    final={'status':'DONE','mode':summary['mode'],'attempts':len(attempt_rows),
           'episodes':summary['episodes'],'interaction_processes':len(process_rows),
           'intermittent4':summary['early4_intermit_n'],
           'errors':summary['attempt_status_counts'].get('ERROR',0),'output':str(out)}
    print('HLL_PROCESS_FINAL',json.dumps(final,ensure_ascii=False),flush=True)
    return final


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    group=ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan',action='store_true')
    group.add_argument('--run',action='store_true')
    ap.add_argument('--root',type=Path,default=Path('.'))
    ap.add_argument('--pilot-size',type=int,default=15)
    ap.add_argument('--seed',type=int,default=20261103)
    ap.add_argument('--visual-controls',default='WAXPUSDT,AAVEUSDT,BSVUSDT')
    ap.add_argument('--full',action='store_true')
    ap.add_argument('--horizon',type=int,default=96)
    ap.add_argument('--merge-gap',type=int,default=2)
    ap.add_argument('--output-dir',type=Path)
    args=ap.parse_args(argv)
    root=args.root.resolve()
    if args.plan:
        paths,proof=source_check(root,check_hash=False)
        print('HLL_PROCESS_PLAN',json.dumps({'version':VERSION,'root':str(root),
           'mode':'OFFLINE_NO_LIVE','requested':'FULL290' if args.full else f'PILOT{args.pilot_size}',
           'sources_present':{k:p.exists() for k,p in paths.items()},
           'levels_present':(root/LEVELS).exists(),
           'uses_existing_universal_engine':True,
           'writes_sources':False,'horizon':args.horizon,
           'group_max_empty_bars':args.merge_gap},ensure_ascii=False))
    else:
        run(root,pilot_size=args.pilot_size,seed=args.seed,
            visual_controls=args.visual_controls,full=args.full,
            horizon=args.horizon,max_gap=args.merge_gap,output_dir=args.output_dir)

if __name__=='__main__':
    main()
