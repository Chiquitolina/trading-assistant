"""HLL full-290-only audit: V29 signal 30m OPEN vs actual execution 1m OPEN.

A confirmed 30m continuation/reclaim remains confirmed. Its archived 30m OPEN is
retained as source evidence in the audit and attempt-route coverage; 1m OPEN at
exactly the same timestamp is the only simulated execution price. No change to
entry timestamps, route membership, signal validators, or LIVE.
"""
from __future__ import annotations

from pathlib import Path
import math

import pandas as pd

from dashboard.views.hll_v294_entry_matrix import _read_observed_opens

AUDIT_COLUMNS = [
    'entry_id', 'attempt_id', 'symbol', 'route', 'entry_ts', 'entry_utc',
    'signal_reference_open_30m', 'execution_open_1m', 'difference_pct',
]
ROUTES_30M = frozenset(('V29_CONTINUATION_30M', 'V29_RECLAIM_30M'))
ROUTE_FLIP = 'FLIP_1M'


def reconcile_verified_opens(entries: pd.DataFrame, minute_dir: Path,
                             max_source_disagreement_pct: float = 0.1):
    """Return entries with executable 1m price and separate discrepancy audit.

    0.1% is a *source-integrity stop*, not a trading/filter tolerance: it does
    not silently discard entries or adjust when signals are recognized.
    Widening this guard requires a fresh, documented source investigation.
    """
    if not math.isfinite(max_source_disagreement_pct) or max_source_disagreement_pct <= 0:
        raise ValueError('invalid source disagreement guard')
    if entries.empty:
        return entries.copy(), pd.DataFrame(columns=AUDIT_COLUMNS)
    result = entries.copy()
    required = {'entry_id', 'attempt_id', 'symbol', 'route', 'entry_ts', 'entry_utc', 'entry_price'}
    if missing := required.difference(result.columns):
        raise ValueError('Missing entry fields: ' + str(sorted(missing)))
    if result.entry_id.isna().any() or result.entry_id.duplicated().any():
        raise ValueError('Invalid/duplicate entry_id')
    audit=[]
    for symbol, group in result.groupby('symbol', sort=True):
        stamps = set()
        for e in group.itertuples():
            if not math.isfinite(float(e.entry_ts)) or int(e.entry_ts) % 60_000:
                raise ValueError(f'Invalid entry timestamp: {e.entry_id}')
            stamps.add(int(e.entry_ts))
        archive = Path(minute_dir)/f'{symbol}_1m.csv'
        opens = _read_observed_opens(archive, stamps)
        for e in group.itertuples():
            ts=int(e.entry_ts)
            if ts not in opens:
                raise ValueError(f'Missing exact 1m entry OPEN: {e.entry_id} {symbol} {ts}')
            entry_price=float(e.entry_price)
            executed=float(opens[ts])
            if not math.isfinite(entry_price) or entry_price <= 0:
                raise ValueError(f'Invalid archived entry_price: {e.entry_id}')
            if not math.isfinite(executed) or executed <= 0:
                raise ValueError(f'Invalid exact 1m OPEN: {e.entry_id}')
            delta_pct=100*(executed/entry_price-1)
            # Preserve original exact-price source validation: never change a
            # FLIP_1M; it already must have been verified with 1m.
            mismatched=abs(executed-entry_price) > max(1e-10,abs(entry_price)*1e-7)
            if mismatched and e.route == ROUTE_FLIP:
                raise ValueError(f'FLIP_1M source changed: {e.entry_id}')
            if e.route not in ROUTES_30M and e.route != ROUTE_FLIP:
                raise ValueError(f'Unknown verified entry route: {e.route}')
            if mismatched and abs(delta_pct) > max_source_disagreement_pct:
                raise ValueError(f'30m/1m OPEN difference exceeds source guard: '
                                 f'{e.entry_id}, {symbol}, {delta_pct:.8f}%')
            if mismatched:
                audit.append(dict(entry_id=e.entry_id, attempt_id=e.attempt_id,
                    symbol=symbol, route=e.route, entry_ts=ts, entry_utc=e.entry_utc,
                    signal_reference_open_30m=entry_price, execution_open_1m=executed,
                    difference_pct=delta_pct))
            # Always simulate from the observed executable 1m open, even when
            # variation is within strict floating-point equality tolerances.
            result.at[e.Index, 'entry_price']=executed
    return result, pd.DataFrame(audit,columns=AUDIT_COLUMNS)
