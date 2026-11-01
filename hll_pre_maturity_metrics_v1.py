#!/usr/bin/env python3
"""HLL PRE maturity descriptors, offline, read-only, CROSS_UP conditioned cohort.

No quality label, no trade outcome, no model training, no post, no change to LIVE.
Each row is an individual PRE cutoff. Episode-first summary uses ONLY its first
CROSS_UP; full-episode attempt counts are NOT allowed as causal predictors.

Definitions are fixed for descriptive comparison, not fitted to visual labels.
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

from hll_structural_discovery_v1 import CANDLES, OUTPUT_PARENT as DISCOVERY_PARENT, TF, bar_window
import hll_structural_approach_ohlc_pre as ohlc

DISCOVERY = DISCOVERY_PARENT / 'full290_v1'
OVERLAP = Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1')
OUTPUT = Path('reports/horizontal_level_pre_maturity_metrics_v1')
SOURCE_COLUMNS = ('attempt_id','symbol','level_id','pre_closed_ts','zone_low',
                  'zone_high','atr_pre','status','family_1p0','scale_agreement')
MAP_COLUMNS = ('attempt_id','symbol','level_id','pre_closed_ts','episode_id',
               'episode_ordinal','episode_first_attempt_id','is_first_in_episode')
METRICS = (
    'amplitude_24_early_atr','amplitude_24_middle1_atr','amplitude_24_middle2_atr',
    'amplitude_24_recent_atr','compression_24_recent_vs_early',
    'amplitude_48_first_atr','amplitude_48_recent_atr','compression_48_recent_vs_first',
    'trough_low_q10_step1_atr','trough_low_q10_step2_atr',
    'trough_progress_steps_last3','trough_net_progress_last3_atr',
    'near_zone_share_24_half_atr','near_zone_share_48_half_atr',
    'near_zone_share_96_half_atr','near_zone_share_48_one_atr',
    'above_zone_share_48','near_zone_trailing_bars_96',
    'max_low_below_zone_48_atr','median_close_below_zone_48_atr',
    'pre_close_distance_below_zone_atr','near_zone_share_288_half_atr',
    'amplitude_72_first_288_atr','amplitude_72_recent_288_atr',
    'compression_72_recent_vs_first_288',
)
FIELDS = (*SOURCE_COLUMNS,'episode_id','episode_ordinal','is_first_in_episode',
          'pre_96_bars_found','pre_288_bars_found','status_96','status_288',*METRICS)
EP_FIELDS = ('first_attempt_id',*FIELDS)


def sha(path: Path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def quantile(values, q):
    if not values:
        raise ValueError('Cuantil sin valores')
    arr=sorted(values)
    idx=(len(arr)-1)*q
    i=int(idx)
    return arr[i]*(1-(idx-i)) + arr[min(i+1,len(arr)-1)]*(idx-i)


def robust_amplitude(bars, atr):
    """P90(high)-P10(low), in ATR units: limits single-wick dominance."""
    if not bars or not math.isfinite(atr) or atr<=0:
        raise ValueError('Velas/ATR inválidos')
    return (quantile([r['high'] for r in bars],.9)-quantile([r['low'] for r in bars],.1))/atr


def safe_ratio(num,den):
    return num/den if den>1e-9 else None


def metric_snapshot(bars96, bars288, zone_low, zone_high, atr):
    """Read closed bars strictly BEFORE the PRE cutoff. No future or outcome input."""
    if len(bars96)!=96:
        raise ValueError('Se necesitan 96 velas contiguas')
    if not all(math.isfinite(x) and x>0 for x in (zone_low,zone_high,atr)) or zone_high<zone_low:
        raise ValueError('Zona/ATR no válidos')
    def amp(b):return robust_amplitude(b,atr)
    blocks=[bars96[i:i+24] for i in (0,24,48,72)]
    am=[amp(b) for b in blocks]
    ql=[quantile([x['low'] for x in b],.1) for b in blocks[1:]]
    lo_steps=[(ql[i+1]-ql[i])/atr for i in (0,1)]
    def near_share(recent, tolerance):
        return sum(zone_low-tolerance*atr <= b['close'] <= zone_high for b in recent)/len(recent)
    def close_below(recent):
        distances=[max(0,(zone_low-b['close'])/atr) for b in recent]
        return quantile(distances,.5)
    trailing=0
    for b in reversed(bars96):
        if zone_low-.5*atr <= b['close'] <= zone_high:trailing+=1
        else:break
    result=dict(
        amplitude_24_early_atr=am[0],amplitude_24_middle1_atr=am[1],
        amplitude_24_middle2_atr=am[2],amplitude_24_recent_atr=am[3],
        compression_24_recent_vs_early=safe_ratio(am[3],am[0]),
        amplitude_48_first_atr=amp(bars96[:48]),
        amplitude_48_recent_atr=amp(bars96[-48:]),
        compression_48_recent_vs_first=safe_ratio(amp(bars96[-48:]),amp(bars96[:48])),
        trough_low_q10_step1_atr=lo_steps[0],
        trough_low_q10_step2_atr=lo_steps[1],
        trough_progress_steps_last3=sum(v>0 for v in lo_steps),
        trough_net_progress_last3_atr=(ql[-1]-ql[0])/atr,
        near_zone_share_24_half_atr=near_share(bars96[-24:],.5),
        near_zone_share_48_half_atr=near_share(bars96[-48:],.5),
        near_zone_share_96_half_atr=near_share(bars96,.5),
        near_zone_share_48_one_atr=near_share(bars96[-48:],1),
        above_zone_share_48=sum(b['close']>zone_high for b in bars96[-48:])/48,
        near_zone_trailing_bars_96=trailing,
        max_low_below_zone_48_atr=max(0,(zone_low-min(b['low'] for b in bars96[-48:]))/atr),
        median_close_below_zone_48_atr=close_below(bars96[-48:]),
        pre_close_distance_below_zone_atr=(zone_low-bars96[-1]['close'])/atr,
    )
    if bars288 is not None:
        if len(bars288)!=288 or bars288[-96:]!=bars96:
            raise ValueError('Historia PRE 288 no coincide con PRE 96')
        a0=amp(bars288[:72]);a1=amp(bars288[-72:])
        result.update(near_zone_share_288_half_atr=near_share(bars288,.5),
            amplitude_72_first_288_atr=a0,amplitude_72_recent_288_atr=a1,
            compression_72_recent_vs_first_288=safe_ratio(a1,a0))
    return result


def _read_csv(path:Path, cols):
    with path.open('r',newline='',encoding='utf-8-sig') as f:
        reader=csv.DictReader(f)
        if not set(cols).issubset(reader.fieldnames or ()):
            raise ValueError('Columnas faltantes '+str(set(cols)-set(reader.fieldnames or ())))
        return [{k:r[k] for k in cols} for r in reader]


def read_inputs(discovery=DISCOVERY, overlap=OVERLAP):
    p=discovery/'attempts_pre.csv'; mp=discovery/'manifest.json'
    op=overlap/'attempt_episode_map.csv';om=overlap/'manifest.json'
    if not all(f.is_file() for f in (p,mp,op,om)):
        raise FileNotFoundError('Falta reporte Discovery 290 o Overlap 290')
    m=json.loads(mp.read_text());o=json.loads(om.read_text())
    if (o.get('rule',{}).get('max_gap_bars_default') != 24
        or o.get('rule',{}).get('max_span_from_first_pre_bars') != 96):
        raise ValueError('Reglas de episodios originales cambiadas')
    if (m.get('version')!='HLL_STRUCTURAL_DISCOVERY_V1'
        or m.get('reads_post') is not False or m.get('attempts')!=55717
        or m.get('symbols')!=290 or o.get('version')!='HLL_OVERLAP_AUDIT_V1'
        or o.get('no_post_used') is not True or o.get('different_level_ids_merged') is not False):
        raise ValueError('Manifiestos originales incompatibles')
    if (sha(p)!=m['output_sha256'].get('attempts_pre.csv')
        or sha(op)!=o['outputs_sha256'].get('attempt_episode_map.csv')
        or o['source_sha256'].get('discovery')!=m['output_sha256'].get('attempts_pre.csv')):
        raise ValueError('Hash/linaje de fuentes inválido')
    dr=_read_csv(p,SOURCE_COLUMNS)
    er=_read_csv(op,MAP_COLUMNS)
    if len(dr)!=55717 or len(er)!=55717:
        raise ValueError('El universo ya no tiene 55.717 intentos')
    emap={}
    for e in er:
        aid=e['attempt_id']
        if aid in emap:raise ValueError('Intento duplicado en episodio')
        emap[aid]=e
    seen=set(); first_counts=Counter()
    combined=[]
    for r in dr:
        aid=r['attempt_id']
        if (aid in seen or aid not in emap or '/' in r['symbol']
            or '\\' in r['symbol'] or '..' in r['symbol']):
            raise ValueError('Intentos duplicados/ausentes o símbolo inválido')
        seen.add(aid);e=emap[aid]
        if any(str(r[k])!=str(e[k]) for k in ('symbol','level_id','pre_closed_ts')):
            raise ValueError('Episodio no corresponde al corte PRE')
        if int(e['episode_ordinal'])<1 or int(e['is_first_in_episode'])!=(int(e['episode_ordinal'])==1):
            raise ValueError('Posición del intento inválida')
        for k in ('zone_low','zone_high','atr_pre'):
            if not math.isfinite(float(r[k])) or float(r[k])<=0:
                raise ValueError('Valores de nivel no válidos')
        e_id=e['episode_id']
        if int(e['is_first_in_episode']):first_counts[e_id]+=1
        combined.append({**r,'episode_id':e_id,'episode_ordinal':int(e['episode_ordinal']),
                         'is_first_in_episode':int(e['is_first_in_episode'])})
    if len(first_counts)!=int(o['heuristic_episodes_gap24_cap96']) or any(v!=1 for v in first_counts.values()):
        raise ValueError('Episodios sin representante único')
    if len(set(r['symbol'] for r in combined))!=290:raise ValueError('Universo 290 incorrecto')
    combined.sort(key=lambda r:(r['symbol'],int(r['pre_closed_ts']),r['attempt_id']))
    return combined,m,o,{'discovery_attempts':sha(p),'episode_map':sha(op)}


def attempt_metrics(row,candles):
    """Return one row for every attempted CROSS_UP, including incomplete context."""
    base={k:row[k] for k in SOURCE_COLUMNS}
    base.update(episode_id=row['episode_id'],episode_ordinal=row['episode_ordinal'],
                is_first_in_episode=row['is_first_in_episode'])
    pre=int(row['pre_closed_ts'])
    if pre % TF:raise ValueError('PRE no alineado en 30m')
    if row['status'] not in ('COMPLETE_PRE','INCOMPLETE_PRE_96'):
        raise ValueError('Estado de Discovery desconocido')
    b96=bar_window(candles,pre,96) if candles is not None else None
    b288=bar_window(candles,pre,288) if candles is not None else None
    n96=sum(t in candles for t in range(pre-96*TF,pre,TF)) if candles else 0
    n288=sum(t in candles for t in range(pre-288*TF,pre,TF)) if candles else 0
    base.update(pre_96_bars_found=n96,pre_288_bars_found=n288,
                status_96='COMPLETE_PRE' if b96 is not None else 'INCOMPLETE_PRE_96',
                status_288='COMPLETE_PRE_288' if b288 is not None else 'INCOMPLETE_PRE_288')
    if (b96 is not None)!=(row['status']=='COMPLETE_PRE'):
        raise ValueError('Cobertura real de velas contradice Discovery congelado: '+row['attempt_id'])
    if b96 is not None:
        base.update(metric_snapshot(b96,b288,float(row['zone_low']),
                                    float(row['zone_high']),float(row['atr_pre'])))
    return base


def write_rows(output,rows,disc_manifest,overlap_manifest,source_hashes,candle_dir=CANDLES):
    if output.exists():raise FileExistsError('Nunca sobrescribir salida existente: '+str(output))
    if not rows:raise ValueError('No hay intentos')
    if output.resolve()==Path(candle_dir).resolve() or Path(candle_dir).resolve() in output.resolve().parents:
        raise ValueError('La salida no puede estar dentro de los históricos')
    symbols=sorted({r['symbol'] for r in rows})
    output.parent.mkdir(parents=True,exist_ok=True)
    by_symbol={s:[] for s in symbols}
    for r in rows:by_symbol[r['symbol']].append(r)
    status96=Counter();status288=Counter(); first=0; processed=0
    with tempfile.TemporaryDirectory(prefix='.hll_pre_maturity_partial_',dir=output.parent) as tmp:
        tmp=Path(tmp)
        with (tmp/'attempt_maturity_pre.csv').open('w',encoding='utf-8',newline='') as f, \
             (tmp/'episode_first_maturity_pre.csv').open('w',encoding='utf-8',newline='') as ef:
            w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader()
            ew=csv.DictWriter(ef,fieldnames=EP_FIELDS);ew.writeheader()
            for i,s in enumerate(symbols,1):
                path=Path(candle_dir)/f'{s}_30m.csv'
                candles=ohlc.load_candles(path,timestamp_kind='open')[0] if path.is_file() else None
                for r in by_symbol[s]:
                    result=attempt_metrics(r,candles)
                    w.writerow({k:result.get(k,'') for k in FIELDS})
                    if r['is_first_in_episode']:
                        first+=1
                        ew.writerow({**{'first_attempt_id':r['attempt_id']},
                                     **{k:result.get(k,'') for k in FIELDS}})
                    processed+=1
                    status96[result['status_96']]+=1
                    status288[result['status_288']]+=1
                if i==1 or i%25==0 or i==len(symbols):
                    print('HLL_MATURITY_PROGRESS '+json.dumps({'symbols_done':i,'symbols_total':len(symbols),
                          'attempts_done':processed},ensure_ascii=False),flush=True)
        expected_first=(int(overlap_manifest['heuristic_episodes_gap24_cap96'])
                        if len(symbols)==290 else len({r['episode_id'] for r in rows}))
        if processed!=len(rows) or first!=expected_first:
            raise ValueError('Se perdió una fila o un episodio')
        man=dict(version='HLL_PRE_MATURITY_METRICS_V1',mode='OFFLINE_READ_ONLY',
            input_scope='CROSS_UP_CONDITIONED',symbols=len(symbols),attempts=len(rows),
            episodes=first,source_sha256=source_hashes,reads_post=False,
            reads_visual_labels=False,modifies_live=False,quality_score_generated=False,
            status_96=dict(status96),status_288=dict(status288),
            definitions={'amplitude':'(P90(high)-P10(low))/ATR_PRE',
                'compression':'recent robust amplitude divided by earlier robust amplitude; <1 = contraction',
                'trough_proxy':'P10(low) in three successive 24-bar blocks; NOT confirmed swing lows',
                'near_zone':'close between zone_low-0.5*ATR_PRE and zone_high',
                '288':'context only; missing/gapped windows are NA, never padded'},
            warning='Descriptivo, no calibrado: universo histórico seleccionado por CROSS_UP. Ninguna medida implica madurez buena, predictividad ni calidad.')
        man['outputs_sha256']={p.name:sha(p) for p in (tmp/'attempt_maturity_pre.csv',tmp/'episode_first_maturity_pre.csv')}
        (tmp/'manifest.json').write_text(json.dumps(man,indent=2,ensure_ascii=False),encoding='utf-8')
        os.replace(tmp,output)
    return man


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan',action='store_true')
    group.add_argument('--run',action='store_true')
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--discovery',type=Path,default=DISCOVERY)
    p.add_argument('--overlap',type=Path,default=OVERLAP)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output',type=Path,default=None)
    args=p.parse_args(argv)
    if not 0<=args.max_symbols<=290:p.error('--max-symbols fuera de rango')
    rows,dm,om,h=read_inputs(args.discovery,args.overlap)
    symbols=sorted(set(r['symbol'] for r in rows))
    if args.max_symbols:
        selected=set(symbols[:args.max_symbols])
        rows=[r for r in rows if r['symbol'] in selected]
    tag=f'pilot_first_{args.max_symbols}_v1' if args.max_symbols else 'full290_v1'
    out=args.output or OUTPUT/tag
    missing=[s for s in sorted({r['symbol'] for r in rows}) if not (args.candles_dir/f'{s}_30m.csv').is_file()]
    plan=dict(attempts=len(rows),symbols=len({r['symbol'] for r in rows}),
        episodes=len({r['episode_id'] for r in rows}),missing_candle_symbols=missing[:30],
        missing_candle_count=len(missing),output=str(out),reads_post=False,
        reads_visual_labels=False,modifies_live=False,no_overwrite=True,
        measures=['amplitude_and_compression','trough_proxy_progress','persistence_near_resistance'],
        conditioned_on_known_cross_up=True)
    print('HLL_MATURITY_PLAN '+json.dumps(plan,ensure_ascii=False,sort_keys=True),flush=True)
    if args.plan:return 0
    result=write_rows(out,rows,dm,om,h,args.candles_dir)
    print('HLL_MATURITY_DONE '+json.dumps({k:result[k] for k in
             ('symbols','attempts','episodes','status_96','status_288')},ensure_ascii=False,sort_keys=True),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
