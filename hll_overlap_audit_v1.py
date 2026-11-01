#!/usr/bin/env python3
"""HLL overlap audit V1: attempt vs horizontal-level vs bounded PRE episodes.

READ-ONLY relative to all existing reports. No POST, no order/entry logic and no
merging of different level IDs. The heuristic episodes are not ground-truth
independent formations. Inputs: frozen V2.8.2 cohort + completed Discovery V1.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path

TF = 1_800_000
COHORT = Path('reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv')
DISCOVERY = Path('reports/horizontal_level_structural_discovery_v1/full290_v1')
OUTPUT_BASE = Path('reports/horizontal_level_structural_overlap_audit_v1')
REQUIRED_COHORT = ('attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'breakout_closed_ts',
                   'zone_low', 'zone_high', 'atr_pre', 'pre_96_coverage')
REQUIRED_DISCOVERY = ('attempt_id', 'symbol', 'level_id', 'pre_closed_ts',
                      'zone_low', 'zone_high', 'status', 'family_1p0')
MAP_FIELDS = ('attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'breakout_closed_ts',
              'zone_low', 'zone_high', 'atr_pre', 'status', 'family_1p0',
              'episode_id', 'episode_first_attempt_id', 'episode_anchor_pre_ts',
              'episode_ordinal', 'episode_attempt_count', 'is_first_in_episode',
              'same_level_previous_gap_bars', 'pre96_overlap_with_prev_same_level_bars',
              'other_levels_same_candle', 'candidate_other_level_prior_count',
              'candidate_nearest_other_level_attempt_id', 'candidate_center_distance_atr',
              'candidate_time_distance_bars')
LEVEL_FIELDS = ('symbol', 'level_id', 'attempts', 'first_pre_ts', 'last_pre_ts',
                'span_bars', 'episodes_gap24', 'zone_low', 'zone_high',
                'first_attempt_id', 'last_attempt_id')
EPISODE_FIELDS = ('episode_id', 'symbol', 'level_id', 'first_attempt_id',
                  'first_pre_ts', 'last_pre_ts', 'span_bars', 'attempts',
                  'first_family_1p0', 'distinct_families_1p0', 'family_changes_within_episode')
EDGE_FIELDS = ('attempt_id', 'symbol', 'pre_closed_ts', 'level_id', 'other_attempt_id',
               'other_level_id', 'other_pre_closed_ts', 'delta_bars',
               'center_distance_atr', 'zone_gap_atr', 'interpretation')


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def ident(*items):
    return hashlib.sha256('|'.join(map(str, items)).encode('utf8')).hexdigest()[:24]


def integer(val, field):
    if not isinstance(val, str) or not val.isdecimal():
        raise ValueError(f'{field} inválido: {val!r}')
    return int(val)


def number(val, field, positive=True):
    try:
        n = float(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} inválido: {val!r}') from exc
    if not math.isfinite(n) or (positive and n <= 0):
        raise ValueError(f'{field} inválido: {val!r}')
    return n


def same_price(a,b):
    return math.isclose(a,b,rel_tol=1e-8,abs_tol=0.0)


def read_sources(cohort_path=COHORT, discovery_dir=DISCOVERY, *, enforce_full=True):
    """Explicit allowlist of PRE fields; POST fields are never read or retained."""
    cm_path = cohort_path.with_name('manifest.json')
    dm_path = discovery_dir/'manifest.json'
    dp = discovery_dir/'attempts_pre.csv'
    paths = (cohort_path,cm_path,dm_path,dp)
    missing=[str(p) for p in paths if not p.is_file()]
    if missing:raise FileNotFoundError('Faltan fuentes: '+', '.join(missing))
    cm=json.loads(cm_path.read_text(encoding='utf-8'))
    dm=json.loads(dm_path.read_text(encoding='utf-8'))
    if cm.get('version')!='HLL_V282_PRE_COHORT' or cm.get('mode')!='OFFLINE_READ_ONLY':
        raise ValueError('Manifiesto V2.8.2 inválido')
    if dm.get('version')!='HLL_STRUCTURAL_DISCOVERY_V1' or dm.get('reads_post') is not False:
        raise ValueError('Manifiesto Discovery V1 incompatible')
    if dm.get('scope')!='CROSS_UP_CONDITIONED_PRE_ONLY' or dm.get('all_attempts_preserved') is not True:
        raise ValueError('Discovery no conserva íntegra la cohorte')
    if digest(dp)!=dm.get('output_sha256',{}).get('attempts_pre.csv'):
        raise ValueError('Hash de Discovery attempts_pre.csv inválido')
    if digest(cohort_path)!=dm.get('source_sha256',{}).get('cohort'):
        raise ValueError('Discovery y cohorte provienen de distintas fuentes')
    if enforce_full and (cm.get('original_attempts')!=55717 or cm.get('symbols')!=290
                         or dm.get('attempts')!=55717 or dm.get('symbols')!=290):
        raise ValueError('Se esperaba la cohorte full 290/55.717')
    cohort={}
    with cohort_path.open('r',encoding='utf-8-sig',newline='') as f:
        rd=csv.DictReader(f)
        if set(REQUIRED_COHORT)-set(rd.fieldnames or []):
            raise ValueError('Faltan campos PRE en cohorte')
        for raw in rd:
            r={k:raw[k] for k in REQUIRED_COHORT}
            aid=r['attempt_id'];sym=r['symbol'];lid=r['level_id']
            if not aid or not sym or not lid or aid in cohort or '/' in sym or '..' in sym:
                raise ValueError('Attempt ID/símbolo/level ID inválido o duplicado')
            r['pre_closed_ts']=integer(r['pre_closed_ts'],'pre_closed_ts')
            r['breakout_closed_ts']=integer(r['breakout_closed_ts'],'breakout_closed_ts')
            if (r['pre_closed_ts']%TF or r['breakout_closed_ts']-r['pre_closed_ts']!=TF):
                raise ValueError(f'Corte causal PRE inválido: {aid}')
            for col in ('zone_low','zone_high','atr_pre'):
                r[col]=number(r[col],col)
            if r['zone_low']>r['zone_high']:
                raise ValueError(f'Zona invertida: {aid}')
            if r['pre_96_coverage'] not in ('True','False'):
                raise ValueError('Cobertura PRE ambigua')
            r['covered']=r['pre_96_coverage']=='True'
            cohort[aid]=r
    if len(cohort)!=int(cm['original_attempts']):
        raise ValueError('Conteo de intentos en cohorte incorrecto')
    rows=[];seen=set()
    with dp.open('r',encoding='utf-8-sig',newline='') as f:
        rd=csv.DictReader(f)
        if set(REQUIRED_DISCOVERY)-set(rd.fieldnames or []):
            raise ValueError('Faltan campos necesarios en Discovery')
        for raw in rd:
            aid=raw['attempt_id']
            if aid in seen or aid not in cohort:raise ValueError(f'ID duplicado/desconocido: {aid}')
            r=cohort[aid].copy()
            if (raw['symbol']!=r['symbol'] or raw['level_id']!=r['level_id']
                    or integer(raw['pre_closed_ts'],'Discovery.pre')!=r['pre_closed_ts']):
                raise ValueError(f'Joining de episodio incorrecto: {aid}')
            if (not same_price(number(raw['zone_low'],'Discovery.zone_low'),r['zone_low'])
                    or not same_price(number(raw['zone_high'],'Discovery.zone_high'),r['zone_high'])):
                raise ValueError(f'Zona inconsistente entre fuentes: {aid}')
            status=raw['status']
            if status not in ('COMPLETE_PRE','INCOMPLETE_PRE_96') or (status=='COMPLETE_PRE')!=r['covered']:
                raise ValueError(f'Cobertura discordante: {aid}')
            r['status']=status;r['family_1p0']=raw['family_1p0'] or 'SIN_DATOS'
            seen.add(aid);rows.append(r)
    if seen!=set(cohort) or len(rows)!=int(dm['attempts']):
        raise ValueError('Discovery no contiene exactamente los intentos de cohorte')
    if len({r['symbol'] for r in rows})!=int(dm['symbols']):
        raise ValueError('Conteo de símbolos incompatible')
    rows.sort(key=lambda r:(r['symbol'],r['pre_closed_ts'],r['level_id'],r['attempt_id']))
    return rows,{'cohort':digest(cohort_path),'discovery':digest(dp)},cm,dm


def zone_gap(a,b):
    return max(0.,a['zone_low']-b['zone_high'],b['zone_low']-a['zone_high'])


def near_other_level(a,b,*,max_time_bars=48, max_center_atr=0.5, max_gap_atr=0.25):
    if a['symbol']!=b['symbol'] or a['level_id']==b['level_id']:
        return None
    dt=a['pre_closed_ts']-b['pre_closed_ts']
    if dt<0 or dt>max_time_bars*TF:return None
    atr=(a['atr_pre']+b['atr_pre'])/2
    center=abs((a['zone_low']+a['zone_high'])/2-(b['zone_low']+b['zone_high'])/2)/atr
    gap=zone_gap(a,b)/atr
    if center<=max_center_atr and gap<=max_gap_atr:
        return (center,gap,dt//TF)
    return None


def episodes(rows, *, gap_bars=24, cap_bars=96, collect_edges=True):
    """Causal assignment by exact level_id with bounded first-to-last episode span.

    A PRE window overlap is a heuristic, NOT an independent pattern proof.
    Same-zone different-level pairs are only reported, never auto-merged.
    """
    if gap_bars<1 or cap_bars<gap_bars:raise ValueError('Parámetros de episodio inválidos')
    recs=[];episodes_by_id={};by_level={};last={};active=deque();edges=[]
    same_candle=Counter((r['symbol'],r['pre_closed_ts']) for r in rows)
    for r in rows:
        key=(r['symbol'],r['level_id']);pre=r['pre_closed_ts']
        old=last.get(key)
        delta=None if old is None else (pre-old['pre_closed_ts'])//TF
        current=episodes_by_id[old['episode_id']] if old is not None else None
        join=(current is not None and delta<=gap_bars
              and pre-current['first_pre_ts']<cap_bars*TF)
        if join:
            e=current
        else:
            eid=ident('HLL_OVERLAP_V1',*key,pre,r['attempt_id'],gap_bars,cap_bars)
            e={'episode_id':eid,'symbol':r['symbol'],'level_id':r['level_id'],
               'first_pre_ts':pre,'last_pre_ts':pre,'first_attempt_id':r['attempt_id'],
               'attempts':0,'families':set(),'first_family_1p0':r['family_1p0']}
            episodes_by_id[eid]=e
        e['attempts']+=1;e['last_pre_ts']=pre;e['families'].add(r['family_1p0'])
        if key not in by_level:
            by_level[key]={'symbol':r['symbol'],'level_id':r['level_id'],
                           'first_pre_ts':pre,'first_attempt_id':r['attempt_id'],
                           'zone_low':r['zone_low'],'zone_high':r['zone_high'],
                           'attempts':0,'episodes_gap24':0}
        lv=by_level[key]
        if not same_price(r['zone_low'],lv['zone_low']) or not same_price(r['zone_high'],lv['zone_high']):
            raise ValueError(f'Un mismo level_id tiene distintas zonas: {key}')
        lv['attempts']+=1;lv['last_pre_ts']=pre;lv['last_attempt_id']=r['attempt_id']
        if not join:lv['episodes_gap24']+=1
        # compare ONLY earlier events (same-time sibling IDs are not used as a prior):
        while active and (active[0]['symbol']!=r['symbol'] or
                          pre-active[0]['pre_closed_ts']>48*TF):
            active.popleft()
        nearby=[]
        if collect_edges:
            for before in active:
                if before['pre_closed_ts']==pre:continue
                metric=near_other_level(r,before)
                if metric is not None:nearby.append((metric,before))
        closest=min(nearby,key=lambda x:(x[0][0],x[0][2],x[1]['attempt_id'])) if nearby else None
        row={'attempt_id':r['attempt_id'],'symbol':r['symbol'],'level_id':r['level_id'],
             'pre_closed_ts':pre,'breakout_closed_ts':r['breakout_closed_ts'],
             'zone_low':r['zone_low'],'zone_high':r['zone_high'],'atr_pre':r['atr_pre'],
             'status':r['status'],'family_1p0':r['family_1p0'],
             'episode_id':e['episode_id'],'episode_first_attempt_id':e['first_attempt_id'],
             'episode_anchor_pre_ts':e['first_pre_ts'],'episode_ordinal':e['attempts'],
             'is_first_in_episode':int(not join),
             'same_level_previous_gap_bars':'' if delta is None else delta,
             'pre96_overlap_with_prev_same_level_bars':0 if delta is None else max(0,96-delta),
             'other_levels_same_candle':same_candle[(r['symbol'],pre)]-1,
             'candidate_other_level_prior_count':len(nearby),
             'candidate_nearest_other_level_attempt_id':closest[1]['attempt_id'] if closest else '',
             'candidate_center_distance_atr':round(closest[0][0],6) if closest else '',
             'candidate_time_distance_bars':closest[0][2] if closest else ''}
        recs.append(row)
        if closest:
            (cd,zg,db),o=closest
            edges.append({'attempt_id':r['attempt_id'],'symbol':r['symbol'],
                          'pre_closed_ts':pre,'level_id':r['level_id'],
                          'other_attempt_id':o['attempt_id'],'other_level_id':o['level_id'],
                          'other_pre_closed_ts':o['pre_closed_ts'],'delta_bars':db,
                          'center_distance_atr':round(cd,6),'zone_gap_atr':round(zg,6),
                          'interpretation':'POSIBLE_ZONA_EQUIVALENTE_NO_FUSIONADA'})
        last[key]={'pre_closed_ts':pre,'episode_id':e['episode_id']}
        active.append(r)
    for row in recs:row['episode_attempt_count']=episodes_by_id[row['episode_id']]['attempts']
    lvrows=[]
    for lv in by_level.values():
        lvrows.append({**lv,'span_bars':(lv['last_pre_ts']-lv['first_pre_ts'])//TF})
    erows=[]
    for e in episodes_by_id.values():
        erows.append({k:v for k,v in e.items() if k!='families'}|{
            'span_bars':(e['last_pre_ts']-e['first_pre_ts'])//TF,
            'distinct_families_1p0':len(e['families']),
            'family_changes_within_episode':int(len(e['families'])>1)})
    lvrows.sort(key=lambda x:(x['symbol'],x['first_pre_ts'],x['level_id']))
    erows.sort(key=lambda x:(x['symbol'],x['first_pre_ts'],x['level_id']))
    return recs,lvrows,erows,edges


def summarize(rows,recs,levels,eps,edges,scenario_counts):
    c=Counter(r['symbol'] for r in recs)
    n=len(recs)
    first=sum(int(r['is_first_in_episode']) for r in recs)
    reused=sum(x['attempts']>1 for x in levels)
    repeated_level_attempts=sum(x['attempts']-1 for x in levels)
    multiple_ep_attempts=sum(r['episode_attempt_count']>1 for r in recs)
    same_candle=sum(r['other_levels_same_candle']>0 for r in recs)
    summary={'version':'HLL_OVERLAP_AUDIT_V1','attempts':n,'symbols':len(c),
       'distinct_level_ids_with_attempts':len(levels),
       'level_ids_with_multiple_attempts':reused,
       'attempts_after_first_on_same_level':repeated_level_attempts,
       'heuristic_episodes_gap24_cap96':len(eps),
       'attempts_after_first_in_heuristic_episode':n-first,
       'attempts_in_multi_attempt_episodes':multiple_ep_attempts,
       'episodes_with_multiple_families_1atr':sum(e['family_changes_within_episode'] for e in eps),
       'attempts_with_other_level_same_cutoff':same_candle,
       'attempts_with_prior_similar_zone_other_level':len(edges),
       'scenario_counts':scenario_counts,
       'no_post_used':True,'different_level_ids_merged':False,
       'selection_conditioned_on_cross_up':True,
       'warning':'Los episodios son agrupaciones heurísticas de intentos; NO son estructuras independientes comprobadas.'}
    if sum(x['attempts'] for x in levels)!=n or sum(x['attempts'] for x in eps)!=n:
        raise ValueError('El conteo por niveles/episodios no conserva todos los intentos')
    if len({r['attempt_id'] for r in recs})!=n:raise ValueError('Intentos duplicados en salida')
    return summary


def write_csv(path,rows,fields):
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        w.writeheader()
        for row in rows:w.writerow({k:row.get(k,'') for k in fields})


def run(rows,hashes,out,*,pilot=False):
    if out.exists():raise FileExistsError(f'Salida ya existe; no se sobrescribe: {out}')
    parent=out.parent
    parent.mkdir(parents=True,exist_ok=True)
    if out.resolve()==COHORT.resolve() or out.resolve()==DISCOVERY.resolve():
        raise ValueError('Output invade fuentes')
    with tempfile.TemporaryDirectory(prefix='.hll_overlap_partial_',dir=parent) as td:
        temp=Path(td)
        attempts,levels,eps,edges=episodes(rows)
        sensitivity={}
        for gap in (12,24,48):
            # Optimization: no need for near-level comparisons at alternate gaps.
            _,_,alternative,_=episodes(rows,gap_bars=gap,collect_edges=False)
            sensitivity[f'gap{gap}_cap96']=len(alternative)
        m=summarize(rows,attempts,levels,eps,edges,sensitivity)
        m['source_sha256']=hashes
        m['rule']={'same_level_exact':True,'max_gap_bars_default':24,
                   'max_span_from_first_pre_bars':96,
                   'different_level_time_neighborhood_bars':48,
                   'different_level_center_distance_atr':0.5,
                   'different_level_zone_gap_atr':0.25,
                   'cross_level_nearest_only':'prior attempt, same symbol, no auto merge'}
        m['mode']='READ_ONLY_ANALYSIS'
        write_csv(temp/'attempt_episode_map.csv',attempts,MAP_FIELDS)
        write_csv(temp/'level_reuse.csv',levels,LEVEL_FIELDS)
        write_csv(temp/'episode_candidates.csv',eps,EPISODE_FIELDS)
        write_csv(temp/'nearby_other_levels_review.csv',edges,EDGE_FIELDS)
        with (temp/'symbol_summary.csv').open('w',encoding='utf8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['symbol','attempts','distinct_levels','episodes','repeated_attempts_same_level','possible_other_level_zone'])
            w.writeheader()
            cnt=Counter(r['symbol'] for r in rows)
            lev=Counter(x['symbol'] for x in levels)
            epc=Counter(x['symbol'] for x in eps)
            edc=Counter(x['symbol'] for x in edges)
            for s in sorted(cnt):w.writerow({'symbol':s,'attempts':cnt[s],
                'distinct_levels':lev[s],'episodes':epc[s],
                'repeated_attempts_same_level':cnt[s]-lev[s],
                'possible_other_level_zone':edc[s]})
        m['outputs_sha256']={p.name:digest(p) for p in temp.glob('*.csv')}
        (temp/'manifest.json').write_text(json.dumps(m,indent=2,ensure_ascii=False),encoding='utf8')
        os.replace(temp,out)
    return m


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    parser.add_argument('--max-symbols',type=int,default=0,help='Piloto con primeros N símbolos alfabéticos')
    parser.add_argument('--cohort',type=Path,default=COHORT)
    parser.add_argument('--discovery',type=Path,default=DISCOVERY)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    if args.max_symbols<0 or args.max_symbols>290:parser.error('--max-symbols debe estar entre 0 y 290')
    rows,hashes,cm,dm=read_sources(args.cohort,args.discovery,enforce_full=args.max_symbols==0)
    all_syms=sorted({r['symbol'] for r in rows})
    if args.max_symbols:
        selected=set(all_syms[:args.max_symbols]);rows=[r for r in rows if r['symbol'] in selected]
    scope=f'pilot_first_{args.max_symbols}_v1' if args.max_symbols else 'full290_v1'
    out=args.output or OUTPUT_BASE/scope
    p={'attempts':len(rows),'symbols':len({r['symbol'] for r in rows}),
       'source_hashes':hashes,'output':str(out),'sources_present':True,
       'no_overwrite':True,'reads_post':False,'modifies_live':False,
       'different_level_ids_merged':False,'selection_conditioned_on_cross_up':True}
    print('HLL_OVERLAP_PLAN '+json.dumps(p,ensure_ascii=False),flush=True)
    if args.plan:return 0
    m=run(rows,hashes,out)
    keys=('attempts','symbols','distinct_level_ids_with_attempts','level_ids_with_multiple_attempts',
          'attempts_after_first_on_same_level','heuristic_episodes_gap24_cap96',
          'attempts_after_first_in_heuristic_episode','attempts_in_multi_attempt_episodes',
          'attempts_with_other_level_same_cutoff','attempts_with_prior_similar_zone_other_level',
          'episodes_with_multiple_families_1atr','scenario_counts')
    print('HLL_OVERLAP_DONE '+json.dumps({k:m[k] for k in keys},ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
