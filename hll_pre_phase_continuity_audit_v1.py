#!/usr/bin/env python3
"""HLL PRE: conservación de recuperaciones y cesión entre fases (CORE15).

Investigación descriptiva, de solo lectura, sobre las mismas 96 velas PRE
congeladas por Phase Reconstruction V1. Compara los 3 ATR sin seleccionar
ex-post el mejor mínimo, sin POST, etiquetas visuales, señales o LIVE.

Definiciones:
 - Último mínimo = último LOW **confirmado al corte PRE**, no el mejor rebote.
 - Retención = (cierre PRE - cierre del LOW) / (máximo cierre después del LOW
   - cierre del LOW), definido solo si hubo excursión positiva. Puede ser <0.
 - Ciclo completo = un UP CONFIRMED_PRE seguido de DOWN CONFIRMED_PRE.
   Cesión = caída desde el máximo del UP hasta el mínimo del DOWN / subida UP.
   Cesión >=1 implica devolución completa de ese UP. Si comienza en el
   borde izquierdo de la ventana, el ciclo es censurado y no se suma a
   los ciclos plenamente observados.
 - Terminal PROVISIONAL se muestra, nunca se equipara a ciclo confirmado.

Una fotografia PRE es retrospectiva respecto de pivotes confirmados; no es
un backtest de señales bar-by-bar. Los 50 intentos NO son muestra holdout.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path('reports/horizontal_level_second_ascent_pre')
PHASE = BASE / 'core15_phase_reconstruction_pre_v1'
PRIOR = BASE / 'core15_pre_family_audit_v1'
OUTPUT = BASE / 'core15_pre_phase_continuity_audit_v1'
SCALES = (0.5, 1.0, 1.5)
TF_MS = 1_800_000
SOURCES = {
    'audit_matrix': PRIOR / 'audit_matrix_pre.csv',
    'audit_manifest': PRIOR / 'manifest.json',
    'phase_manifest': PHASE / 'manifest.json',
    'summary': PHASE / 'phase_summary_pre.csv',
    'legs': PHASE / 'phase_legs_pre.csv',
    'candidates': PHASE / 'phase_recovery_candidates_pre.csv',
}
# Required to honor frozen parent audit provenance.
PRIOR_SOURCES = {
    'families': BASE / 'core15_pre_family_interpreter_v1/families_pre.csv',
    'family_manifest': BASE / 'core15_pre_family_interpreter_v1/manifest.json',
    'resistance': BASE / 'core15_resistance_interaction_pre_v3/resistance_windows_pre.csv',
    'ohlc': BASE / 'core15_structural_approach_ohlc_pre_v2/candle_windows_pre.csv',
}
SCALE_COLUMNS = [
    'symbol','attempt_id','level_id','pre_closed_ts','scale_atr','status',
    'latest_confirmed_low_age_bars','latest_low_confirmed_known_closed_ts',
    'latest_recovery_net_atr','latest_recovery_max_favorable_atr',
    'latest_recovery_retention_frac','latest_recovery_peak_drawback_atr',
    'latest_recovery_undercut','latest_recovery_gap_reduction_atr',
    'previous_confirmed_low_age_bars','previous_recovery_retention_frac',
    'previous_recovery_peak_drawback_atr',
    'terminal_leg_direction','terminal_leg_is_provisional',
    'completed_up_down_cycles','left_censored_cycles',
    'complete_givebacks_all','complete_givebacks_last3','max_giveback_last3',
    'last_completed_giveback','last_completed_cycle_broke_its_anchor_low',
    'rise_sum_last3_atr','drop_sum_last3_atr','net_trough_progress_last3_atr',
    'preserved_fraction_last3','undercuts_last3',
    'cycle_detail_last3','data_limitations',
]
MATRIX_COLUMNS = ['symbol','attempt_id','level_id','pre_closed_ts',
    'review_visual','review_provenance','family_1p0','intercontact_direction_48',
    'retention_0p5','retention_1p0','retention_1p5',
    'givebacks_0p5','givebacks_1p0','givebacks_1p5',
    'latest_low_age_1p0','latest_recovery_net_atr_1p0',
    'latest_recovery_peak_drawback_atr_1p0',
    'previous_recovery_retention_1p0',
    'completed_cycles_1p0','complete_givebacks_last3_1p0',
    'max_giveback_last3_1p0','last_completed_giveback_1p0',
    'net_trough_progress_last3_atr_1p0','preserved_fraction_last3_1p0',
    'undercuts_last3_1p0',
    'terminal_leg_1p0','data_limitations_1p0',
    'structure_review_notes','metrics_are_not_quality_labels']
CYCLE_COLUMNS = ['symbol','attempt_id','level_id','pre_closed_ts','scale_atr',
    'cycle_index','up_leg_index','down_leg_index','up_start_open_ts',
    'down_trough_open_ts','down_confirmed_known_closed_ts',
    'up_amplitude_atr','down_amplitude_atr','giveback_ratio',
    'full_giveback','undercut_previous_low','left_censored',
    'status']


def read_csv(path: Path):
    with path.open('r',encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def sha(path: Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for c in iter(lambda:f.read(1024*1024),b''):
            h.update(c)
    return h.hexdigest()


def number(value):
    if value is None or str(value).strip().lower() in ('','nan','none','null'):
        return None
    f=float(value)
    if not math.isfinite(f): raise ValueError('Número no finito')
    return f


def integer(value):
    n=number(value)
    if n is None or n!=int(n): raise ValueError(f'Entero inválido: {value!r}')
    return int(n)


def key(row):
    return (str(row['attempt_id']),float(row['scale_atr']))


def _unique(rows, keys, label):
    result={}
    for r in rows:
        k=keys(r)
        if k in result:raise ValueError(f'Duplicado en {label}: {k}')
        result[k]=r
    return result


def verify_parent_manifests(prior: dict, phase: dict):
    if not (prior.get('inputs_are_pre_only') is True
            and prior.get('reads_post') is False
            and prior.get('reads_visual_for_family') is False
            and prior.get('modifies_live') is False
            and prior.get('rows')==50 and prior.get('attempts')==50):
        raise ValueError('Manifiesto auditor V1 no certifica 50 intentos PRE')
    if not (phase.get('lookback_closed_30m_bars')==96
            and phase.get('summary_rows')==150
            and phase.get('guardrails',{}).get('uses_post') is False
            and phase.get('guardrails',{}).get('uses_visual_labels') is False
            and phase.get('guardrails',{}).get('modifies_live') is False):
        raise ValueError('Manifiesto fases no certifica 96 velas PRE aisladas')
    for name,p in PRIOR_SOURCES.items():
        if not p.is_file() or sha(p)!=prior.get('source_sha256',{}).get(name):
            raise ValueError(f'Auditoría previa desactualizada: {name}')


def _grouper(rows, label):
    grouped=defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return grouped


def _core_validate(source, meta, what):
    if (source['attempt_id']!=meta['attempt_id'] or
        source['symbol']!=meta['symbol'] or
        source['level_id']!=meta['level_id'] or
        integer(source['pre_closed_ts'])!=integer(meta['pre_closed_ts'])):
        raise ValueError(f'{what} con símbolo/nivel/corte incorrecto: {meta["attempt_id"]}')


def analyze_one(summary, legs, candidates):
    """Produce an immutable-at-cutoff interpretation of one attempt×ATR."""
    if summary['status']!='COMPLETE_PRE' or integer(summary['closed_bars'])!=96:
        raise ValueError('Fases incompletas o horizonte distinto de 96 PRE')
    pre=integer(summary['pre_closed_ts'])
    if pre<=0:raise ValueError('PRE inválido')
    # Order by existing leg index; never rank candidates by return, gain or quality.
    legs=sorted(legs,key=lambda x:integer(x['leg_index']))
    if [integer(l['leg_index']) for l in legs]!=list(range(1,len(legs)+1)):
        raise ValueError('Índices de tramos no contiguos')
    if len(legs)!=integer(summary['confirmed_pivots']):
        raise ValueError('Un tramo por pivote confirmado requerido')
    for i,l in enumerate(legs):
        _core_validate(l,summary,'tramo')
        if integer(l['start_pivot_known_closed_ts'])>pre:
            raise ValueError('Tramo anclado en pivote futuro')
        if integer(l['end_extreme_open_ts'])+TF_MS>pre:
            raise ValueError('Extremo de tramo fuera de PRE')
        if integer(l['start_pivot_open_ts'])>integer(l['end_extreme_open_ts']):
            raise ValueError('Extremo anterior al pivote de inicio')
        expected_status='PROVISIONAL' if i==len(legs)-1 else 'CONFIRMED_PRE'
        if l['status']!=expected_status:
            raise ValueError('Secuencia de tramos confirmados/provisionales inconsistente')
        if l['status']=='CONFIRMED_PRE':
            if integer(l['end_extreme_confirmed_known_closed_ts'])>pre:
                raise ValueError('Extremo confirmado con conocimiento futuro')
            if integer(l['end_extreme_confirmed_known_closed_ts'])<integer(l['end_extreme_open_ts'])+TF_MS:
                raise ValueError('Confirmación antes del cierre del extremo')
        elif number(l['end_extreme_confirmed_known_closed_ts']) is not None:
            raise ValueError('Tramo provisional incorrectamente confirmado')
        if l['leg_direction'] != ('UP' if l['leg_role']=='RECOVERY_LEG' else 'DOWN'):
            raise ValueError('Dirección/rol de tramo contradictorios')
        if i and l['leg_direction']==legs[i-1]['leg_direction']:
            raise ValueError('Tramos de fases no alternan')
        if i and integer(l['start_pivot_open_ts'])!=integer(legs[i-1]['end_extreme_open_ts']):
            raise ValueError('Extremos de tramos no empalman')
    if bool(legs) != bool(integer(summary['terminal_leg_provisional'])):
        raise ValueError('Terminal provisional incoherente')
    if legs and summary['terminal_leg_direction']!=legs[-1]['leg_direction']:
        raise ValueError('Dirección terminal incoherente')
    if sum(l['status']=='CONFIRMED_PRE' and l['leg_direction']=='UP' for l in legs)!=integer(summary['confirmed_up_legs']):
        raise ValueError('Conteo de subidas confirmadas inconsistente')
    if sum(l['status']=='CONFIRMED_PRE' and l['leg_direction']=='DOWN' for l in legs)!=integer(summary['confirmed_down_legs']):
        raise ValueError('Conteo de correcciones confirmadas inconsistente')
    candidates=sorted(candidates,key=lambda x:integer(x['anchor_low_open_ts']))
    for c in candidates:
        _core_validate(c,summary,'mínimo candidato')
        if (integer(c['anchor_low_known_closed_ts'])>pre or
            integer(c['anchor_low_open_ts'])+TF_MS>pre or
            integer(c['candidate_is_retrospective_not_trade'])!=1):
            raise ValueError('Mínimo posterior a PRE o presentado como entrada')
        if integer(c['age_from_low_bars'])!=(pre-integer(c['anchor_low_open_ts']))//TF_MS-1:
            raise ValueError('Edad del mínimo incorrecta')
        gain=number(c['max_favorable_close_from_low_atr'])
        net=number(c['net_change_from_low_atr'])
        drawback=number(c['max_peak_to_last_close_drawback_atr'])
        retention=number(c['last_close_retention_fraction'])
        if gain is None or net is None or drawback is None or gain < 0 or drawback < -1e-7:
            raise ValueError('Métricas de mínimo incompletas/no válidas')
        if not math.isclose(drawback,gain-net,rel_tol=1e-6,abs_tol=3e-7):
            raise ValueError('Cesión desde máximo posterior no coincide con neto del LOW')
        if gain>0:
            if retention is None or not math.isclose(retention,net/gain,rel_tol=1e-6,abs_tol=3e-7):
                raise ValueError('Retención del mínimo no coincide con excursión y neto')
        elif retention is not None:
            raise ValueError('Retención fabricada sin avance favorable')
    if len(candidates)!=integer(summary['recovery_candidates']):
        raise ValueError('Cantidad de mínimos candidatos inconsistente')
    latest=[c for c in candidates if integer(c['is_latest_confirmed_low'])==1]
    if len(latest)!=(1 if candidates else 0) or (latest and latest[0] is not candidates[-1]):
        raise ValueError('Último LOW confirmado ausente/duplicado/no cronológico')
    cur=latest[0] if latest else None
    prev=candidates[-2] if len(candidates)>=2 else None
    cycles=[]
    for up, down in zip(legs,legs[1:]):
        if (up['status']!='CONFIRMED_PRE' or down['status']!='CONFIRMED_PRE'
            or up['leg_direction']!='UP' or down['leg_direction']!='DOWN'):
            continue
        up_start=number(up['start_pivot_price_close'])
        up_end=number(up['end_extreme_price_close'])
        down_start=number(down['start_pivot_price_close'])
        down_end=number(down['end_extreme_price_close'])
        if not math.isclose(up_end,down_start,rel_tol=1e-10,abs_tol=1e-12):
            raise ValueError('Ciclo con inicio de corrección distinto al máximo confirmado')
        rise=up_end-up_start
        drop=down_start-down_end
        if rise<=0 or drop<=0 or number(up['amplitude_atr'])<=0 or number(down['amplitude_atr'])<=0:
            raise ValueError('Ciclo con subida/caída no positiva')
        ratio=drop/rise
        amp_up=number(up['amplitude_atr']);amp_down=number(down['amplitude_atr'])
        if not math.isclose(amp_down/amp_up,ratio,rel_tol=1e-6,abs_tol=1e-6):
            raise ValueError('ATR de cesión no coincide con los precios de fase')
        censored=integer(up['start_is_window_boundary'])==1
        cycles.append({**{k:summary[k] for k in ('symbol','attempt_id','level_id','pre_closed_ts')},
            'scale_atr':number(summary['scale_atr']),
            'cycle_index':len(cycles)+1,
            'up_leg_index':integer(up['leg_index']),'down_leg_index':integer(down['leg_index']),
            'up_start_open_ts':integer(up['start_pivot_open_ts']),
            'down_trough_open_ts':integer(down['end_extreme_open_ts']),
            'down_confirmed_known_closed_ts':integer(down['end_extreme_confirmed_known_closed_ts']),
            'up_amplitude_atr':number(up['amplitude_atr']),
            'down_amplitude_atr':number(down['amplitude_atr']),
            'giveback_ratio':round(ratio,8),'full_giveback':int(ratio>=1-1e-10),
            'undercut_previous_low':int(down_end<up_start-1e-12),
            'left_censored':int(censored),
            'status':'BORDE_IZQUIERDO' if censored else 'CICLO_CONFIRMADO_PRE'})
    complete=[c for c in cycles if not c['left_censored']]
    last3=complete[-3:]
    def val(c,field):return number(c.get(field)) if c else None
    limitations=[]
    if not cur: limitations.append('SIN_MINIMO_CONFIRMADO')
    elif integer(cur['anchor_is_window_boundary'])==1:limitations.append('MINIMO_EN_BORDE_IZQUIERDO')
    if cur and integer(cur['age_from_low_bars'])<6:
        # Label only, not a quality rule; less than three hours of 30m closes.
        limitations.append('MINIMO_RECIENTE_MENOS_3_HORAS')
    if cur and number(cur['last_close_retention_fraction']) is None:
        limitations.append('SIN_EXCURSION_FAVORABLE_DESDE_ULTIMO_MINIMO')
    if prev is None:limitations.append('SIN_MINIMO_ANTERIOR')
    if not complete: limitations.append('SIN_CICLOS_COMPLETOS_OBSERVABLES')
    if any(c['left_censored'] for c in cycles):limitations.append('PRIMER_CICLO_CENSURADO_IZQUIERDA')
    if legs:limitations.append('ULTIMO_TRAMO_PROVISIONAL')
    if not limitations: limitations.append('SIN_LIMITACIONES_ADICIONALES')
    row={**{k:summary[k] for k in ('symbol','attempt_id','level_id','pre_closed_ts')},
        'scale_atr':number(summary['scale_atr']),'status':'DESCRIPTIVE_PRE_ONLY',
        'latest_confirmed_low_age_bars':integer(cur['age_from_low_bars']) if cur else None,
        'latest_low_confirmed_known_closed_ts':integer(cur['anchor_low_known_closed_ts']) if cur else None,
        'latest_recovery_net_atr':val(cur,'net_change_from_low_atr'),
        'latest_recovery_max_favorable_atr':val(cur,'max_favorable_close_from_low_atr'),
        'latest_recovery_retention_frac':val(cur,'last_close_retention_fraction'),
        'latest_recovery_peak_drawback_atr':val(cur,'max_peak_to_last_close_drawback_atr'),
        'latest_recovery_undercut':integer(cur['later_lowest_close_undercut']) if cur else None,
        'latest_recovery_gap_reduction_atr':val(cur,'gap_reduction_atr'),
        'previous_confirmed_low_age_bars':integer(prev['age_from_low_bars']) if prev else None,
        'previous_recovery_retention_frac':val(prev,'last_close_retention_fraction'),
        'previous_recovery_peak_drawback_atr':val(prev,'max_peak_to_last_close_drawback_atr'),
        'terminal_leg_direction':summary['terminal_leg_direction'],
        'terminal_leg_is_provisional':integer(summary['terminal_leg_provisional']),
        'completed_up_down_cycles':len(complete),
        'left_censored_cycles':len(cycles)-len(complete),
        'complete_givebacks_all':sum(c['full_giveback'] for c in complete),
        'complete_givebacks_last3':sum(c['full_giveback'] for c in last3) if last3 else None,
        'max_giveback_last3':max((c['giveback_ratio'] for c in last3),default=None),
        'last_completed_giveback':last3[-1]['giveback_ratio'] if last3 else None,
        'last_completed_cycle_broke_its_anchor_low':last3[-1]['undercut_previous_low'] if last3 else None,
        'rise_sum_last3_atr':sum(c['up_amplitude_atr'] for c in last3) if last3 else None,
        'drop_sum_last3_atr':sum(c['down_amplitude_atr'] for c in last3) if last3 else None,
        'net_trough_progress_last3_atr':(sum(c['up_amplitude_atr']-c['down_amplitude_atr'] for c in last3) if last3 else None),
        'preserved_fraction_last3':(1-sum(c['down_amplitude_atr'] for c in last3)/sum(c['up_amplitude_atr'] for c in last3)) if last3 else None,
        'undercuts_last3':sum(c['undercut_previous_low'] for c in last3) if last3 else None,
        'cycle_detail_last3':' | '.join(f"{c['giveback_ratio']:.2f}×" for c in last3),
        'data_limitations':' | '.join(limitations),
    }
    return row,cycles


def build_data(audit,summary,legs,candidates):
    """Strict 50×3 join; labels in audit are carried ONLY to output navigation."""
    if len(audit)!=50 or len(summary)!=150:
        raise ValueError('Se requieren auditoría 50 y fases 150')
    audit_by_id=_unique(audit,lambda r:r['attempt_id'],'auditoría 50')
    summaries=_unique(summary,key,'resumen 50×3')
    expected={(aid,s) for aid in audit_by_id for s in SCALES}
    if set(summaries)!=expected:
        raise ValueError('Faltan o sobran escalas en fases V1')
    leg_by_key=_grouper(legs,'tramos')
    candidate_by_key=_grouper(candidates,'mínimos')
    if (set(leg_by_key)-expected) or (set(candidate_by_key)-expected):
        raise ValueError('Tramos o mínimos de cohort desconocido')
    by_key={}; cycles_all=[]
    for aid in sorted(audit_by_id):
        base=audit_by_id[aid]
        for scale in SCALES:
            sr=summaries[(aid,scale)]
            if (sr['symbol']!=base['symbol'] or sr['level_id']!=base['level_id'] or
                integer(sr['pre_closed_ts'])!=integer(base['pre_closed_ts'])):
                raise ValueError(f'Corte/símbolo/nivel discrepante en {aid}/{scale}')
            row,cy=analyze_one(sr,leg_by_key.get((aid,scale),[]),candidate_by_key.get((aid,scale),[]))
            by_key[(aid,scale)]=row
            cycles_all.extend(cy)
    output=[]
    for aid,base in sorted(audit_by_id.items()):
        r05=by_key[(aid,0.5)];r10=by_key[(aid,1.0)];r15=by_key[(aid,1.5)]
        row={k:base[k] for k in ('symbol','attempt_id','level_id','pre_closed_ts',
            'review_visual','review_provenance','family_1p0','intercontact_direction_48')}
        row.update({
            'retention_0p5':r05['latest_recovery_retention_frac'],
            'retention_1p0':r10['latest_recovery_retention_frac'],
            'retention_1p5':r15['latest_recovery_retention_frac'],
            'givebacks_0p5':r05['complete_givebacks_last3'],
            'givebacks_1p0':r10['complete_givebacks_last3'],
            'givebacks_1p5':r15['complete_givebacks_last3'],
            'latest_low_age_1p0':r10['latest_confirmed_low_age_bars'],
            'latest_recovery_net_atr_1p0':r10['latest_recovery_net_atr'],
            'latest_recovery_peak_drawback_atr_1p0':r10['latest_recovery_peak_drawback_atr'],
            'previous_recovery_retention_1p0':r10['previous_recovery_retention_frac'],
            'completed_cycles_1p0':r10['completed_up_down_cycles'],
            'complete_givebacks_last3_1p0':r10['complete_givebacks_last3'],
            'max_giveback_last3_1p0':r10['max_giveback_last3'],
            'last_completed_giveback_1p0':r10['last_completed_giveback'],
            'net_trough_progress_last3_atr_1p0':r10['net_trough_progress_last3_atr'],
            'preserved_fraction_last3_1p0':r10['preserved_fraction_last3'],
            'undercuts_last3_1p0':r10['undercuts_last3'],
            'terminal_leg_1p0':r10['terminal_leg_direction'],
            'data_limitations_1p0':r10['data_limitations'],
            'structure_review_notes':('REVISAR_CESION_HISTORICA' if number(r10['max_giveback_last3']) is not None and r10['max_giveback_last3']>=1 else 'SIN_CESION_TOTAL_ULTIMOS_CICLOS') if r10['completed_up_down_cycles'] else 'SIN_CICLO_COMPLETO',
            'metrics_are_not_quality_labels':1,
        })
        output.append(row)
    return [by_key[k] for k in sorted(by_key)],output,cycles_all


def summarize(scale_rows,matrix,cycles):
    at1=[r for r in scale_rows if r['scale_atr']==1.0]
    return {'attempts':len(matrix),'scale_rows':len(scale_rows),
        'cycles':len(cycles),
        '1atr_latest_low_with_retention':sum(r['latest_recovery_retention_frac'] is not None for r in at1),
        '1atr_with_completed_cycles':sum(r['completed_up_down_cycles']>0 for r in at1),
        '1atr_with_full_giveback_last3':sum((r['complete_givebacks_last3'] or 0)>0 for r in at1),
        '1atr_left_censored_cycle_cases':sum(r['left_censored_cycles']>0 for r in at1),
        '1atr_negative_net_progress_last3':sum(number(r['net_trough_progress_last3_atr']) is not None and r['net_trough_progress_last3_atr']<0 for r in at1),
        '1atr_terminal_provisional':sum(r['terminal_leg_is_provisional']==1 for r in at1),
        'no_post_used':True,'visual_labels_used_in_metrics':False,
        'warning':'Exploratorio sobre 50 casos previamente inspeccionados: no mide rentabilidad ni ventaja predictiva.'}


def markdown(summary,matrix):
    lines=['# HLL · Conservación y deterioro de fases PRE — V1','',
        '**Solo descriptivo y retrospectivo al corte PRE.** Sin POST, calidad buena/mala, entradas, TP/SL ni LIVE.','',
        f"- {summary['attempts']} intentos; {summary['scale_rows']} combinaciones intento × ATR.",
        f"- A 1 ATR: {summary['1atr_latest_low_with_retention']}/50 con retención definida desde último LOW confirmado.",
        f"- {summary['1atr_with_completed_cycles']}/50 con al menos un ciclo UP→DOWN completamente confirmado y no censurado por izquierda.",
        f"- {summary['1atr_with_full_giveback_last3']}/50 con al menos una devolución completa entre sus últimos tres ciclos cerrados.",
        '', '## Interpretación',
        '- La retención del último mínimo puede ser alta aunque haya habido una caída anterior grande.',
        '- La cesión de ciclos permite ver esas pérdidas anteriores sin seleccionar el mínimo «mejor».',
        '- La fracción conservada de los 3 últimos ciclos es 1 - (suma de correcciones/suma de subidas), y puede ser negativa; no es una probabilidad.',
        '- El tramo terminal es provisional; no se cuenta como ciclo confirmado.',
        '- Un ciclo que empieza en el borde izquierdo de las 96 velas se exporta, pero se excluye de conteos de ciclo completo.',
        '- Los máximos y mínimos de los tramos son extremos de **cierres** y los pivotes fueron confirmados posteriormente. Esto NO es un replay de señales.',
        '- Un 100% de cesión es una referencia geométrica natural, no un filtro ni una regla rentable.',
        '- Los eventos comparten símbolos/mercado y no constituyen observaciones independientes. Las referencias visuales son preliminares.',
        '', '## Casos conocidos a 1 ATR',
        '| Símbolo | ID | Retención último LOW | Conservación últimos 3 ciclos | Cesiones ≥100% | Ciclos completos |',
        '|---|---|---:|---:|---:|---:|']
    known={'2221ee173c945cf17b026f2d','5c16416a996220d47fe32c96',
        '412e50a892f6e90f492ceaaa','917bcd9be0ad18833727dc3d',
        'a419c2ae10918faceff87cc4'}
    for r in matrix:
        if r['attempt_id'] in known:
            retention=r['retention_1p0'];value='SIN_DATOS' if retention is None else f'{retention*100:.1f}%'
            preserved=r["preserved_fraction_last3_1p0"]
            preserved_s="SIN_DATOS" if preserved is None else f"{preserved*100:+.1f}%"
            lines.append(f"| {r['symbol']} | `{r['attempt_id'][:10]}…` | {value} | {preserved_s} | {r['complete_givebacks_last3_1p0'] if r['complete_givebacks_last3_1p0'] is not None else 'SIN_DATOS'} | {r['completed_cycles_1p0']} |")
    lines+=['','## Próximo paso',
        'Auditar inconsistencias, registros censurados y sensibilidad ATR; contrastar con evaluaciones visuales **solo después** de congelar estas métricas. No introducir filtros por estos casos.']
    return '\n'.join(lines)+'\n'


def write_csv(path, rows,columns):
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        w.writeheader();w.writerows(rows)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true')
    m.add_argument('--run',action='store_true')
    opts=p.parse_args(argv)
    absent=[str(f) for f in [*SOURCES.values(),*PRIOR_SOURCES.values()] if not f.is_file()]
    plan={'output':str(OUTPUT),'sources':{k:str(v) for k,v in SOURCES.items()},
        'missing':absent,'reads_post':False,'reads_visual_for_metrics':False,
        'modifies_live':False,'overwrites_original_audits':False}
    print('HLL_CONTINUITY_PLAN '+json.dumps(plan,ensure_ascii=False))
    if absent:return 2
    try:
        prior=json.loads(SOURCES['audit_manifest'].read_text(encoding='utf-8'))
        phase=json.loads(SOURCES['phase_manifest'].read_text(encoding='utf-8'))
        verify_parent_manifests(prior,phase)
        scales,matrix,cycles=build_data(read_csv(SOURCES['audit_matrix']),
            read_csv(SOURCES['summary']),read_csv(SOURCES['legs']),
            read_csv(SOURCES['candidates']))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f'HLL_CONTINUITY_ABORT {exc}')
        return 2
    if opts.plan:
        print('HLL_CONTINUITY_VALID',json.dumps({'attempts':len(matrix),'rows':len(scales),'cycles':len(cycles)}))
        return 0
    OUTPUT.mkdir(parents=True,exist_ok=True)
    write_csv(OUTPUT/'continuity_scales_pre.csv',scales,SCALE_COLUMNS)
    write_csv(OUTPUT/'continuity_matrix_pre.csv',matrix,MATRIX_COLUMNS)
    write_csv(OUTPUT/'continuity_cycles_pre.csv',cycles,CYCLE_COLUMNS)
    stats=summarize(scales,matrix,cycles)
    (OUTPUT/'continuity_summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (OUTPUT/'continuity_report.md').write_text(markdown(stats,matrix),encoding='utf-8')
    manifest={'name':'HLL_PRE_PHASE_CONTINUITY_V1','attempts':50,'scale_rows':150,
        'source_sha256':{name:sha(path) for name,path in SOURCES.items()},
        'output_sha256':{name:sha(OUTPUT/name) for name in ('continuity_scales_pre.csv','continuity_matrix_pre.csv','continuity_cycles_pre.csv')},
        'reads_post':False,'reads_visual_for_metrics':False,
        'modifies_live':False,'classifies_good_bad':False,
        'phase_window_bars':96,'timeframe_minutes':30,
        'notes':'Se calculó con todas las fases confirmadas y último LOW por recencia, sin seleccionar el más favorable.'}
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('HLL_CONTINUITY_DONE '+json.dumps({'output':str(OUTPUT),**stats},ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
