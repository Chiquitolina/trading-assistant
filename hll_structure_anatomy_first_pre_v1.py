#!/usr/bin/env python3
"""First-CROSS_UP PRE anatomy baseline for 15 frozen reference episodes.

Requires the previous collection ZIP for 15 symbols and the full overlap
`attempt_episode_map.csv` for authoritative first-episode attempt metadata.
Runs OFFLINE/read-only and never supplies review labels to metrics.

 python3 hll_structure_anatomy_first_pre_v1.py --plan
 python3 hll_structure_anatomy_first_pre_v1.py --run
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import html
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import hll_structure_anatomy_pre_v1 as anatomy

DEFAULT_MAP = Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv')
DEFAULT_OUTPUT = Path('reports/horizontal_level_anatomy_pre_v1/first_baseline_15_v1')
COMP_FIELDS = ('lows_confirmed', 'low_up_steps_last3', 'low_net_progress_last3_atr',
  'latest_lhl_retention_fraction','signed_efficiency_48', 'residual_osc_24_recent_atr',
  'residual_osc_ratio_recent_vs_prev24','near_resistance_share_48',
  'zone_position_in_window_range')


def parse_mapped_row(row):
    required=set(anatomy.REQUIRED_MAP) | {'is_first_in_episode','episode_anchor_pre_ts'}
    if not required.issubset(row):
        raise ValueError('Faltan columnas en mapa completo: '+str(sorted(required-set(row))))
    r=dict(row)
    r['pre_closed_ts']=int(r['pre_closed_ts'])
    r['episode_ordinal']=int(r['episode_ordinal'])
    r['atr_pre']=float(r['atr_pre'])
    r['zone_low']=float(r['zone_low'])
    r['zone_high']=float(r['zone_high'])
    if (r['pre_closed_ts'] <= 0 or r['pre_closed_ts']%anatomy.BAR_MS or
        not (math.isfinite(r['atr_pre']) and r['atr_pre']>0) or
        not all(math.isfinite(r[k]) and r[k]>0 for k in ('zone_low','zone_high')) or
        r['zone_low']>r['zone_high']):
        raise ValueError('Metadatos PRE inválidos '+r['attempt_id'])
    return r


def load_first_baseline(source_zip:Path, full_map:Path):
    rows,candles,_,src_hash=anatomy.load_source(source_zip)
    if not full_map.is_file():
        raise FileNotFoundError('Mapa completo requerido en '+str(full_map))
    known_by_id={r['attempt_id']:r for r in rows}
    episode_symbol={eid:s for s,(_,eid) in anatomy.REFERENCES.items()}
    first_by_episode={}; seen_selected=set()
    with full_map.open('r',newline='',encoding='utf-8-sig') as f:
        reader=csv.DictReader(f)
        for raw in reader:
            eid=raw['episode_id']
            if eid not in episode_symbol:continue
            r=parse_mapped_row(raw);symbol=episode_symbol[eid]
            if r['symbol']!=symbol:
                raise ValueError('Símbolo equivocado en episodio '+eid)
            if r['attempt_id'] in known_by_id:
                old=known_by_id[r['attempt_id']]
                for k in ('symbol','level_id','pre_closed_ts','episode_id','episode_ordinal','zone_low','zone_high','atr_pre'):
                    if old[k]!=r[k]:
                        raise ValueError('Conflicto fuentes extraídas/mapa en '+r['attempt_id']+' '+k)
                seen_selected.add(r['attempt_id'])
            if r['is_first_in_episode']=='1':
                if eid in first_by_episode:raise ValueError('Primer intento duplicado '+eid)
                if (r['episode_ordinal']!=1 or r['episode_first_attempt_id']!=r['attempt_id'] or
                    int(r['episode_anchor_pre_ts'])!=r['pre_closed_ts']):
                    raise ValueError('Primer intento inconsistente '+eid)
                first_by_episode[eid]=r
    if seen_selected!=set(known_by_id):
        raise ValueError('Cortes originales ausentes del mapa completo: '+str(set(known_by_id)-seen_selected))
    if set(first_by_episode)!=set(episode_symbol):
        raise ValueError('Faltan primeras coordenadas: '+str(set(episode_symbol)-set(first_by_episode)))
    first_rows=[]
    for symbol,(_,eid) in anatomy.REFERENCES.items():
        r=first_by_episode[eid]
        if r['episode_first_attempt_id']!=r['attempt_id']:
            raise ValueError('first id inconsistente '+symbol)
        first_rows.append(r)
        if r['attempt_id'] not in known_by_id:
            known_by_id[r['attempt_id']]=r
    all_rows=sorted(known_by_id.values(),key=lambda r:(r['symbol'],r['pre_closed_ts']))
    if len(first_rows)!=15 or len(all_rows)!=40:
        raise ValueError('Esperados 15 primeros + 40 cortes totales, recibidos '+str((len(first_rows),len(all_rows))))
    if any(anatomy.take_pre(candles[r['symbol']],r['pre_closed_ts'],n) is None
           for r in all_rows for n in anatomy.WINDOWS):
        raise ValueError('Faltan velas PRE de algún primer corte o referencia')
    return first_rows,all_rows,candles,src_hash,anatomy.sha_bytes(full_map.read_bytes())


def status_first(first_rows,all_rows):
    byid={r['attempt_id']:r for r in all_rows}
    result=[]
    for first in first_rows:
        s=first['symbol'];rid,eid=anatomy.REFERENCES[s]
        review=byid[rid]
        assert review['episode_id']==eid and review['episode_ordinal']>=1
        result.append({'symbol':s,'episode_id':eid,
            'first_attempt_id':first['attempt_id'], 'first_pre_utc':anatomy.tsutc(first['pre_closed_ts']),
            'first_pre_closed_ts':first['pre_closed_ts'],
            'reviewed_attempt_id':rid,'reviewed_episode_ordinal':review['episode_ordinal'],
            'reviewed_pre_utc':anatomy.tsutc(review['pre_closed_ts']),
            'review_is_first':int(rid==first['attempt_id']),
            'visual_reference_grade_report_only':anatomy.REVIEW[s][0],
            'visual_reference_comment_report_only':anatomy.REVIEW[s][1]})
    return result


def pivot_table(rows,summaries,cache,pivots):
    ss={(r['attempt_id'],r['window_bars'],r['scale_atr']):r for r in summaries}
    details=status_first(rows,list({r['attempt_id']:r for r in rows}.values()))
    report=[]
    for d in details:
        aid=d['first_attempt_id'];sym=d['symbol']
        m={'symbol':sym,'episode_id':d['episode_id'],'attempt_id':aid,
          'pre_utc':d['first_pre_utc'],'status':'FIRST_CROSS_UP_PRE',
          'reviewed_attempt_id':d['reviewed_attempt_id'],
          'reviewed_ordinal':d['reviewed_episode_ordinal']}
        for n in anatomy.WINDOWS:
            r=ss[(aid,n,1.0)]
            for k in COMP_FIELDS:m[f'w{n}_{k}']=r[k]
        report.append(m)
    return report


def baseline_table(first_rows,summaries):
    ss={(r['attempt_id'],r['window_bars'],r['scale_atr']):r for r in summaries}
    result=[]
    for meta in first_rows:
        rid,eid=anatomy.REFERENCES[meta['symbol']]
        entry={'symbol':meta['symbol'],'episode_id':eid,'first_attempt_id':meta['attempt_id'],
            'first_utc':anatomy.tsutc(meta['pre_closed_ts']),
            'reviewed_attempt_id':rid,'reviewed_is_first':int(rid==meta['attempt_id'])}
        for n in anatomy.WINDOWS:
            r=ss[(meta['attempt_id'],n,1.0)]
            for f in COMP_FIELDS:entry[f'w{n}_{f}']=r[f]
        result.append(entry)
    return result


def comparisons(first_rows,rows,summaries):
    byid={r['attempt_id']:r for r in rows}
    ss={(r['attempt_id'],r['window_bars'],r['scale_atr']):r for r in summaries}
    result=[]
    for first in first_rows:
        sym=first['symbol'];reviewed_id,eid=anatomy.REFERENCES[sym]
        review=byid[reviewed_id]
        for w in anatomy.WINDOWS:
            initial=ss[(first['attempt_id'],w,1.0)];later=ss[(reviewed_id,w,1.0)]
            entry={'symbol':sym,'episode_id':eid,'window_bars':w,
                'first_attempt_id':first['attempt_id'],'first_pre_utc':anatomy.tsutc(first['pre_closed_ts']),
                'reviewed_attempt_id':reviewed_id,'reviewed_ordinal':review['episode_ordinal'],
                'reviewed_pre_utc':anatomy.tsutc(review['pre_closed_ts']),
                'different_cuts':int(reviewed_id!=first['attempt_id'])}
            for f in COMP_FIELDS:
                x=initial[f];y=later[f];entry[f+'_first']=x;entry[f+'_reviewed']=y
                entry[f+'_delta']=y-x if x is not None and y is not None else None
            result.append(entry)
    return result


def html_report(first_rows,all_rows,summaries,pivots,cache,basecsv,comparisons_rows):
    pby=defaultdict(list)
    for p in pivots:
        if p['scale_atr']==1.0 and p['window_bars']==288:
            pby[p['attempt_id']].append(p)
    byid={r['attempt_id']:r for r in all_rows}
    css='''<style>body{background:#0b1118;color:#e7ebee;font:14px system-ui;max-width:1140px;margin:24px auto;line-height:1.5}h1,h2{color:#f2f8fc}article{background:#151e26;border:1px solid #36424c;padding:16px;margin:22px 0;border-radius:10px}table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #303e49;text-align:left;padding:7px;font-size:12px}th{color:#9dcddc}code{color:#d8c490}small{color:#b5c3cc}.warning{padding:12px;background:#28271e;border-left:4px solid #c8a04d}svg{max-width:100%}</style>'''
    data=['<!doctype html><html lang="es"><meta charset="utf-8"><title>HLL - primer CROSS_UP de 15 episodios</title>',css,
      '<body><h1>HLL · Anatomía PRE · Línea de base del primer CROSS_UP</h1>',
      '<p>15 episodios de referencia, medidos en su <strong>primer intento real</strong>, con 96 y 288 velas PRE, 0.5/1.0/1.5 ATR. Las métricas no acceden a POST ni a calificaciones.</p>',
      '<p class="warning"><strong>Importante:</strong> las notas A/B/C pertenecen al corte visual que se evaluó históricamente, que en BAND, BICO, AXL y AWE fue posterior al primero. <strong>No</strong> se asigna esa nota al corte #1. La auditoría mide #1 para todos y conserva los cortes posteriores únicamente para comparar evolución.</p>',
      '<h2>Línea de base, PRE 96 y 1 ATR</h2><table><tr><th>Símbolo</th><th>First Attempt ID</th><th>Corte visual</th><th>LOW ↑</th><th>Progreso LOW (ATR)</th><th>Retención L-H-L</th><th>Eficiencia 48</th><th>Oscilación residual 24 (ATR)</th><th>Cerca de resistencia 48</th></tr>']
    ss={(r['attempt_id'],r['window_bars'],r['scale_atr']):r for r in summaries}
    for b in basecsv:
        aid=b['first_attempt_id'];r=ss[(aid,96,1.0)];rid=b['reviewed_attempt_id'];rev=byid[rid]
        vals=(b['symbol'],aid,'#'+str(rev['episode_ordinal']),r['low_up_steps_last3'],
          r['low_net_progress_last3_atr'],r['latest_lhl_retention_fraction'],r['signed_efficiency_48'],
          r['residual_osc_24_recent_atr'],r['near_resistance_share_48'])
        data.append('<tr>'+''.join(f'<td>{anatomy.fmt(x)}</td>' for x in vals)+'</tr>')
    data.append('</table>')
    for first in first_rows:
        s=first['symbol'];rid,eid=anatomy.REFERENCES[s]
        review=byid[rid];a=first['attempt_id']
        data.append('<article>')
        data.append(f'<h2>{html.escape(s)} · corte inicial #1</h2>')
        data.append(f'<p><small>Primer Attempt <code>{a}</code> · Episode <code>{eid}</code> · {anatomy.tsutc(first["pre_closed_ts"])}<br>Referencia visual: intento #{review["episode_ordinal"]}, <code>{rid}</code> (nota {html.escape(anatomy.REVIEW[s][0])}); la nota no describe necesariamente el primer corte.</small></p>')
        data.append(anatomy.svg_graph(cache[(a,288)],first,pby[a],288))
        data.append('<table><tr><th>Métrica del intento #1</th><th>96 velas</th><th>288 velas</th></tr>')
        for f in COMP_FIELDS:
            data.append(f'<tr><td>{html.escape(f)}</td><td>{anatomy.fmt(ss[(a,96,1.0)][f])}</td><td>{anatomy.fmt(ss[(a,288,1.0)][f])}</td></tr>')
        data.append('</table>')
        if rid!=a:
            data.append('<h3>Comparación #1 frente al corte realmente evaluado</h3>')
            data.append(anatomy.svg_graph(cache[(rid,288)],review,pby[rid],288))
            data.append(f'<p><small>Corte visual #{review["episode_ordinal"]} {anatomy.tsutc(review["pre_closed_ts"])}; evolución, no conocimiento disponible en #1.</small></p>')
        data.append('</article>')
    for symbol in ('WAXPUSDT','BSVUSDT'):
        seq=sorted((r for r in all_rows if r['symbol']==symbol),key=lambda r:r['episode_ordinal'])
        data.append(f'<h2>Serie de evolución · {symbol} ({len(seq)} intentos)</h2>')
        data.append('<table><tr><th>#</th><th>UTC</th><th>Low prog (ATR)</th><th>Retención LHL</th><th>Eff48</th><th>Resid24 (ATR)</th><th>Zona48</th></tr>')
        for r in seq:
            q=ss[(r['attempt_id'],96,1.0)]
            vs=(r['episode_ordinal'],anatomy.tsutc(r['pre_closed_ts']),q['low_net_progress_last3_atr'],
                q['latest_lhl_retention_fraction'],q['signed_efficiency_48'],
                q['residual_osc_24_recent_atr'],q['near_resistance_share_48'])
            data.append('<tr>'+''.join(f'<td>{anatomy.fmt(x)}</td>' for x in vs)+'</tr>')
        data.append('</table>')
    data.append('<h2>Alcance</h2><p>Las métricas de swings son descriptivas y dependen de la escala ATR de cada corte. La amplitud residual se calcula descontando una tendencia lineal de cierres. Los pivotes solo se usan desde su confirmación. No son señales de entrada, no miden probabilidad de breakout ni constituyen validación fuera de muestra. Los episodios se eligieron a partir de una bitácora visual; ningún rótulo participa del cálculo.</p></body></html>')
    return ''.join(data)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan',action='store_true');group.add_argument('--run',action='store_true')
    parser.add_argument('--source-zip',type=Path,default=anatomy.DEFAULT_SOURCE)
    parser.add_argument('--episode-map',type=Path,default=DEFAULT_MAP)
    parser.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    opt=parser.parse_args(argv)
    first,all_rows,candles,source_hash,map_hash=load_first_baseline(opt.source_zip,opt.episode_map)
    meta={'version':'HLL_ANATOMY_PRE_FIRST_BASELINE_V1','first_attempts':len(first),
        'distinct_episodes':len(anatomy.REFERENCES),'analyzed_distinct_cuts':len(all_rows),
        'additional_first_cuts_recovered':4,'timeframe':'30m','windows':list(anatomy.WINDOWS),
        'scales_atr':list(anatomy.SCALES),
        'source_sha256':source_hash,'episode_map_sha256':map_hash,
        'reads_post':False,'uses_review_labels_to_compute_metrics':False,'modifies_live':False,
        'review_labels_are_on_original_cuts_not_necessarily_first':True,
        'output':str(opt.output_dir)}
    print('HLL_ANATOMY_FIRST_PLAN',json.dumps(meta,ensure_ascii=False))
    if opt.plan:return 0
    if opt.output_dir.exists():raise FileExistsError('Salida ya existe, no sobrescribimos: '+str(opt.output_dir))
    summary,pivots,legs,chains,missing,cache=anatomy.collect(all_rows,candles)
    if missing:raise ValueError('Datos incompletos: '+str(missing[:5]))
    first15=baseline_table(first,summary)
    comp=comparisons(first,all_rows,summary)
    lineage=status_first(first,all_rows)
    html_text=html_report(first,all_rows,summary,pivots,cache,first15,comp)
    opt.output_dir.mkdir(parents=True,exist_ok=False)
    datafiles={
      'first15_summary_pre.csv':first15,
      'first_to_reviewed_pre.csv':comp,
      'attempt_lineage.csv':lineage,
      'summary_all_cuts_pre.csv':summary,
      'pivots_pre.csv':pivots,
      'legs_pre.csv':legs,
      'chains_pre.csv':chains}
    for name,rows in datafiles.items():anatomy.write_csv(opt.output_dir/name,rows)
    (opt.output_dir/'structure_audit_first15.html').write_text(html_text,encoding='utf-8')
    manifest={**meta,'status':'DONE','summary_rows':len(summary),
      'pivots':len(pivots),'legs':len(legs),'chains':len(chains),
      'first_attempt_ids':{r['symbol']:r['attempt_id'] for r in first},
      'source_contract':'ZIP + full frozen episode map; only closed PRE candles',
      'output_hashes':{n:anatomy.sha_bytes((opt.output_dir/n).read_bytes()) for n in (*datafiles,'structure_audit_first15.html')}}
    (opt.output_dir/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_ANATOMY_FIRST_DONE',json.dumps({'status':'DONE','episodes':len(first),
      'cuts':len(all_rows),'summary_rows':len(summary),'pivots':len(pivots),
      'output':str(opt.output_dir)},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
