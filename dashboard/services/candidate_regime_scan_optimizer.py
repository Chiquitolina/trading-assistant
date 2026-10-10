"""Exact one-slot / 80%-margin candidate scanner accelerator.

The UI and final inspections still use the original portfolio engine. This
one-slot specialization keeps the engine's chronological exit-before-entry,
same-minute Room -> Strength ordering, compounding and net execution PnL.
Canary parity in app.py disables it for the scan if the historical engine differs.
No optional dependencies beyond pandas/numpy.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

SCAN_VERSION = 'scan-optimized-v1'


def scanner_fingerprint(frame, feature_columns, version, variant):
    """Hash every field that can affect eligibility, feature masks or priority."""
    cols = sorted(set(feature_columns) | {
        '_entry_ts', '_effective_exit_ts', 'net_pnl_pct', 'Outcome',
        'execution_cost_pct', 'symbol', 'side', 'candidate_v1_event_key',
        'candidate_v2_event_key', 'nearest_opposing_room_pct',
        'nearest_opposing_swing_tf', 'nearest_opposing_swing_price',
        'nearest_opposing_swing_pivot_timestamp',
        'nearest_opposing_swing_confirmed_timestamp',
        'nearest_opposing_swing_actionable_timestamp',
        'candidate_v1_reaction_known_ts', 'retest_timestamp',
        'retest_close', 'reaction_price',
        'side_adjusted_strength_vs_btc_4h',
    })
    cols = [c for c in cols if c in frame.columns]
    digest = hashlib.sha256()
    digest.update(f'{SCAN_VERSION}|{version}|{variant}|{len(frame)}|'.encode())
    digest.update('|'.join(cols).encode())
    for c in cols:
        # Hash original order, index-independent (a reordering changes tie resolution).
        hashed = pd.util.hash_pandas_object(frame[c].reset_index(drop=True), index=False)
        digest.update(hashed.to_numpy(dtype=np.uint64).tobytes())
    return digest.hexdigest()[:24]


def cache_load(path):
    try:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        if obj.get('version') != SCAN_VERSION:
            return None
        return obj
    except (OSError, ValueError, TypeError):
        return None


def cache_save(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'version': SCAN_VERSION, **content}
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', delete=False, dir=path.parent,
        prefix=f'.{path.name}.', suffix='.tmp',
    ) as fd:
        tmp = Path(fd.name)
        json.dump(payload, fd, ensure_ascii=False, default=str)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class PreparedOneSlot:
    """Precompute invariant eligibility/order once; replay selected rows in O(N).

    Return the subset of portfolio fields used by automatic scanners. All
    accounting decisions match _candidate_v2_portfolio_simulation under the
    scanner's fixed settings: one slot, 80%-equity margin, x3, flow OFF.
    """

    def __init__(self, resolved, priority_function):
        self.index = resolved.index
        work = resolved.copy()
        work['_entry_ts'] = pd.to_numeric(work['_entry_ts'], errors='coerce')
        work['_effective_exit_ts'] = pd.to_numeric(work['_effective_exit_ts'], errors='coerce')
        work['_portfolio_net_pct'] = pd.to_numeric(work['net_pnl_pct'], errors='coerce')
        valid = (work['_entry_ts'].notna() & work['_effective_exit_ts'].notna()
                 & work['_portfolio_net_pct'].notna()
                 & work['_effective_exit_ts'].gt(work['_entry_ts']))
        work = work.loc[valid].sort_values(['_entry_ts', 'symbol'], kind='stable')
        if not work.index.is_unique or not resolved.index.is_unique:
            raise ValueError('Scan accelerator needs unique resolved indices')
        self.valid_pos = np.asarray(resolved.index.get_indexer(work.index), dtype=np.int64)
        # Room eligibility/strength are row-wise causal fields. Sorting once by
        # the canonical selector, then stable entry-time sorting, is equivalent
        # to applying that selector separately to every minute's group.
        prioritized = priority_function(work, 'Most HTF Room → Strength')
        prioritized = prioritized.sort_values('_entry_ts', kind='stable')
        pos = resolved.index.get_indexer(prioritized.index)
        self.order = np.asarray(pos, dtype=np.int64)
        self.entries = pd.to_numeric(resolved['_entry_ts'], errors='coerce').to_numpy(dtype=float)
        self.exits = pd.to_numeric(resolved['_effective_exit_ts'], errors='coerce').to_numpy(dtype=float)
        self.nets = pd.to_numeric(resolved['net_pnl_pct'], errors='coerce').to_numpy(dtype=float)
        self.count = len(resolved)
        self.valid = np.zeros(self.count, dtype=bool)
        self.valid[self.valid_pos] = True

    def simulate(self, blocked_mask):
        blocked = np.asarray(blocked_mask, dtype=bool)
        if len(blocked) != self.count:
            raise ValueError('Mask length differs from prepared research universe')
        positions = self.order[~blocked[self.order]]
        if len(positions) == 0:
            return {}

        # The chronological engine does not change past positions after a gate;
        # it only excludes candidates from consideration at entry time.
        entries = self.entries
        exits = self.exits
        nets = self.nets
        balance = 200.0
        active_exit = None
        active_pnl = 0.0
        raw = []
        equity = [{'timestamp': int(entries[positions[0]]),
                   'equity': 200.0, 'event': 'START'}]
        accept = 0
        skip_slot = 0
        skip_nonpositive = 0
        skip_margin = 0
        gains = []
        losses = []
        for p in positions:
            entry = int(entries[p])
            if active_exit is not None and active_exit <= entry:
                balance += active_pnl
                equity.append({'timestamp': int(active_exit),
                               'equity': float(balance), 'event': 'EXIT'})
                active_exit = None
            if balance <= 0:
                skip_nonpositive += 1
                continue
            if active_exit is not None:
                skip_slot += 1
                continue
            margin = float(balance) * 80.0 / 100.0
            if margin > max(float(balance), 0.0) * 80.0 / 100.0 + 1e-12:
                skip_margin += 1
                continue
            notional = margin * 3.0
            pnl = notional * float(nets[p]) / 100.0
            raw.append({'accepted': True, 'exit_timestamp': int(exits[p]),
                        'pnl_usd': float(pnl), 'raw_net_pnl_pct': float(nets[p])})
            accept += 1
            if pnl > 0:
                gains.append(pnl)
            elif pnl < 0:
                losses.append(pnl)
            active_exit = int(exits[p])
            active_pnl = float(pnl)
        if active_exit is not None:
            balance += active_pnl
            equity.append({'timestamp': int(active_exit),
                           'equity': float(balance), 'event': 'EXIT'})
        curve = pd.DataFrame(equity)
        curve = (curve.sort_values('timestamp', kind='stable')
                 .groupby('timestamp', as_index=False, sort=True)
                 .agg(equity=('equity', 'last'), event=('event', 'last')))
        curve['drawdown_usd'] = curve['equity'] - curve['equity'].cummax()
        curve['drawdown_pct'] = curve['drawdown_usd'] / curve['equity'].cummax().replace(0, np.nan) * 100.0
        pf = np.nan
        if losses and abs(float(sum(losses))) > 1e-12:
            pf = float(sum(gains) / abs(sum(losses)))
        elif gains:
            pf = np.inf
        summary = {
            'Starting equity': 200.0, 'Final equity': float(balance),
            'Return %': float((balance / 200.0 - 1.0) * 100.0),
            'Max drawdown %': abs(float(curve['drawdown_pct'].min())),
            'Max drawdown $': abs(float(curve['drawdown_usd'].min())),
            'Net PnL $': float(balance - 200.0),
            'Portfolio PF': pf, 'Eligible trades': int(len(positions)),
            'Accepted trades': int(accept),
            'Skipped trades': int(len(positions) - accept),
            'Skipped slot limit': int(skip_slot),
            'Skipped margin limit': int(skip_margin),
        }
        return {'summary': summary, 'ledger_raw': pd.DataFrame(raw),
                'equity_curve': curve}


def verify_parity(prepared, resolved, reference, summary_fn, stability_fn):
    """Run deterministic canaries; disable optimizer if ANY relevant field differs."""
    n = len(resolved)
    if n == 0:
        return True, 'empty'
    masks = [np.zeros(n, dtype=bool),
             np.arange(n) % 3 == 0,
             np.arange(n) % 5 < 2]
    fields = ('Eligible', 'Accepted', 'Final equity', 'Return %', 'Max DD %', 'PF')
    stability_fields = ('Below start time %', 'Underwater time %', 'Positive days %',
                        'Top 1 positive day share %', 'Top 2 positive days share %',
                        'Worst day $', 'Positive blocks / 3', 'Recovery factor')
    for mask in masks:
        fast = prepared.simulate(mask)
        slow = reference(resolved.loc[~mask])
        a = summary_fn(fast)
        b = summary_fn(slow)
        for key in fields:
            x = float(a.get(key, np.nan))
            y = float(b.get(key, np.nan))
            if not (np.isclose(x, y, rtol=1e-9, atol=1e-8, equal_nan=True)
                    or np.isinf(x) and x == y):
                return False, f'{key}: optimized={x} reference={y}'
        a = stability_fn(fast)
        b = stability_fn(slow)
        for key in stability_fields:
            x = float(a.get(key, np.nan))
            y = float(b.get(key, np.nan))
            if not (np.isclose(x, y, rtol=1e-9, atol=1e-8, equal_nan=True)
                    or np.isinf(x) and x == y):
                return False, f'{key}: optimized={x} reference={y}'
    return True, '3 portfolio/stability parity canaries passed'
