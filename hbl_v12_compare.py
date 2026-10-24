#!/usr/bin/env python3
"""Read-only comparison for Horizontal Breakout Lab V1.2 outputs."""
from pathlib import Path
import sys
import pandas as pd

run = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/home/multiservices-1m/reports/horizontal_breakout_lab_v1/lifecycle_v12/runs/5cbde3dd3256d2')
levels = pd.read_csv(run / 'levels.csv')
attempts = pd.read_csv(run / 'attempts.csv')
outcomes = pd.read_csv(run / 'outcomes.csv')
keys = ['attempt_id', 'level_id', 'symbol', 'signal_closed_ts']
d = attempts.merge(outcomes, on=keys, how='inner', validate='one_to_one')
assert len(d) == len(attempts) == len(outcomes), 'Join mismatch'

n_by_level = attempts.groupby('level_id').size()
print(f'RUN: {run}')
print(f'Niveles: {len(levels)} | Intentos: {len(d)} | Niveles sin intentos: {len(levels)-len(n_by_level)}')
print(f'Niveles con múltiples intentos: {(n_by_level >= 2).sum()} | Máximo intentos en un nivel: {n_by_level.max()}')
print('\nFUERZA x TIPO DE DISPARO')
print(pd.crosstab(d['strength_class'], d['trigger_kind']).to_string())
print('\nFUERZA x ESTADO POST (solo descriptivo)')
print(pd.crosstab(d['strength_class'], d['post_label']).to_string())
print('\nFUERZA x COBERTURA')
print(pd.crosstab(d['strength_class'], d['path_state']).to_string())

rows = []
for horizon in (180, 360, 1440, 2880):
    for tp in (2, 3):
        col = f'tp{tp}_h{horizon}'
        for strength, group in d.groupby('strength_class', dropna=False):
            counts = group[col].value_counts(dropna=False)
            n = len(group)
            wins = int(counts.get('TP_FIRST', 0))
            losses = int(counts.get('SL_FIRST', 0))
            no_hit = int(counts.get('NO_HIT', 0))
            ambiguous = int(counts.get('BOTH_SAME_1M', 0))
            censored = int(counts.get('CENSORED', 0))
            rows.append({
                'horizon_m': horizon, 'TP_%': tp, 'strength': strength,
                'N': n, 'TP_FIRST': wins, 'SL_FIRST': losses, 'NO_HIT': no_hit,
                'BOTH_1M': ambiguous, 'CENSORED': censored,
                'TP_RATE_ALL_%': round(100 * wins/n, 1) if n else None,
            })
print('\nTP 2% / 3% VS SL 1.5% (entrada siguiente apertura 1m)')
print(pd.DataFrame(rows).to_string(index=False))

first = d.sort_values(['level_id', 'signal_closed_ts']).drop_duplicates('level_id')
print('\nPRIMER INTENTO POR NIVEL (reducción de intentos repetidos)')
print('N:', len(first))
for horizon in (360, 2880):
    col = f'tp2_h{horizon}'
    for strength, group in first.groupby('strength_class'):
        vc = group[col].value_counts()
        print(f'{horizon}m {strength}: N={len(group)}, TP={vc.get("TP_FIRST",0)}, SL={vc.get("SL_FIRST",0)}, NO_HIT={vc.get("NO_HIT",0)}, AMBIG={vc.get("BOTH_SAME_1M",0)}, CENSORED={vc.get("CENSORED",0)}')
