"""Inspector HLL: exploración descriptiva PRE del universo CROSS_UP 290.

No toca señales, vistas existentes, POST de referencia, órdenes ni LIVE.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from statistics import median
from pathlib import Path

import pandas as pd

from hll_structural_discovery_v1 import CANDLES, OUTPUT_PARENT, TF, bar_window, sha

OVERLAP_PARENT = Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1')
MATURITY_PARENT = Path('reports/horizontal_level_pre_maturity_metrics_v1/full290_v1')
# V2.6 independent test events, only test timestamps at or before selected PRE cutoff.
TOUCHES_SOURCE = Path('reports/horizontal_level_structure_lab_v26/runs/sep290_frozen_v26_roundtrip/touches.csv')

# These are DISPLAY SORTS only, not entry filters or quality labels.
TOUCH_SORTS = {
    'Más contactos PRE desde abajo (288 velas)': ('touches_below_pre288', False),
    'Más contactos PRE desde abajo (96 velas)': ('touches_below_pre96', False),
    'Más contactos PRE en total (288 velas)': ('touches_all_pre288', False),
    'Mayor duración entre contactos PRE desde abajo (288 velas)': ('touch_span_pre288_bars', False),
    'Menor separación mediana entre contactos PRE desde abajo (288 velas)': ('touch_gap_median_pre288_bars', True),
    'Mayor duración observada entre contactos PRE desde abajo (histórico V2.6)': ('touch_span_all_pre_bars', False),
}
MATURITY_SORTS = {
    'Menor amplitud robusta reciente (24 velas / ATR)': ('amplitude_24_recent_atr', True),
    'Más compresión 24 vs primeras 24 (96 PRE)': ('compression_24_recent_vs_early', True),
    'Más compresión 48 vs primeras 48 (96 PRE)': ('compression_48_recent_vs_first', True),
    'Más compresión 72 vs primeras 72 (288 PRE)': ('compression_72_recent_vs_first_288', True),
    'Mayor progreso de mínimos por bloques (proxy 96 PRE)': ('trough_net_progress_last3_atr', False),
    'Mayor persistencia cercana a resistencia (48 PRE)': ('near_zone_share_48_half_atr', False),
    'Mayor persistencia cercana a resistencia (288 PRE)': ('near_zone_share_288_half_atr', False),
    'Menor distancia extrema debajo de zona (48 PRE)': ('max_low_below_zone_48_atr', True),
}
EPISODE_SORTS = {
    'Más CROSS_UP del episodio (retrospectivo)': ('attempts', False),
    'Menos CROSS_UP del episodio (retrospectivo)': ('attempts', True),
    'Más duración entre primer y último CROSS_UP (retrospectivo)': ('span_bars', False),
}

REQUIRED=('attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high',
          'atr_pre','status','family_0p5','family_1p0','family_1p5')
FAMILY_LABELS={
 'ASCENSO_ESCALONADO':'Ascenso escalonado',
 'RECONSTRUCCION_ENTRE_CONTACTOS':'Reconstrucción entre contactos',
 'OSCILACION_EN_RESISTENCIA':'Oscilación en resistencia',
 'REBOTE_TRAS_CORRECCION':'Rebote tras corrección',
 'DETERIORO_DESDE_MINIMO':'Deterioro desde mínimo',
 'CORRECCION_DOMINANTE':'Corrección dominante',
 'SECUENCIA_MIXTA_INDETERMINADA':'Mixta / indeterminada',
 'INDETERMINADA_SIN_MINIMO':'Sin mínimo confirmado',
}


def compute_pre_touch_metrics(attempts: pd.DataFrame, touches: pd.DataFrame) -> pd.DataFrame:
    """Counts tests with touch_closed_ts <= this attempt's PRE cutoff only.

    V2.6 `touches.csv` events are not CROSS_UP attempts. No resolver/outcome is read.
    Insufficient contacts produce null spans, NEVER a fabricated zero duration.
    """
    required_attempt={'attempt_id','symbol','level_id','pre_closed_ts'}
    required_touch={'symbol','level_id','test_id','touch_closed_ts','side'}
    if required_attempt-set(attempts) or required_touch-set(touches):
        raise ValueError('Faltan columnas PRE para medir contactos independientes V2.6')
    if attempts.attempt_id.isna().any() or attempts.attempt_id.duplicated().any():
        raise ValueError('Attempt ID duplicado en cálculo de contactos')
    x=touches[list(required_touch)].copy()
    if (x[list(required_touch)].isna().any().any()
            or x.duplicated(['symbol','level_id','test_id']).any()):
        raise ValueError('Contactos V2.6 duplicados o incompletos')
    x['touch_closed_ts']=pd.to_numeric(x.touch_closed_ts,errors='raise').astype('int64')
    if x.touch_closed_ts.le(0).any():
        raise ValueError('Fechas de contactos no válidas')
    observed={}
    for key,g in x.groupby(['symbol','level_id'],sort=False):
        all_ts=sorted(g.touch_closed_ts.astype('int64').tolist())
        below_ts=sorted(g.loc[g.side.eq('BELOW'),'touch_closed_ts'].astype('int64').tolist())
        observed[key]=(all_ts,below_ts)
    out=[]
    for rec in attempts[['attempt_id','symbol','level_id','pre_closed_ts']].itertuples(index=False):
        pre=int(rec.pre_closed_ts)
        all_ts,below_ts=observed.get((rec.symbol,rec.level_id),([],[]))
        def recent(seq,bars):
            # Only COMPLETED test events inside the preceding `bars` 30m intervals.
            end=bisect_right(seq,pre)
            left=bisect_right(seq,pre-bars*TF)
            return seq[left:end]
        a288=recent(all_ts,288)
        b288=recent(below_ts,288)
        b96=recent(below_ts,96)
        known_below_count=bisect_right(below_ts,pre)
        span288=(b288[-1]-b288[0])//TF if len(b288)>=2 else float('nan')
        span_all=((below_ts[known_below_count-1]-below_ts[0])//TF
                  if known_below_count>=2 else float('nan'))
        med_gap=(float(median([b-a for a,b in zip(b288,b288[1:])]))/TF
                 if len(b288)>=2 else float('nan'))
        out.append((rec.attempt_id,len(a288),len(b288),len(b96),span288,med_gap,span_all))
    return pd.DataFrame(out,columns=['attempt_id','touches_all_pre288',
          'touches_below_pre288','touches_below_pre96','touch_span_pre288_bars',
          'touch_gap_median_pre288_bars','touch_span_all_pre_bars'])


def sort_discovery_rows(rows: pd.DataFrame, order: str, *, episodic: bool) -> pd.DataFrame:
    """Stable sort without using POST; retrospective episode statistics are explicit."""
    sort_options={**TOUCH_SORTS,**MATURITY_SORTS,**(EPISODE_SORTS if episodic else {})}
    if order=='Fecha PRE (actual)':
        keys=(['symbol','first_pre_ts','episode_id'] if episodic
              else ['symbol','pre_closed_ts','attempt_id'])
        return rows.sort_values(keys,kind='mergesort')
    if order not in sort_options:
        raise ValueError('Ordenamiento de Discovery desconocido')
    col,asc=sort_options[order]
    if col not in rows:
        raise ValueError(f'Falta métrica para ordenar: {col}')
    key=('episode_id' if episodic else 'attempt_id')
    return rows.sort_values([col,'symbol',key],ascending=[asc,True,True],
                            kind='mergesort',na_position='last')


def filter_episodes_by_attempt_count(rows: pd.DataFrame, minimum: int, maximum: int) -> pd.DataFrame:
    """Inclusive episode-level count filter. NEVER changes underlying CROSS_UP membership.

    Counts describe the whole bounded episode, including attempts later than the
    first PRE cutoff. This is retrospective navigation, not an entry feature.
    """
    if 'attempts' not in rows:
        raise ValueError('Falta la cantidad de intentos por episodio')
    if (not isinstance(minimum, int) or isinstance(minimum, bool)
            or not isinstance(maximum, int) or isinstance(maximum, bool)
            or minimum < 1 or maximum < minimum):
        raise ValueError('Rango de intentos inválido')
    counts=pd.to_numeric(rows.attempts,errors='coerce')
    if (counts.isna().any() or counts.lt(1).any() or counts.mod(1).ne(0).any()):
        raise ValueError('Cantidad de intentos ausente o inválida')
    return rows.loc[counts.between(minimum,maximum)].copy()


def load_discovery(directory: Path):
    manifest_path=directory/'manifest.json'
    csv_path=directory/'attempts_pre.csv'
    if not manifest_path.is_file() or not csv_path.is_file():
        return None
    manifest=json.loads(manifest_path.read_text(encoding='utf8'))
    if manifest.get('version')!='HLL_STRUCTURAL_DISCOVERY_V1' or manifest.get('reads_post') is not False:
        raise ValueError('Manifiesto de Discovery incompatible/no causal')
    # Lightweight integrity: expensive SHA check done once on actual render via cache below.
    if sha(csv_path)!=manifest.get('output_sha256',{}).get('attempts_pre.csv'):
        raise ValueError('Hash de CSV Discovery no coincide con manifiesto')
    df=pd.read_csv(csv_path,low_memory=False)
    if set(REQUIRED)-set(df):raise ValueError('Faltan columnas de Discovery')
    if len(df)!=int(manifest['attempts']) or df.attempt_id.isna().any() or df.attempt_id.duplicated().any():
        raise ValueError('Cardinalidad/IDs de Discovery inválidos')
    if df.symbol.nunique()!=int(manifest['symbols']):raise ValueError('Símbolos no coinciden con manifiesto')
    if 'post_outcome' in df or 'v26_status' in df:
        raise ValueError('El archivo de Discovery expone columnas POST')
    return df,manifest



def load_maturity(directory: Path, discovery: pd.DataFrame, discovery_manifest: dict,
                  overlap_manifest: dict):
    """Validate the PRE-only output and exact source membership before display."""
    mp=directory/'manifest.json'
    csvp=directory/'attempt_maturity_pre.csv'
    epp=directory/'episode_first_maturity_pre.csv'
    if not all(p.is_file() for p in (mp,csvp,epp)):
        return None
    man=json.loads(mp.read_text(encoding='utf-8'))
    hashes=man.get('outputs_sha256',{})
    sources=man.get('source_sha256',{})
    if (man.get('version')!='HLL_PRE_MATURITY_METRICS_V1'
        or man.get('mode')!='OFFLINE_READ_ONLY'
        or man.get('reads_post') is not False
        or man.get('reads_visual_labels') is not False
        or man.get('quality_score_generated') is not False
        or man.get('attempts')!=len(discovery)
        or man.get('episodes')!=overlap_manifest.get('heuristic_episodes_gap24_cap96')
        or sources.get('discovery_attempts') != discovery_manifest.get('output_sha256',{}).get('attempts_pre.csv')
        or sources.get('episode_map') != overlap_manifest.get('outputs_sha256',{}).get('attempt_episode_map.csv')):
        raise ValueError('Manifiesto de madurez no corresponde a Discovery/Episodios')
    if sha(csvp)!=hashes.get(csvp.name) or sha(epp)!=hashes.get(epp.name):
        raise ValueError('Hash de métricas PRE inválido')
    from hll_pre_maturity_metrics_v1 import METRICS
    cols=['attempt_id','symbol','level_id','pre_closed_ts','status',
          'status_96','status_288',*METRICS]
    metrics=pd.read_csv(csvp,usecols=cols,low_memory=False)
    if (len(metrics)!=len(discovery) or metrics.attempt_id.isna().any()
            or metrics.attempt_id.duplicated().any()):
        raise ValueError('Intentos duplicados/faltantes en madurez')
    joined=metrics[['attempt_id','symbol','level_id','pre_closed_ts','status']].merge(
        discovery[['attempt_id','symbol','level_id','pre_closed_ts','status']],on='attempt_id',
        how='outer',validate='one_to_one',indicator=True,suffixes=('_mat','_dis'))
    if joined._merge.ne('both').any():
        raise ValueError('Métrica no coincide con el universo de intentos')
    for k in ['symbol','level_id','pre_closed_ts','status']:
        if joined[f'{k}_mat'].astype(str).ne(joined[f'{k}_dis'].astype(str)).any():
            raise ValueError('Métrica con PRE/episodio alterado: '+k)
    if (metrics.status_96.eq('COMPLETE_PRE').ne(metrics.status.eq('COMPLETE_PRE')).any()
            or metrics.loc[metrics.status_288.ne('COMPLETE_PRE_288'),
               ['near_zone_share_288_half_atr','compression_72_recent_vs_first_288']].notna().any().any()):
        raise ValueError('Cobertura/valores 288 inconsistentes')
    return metrics[['attempt_id','status_96','status_288',*METRICS]]


OVERLAP_MAP_REQUIRED = {
    'attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'zone_low', 'zone_high',
    'status', 'family_1p0', 'episode_id', 'episode_first_attempt_id',
    'episode_anchor_pre_ts', 'episode_ordinal', 'episode_attempt_count',
    'is_first_in_episode', 'other_levels_same_candle',
    'candidate_other_level_prior_count',
}
OVERLAP_EP_REQUIRED = {
    'episode_id', 'symbol', 'level_id', 'first_attempt_id', 'first_pre_ts',
    'last_pre_ts', 'span_bars', 'attempts', 'first_family_1p0',
    'distinct_families_1p0', 'family_changes_within_episode',
}
OVERLAP_NEAR_REQUIRED = {
    'attempt_id', 'symbol', 'pre_closed_ts', 'level_id', 'other_attempt_id',
    'other_level_id', 'other_pre_closed_ts', 'delta_bars',
    'center_distance_atr', 'zone_gap_atr', 'interpretation',
}


def load_overlap(directory: Path, discovery: pd.DataFrame, discovery_manifest: dict):
    """Check source lineage, output hashes and exact one-to-one PRE membership.

    Different level IDs remain separate. CSV data is not used to change any family.
    """
    manifest_path = directory/'manifest.json'
    if not manifest_path.is_file():
        return None
    manifest=json.loads(manifest_path.read_text(encoding='utf8'))
    rule=manifest.get('rule', {})
    if (manifest.get('version')!='HLL_OVERLAP_AUDIT_V1'
            or manifest.get('mode')!='READ_ONLY_ANALYSIS'
            or manifest.get('no_post_used') is not True
            or manifest.get('different_level_ids_merged') is not False
            or rule.get('same_level_exact') is not True
            or rule.get('max_gap_bars_default')!=24
            or rule.get('max_span_from_first_pre_bars')!=96):
        raise ValueError('Manifiesto de episodios incompatible (reglas 24/96)')
    src=manifest.get('source_sha256',{})
    dsrc=discovery_manifest.get('source_sha256',{})
    dout=discovery_manifest.get('output_sha256',{})
    if (src.get('discovery')!=dout.get('attempts_pre.csv')
            or src.get('cohort')!=dsrc.get('cohort')):
        raise ValueError('Episodios y Discovery tienen fuentes distintas')
    outputs=manifest.get('outputs_sha256',{})
    required={'attempt_episode_map.csv','episode_candidates.csv',
              'nearby_other_levels_review.csv','level_reuse.csv','symbol_summary.csv'}
    if not required.issubset(outputs):
        raise ValueError('Manifiesto de episodios incompleto')
    for name in sorted(required):
        path=directory/name
        if not path.is_file() or sha(path)!=outputs[name]:
            raise ValueError(f'Archivo de episodios ausente o modificado: {name}')
    amap=pd.read_csv(directory/'attempt_episode_map.csv',low_memory=False)
    eps=pd.read_csv(directory/'episode_candidates.csv',low_memory=False)
    near=pd.read_csv(directory/'nearby_other_levels_review.csv',low_memory=False)
    if (OVERLAP_MAP_REQUIRED-set(amap) or OVERLAP_EP_REQUIRED-set(eps)
            or OVERLAP_NEAR_REQUIRED-set(near)):
        raise ValueError('Columnas requeridas de episodios incompletas')
    if (len(amap)!=len(discovery) or len(amap)!=manifest.get('attempts')
            or len(eps)!=manifest.get('heuristic_episodes_gap24_cap96')
            or amap.symbol.nunique()!=manifest.get('symbols')
            or amap.attempt_id.isna().any() or amap.attempt_id.duplicated().any()
            or eps.episode_id.isna().any() or eps.episode_id.duplicated().any()):
        raise ValueError('Cardinalidad/IDs de episodios inválidos')
    # Validate every attempt against the source, including incomplete PRE cases.
    keys=['attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high','status','family_1p0']
    source=discovery[keys].copy()
    source['family_1p0']=source.family_1p0.fillna('SIN_DATOS')
    joined=amap[keys].merge(source,on='attempt_id',how='outer',validate='one_to_one',
                             suffixes=('_ep','_dis'),indicator=True)
    if joined._merge.ne('both').any():
        raise ValueError('Faltan intentos o existen intentos extra en episodios')
    for field in ('symbol','level_id','pre_closed_ts','status','family_1p0'):
        if joined[f'{field}_ep'].astype(str).ne(joined[f'{field}_dis'].astype(str)).any():
            raise ValueError(f'Episodio vs Discovery inconsistente en {field}')
    for field in ('zone_low','zone_high'):
        if not pd.Series([abs(a-b)<=max(1e-12,abs(b)*1e-8)
                          for a,b in zip(joined[f'{field}_ep'],joined[f'{field}_dis'])]).all():
            raise ValueError(f'Episodio vs Discovery inconsistente en {field}')
    if (amap.episode_id.isna().any() or amap.episode_ordinal.isna().any()
            or amap.episode_attempt_count.isna().any()):
        raise ValueError('Asignación incompleta de episodios')
    ordered=amap.sort_values(['episode_id','episode_ordinal'])
    group=ordered.groupby('episode_id',sort=False)
    sizes=group.size()
    if (ordered.episode_ordinal.ne(group.cumcount()+1).any()
            or ordered.episode_attempt_count.ne(ordered.episode_id.map(sizes)).any()
            or ordered.is_first_in_episode.ne(ordered.episode_ordinal.eq(1).astype(int)).any()
            or len(sizes)!=len(eps)):
        raise ValueError('Orden/contadores de episodios inconsistentes')
    summary=group.agg(symbol=('symbol','first'),levels=('level_id','nunique'),
                      symbols=('symbol','nunique'),level_id=('level_id','first'),
                      first_attempt_id=('attempt_id','first'),
                      first_pre_ts=('pre_closed_ts','first'),
                      last_pre_ts=('pre_closed_ts','last'),
                      first_family_1p0=('family_1p0','first'),
                      distinct_families_1p0=('family_1p0','nunique'),
                      zone_lows=('zone_low','nunique'),zone_highs=('zone_high','nunique'))
    if (summary.levels.ne(1).any() or summary.symbols.ne(1).any()
            or summary.zone_lows.ne(1).any() or summary.zone_highs.ne(1).any()
            or group.pre_closed_ts.apply(lambda col: (col.diff().dropna()>24*TF).any()).any()
            or (summary.last_pre_ts-summary.first_pre_ts).ge(96*TF).any()):
        raise ValueError('Un episodio fusiona niveles/zonas o supera 24/96 velas')
    e=eps.set_index('episode_id')
    if not e.index.equals(summary.index):
        e=e.reindex(summary.index)
    if e.isna().all(axis=1).any():
        raise ValueError('Faltan IDs de episodios')
    for field in ('symbol','level_id','first_attempt_id','first_pre_ts',
                  'last_pre_ts','first_family_1p0','distinct_families_1p0'):
        if e[field].astype(str).ne(summary[field].astype(str)).any():
            raise ValueError(f'Resumen de episodios inconsistente: {field}')
    if (e.attempts.ne(e.index.map(sizes)).any()
            or e.span_bars.ne((summary.last_pre_ts-summary.first_pre_ts)//TF).any()
            or e.family_changes_within_episode.ne(summary.distinct_families_1p0.gt(1).astype(int)).any()
            or ordered.episode_first_attempt_id.ne(ordered.episode_id.map(summary.first_attempt_id)).any()
            or ordered.episode_anchor_pre_ts.ne(ordered.episode_id.map(summary.first_pre_ts)).any()):
        raise ValueError('Mapa de episodios y resumen no coinciden')
    if (near.attempt_id.duplicated().any() or len(near)!=manifest.get('attempts_with_prior_similar_zone_other_level',len(near))
            or not near.attempt_id.isin(amap.attempt_id).all()):
        raise ValueError('Alertas de niveles cercanos inconsistentes')
    if not near.empty and (near.level_id.eq(near.other_level_id).any()
                           or near.pre_closed_ts.le(near.other_pre_closed_ts).any()
                           or near.interpretation.ne('POSIBLE_ZONA_EQUIVALENTE_NO_FUSIONADA').any()):
        raise ValueError('Alerta de nivel distinto inválida')
    return amap,eps,near,manifest


def episode_catalog(discovery: pd.DataFrame, amap: pd.DataFrame, episodes: pd.DataFrame, scale_field: str):
    """Build catalog using the first attempt, regardless of later favorable families."""
    first=amap.loc[amap.episode_ordinal.eq(1),['episode_id','attempt_id','symbol','level_id','pre_closed_ts',
                        'other_levels_same_candle','candidate_other_level_prior_count']].copy()
    first=first.rename(columns={'attempt_id':'first_attempt_id',
                   'pre_closed_ts':'representative_pre_ts',
                   'other_levels_same_candle':'first_other_levels_same_candle',
                   'candidate_other_level_prior_count':'first_nearby_levels'})
    metric_cols=[c for c in ('touches_all_pre288','touches_below_pre288','touches_below_pre96',
                    'touch_span_pre288_bars','touch_gap_median_pre288_bars',
                    'touch_span_all_pre_bars',
                    'compression_24_recent_vs_early','compression_48_recent_vs_first',
                    'amplitude_24_recent_atr','compression_72_recent_vs_first_288',
                    'trough_net_progress_last3_atr',
                    'near_zone_share_48_half_atr','near_zone_share_288_half_atr',
                    'max_low_below_zone_48_atr','status_288') if c in discovery]
    d=discovery[['attempt_id',scale_field,'scale_agreement','status']+metric_cols].rename(columns={
        'attempt_id':'first_attempt_id',scale_field:'representative_family',
        'scale_agreement':'representative_agreement','status':'representative_status'})
    catalog=episodes.merge(first,on=['episode_id','first_attempt_id','symbol','level_id'],validate='one_to_one')
    catalog=catalog.merge(d,on='first_attempt_id',validate='one_to_one')
    if len(catalog)!=len(episodes):
        raise ValueError('Faltan representantes de episodios')
    return catalog


def filter_episodes(catalog, discovery, amap, scale_field, *, family='TODAS',
                    symbol='TODOS', agree='TODOS', family_match='PRIMERO', change='TODOS'):
    """The default is causal first attempt. ANY is retrospective and explicitly labeled."""
    result=catalog
    if symbol!='TODOS':result=result[result.symbol.eq(symbol)]
    if agree!='TODOS':result=result[result.representative_agreement.eq(agree)]
    if family!='TODAS':
        if family_match=='PRIMERO':
            result=result[result.representative_family.eq(family)]
        elif family_match=='CUALQUIERA':
            matched=amap[['episode_id','attempt_id']].merge(discovery[['attempt_id',scale_field]],
                         on='attempt_id',validate='one_to_one')
            ids=matched.loc[matched[scale_field].eq(family),'episode_id'].unique()
            result=result[result.episode_id.isin(ids)]
        else:raise ValueError('Modo de coincidencia de familias inválido')
    if change=='CAMBIA':result=result[result.family_changes_within_episode.eq(1)]
    elif change=='ESTABLE':result=result[result.family_changes_within_episode.eq(0)]
    elif change!='TODOS':raise ValueError('Filtro de cambios inválido')
    return result

def drawable_bars(candles,pre,pre_count,post_count):
    """Trailing PRE must be contiguous; any unavailable tail is explicit."""
    import hll_structural_approach_ohlc_pre as ohlc
    if pre_count not in (48,96,144,192,288,384) or post_count not in (0,12,24,48,72,96):
        raise ValueError('Ventana visual no permitida')
    before=[]
    for ts in range(pre-TF,pre-pre_count*TF-1,-TF):
        if ts not in candles:break
        before.append(candles[ts])
    before.reverse()
    if not before or before[-1]['close_ts']!=pre:
        raise ValueError('No hay ninguna vela cerrada justo antes de PRE')
    future=[]
    for ts in range(pre,pre+post_count*TF,TF):
        if ts not in candles:break
        future.append(candles[ts])
    return before,future


def chart_for_attempt(r,candles,scale,pre_bars,post_bars,show_pivots):
    import plotly.graph_objects as go
    import hll_phase_reconstruction_pre as phase
    pre=int(r['pre_closed_ts'])
    before,future=drawable_bars(candles,pre,pre_bars,post_bars)
    display=before+future
    def ts(b):return pd.Timestamp(b['open_ts'],unit='ms',tz='UTC')
    fig=go.Figure(go.Candlestick(x=[ts(b) for b in display],
             open=[b['open'] for b in display],high=[b['high'] for b in display],
             low=[b['low'] for b in display],close=[b['close'] for b in display],
             name='30m',increasing_line_color='#169977',decreasing_line_color='#d45b67'))
    lo=float(r['zone_low']);hi=float(r['zone_high'])
    fig.add_hrect(y0=lo,y1=hi,opacity=.16,fillcolor='#d5a24d',line_width=0)
    for price in (lo,hi):fig.add_hline(y=price,line_dash='dash',line_color='#bd9248')
    fig.add_vline(x=pd.Timestamp(pre,unit='ms',tz='UTC'),line_dash='dash',line_color='#975ad0')
    if len(before)>96:
        fig.add_vline(x=pd.Timestamp(pre-96*TF,unit='ms',tz='UTC'),line_dash='dot',line_color='#8b99a7')
    if show_pivots and r['status']=='COMPLETE_PRE':
        core=bar_window(candles,pre,96)
        if core is None:raise ValueError('Faltan las 96 velas del overlay PRE')
        m={k:(int(r[k]) if k=='pre_closed_ts' else float(r[k]) if k in ('zone_low','zone_high','atr_pre') else str(r[k]))
           for k in ('attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high','atr_pre')}
        _,pivots,legs,_=phase.describe_attempt(core,m,scale)
        for kind,col in [('LOW','#19aa80'),('HIGH','#d7a045')]:
            ps=[p for p in pivots if p['pivot_type']==kind and int(p['confirmed_known_closed_ts'])<=pre]
            if ps:
                fig.add_trace(go.Scatter(x=[pd.Timestamp(int(p['pivot_open_ts']),unit='ms',tz='UTC') for p in ps],
                         y=[float(p['pivot_price_close']) for p in ps],mode='markers',
                         marker=dict(size=8,color=col,symbol='triangle-up' if kind=='LOW' else 'triangle-down'),
                         name=f'Pivote {kind} confirmado PRE',
                         text=[f"Conocido: {pd.Timestamp(int(p['confirmed_known_closed_ts']),unit='ms',tz='UTC')}" for p in ps],
                         hovertemplate='%{text}<br>Precio: %{y}<extra></extra>'))
        for leg in legs:
            confirmed=leg['status']=='CONFIRMED_PRE'
            fig.add_trace(go.Scatter(x=[pd.Timestamp(int(leg['start_pivot_open_ts']),unit='ms',tz='UTC'),
                pd.Timestamp(int(leg['end_extreme_open_ts']),unit='ms',tz='UTC')],
                y=[float(leg['start_pivot_price_close']),float(leg['end_extreme_price_close'])],
                mode='lines',line=dict(color='#28b084' if leg['leg_direction']=='UP' else '#e6a753',
                                       dash='solid' if confirmed else 'dot',width=1.7),
                name='Fase '+leg['leg_direction']+' '+('confirmada' if confirmed else 'provisional'),showlegend=False))
    fig.update_layout(height=580,xaxis_rangeslider_visible=False,
            margin=dict(l=8,r=8,t=18,b=20),xaxis_title='UTC',yaxis_title='Precio')
    return fig,len(before),len(future)



def render_hll_structural_discovery(root):
    import streamlit as st
    import hll_structural_approach_ohlc_pre as ohlc
    root=Path(root)
    folder=root/OUTPUT_PARENT/'full290_v1'
    pilot_folders=sorted((root/OUTPUT_PARENT).glob('pilot_first_*_v1')) if (root/OUTPUT_PARENT).is_dir() else []
    folders=([folder] if (folder/'manifest.json').is_file() else []) + [p for p in pilot_folders if (p/'manifest.json').is_file()]
    st.markdown('#### 🔎 Discovery estructural PRE · 290 símbolos')
    st.caption('Universo condicionado a CROSS_UP; clasificaciones morfológicas, NO setups confirmados ni predicción de ruptura.')
    if not folders:
        st.info('Sin corrida Discovery. Ejecutá primero `python3 hll_structural_discovery_v1.py --plan` y un piloto `--run --max-symbols 5`.')
        return
    selected=st.selectbox('Corrida Discovery',folders,format_func=lambda p:p.name,key='hll_discovery_run')
    @st.cache_data(show_spinner=False)
    def cached_read(path,mtime):
        return load_discovery(Path(path))
    try:
        payload=cached_read(str(selected),(selected/'attempts_pre.csv').stat().st_mtime_ns)
        if payload is None:raise ValueError('Archivos de corrida incompletos')
        data,manifest=payload
    except (ValueError,OSError,KeyError) as exc:
        st.error(f'No se pudo cargar Discovery: {exc}');return
    c1,c2,c3=st.columns(3)
    c1.metric('Intentos totales',len(data))
    c2.metric('Con PRE 96 completo',int(data.status.eq('COMPLETE_PRE').sum()))
    c3.metric('Sin PRE 96 completo',int(data.status.ne('COMPLETE_PRE').sum()))

    overlap_dir=root/OVERLAP_PARENT
    overlap=None
    if selected.name=='full290_v1' and (overlap_dir/'manifest.json').is_file():
        @st.cache_data(show_spinner=False)
        def cached_overlap(path,mtimes,discovery_mtime):
            return load_overlap(Path(path),data,manifest)
        file_names=['manifest.json','attempt_episode_map.csv','episode_candidates.csv',
                    'nearby_other_levels_review.csv','level_reuse.csv','symbol_summary.csv']
        try:
            mtimes=tuple((overlap_dir/x).stat().st_mtime_ns for x in file_names)
            overlap=cached_overlap(str(overlap_dir),mtimes,(selected/'attempts_pre.csv').stat().st_mtime_ns)
        except (OSError,ValueError,KeyError) as exc:
            st.error(f'No se puede habilitar el modo episodios: {exc}')
            st.caption('El modo intentos original continúa disponible. No se utilizaron datos de solapamiento inválidos.')
    elif selected.name=='full290_v1':
        st.info('Todavía no está el reporte de solapamiento; por ahora se conserva el modo intentos.')
    amap=eps=near=overlap_manifest=None
    if overlap is not None:
        amap,eps,near,overlap_manifest=overlap
        e1,e2,e3=st.columns(3)
        e1.metric('Episodios candidatos (24/96)',len(eps))
        e2.metric('Intentos posteriores agrupados',len(amap)-len(eps))
        e3.metric('Episodios con familias cambiantes (1 ATR)',int(eps.family_changes_within_episode.sum()))
        st.caption('Agrupación heurística por mismo level_id: separación máxima de 24 velas y extensión menor de 96. '
                   'Otros niveles cercanos NO se fusionan; ningún CROSS_UP se elimina.')
    touch_path=root/TOUCHES_SOURCE
    touch_ready=False
    if touch_path.is_file():
        @st.cache_data(show_spinner=False)
        def cached_touches(path,mtime,discovery_mtime):
            source=pd.read_csv(path,usecols=['symbol','level_id','test_id','touch_closed_ts','side'],
                               low_memory=False)
            return compute_pre_touch_metrics(data,source)
        try:
            metrics=cached_touches(str(touch_path),touch_path.stat().st_mtime_ns,
                                  (selected/'attempts_pre.csv').stat().st_mtime_ns)
            data=data.merge(metrics,on='attempt_id',how='left',validate='one_to_one')
            if data.touches_all_pre288.isna().any():
                raise ValueError('Métricas de contactos PRE incompletas')
            touch_ready=True
        except (OSError,ValueError,KeyError,TypeError) as exc:
            st.warning(f'No se pudo cargar la medición de contactos V2.6: {exc}')
    else:
        st.caption('No se encontró touches.csv de V2.6: ordenamientos por contactos no disponibles.')
    maturity_ready=False
    maturity_dir=root/MATURITY_PARENT
    if selected.name=='full290_v1' and overlap_manifest is not None and (maturity_dir/'manifest.json').is_file():
        @st.cache_data(show_spinner=False)
        def cached_maturity(path,mtimes,discovery_mtime,overlap_mtime):
            return load_maturity(Path(path),data,manifest,overlap_manifest)
        try:
            names=['manifest.json','attempt_maturity_pre.csv','episode_first_maturity_pre.csv']
            mtimes=tuple((maturity_dir/x).stat().st_mtime_ns for x in names)
            mdata=cached_maturity(str(maturity_dir),mtimes,
                (selected/'attempts_pre.csv').stat().st_mtime_ns,
                (overlap_dir/'manifest.json').stat().st_mtime_ns)
            if mdata is None:raise ValueError('Faltan archivos de madurez')
            data=data.merge(mdata,on='attempt_id',how='left',validate='one_to_one')
            if data.status_96.isna().any():raise ValueError('Faltan intentos de madurez')
            maturity_ready=True
        except (OSError,ValueError,KeyError,TypeError) as exc:
            st.warning(f'No se pudieron validar las métricas PRE de compresión: {exc}')
    elif selected.name=='full290_v1':
        st.caption('Para ver compresión/persistencia ejecutá hll_pre_maturity_metrics_v1.py --run (solo lectura).')
    units=['Episodios (mismo nivel, 24/96)','Intentos CROSS_UP'] if overlap is not None else ['Intentos CROSS_UP']
    unit=st.radio('Unidad de análisis',units,horizontal=True,key='hll_discovery_unit')
    in_episodes=unit.startswith('Episodios')

    scale=st.selectbox('Sensibilidad ATR',[0.5,1.0,1.5],index=1,key='hll_discovery_atr')
    field={0.5:'family_0p5',1.0:'family_1p0',1.5:'family_1p5'}[scale]
    names=sorted(data[field].dropna().astype(str).unique().tolist())
    c4,c5,c6=st.columns(3)
    with c4:fam=st.selectbox('Familia',['TODAS']+names,format_func=lambda x:FAMILY_LABELS.get(x,x),key='hll_discovery_family')
    with c5:sym=st.selectbox('Símbolo',['TODOS']+sorted(data.symbol.unique()),key='hll_discovery_sym')
    with c6:agree=st.selectbox('Acuerdo ATR',['TODOS','IGUAL_3_ATR','DISCREPA_ATR','SIN_DATOS'],key='hll_discovery_agree')

    # In episode mode, show CROSS_UP count sorts first; touch-based sorts remain available.
    order_options=(list(EPISODE_SORTS)+['Fecha PRE (actual)'] if in_episodes
                   else ['Fecha PRE (actual)'])
    if touch_ready:
        order_options+=list(TOUCH_SORTS)
    if maturity_ready:
        order_options+=list(MATURITY_SORTS)
    order=st.selectbox('Ordenar por · intentos / madurez / contactos',order_options,
                       key='hll_discovery_maturity_sort')
    if order in EPISODE_SORTS:
        st.caption('Orden retrospectivo: usa todos los CROSS_UP del episodio, incluidos los posteriores al primer corte. NO es una métrica causal al inicio.')
    elif order in TOUCH_SORTS:
        st.caption('Contactos independientes registrados por V2.6, hasta el corte PRE seleccionado (incluido), sin POST. '
                   'En modo episodios se miden SOLO al primer CROSS_UP; para comparar cortes elegí cada intento.')
    if order in MATURITY_SORTS:
        st.caption('Orden descriptivo usando exclusivamente velas cerradas PRE. '
                   'En episodios se usa el primer corte; una compresión alta o baja no garantiza calidad. '
                   'Los casos sin 288 velas completas quedan sin ese dato.')
    if in_episodes:
        s1,s2=st.columns(2)
        with s1:
            matching=st.selectbox('Familia del episodio según',
                 ['Primer intento (causal)','Cualquier intento (retrospectivo)'],
                 key='hll_discovery_episode_family_match')
        with s2:
            changes=st.selectbox('Evolución de familia a 1 ATR',
                 ['Todos','Cambia entre intentos','No cambia'],
                 key='hll_discovery_episode_changes')
        match='PRIMERO' if matching.startswith('Primer') else 'CUALQUIERA'
        change={'Todos':'TODOS','Cambia entre intentos':'CAMBIA','No cambia':'ESTABLE'}[changes]
        catalog=episode_catalog(data,amap,eps,field)
        # Full-episode attempt counts are known retrospectively. Do not present
        # them as information available at the first CROSS_UP cutoff.
        max_episode_attempts=int(catalog.attempts.max())
        a1,a2=st.columns(2)
        with a1:
            min_attempts=int(st.number_input('Mínimo de intentos CROSS_UP por episodio',
                min_value=1,max_value=max_episode_attempts,value=1,step=1,
                key='hll_discovery_min_episode_attempts'))
        with a2:
            max_attempts=int(st.number_input('Máximo de intentos CROSS_UP por episodio',
                min_value=1,max_value=max_episode_attempts,value=max_episode_attempts,step=1,
                key='hll_discovery_max_episode_attempts'))
        st.caption('Cantidad de CROSS_UP del mismo episodio, NO toques independientes. '
                   'Filtro retrospectivo: incluye intentos posteriores al primer corte PRE; '
                   'sirve para explorar el histórico, no para predecir un primer breakout.')
        filtered=filter_episodes(catalog,data,amap,field,family=fam,symbol=sym,agree=agree,
                                 family_match=match,change=change)
        if min_attempts > max_attempts:
            st.warning('El mínimo de intentos supera al máximo. Ajustá el rango.')
            return
        filtered=filter_episodes_by_attempt_count(filtered,min_attempts,max_attempts)
        st.caption(f'{len(filtered)} episodios candidatos con estos filtros. '
            'Familia y acuerdo ATR del representante = primer CROSS_UP, no el mejor resultado.'
            + (' Buscar cualquier intento usa información posterior al inicio del episodio y es retrospectivo.'
               if match=='CUALQUIERA' else ''))
        if filtered.empty:return
        filtered=sort_discovery_rows(filtered,order,episodic=True)
        pages=max(1,math.ceil(len(filtered)/80))
        page=st.number_input('Página de episodios',min_value=1,max_value=pages,value=1,step=1,key='hll_discovery_episode_page')
        shown=filtered.iloc[(page-1)*80:page*80]
        columns=['symbol','episode_id','level_id','first_pre_ts','last_pre_ts','attempts',
                 'touches_below_pre288','touches_below_pre96','touch_span_pre288_bars',
                 'touch_gap_median_pre288_bars',
                 'compression_24_recent_vs_early','compression_72_recent_vs_first_288',
                 'trough_net_progress_last3_atr','near_zone_share_48_half_atr','status_288',
                 'representative_family','representative_agreement','family_changes_within_episode',
                 'representative_status']
        st.dataframe(shown[[c for c in columns if c in shown]],hide_index=True,use_container_width=True)
        chosen=st.selectbox('Inspeccionar episodio de esta página',shown.episode_id.tolist(),
                 format_func=lambda eid:(f'{shown.loc[shown.episode_id.eq(eid),"symbol"].iloc[0]}'
                     f' · {eid} · {int(shown.loc[shown.episode_id.eq(eid),"attempts"].iloc[0])} intentos'),
                 key='hll_discovery_episode_id')
        ep=shown.loc[shown.episode_id.eq(chosen)].iloc[0]
        members=amap.loc[amap.episode_id.eq(chosen)].sort_values('episode_ordinal').copy()
        member_extra=([k for k in ('compression_24_recent_vs_early',
                 'compression_72_recent_vs_first_288','trough_net_progress_last3_atr',
                 'near_zone_share_48_half_atr','status_288') if k in data]
                 if maturity_ready else [])
        member_view=members[['attempt_id','episode_ordinal','pre_closed_ts',
                             'status','other_levels_same_candle',
                             'candidate_other_level_prior_count']].merge(
                    data[['attempt_id',field,'scale_agreement']+member_extra],
                    on='attempt_id',validate='one_to_one')
        st.markdown('**Evolución del episodio · cortes PRE consecutivos**')
        st.caption('La tabla incluye todos los intentos del episodio y permite estudiar su evolución histórica; '
                   'no representa información disponible en el primer corte.')
        member_view['fecha_pre_utc']=pd.to_datetime(member_view.pre_closed_ts,unit='ms',utc=True)
        st.dataframe(member_view,hide_index=True,use_container_width=True)
        if maturity_ready:
            st.caption('Las métricas de cada fila se calcularon con el PRE de ESE intento. '
                       'Ver filas futuras del episodio es un análisis retrospectivo, '
                       'no información disponible en su primer corte.')
        opts=members.attempt_id.tolist()
        attempt=st.selectbox('Inspeccionar corte PRE dentro del episodio',opts,
                 format_func=lambda aid:(f'#{int(members.loc[members.attempt_id.eq(aid),"episode_ordinal"].iloc[0])}'
                    f' · {pd.Timestamp(int(members.loc[members.attempt_id.eq(aid),"pre_closed_ts"].iloc[0]),unit="ms",tz="UTC")}'
                    f' · {aid[:12]}'),key='hll_discovery_episode_attempt')
        r=members.loc[members.attempt_id.eq(attempt)].iloc[0]
        row=data.loc[data.attempt_id.eq(attempt)].iloc[0]
        past_cuts=members.loc[members.pre_closed_ts.lt(int(row.pre_closed_ts)),'pre_closed_ts'].tolist()
        st.caption(f'Episodio {ep.episode_id} · nivel {ep.level_id} · '
                   f'{len(members)} intentos del mismo nivel · representante: {ep.first_attempt_id}.'
                   ' Los cortes futuros no se dibujan en el gráfico PRE actual.')
        matching_edges=near.loc[near.attempt_id.isin(opts)]
        with st.expander('⚠️ Posibles zonas de otros niveles (no fusionadas)',expanded=False):
            st.caption('Coincidencias de nivel distinto, revisadas hacia atrás en el tiempo. '
                       'No implican por sí solas la misma consolidación.')
            st.metric('Alertas en intentos de este episodio',len(matching_edges))
            st.metric('Otros niveles en el mismo corte del intento elegido',int(r.other_levels_same_candle))
            if not matching_edges.empty:
                st.dataframe(matching_edges[['attempt_id','other_attempt_id','other_level_id',
                      'delta_bars','center_distance_atr','zone_gap_atr']],
                      hide_index=True,use_container_width=True)
    else:
        filtered=data
        if fam!='TODAS':filtered=filtered[filtered[field].eq(fam)]
        if sym!='TODOS':filtered=filtered[filtered.symbol.eq(sym)]
        if agree!='TODOS':filtered=filtered[filtered.scale_agreement.eq(agree)]
        st.caption(f'{len(filtered)} intentos con estos filtros. Ninguno se selecciona por POST ni PnL.')
        if filtered.empty:return
        filtered=sort_discovery_rows(filtered,order,episodic=False)
        pages=max(1,math.ceil(len(filtered)/80))
        page=st.number_input('Página de intentos',min_value=1,max_value=pages,value=1,step=1,key='hll_discovery_page')
        shown=filtered.iloc[(page-1)*80:page*80]
        display_cols=['symbol','attempt_id','pre_closed_ts','touches_below_pre288',
                      'touches_below_pre96','touch_span_pre288_bars','touch_gap_median_pre288_bars',
                      field,'scale_agreement','original_pre_pattern_48',
                      'intercontact_48','latest_recovery_retention_1p0','preserved_fraction_last3_1p0',
                      'compression_24_recent_vs_early','trough_net_progress_last3_atr',
                      'near_zone_share_48_half_atr','status_288','status']
        st.dataframe(shown[[x for x in display_cols if x in shown]],hide_index=True,use_container_width=True)
        opts=shown.attempt_id.tolist()
        attempt=st.selectbox('Inspeccionar intento de esta página',opts,
                 format_func=lambda aid:f'{shown.loc[shown.attempt_id.eq(aid),"symbol"].iloc[0]} · {aid}',
                 key='hll_discovery_attempt')
        row=shown.loc[shown.attempt_id.eq(attempt)].iloc[0]
        past_cuts=[]
        if overlap is not None:
            member=amap.loc[amap.attempt_id.eq(attempt)].iloc[0]
            st.caption(f'Episodio asociado: {member.episode_id} · intento '
                       f'{int(member.episode_ordinal)}/{int(member.episode_attempt_count)} del mismo nivel. '
                       'Para navegar entre intentos relacionados, elegí el modo Episodios.')
    if touch_ready:
        cols=st.columns(4)
        cols[0].metric('Contactos PRE desde abajo (288 velas)',int(row.touches_below_pre288))
        cols[1].metric('Contactos PRE desde abajo (96 velas)',int(row.touches_below_pre96))
        cols[2].metric('Lapso PRE desde abajo (288 velas)',
            f'{row.touch_span_pre288_bars/48:.1f} días' if pd.notna(row.touch_span_pre288_bars) else 'Sin 2 contactos')
        cols[3].metric('Mediana entre toques desde abajo',
            f'{row.touch_gap_median_pre288_bars:.0f} velas' if pd.notna(row.touch_gap_median_pre288_bars) else 'Sin 2 contactos')
        st.caption('Estos son contactos independientes PRE de V2.6, no intentos CROSS_UP. '
                   'No representan por sí solos calidad ni un patrón de compresión.')
    if maturity_ready:
        with st.expander('📏 Compresión, progreso y persistencia PRE · descriptivo',expanded=False):
            def metric_fmt(name, percent=False):
                v=row.get(name)
                if pd.isna(v):return 'Sin datos'
                return f'{100*float(v):.1f}%' if percent else f'{float(v):.2f}'
            a,b,c,d=st.columns(4)
            a.metric('Amplitud últimas 24 (ATR)',metric_fmt('amplitude_24_recent_atr'))
            b.metric('Compresión 24 / inicio 96',metric_fmt('compression_24_recent_vs_early'))
            c.metric('Progreso mínimos (ATR)',metric_fmt('trough_net_progress_last3_atr'))
            d.metric('Cerca de resistencia · 48',metric_fmt('near_zone_share_48_half_atr',True))
            st.caption('Amplitud robusta = [P90(high)-P10(low)] / ATR_PRE. '
                       'Compresión = amplitud últimas 24 / primeras 24 velas de las 96 PRE; '
                       'menor que 1 indica contracción, no necesariamente una estructura mejor. '
                       'Progreso de mínimos = diferencia entre cuantiles P10(low) de bloques de 24; '
                       'es un proxy, NO pivotes confirmados.')
            st.caption(f'Contexto 288: {row.get("status_288", "Sin datos")} · '
                       f'Persistencia cerca del nivel (288): {metric_fmt("near_zone_share_288_half_atr",True)} · '
                       f'Retroceso extremo debajo de resistencia (48, ATR): {metric_fmt("max_low_below_zone_48_atr")}. '
                       'Cerca del nivel significa cierres entre zone_low−0,5 ATR y zone_high; '
                       'el tiempo por encima se mide por separado. Las métricas usan ATR_PRE fijo y no cambian al elegir otra sensibilidad de la familia.')
            vals=[row.get(k) for k in ['amplitude_24_early_atr',
                 'amplitude_24_middle1_atr','amplitude_24_middle2_atr','amplitude_24_recent_atr']]
            if all(pd.notna(v) for v in vals):
                amp_data=pd.DataFrame({'Amplitud / ATR':[float(v) for v in vals]},
                    index=['96–73 PRE','72–49 PRE','48–25 PRE','24–1 PRE'])
                st.bar_chart(amp_data,use_container_width=True)
            st.caption('Todas las métricas se calculan hasta el corte PRE seleccionado. '
                       'No se usan intentos futuros, etiquetas visuales ni resultados POST.')
    if row['status']!='COMPLETE_PRE':
        st.warning(f'PRE no evaluable: {row["reason"]}. No se fabricó clasificación.')
        return
    pre_bars=st.selectbox('Velas PRE para visualizar',[48,96,144,192,288,384],index=4,key='hll_discovery_pre_count')
    show_post=st.checkbox('Revelar POST (solo visual, no afecta familias)',value=False,key='hll_discovery_post')
    post_bars=st.selectbox('Velas POST',[12,24,48,72,96],index=4,key='hll_discovery_post_count',disabled=not show_post) if show_post else 0
    pivots=st.checkbox('Mostrar pivotes y fases PRE',value=True,key='hll_discovery_pivots')
    candle=root/CANDLES/f'{row.symbol}_30m.csv'
    @st.cache_data(show_spinner=False)
    def cached_candles(path,mtime):
        return ohlc.load_candles(Path(path),timestamp_kind='open')[0]
    if not candle.is_file():st.warning(f'Falta histórico para {row.symbol}');return
    try:
        candles=cached_candles(str(candle),candle.stat().st_mtime_ns)
        fig,actual_pre,actual_post=chart_for_attempt(row,candles,scale,pre_bars,post_bars,pivots)
        if in_episodes:
            # Display only attempt cutoffs known before this selected PRE cutoff.
            earliest=int(row.pre_closed_ts)-actual_pre*TF
            for cut in past_cuts:
                if earliest<=cut<int(row.pre_closed_ts):
                    fig.add_vline(x=pd.Timestamp(int(cut),unit='ms',tz='UTC'),
                                  line_dash='dot',line_width=1,line_color='#5e94c9')
        st.plotly_chart(fig,use_container_width=True,key='hll_discovery_chart')
        st.caption(f'PRE real mostrado: {actual_pre}/{pre_bars} velas · POST real mostrado: {actual_post}/{post_bars if show_post else 0}. '
                   'Fases calculadas exclusivamente con 96 velas PRE.'
                   + (' Líneas azules: cruces anteriores del mismo episodio.' if in_episodes else ''))
        if in_episodes:
            # Optional, read-only dynamic fingerprint. Uses the SAME selected
            # Attempt ID, the validated episode membership and original candles.
            # The engine truncates features to the chosen cutoff in causal mode.
            from dashboard.views.hll_fingerprint_evolution_view import render_fingerprint_evolution
            render_fingerprint_evolution(members, attempt, candles, scale)
    except (ValueError,TypeError,KeyError) as exc:
        st.error(f'No se pudo dibujar el episodio: {exc}');return
    st.markdown(f'**Familia {scale:g} ATR:** {FAMILY_LABELS.get(str(row[field]),str(row[field]))}')
    suffix={0.5:'0p5',1.0:'1p0',1.5:'1p5'}[scale]
    for name,col in [('A favor',f'evidence_{suffix}'),('En contra',f'contradictions_{suffix}'),('Limitaciones',f'limitations_{suffix}')]:
        value=row.get(col)
        st.caption(f'{name}: {value if pd.notna(value) and str(value).strip() else "Sin observaciones específicas"}')
    if scale!=1.0:st.caption('La tabla principal muestra métricas de continuidad a 1 ATR; los argumentos de familia sí corresponden a la escala elegida.')
    st.caption('Una familia positiva no implica calidad, ruptura futura, entrada real ni rentabilidad.')
