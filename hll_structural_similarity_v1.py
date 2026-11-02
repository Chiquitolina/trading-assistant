#!/usr/bin/env python3
"""Índice y búsqueda universal de similitud de huellas PRE de HLL.

- Referencia: cualquier intento PRE completo y verificado, por su propia huella.
- Candidatos V1: SOLO el primer CROSS_UP de cada episodio, con 96+288 PRE.
- Comparación por familias elegidas; pesos IGUALES por familia (no por cantidad de variables).
- Distancia, no calidad, probabilidad de ruptura o rentabilidad.
- El indexador jamás lee POST, nota visual, ni intentos futuros como feature.
- Artefacto global inmutable: genera carpeta nueva y rechaza sobreescritura.

  python3 hll_structural_similarity_v1.py --plan
  python3 hll_structural_similarity_v1.py --run --max-symbols 5 --output .../pilot_v1
  python3 -u hll_structural_similarity_v1.py --run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path

import hll_structural_fingerprint_v1 as fp

VERSION = 'HLL_STRUCTURAL_SIMILARITY_PRE_V1'
DEFAULT_MAP = Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv')
DEFAULT_OVERLAP_MANIFEST = DEFAULT_MAP.parent / 'manifest.json'
DEFAULT_CANDLES = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')
DEFAULT_OUTPUT = Path('reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1')
SCALE_ATR = 1.0
FLOOR_BY_UNIT = {'fraccion': .10, 'ATR': .5, 'ratio': .25,
                 'cantidad': 1.0, 'velas': 4.0, '[-1,1]': .10}
META_KEYS = ('attempt_id','symbol','episode_id','level_id','pre_closed_ts','zone_low','zone_high','atr_pre')


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for piece in iter(lambda:f.read(1024*1024), b''):
            h.update(piece)
    return h.hexdigest()


def finite(x):
    try:
        n=float(x)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError,OverflowError):
        return None


def load_episode_first(map_path=DEFAULT_MAP, manifest_path=DEFAULT_OVERLAP_MANIFEST):
    """Require exact first attempt per ID, plus overlap file checksum."""
    if not Path(manifest_path).is_file():
        raise FileNotFoundError('No se encontró manifest del solapamiento: '+str(manifest_path))
    man=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    hashes=man.get('outputs_sha256',{})
    expected=hashes.get('attempt_episode_map.csv')
    if not expected or sha256(map_path)!=expected:
        raise ValueError('Mapa de episodios distinto al manifest o sin hash')
    output=[]; first_ids=set(); episode_ids=set(); seen_attempt=set()
    with Path(map_path).open(newline='',encoding='utf-8-sig') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames or set(META_KEYS + ('is_first_in_episode','episode_ordinal','episode_first_attempt_id','status'))-set(reader.fieldnames):
            raise ValueError('Faltan columnas del mapa canónico')
        for r in reader:
            aid=r['attempt_id']
            if not aid or aid in seen_attempt:raise ValueError('Attempt ID duplicado/vacío')
            seen_attempt.add(aid)
            if r['is_first_in_episode']!='1':continue
            if r['episode_ordinal']!='1' or r['episode_first_attempt_id']!=aid:
                raise ValueError('Primer CROSS_UP no consistente con el mapa')
            eid=r['episode_id']
            if not eid or eid in episode_ids:raise ValueError('Episode ID duplicado/vacío')
            episode_ids.add(eid)
            for field in ('pre_closed_ts','atr_pre','zone_low','zone_high'):
                if finite(r[field]) is None:raise ValueError('Metadatos iniciales no numéricos')
            pre=int(r['pre_closed_ts'])
            if (pre % fp.anatomy.BAR_MS or finite(r['atr_pre'])<=0
                    or finite(r['zone_low'])<=0 or finite(r['zone_high'])<finite(r['zone_low'])):
                raise ValueError('Metadatos iniciales inválidos')
            output.append({**r,'pre_closed_ts':pre,'episode_ordinal':1,
                           'atr_pre':float(r['atr_pre']),
                           'zone_low':float(r['zone_low']), 'zone_high':float(r['zone_high'])})
    if not output:raise ValueError('No hay primeros intentos')
    if man.get('attempts') is not None and len(seen_attempt)!=int(man['attempts']):
        raise ValueError('Intentos del mapa no coinciden con manifest')
    if man.get('episodes') is not None and len(output)!=int(man['episodes']):
        raise ValueError('Cantidad de episodios no coincide con manifest')
    return sorted(output,key=lambda r:(r['symbol'],r['pre_closed_ts'],r['episode_id']))


def _quantile(seq, q):
    if not seq:return None
    values=sorted(seq)
    i=(len(values)-1)*q
    lo=int(i); hi=min(lo+1,len(values)-1)
    return values[lo]*(1-(i-lo))+values[hi]*(i-lo)


def build_normalizers(rows):
    if not rows:raise ValueError('No hay índice para normalizar')
    norm={}
    for key,(_,unit,*_) in fp.FEATURES.items():
        vals=[finite(r.get(key)) for r in rows if r.get('status')=='COMPLETE_PRE_96_288']
        vals=[v for v in vals if v is not None]
        floor=FLOOR_BY_UNIT[unit]
        iqr=(_quantile(vals,.75)-_quantile(vals,.25)) if vals else 0
        norm[key]={'scale':max(iqr,floor),'floor':floor,'observations':len(vals),
                   'p25':_quantile(vals,.25),'p75':_quantile(vals,.75)}
    return norm


def index_rows(first_rows, candle_dir, max_symbols=None, symbols=None, *, progress=None):
    """Load one symbol at a time; do not materialize all 290 OHLC streams."""
    import hll_structural_approach_ohlc_pre as ohlc
    symbol_groups=defaultdict(list)
    for r in first_rows:symbol_groups[r['symbol']].append(r)
    selected=sorted(symbol_groups)
    if symbols is not None:
        only=set(symbols)
        missing=only-set(selected)
        if missing:raise ValueError('Símbolos ausentes del mapa: '+','.join(sorted(missing)))
        selected=[s for s in selected if s in only]
    if max_symbols is not None:
        if max_symbols<1:raise ValueError('--max-symbols debe ser >=1')
        selected=selected[:max_symbols]
    all_rows=[]
    for i,symbol in enumerate(selected,1):
        path=Path(candle_dir)/(symbol+'_30m.csv')
        if not path.is_file():
            raise FileNotFoundError('No existe archivo OHLC 30m: '+str(path))
        candles,_=ohlc.load_candles(path,timestamp_kind='open')
        for r in symbol_groups[symbol]:
            if r['status']!='COMPLETE_PRE':
                row=empty_index_row(r,'INCOMPLETE_PRE_96')
            else:
                pre=r['pre_closed_ts']
                a=fp.anatomy.take_pre(candles,pre,96)
                b=fp.anatomy.take_pre(candles,pre,288)
                if a is None or b is None:
                    row=empty_index_row(r,'INCOMPLETE_PRE_96' if a is None else 'INCOMPLETE_PRE_288')
                else:
                    row=fp.build_fingerprint(r,a,b,SCALE_ATR)
            all_rows.append(row)
        if progress and (i==1 or i%25==0 or i==len(selected)):
            progress(i,len(selected),len(all_rows))
    return all_rows,selected


def empty_index_row(meta,status):
    return {'fingerprint_version':fp.VERSION, 'symbol':meta['symbol'],'episode_id':meta['episode_id'],
            'attempt_id':meta['attempt_id'],'level_id':meta['level_id'],
            'pre_closed_ts':meta['pre_closed_ts'],'episode_ordinal_report_only':1,
            'atr_pre_report_only':meta['atr_pre'],'zone_low_report_only':meta['zone_low'],
            'zone_high_report_only':meta['zone_high'],'scale_atr':SCALE_ATR,
            'status':status,'observed_features':0,'total_features':fp.NUM_FEATURES,
            **{f'coverage_{fam}':f'0/{sum(x[0]==fam for x in fp.FEATURES.values())}'
               for fam in fp.FAMILIES},**{name:None for name in fp.FEATURES}}


def build_reference(selected_row, candles, scale_atr=1.0):
    """Reference can be #N, not only first attempt; never substitute #1."""
    if scale_atr != SCALE_ATR:raise ValueError('Este índice V1 trabaja exclusivamente en escala 1 ATR')
    pre=int(selected_row['pre_closed_ts'])
    a=fp.anatomy.take_pre(candles,pre,96)
    b=fp.anatomy.take_pre(candles,pre,288)
    if a is None or b is None:
        raise ValueError('La referencia requiere 96 y 288 velas PRE contiguas')
    return fp.build_fingerprint(selected_row,a,b,SCALE_ATR)


