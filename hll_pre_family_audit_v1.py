#!/usr/bin/env python3
"""HLL: auditoría comparativa de los 50 intentos CORE15, sin POST ni reentrenamiento.

Cruza el output CONGELADO del intérprete PRE V1 con OHLC V2, resistencia V3
 y notas visuales de referencia (estas NO intervienen en la familia calculada).
Produce una matriz por intento y contraejemplos para revisión humana, nunca
señales, etiquetas predictivas ni resultados de trading.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('reports/horizontal_level_second_ascent_pre')
FAMILIES = ROOT / 'core15_pre_family_interpreter_v1/families_pre.csv'
FAMILY_MANIFEST = ROOT / 'core15_pre_family_interpreter_v1/manifest.json'
RESISTANCE = ROOT / 'core15_resistance_interaction_pre_v3/resistance_windows_pre.csv'
OHLC = ROOT / 'core15_structural_approach_ohlc_pre_v2/candle_windows_pre.csv'
OUTPUT = ROOT / 'core15_pre_family_audit_v1'
SCALES = (0.5, 1.0, 1.5)
FAMILY_ORDER = ('ASCENSO_ESCALONADO', 'RECONSTRUCCION_ENTRE_CONTACTOS',
                'REBOTE_TRAS_CORRECCION', 'OSCILACION_EN_RESISTENCIA',
                'DETERIORO_DESDE_MINIMO', 'CORRECCION_DOMINANTE',
                'SECUENCIA_MIXTA_INDETERMINADA')

# Copia explícita de los 17 casos visibles de la vista de tandas. Etiquetas
# preliminares; NO se emplean al asignar familias o escoger umbrales.
# La tanda 3 tiene valoraciones propuestas por el asistente, NO validadas por usuario.
REVIEW_REFERENCES = (
    # symbol, id, judgment, provenance, batch
    ('AAVEUSDT','2221ee173c945cf17b026f2d','CONSTRUCTIVA','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('AIAUSDT','81bcd23506a40114b2eba209','CONSTRUCTIVA','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('1000PEPEUSDT','5c16416a996220d47fe32c96','CONSTRUCTIVA','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('2ZUSDT','ffcb487cc3523ae7d1c48dbe','CONSTRUCTIVA','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('ACUUSDT','b7b84e538c0fbe8799b2762a','NO_CONVENCE','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('1000FLOKIUSDT','a8cd48b0f38f97d50a91be6d','NO_CONVENCE','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('1INCHUSDT','917bcd9be0ad18833727dc3d','NO_CONVENCE','REVISION_VISUAL_PRELIMINAR','TANDA_1'),
    ('ACUUSDT','bb5f7458c98c04efff3edaa0','CONSTRUCTIVA','USUARIO_FANTASTICA','TANDA_2'),
    ('AAVEUSDT','abe8975e8b1ff733e8513f76','CONSTRUCTIVA','USUARIO_FANTASTICA','TANDA_2'),
    ('2ZUSDT','412e50a892f6e90f492ceaaa','CONSTRUCTIVA','USUARIO_FANTASTICA','TANDA_2'),
    ('1000BONKUSDT','a419c2ae10918faceff87cc4','NO_CONVENCE','USUARIO','TANDA_2'),
    ('4USDT','edc22fc645c77831fe3f6a48','CONSTRUCTIVA','ASISTENTE_PROVISIONAL','TANDA_3'),
    ('1000FLOKIUSDT','405fb903640b8544e19ab2e0','DUDOSA','ASISTENTE_PROVISIONAL','TANDA_3'),
    ('ADAUSDT','4958928649fa8e6fe52be59e','DUDOSA','ASISTENTE_PROVISIONAL','TANDA_3'),
    ('1000PEPEUSDT','b00b2dac6b48b6fa67caa257','CONSTRUCTIVA','ASISTENTE_PROVISIONAL','TANDA_3'),
    ('1000BONKUSDT','a3015ca2f41fa79ef27de2c1','NO_CONVENCE','ASISTENTE_PROVISIONAL','TANDA_3'),
    ('AIAUSDT','2731c934c96999d326127e57','CONSTRUCTIVA','ASISTENTE_PROVISIONAL','TANDA_3'),
)

MATRIX_COLUMNS = [
    'symbol','attempt_id','level_id','pre_closed_ts','pre_utc','review_visual',
    'review_provenance','review_batch','family_0p5','family_1p0','family_1p5',
    'scales_agree','distinct_families','terminal_phase_1atr',
    'low_progression_1atr','high_progression_1atr','low_steps_up_1atr',
    'low_steps_down_1atr','high_steps_up_1atr','high_steps_down_1atr',
    'intercontact_direction_48','intercontact_delta_48_atr','intercontact_trough_count_48',
    'touch_episodes_48','zone_center_crossings_48','zone_touch_share_48',
    'net_return_pre24_pct','net_return_pre48_pct','net_return_pre96_pct',
    'close_path_efficiency_48','pre_close_position_in_range_96',
    'first_close_relative_zone_atr_96','start_below_zone_gap_atr_96',
    'latest_low_net_change_atr_1atr','latest_low_undercut_1atr',
    'evidence_favorable_1atr','evidence_contrary_1atr','limitations_1atr',
    'audit_flags','audit_flags_n','audit_priority',
]
REVIEW_COLUMNS = ['symbol','attempt_id','review_visual','review_provenance','review_batch']


def read_csv(path: Path):
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def file_hash(path: Path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def number(v):
    if v is None or str(v).strip().lower() in ('','nan','none','null'):
        return None
    x = float(v)
    return x if math.isfinite(x) else None


def normalized_int(v):
    x = number(v)
    if x is None or int(x) != x:
        raise ValueError(f'Entero requerido: {v!r}')
    return int(x)


def group_keys(rows, keys, label):
    d = {}
    for row in rows:
        k = tuple(str(row[key]) for key in keys)
        if k in d:
            raise ValueError(f'Duplicado en {label}: {k}')
        d[k] = row
    return d


def verify_family_provenance(family_manifest):
    guard = family_manifest.get('guardrails', {})
    if not (guard.get('reads_post') is False and guard.get('reads_visual_labels') is False
            and guard.get('changes_live') is False and guard.get('writes_signals') is False):
        raise ValueError('El manifiesto del intérprete no declara aislamiento PRE')
    original = {
        'summary': ROOT / 'core15_phase_reconstruction_pre_v1/phase_summary_pre.csv',
        'pivots': ROOT / 'core15_phase_reconstruction_pre_v1/phase_confirmed_pivots_pre.csv',
        'legs': ROOT / 'core15_phase_reconstruction_pre_v1/phase_legs_pre.csv',
        'recovery': ROOT / 'core15_phase_reconstruction_pre_v1/phase_recovery_candidates_pre.csv',
        'resistance': RESISTANCE, 'ohlc': OHLC,
    }
    checks = family_manifest.get('source_sha256', {})
    for key, path in original.items():
        if not path.is_file() or checks.get(key) != file_hash(path):
            raise ValueError(f'La fuente {key} no corresponde al intérprete V1 congelado: {path}')
    if family_manifest.get('rows') != 150 or family_manifest.get('attempts') != 50:
        raise ValueError('El manifiesto V1 no es el cohort CORE15 esperado (50 × 3)')


def compare_sign(value):
    v = number(value)
    if v is None: return 'SIN_DATOS'
    if v > 0: return 'SUBE'
    if v < 0: return 'BAJA'
    return 'IGUAL'


def review_reference_map():
    mapped = {}
    for symbol, aid, label, provenance, batch in REVIEW_REFERENCES:
        if aid in mapped: raise ValueError(f'Visual ID duplicado: {aid}')
        mapped[aid] = {'symbol':symbol, 'attempt_id':aid, 'review_visual':label,
                       'review_provenance':provenance, 'review_batch':batch}
    return mapped


def classify_audit_flags(record):
    """Marcadores de discrepancia para inspección. Jamás calidad/trade score."""
    visual = record['review_visual']
    family = record['family_1p0']
    inter = record['intercontact_direction_48']
    net = compare_sign(record['net_return_pre48_pct'])
    out = []
    # La familia es DESCRIPTIVA; visual discrepante no prueba un falso positivo.
    if visual == 'NO_CONVENCE' and family in {'ASCENSO_ESCALONADO', 'RECONSTRUCCION_ENTRE_CONTACTOS'}:
        out.append('NO_CONVENCE_CON_FAMILIA_ASCENDENTE')
    if visual == 'CONSTRUCTIVA' and inter == 'BAJA':
        out.append('CONSTRUCTIVA_CON_INTERCONTACT_DESCENDENTE')
    if visual == 'NO_CONVENCE' and inter == 'SUBE':
        out.append('NO_CONVENCE_CON_INTERCONTACT_ASCENDENTE')
    if visual == 'NO_CONVENCE' and net == 'SUBE':
        out.append('NO_CONVENCE_CON_AVANCE_48_POSITIVO')
    if visual == 'CONSTRUCTIVA' and net == 'BAJA':
        out.append('CONSTRUCTIVA_CON_AVANCE_48_NEGATIVO')
    if inter == 'SIN_DATOS':
        out.append('INTERCONTACT_NO_EVALUABLE')
    if record['scales_agree'] == 0:
        out.append('FAMILIA_INESTABLE_ENTRE_ATR')
    if ((record['low_progression_1atr'] == 'CRECIENTE' and inter == 'BAJA') or
        (record['low_progression_1atr'] == 'DECRECIENTE' and inter == 'SUBE')):
        out.append('PIVOTES_Y_CONTACTOS_DIVERGENTES')
    if inter != 'SIN_DATOS' and net != 'SIN_DATOS' and inter in ('SUBE','BAJA') and net in ('SUBE','BAJA') and inter != net:
        out.append('CONTACTOS_Y_AVANCE_48_DIVERGENTES')
    return out


def make_matrix(families, v3, v2):
    if len(families) != 150:
        raise ValueError('Se requieren exactamente 150 interpretaciones (50 intentos × 3 ATR)')
    fam = group_keys(families, ('attempt_id','scale_atr'), 'familias')
    w3 = group_keys(v3, ('attempt_id','window_bars'), 'V3')
    w2 = group_keys(v2, ('attempt_id','window_bars'), 'V2')
    aids = sorted({r['attempt_id'] for r in families})
    if len(aids) != 50 or set(fam) != {(a,str(s)) for a in aids for s in SCALES}:
        raise ValueError('Familias incompletas, ID×escala no son los 50 × 3 esperados')
    needed = {(a,str(w)) for a in aids for w in (12,24,48,96)}
    if set(w3) != needed or set(w2) != needed:
        raise ValueError('Ventanas V2/V3 no cubren los mismos 50 intentos y 4 tamaños')
    visual = review_reference_map()
    if not set(visual).issubset(aids):
        raise ValueError('Alguna referencia visual no existe en el cohort')
    results=[]
    for aid in aids:
        rs = [fam[(aid,str(s))] for s in SCALES]
        core = rs[1]
        if any((x['symbol'],x['level_id'],x['pre_closed_ts']) !=
               (core['symbol'],core['level_id'],core['pre_closed_ts']) for x in rs):
            raise ValueError(f'Símbolo/nivel/corte no coinciden entre escalas: {aid}')
        for w in (12,24,48,96):
            for group in (w2,w3):
                src = group[(aid,str(w))]
                if src['symbol'] != core['symbol'] or normalized_int(src['pre_closed_ts']) != normalized_int(core['pre_closed_ts']):
                    raise ValueError(f'Cruce inválido por corte PRE o símbolo: {aid}/{w}')
                if src['status'] != 'COMPLETE_PRE':
                    raise ValueError(f'Ventana incompleta: {aid}/{w}')
            if w3[(aid,str(w))]['level_id'] != core['level_id']:
                raise ValueError(f'Nivel V3 inconsistente: {aid}/{w}')
        if any(x['status']!='DESCRIPTIVE_PRE_ONLY' for x in rs):
            raise ValueError(f'Se intenta usar una familia que no es descriptiva PRE: {aid}')
        r48=w3[(aid,'48')]; r96=w3[(aid,'96')]
        c24=w2[(aid,'24')]; c48=w2[(aid,'48')]; c96=w2[(aid,'96')]
        r=visual.get(aid,{'review_visual':'SIN_EVALUACION','review_provenance':'SIN_ETIQUETA','review_batch':'NO_REVISADO'})
        inter=compare_sign(r48['intercontact_trough_last_minus_first_atr'])
        if inter != core['intercontact_trough_direction_48']:
            raise ValueError(f'Dirección intercontact V3 / familias distinta: {aid}')
        if number(c48['net_close_return_pct']) is None or abs(float(c48['net_close_return_pct']) - float(core['net_return_48_pct'])) > 1e-8:
            raise ValueError(f'Retorno V2 / familia distinto: {aid}')
        row = {
            'symbol':core['symbol'],'attempt_id':aid,'level_id':core['level_id'],
            'pre_closed_ts':normalized_int(core['pre_closed_ts']),
            'pre_utc':datetime.fromtimestamp(normalized_int(core['pre_closed_ts'])/1000,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
            'review_visual':r['review_visual'],'review_provenance':r['review_provenance'],'review_batch':r['review_batch'],
            'family_0p5':rs[0]['family_candidate'],'family_1p0':rs[1]['family_candidate'],
            'family_1p5':rs[2]['family_candidate'],
            'scales_agree':int(len({x['family_candidate'] for x in rs}) == 1),
            'distinct_families':len({x['family_candidate'] for x in rs}),
            'terminal_phase_1atr':core['terminal_phase'],
            'low_progression_1atr':core['confirmed_low_progression'],
            'high_progression_1atr':core['confirmed_high_progression'],
            'low_steps_up_1atr':normalized_int(core['low_steps_up']),
            'low_steps_down_1atr':normalized_int(core['low_steps_down']),
            'high_steps_up_1atr':normalized_int(core['high_steps_up']),
            'high_steps_down_1atr':normalized_int(core['high_steps_down']),
            'intercontact_direction_48':inter,
            'intercontact_delta_48_atr':r48['intercontact_trough_last_minus_first_atr'],
            'intercontact_trough_count_48':normalized_int(r48['intercontact_troughs']),
            'touch_episodes_48':normalized_int(r48['touch_episodes']),
            'zone_center_crossings_48':normalized_int(r48['zone_center_close_crossings']),
            'zone_touch_share_48':r48['touch_bars_share'],
            'net_return_pre24_pct':c24['net_close_return_pct'],
            'net_return_pre48_pct':c48['net_close_return_pct'],
            'net_return_pre96_pct':c96['net_close_return_pct'],
            'close_path_efficiency_48':c48['close_path_efficiency'],
            'pre_close_position_in_range_96':c96['pre_close_position_in_range'],
            'first_close_relative_zone_atr_96':r96['first_close_relative_zone_atr'],
            'start_below_zone_gap_atr_96':r96['start_below_zone_gap_atr'],
            'latest_low_net_change_atr_1atr':core['latest_low_net_change_atr'],
            'latest_low_undercut_1atr':core['latest_low_undercut'],
            'evidence_favorable_1atr':core['supporting_observations'],
            'evidence_contrary_1atr':core['contradicting_observations'],
            'limitations_1atr':core['limitations'],
        }
        flags=classify_audit_flags(row)
        row['audit_flags']=' | '.join(flags)
        row['audit_flags_n']=len(flags)
        # Solo prioridad de revisión: más contradicciones; nunca calidad.
        row['audit_priority']='REVISAR' if any(x.startswith(('NO_CONVENCE_CON_FAMILIA','CONSTRUCTIVA_CON_','NO_CONVENCE_CON_INTERCONTACT')) for x in flags) else ('SIN_DATOS_CLAVE' if inter=='SIN_DATOS' else ('COMPARAR_ESCALAS' if not row['scales_agree'] else 'DESCRIPTIVA'))
        results.append(row)
    return results


def make_summary(rows):
    n=len(rows)
    review=Counter(r['review_visual'] for r in rows)
    fm=Counter(r['family_1p0'] for r in rows)
    ic=Counter(r['intercontact_direction_48'] for r in rows)
    scales=Counter(r['scales_agree'] for r in rows)
    flags=Counter(flag for r in rows for flag in r['audit_flags'].split(' | ') if flag)
    crosstab=[]
    for label in ('CONSTRUCTIVA','NO_CONVENCE','DUDOSA','SIN_EVALUACION'):
        for direction in ('SUBE','BAJA','IGUAL','SIN_DATOS'):
            q=[r for r in rows if r['review_visual']==label and r['intercontact_direction_48']==direction]
            if q: crosstab.append({'review_visual':label,'intercontact_direction_48':direction,'intentos':len(q)})
    return {'attempts':n,'level_scales':n*3,'symbols':len({r['symbol'] for r in rows}),
            'distinct_levels':len({(r['symbol'],r['level_id']) for r in rows}),
            'visual_review_counts':dict(review),'family_counts_1atr':dict(fm),
            'intercontact_direction_48':dict(ic),'scales_agree':scales.get(1,0),
            'scales_disagree':scales.get(0,0),'flag_counts':dict(flags),
            'review_vs_intercontact':crosstab,'no_post_inputs':True,
            'remarks':[
                'Familias morfológicas, no clasificación de buena/mala ni inferencia de rentabilidad.',
                'Etiquetas visuales preliminares y con origen; no son verdad-terreno definitiva.',
                'Solo 17/50 con referencia visual en TANDAS; el resto SIN_EVALUACION.',
                'Cada intento tiene 3 escalas ATR; no sumar 150 como observaciones independientes.',
                'Ventana de 48 velas ya explorada con ejemplos, no validación fuera de muestra.',
                'Comparación intercontact SIN_DATOS significa sin medida, NO significa BAJA.',
                'Los mismos símbolos/condiciones y ventanas pueden generar dependencia.',
                'No usa POST; las velas PRE de 288/384 de la vista son contexto visual solamente.',
            ]}


def write_csv(path, rows, columns):
    with path.open('w', encoding='utf-8', newline='') as f:
        w=csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def render_markdown(summary, rows):
    t=[]
    t.append('# HLL · Auditoría comparativa PRE de 50 intentos (V1)\n')
    t.append('**Interpretación morfológica exploratoria**, no detector validado de calidad. Sin POST, TP/SL ni señal operativa.\n')
    t.append(f"- Cobertura: **{summary['attempts']} intentos × 3 escalas = {summary['level_scales']} interpretaciones**; {summary['symbols']} símbolos; {summary['distinct_levels']} niveles distintos.")
    t.append(f"- Acuerdo exacto entre ATR: **{summary['scales_agree']}/50**; desacuerdos: {summary['scales_disagree']}/50.")
    t.append(f"- Referencias visuales: **{sum(v for k,v in summary['visual_review_counts'].items() if k!='SIN_EVALUACION')}/50**, con origen indicado; el resto no etiquetado.")
    t.append(f"- Comparación intercontact 48 velas: " + ', '.join(f'{k}={v}' for k,v in sorted(summary['intercontact_direction_48'].items())) + '.\n')
    t.append('## Familias PRE a 1 ATR\n')
    t.append('| Familia | Intentos |\n|---|---:|')
    for k,v in sorted(summary['family_counts_1atr'].items(), key=lambda x:(-x[1],x[0])): t.append(f'| `{k}` | {v} |')
    t.append('\n## Visual preliminar × mínimos entre contactos (48 velas)\n')
    t.append('| Revisión visual | SUBE | BAJA | IGUAL | SIN_DATOS | Total |\n|---|---:|---:|---:|---:|---:|')
    for label in ('CONSTRUCTIVA','NO_CONVENCE','DUDOSA','SIN_EVALUACION'):
        data=Counter(r['intercontact_direction_48'] for r in rows if r['review_visual']==label)
        t.append(f"| {label} | {data['SUBE']} | {data['BAJA']} | {data['IGUAL']} | {data['SIN_DATOS']} | {sum(data.values())} |")
    t.append('\n**Importante:** se muestran distribuciones descriptivas de un conjunto seleccionado; no son sensibilidad, especificidad ni validación.\n')
    t.append('## Casos prioritarios de contradicción (etiquetas preliminares)\n')
    interest=[r for r in rows if r['audit_priority']=='REVISAR']
    interest.sort(key=lambda r:(-r['audit_flags_n'],r['symbol'],r['attempt_id']))
    if not interest: t.append('No aparecieron banderas prioritarias bajo las reglas de auditoría actual.\n')
    else:
        t.append('| Símbolo | Attempt ID | Visual (origen) | Familia 1 ATR | Contactos | PRE48 (%) | Motivos de revisión |\n|---|---|---|---|---|---:|---|')
        for r in interest:
            k='; '.join(x for x in r['audit_flags'].split(' | ') if x.startswith(('NO_CONVENCE_CON_FAMILIA','CONSTRUCTIVA_CON_','NO_CONVENCE_CON_INTERCONTACT')))
            t.append(f"| {r['symbol']} | `{r['attempt_id']}` | {r['review_visual']} ({r['review_provenance']}) | {r['family_1p0']} | {r['intercontact_direction_48']} | {float(r['net_return_pre48_pct']):+.2f} | {k} |")
    t.append('\n## Verificación de casos conocidos (a 1 ATR)\n')
    known=['2221ee173c945cf17b026f2d','5c16416a996220d47fe32c96',
           '917bcd9be0ad18833727dc3d','a419c2ae10918faceff87cc4',
           'a3015ca2f41fa79ef27de2c1']
    for aid in known:
        r=next(q for q in rows if q['attempt_id']==aid)
        t.append(f"- **{r['symbol']} `{aid[:10]}…`**: visual {r['review_visual']} ({r['review_provenance']}); familia {r['family_1p0']}; mínimos/máximos {r['low_progression_1atr']}/{r['high_progression_1atr']}; intercontact {r['intercontact_direction_48']}; PRE48 {float(r['net_return_pre48_pct']):+.2f}%; escalas {'coinciden' if r['scales_agree'] else 'discrepan'}.")
    t.append('\n## Próxima decisión científica\n')
    t.append('1. Revisar **los casos con etiquetas preliminares contradictorias** y las filas con `SIN_DATOS`, sin retocar umbrales.')
    t.append('2. Etiquetar de manera ciega una muestra nueva (PRE solamente) y registrar quién evaluó cada intento.')
    t.append('3. Congelar cualquier nueva regla **antes** de evaluar casos no usados para diseñarla (preferentemente de los 290 símbolos).')
    t.append('4. Recién después, contrastar POST y resultados operativos por un protocolo independiente.\n')
    return '\n'.join(t) + '\n'


def build_from_paths():
    required=(FAMILIES,FAMILY_MANIFEST,RESISTANCE,OHLC)
    missing=[str(p) for p in required if not p.is_file()]
    if missing: raise FileNotFoundError('Faltan entradas: '+', '.join(missing))
    manifest=json.loads(FAMILY_MANIFEST.read_text(encoding='utf-8'))
    verify_family_provenance(manifest)
    data=make_matrix(read_csv(FAMILIES),read_csv(RESISTANCE),read_csv(OHLC))
    return data,make_summary(data)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    args=p.parse_args()
    rows,summary=build_from_paths()
    print('HLL_AUDIT_PLAN',json.dumps({'output':str(OUTPUT),'attempts':len(rows),
        'visual_references':len(REVIEW_REFERENCES),'scales_agree':summary['scales_agree'],
        'reads_post':False,'reads_visual_for_family':False,'updates_live':False,
        'requires_new_detector_run':False},ensure_ascii=False))
    if args.plan: return
    OUTPUT.mkdir(parents=True,exist_ok=True)
    write_csv(OUTPUT/'audit_matrix_pre.csv',rows,MATRIX_COLUMNS)
    visual_rows=[{'symbol':r[0],'attempt_id':r[1],'review_visual':r[2],
                  'review_provenance':r[3],'review_batch':r[4]} for r in REVIEW_REFERENCES]
    write_csv(OUTPUT/'visual_reference_pre.csv',visual_rows,REVIEW_COLUMNS)
    flagged=[r for r in rows if r['audit_flags']]
    flagged.sort(key=lambda r:(r['audit_priority']!='REVISAR',-r['audit_flags_n'],r['symbol'],r['attempt_id']))
    write_csv(OUTPUT/'audit_flags_pre.csv',flagged,MATRIX_COLUMNS)
    (OUTPUT/'audit_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    (OUTPUT/'audit_report.md').write_text(render_markdown(summary,rows),encoding='utf-8')
    report_manifest={
        'name':'HLL_PRE_FAMILY_AUDIT_V1','attempts':len(rows),'rows':len(rows),
        'source_sha256':{'families':file_hash(FAMILIES),'family_manifest':file_hash(FAMILY_MANIFEST),
                         'resistance':file_hash(RESISTANCE),'ohlc':file_hash(OHLC)},
        'inputs_are_pre_only':True,'reads_post':False,'reads_visual_for_family':False,
        'visual_references_are_not_ground_truth':True,'modifies_live':False,
        'classification_is_morphological_not_trade_signal':True,
    }
    (OUTPUT/'manifest.json').write_text(json.dumps(report_manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_AUDIT_DONE',json.dumps({'output':str(OUTPUT),'attempts':len(rows),
        'visual_references':len(REVIEW_REFERENCES),'scales_agree':summary['scales_agree'],
        'intercontact':summary['intercontact_direction_48'],'review_priority':sum(r['audit_priority']=='REVISAR' for r in rows)},ensure_ascii=False))

if __name__=='__main__':main()
