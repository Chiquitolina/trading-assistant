"""HLL full-290-only audit: V29 signal 30m OPEN vs actual execution 1m OPEN.

A confirmed 30m continuation/reclaim remains confirmed. Its archived 30m OPEN is
retained as source evidence in the audit and attempt-route coverage; 1m OPEN at
exactly the same timestamp is the only simulated execution price. No change to
entry timestamps, route membership, signal validators, or LIVE.
"""
from __future__ import annotations

from pathlib import Path
import math
import json

import pandas as pd

from dashboard.views.hll_v294_entry_matrix import _read_observed_opens

AUDIT_COLUMNS = [
    'entry_id', 'attempt_id', 'symbol', 'route', 'entry_ts', 'entry_utc',
    'signal_reference_open_30m', 'execution_open_1m', 'difference_pct',
]
ROUTES_30M = frozenset(('V29_CONTINUATION_30M', 'V29_RECLAIM_30M'))
ROUTE_FLIP = 'FLIP_1M'

# The full FIRST_PER_LEVEL 290-symbol preflight (11,694 verified entries)
# identified exactly these three over-guard source conflicts on 2026-10-28.
# They are *not* resolved with an independently verified exchange tape.
# For OFFLINE research only, these three exact signals may be simulated from
# archived exact-timestamp 1m opens and MUST stay in the source audit.
# Any new, modified, or wrong-time source difference still hard-stops.
DOCUMENTED_SOURCE_CONFLICTS = {
    '355e8982760189778d4eedc8': dict(
        attempt_id='25dd7962127baeb5a35b6f8f', symbol='GOATUSDT',
        route='V29_CONTINUATION_30M', entry_ts=1789921800000,
        reference=0.0168, minute=0.01676),
    'c4f114c1c55fed843e0cf254': dict(
        attempt_id='8bec1dd324dff5dd065bafc2', symbol='FLUIDUSDT',
        route='V29_CONTINUATION_30M', entry_ts=1790134200000,
        reference=1.443, minute=1.441),
    '31df38a818bb7a2c455057f7': dict(
        attempt_id='04462d9791df3cdd3b1241ec', symbol='OGUSDT',
        route='V29_CONTINUATION_30M', entry_ts=1790335800000,
        reference=2.921, minute=2.924),
}
DEFAULT_30M_DIR = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')


def _documented_exception(e, reference, minute, candles_30m_dir):
    """Accept only a frozen preflight match with corroborating original 30m OPEN.

    Only protects the existing research from a systemic 30m/1m *OPEN* equality
    assumption. No interpolation, no resampling, no fill-price substitution.
    """
    rec = DOCUMENTED_SOURCE_CONFLICTS.get(str(e.entry_id))
    if not rec:
        return False
    for key in ('attempt_id', 'symbol', 'route'):
        if str(getattr(e, key)) != rec[key]:
            raise ValueError(f'Documented source exception identity changed: {e.entry_id}: {key}')
    if int(e.entry_ts) != rec['entry_ts']:
        raise ValueError(f'Documented source exception timestamp changed: {e.entry_id}')
    if not math.isclose(float(reference), rec['reference'], rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError(f'Documented source exception 30m signal OPEN changed: {e.entry_id}')
    if not math.isclose(float(minute), rec['minute'], rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError(f'Documented source exception 1m execution OPEN changed: {e.entry_id}')
    archive = Path(candles_30m_dir) / f"{rec['symbol']}_30m.csv"
    if not archive.is_file():
        raise FileNotFoundError(f'Documented source exception missing 30m archive: {archive}')
    candles = pd.read_csv(archive, usecols=['timestamp', 'open'])
    matching = candles.loc[pd.to_numeric(candles.timestamp, errors='raise') == rec['entry_ts']]
    if len(matching) != 1 or not math.isclose(
        float(matching.open.iloc[0]), rec['reference'], rel_tol=1e-10, abs_tol=1e-12
    ):
        raise ValueError(f'Documented source exception original 30m archive changed: {e.entry_id}')
    return True


def reconcile_verified_opens(entries: pd.DataFrame, minute_dir: Path,
                             max_source_disagreement_pct: float = 0.1,
                             candles_30m_dir: Path = DEFAULT_30M_DIR):
    """Return entries with executable 1m price and separate discrepancy audit.

    0.1% is a *source-integrity stop*, not a trading/filter tolerance: it does
    not silently discard entries or adjust when signals are recognized.
    Three precise, dated FIRST_PER_LEVEL preflight conflicts are permitted
    only when their identities and original 30m archive remain unchanged;
    all remain visible in the existing source-audit CSV.
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
                if not _documented_exception(e, entry_price, executed, candles_30m_dir):
                    raise ValueError(f'30m/1m OPEN difference exceeds source guard: '
                                     f'{e.entry_id}, {symbol}, {delta_pct:.8f}%')
                print('HLL_OPEN_DOCUMENTED_SOURCE_CONFLICT', json.dumps({
                    'entry_id': str(e.entry_id), 'symbol': symbol, 'route': str(e.route),
                    'entry_ts': ts, 'signal_open_30m': entry_price,
                    'execution_open_1m': executed, 'difference_pct': delta_pct,
                    'evidence': 'FIRST_PER_LEVEL full290 preflight + unchanged archived 30m open',
                    'disposition': 'OFFLINE_SIMULATION_ONLY_FLAGGED_FOR_SENSITIVITY',
                }, ensure_ascii=False), flush=True)
            if mismatched:
                audit.append(dict(entry_id=e.entry_id, attempt_id=e.attempt_id,
                    symbol=symbol, route=e.route, entry_ts=ts, entry_utc=e.entry_utc,
                    signal_reference_open_30m=entry_price, execution_open_1m=executed,
                    difference_pct=delta_pct))
            # Always simulate from the observed executable 1m open, even when
            # variation is within strict floating-point equality tolerances.
            result.at[e.Index, 'entry_price']=executed
    return result, pd.DataFrame(audit,columns=AUDIT_COLUMNS)
