#!/usr/bin/env python3
"""HLL: offline PRE-only two-leg segmentation, no signal or trade execution.

Source cohort is restricted to the previously explored 96-broken / 24+48-intact
FIRST-line audit, not all pre-breakout opportunities. Metrics are descriptive,
not probabilities, selection filters, or an out-of-sample test.

Author: experimental research extension; never imports LIVE or dashboard.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

TF = 1_800_000
AUDIT = Path('reports/horizontal_level_relaxed_hl_sequence/core15_initial_line_new_hl/sequence_pre.csv')
COHORT = Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT = Path('reports/horizontal_level_second_ascent_pre/core15_two_leg_v12')
SEED = 'd169b99bd00b4a7a21bd5dcb'
# Visual annotations are exploratory, not ground truth. Some reflect assistant
# assessments of screenshots rather than explicit user acceptance.
LABELS = {
    SEED: 'REFERENCIA_INICIAL',
    'ffcb487cc3523ae7d1c48dbe': 'BUENO_PRELIMINAR',
    '2221ee173c945cf17b026f2d': 'BUENO_PRELIMINAR',
    '81bcd23506a40114b2eba209': 'BUENO_PRELIMINAR',
    '5c16416a996220d47fe32c96': 'BUENO_PRELIMINAR',
    '46fb73a27f33aa7102a573d4': 'NO_CONVENCE_PRELIMINAR',
    '917bcd9be0ad18833727dc3d': 'NO_CONVENCE_ASISTENTE',
    'f1fbe367222fe92d05ef6d4': 'NO_CONVENCE_ASISTENTE',
    'a8cd48b0f38f97d50a91be6d': 'NO_CONVENCE_ASISTENTE',
    'b7b84e538c0fbe8799b2762a': 'NO_CONVENCE_ASISTENTE',
    '79abb398695ef676214afb84': 'BUENO_ASISTENTE',
    '1555fb36b41799c19b23272d': 'DUDOSO_ASISTENTE',
}


def hashfile(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_candles(path):
    bars = pd.read_csv(path, usecols=['timestamp', 'open', 'high', 'low', 'close'])
    if bars.empty:
        raise ValueError(f'No candles: {path}')
    for col in bars.columns:
        bars[col] = pd.to_numeric(bars[col], errors='raise')
    bars['timestamp'] = bars.timestamp.astype('int64')
    bars = bars.sort_values('timestamp', kind='stable').reset_index(drop=True)
    if bars.timestamp.duplicated().any() or (bars.timestamp % TF != 0).any():
        raise ValueError(f'Duplicate / unaligned candles: {path}')
    o, h, l, c = (bars[k].to_numpy(float) for k in ['open', 'high', 'low', 'close'])
    if not (np.isfinite(bars[['open','high','low','close']].to_numpy(float)).all()
            and (np.minimum.reduce([o,h,l,c]) > 0).all()
            and (l <= np.minimum(o,c)).all()
            and (h >= np.maximum(o,c)).all()
            and (l <= h).all()):
        raise ValueError(f'Invalid OHLC: {path}')
    return bars


def prefix_candles(bars, pre, n=96):
    """Return EXACTLY n continuous closed 30m candles ending at PRE."""
    pre = int(pre)
    expected = np.arange(pre - n * TF, pre, TF, dtype='int64')
    sub = bars[(bars.timestamp >= expected[0]) & (bars.timestamp < pre)].copy()
    if len(sub) != n or not np.array_equal(sub.timestamp.to_numpy('int64'), expected):
        raise ValueError(f'GAP_30M_PRE={pre}: expected={n}, got={len(sub)}')
    return sub.reset_index(drop=True)


def confirmed_pivot_highs(bars, pre):
    """2-left / 2-right pivot, known only after right-hand bars CLOSE."""
    a = bars.high.to_numpy(float)
    t = bars.timestamp.to_numpy('int64')
    result = []
    for j in range(2, len(bars) - 2):
        known = int(t[j + 2]) + TF
        if (known <= pre and a[j] > max(a[j-2:j])
                and a[j] > max(a[j+1:j+3])):
            result.append(dict(open_ts=int(t[j]), known_closed_ts=known,
                               high=float(a[j])))
    return result


def load_known_lows(raw, pre, window_start):
    if not isinstance(raw, str):
        raise ValueError('Missing PRE lows JSON')
    pivots = json.loads(raw)
    if not isinstance(pivots, list):
        raise ValueError('PRE lows must be a list')
    out = []
    for p in pivots:
        op, known, low = int(p['open_ts']), int(p['known_closed_ts']), float(p['low'])
        if not (np.isfinite(low) and low > 0):
            raise ValueError('Invalid pivot low')
        if window_start <= op < pre and known <= pre:
            out.append(dict(open_ts=op, known_closed_ts=known, low=low))
    out.sort(key=lambda v: v['open_ts'])
    if len(set(p['open_ts'] for p in out)) != len(out):
        raise ValueError('Duplicated confirmed pivot lows')
    return out


def select_candidates(a, c):
    """Recreate prior Group A comparison; one row per (symbol, level_id)."""
    needed = ['attempt_id','symbol','level_id','window_30m','line_state','sequence',
              'slope_reduced','zone_reapproach_before_breakout',
              'initial_slope_atr_per_bar','slope_ratio_to_initial',
              'low_vs_last_anchor_atr','pre_closed_ts','initial_break_closed_ts',
              'new_hl_open_ts','new_hl_known_closed_ts','new_hl_low',
              'anchor_first_ts','anchor_third_ts','anchor_confirmed_ts',
              'original_pre_pattern']
    missing = [v for v in needed if v not in a]
    if missing:
        raise ValueError('Missing audit fields: '+str(missing))
    required_c = ['attempt_id','atr_pre','zone_low','zone_high','pre96_pivot_lows_json']
    if any(v not in c for v in required_c):
        raise ValueError('Missing cohort columns')
    if a.duplicated(['attempt_id','window_30m']).any() or c.attempt_id.duplicated().any():
        raise ValueError('Repeated audit or cohort IDs')
    states = (a[a.window_30m.isin([24,48])]
        .pivot(index='attempt_id', columns='window_30m', values='line_state')
        .rename(columns={24:'state24',48:'state48'}).reset_index())
    q = a[a.window_30m.eq(96)].merge(states, on='attempt_id', validate='one_to_one')
    q = q[(q.line_state == 'INICIAL_ROTA') &
          (q.state24 == 'INICIAL_INTACTA') &
          (q.state48 == 'INICIAL_INTACTA') &
          (q.sequence == 'ROTA_NUEVO_HL_ESTRICTO') &
          q.slope_reduced.astype(str).str.lower().eq('true') &
          q.zone_reapproach_before_breakout.astype(str).str.lower().eq('true') &
          (q.initial_slope_atr_per_bar > 0) & (q.slope_ratio_to_initial > 0)].copy()
    if len(q) == 0:
        raise ValueError('Empty Group A cohort')
    seeds = q[q.attempt_id == SEED]
    if len(seeds) != 1:
        raise ValueError('Reference AGLD seed not present exactly once')
    s = seeds.iloc[0]
    q['similarity'] = (np.abs(np.log(q.initial_slope_atr_per_bar / s.initial_slope_atr_per_bar))
                       + np.abs(np.log(q.slope_ratio_to_initial / s.slope_ratio_to_initial))
                       + .25*np.abs(q.low_vs_last_anchor_atr - s.low_vs_last_anchor_atr))
    q = q.sort_values(['similarity','attempt_id']).drop_duplicates(['symbol','level_id'])
    q = q.merge(c[required_c], on='attempt_id', validate='one_to_one')
    if q[['symbol','level_id']].duplicated().any():
        raise AssertionError('Duplicate symbol/level')
    return q


def segment_one(row, bars):
    """Return PRE diagnostics and extrema, with explicit unresolved-phase flags.

    A higher low need NOT be the absolute correction trough. A missing first
    rise within the selected 96-bar window is censored, not a negative label.
    Neither situation is proof of a complete two-leg geometry.
    """
    pre, broken, hl_open, hl_known = (int(row[k]) for k in [
        'pre_closed_ts','initial_break_closed_ts','new_hl_open_ts','new_hl_known_closed_ts'])
    first, third, anchor_known = (int(row[k]) for k in [
        'anchor_first_ts','anchor_third_ts','anchor_confirmed_ts'])
    atr, floor, ceiling, hl_low = (float(row[k]) for k in [
        'atr_pre','zone_low','zone_high','new_hl_low'])
    if not (np.isfinite([atr,floor,ceiling,hl_low]).all()
            and atr > 0 and 0 < floor <= ceiling and hl_low > 0):
        raise ValueError('BAD_ATR_ZONE')
    if not (pre-96*TF <= first <= third < anchor_known <= broken <= hl_open < hl_known <= pre):
        raise ValueError('INVALID_CAUSAL_ORDER')
    x = prefix_candles(bars, pre)
    lows = load_known_lows(row['pre96_pivot_lows_json'], pre, pre-96*TF)
    highs = confirmed_pivot_highs(x, pre)
    anchor = x[x.timestamp == first]
    if len(anchor) != 1:
        raise ValueError('FIRST_ANCHOR_OUTSIDE_PRE')
    anchor_low = float(anchor.iloc[0].low)
    first_anchor_pivots = [p for p in lows if p['open_ts'] == first]
    if len(first_anchor_pivots) != 1:
        raise ValueError('FIRST_ANCHOR_PIVOT_MISMATCH')
    hl = [p for p in lows if p['open_ts'] == hl_open]
    if len(hl) != 1 or not np.isclose(hl[0]['low'], hl_low, rtol=1e-6, atol=0):
        raise ValueError('NEW_HL_PIVOT_MISMATCH')

    # First leg: best HIGH within early ascent before the new HL. This is a
    # descriptive PRE retrospective extremum, not a tradable timestamp signal.
    prior = x[(x.timestamp >= first) & (x.timestamp < hl_open)]
    if len(prior) < 2:
        raise ValueError('FIRST_LEG_TOO_SHORT')
    p1 = prior.loc[prior.high.idxmax()]
    peak1_ts, peak1 = int(p1.timestamp), float(p1.high)
    first_rise_visible = bool(peak1_ts > first and peak1 > anchor_low + atr*.1)
    first_peak_confirmed = any(p['open_ts'] == peak1_ts for p in highs)

    # PRE-descriptive correction, starting AFTER the largest preceding high.
    # An audited NEW HL may be higher than an earlier, deeper correction low;
    # this is a potentially relevant staircase, not a reason to drop the case.
    correction = x[(x.timestamp > peak1_ts) & (x.timestamp <= hl_open)]
    if correction.empty:
        raise ValueError('NO_SEPARATING_CORRECTION')
    trough_bar = correction.loc[correction.low.idxmin()]
    trough_ts = int(trough_bar.timestamp)
    trough_low = float(trough_bar.low)
    gap_atr = max(0., (hl_low - trough_low)/atr)
    earlier_lower_trough = bool(trough_ts < hl_open and gap_atr > 1e-7)
    trough_pivot = next((p for p in lows
                         if p['open_ts'] == trough_ts and
                         np.isclose(p['low'], trough_low, rtol=1e-6, atol=0)), None)
    if not first_rise_visible and earlier_lower_trough:
        phase_status = 'FIRST_RISE_UNRESOLVED_AND_EARLIER_LOWER_TROUGH'
    elif not first_rise_visible:
        phase_status = 'FIRST_RISE_UNRESOLVED'
    elif earlier_lower_trough:
        phase_status = 'EARLIER_LOWER_CORRECTION_TROUGH'
    else:
        phase_status = 'AUDIT_HL_IS_CORRECTION_TROUGH'
    # NaN means the first rise is NOT established by this window.
    old_rise = (peak1-anchor_low)/atr if first_rise_visible else np.nan
    correction_depth = (peak1-hl_low)/atr
    correction_true_depth = (peak1-trough_low)/atr

    # Second leg: the low at hl_open is the anchor. Market readings start at
    # hl_known; prices prior to this may describe geometry but cannot be used
    # to trigger a signal before this confirmation timestamp.
    second = x[(x.timestamp >= hl_known) & (x.timestamp+TF <= pre)]
    if len(second) < 3:
        raise ValueError('SECOND_KNOWN_PHASE_TOO_SHORT')
    p2 = second.loc[second.high.idxmax()]
    peak2_ts, peak2 = int(p2.timestamp), float(p2.high)
    last_close = float(x.close.iloc[-1])
    closes = second.close.to_numpy(float)
    close_peak_idx = int(np.argmax(closes))
    path = closes[:close_peak_idx+1]
    path_abs = float(np.abs(np.diff(path)).sum())
    path_eff = ((float(path[-1]-path[0])/path_abs)
                if path_abs > 0 and len(path)>1 else np.nan)
    # First post-HL pivot may be known after it forms; ALL are known by PRE.
    phase_lows = [hl[0]] + [p for p in lows if p['open_ts'] > hl_open]
    vals = np.array([p['low'] for p in phase_lows])
    steps = np.diff(vals)/atr
    up, down = int((steps >= .1).sum()), int((steps <= -.1).sum())
    run = best = 0
    for v in steps:
        run = run+1 if v >= .1 else 0
        best = max(best,run)
    above = second.close >= floor
    crosses = int((above != above.shift()).iloc[1:].sum())
    near = (second.close >= floor-.5*atr) & (second.close <= ceiling+.25*atr)
    peak2_progress = (peak2-hl_low)/atr
    retained = ((last_close-hl_low)/(peak2-hl_low)*100) if peak2>hl_low else np.nan
    undid = bool((second.low < hl_low-.1*atr).any())
    phase_extrema = [
        dict(kind='FIRST_LOW',open_ts=first,known_closed_ts=first_anchor_pivots[0]['known_closed_ts'],
             price=anchor_low,confirmed_pre=True),
        dict(kind='FIRST_PEAK',open_ts=peak1_ts,
             known_closed_ts=next((p['known_closed_ts'] for p in highs if p['open_ts']==peak1_ts),None),
             price=peak1,confirmed_pre=first_peak_confirmed),
        dict(kind='CORRECTION_BAR_TROUGH',open_ts=trough_ts,
             known_closed_ts=(trough_pivot['known_closed_ts'] if trough_pivot else None),
             price=trough_low,confirmed_pre=bool(trough_pivot),
             bar_closed_ts=trough_ts+TF,
             note='bar-extremum known at PRE; pivot only when confirmed'),
        dict(kind='CORRECTION_HL',open_ts=hl_open,known_closed_ts=hl_known,
             price=hl_low,confirmed_pre=True,
             note='audited new HL; not necessarily lowest correction bar'),
        dict(kind='SECOND_PEAK',open_ts=peak2_ts,
             known_closed_ts=next((p['known_closed_ts'] for p in highs if p['open_ts']==peak2_ts),None),
             price=peak2,confirmed_pre=any(p['open_ts']==peak2_ts for p in highs)),
    ]
    for p in phase_lows[1:]:
        phase_extrema.append(dict(kind='SECOND_PIVOT_LOW',open_ts=p['open_ts'],
            known_closed_ts=p['known_closed_ts'],price=p['low'],confirmed_pre=True))
    # Diagnostic features deliberately do not assert a positive structure.
    features = {
        'break_at_hl_open':broken==hl_open,
        'phase_status':phase_status,
        'first_rise_visible_in_pre_window':first_rise_visible,
        'first_peak_confirmed_pre':first_peak_confirmed,
        'first_rise_atr':round(old_rise,4) if first_rise_visible else np.nan,
        'first_rise_bars':int((peak1_ts-first)/TF) if first_rise_visible else np.nan,
        'correction_bar_trough_open_ts':trough_ts,
        'correction_bar_trough_price':trough_low,
        'correction_bar_trough_pivot_confirmed_pre':bool(trough_pivot),
        'correction_trough_before_audit_hl':earlier_lower_trough,
        'audit_hl_above_correction_trough_atr':round(gap_atr,4),
        'correction_trough_to_audit_hl_bars':int((hl_open-trough_ts)/TF),
        'correction_true_depth_atr':round(correction_true_depth,4),
        'first_peak_to_hl_bars':int((hl_open-peak1_ts)/TF),
        'correction_atr':round(correction_depth,4),
        'correction_vs_first_pct':round(100*correction_depth/old_rise,2) if old_rise>0 else np.nan,
        'second_known_bars':len(second),
        'second_peak_atr':round(peak2_progress,4),
        'second_peak_from_true_correction_trough_atr':round((peak2-trough_low)/atr,4),
        'second_pre_atr':round((last_close-hl_low)/atr,4),
        'second_vs_first_peak_atr':round((peak2-peak1)/atr,4),
        'second_peak_confirmed_pre':any(p['open_ts']==peak2_ts for p in highs),
        'second_peak_ts':peak2_ts,
        'second_hl_pivots':len(phase_lows),
        'second_hl_up_steps':up,
        'second_hl_down_steps':down,
        'second_hl_max_streak':best,
        'second_undercut_hl_after_known':undid,
        'second_peak_efficiency_closes':round(path_eff,4),
        'second_retention_pct':round(retained,2),
        'second_closes_near_zone':int(near.sum()),
        'second_last6_near_zone':int(near.tail(6).sum()),
        'second_close_zone_crosses':crosses,
        'pre_distance_zone_atr':round((floor-last_close)/atr,4),
        'first_peak_to_zone_atr':round((floor-peak1)/atr,4),
        'second_peak_to_zone_atr':round((floor-peak2)/atr,4),
        'pivot_highs_confirmed_pre':len(highs),
    }
    return features, phase_extrema



def topology_audit_one(row, bars, features, extrema):
    """Supplementary geometry, not a classifier or trade-time signal.

    Uses only <= PRE confirmed pivots. A retrospective PRE peak need not have
    been known at its OPEN; `known_closed_ts` retains the actual knowledge time.
    Existing V1.1 metrics and segment statuses are intentionally unchanged.
    """
    pre=int(row['pre_closed_ts']); known=int(row['new_hl_known_closed_ts'])
    first=int(row['anchor_first_ts']); hl_open=int(row['new_hl_open_ts'])
    atr=float(row['atr_pre']); hl_low=float(row['new_hl_low'])
    x=prefix_candles(bars,pre)
    highs=confirmed_pivot_highs(x,pre)
    lows=load_known_lows(row['pre96_pivot_lows_json'],pre,pre-96*TF)
    ev={e['kind']:e for e in extrema if e['kind']!='SECOND_PIVOT_LOW'}
    p1=ev['FIRST_PEAK']; trough=ev['CORRECTION_BAR_TROUGH']
    tr_ts=int(trough['open_ts']); tr_price=float(trough['price'])
    p1_ts=int(p1['open_ts'])
    # Confirmed alternatives to the retrospectively selected first maximum.
    # Not silently substituted for the original first rise.
    options=[h for h in highs if first < h['open_ts'] < hl_open
             and h['known_closed_ts']<=known]
    alt=max(options,key=lambda h:(h['high'],-h['open_ts'])) if options else None
    chosen_peak_confirmed_by_hl=bool(any(
        h['open_ts']==p1_ts and h['known_closed_ts']<=known for h in highs))
    # Recovery ladder anchored at the earliest confirmed correction trough,
    # not automatically the auditor's selected, potentially higher, HL.
    after=[v for v in lows if tr_ts<=v['open_ts']<pre]
    if not any(v['open_ts']==tr_ts and abs(v['low']-tr_price)<=1e-6*tr_price for v in after):
        # The trough may be a bar extremum rather than a 2x2 pivot; retain
        # its distinct status. No fabricated confirmed pivot is introduced.
        ladder=[dict(open_ts=tr_ts,known_closed_ts=None,low=tr_price)] + after
        tr_confirmed=False
    else:
        ladder=after
        tr_confirmed=True
    ladder=sorted(ladder,key=lambda v:v['open_ts'])
    # Sorting includes only knowable PRE pivots; pairwise ladder steps are
    # descriptive of the fully observed PRE prefix, never early-entry signals.
    dd=np.diff([v['low'] for v in ladder])/atr
    up=int((dd>=.1).sum()); down=int((dd<=-.1).sum()); run=best=0
    for d in dd:
        run=run+1 if d>=.1 else 0
        best=max(best,run)
    second_lows=[v for v in lows if v['open_ts']>hl_open]
    after_known=x[(x.timestamp>=known)&(x.timestamp+TF<=pre)]
    below_hl=sum(v['low']<hl_low-.1*atr for v in second_lows)
    below_trough=sum(v['low']<tr_price-.1*atr for v in second_lows)
    # Distinguish validated pivot lows from intrabar lows: both PRE-only.
    min_second_bar_low=float(after_known.low.min()) if len(after_known) else np.nan
    second_highs=[h for h in highs if h['open_ts']>tr_ts and h['open_ts']<pre]
    hh=np.diff([h['high'] for h in second_highs])/atr
    flags=[]
    if not features['first_rise_visible_in_pre_window']:
        flags.append('FIRST_RISE_UNRESOLVED')
    if not chosen_peak_confirmed_by_hl:
        flags.append('FIRST_PEAK_NOT_CONFIRMED_BY_HL')
    if features['first_peak_to_hl_bars']<=3:
        flags.append('FIRST_PEAK_NEAR_HL')
    if not tr_confirmed:
        flags.append('TROUGH_NOT_CONFIRMED_PIVOT')
    if below_hl:
        flags.append('AUDIT_HL_PIVOT_UNDERCUT')
    if below_trough:
        flags.append('CORRECTION_TROUGH_PIVOT_UNDERCUT')
    if np.isfinite(min_second_bar_low) and min_second_bar_low<tr_price-.1*atr:
        flags.append('CORRECTION_TROUGH_BAR_UNDERCUT')
    f=dict(
        topology_flags='|'.join(flags) if flags else 'NONE',
        first_peak_confirmed_by_hl_known=chosen_peak_confirmed_by_hl,
        confirmed_first_peak_candidates=len(options),
        best_confirmed_first_peak_open_ts=alt['open_ts'] if alt else np.nan,
        best_confirmed_first_peak_known_ts=alt['known_closed_ts'] if alt else np.nan,
        best_confirmed_first_peak_price=alt['high'] if alt else np.nan,
        original_peak_minus_best_confirmed_atr=round(
            (float(p1['price'])-alt['high'])/atr,4) if alt else np.nan,
        true_correction_trough_confirmed=tr_confirmed,
        confirmed_ladder_lows_from_true_trough=len(ladder)-(0 if tr_confirmed else 1),
        ladder_up_steps_from_true_trough=up,
        ladder_down_steps_from_true_trough=down,
        ladder_max_up_streak=best,
        after_audit_hl_pivots_below_hl=below_hl,
        after_audit_hl_pivots_below_true_trough=below_trough,
        after_hl_min_bar_vs_true_trough_atr=round((min_second_bar_low-tr_price)/atr,4),
        confirmed_highs_since_true_trough=len(second_highs),
        confirmed_higher_high_steps_since_trough=int((hh>=.1).sum()),
        confirmed_lower_high_steps_since_trough=int((hh<=-.1).sum()),
    )
    # Full confirmed pivot export with original phase context, no POST.
    high_rows=[dict(kind='HIGH_2X2',open_ts=h['open_ts'],
                    known_closed_ts=h['known_closed_ts'],price=h['high'],
                    phase=('PRE_FIRST_ANCHOR' if h['open_ts']<first else
                           'FIRST_LEG' if h['open_ts']<=p1_ts else
                           'BETWEEN_FIRST_PEAK_AND_TROUGH' if h['open_ts']<=tr_ts else
                           'TROUGH_TO_AUDIT_HL' if h['open_ts']<=hl_open else
                           'SECOND_PHASE')) for h in highs]
    low_rows=[dict(kind='LOW_CONFIRMED_PRE',open_ts=v['open_ts'],
                   known_closed_ts=v['known_closed_ts'],price=v['low'],
                   phase=('PRE_FIRST_ANCHOR' if v['open_ts']<first else
                          'FIRST_LEG' if v['open_ts']<=p1_ts else
                          'BETWEEN_FIRST_PEAK_AND_TROUGH' if v['open_ts']<=tr_ts else
                          'TROUGH_TO_AUDIT_HL' if v['open_ts']<=hl_open else
                          'SECOND_PHASE')) for v in lows]
    for v in high_rows+low_rows:
        if not (v['open_ts']<pre and v['known_closed_ts']<=pre):
            raise AssertionError('POST_LEAK_IN_TOPOLOGY')
    return f,high_rows+low_rows

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan', action='store_true')
    group.add_argument('--run', action='store_true')
    parser.add_argument('--audit',type=Path,default=AUDIT)
    parser.add_argument('--cohort',type=Path,default=COHORT)
    parser.add_argument('--candles-dir',type=Path,default=CANDLES)
    parser.add_argument('--output-dir',type=Path,default=OUTPUT)
    args = parser.parse_args(argv)
    try:
        for p in [args.audit,args.cohort]:
            if not p.is_file():
                raise FileNotFoundError(str(p))
        manifest = args.audit.parent/'manifest.json'
        if manifest.is_file():
            md = json.loads(manifest.read_text(encoding='utf-8'))
            expected = md.get('cohort_sha256')
            if expected and hashfile(args.cohort) != expected:
                raise ValueError('PRE_COHORT_SHA256_MISMATCH')
        a = pd.read_csv(args.audit,low_memory=False)
        c = pd.read_csv(args.cohort, usecols=[
            'attempt_id','atr_pre','zone_low','zone_high','pre96_pivot_lows_json'])
        q = select_candidates(a,c)
        missing = sorted(sym for sym in q.symbol.unique()
                         if not (args.candles_dir/f'{sym}_30m.csv').is_file())
        plan = dict(scope='CORE15_GROUP_A_PRE',candidate_levels=len(q),
                    symbols=q.symbol.nunique(),missing_candles=missing,
                    output=str(args.output_dir),read_only_inputs=True,
                    no_live=True,no_dashboard_changes=True)
        print('HLL_TWO_LEG_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
        if args.plan:
            return 0
        if missing:
            raise ValueError('MISSING_30M_CANDLES '+repr(missing))
        dest = args.output_dir.resolve()
        sources = [args.audit.resolve(),args.cohort.resolve(),args.candles_dir.resolve()]
        if any(dest==p or dest in p.parents or p in dest.parents for p in sources):
            raise ValueError('OUTPUT_SOURCE_PATH_OVERLAP')
        if dest.exists():
            raise ValueError('OUTPUT_EXISTS_NON_OVERWRITE '+str(dest))
        if shutil.disk_usage(Path.cwd()).free < 100*1024**2:
            raise ValueError('DISK_FREE_BELOW_100MB')
        cache = {}
        results=[]; extrema=[]; topology=[]; full_pivots=[]; exclusions=[]
        for r in q.to_dict('records'):
            sym = str(r['symbol'])
            if sym not in cache:
                cache[sym] = load_candles(args.candles_dir/f'{sym}_30m.csv')
            data = dict(symbol=sym,level_id=str(r['level_id']),
                        attempt_id=str(r['attempt_id']),
                        pre_closed_ts=int(r['pre_closed_ts']),
                        pre_utc=pd.to_datetime(int(r['pre_closed_ts']),unit='ms',utc=True).strftime('%Y-%m-%d %H:%M'),
                        original_pre_pattern=str(r['original_pre_pattern']),
                        visual_label=LABELS.get(str(r['attempt_id']),'SIN_REVISAR'),
                        similarity=round(float(r['similarity']),4),
                        atr_pre=float(r['atr_pre']),zone_low=float(r['zone_low']),
                        zone_high=float(r['zone_high']),
                        initial_break_closed_ts=int(r['initial_break_closed_ts']),
                        new_hl_open_ts=int(r['new_hl_open_ts']),
                        new_hl_known_closed_ts=int(r['new_hl_known_closed_ts']))
            try:
                f, ext = segment_one(r,cache[sym])
                topo, all_pivots = topology_audit_one(r,cache[sym],f,ext)
                data.update(coverage='COMPLETE',**f)
                results.append(data)
                topology.append(dict(attempt_id=r['attempt_id'],symbol=sym,
                                     pre_closed_ts=r['pre_closed_ts'],
                                     visual_label=data['visual_label'],**topo))
                for p in all_pivots:
                    full_pivots.append(dict(attempt_id=r['attempt_id'],
                                             symbol=sym,pre_closed_ts=r['pre_closed_ts'],**p))
                for p in ext:
                    extrema.append(dict(attempt_id=r['attempt_id'],symbol=sym,
                                        pre_closed_ts=r['pre_closed_ts'],**p))
            except (ValueError,KeyError,TypeError,OverflowError) as exc:
                exclusions.append(dict(symbol=sym,attempt_id=r['attempt_id'],
                                       reason=str(exc)))
        if not results:
            raise ValueError('ALL_SEGMENTATIONS_FAILED '+str(exclusions[:8]))
        df=pd.DataFrame(results)
        ex=pd.DataFrame(exclusions,columns=['symbol','attempt_id','reason'])
        ev=pd.DataFrame(extrema)
        topology_df=pd.DataFrame(topology)
        allp_df=pd.DataFrame(full_pivots)
        if not topology_df.attempt_id.is_unique:
            raise ValueError('DUPLICATED_TOPOLOGY_ATTEMPT')
        features = ['first_rise_atr','correction_atr','first_peak_to_hl_bars',
                    'second_known_bars','second_hl_up_steps','second_hl_down_steps',
                    'second_peak_atr','second_pre_atr','second_retention_pct',
                    'second_peak_efficiency_closes','second_last6_near_zone',
                    'second_close_zone_crosses']
        summary = (df.groupby('visual_label',dropna=False)[features]
                   .median().round(3).reset_index())
        summary.insert(1,'n',summary.visual_label.map(df.visual_label.value_counts()).astype(int))
        # Blind review: no outcome, no category-driven optimization. Fixed hash
        # ordering excludes anything already viewed and avoids same-level repeats.
        blind = df[df.visual_label.eq('SIN_REVISAR')].copy()
        blind['blind_key'] = blind.attempt_id.map(lambda s: hashlib.sha256(
            ('two_leg_20261030|'+s).encode()).hexdigest())
        blind=blind.sort_values('blind_key').head(15)[[
            'symbol','attempt_id','pre_closed_ts','pre_utc','original_pre_pattern',
            'phase_status','first_rise_atr','correction_atr',
            'audit_hl_above_correction_trough_atr','second_hl_up_steps',
            'second_pre_atr','second_retention_pct','second_peak_efficiency_closes']].copy()
        blind['visual_review']='PENDIENTE'
        blind['observaciones']=''
        report=dict(**plan,analyzed=len(df),excluded=len(exclusions),
                    topology_first_peak_confirmed_by_hl=int(topology_df.first_peak_confirmed_by_hl_known.sum()),
                    topology_audit_hl_pivot_undercut=int((topology_df.after_audit_hl_pivots_below_hl>0).sum()),
                    topology_initial_correction_pivot_undercut=int((topology_df.after_audit_hl_pivots_below_true_trough>0).sum()),
                    topology_note='Descriptive PRE phase diagnostics only; no positive classification.',
                    phase_status_counts=df.phase_status.value_counts().to_dict(),
                    labels=df.visual_label.value_counts().to_dict(),
                    audit_sha256=hashfile(args.audit),cohort_sha256=hashfile(args.cohort),
                    warning=('Group A is conditional on known breakout attempts. '
                      'No inference about frequency before breakout or P(breakout|two-leg). '
                      'No PnL, outcomes, or POST used. Segments are descriptive as of PRE; '
                      'their extrema are not early-entry signals. Visual labels are exploratory. '
                      'First rise may be censored; earlier correction trough may be '
                      'below the audited HL. Neither status proves full two-leg structure.'))
        dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=Path(tempfile.mkdtemp(prefix='.two-leg-',dir=dest.parent))
        try:
            df.to_csv(tmp/'segments_pre.csv',index=False)
            df[df.phase_status.ne('AUDIT_HL_IS_CORRECTION_TROUGH')].to_csv(
                tmp/'phase_flags_pre.csv',index=False)
            ev.to_csv(tmp/'segment_pivots_pre.csv',index=False)
            topology_df.to_csv(tmp/'topology_pre.csv',index=False)
            allp_df.to_csv(tmp/'all_confirmed_pivots_pre.csv',index=False)
            ex.to_csv(tmp/'exclusions.csv',index=False)
            summary.to_csv(tmp/'visual_comparison_pre.csv',index=False)
            blind.to_csv(tmp/'blind_review_pre.csv',index=False)
            (tmp/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            if dest.exists():
                raise ValueError('OUTPUT_EXISTS_NON_OVERWRITE '+str(dest))
            os.rename(tmp,dest)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
        print('HLL_TWO_LEG_DONE',json.dumps(dict(analyzed=len(df),excluded=len(ex),
            by_label=df.visual_label.value_counts().to_dict(),output=str(dest)),
            ensure_ascii=False),flush=True)
        print('\n=== ESTADOS DE SEGMENTACION PRE ===')
        print(df.phase_status.value_counts().to_string())
        print('\n=== ETIQUETADOS ===')
        cols=['symbol','attempt_id','visual_label','phase_status',
              'first_rise_atr','audit_hl_above_correction_trough_atr',
              'correction_atr','second_hl_up_steps','second_hl_down_steps',
              'second_peak_atr','second_pre_atr','second_retention_pct',
              'second_peak_efficiency_closes']
        print(df[df.visual_label!='SIN_REVISAR'][cols].to_string(index=False))
        print('\n=== MEDIANAS EXPLORATORIAS ===')
        print(summary.to_string(index=False))
        print('\n=== TOPOLOGIA: AUDITORIA ===')
        print('Primer pico confirmado al conocer HL:',int(topology_df.first_peak_confirmed_by_hl_known.sum()),'/',len(topology_df))
        print('Pivotes bajos que perforan el HL auditado:',int((topology_df.after_audit_hl_pivots_below_hl>0).sum()))
        print('Pivotes bajos que perforan el valle real:',int((topology_df.after_audit_hl_pivots_below_true_trough>0).sum()))
        print('Banderas en casos etiquetados:')
        print(topology_df[topology_df.visual_label.ne('SIN_REVISAR')][
            ['symbol','attempt_id','visual_label','topology_flags',
             'ladder_up_steps_from_true_trough',
             'ladder_down_steps_from_true_trough',
             'confirmed_higher_high_steps_since_trough']].to_string(index=False))
        print('\n=== INCIDENCIAS (SOLO ERRORES REALES) ===')
        print(ex.to_string(index=False))
        return 0
    except (FileNotFoundError,ValueError,KeyError,TypeError) as exc:
        print('HLL_TWO_LEG_ERROR',str(exc),flush=True)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
