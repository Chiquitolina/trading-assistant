#!/usr/bin/env python3
"""HLL Discovery PRE, universo congelado de intentos CROSS_UP 290 (offline).

Conserva TODOS los intentos; no incorpora POST, resultados, etiquetas visuales,
órdenes ni parámetros de optimización. Reutiliza las rutinas CORE15 ya probadas
para describir fases, familias y continuidad en 96 velas cerradas PRE.

ADVERTENCIA: el universo está CONDICIONADO a CROSS_UP futuros. Este estudio
NO estima P(ruptura | estructura) y NO es todavía un escáner prospectivo.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path

import hll_phase_reconstruction_pre as phase
import hll_pre_family_interpreter as family
import hll_pre_phase_continuity_audit_v1 as continuity
import hll_resistance_interaction_pre as resistance
import hll_structural_approach_ohlc_pre as ohlc

TF = 1_800_000
SCALES = (0.5, 1.0, 1.5)
COHORT = Path('reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv')
COHORT_MANIFEST = COHORT.with_name('manifest.json')
CANDLES = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')
OUTPUT_PARENT = Path('reports/horizontal_level_structural_discovery_v1')
REQUIRED = ('attempt_id','symbol','level_id','breakout_closed_ts','pre_closed_ts',
            'zone_low','zone_high','atr_pre','pre_pattern_24','pre_pattern_48',
            'pre_pattern_96','pre_96_coverage')

FIELDS_SCALE = ('attempt_id','symbol','level_id','pre_closed_ts','scale_atr','status',
    'family_candidate','family_basis','terminal_phase',
    'confirmed_low_progression','confirmed_high_progression',
    'low_steps_up','low_steps_down','high_steps_up','high_steps_down',
    'recent_recovery_progress','intercontact_trough_direction_48',
    'intercontact_trough_delta_48_atr','zone_touch_episodes_48',
    'zone_crossings_48','zone_touch_share_48','net_return_48_pct',
    'latest_recovery_retention_frac','preserved_fraction_last3',
    'net_trough_progress_last3_atr','completed_up_down_cycles',
    'complete_givebacks_last3','left_censored_cycles',
    'supporting_observations','contradicting_observations','limitations','data_limitations')

FIELDS_ATTEMPT = ('attempt_id','symbol','level_id','pre_closed_ts','breakout_closed_ts',
    'zone_low','zone_high','atr_pre','original_pre_pattern_48','original_pre_pattern_96',
    'status','reason','pre_bars_found','family_0p5','family_1p0','family_1p5',
    'scale_agreement','intercontact_48','net_return_48_pct',
    'latest_recovery_retention_1p0','preserved_fraction_last3_1p0',
    'net_trough_progress_last3_atr_1p0','completed_cycles_1p0',
    'low_progression_1p0','high_progression_1p0',
    'evidence_0p5','contradictions_0p5','limitations_0p5',
    'evidence_1p0','contradictions_1p0','limitations_1p0',
    'evidence_1p5','contradictions_1p5','limitations_1p5')


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def integer(value, field):
    try:
        n = int(value)
    except (ValueError,TypeError) as e:
        raise ValueError(f'{field} no entero: {value!r}') from e
    if str(n) != str(value).strip():
        raise ValueError(f'{field} escrito en formato inesperado: {value!r}')
    return n


def read_cohort(path=COHORT, manifest_path=COHORT_MANIFEST, *, enforce_290=True):
    if not path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f'Falta cohorte V2.8.2: {path} / {manifest_path}')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('version') != 'HLL_V282_PRE_COHORT' or manifest.get('mode') != 'OFFLINE_READ_ONLY':
        raise ValueError('Manifiesto V2.8.2 inesperado')
    if enforce_290 and (manifest.get('symbols')!=290 or manifest.get('original_attempts')!=55717):
        raise ValueError('Se esperaba la cohorte congelada 290 / 55717')
    rows=[];ids=set();syms=set()
    with path.open('r',encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if missing:=set(REQUIRED)-set(reader.fieldnames or ()):
            raise ValueError(f'Faltan columnas PRE: {sorted(missing)}')
        for src in reader:
            r={k:src[k] for k in REQUIRED}  # jamás materializar campos POST
            aid=r['attempt_id'];sym=r['symbol'];level=r['level_id']
            if not aid or not sym or not level or aid in ids or '/' in sym or '\\' in sym or '..' in sym:
                raise ValueError(f'ID/símbolo/nivel inválido o duplicado: {aid} {sym}')
            pre=integer(r['pre_closed_ts'],'pre_closed_ts')
            cross=integer(r['breakout_closed_ts'],'breakout_closed_ts')
            if pre%TF or cross-pre!=TF:
                raise ValueError(f'Corte PRE/CROSS_UP incorrecto: {aid}')
            lo,hi,atr=(float(r[k]) for k in ('zone_low','zone_high','atr_pre'))
            if not all(math.isfinite(n) for n in (lo,hi,atr)) or not (0<lo<=hi and atr>0):
                raise ValueError(f'Resistencia o ATR inválido: {aid}')
            if r['pre_96_coverage'] not in ('True','False'):
                raise ValueError(f'Cobertura PRE ambigua: {aid}')
            r.update(pre_closed_ts=pre,breakout_closed_ts=cross,zone_low=lo,zone_high=hi,atr_pre=atr,
                     pre_96_coverage=r['pre_96_coverage']=='True')
            rows.append(r);ids.add(aid);syms.add(sym)
    if len(rows)!=int(manifest['original_attempts']) or len(syms)!=int(manifest['symbols']):
        raise ValueError('Cantidad de intentos/símbolos no coincide con manifiesto')
    rows.sort(key=lambda r:(r['symbol'],r['pre_closed_ts'],r['attempt_id']))
    return rows,manifest


def planned(rows, candle_dir=CANDLES, *, max_symbols=0):
    all_symbols=sorted({r['symbol'] for r in rows})
    selected=all_symbols[:max_symbols] if max_symbols else all_symbols
    subset=[r for r in rows if r['symbol'] in set(selected)]
    missing=[s for s in selected if not (candle_dir/f'{s}_30m.csv').is_file()]
    return dict(attempts=len(subset),symbols=len(selected),missing_candle_symbols=missing[:30],
                missing_candle_symbols_count=len(missing),
                pre96_covered_in_frozen_cohort=sum(r['pre_96_coverage'] for r in subset),
                pre96_uncovered_in_frozen_cohort=sum(not r['pre_96_coverage'] for r in subset),
                sample_is_conditioned_on_known_cross_up=True,reads_post=False,
                reads_visual_labels=False,modifies_live=False)


def bar_window(candles,pre,n):
    expected=range(pre-n*TF,pre,TF)
    result=[candles[t] for t in expected if t in candles]
    return result if len(result)==n else None


def scale_for_attempt(bars,m,scale,interaction,group_size=1):
    """Reuse the exact CORE15 snapshot logic, only at the frozen PRE cutoff."""
    summ,piv,legs,rec=phase.describe_attempt(bars,m,scale,group_size=group_size)
    cont,cycles=continuity.analyze_one(summ,legs,rec)
    if not rec:
        return {**{k:m[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts')},
                'scale_atr':scale,'status':'SIN_MINIMO_CONFIRMADO',
                'family_candidate':'INDETERMINADA_SIN_MINIMO',
                'limitations':'Sin mínimo confirmado a esta sensibilidad',
                'data_limitations':cont['data_limitations']},cycles
    f=family.interpret(summ,piv,legs,rec,interaction,
                       {**ohlc.compute_metrics(bars[-48:])})
    return {**f,'status':'COMPLETE_PRE',
            'latest_recovery_retention_frac':cont['latest_recovery_retention_frac'],
            'preserved_fraction_last3':cont['preserved_fraction_last3'],
            'net_trough_progress_last3_atr':cont['net_trough_progress_last3_atr'],
            'completed_up_down_cycles':cont['completed_up_down_cycles'],
            'complete_givebacks_last3':cont['complete_givebacks_last3'],
            'left_censored_cycles':cont['left_censored_cycles'],
            'data_limitations':cont['data_limitations']},cycles


def attempt_result(r,candles,group_size=1):
    """Always produce one row for every source attempt, including no-coverage cases."""
    pre=r['pre_closed_ts']
    bars=bar_window(candles,pre,96) if candles is not None else None
    count=sum(t in candles for t in range(pre-96*TF,pre,TF)) if candles is not None else 0
    primary={k:r[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts','breakout_closed_ts',
                              'zone_low','zone_high','atr_pre')}
    primary.update(original_pre_pattern_48=r['pre_pattern_48'],
                   original_pre_pattern_96=r['pre_pattern_96'],pre_bars_found=count)
    if (bars is not None)!=r['pre_96_coverage']:
        raise ValueError(f'PRE 96 coverage contradicts frozen V2.8.2: {r["attempt_id"]}')
    if bars is None:
        primary.update(status='INCOMPLETE_PRE_96',reason='Sin 96 velas 30m contiguas antes del corte',
                       scale_agreement='SIN_DATOS')
        return primary,[]
    meta={k:r[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high','atr_pre')}
    interaction,_=resistance.describe_window(bars[-48:],meta,48)
    result=[]
    for scale in SCALES:
        row,_=scale_for_attempt(bars,meta,scale,interaction,group_size)
        result.append(row)
    primary.update(status='COMPLETE_PRE',reason='',
                   intercontact_48=interaction['intercontact_trough_last_minus_first_atr'],
                   net_return_48_pct=result[1].get('net_return_48_pct',''))
    tags=('family_0p5','family_1p0','family_1p5')
    for k,row in zip(tags,result):primary[k]=row['family_candidate']
    primary['scale_agreement']='IGUAL_3_ATR' if len({primary[k] for k in tags})==1 else 'DISCREPA_ATR'
    for suffix,row in zip(('0p5','1p0','1p5'),result):
        primary[f'evidence_{suffix}']=row.get('supporting_observations')
        primary[f'contradictions_{suffix}']=row.get('contradicting_observations')
        primary[f'limitations_{suffix}']=row.get('limitations')
    at1=result[1]
    primary.update(latest_recovery_retention_1p0=at1.get('latest_recovery_retention_frac'),
                   preserved_fraction_last3_1p0=at1.get('preserved_fraction_last3'),
                   net_trough_progress_last3_atr_1p0=at1.get('net_trough_progress_last3_atr'),
                   completed_cycles_1p0=at1.get('completed_up_down_cycles'),
                   low_progression_1p0=at1.get('confirmed_low_progression'),
                   high_progression_1p0=at1.get('confirmed_high_progression'),
                   evidence_1p0=at1.get('supporting_observations'),
                   contradictions_1p0=at1.get('contradicting_observations'),
                   limitations_1p0=at1.get('limitations'))
    return primary,result


def write_csv_row(writer,row,fields):
    writer.writerow({k:('' if row.get(k) is None else row[k]) for k in fields})


def run(rows,cohort_path,manifest,candle_dir,output,max_symbols=0):
    if output.exists():
        raise FileExistsError(f'Output ya existe (no se sobrescribe): {output}')
    if output.resolve()==cohort_path.resolve() or candle_dir.resolve() in output.resolve().parents:
        raise ValueError('Output dentro de fuentes protegidas')
    symbols=sorted({r['symbol'] for r in rows})
    if max_symbols:symbols=symbols[:max_symbols]
    selected=set(symbols)
    entries=[r for r in rows if r['symbol'] in selected]
    counts=Counter((r['symbol'],r['level_id']) for r in entries)
    status=Counter();families=Counter();agreements=Counter();written=0;scale_count=0
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.hll_discovery_partial_',dir=output.parent) as d:
        temp=Path(d)
        with (temp/'attempts_pre.csv').open('w',newline='',encoding='utf-8') as f1,\
             (temp/'scale_features_pre.csv').open('w',newline='',encoding='utf-8') as f2:
            a=csv.DictWriter(f1,fieldnames=FIELDS_ATTEMPT);a.writeheader()
            b=csv.DictWriter(f2,fieldnames=FIELDS_SCALE);b.writeheader()
            pos=0
            for i,sym in enumerate(symbols,1):
                fp=candle_dir/f'{sym}_30m.csv'
                if fp.is_file():
                    candles,_=ohlc.load_candles(fp,timestamp_kind='open')
                else:
                    candles=None
                while pos<len(entries) and entries[pos]['symbol']==sym:
                    r=entries[pos]
                    one,scales=attempt_result(r,candles,counts[(sym,r['level_id'])])
                    write_csv_row(a,one,FIELDS_ATTEMPT);written+=1
                    status[one['status']]+=1
                    agreements[one.get('scale_agreement','SIN_DATOS')]+=1
                    for sc in scales:
                        write_csv_row(b,sc,FIELDS_SCALE)
                        families[(str(sc['scale_atr']),sc['family_candidate'])]+=1
                        scale_count+=1
                    pos+=1
                if i==1 or i%10==0 or i==len(symbols):
                    print('HLL_DISCOVERY_PROGRESS '+json.dumps({'symbols_done':i,'symbols_total':len(symbols),
                        'attempts_done':written,'scale_rows':scale_count},ensure_ascii=False),flush=True)
        if pos!=len(entries) or written!=len(entries):raise ValueError('Perdimos intentos al agrupar símbolos')
        m=dict(version='HLL_STRUCTURAL_DISCOVERY_V1',scope='CROSS_UP_CONDITIONED_PRE_ONLY',
               sources={'cohort':str(cohort_path),'candles_dir':str(candle_dir)},
               source_sha256={'cohort':sha(cohort_path)},
               attempts=written,symbols=len(symbols),scale_rows=scale_count,
               statuses=dict(status),family_counts_by_scale={f'{k[0]}|{k[1]}':v for k,v in families.items()},
               agreement=dict(agreements),scales_atr=SCALES,lookback_closed_bars=96,
               chart_context_pre_bars=288,chart_context_post_bars_max=96,
               all_attempts_preserved=True,reads_post=False,reads_visual_labels=False,
               modifies_live=False,overwrites_frozen_sources=False,
               no_profit_or_quality_label=True,
               warning='Cohorte condicionada a futuros CROSS_UP: NO es scanner prospectivo; no estima frecuencia de oportunidades ni ventaja predictiva.')
        m['output_sha256']={p.name:sha(p) for p in (temp/'attempts_pre.csv',temp/'scale_features_pre.csv')}
        (temp/'manifest.json').write_text(json.dumps(m,indent=2,ensure_ascii=False),encoding='utf-8')
        os.replace(temp,output)
    return m


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--max-symbols',type=int,default=0,help='Piloto determinista con primeros N símbolos')
    p.add_argument('--cohort',type=Path,default=COHORT)
    p.add_argument('--cohort-manifest',type=Path,default=COHORT_MANIFEST)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output',type=Path,default=None)
    args=p.parse_args(argv)
    if args.max_symbols<0 or args.max_symbols>290:p.error('--max-symbols fuera de rango')
    rows,m=read_cohort(args.cohort,args.cohort_manifest)
    scope=f'pilot_first_{args.max_symbols}_v1' if args.max_symbols else 'full290_v1'
    out=args.output or OUTPUT_PARENT/scope
    plan=planned(rows,args.candles_dir,max_symbols=args.max_symbols)
    plan.update(output=str(out),source_manifest=str(args.cohort_manifest),
                rule='PRE 96 CLOSED 30m; 3 ATR; no future candles',
                no_overwrite=True)
    print('HLL_DISCOVERY_PLAN '+json.dumps(plan,ensure_ascii=False,sort_keys=True),flush=True)
    if args.plan:return 0
    result=run(rows,args.cohort,m,args.candles_dir,out,args.max_symbols)
    print('HLL_DISCOVERY_DONE '+json.dumps({k:result[k] for k in
          ('attempts','symbols','scale_rows','statuses','agreement','family_counts_by_scale')},
          ensure_ascii=False,sort_keys=True),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
