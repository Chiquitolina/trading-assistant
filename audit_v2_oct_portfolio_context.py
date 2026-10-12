#!/usr/bin/env python3
"""Read-only controlled Candidate V2 October portfolio context audit.

Uses the frozen dashboard's *resolved* universe, entry/exit occupancy and
per-trade net PnL. Replaces only the ranking fields (HTF Room, Strength)
with reconstructed historical values. Does not model replay-only events.

The frozen selector is room-desc, strength-desc, symbol-asc. The exported
snapshot does not contain per-event room_causal_verified metadata; matching
the original 118 trades validates this ranking for this frozen cohort only.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ID = 'candidate_v2_event_key'
ROOM = 'nearest_opposing_room_pct'
STRENGTH = 'side_adjusted_strength_vs_btc_4h'


def simulate(rows, room_origin, strength_origin):
    room_col = f'{ROOM}_{room_origin}'
    strength_col = f'{STRENGTH}_{strength_origin}'
    selected = []
    equity = 200.0
    active_until = None
    pending_pnl = 0.0

    for minute, group in rows.groupby('entry_timestamp', sort=True):
        minute = int(minute)
        if active_until is not None and active_until <= minute:
            equity += pending_pnl
            active_until = None
            pending_pnl = 0.0

        ordered = group.sort_values(
            [room_col, strength_col, 'symbol'],
            ascending=[False, False, True],
            na_position='last',
            kind='stable',
        )
        for _, signal in ordered.iterrows():
            if active_until is not None:
                continue
            # Frozen benchmark: 80% of realized equity as margin, isolated x3.
            notional = equity * 0.8 * 3.0
            pnl = notional * float(signal['raw_net_pnl_pct']) / 100.0
            selected.append({
                'Event ID': str(signal['Event ID']),
                'entry_equity_usd': equity,
                'margin_usd': equity * 0.8,
                'notional_usd': notional,
                'pnl_usd': pnl,
            })
            active_until = int(signal['exit_timestamp'])
            pending_pnl = pnl

    if active_until is not None:
        equity += pending_pnl

    return pd.DataFrame(selected), float(equity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='/home/multiservices-1m')
    parser.add_argument('--run-id', default='20261003_20261011_c1e8bb5f165f_df_room_240_168_150_v1')
    parser.add_argument('--reference-dir', default='refs/v2_oct_20261011')
    args = parser.parse_args()
    project = Path(args.project)
    refs = Path(args.reference_dir)
    if not refs.is_absolute():
        refs = project / refs

    monitor = pd.read_csv(refs / 'oct_dashboard_snapshot_monitor.csv', low_memory=False)
    portfolio = pd.read_csv(refs / 'oct_dashboard_snapshot_portfolio.csv', low_memory=False)
    historical = pd.read_csv(
        project / 'reports/candidate_long_horizon/runs' / args.run_id / 'events.csv',
        low_memory=False,
    )

    assert ID in monitor.columns and ID in historical.columns
    assert monitor[ID].is_unique and historical[ID].is_unique
    assert portfolio['Event ID'].is_unique

    ref_accepted = portfolio['accepted'].astype(str).str.strip().str.lower().eq('true')
    original = portfolio.loc[ref_accepted].copy().sort_values('entry_timestamp', kind='stable')
    expected = list(original['Event ID'].astype(str))

    base = portfolio.merge(
        monitor[[ID, ROOM, STRENGTH]],
        left_on='Event ID', right_on=ID, how='left', validate='one_to_one', indicator=True,
    )
    if not base['_merge'].eq('both').all():
        raise SystemExit('ERROR: some frozen portfolio events are absent from monitor')
    base = base.drop(columns=['_merge'])
    base = base.merge(
        historical[[ID, ROOM, STRENGTH]],
        on=ID, how='left', validate='one_to_one', suffixes=('_monitor', '_replay'), indicator=True,
    )
    if not base['_merge'].eq('both').all():
        raise SystemExit('ERROR: some frozen portfolio events are absent from historical replay')
    base = base.drop(columns=['_merge'])

    for col in ['entry_timestamp', 'exit_timestamp', 'raw_net_pnl_pct',
                f'{ROOM}_monitor', f'{ROOM}_replay',
                f'{STRENGTH}_monitor', f'{STRENGTH}_replay']:
        base[col] = pd.to_numeric(base[col], errors='coerce')
    essential = ['entry_timestamp', 'exit_timestamp', 'raw_net_pnl_pct']
    if base[essential].isna().any().any():
        raise SystemExit('ERROR: incomplete frozen entry/exit/PnL data')

    print('=== FROZEN COHORT ===')
    print('Resolved events:', len(base))
    print('Dashboard accepted:', len(original))
    print('Replay-only events are EXCLUDED from this controlled test.')

    scenarios = [
        ('Frozen dashboard: Room + Strength', 'monitor', 'monitor'),
        ('Replay Room + dashboard Strength', 'replay', 'monitor'),
        ('Dashboard Room + replay Strength', 'monitor', 'replay'),
        ('Replay Room + replay Strength', 'replay', 'replay'),
    ]
    for label, room_from, strength_from in scenarios:
        selected, equity = simulate(base, room_from, strength_from)
        got = list(selected['Event ID'])
        missing = set(expected) - set(got)
        extra = set(got) - set(expected)
        print('\n===', label, '===')
        print('Accepted:', len(selected))
        print('Event IDs matching dashboard:', len(set(got) & set(expected)), '/', len(expected))
        print('Same ordered trade sequence:', got == expected)
        print('Final equity: %.12f' % equity)
        print('Return: %.6f%%' % ((equity / 200.0 - 1.0) * 100.0))
        print('Missing reference IDs:', len(missing), '| Additional IDs:', len(extra))
        if missing:
            print('First missed IDs:', sorted(missing)[:5])
        if extra:
            print('First additional IDs:', sorted(extra)[:5])

        if room_from == strength_from == 'monitor':
            if got != expected:
                raise SystemExit('FAIL: frozen dashboard trade selection does not match')
            joined = original.merge(selected, on='Event ID', suffixes=('_ref','_reproduced'), validate='one_to_one')
            for col in ['entry_equity_usd', 'margin_usd', 'notional_usd', 'pnl_usd']:
                x = joined[f'{col}_ref'].to_numpy(dtype=float)
                y = joined[f'{col}_reproduced'].to_numpy(dtype=float)
                if not np.allclose(x, y, rtol=0, atol=1e-8):
                    raise SystemExit('FAIL: frozen dashboard ' + col + ' mismatch')
            reference_equity = 200.0 + original['pnl_usd'].sum()
            if not np.isclose(equity, reference_equity, rtol=0, atol=1e-8):
                raise SystemExit('FAIL: final equity mismatch')
            print('PASS: all 118 accepted, per-trade equity/margin/PnL and final equity agree')

    print('\nAUDIT COMPLETE (read-only)')
    print('NOTE: This validates ranking on the frozen resolved cohort only;')
    print('it does not certify full historical signal coverage or missing room-causality flags.')


if __name__ == '__main__':
    main()
