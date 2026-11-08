#!/usr/bin/env python3
"""HLL structural-zone importance and historical-map selection V2.1 (research).

Read-only, prefix-causal: consumes snapshot_zones at a CLOSED 30m cutoff plus
its V1 events; never rewrites HLL V1/V2 frozen results. The relative grade is
NOT a validated trading signal and does NOT claim an absolute structural truth.

Unlike MAX_MEMBER V2, evidence uses *deduplicated zone-level episodes* from
snapshot_zones. The grade is relative to the universe of causal subzones at
that cutoff. Historical-map selection is a retrospective visualization at
that cutoff; it must never be presented as a rating known at the chart's start.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

TF = 1_800_000
DAY = 86_400_000
TESTS = frozenset(('TEST_FROM_ABOVE', 'TEST_FROM_BELOW'))
TIERS = {'A': 0, 'B': 1, 'C': 2, 'D': 3}


def _independent(timestamps: list[int], gap_ms: int = 4 * TF) -> list[int]:
    result = []
    for ts in sorted(set(int(t) for t in timestamps)):
        if not result or ts - result[-1] >= gap_ms:
            result.append(ts)
    return result


def _members(value) -> tuple[str, ...]:
    ids = json.loads(value) if isinstance(value, str) else value
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError('Miembros de subzona incorrectos')
    if any(not isinstance(x, str) or not x for x in ids):
        raise ValueError('level_id inválido en subzona')
    return tuple(ids)


def rank_zone_evidence(zones: pd.DataFrame, events: pd.DataFrame, cutoff: int,
                       window_start: int) -> pd.DataFrame:
    """Compute a non-saturated, relative as-of priority without outcome labels.

    Test, defense, and loss counts are zone-level episodes already deduplicated
    by V2. Loss is evidence of *interaction*, NOT proof the level is defended.
    Current state is retained separately without subtracting it from evidence.
    Phase describes which part of the visible *past* had most confirmed tests.
    """
    if cutoff % TF or window_start >= cutoff:
        raise ValueError('Corte o ventana histórica inválidos')
    if zones.empty:
        return zones.copy()
    required = {'zone_id', 'level_ids', 'members', 'created_at', 'center',
                'tests_since_zone_birth', 'defenses_since_birth',
                'losses_since_birth', 'age_days', 'tier', 'importance_score'}
    if not required.issubset(zones.columns):
        raise ValueError('Faltan campos V2 de subzona causal')
    if zones.zone_id.astype(str).duplicated().any():
        raise ValueError('zone_id repetido')
    e = events.loc[events.event_type.isin(TESTS) &
                   (events.observed_closed_ts.astype('int64') <= cutoff),
                   ['level_id', 'event_type', 'observed_closed_ts']].copy()
    event_by_line = defaultdict(list)
    for row in e.itertuples(index=False):
        event_by_line[str(row.level_id)].append((int(row.observed_closed_ts),
                                                  str(row.event_type)))
    all_rows = []
    third = (cutoff-window_start) / 3
    for row in zones.itertuples(index=False):
        ids = _members(row.level_ids)
        if int(row.members) != len(ids):
            raise ValueError('Cardinalidad de subzona incompatible')
        born = int(row.created_at)
        if born > cutoff:
            raise ValueError('La subzona todavía no existía al corte')
        tests = int(row.tests_since_zone_birth)
        defenses = int(row.defenses_since_birth)
        losses = int(row.losses_since_birth)
        age = float(row.age_days)
        if min(tests, defenses, losses) < 0 or not math.isfinite(age) or age < 0:
            raise ValueError('Evidencia de zona inválida')
        # Diminishing returns; unlike old 0..100 fixed sum, this NEVER clamps.
        evidence = (16.0*math.log1p(tests) +
                    6.0*math.log1p(defenses + losses) +
                    4.0*math.log1p(min(age, 60.0)/7.0) +
                    2.5*math.log1p(len(ids)))
        phase_tests = [0, 0, 0]
        last_contact = None
        for typ in TESTS:
            times = _independent([t for name in ids
                     for t, kind in event_by_line[name]
                     if kind == typ and born <= t <= cutoff])
            for t in times:
                if last_contact is None or t > last_contact:
                    last_contact = t
                if t > window_start:
                    phase = min(2, int((t-window_start) / third))
                    phase_tests[phase] += 1
        # No visible test: place in the phase in which zone was first formed.
        phase = (int(np.argmax(phase_tests)) if any(phase_tests) else
                 min(2, max(0, int((max(born, window_start)-window_start)/third))))
        all_rows.append(dict(zone_id=str(row.zone_id), v2_tier=str(row.tier),
              v2_score=int(row.importance_score), evidence_index=round(evidence, 3),
              tests_in_window=sum(phase_tests), phase_early=phase_tests[0],
              phase_middle=phase_tests[1], phase_late=phase_tests[2],
              primary_phase=phase, last_test_closed_ts=last_contact,
              zone_kind='SUBZONA_MULTILINEA' if len(ids)>1 else 'LINEA_INDIVIDUAL'))
    extra = pd.DataFrame(all_rows)
    # percentiles compare *zones* (not constituent lines), with deterministic
    # equal-ranking behavior. 100 = highest percentile, not 100 score points.
    extra['relative_percentile'] = (extra.evidence_index.rank(method='average', pct=True)*100).round(1)
    lookup = {r.zone_id:r for r in zones.itertuples(index=False)}
    def grade(r):
        n = int(lookup[r.zone_id].tests_since_zone_birth)
        p = float(r.relative_percentile)
        if len(extra)>=5 and p>=85.0 and n>=3:
            return 'A'
        if p>=60.0 and n>=2:
            return 'B'
        if p>=30.0 and n>=1:
            return 'C'
        return 'D'
    extra['relative_tier'] = extra.apply(grade,axis=1)
    result = zones.merge(extra,on='zone_id',how='left',validate='one_to_one')
    result['evidence_method'] = 'LOG_DEDUP_CAUSAL_RELATIVE_V21_EXPLORATORY'
    return result.sort_values(['evidence_index','zone_id'],
                       ascending=[False,True],kind='stable').reset_index(drop=True)


def select_historical_map(ranked: pd.DataFrame, limit: int = 10,
                          spacing_pct: float = 0.70) -> pd.DataFrame:
    """Balance early/mid/late confirmed historical zones and price bands.

    Selection is as-of-cutoff, not a signal at historical chart timestamps.
    Returns <= limit; NEVER forces overlapping near-duplicate prices to fill.
    All other zones remain inspectable in the separate full selector/table.
    """
    if limit < 1 or not 0.05 <= spacing_pct <= 5.0:
        raise ValueError('Límite o distancia de selección inválidos')
    if ranked.empty:return ranked.copy()
    needed={'zone_id','center','evidence_index','primary_phase','relative_tier'}
    if not needed.issubset(ranked.columns):
        raise ValueError('Faltan valores de prioridad V2.1')
    if ranked.zone_id.astype(str).duplicated().any():
        raise ValueError('Zona duplicada en mapa')
    df=ranked.sort_values(['evidence_index','zone_id'],ascending=[False,True],kind='stable')
    chosen=[]
    def can_pick(row):
        c=float(row.center)
        return c>0 and math.isfinite(c) and all(
          100*abs(c-float(old.center))/min(c,float(old.center))>=spacing_pct for old in chosen)
    # First pass: historical coverage if each phase has an eligible zone.
    for phase in range(3):
        if len(chosen)>=limit:break
        for r in df.itertuples(index=False):
            if int(r.primary_phase)==phase and can_pick(r):
                chosen.append(r);break
    # Second: strongest remaining zones without duplicate price concentration.
    for r in df.itertuples(index=False):
        if len(chosen)>=limit:break
        if r.zone_id in {x.zone_id for x in chosen}:continue
        if can_pick(r):chosen.append(r)
    if not chosen:return ranked.iloc[0:0].copy()
    ids=[x.zone_id for x in chosen]
    return ranked.set_index('zone_id',drop=False).loc[ids].reset_index(drop=True).copy()
