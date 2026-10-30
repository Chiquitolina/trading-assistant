#!/usr/bin/env python3
"""HLL offline PRE-only research: consolidate confirmed 30m pivots into structural swings.

Uses ONLY frozen V1.2 PRE exports. No LIVE, dashboard, POST or outcome data.
Major swing confirmed when a confirmed opposite pivot moves >= threshold ATR;
its knowledge timestamp is the opposite pivot confirmation, not its candle open.
All alternative L-H-L-H chains are exported. The latest is descriptive, NOT
an entry signal and NOT an automatically verified second ascent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

TF = 1_800_000
ROOT = Path('reports/horizontal_level_second_ascent_pre')
SOURCE = ROOT / 'core15_two_leg_v12'
OUTPUT = ROOT / 'core15_swing_topology_pre_v1'
THRESHOLDS = (0.5, 1.0, 1.5)  # sensitivity, NOT optimized trading thresholds
KINDS = {'HIGH_2X2': 'H', 'LOW_CONFIRMED_PRE': 'L'}


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Pivot:
    kind: str
    open_ts: int
    known_ts: int
    price: float


def load_pivots(records, pre, atr):
    """Validate source confirmed pivots, removing ambiguous dual-extreme bars.

    All original pivots remain in the source V1.2 export. We explicitly flag
    dual H/L on the same candle because intrabar order is not observable.
    """
    pre = int(pre)
    if not np.isfinite(float(atr)) or float(atr) <= 0:
        raise ValueError('BAD_ATR')
    valid = []
    for r in records:
        kind = KINDS.get(str(r['kind']))
        if kind is None:
            raise ValueError('UNEXPECTED_KIND')
        op = int(r['open_ts']); known = int(r['known_closed_ts']); price = float(r['price'])
        if not (np.isfinite(price) and price > 0 and op % TF == 0
                and op < pre and op + TF <= known <= pre):
            raise ValueError('INVALID_CONFIRMED_PIVOT')
        valid.append(Pivot(kind, op, known, price))
    unique = {(p.kind, p.open_ts, p.known_ts) for p in valid}
    if len(unique) != len(valid):
        raise ValueError('DUPLICATE_PIVOT')
    kinds_by_open = {}
    for p in valid:
        kinds_by_open.setdefault(p.open_ts, set()).add(p.kind)
    ambiguous = sorted(t for t, kinds in kinds_by_open.items() if len(kinds) > 1)
    keep = [p for p in valid if p.open_ts not in ambiguous]
    keep.sort(key=lambda p: (p.known_ts, p.open_ts, p.kind))
    # 2x2 confirmation from this dataset is normally 3 candles after pivot open.
    # The explicit known_ts check remains the causal authority regardless.
    return keep, ambiguous


def structural_swings(pivots, atr, threshold):
    """Causal, alternating swing reducer without forward data.

    Once an opposite confirmed pivot exceeds the amplitude threshold, the
    preceding extreme is finalized AS OF the opposite pivot's KNOWN timestamp.
    The final candidate is only a pivot, never a finalized structural swing.
    """
    if atr <= 0 or threshold <= 0:
        raise ValueError('BAD_THRESHOLD_OR_ATR')
    confirmed = []
    pending = None
    for p in pivots:
        if pending is None:
            pending = p
            continue
        if p.known_ts < pending.known_ts:
            raise ValueError('NON_CAUSAL_PIVOT_ORDER')
        if p.kind == pending.kind:
            better = (p.price > pending.price if p.kind == 'H' else p.price < pending.price)
            if better:
                pending = p
            continue
        move = (pending.price - p.price if pending.kind == 'H'
                else p.price - pending.price) / atr
        if move >= threshold:
            confirmed.append(dict(kind=pending.kind,
                                  open_ts=pending.open_ts,
                                  pivot_known_ts=pending.known_ts,
                                  swing_known_ts=p.known_ts,
                                  price=pending.price,
                                  reversal_atr=round(float(move), 6),
                                  status='SWING_CONFIRMED'))
            pending = p
    provisional = None
    if pending is not None:
        provisional = dict(kind=pending.kind,
                           open_ts=pending.open_ts,
                           pivot_known_ts=pending.known_ts,
                           swing_known_ts=None,
                           price=pending.price,
                           reversal_atr=None,
                           status='PIVOT_ONLY_UNCONFIRMED_SWING')
    return confirmed, provisional


def enumerate_two_leg_sequences(swings, provisional, atr, floor, ceiling, pre):
    """Enumerate L-H-L-H chains; do not choose by known visual labels or outcome.

    A provisional final H is allowed but is flagged as a pivot-only H2.
    All displayed stages have their real knowledge times by PRE, including
    any later swing confirmation. No attempt to infer intrabar ordering.
    """
    if not (atr > 0 and 0 < floor <= ceiling):
        raise ValueError('BAD_ZONE')
    s = [v for v in swings if int(v['swing_known_ts']) <= pre]
    if provisional and provisional['pivot_known_ts'] <= pre:
        s.append(provisional)
    results = []
    for j in range(len(s) - 3):
        leg = s[j:j + 4]
        if [v['kind'] for v in leg] != ['L', 'H', 'L', 'H']:
            continue
        if not all(leg[k]['open_ts'] < leg[k + 1]['open_ts'] for k in range(3)):
            continue
        l0, h1, l1, h2 = leg
        a = (h1['price'] - l0['price']) / atr
        corr = (h1['price'] - l1['price']) / atr
        b = (h2['price'] - l1['price']) / atr
        if a <= 0 or corr <= 0 or b <= 0:
            continue
        results.append(dict(
            l0_open_ts=l0['open_ts'], l0_known_ts=l0['swing_known_ts'], l0_price=l0['price'],
            h1_open_ts=h1['open_ts'], h1_known_ts=h1['swing_known_ts'], h1_price=h1['price'],
            l1_open_ts=l1['open_ts'], l1_known_ts=l1['swing_known_ts'], l1_price=l1['price'],
            h2_open_ts=h2['open_ts'], h2_pivot_known_ts=h2['pivot_known_ts'],
            h2_swing_known_ts=h2['swing_known_ts'], h2_price=h2['price'],
            h2_status=h2['status'],
            first_rise_atr=round(a, 4), correction_atr=round(corr, 4),
            second_rise_atr=round(b, 4),
            correction_low_vs_first_low_atr=round((l1['price'] - l0['price']) / atr, 4),
            h2_vs_h1_atr=round((h2['price'] - h1['price']) / atr, 4),
            h2_distance_zone_atr=round((floor - h2['price']) / atr, 4),
            first_rise_bars=int((h1['open_ts'] - l0['open_ts']) / TF),
            correction_bars=int((l1['open_ts'] - h1['open_ts']) / TF),
            second_rise_bars=int((h2['open_ts'] - l1['open_ts']) / TF),
            all_pivots_known_pre=True,
            full_structural_confirmation_pre=(h2['status'] == 'SWING_CONFIRMED'),
        ))
    return results


def analyze(attempts, pivot_rows, thresholds=THRESHOLDS):
    if attempts.attempt_id.duplicated().any():
        raise ValueError('DUPLICATED_ATTEMPT_IN_SEGMENTS')
    if pivot_rows.empty:
        raise ValueError('EMPTY_PIVOTS')
    if not set(pivot_rows.attempt_id).issubset(set(attempts.attempt_id)):
        raise ValueError('PIVOT_WITHOUT_SEGMENT')
    by_id = {k: g.to_dict('records') for k, g in pivot_rows.groupby('attempt_id')}
    summary = []; events = []; candidates = []
    for r in attempts.to_dict('records'):
        aid = str(r['attempt_id']); pre = int(r['pre_closed_ts'])
        atr = float(r['atr_pre']); floor = float(r['zone_low']); ceiling = float(r['zone_high'])
        if not (np.isfinite([atr, floor, ceiling]).all() and atr > 0 and 0 < floor <= ceiling):
            raise ValueError('INVALID_SEGMENT_PRICE_FIELDS')
        records = by_id.get(aid, [])
        if not records:
            raise ValueError('MISSING_ATTEMPT_PIVOTS ' + aid)
        if any(int(p['pre_closed_ts']) != pre for p in records):
            raise ValueError('PIVOT_PRE_MISMATCH ' + aid)
        ps, ambiguous = load_pivots(records, pre, atr)
        for th in thresholds:
            swings, pending = structural_swings(ps, atr, float(th))
            seq = enumerate_two_leg_sequences(swings, pending, atr, floor, ceiling, pre)
            common = dict(attempt_id=aid, symbol=r['symbol'], pre_closed_ts=pre,
                          pre_utc=r.get('pre_utc', ''), visual_label=r.get('visual_label', ''),
                          threshold_atr=th)
            for event in swings + ([pending] if pending else []):
                events.append(dict(**common, **event))
            for idx, pattern in enumerate(seq):
                candidates.append(dict(**common, chain_id=idx + 1, **pattern))
            # 'Latest' is deterministic and based strictly on the most recent
            # H2 candle, NOT best match against manually reviewed labels.
            latest = max(seq, key=lambda x:(x['h2_open_ts'], x['l0_open_ts'])) if seq else None
            summary.append(dict(**common,
                input_confirmed_pivots=len(records),
                excluded_dual_extreme_bars=len(ambiguous),
                structurally_confirmed_swings=len(swings),
                pending_swing_kind=pending['kind'] if pending else '',
                total_LHLH_chains=len(seq),
                chains_h2_last12_bars=sum((pre-v['h2_open_ts'])/TF<=12 for v in seq),
                chains_h2_last24_bars=sum((pre-v['h2_open_ts'])/TF<=24 for v in seq),
                latest_chain_h2_age_bars=(int((pre-latest['h2_open_ts'])/TF) if latest else np.nan),
                pre_distance_zone_atr=r.get('pre_distance_zone_atr', np.nan),
                latest_chain_h2_open_ts=(latest['h2_open_ts'] if latest else np.nan),
                latest_chain_h2_status=(latest['h2_status'] if latest else ''),
                latest_chain_second_rise_atr=(latest['second_rise_atr'] if latest else np.nan),
                latest_chain_h2_distance_zone_atr=(latest['h2_distance_zone_atr'] if latest else np.nan),
                latest_chain_l1_vs_l0_atr=(latest['correction_low_vs_first_low_atr'] if latest else np.nan),
                warning=('HAS_SAME_BAR_DUAL_EXTREME' if ambiguous else 'NONE')))
    return pd.DataFrame(summary), pd.DataFrame(events), pd.DataFrame(candidates)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    a = parser.add_mutually_exclusive_group(required=True)
    a.add_argument('--plan', action='store_true')
    a.add_argument('--run', action='store_true')
    parser.add_argument('--source-dir', type=Path, default=SOURCE)
    parser.add_argument('--output-dir', type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    needed = [args.source_dir / n for n in ('segments_pre.csv', 'all_confirmed_pivots_pre.csv')]
    missing = [str(x) for x in needed if not x.is_file()]
    manifest = args.source_dir / 'manifest.json'
    if not manifest.exists():
        missing.append(str(manifest))
    plan = dict(source=str(args.source_dir), output=str(args.output_dir),
                missing=missing, read_only_inputs=True, no_live=True,
                no_dashboard_changes=True, sensitivity_atr=list(THRESHOLDS))
    print('HLL_SWING_PLAN', json.dumps(plan, ensure_ascii=False), flush=True)
    if args.plan:
        return 0
    if missing:
        print('HLL_SWING_ERROR MISSING_INPUT', missing)
        return 2
    dest = args.output_dir.resolve()
    source = args.source_dir.resolve()
    if dest == source or dest in source.parents or source in dest.parents:
        raise ValueError('OUTPUT_SOURCE_OVERLAP')
    if dest.exists():
        raise ValueError('OUTPUT_EXISTS_NON_OVERWRITE ' + str(dest))
    if shutil.disk_usage(Path.cwd()).free < 30 * 1024**2:
        raise ValueError('DISK_FREE_BELOW_30MB')
    attempts = pd.read_csv(needed[0], low_memory=False)
    pivots = pd.read_csv(needed[1], low_memory=False)
    summary, events, chains = analyze(attempts, pivots)
    tallies = (summary.groupby('threshold_atr').agg(
        levels=('attempt_id', 'size'),
        with_chain=('total_LHLH_chains', lambda col: int((col > 0).sum())),
        same_bar_ambiguous=('excluded_dual_extreme_bars', lambda col: int((col > 0).sum()))
    ).reset_index())
    # No visual labels drive selection; report exploratory labels only to
    # compare and falsify the hypothesis. Not a positive-class classifier.
    reviewed = summary[summary.visual_label.ne('SIN_REVISAR')][[
        'symbol','attempt_id','visual_label','threshold_atr',
        'excluded_dual_extreme_bars','structurally_confirmed_swings',
        'total_LHLH_chains','chains_h2_last12_bars','latest_chain_h2_age_bars',
        'latest_chain_h2_status',
        'latest_chain_second_rise_atr','latest_chain_h2_distance_zone_atr']]
    # Guarantee data consistency: no endpoints or structural confirmation POST.
    if not events.empty and (events.pivot_known_ts.astype('int64') > events.pre_closed_ts.astype('int64')).any():
        raise AssertionError('POST_LEAK_EVENTS')
    if not chains.empty and (chains.h2_pivot_known_ts.astype('int64') > chains.pre_closed_ts.astype('int64')).any():
        raise AssertionError('POST_LEAK_CHAINS')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='.hll-swing-', dir=dest.parent))
    try:
        summary.to_csv(temp/'swing_summary_pre.csv', index=False)
        events.to_csv(temp/'structural_swings_pre.csv', index=False)
        chains.to_csv(temp/'all_two_leg_chains_pre.csv', index=False)
        reviewed.to_csv(temp/'reviewed_cases_pre.csv', index=False)
        tallies.to_csv(temp/'sensitivity_pre.csv', index=False)
        report = dict(**plan,
            levels=int(attempts.shape[0]),
            input_pivots=int(pivots.shape[0]),
            source_segments_sha256=sha256_file(needed[0]),
            source_pivots_sha256=sha256_file(needed[1]),
            sensitivity=tallies.to_dict('records'),
            warning=('Exploratory and breakout-conditioned; no PRE universe of non-breakouts. '
                'Raw 2x2 pivots confirmed as-of PRE, structural swings are confirmed '
                'only at reversal known timestamp. H2 may be pivot-only. '
                'No visual-positive classifier, PnL, POST or LIVE.'))
        (temp/'manifest.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf8')
        if dest.exists():
            raise ValueError('OUTPUT_EXISTS_NON_OVERWRITE')
        os.rename(temp, dest)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    print('\n=== SENSIBILIDAD DE SWINGS PRE ===')
    print(tallies.to_string(index=False))
    print('\n=== CASOS VISUALES CONOCIDOS ===')
    print(reviewed.to_string(index=False))
    print('\nHLL_SWING_DONE', json.dumps(dict(output=str(dest), levels=len(attempts),
              confirmed_pivot_rows=len(pivots)), ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
