#!/usr/bin/env python3
"""Experimental, descriptive PRE-only HLL phase-family interpreter.

Never labels setups as profitable/good, generates entries, or changes existing models.
Input is immutable snapshots of Phase PRE V1, Resistance V3 and OHLC V2.
Rules distinguish morphology rather than optimizing thresholds on reviewed examples.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

PHASE_DIR = Path('reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1')
V3_DIR = Path('reports/horizontal_level_second_ascent_pre/core15_resistance_interaction_pre_v3')
V2_DIR = Path('reports/horizontal_level_second_ascent_pre/core15_structural_approach_ohlc_pre_v2')
OUTPUT = Path('reports/horizontal_level_second_ascent_pre/core15_pre_family_interpreter_v1')
SCALES = (0.5, 1.0, 1.5)
WINDOWS = (12, 24, 48, 96)
BAR = 1800000

SOURCES = {
    'summary': PHASE_DIR / 'phase_summary_pre.csv',
    'pivots': PHASE_DIR / 'phase_confirmed_pivots_pre.csv',
    'legs': PHASE_DIR / 'phase_legs_pre.csv',
    'recovery': PHASE_DIR / 'phase_recovery_candidates_pre.csv',
    'resistance': V3_DIR / 'resistance_windows_pre.csv',
    'ohlc': V2_DIR / 'candle_windows_pre.csv',
}
FIELDS = [
    'attempt_id','symbol','level_id','pre_closed_ts','scale_atr','family_candidate',
    'family_basis','terminal_phase','confirmed_low_progression','confirmed_high_progression',
    'low_steps_up','low_steps_down','high_steps_up','high_steps_down',
    'last_confirmed_low_age_bars','latest_low_net_change_atr',
    'latest_low_gap_reduction_atr','recent_recovery_progress','latest_low_undercut',
    'intercontact_trough_delta_48_atr','intercontact_trough_direction_48',
    'intercontact_troughs_48','zone_touch_episodes_48','zone_crossings_48',
    'zone_touch_share_48','net_return_24_pct','net_return_48_pct',
    'supporting_observations','contradicting_observations',
    'limitations','recovery_anchors_considered','status',
]


def read(path):
    with path.open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def num(value):
    if value is None or value == '':
        return None
    x = float(value)
    if not math.isfinite(x):
        return None
    return x


def integer(value):
    v = num(value)
    if v is None or int(v) != v:
        raise ValueError(f'Integer expected: {value!r}')
    return int(v)


def key(row):
    return (str(row['attempt_id']), float(row['scale_atr']))


def sign(value):
    v = num(value)
    if v is None:
        return 'SIN_DATOS'
    return 'SUBE' if v > 0 else 'BAJA' if v < 0 else 'IGUAL'


def trend(pivots, n=3):
    """Ordinal pivot trajectory, no fitted thresholds."""
    if len(pivots) < n:
        return 'SIN_DATOS', 0, 0
    x = [float(p['pivot_price_close']) for p in pivots[-n:]]
    up = sum(b > a for a, b in zip(x,x[1:]))
    down = sum(b < a for a, b in zip(x,x[1:]))
    if up == n-1:
        kind = 'CRECIENTE'
    elif down == n-1:
        kind = 'DECRECIENTE'
    elif up or down:
        kind = 'MIXTA'
    else:
        kind = 'PLANA'
    return kind, up, down


def morph_family(*, low_trend, high_trend, recent_recovery, intercontact,
                 contact_count, has_confirmed_down):
    """Descriptive ordering, not setup quality or profitability prediction.

    A 'repeated' zone interaction logically needs >=2 contacts; not a
    statistically fitted cutoff. No condition excludes an attempt.
    """
    repeated = contact_count >= 2
    if low_trend == 'CRECIENTE' and high_trend == 'CRECIENTE':
        return 'ASCENSO_ESCALONADO', '3 mínimos y 3 máximos confirmados progresivos'
    if low_trend == 'DECRECIENTE' and high_trend == 'DECRECIENTE' and recent_recovery != 'SUBE':
        return 'CORRECCION_DOMINANTE', 'máximos y mínimos recientes decrecientes'
    if repeated and intercontact == 'BAJA' and low_trend != 'CRECIENTE':
        return 'OSCILACION_EN_RESISTENCIA', 'visitas reiteradas y mínimos entre contactos deteriorados'
    if recent_recovery == 'SUBE' and has_confirmed_down and intercontact == 'SUBE':
        return 'RECONSTRUCCION_ENTRE_CONTACTOS', 'recuperación tras corrección y avance de mínimos entre contactos'
    if recent_recovery == 'SUBE' and has_confirmed_down:
        return 'REBOTE_TRAS_CORRECCION', 'recuperación desde mínimo confirmado tras fase descendente'
    if recent_recovery == 'BAJA':
        return 'DETERIORO_DESDE_MINIMO', 'el cierre PRE está por debajo del último mínimo confirmado'
    return 'SECUENCIA_MIXTA_INDETERMINADA', 'no hay evidencia suficiente para una familia más específica'


def keyed(rows, fields, label):
    d = {}
    for r in rows:
        k = tuple(str(r[f]) for f in fields)
        if k in d:
            raise ValueError(f'Duplicado en {label}: {k}')
        d[k] = r
    return d


def verify(rows):
    """Fail closed on missing or mismatched PRE sources and late pivots."""
    s = keyed(rows['summary'], ('attempt_id','scale_atr'), 'phase summary')
    expected = {(a, str(scale)) for a in {r['attempt_id'] for r in rows['summary']}
                for scale in SCALES}
    # Numerical spelling '1.0' is canonical in Phase PRE CSV.
    if set(s) != expected:
        raise ValueError('Phase summary requires exactly 3 ATR scales for every attempt')
    if any(r['status'] != 'COMPLETE_PRE' for r in s.values()):
        raise ValueError('Incomplete Phase PRE')
    a = keyed(rows['resistance'], ('attempt_id','window_bars'), 'resistance')
    b = keyed(rows['ohlc'], ('attempt_id','window_bars'), 'ohlc')
    attempts = {r['attempt_id'] for r in rows['summary']}
    expected_windows = {(aid,str(n)) for aid in attempts for n in WINDOWS}
    if set(a) != expected_windows or set(b) != expected_windows:
        raise ValueError('Expected all V2/V3 PRE windows for all Phase attempts')
    for (aid,scl), r in s.items():
        pre = integer(r['pre_closed_ts'])
        if integer(r['closed_bars']) != 96:
            raise ValueError('Phase window must be 96 closed bars')
        for n in WINDOWS:
            for src in (a,b):
                x = src[(aid,str(n))]
                if x['symbol'] != r['symbol'] or integer(x['pre_closed_ts']) != pre:
                    raise ValueError('Mismatched symbol or PRE across snapshots')
                if x['status'] != 'COMPLETE_PRE':
                    raise ValueError('Incomplete V2/V3 window')
        if a[(aid,'48')]['level_id'] != r['level_id']:
            raise ValueError('Level ID mismatch V3 / phase')
    for label, index_name in (('pivots', 'pivot_index'),
                              ('legs', 'leg_index'),
                              ('recovery', 'candidate_index')):
        seen_indexes = set()
        for r in rows[label]:
            unique_key = (r['attempt_id'], str(r['scale_atr']), str(r[index_name]))
            if unique_key in seen_indexes:
                raise ValueError(f'Duplicate {label} index: {unique_key}')
            seen_indexes.add(unique_key)
            k = (r['attempt_id'],str(r['scale_atr']))
            if k not in s:
                raise ValueError(f'Unknown {label} attempt/scale')
            pre = integer(s[k]['pre_closed_ts'])
            if r['symbol'] != s[k]['symbol'] or integer(r['pre_closed_ts']) != pre:
                raise ValueError(f'Mismatched {label} PRE')
            if label == 'pivots':
                if r['status'] != 'CONFIRMED_PRE':
                    raise ValueError('Unconfirmed pivot supplied')
                if integer(r['confirmed_known_closed_ts']) > pre or integer(r['pivot_open_ts']) + BAR > pre:
                    raise ValueError('Future pivot supplied')
            elif label == 'legs':
                if r['status'] not in {'CONFIRMED_PRE','PROVISIONAL'}:
                    raise ValueError('Unexpected phase leg status')
                if integer(r['start_pivot_known_closed_ts']) > pre or integer(r['end_extreme_open_ts']) + BAR > pre:
                    raise ValueError('Future leg supplied')
                if r['status'] == 'CONFIRMED_PRE' and integer(r['end_extreme_confirmed_known_closed_ts']) > pre:
                    raise ValueError('Future confirmed leg supplied')
            else:
                if integer(r['anchor_low_known_closed_ts']) > pre or integer(r['anchor_low_open_ts']) + BAR > pre:
                    raise ValueError('Future recovery anchor supplied')
    return s,a,b


def interpret(r, pivots, legs, recoveries, resistance, ohlc):
    lows = sorted((p for p in pivots if p['pivot_type'] == 'LOW'), key=lambda x: integer(x['pivot_open_ts']))
    highs = sorted((p for p in pivots if p['pivot_type'] == 'HIGH'), key=lambda x: integer(x['pivot_open_ts']))
    lt, lu, ld = trend(lows)
    ht, hu, hd = trend(highs)
    terminal = r['terminal_leg_direction']
    if terminal not in ('UP','DOWN') or str(r['terminal_leg_provisional']) not in ('1','1.0'):
        raise ValueError('Terminal phase status inconsistent')
    latest = [p for p in recoveries if integer(p['is_latest_confirmed_low']) == 1]
    if len(latest) != 1:
        raise ValueError('Latest low recovery anchor must be unique')
    anchor = latest[0]
    if not lows or (integer(anchor['anchor_low_open_ts']) != integer(lows[-1]['pivot_open_ts'])
                    or integer(anchor['anchor_low_known_closed_ts']) != integer(lows[-1]['confirmed_known_closed_ts'])):
        raise ValueError('Latest recovery anchor does not match last confirmed LOW pivot')
    # This is descriptive context, never a "best" candidate selection.
    progress = sign(anchor['net_change_from_low_atr'])
    inter = sign(resistance['intercontact_trough_last_minus_first_atr'])
    contacts = integer(resistance['touch_episodes'])
    confirmed_down = any(lg['leg_direction'] == 'DOWN' and lg['status'] == 'CONFIRMED_PRE' for lg in legs)
    fam, basis = morph_family(low_trend=lt,high_trend=ht,recent_recovery=progress,
                             intercontact=inter,contact_count=contacts,has_confirmed_down=confirmed_down)
    supporting, contradicting, limits = [], [], []
    if lt == 'CRECIENTE': supporting.append('mínimos confirmados crecientes (últimos tres)')
    elif lt == 'DECRECIENTE': contradicting.append('mínimos confirmados decrecientes (últimos tres)')
    elif lt == 'MIXTA': limits.append('mínimos confirmados mixtos')
    else: limits.append('mínimos confirmados insuficientes / planos')
    if ht == 'CRECIENTE': supporting.append('máximos confirmados crecientes (últimos tres)')
    elif ht == 'DECRECIENTE': contradicting.append('máximos confirmados decrecientes (últimos tres)')
    elif ht == 'MIXTA': limits.append('máximos confirmados mixtos')
    else: limits.append('máximos confirmados insuficientes / planos')
    if progress == 'SUBE': supporting.append('cierre PRE recuperado desde último mínimo confirmado')
    elif progress == 'BAJA': contradicting.append('cierre PRE por debajo del último mínimo confirmado')
    if inter == 'SUBE': supporting.append('mínimo entre contactos final superior (48 velas)')
    elif inter == 'BAJA': contradicting.append('mínimo entre contactos final inferior (48 velas)')
    elif inter == 'SIN_DATOS': limits.append('mínimos entre contactos no evaluables (48 velas)')
    ret24 = sign(ohlc['net_close_return_pct'])
    if ret24 == 'BAJA': contradicting.append('avance neto PRE 48 velas negativo')
    if ret24 == 'SUBE': supporting.append('avance neto PRE 48 velas positivo')
    if terminal == 'DOWN': limits.append('último tramo descendente provisional; no invalida por sí solo')
    if integer(anchor['anchor_is_window_boundary']): limits.append('ancla del último mínimo censurada en borde de 96 velas')
    if len(recoveries) > 1: limits.append('otras recuperaciones disponibles; ninguna seleccionada retrospectivamente')
    if integer(r['level_group_attempts']) > 1: limits.append('otros intentos sobre el mismo nivel; observaciones dependientes')
    out = {
        'attempt_id':r['attempt_id'],'symbol':r['symbol'],'level_id':r['level_id'],
        'pre_closed_ts':r['pre_closed_ts'],'scale_atr':r['scale_atr'],
        'family_candidate':fam,'family_basis':basis,
        'terminal_phase':f'{terminal}_PROVISIONAL',
        'confirmed_low_progression':lt,'confirmed_high_progression':ht,
        'low_steps_up':lu,'low_steps_down':ld,'high_steps_up':hu,'high_steps_down':hd,
        'last_confirmed_low_age_bars':r['last_confirmed_low_age_bars'],
        'latest_low_net_change_atr':anchor['net_change_from_low_atr'],
        'latest_low_gap_reduction_atr':anchor['gap_reduction_atr'],
        'recent_recovery_progress':progress,
        'latest_low_undercut':anchor['later_lowest_close_undercut'],
        'intercontact_trough_delta_48_atr':resistance['intercontact_trough_last_minus_first_atr'],
        'intercontact_trough_direction_48':inter,
        'intercontact_troughs_48':resistance['intercontact_troughs'],
        'zone_touch_episodes_48':contacts,'zone_crossings_48':resistance['zone_center_close_crossings'],
        'zone_touch_share_48':resistance['touch_bars_share'],
        'net_return_24_pct':'',
        'net_return_48_pct':ohlc['net_close_return_pct'],
        'supporting_observations':' | '.join(supporting),
        'contradicting_observations':' | '.join(contradicting),
        'limitations':' | '.join(limits),
        'recovery_anchors_considered':len(recoveries),
        'status':'DESCRIPTIVE_PRE_ONLY',
    }
    return out


def build(rows):
    s,a,b = verify(rows)
    grouped = {}
    for label in ('pivots','legs','recovery'):
        d = defaultdict(list)
        for r in rows[label]: d[key(r)].append(r)
        grouped[label] = d
    results=[]
    for (_, _), sr in sorted(s.items(), key=lambda z:(z[0][0],float(z[0][1]))):
        k=key(sr)
        v=interpret(sr,grouped['pivots'][k],grouped['legs'][k],grouped['recovery'][k],
                    a[(sr['attempt_id'],'48')],b[(sr['attempt_id'],'48')])
        v['net_return_24_pct']=b[(sr['attempt_id'],'24')]['net_close_return_pct']
        results.append(v)
    if len(results) != len(s): raise ValueError('Interpretation lost records')
    counts=Counter(r['family_candidate'] for r in results)
    return results,counts


def main():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--run', action='store_true')
    opts=p.parse_args()
    missing=[str(p) for p in SOURCES.values() if not p.is_file()]
    plan={'sources':{k:str(v) for k,v in SOURCES.items()},'missing':missing,
          'output':str(OUTPUT),'reads_post':False,'reads_visual_labels':False,
          'writes_to_live':False,'updates_dashboard':False,
          'classes_are_descriptive_not_trade_signals':True}
    if missing:
        print('HLL_FAMILY_PLAN',json.dumps(plan,ensure_ascii=False));raise SystemExit(2)
    rows={k:read(v) for k,v in SOURCES.items()}
    result,counts=build(rows)
    plan.update({'attempts':len({r['attempt_id'] for r in result}),
                 'level_scales':len(result),'family_counts':dict(counts)})
    print('HLL_FAMILY_PLAN',json.dumps(plan,ensure_ascii=False))
    if opts.plan: return
    OUTPUT.mkdir(parents=True,exist_ok=True)
    out=OUTPUT/'families_pre.csv'
    with out.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(result)
    ids=defaultdict(list)
    for r in result: ids[r['attempt_id']].append(r)
    stable=[]
    for aid,group in sorted(ids.items()):
        families={r['family_candidate'] for r in group}
        stable.append({'attempt_id':aid,'symbol':group[0]['symbol'],
            'family_0p5':next(r['family_candidate'] for r in group if float(r['scale_atr'])==.5),
            'family_1p0':next(r['family_candidate'] for r in group if float(r['scale_atr'])==1.0),
            'family_1p5':next(r['family_candidate'] for r in group if float(r['scale_atr'])==1.5),
            'scales_agree':int(len(families)==1),'distinct_families':len(families)})
    out2=OUTPUT/'scale_agreement_pre.csv'
    with out2.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(stable[0]));w.writeheader();w.writerows(stable)
    manifest={'name':'HLL_PRE_FAMILY_INTERPRETER_V1','attempts':len(ids),'rows':len(result),
              'scales_atr':SCALES,'family_counts':dict(counts),
              'scale_agreement':sum(x['scales_agree'] for x in stable),
              'source_sha256':{k:digest(path) for k,path in SOURCES.items()},
              'methodology':[
                'Close-confirmed 30m pivot morphology; last three lows/highs, no label-conditioned anchors',
                'Latest confirmed LOW chosen by chronology, never by future outcome/visual fit',
                '48-bar resistance intercontact trend only when measurable; missing is not negative',
                'The 48-bar horizon was previously inspected in these examples and is exploratory, not holdout',
                'Families describe geometries; NOT calibrated good/bad or predictive probabilities',
                'Three ATR scales evaluated independently; disagreements are retained',
                'The 50 CORE15 attempts are a selected, dependent historical cohort, not holdout data'],
              'guardrails':{'reads_post':False,'reads_visual_labels':False,'changes_live':False,
                            'changes_existing_detector':False,'writes_signals':False}}
    (OUTPUT/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_FAMILY_DONE',json.dumps({'output':str(OUTPUT),'attempts':len(ids),'rows':len(result),
                                     'family_counts':dict(counts),'scale_agreement':manifest['scale_agreement']},ensure_ascii=False))

if __name__=='__main__':main()
