"""Read-only exploratory multidimensional rule discovery for Continuous V1.

Causal features only, fixed predeclared buckets, full contiguous-horizon outcomes.
No Redis, writes, trade signals, runner-only sampling, or post-event features.
Discovery scores are descriptive, not p-values or evidence of profitable trades.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
import math

import numpy as np
import pandas as pd

from reaction_lab_runner_analyzer import FACTORS, NUMERIC_FACTORS, TARGETS, _group_bucket

# Data-quality labels / capture modalities are not market features: exclude from
# automated mining even though they remain available in manual diagnostics.
EXCLUDED_SEARCH_FACTORS = frozenset({'BTC context status', 'Tipo de observación', 'Calidad microestructura'})
DEFAULT_PATTERN_FACTORS = (
    'Lado', 'HTF Room mínimo', 'HTF Room 1h',
    'Strength BTC 4h (direccional)', 'Breadth 1h', 'Delta breadth 1h',
    'Market Flow', 'Flow 4h', 'Volumen relativo 15m',
    'RSI 15m', 'Departure pre-retest', 'Fuerza sector vs BTC 4h',
    'BTC Corr 5m / 4h', 'BTC Beta 5m / 4h', 'BTC residual 1h',
    'BTC velocidad 15m (%)', 'BTC trade alignment',
    'Departure velocidad (%/min)', 'Departure eficiencia',
    'Retest mecha rechazo', 'Retest volumen vs 30m',
    'ATR 1m (%)', 'Densidad REACTIONs 15m',
)
MAX_FACTORS = 24


@dataclass(frozen=True)
class Condition:
    factor: str
    bucket: str


def _temporal_partition(frame, horizon, min_n):
    """Split by UTC DAY, purging any training outcome overlapping validation."""
    ts = pd.to_numeric(frame.get('known_ts'), errors='coerce')
    if ts is None or len(ts) != len(frame):
        raise ValueError('Missing known_ts for temporal partition')
    ts = ts.to_numpy(dtype=float)
    valid_ts = np.isfinite(ts)
    days = pd.to_datetime(ts[valid_ts], unit='ms', utc=True).floor('D')
    unique = sorted(days.unique())
    full = np.ones(len(frame), dtype=bool)
    if len(unique) < 5:
        return full, np.zeros(len(frame), dtype=bool), {'mode': 'EXPLORATORIO', 'days': len(unique), 'embargo': 0}
    cutoff = unique[max(1, int(len(unique)*.70))]
    cutoff_ms = cutoff.value / 1_000_000
    train = valid_ts & (ts + horizon*60_000 < cutoff_ms)
    valid = valid_ts & (ts >= cutoff_ms)
    min_train = max(40, min_n*3)
    min_valid = max(20, min_n)
    if train.sum() < min_train or valid.sum() < min_valid:
        return full, np.zeros(len(frame), dtype=bool), {'mode': 'EXPLORATORIO', 'days': len(unique), 'embargo': 0, 'reason':'too_few_events_per_period'}
    return train, valid, {
        'mode': 'TEMPORAL', 'days':len(unique), 'cutoff_utc':str(cutoff),
        'embargo':int((~train & ~valid).sum()),
        'train_events':int(train.sum()), 'validation_events':int(valid.sum()),
    }


def _factor_values(frame, factors, train_mask, min_n):
    masks = {}
    valid_masks = {}
    singles = []
    for factor in factors:
        if factor not in FACTORS or factor in EXCLUDED_SEARCH_FACTORS:
            raise ValueError('Invalid or noncausal/disallowed discovery factor: '+str(factor))
        if FACTORS[factor] not in frame.columns:
            continue
        bucket = _group_bucket(frame, factor)
        valid_masks[factor] = bucket.notna().to_numpy(dtype=bool)
        counts = bucket[train_mask].value_counts(dropna=True)
        # Avoid very rare, excessively common or stale/capture-bias buckets.
        total_valid = int((valid_masks[factor] & train_mask).sum())
        if total_valid < min_n*2:
            continue
        for label, cnt in counts.items():
            if cnt < min_n or cnt > max(min_n, int(.95*total_valid)):
                continue
            cond = Condition(factor, str(label))
            masks[cond] = bucket.eq(label).fillna(False).to_numpy(dtype=bool)
            singles.append(cond)
    return masks, valid_masks, singles


def _score_rule(conditions, matches, valid_masks, train_mask, y, min_n):
    scope = train_mask.copy()
    for cond in conditions:
        scope &= valid_masks[cond.factor]
    selected = scope & matches
    control = scope & ~matches
    n = int(selected.sum())
    n_control = int(control.sum())
    if n < min_n or n_control < min_n:
        return None
    hits = int(y[selected].sum())
    ctrl_hits = int(y[control].sum())
    rate = 100.0*hits/n
    ctrl = 100.0*ctrl_hits/n_control
    diff = rate - ctrl
    if not math.isfinite(diff) or abs(diff) < 1e-12:
        return None
    score = abs(diff) * math.sqrt(n/(n+min_n)) * math.sqrt(n_control/(n_control+min_n))
    return {'conditions':conditions, 'mask':matches, 'n':n, 'hits':hits,
            'ctrl_n':n_control, 'ctrl_hits':ctrl_hits,
            'rate':rate, 'ctrl_rate':ctrl, 'diff':diff, 'score':score}


def _rule_order(rule):
    return (-rule['score'], -rule['n'], tuple((c.factor,c.bucket) for c in rule['conditions']))


def _holdout(rule, validation_mask, valid_masks, y, min_val):
    scope = validation_mask.copy()
    for cond in rule['conditions']:
        scope &= valid_masks[cond.factor]
    selected = scope & rule['mask']
    control = scope & ~rule['mask']
    n, cn = int(selected.sum()), int(control.sum())
    result = {'n_validation':n, 'hits_validation':int(y[selected].sum()),
              'n_validation_control':cn, 'hits_validation_control':int(y[control].sum()),
              'rate_validation_pct':math.nan, 'control_validation_pct':math.nan,
              'diff_validation_pp':math.nan, 'validation':'SIN MUESTRA'}
    if n < min_val or cn < min_val:
        return result
    rate = 100*result['hits_validation']/n
    ctrl = 100*result['hits_validation_control']/cn
    delta = rate-ctrl
    result.update(rate_validation_pct=rate, control_validation_pct=ctrl,
                  diff_validation_pp=delta,
                  validation=('MISMO SIGNO' if delta*rule['diff']>0 else 'SIGNO INVERSO' if delta*rule['diff']<0 else 'SIN DIFERENCIA'))
    return result


def discover_runner_patterns(frame, *, target=2., horizon=180, min_n=20,
                             factors=None, depth=3, top_each=25, beam=45):
    """Beam search over fixed causal buckets; train-only selection, day holdout.

    Baseline is the subset with all selected factors available (not the entire
    cohort), preventing absent BTC features from contaminating comparisons.
    Returns positive, negative, metadata. Sorting is ALWAYS by train results.
    """
    if target not in TARGETS:raise ValueError('Unsupported target')
    if depth not in (1,2,3):raise ValueError('Depth must be 1, 2 or 3')
    if not 5 <= min_n <= 1000:raise ValueError('Invalid minimum sample')
    if not 1 <= top_each <= 100:raise ValueError('Invalid top')
    requested = tuple(factors if factors is not None else DEFAULT_PATTERN_FACTORS)
    if not requested or len(requested)>MAX_FACTORS:raise ValueError('Select 1-24 factors')
    factors = tuple(dict.fromkeys(requested))
    for factor in factors:
        if factor not in FACTORS or factor in EXCLUDED_SEARCH_FACTORS:
            raise ValueError('Noncausal or unsupported factor: '+str(factor))
    col = f'hit_{str(target).replace(".","_")}pct'
    needed = ('event_key','known_ts',col)
    if any(key not in frame.columns for key in needed):
        raise ValueError('Expected one row per eligible event with known_ts and outcome')
    if frame.event_key.duplicated().any():raise ValueError('Duplicate Event IDs')
    if frame[col].isna().any():raise ValueError('Incomplete outcome is not eligible')
    working = frame.reset_index(drop=True)
    y = working[col].astype(bool).to_numpy()
    train, validation, split = _temporal_partition(working, horizon, min_n)
    masks, valid_masks, singles = _factor_values(working, factors, train, min_n)
    discovered = []
    evaluated = 0
    candidates = []

    def evaluate(conditions, mask):
        nonlocal evaluated
        evaluated += 1
        return _score_rule(conditions, mask, valid_masks, train, y, min_n)

    for cond in singles:
        rule = evaluate((cond,), masks[cond])
        if rule is not None:
            discovered.append(rule)
    if depth >= 2:
        pairs = []
        for a,b in combinations(singles,2):
            if a.factor == b.factor:continue
            mask = masks[a] & masks[b]
            if int((mask & train).sum()) < min_n:continue
            rule = evaluate((a,b), mask)
            if rule:
                discovered.append(rule)
                pairs.append(rule)
        if depth == 3 and pairs:
            # Explore promising positive AND negative parents; held-out dates
            # are never consulted for candidate selection.
            positive = sorted((r for r in pairs if r['diff']>0),key=_rule_order)[:beam]
            negative = sorted((r for r in pairs if r['diff']<0),key=_rule_order)[:beam]
            seen = set()
            for parent in positive+negative:
                used = {c.factor for c in parent['conditions']}
                for cond in singles:
                    if cond.factor in used:continue
                    candidate = tuple(sorted(parent['conditions']+(cond,),key=lambda c:(c.factor,c.bucket)))
                    if candidate in seen:continue
                    seen.add(candidate)
                    mask = parent['mask'] & masks[cond]
                    if int((mask & train).sum()) < min_n:continue
                    rule = evaluate(candidate,mask)
                    if rule:discovered.append(rule)
    min_val = max(5, min_n//2)
    def table(records, sign):
        rows=[]
        # Different bucket names can select exactly the same Event IDs.
        # Collapse such redundant discoveries; validation is never used here.
        seen_signatures=set()
        for rule in sorted((r for r in records if r['diff']*sign>0),key=_rule_order):
            scope=train.copy()
            for condition in rule['conditions']:
                scope &= valid_masks[condition.factor]
            signature=np.packbits(scope & rule['mask']).tobytes()
            if signature in seen_signatures:continue
            seen_signatures.add(signature)
            if len(rows)>=top_each:break
            row = {
                'Regla':' ∧ '.join(f'{c.factor} = {c.bucket}' for c in rule['conditions']),
                'N factores':len(rule['conditions']),
                'N exploración':rule['n'], 'Runners exploración':rule['hits'],
                'Controles grupo':rule['n']-rule['hits'],
                'Tasa grupo %':round(rule['rate'],2),
                'N resto comparable':rule['ctrl_n'],
                'Tasa resto %':round(rule['ctrl_rate'],2),
                'Diferencia pp':round(rule['diff'],2),
                'Lift vs resto':round(rule['rate']/rule['ctrl_rate'],3) if rule['ctrl_rate']>0 else math.nan,
                'Score exploratorio':round(rule['score'],3),
            }
            if split['mode']=='TEMPORAL':
                check=_holdout(rule,validation,valid_masks,y,min_val)
                row.update({'N validación':check['n_validation'],
                            'Runners validación':check['hits_validation'],
                            'Tasa validación %':round(check['rate_validation_pct'],2),
                            'Resto validación %':round(check['control_validation_pct'],2),
                            'Diferencia validación pp':round(check['diff_validation_pp'],2),
                            'Validación':check['validation']})
            rows.append(row)
        return pd.DataFrame(rows)

    meta={**split,'train_base':int(train.sum()),'validation_base':int(validation.sum()),
          'baseline_train_rate_pct':round(100*float(y[train].mean()),2) if train.any() else math.nan,
          'factors_requested':len(factors),'factors_with_buckets':len({c.factor for c in singles}),
          'buckets':len(singles),'candidate_evaluations':evaluated,
          'valid_rules':len(discovered),'depth':depth,'min_n':min_n,
          'warnings':('La búsqueda prueba muchas combinaciones: las diferencias descubiertas pueden ser azar. '
                      'Señales simultáneas no son independientes; no son pruebas de rentabilidad.')}
    return table(discovered,+1), table(discovered,-1), meta


def match_rule(frame, rule_text):
    """Select exactly the fixed buckets of a displayed pattern. No evaluation."""
    if not isinstance(rule_text, str) or not rule_text:
        raise ValueError('Empty rule')
    selected = np.ones(len(frame), dtype=bool)
    seen = set()
    for expression in rule_text.split(' ∧ '):
        if ' = ' not in expression:
            raise ValueError('Malformed rule')
        factor, bucket = expression.split(' = ', 1)
        if factor not in FACTORS or factor in EXCLUDED_SEARCH_FACTORS or factor in seen:
            raise ValueError('Invalid factor in rule')
        seen.add(factor)
        if FACTORS[factor] not in frame:
            return np.zeros(len(frame), dtype=bool)
        selected &= _group_bucket(frame, factor).eq(bucket).fillna(False).to_numpy(dtype=bool)
    return selected
