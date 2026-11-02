#!/usr/bin/env python3
"""HLL - Huella Estructural PRE V1 (offline, 30m, sin puntuacion).

Un vector descriptivo POR Attempt ID y corte PRE. No utiliza POST, notas humanas,
retorno futuro ni cantidad retrospectiva de intentos. Separa seis familias. Las
huellas pueden compararse mas adelante mediante un buscador universal, pero
este modulo NO clasifica ni recomienda operaciones.

Reutiliza las ventanas y pivotes CLOSE confirmados de HLL Anatomy PRE V1.
Primera corrida: 15 episodios, 40 cortes (la linea de base #1 es adicional a
algunas referencias visuales que se evaluaron en cortes posteriores).

 python3 hll_structural_fingerprint_v1.py --plan
 python3 hll_structural_fingerprint_v1.py --run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

import hll_structure_anatomy_pre_v1 as anatomy
import hll_structure_anatomy_first_pre_v1 as baseline

VERSION = 'HLL_STRUCTURAL_FINGERPRINT_PRE_V1'
DEFAULT_ZIP = Path('HLL_FUENTES_ANATOMIA_PRE_V2.zip')
DEFAULT_MAP = Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv')
DEFAULT_OUTPUT = Path('reports/horizontal_level_structural_fingerprint_v1/reference_15_v1')

FAMILIES = {
    'geometry': 'Swings y geometria',
    'reconstruction': 'Reconstruccion',
    'compression': 'Compresion',
    'resistance': 'Relacion con resistencia',
    'maturity': 'Madurez temporal',
    'movement': 'Calidad del movimiento (descriptiva)',
}

# id -> (familia, unidad, definicion exacta, ventana, estado/metodologia)
FEATURES = {
    'geo_low_up_share_96': ('geometry', 'fraccion', 'Proporcion de cambios positivos entre LOW confirmados consecutivos en PRE 96; minimo 2 LOW.', 96, 'derived_confirmed_pivots'),
    'geo_high_up_share_96': ('geometry', 'fraccion', 'Proporcion de cambios positivos entre HIGH confirmados consecutivos en PRE 96; minimo 2 HIGH.', 96, 'derived_confirmed_pivots'),
    'geo_low_progress_last3_atr_96': ('geometry', 'ATR', '(Ultimo LOW menos antepenultimo LOW)/ATR del corte; requiere 3 LOW confirmados.', 96, 'reused_anatomy'),
    'geo_high_progress_last3_atr_96': ('geometry', 'ATR', '(Ultimo HIGH menos antepenultimo HIGH)/ATR del corte; requiere 3 HIGH confirmados.', 96, 'reused_anatomy'),
    'geo_confirmed_pivots_96': ('geometry', 'cantidad', 'Numero de pivotes confirmados por reversion de cierre en 96 velas.', 96, 'reused_anatomy'),
    'geo_leg_amplitude_cv_96': ('geometry', 'ratio', 'Desvio poblacional/amplitud media ATR de tramos entre pivotes confirmados; minimo 3 tramos.', 96, 'derived_confirmed_legs'),
    'rebuild_last_low_step_atr_96': ('reconstruction', 'ATR', 'LOW2 menos LOW1 en ultima cadena L-H-L-H plenamente confirmada, en ATR.', 96, 'reused_anatomy'),
    'rebuild_last_high_step_atr_96': ('reconstruction', 'ATR', 'HIGH2 menos HIGH1 en ultima cadena L-H-L-H plenamente confirmada, en ATR.', 96, 'reused_anatomy'),
    'rebuild_last_correction_fraction_96': ('reconstruction', 'fraccion', '(H1-L2)/(H1-L1) de ultima cadena confirmada; puede exceder 1.', 96, 'derived_confirmed_chain'),
    'rebuild_second_over_first_rise_96': ('reconstruction', 'ratio', '(H2-L2)/(H1-L1) de ultima cadena confirmada; no implica ruptura.', 96, 'derived_confirmed_chain'),
    'rebuild_chain_count_96': ('reconstruction', 'cantidad', 'Cantidad de cadenas L-H-L-H completamente confirmadas dentro de 96 PRE.', 96, 'reused_anatomy'),
    'compress_residual_osc24_atr_96': ('compression', 'ATR', 'P90-P10 de residuos de cierres tras quitar deriva lineal por extremos en ultimas 24 velas; /ATR.', 96, 'reused_anatomy'),
    'compress_residual_ratio_24_96': ('compression', 'ratio', 'Oscilacion residual ultima 24 / oscilacion residual 24 anterior. No es rango total ni label de calidad.', 96, 'reused_anatomy'),
    'compress_downleg_ratio_new_old_96': ('compression', 'ratio', 'Mediana de ultimas 2 caidas confirmadas dividida por mediana de 2 caidas anteriores; requiere 4 DOWN.', 96, 'derived_confirmed_legs'),
    'compress_downleg_cv_96': ('compression', 'ratio', 'Coeficiente de variacion de amplitud de caidas confirmadas; requiere 3 DOWN.', 96, 'derived_confirmed_legs'),
    'level_close_distance_atr_96': ('resistance', 'ATR', '(borde inferior de resistencia - cierre PRE)/ATR; negativo si cierre esta por encima.', 96, 'reused_anatomy'),
    'level_near_share_48_96': ('resistance', 'fraccion', 'Fraccion de 48 cierres entre (zone_low-0.5 ATR) y zone_high.', 96, 'reused_anatomy'),
    'level_near_share_24_96': ('resistance', 'fraccion', 'Fraccion de ultimos 24 cierres entre (zone_low-0.5 ATR) y zone_high.', 96, 'derived_closed_candles'),
    'level_approach_rearm_count_96': ('resistance', 'cantidad', 'Entradas en banda cercana tras observar un cierre debajo de (zone_low-1 ATR); se rearma solo tras otra salida bajo -1 ATR. NO es Touch V2.6.', 96, 'experimental_heuristic'),
    'level_above_zone_share_48_96': ('resistance', 'fraccion', 'Fraccion de ultimos 48 cierres por encima de zone_high; no es confirmacion de flip.', 96, 'derived_closed_candles'),
    'level_zone_position_288': ('resistance', 'ratio', '(centro de zona - minimo OHLC 288)/(maximo OHLC 288-minimo OHLC 288); puede estar fuera [0,1]. NO mide fuerza del nivel.', 288, 'reused_anatomy'),
    'level_max_depth_below_48_atr_96': ('resistance', 'ATR', 'Maximo alejamiento por debajo de zone_low de LOW OHLC 48 en ATR.', 96, 'reused_anatomy'),
    'maturity_first_low_known_age_96': ('maturity', 'velas', 'Velas desde la CONFIRMACION del primer LOW que el reconstruidor de 96 detecta, hasta PRE. No es inicio real de formacion.', 96, 'experimental_window_bounded'),
    'maturity_first_low_known_age_288': ('maturity', 'velas', 'Velas desde la CONFIRMACION del primer LOW del reconstruidor independiente de 288. Puede diferir de PRE96.', 288, 'experimental_window_bounded'),
    'maturity_last_low_known_age_96': ('maturity', 'velas', 'Velas desde CONFIRMACION del ultimo LOW conocido en PRE96.', 96, 'reused_anatomy'),
    'maturity_last3_low_span_bars_96': ('maturity', 'velas', 'Barras transcurridas entre vela extrema del primero y tercero de los ultimos 3 LOW confirmados.', 96, 'reused_anatomy'),
    'maturity_near_tail_streak_bars_96': ('maturity', 'velas', 'Velas consecutivas al final de PRE con cierre en [zone_low-0.5 ATR, zone_high].', 96, 'derived_closed_candles'),
    'maturity_last_chain_age_bars_96': ('maturity', 'velas', 'Velas desde confirmacion de H2 en ultima cadena L-H-L-H confirmada; no incluye confirmaciones futuras.', 96, 'reused_anatomy'),
    'movement_signed_efficiency_24_96': ('movement', '[-1,1]', '(ultimo cierre-primer cierre)/suma de cambios absolutos, ultimas 24 velas. Nulo si no hubo cambios.', 96, 'reused_anatomy'),
    'movement_signed_efficiency_48_96': ('movement', '[-1,1]', '(ultimo cierre-primer cierre)/suma de cambios absolutos, ultimas 48 velas. Nulo si no hubo cambios.', 96, 'reused_anatomy'),
    'movement_net_advance_48_atr_96': ('movement', 'ATR', 'Ultimo cierre menos primer cierre de ultimas 48 velas, /ATR PRE.', 96, 'derived_closed_candles'),
    'movement_latest_lhl_retention_96': ('movement', 'fraccion', '(LOW posterior - LOW previo)/(HIGH - LOW previo) de ultimo L-H-L confirmado. Puede salir de [0,1].', 96, 'reused_anatomy'),
    'movement_median_lhl_retracement_96': ('movement', 'fraccion', 'Mediana de (HIGH - LOW posterior)/(HIGH - LOW anterior) para L-H-L confirmados.', 96, 'reused_anatomy'),
    'movement_max_close_drawdown_48_atr_96': ('movement', 'ATR', 'Maximo retroceso desde maximo acumulado de cierres durante ultimas 48 velas, /ATR.', 96, 'derived_closed_candles'),
    'movement_up_close_steps_48_96': ('movement', 'fraccion', 'Proporcion de incrementos positivos entre cierres consecutivos de ultimas 48 velas.', 96, 'derived_closed_candles'),
}

NUM_FEATURES = len(FEATURES)


def _number(x):
    if x is None or x == '':
        return None
    try:
        v = float(x)
    except (ValueError, TypeError):
        return None
    return v if math.isfinite(v) else None


def _safe_ratio(a, b):
    return a / b if b is not None and abs(b) > 1e-12 else None


def _cv(values):
    if len(values) < 3:
        return None
    mean = sum(values) / len(values)
    if abs(mean) <= 1e-12:
        return None
    var = sum((v - mean)**2 for v in values) / len(values)
    return (var**0.5) / mean


def _rising_share(pivots, kind):
    x = [p['pivot_price_close'] for p in pivots if p['pivot_type'] == kind]
    return _safe_ratio(sum(b > a for a, b in zip(x, x[1:])), len(x)-1) if len(x)>=2 else None


def _near(price, low, high, atr):
    return (low - .5*atr) <= price <= high


def _approach_runs(bars, low, high, atr):
    """Heuristic closed-price re-arm; not an independent HLL Touch V2.6."""
    armed = False
    count = 0
    for bar in bars:
        close = bar['close']
        if close < low-atr:
            armed = True
        elif armed and _near(close, low, high, atr):
            count += 1
            armed = False
    return count


def _drawdown(bars, atr):
    top = bars[0]['close']
    worst = 0.0
    for b in bars:
        top = max(top, b['close'])
        worst = max(worst, top - b['close'])
    return worst/atr


def _last_near_streak(bars, low, high, atr):
    n = 0
    for b in reversed(bars):
        if not _near(b['close'], low, high, atr):
            break
        n += 1
    return n


def _known_age(pre, pivots, kind):
    sel = [p for p in pivots if p['pivot_type'] == kind]
    return (pre-sel[0]['confirmed_known_closed_ts'])/anatomy.BAR_MS if sel else None


def _verified_input(meta, bars96, bars288):
    pre = int(meta['pre_closed_ts'])
    for n, bars in ((96,bars96),(288,bars288)):
        if bars is None or len(bars) != n:
            raise ValueError(f'PRE {n} incompleto; no crear huella falsa')
        for i, b in enumerate(bars):
            if b['open_ts'] != pre - (n-i)*anatomy.BAR_MS or b['close_ts'] != b['open_ts']+anatomy.BAR_MS:
                raise ValueError('Vela ausente, no contigua o POST')
            if not (0 < b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']):
                raise ValueError('OHLC mal formado')
    if bars288[-96:] != bars96:
        raise ValueError('Ventanas PRE 96 y 288 no representan las mismas velas finales')
    for k in ('atr_pre','zone_low','zone_high'):
        if _number(meta.get(k)) is None or _number(meta[k])<=0:
            raise ValueError('Metadatos de ATR/zona invalidos')
    if float(meta['zone_low']) > float(meta['zone_high']):
        raise ValueError('Zona invertida')
    if pre != bars96[-1]['close_ts']:
        raise ValueError('Corte PRE inconsistente')


def build_fingerprint(meta, bars96, bars288, scale_atr=1.0):
    """Api universal, por corte. Nunca recibe calificaciones, POST ni trayectoria futura."""
    if scale_atr not in (0.5, 1.0, 1.5):
        raise ValueError('Escala de pivotes debe ser 0.5, 1 o 1.5 ATR')
    _verified_input(meta, bars96, bars288)
    s96, p96, leg96, chain96 = anatomy.audit_snapshot(bars96,meta,scale_atr,96)
    s288, p288, leg288, chain288 = anatomy.audit_snapshot(bars288,meta,scale_atr,288)
    pre = int(meta['pre_closed_ts'])
    atr=float(meta['atr_pre']); low=float(meta['zone_low']); high=float(meta['zone_high'])
    closes48 = [b['close'] for b in bars96[-48:]]
    down = [float(l['amplitude_atr']) for l in leg96 if l['direction']=='DOWN']
    latest = chain96[-1] if chain96 else None
    latest2 = down[-2:]; earlier2 = down[-4:-2]
    down_ratio = _safe_ratio(median(latest2), median(earlier2)) if len(down)>=4 else None
    rise1 = float(latest['first_rise_atr']) if latest else None
    rise2 = float(latest['second_rise_atr']) if latest else None
    values = {
        'geo_low_up_share_96': _rising_share(p96,'LOW'),
        'geo_high_up_share_96': _rising_share(p96,'HIGH'),
        'geo_low_progress_last3_atr_96': s96['low_net_progress_last3_atr'],
        'geo_high_progress_last3_atr_96': s96['high_net_progress_last3_atr'],
        'geo_confirmed_pivots_96': s96['pivots_confirmed'],
        'geo_leg_amplitude_cv_96': _cv([l['amplitude_atr'] for l in leg96]),
        'rebuild_last_low_step_atr_96': s96['latest_lhlh_low_step_atr'],
        'rebuild_last_high_step_atr_96': s96['latest_lhlh_high_step_atr'],
        'rebuild_last_correction_fraction_96': latest['correction_ratio'] if latest else None,
        'rebuild_second_over_first_rise_96': _safe_ratio(rise2,rise1),
        'rebuild_chain_count_96': s96['confirmed_lhlh_count'],
        'compress_residual_osc24_atr_96': s96['residual_osc_24_recent_atr'],
        'compress_residual_ratio_24_96': s96['residual_osc_ratio_recent_vs_prev24'],
        'compress_downleg_ratio_new_old_96': down_ratio,
        'compress_downleg_cv_96': _cv(down),
        'level_close_distance_atr_96': s96['close_distance_to_zone_low_atr'],
        'level_near_share_48_96': s96['near_resistance_share_48'],
        'level_near_share_24_96': sum(_near(b['close'],low,high,atr) for b in bars96[-24:])/24,
        'level_approach_rearm_count_96': _approach_runs(bars96,low,high,atr),
        'level_above_zone_share_48_96': sum(b['close']>high for b in bars96[-48:])/48,
        'level_zone_position_288': s288['zone_position_in_window_range'],
        'level_max_depth_below_48_atr_96': s96['max_low_below_zone_48_atr'],
        'maturity_first_low_known_age_96': _known_age(pre,p96,'LOW'),
        'maturity_first_low_known_age_288': _known_age(pre,p288,'LOW'),
        'maturity_last_low_known_age_96': s96['last_low_known_age_bars'],
        'maturity_last3_low_span_bars_96': s96['low_span_last3_bars'],
        'maturity_near_tail_streak_bars_96': _last_near_streak(bars96,low,high,atr),
        'maturity_last_chain_age_bars_96': latest['age_since_h2_confirmed_bars'] if latest else None,
        'movement_signed_efficiency_24_96': s96['signed_efficiency_24'],
        'movement_signed_efficiency_48_96': s96['signed_efficiency_48'],
        'movement_net_advance_48_atr_96': (closes48[-1]-closes48[0])/atr,
        'movement_latest_lhl_retention_96': s96['latest_lhl_retention_fraction'],
        'movement_median_lhl_retracement_96': s96['median_lhl_retracement_fraction'],
        'movement_max_close_drawdown_48_atr_96': _drawdown(bars96[-48:],atr),
        'movement_up_close_steps_48_96': sum(b>a for a,b in zip(closes48,closes48[1:]))/47,
    }
    assert set(values)==set(FEATURES)
    # Numeric null is missing; never replace with a numeric 0.
    if any(x is not None and (not isinstance(x,(int,float)) or not math.isfinite(x)) for x in values.values()):
        raise ValueError('Huella con valores no numericos o no finitos')
    family_cover={f:sum(values[k] is not None for k in FEATURES if FEATURES[k][0]==f) for f in FAMILIES}
    family_tot={f:sum(FEATURES[k][0]==f for k in FEATURES) for f in FAMILIES}
    out={
        'fingerprint_version': VERSION,
        'symbol': meta['symbol'], 'episode_id':meta['episode_id'],
        'attempt_id':meta['attempt_id'], 'level_id':meta['level_id'],
        'pre_closed_ts':pre,
        'episode_ordinal_report_only': int(meta['episode_ordinal']),
        'atr_pre_report_only': atr, 'zone_low_report_only':low,'zone_high_report_only':high,
        'scale_atr':scale_atr, 'status':'COMPLETE_PRE_96_288',
        'observed_features':sum(v is not None for v in values.values()),
        'total_features':NUM_FEATURES,
        **{f'coverage_{f}':f'{family_cover[f]}/{family_tot[f]}' for f in FAMILIES},
        **values,
    }
    return out


def feature_catalog():
    return {
        'version':VERSION,
        'timeframe':'30m', 'time_contract':'PRE = last fully closed candle boundary; bar open<PRE and bar close<=PRE',
        'pivot_contract':'close-only ZigZag ATR reversals; pivot extreme_ts != confirmed_known_closed_ts',
        'null_semantics':'not enough confirmed pivots/legs, zero denominator or incomplete history. NULL never means zero or low quality',
        'cohort_limit':'Historical CROSS_UP-conditioned, not all possible pre-breakout candidate setups',
        'quality_scores':False,'uses_post':False,'uses_review_labels':False,
        'families': [{'id':key,'name':label} for key,label in FAMILIES.items()],
        'features':[{'id':id,'family':family,'unit':unit,'definition':description,'window_bars':window,
                     'method':method,'is_quality_score':False} for id,(family,unit,description,window,method) in FEATURES.items()],
    }


def save_csv(path,rows):
    if not rows:
        return
    cols=list(rows[0]);
    with path.open('x',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=cols,extrasaction='raise');writer.writeheader();writer.writerows(rows)


def load_reference_inputs(source_zip,full_map):
    return baseline.load_first_baseline(source_zip,full_map)


def build_reference_rows(all_rows,candles,scale_atr):
    out=[]
    seen=set()
    for r in sorted(all_rows,key=lambda x:(x['symbol'],x['pre_closed_ts'],x['attempt_id'])):
        aid=r['attempt_id']
        if aid in seen:
            raise ValueError('Attempt ID repetido')
        seen.add(aid)
        bars96=anatomy.take_pre(candles[r['symbol']],r['pre_closed_ts'],96)
        bars288=anatomy.take_pre(candles[r['symbol']],r['pre_closed_ts'],288)
        if bars96 is None or bars288 is None:
            raise ValueError('Ventanas incompletas '+aid)
        out.append(build_fingerprint(r,bars96,bars288,scale_atr))
    return out


def family_coverage(rows):
    result=[]
    for family in FAMILIES:
        ks=[k for k in FEATURES if FEATURES[k][0]==family]
        present=sum(r[k] is not None for r in rows for k in ks)
        result.append({'family':family,'description':FAMILIES[family],
                       'attempts':len(rows),'features_per_cut':len(ks),
                       'available_cells':present,'possible_cells':len(rows)*len(ks),
                       'coverage_fraction':round(present/(len(rows)*len(ks)),6) if rows else None})
    return result


def preview_html(rows):
    """Static, read-only specimen browser; no arbitrary quality coloring or score."""
    by_symbol={}
    for row in rows:
        by_symbol.setdefault(row['symbol'],[]).append(row)
    css="""<style>body{font:14px/1.5 system-ui,sans-serif;background:#10161d;color:#e4ebf1;max-width:1100px;margin:30px auto;padding:0 20px}h1{font-size:27px}h2{font-size:18px;margin-top:26px}h3{font-size:14px;color:#a1d3dd}p,small{color:#a7b7c3}.warn{padding:13px;border-left:4px solid #cc9e59;background:#26261f}.case{margin:18px 0;padding:16px;border:1px solid #374b59;background:#19242e;border-radius:12px}details{padding:8px 0}summary{cursor:pointer;color:#d6e6ef;font-weight:700}table{border-collapse:collapse;width:100%;margin:10px 0}td,th{padding:6px 9px;border-bottom:1px solid #394b58;text-align:left;vertical-align:top}td:first-child{width:46%}th{color:#b8cbd6}code{color:#e1c48c;font-size:12px}small{display:block}.muted{color:#98aab7}em{font-style:normal;color:#d8ad6b}</style>"""
    parts=['<!doctype html><html lang="es"><meta charset="utf-8"><title>HLL Huella Estructural PRE V1</title>',css,
           '<body><h1>HLL · Huella Estructural PRE V1</h1>',
           '<p>Vista previa REAL: 15 episodios de referencia, cada corte PRE por Attempt ID. Las seis familias no se reducen a una puntuacion.</p>',
           '<p class="warn">Descriptores exploratorios, NO senales ni probabilidades. La calificacion visual no se usa ni se asigna al primer intento; los pivotes se conocen tras confirmacion. Una variable vacia significa dato insuficiente, no cero. No se usan velas POST.</p>']
    for sym in sorted(by_symbol):
        cases=sorted(by_symbol[sym],key=lambda r:r['pre_closed_ts'])
        parts.append(f'<section class="case"><h2>{html.escape(sym)} · {len(cases)} corte(s)</h2>')
        for row in cases:
            parts.append('<details'+(' open' if row['episode_ordinal_report_only']==1 else '')+'><summary>'+f'Intento #{row["episode_ordinal_report_only"]} · PRE {anatomy.tsutc(row["pre_closed_ts"])} · {row["observed_features"]}/{row["total_features"]} medidas'+'</summary>')
            parts.append('<small>Attempt ID <code>'+html.escape(row['attempt_id'])+'</code> · Episode ID <code>'+html.escape(row['episode_id'])+'</code></small>')
            for family,label in FAMILIES.items():
                parts.append('<h3>'+html.escape(label)+' · '+row['coverage_'+family]+'</h3><table><tr><th>Variable</th><th>Valor medido</th><th>Unidad</th></tr>')
                for key,(f,unit,definition,window,method) in FEATURES.items():
                    if f!=family:continue
                    value=row[key]
                    val='— <em>insuficiente</em>' if value is None else f'{value:.4f}' if isinstance(value,float) else str(value)
                    parts.append('<tr><td><code>'+html.escape(key)+'</code><small>'+html.escape(definition)+'</small></td><td>'+val+'</td><td>'+html.escape(unit)+'</td></tr>')
                parts.append('</table>')
            parts.append('</details>')
        parts.append('</section>')
    parts.append('<p>Fuente: 30m, cortes PRE historicos, escala de pivote segun manifest. Para comparar con otros simbolos hay que definir pesos, normalizacion y politica de faltantes; NO estan implementados aqui.</p></body></html>')
    return ''.join(parts)


def args_parser():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    p.add_argument('--source-zip',type=Path,default=DEFAULT_ZIP)
    p.add_argument('--full-map',type=Path,default=DEFAULT_MAP)
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--scale-atr',type=float,choices=(.5,1.0,1.5),default=1.0)
    return p


def main(argv=None):
    args=args_parser().parse_args(argv)
    if args.output.exists() and args.run:
        raise SystemExit('NO_OVERWRITE: salida ya existe '+str(args.output))
    first, all_rows, candles, source_hash, map_hash=load_reference_inputs(args.source_zip,args.full_map)
    if len(first)!=15 or len(all_rows)!=40:
        raise ValueError('Cohorte inesperada; no asumir que es el mismo estudio')
    pl={'version':VERSION,'first_attempts':len(first),'attempts':len(all_rows),
        'distinct_episodes':len({r['episode_id'] for r in all_rows}),
        'families':len(FAMILIES),'features':NUM_FEATURES,'scale_atr':args.scale_atr,
        'windows':[96,288],'post':False,'labels_in_features':False,'modifies_live':False,
        'source_sha256':source_hash,'episode_map_sha256':map_hash,'output':str(args.output)}
    print('HLL_FINGERPRINT_PLAN',json.dumps(pl,ensure_ascii=False),flush=True)
    if args.plan:return
    rows=build_reference_rows(all_rows,candles,args.scale_atr)
    # Do not create a partial output before all validations finish.
    args.output.mkdir(parents=True,exist_ok=False)
    save_csv(args.output/'fingerprints_pre.csv',rows)
    coverage=family_coverage(rows)
    save_csv(args.output/'coverage_by_family.csv',coverage)
    (args.output/'feature_catalog.json').write_text(json.dumps(feature_catalog(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (args.output/'fingerprints_preview_pre.html').write_text(preview_html(rows),encoding='utf-8')
    manifest={**pl,'status':'DONE','attempt_ids_sha256':hashlib.sha256('\n'.join(r['attempt_id'] for r in rows).encode()).hexdigest(),
              'incomplete_cells':len(rows)*NUM_FEATURES-sum(r['observed_features'] for r in rows),
              'available_cells':sum(r['observed_features'] for r in rows),
              'first_attempt_count':sum(r['episode_ordinal_report_only']==1 for r in rows),
              'not_a_signal':True,'quality_score_exists':False}
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('HLL_FINGERPRINT_DONE',json.dumps({'status':'DONE','attempts':len(rows),'episodes':pl['distinct_episodes'],
        'features':NUM_FEATURES,'families':len(FAMILIES),'available_cells':manifest['available_cells'],
        'incomplete_cells':manifest['incomplete_cells'],'output':str(args.output)},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
