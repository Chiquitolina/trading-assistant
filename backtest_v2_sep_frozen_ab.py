#!/usr/bin/env python3
"""Offline, read-only-source Candidate V2 Sep-2026 frozen A OR B portfolio audit.

Reads the enriched historical event CSV. Reuses the EXACT current dashboard
portfolio functions via AST extraction, without executing Streamlit UI code.
Only writes new diagnostic reports; source CSV / snapshots remain unchanged.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

RUN = '20260907_20261006_28cb79f0702a'
REL_DATA = 'reports/candidate_long_horizon/diagnostics/v2_sep_2026/events_strength_ab_context.csv'
REL_REPORT = 'reports/candidate_long_horizon/diagnostics/v2_sep_2026/portfolio_frozen_ab'
THRESHOLD_A = -0.4494
THRESHOLD_B = 47.2414
ENGINE_FUNCTIONS = (
    '_candidate_v2_room_causality_components',
    '_candidate_v2_market_flow_gate_mask',
    '_candidate_v2_portfolio_stop_loss_pct',
    '_candidate_v2_portfolio_priority',
    '_candidate_v2_portfolio_simulation',
)
SETTINGS = dict(
    starting_equity=200.0, leverage=3.0, max_slots=1,
    risk_per_trade_pct=0.25, selected_sl_pct=3.0, max_margin_pct=80.0,
    compound=True, priority_mode='Most HTF Room → Strength',
    sizing_mode='Margin % equity', fixed_margin_usd=150.0,
    margin_per_trade_pct=80.0, market_flow_gate_mode='OFF',
)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_canonical_portfolio(app_path: Path):
    """Extract only five existing functions; never import/execute dashboard app."""
    tree = ast.parse(app_path.read_text(encoding='utf-8'), filename=str(app_path))
    by_name = {
        n.name: n
        for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    absent = set(ENGINE_FUNCTIONS) - set(by_name)
    if absent:
        raise RuntimeError(f'Missing canonical functions in {app_path}: {sorted(absent)}')
    selected = []
    for name in ENGINE_FUNCTIONS:
        node = copy.deepcopy(by_name[name])
        node.decorator_list = []  # Remove @st.cache_data; no Streamlit import.
        selected.append(node)
    mod = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(mod)
    namespace = {'pd': pd, 'np': np, 'TZ': 'America/Argentina/Buenos_Aires'}
    exec(compile(mod, str(app_path), 'exec'), namespace)
    return namespace['_candidate_v2_portfolio_simulation']


def audit_events(frame: pd.DataFrame):
    required = [
        'candidate_v1_event_key','symbol','side','entry_timestamp',
        'exit_timestamp','Outcome','net_pnl_pct','execution_cost_pct',
        'side_adjusted_strength_vs_btc_4h','btc_return_pct_4h',
        'btc_return_pct_4h_rebuilt','btc_return_delta_4h',
        'breadth_delta_4h','ab_context_available',
        'nearest_opposing_room_pct','retest_timestamp',
    ]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise RuntimeError(f'Missing columns in enriched events CSV: {missing}')

    numeric = lambda col: pd.to_numeric(frame[col], errors='coerce')
    strength = numeric('side_adjusted_strength_vs_btc_4h')
    selected = frame.loc[strength.gt(0)].copy()
    if selected.empty:
        raise RuntimeError('No Strength > 0 rows')
    selected = selected.reset_index(drop=True)
    num = lambda col: pd.to_numeric(selected[col], errors='coerce')

    for col in ['btc_return_delta_4h','breadth_delta_4h','net_pnl_pct',
                'entry_timestamp','exit_timestamp','execution_cost_pct']:
        if num(col).isna().any():
            raise RuntimeError(f'Missing {col}: {int(num(col).isna().sum())} of {len(selected)} selected')

    if not (num('exit_timestamp') > num('entry_timestamp')).all():
        raise RuntimeError('Non-positive historical exit delay found: refusing to simulate')

    cost = num('execution_cost_pct')
    if not np.allclose(cost.to_numpy(), 0.10, atol=1e-9, rtol=0):
        raise RuntimeError('Replay execution fees do not match 0.10% round trip; investigate first')

    original_btc = num('btc_return_pct_4h')
    rebuilt_btc = num('btc_return_pct_4h_rebuilt')
    valid = original_btc.notna() & rebuilt_btc.notna()
    if not valid.all():
        raise RuntimeError('Not all selected events have both original/rebuilt BTC returns')
    delta = (original_btc - rebuilt_btc).abs()
    parity = {
        'compared': int(valid.sum()),
        'max_abs_diff_pp': float(delta.max()),
        'p95_abs_diff_pp': float(delta.quantile(.95)),
        'above_0_01_pp': int(delta.gt(.01).sum()),
    }
    if parity['above_0_01_pp']:
        sample = selected.loc[delta.gt(.01), ['symbol','entry_timestamp','btc_return_pct_4h','btc_return_pct_4h_rebuilt']].head(7)
        raise RuntimeError('BTC 4h historical parity failed (>0.01 pp) for '
                           + str(parity['above_0_01_pp']) + ' events. Examples:\n'
                           + sample.to_string(index=False))

    a = num('btc_return_delta_4h').le(THRESHOLD_A)
    b = num('breadth_delta_4h').ge(THRESHOLD_B)
    blocked = a | b
    selected['_entry_ts'] = num('entry_timestamp')
    selected['_effective_exit_ts'] = num('exit_timestamp')
    selected['no_trade_A'] = a
    selected['no_trade_B'] = b
    selected['no_trade_A_or_B'] = blocked
    if selected['candidate_v1_event_key'].duplicated().any():
        raise RuntimeError('Duplicate event identity, cannot simulate one signal twice')

    counts = {
        'strength_positive': int(len(selected)),
        'A_only': int((a & ~b).sum()),
        'B_only': int((~a & b).sum()),
        'both': int((a & b).sum()),
        'blocked_total': int(blocked.sum()),
        'eligible_total': int((~blocked).sum()),
    }
    return selected, blocked, parity, counts


def safe_summary(portfolio):
    if not portfolio or 'summary' not in portfolio:
        raise RuntimeError('Canonical portfolio simulator returned no summary')
    summary = portfolio['summary']
    return {str(k): (float(v) if isinstance(v,(np.integer, np.floating)) else v)
            for k,v in summary.items()}


def run_backtest(root: Path, parity_only=False):
    app_path = root / 'dashboard/app.py'
    report_dir = root / REL_REPORT
    source = root / REL_DATA
    config_path = root / 'reports/candidate_long_horizon/runs' / RUN / 'config.json'
    if not source.exists() or not app_path.exists():
        raise RuntimeError('Missing events_strength_ab_context.csv or dashboard/app.py')

    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if config:
        for k, v in {'tp_pct': 0.5, 'sl_pct': 3.0, 'horizon_min': 180}.items():
            if k in config and not np.isclose(float(config[k]), float(v)):
                raise RuntimeError(f'Historical replay {k}={config[k]} conflicts with backtest {v}')

    frame = pd.read_csv(source, low_memory=False)
    selected, blocked, parity, counts = audit_events(frame)
    config_symbols = config.get('symbols', []) if isinstance(config, dict) else []
    n_symbols = len(config_symbols) if isinstance(config_symbols, list) else None
    print('=== HISTORICAL CONTEXT PARITY ===')
    print(json.dumps(parity, indent=2))
    print('=== FROZEN PRE-PORTFOLIO SIGNAL COUNTS ===')
    print(json.dumps(counts, indent=2))
    print('Historical replay configured symbols:', n_symbols if n_symbols is not None else 'unknown')
    if n_symbols is not None and n_symbols < 290:
        print('NOTE: This is a symbol-subset PILOT, NOT a 290-symbol full-universe portfolio.')
    if parity_only:
        print('PASS: parity-only checks passed. Run with --run for portfolio simulations.')
        return

    canonical_simulator = load_canonical_portfolio(app_path)
    no_gate = canonical_simulator(selected.copy(), **SETTINGS)
    with_gate = canonical_simulator(selected.loc[~blocked].copy(), **SETTINGS)
    baseline_summary = safe_summary(no_gate)
    gate_summary = safe_summary(with_gate)
    difference = float(gate_summary['Final equity']) - float(baseline_summary['Final equity'])

    report_dir.mkdir(parents=True, exist_ok=True)
    selected.to_csv(report_dir / 'selected_strength_events_with_gate.csv',index=False)
    for name, output in [('baseline', no_gate),('frozen_ab',with_gate)]:
        output['ledger_raw'].to_csv(report_dir / f'{name}_portfolio_ledger.csv', index=False)
        output['equity_curve'].to_csv(report_dir / f'{name}_equity.csv', index=False)
    payload = {
        'source_events_sha256': sha(source),
        'source_app_sha256': sha(app_path),
        'run': RUN,
        'symbol_subset_count': n_symbols,
        'settings': SETTINGS,
        'rule': {'strength': '> 0', 'A': f'btc_return_delta_4h <= {THRESHOLD_A}',
                 'B': f'breadth_delta_4h >= {THRESHOLD_B}', 'combine':'OR'},
        'btc_4h_parity': parity,
        'candidate_counts': counts,
        'baseline':baseline_summary,
        'with_frozen_gate':gate_summary,
        'delta_equity_usd':difference,
        'limitations': [
            'This uses a historical replay reconstructed with current method, not a live-trading simulation.',
            'Historical market breadth uses the current 290-symbol catalog; historical listing/survivorship could differ.',
            'Actual market breadth parity against a past live store remains unverified.',
            'No liquidation, maintenance margin, slippage beyond historical execution cost, or funding in original simulator.',
        ],
    }
    (report_dir/'portfolio_comparison.json').write_text(json.dumps(payload, indent=2, default=str))
    print('=== BASELINE (V2 Strength > 0) ===')
    print(json.dumps({k: baseline_summary[k] for k in ('Final equity','Return %','Portfolio PF','Max drawdown %','Accepted trades')}, indent=2))
    print('=== WITH FROZEN A OR B ===')
    print(json.dumps({k: gate_summary[k] for k in ('Final equity','Return %','Portfolio PF','Max drawdown %','Accepted trades')}, indent=2))
    print('Difference in equity (frozen - baseline):', round(difference, 6))
    print('Output:', report_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', default='.', help='Git project root')
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--parity-only', action='store_true')
    group.add_argument('--run', action='store_true')
    args=parser.parse_args()
    run_backtest(Path(args.project).resolve(), parity_only=args.parity_only)


if __name__ == '__main__':
    main()