def _family_comparison(reference, candidate, normalizers, family, min_fraction):
    ids=[fid for fid,defn in fp.FEATURES.items() if defn[0]==family]
    paired=[]
    for fid in ids:
        a=finite(reference.get(fid)); b=finite(candidate.get(fid))
        if a is None or b is None:continue
        scale=normalizers[fid]['scale']
        if not scale or not math.isfinite(scale) or scale<=0:
            raise ValueError('Normalización inválida '+fid)
        diff=abs(a-b)/scale
        paired.append((fid,a,b,min(diff,4.0),diff))
    if len(paired)/len(ids)<min_fraction:return None, paired
    return sum(x[3] for x in paired)/len(paired),paired


def compare(reference, candidate, normalizers, selected_families, mode='balanced',
            min_fraction=None):
    """Equal weight per family. Missing values are never turned into zeros.

    Returns None if any chosen family has insufficient comparable observations.
    Strict: worst family dominates; balanced: equal-family mean.
    """
    if mode not in ('balanced','strict'):
        raise ValueError('Modo desconocido')
    chosen=list(selected_families)
    if not chosen or len(set(chosen))!=len(chosen) or set(chosen)-set(fp.FAMILIES):
        raise ValueError('Familias desconocidas o vacías')
    if candidate.get('status')!='COMPLETE_PRE_96_288' or reference.get('status')!='COMPLETE_PRE_96_288':
        return None
    coverage=.7 if min_fraction is None and mode=='balanced' else (.85 if min_fraction is None else min_fraction)
    details={}; matched={}
    for fam in chosen:
        d,pa=_family_comparison(reference,candidate,normalizers,fam,coverage)
        if d is None:return None
        details[fam]=d
        matched[fam]=pa
    distance=sum(details.values())/len(details) if mode=='balanced' else max(details.values())
    return {'distance':distance,'family_distances':details,'paired':matched,
            'family_coverage':{f:len(matched[f])/sum(v[0]==f for v in fp.FEATURES.values()) for f in chosen}}


def rank(reference, candidates, normalizers, selected_families, *, mode='balanced',
         top=20, distinct_symbols=True):
    if not isinstance(top,int) or not 1<=top<=100:raise ValueError('top fuera de rango')
    ranked=[]; missing=0; excluded=0
    for r in candidates:
        if r['episode_id']==reference['episode_id']:
            excluded+=1;continue
        comp=compare(reference,r,normalizers,selected_families,mode)
        if comp is None:
            missing+=1;continue
        ranked.append({'candidate':r,**comp})
    ranked.sort(key=lambda x:(x['distance'],x['candidate']['symbol'],x['candidate']['episode_id']))
    if distinct_symbols:
        seen=set();selected=[]
        for r in ranked:
            sym=r['candidate']['symbol']
            if sym in seen:continue
            selected.append(r);seen.add(sym)
            if len(selected)==top:break
    else:
        selected=ranked[:top]
    return {'results':selected,'eligible_candidates':len(ranked),
            'insufficient_candidates':missing,'excluded_same_episode':excluded,
            'total_candidates':len(candidates), 'mode':mode,'selected_families':list(selected_families),
            'limited_to_episode_first':True,'all_ranked_candidates':len(ranked)}


def load_index(directory):
    directory=Path(directory)
    man=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if man.get('version')!=VERSION or man.get('reads_post') is not False or man.get('quality_score') is not False or man.get('index_cuts')!='EPISODE_FIRST_ONLY':
        raise ValueError('Manifiesto de índice incompatible')
    csvp=directory/'fingerprints_first_pre.csv'; np=directory/'normalizers.json'
    if sha256(csvp)!=man.get('fingerprints_sha256') or sha256(np)!=man.get('normalizers_sha256'):
        raise ValueError('Índice o normalizadores alterados')
    normalizers=json.loads(np.read_text(encoding='utf-8'))
    if set(normalizers)!=set(fp.FEATURES):raise ValueError('Faltan normalizadores')
    rows=[]; seen=set()
    with csvp.open(newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r['episode_id'] in seen or r['episode_ordinal_report_only']!='1' or r['scale_atr']!='1.0':
                raise ValueError('Índice repetido/no inicial/escala no comparable')
            seen.add(r['episode_id'])
            row={**r,**{name:finite(r[name]) for name in fp.FEATURES}}
            rows.append(row)
    if len(rows)!=man.get('episodes'):
        raise ValueError('Cantidad de episodios inválida')
    return rows,normalizers,man


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan',action='store_true')
    group.add_argument('--run',action='store_true')
    p.add_argument('--map',type=Path,default=DEFAULT_MAP)
    p.add_argument('--manifest',type=Path,default=DEFAULT_OVERLAP_MANIFEST)
    p.add_argument('--candles',type=Path,default=DEFAULT_CANDLES)
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--max-symbols',type=int,default=None)
    p.add_argument('--symbols',type=str,default=None,help='CSV de símbolos para piloto aislado')
    args=p.parse_args(argv)
    first=load_episode_first(args.map,args.manifest)
    sym_list=sorted({r['symbol'] for r in first})
    selected=([s for s in sym_list if s in set(args.symbols.split(','))] if args.symbols else sym_list)
    if args.max_symbols is not None:selected=selected[:args.max_symbols]
    if not selected:raise SystemExit('No hay símbolos para analizar')
    plan={'version':VERSION,'episodes_source':len(first),'symbols_source':len(sym_list),
          'symbols_to_index':len(selected),
          'episodes_to_index':sum(r['symbol'] in set(selected) for r in first),
          'features':fp.NUM_FEATURES,'families':len(fp.FAMILIES),
          'index_cuts':'EPISODE_FIRST_ONLY','scale_atr':SCALE_ATR,
          'reads_post':False,'reads_visual_labels':False,'modifies_live':False,
          'output':str(args.output)}
    print('HLL_SIMILARITY_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if args.plan:return 0
    if args.output.exists():
        raise SystemExit('NO_OVERWRITE: salida ya existe '+str(args.output))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def progress(i,total,rows):
        if i==total or i==1 or i%25==0:
            print('HLL_SIMILARITY_PROGRESS',json.dumps({'symbols_done':i,'total':total,'episodes':rows}),flush=True)
    rows,symbols=index_rows(first,args.candles,max_symbols=args.max_symbols,
                            symbols=args.symbols.split(',') if args.symbols else None,
                            progress=progress)
    norm=build_normalizers(rows)
    tmp=Path(tempfile.mkdtemp(prefix='.hll_similarity_build_',dir=args.output.parent))
    try:
        csvp=tmp/'fingerprints_first_pre.csv'
        columns=list(rows[0])
        with csvp.open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=columns,extrasaction='raise')
            writer.writeheader();writer.writerows(rows)
        np=tmp/'normalizers.json';np.write_text(json.dumps(norm,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        man={**plan,'episodes':len(rows),
             'complete_288':sum(r['status']=='COMPLETE_PRE_96_288' for r in rows),
             'fingerprints_sha256':sha256(csvp),'normalizers_sha256':sha256(np),
             'source_map_sha256':sha256(args.map),'status':'DONE','quality_score':False,
             'cohort':'Historical CROSS_UP only; no unbroken pre-cross setups',
             'index_warning':'Candidatos: primer CROSS_UP del episodio; referencia: corte seleccionado #N',
             'no_overwrite':True}
        (tmp/'manifest.json').write_text(json.dumps(man,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.rename(tmp,args.output)
    finally:
        if tmp.exists():
            import shutil;shutil.rmtree(tmp)
    print('HLL_SIMILARITY_DONE',json.dumps({'status':'DONE','episodes':len(rows),
          'complete_288':man['complete_288'],'families':len(fp.FAMILIES),
          'output':str(args.output)},ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
