"""Micro Flow V3 shared *as-observed* microstructure features (research-only).

Features end at the publication of the *original* aggressive impulse, never at
its subsequently known weakening/failure or at a hindsight-selected extreme.
Every observed bucket is checked for same-segment continuity and causal
publication. No synthetic trades/candles, future-only labels, or order-book claims.

Optional footprint uses the original CLOSED, digest-verified SQLite trade tape.
Never fabricate a price-level footprint from OHLCV 1s Replay buckets.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
import math
import sqlite3
import uuid

from research.micro_flow_v3.swing_sweep_discovery import (
    DiscoveryError, file_sha256, validate_buckets,
)

SCHEMA = 'micro-flow-v3-microstructure-exhaustion-features-v1'
DEFAULT_WINDOWS_SECONDS = (5, 15, 30, 60)


def _hash_json(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


def _ceil_ms(ns):
    return (int(ns) + 999_999) // 1_000_000


def _snapshot(data, starts, anchor_idx, window, direction, *,
              min_coverage=.60, max_gap_seconds=3, min_net_delta_base_qty=1.):
    """Past-only fixed-clock feature window anchored at a published bucket.

    Low coverage and gaps produce explicit states, *not* forward-filled rows.
    `window_end_*` refers to the last observed second; `available_*` refers to
    that second's actual publication (which must precede the impulse milestone).
    """
    end = starts[anchor_idx]
    begin = end - (window - 1) * 1000
    left = bisect_left(starts, begin, 0, anchor_idx + 1)
    rows = data[left:anchor_idx + 1]
    meta = {
        'window_seconds': window,
        'window_start_ms': begin,
        'window_end_observed_second_ms': end,
        'observed_seconds': len(rows),
        'coverage_fraction': len(rows) / window,
        'available_wall_ns': int(data[anchor_idx]['available_wall_ns']),
        'available_ms': _ceil_ms(data[anchor_idx]['available_wall_ns']),
        'first_seq': rows[0]['first_seq'] if rows else None,
        'last_seq': rows[-1]['last_seq'] if rows else None,
    }
    if len(rows) / window < min_coverage:
        return dict(meta, state='INSUFFICIENT_OBSERVED_COVERAGE')
    if any(starts[j] - starts[j-1] > max_gap_seconds * 1000
           for j in range(left + 1, anchor_idx + 1)):
        return dict(meta, state='CENSORED_OBSERVATION_GAP')
    if rows[0]['second_start_ms'] > begin + max_gap_seconds * 1000:
        return dict(meta, state='INSUFFICIENT_WINDOW_HEAD')
    buy = sum(float(r['buy_qty']) for r in rows)
    sell = sum(float(r['sell_qty']) for r in rows)
    delta = buy - sell
    gross = buy + sell
    if gross <= 0:
        return dict(meta, state='ZERO_EXECUTED_VOLUME')
    op = float(rows[0]['open'])
    close = float(rows[-1]['close'])
    hi = max(float(r['high']) for r in rows)
    lo = min(float(r['low']) for r in rows)
    if op <= 0:
        raise DiscoveryError('Invalid price window')
    signed_move_pct = direction * (close / op - 1) * 100
    sign_delta = direction * delta
    imbalance = delta / gross
    range_value = hi - lo
    pressure_close_location = ((close - lo) / range_value if direction == 1
                               else (hi - close) / range_value) if range_value > 0 else None

    # Strict order: a prior-second extreme followed by a later *observed*
    # close back away from that extreme. Do NOT use the final bucket extreme;
    # high/low tick ordering inside that same 1s bucket is unknown.
    earlier = rows[:-1]
    prior_extreme = (max(float(r['high']) for r in earlier) if direction == 1
                     else min(float(r['low']) for r in earlier)) if earlier else None
    retrace_bps = (direction * (prior_extreme - close) / op * 10000
                   if prior_extreme is not None else None)
    prior_extreme_second_ms = None
    if prior_extreme is not None:
        candidates = [r for r in earlier if float(r['high' if direction == 1 else 'low']) == prior_extreme]
        prior_extreme_second_ms = int(candidates[0]['second_start_ms'])
    reversal_from_prior_extreme = (retrace_bps is not None and retrace_bps >= 2.0)
    # Ratio based on observed volume in ETH; no synthetic order-book liquidity.
    # Dollar-notional normalizes impact between USDT symbols. Do NOT derive
    # notional from candles when the actual executed quote amounts are missing.
    quote_present = all('buy_quote' in r and 'sell_quote' in r for r in rows)
    gross_quote_usdt = None
    if quote_present:
        quotes = [(float(r['buy_quote']), float(r['sell_quote'])) for r in rows]
        if any(not math.isfinite(b) or not math.isfinite(s) or b < 0 or s < 0
               for b,s in quotes):
            raise DiscoveryError('Invalid executed USDT notional in Replay')
        gross_quote_usdt = sum(b+s for b,s in quotes)
    impact_notional = (signed_move_pct * 100 * 100_000 / gross_quote_usdt
                       if gross_quote_usdt and gross_quote_usdt > 0 else None)
    impact_gross = signed_move_pct * 100 / gross
    impact_net = (signed_move_pct * 100 / abs(delta)
                  if abs(delta) >= min_net_delta_base_qty else None)
    # Compare flow and price *up to now*; not a verified exhaustion label.
    if sign_delta <= 0:
        alignment = 'FLOW_NOT_ALIGNED_WITH_IMPULSE_SIDE'
    elif signed_move_pct <= 0:
        alignment = 'AGGRESSIVE_FLOW_PRICE_DISAGREEMENT'
    elif signed_move_pct <= .01:
        alignment = 'AGGRESSIVE_FLOW_LOW_PRICE_PROGRESS'
    else:
        alignment = 'AGGRESSIVE_FLOW_PRICE_ALIGNED'
    return dict(meta, state='OBSERVED', buy_qty=buy, sell_qty=sell,
        gross_qty=gross, gross_quote_usdt=gross_quote_usdt,
        signed_impact_bps_per_100k_usdt_executed=impact_notional,
        delta_qty=delta, signed_delta_qty=sign_delta,
        imbalance=imbalance, window_open_price=op, window_close_price=close,
        window_high=hi, window_low=lo, price_move_pct=(close/op-1)*100,
        pressure_signed_move_pct=signed_move_pct,
        signed_impact_bps_per_100base_gross_qty=impact_gross*100,
        signed_impact_bps_per_100base_net_delta=(impact_net*100 if impact_net is not None else None),
        # Readable unit: pct per 100 ETH x 100 converts to bps.
        cvd_change_qty=delta, pressure_close_location=pressure_close_location,
        prior_second_extreme_price=prior_extreme,
        prior_extreme_second_start_ms=prior_extreme_second_ms,
        retreat_from_prior_extreme_bps=retrace_bps,
        rejection_after_prior_extreme_observed=reversal_from_prior_extreme,
        flow_price_alignment=alignment,
        first_observed_second_ms=int(rows[0]['second_start_ms']),
        last_observed_second_ms=int(rows[-1]['second_start_ms']),
    )


def build_exhaustion_feature_catalog(buckets, aggression, *, windows_seconds=DEFAULT_WINDOWS_SECONDS,
                   min_coverage=.60, max_gap_seconds=3, min_net_delta_base_qty=1.):
    """Attach causally published features to EVERY original AE impulse.

    Existing AE events/labels are unchanged. This layer does not recompute
    events or select them according to their eventual outcomes.
    """
    windows_seconds = tuple(int(v) for v in windows_seconds)
    if (not windows_seconds or len(set(windows_seconds)) != len(windows_seconds)
            or any(v < 2 or v > 120 for v in windows_seconds)
            or not 0 < min_coverage <= 1 or max_gap_seconds < 1
            or min_net_delta_base_qty <= 0):
        raise DiscoveryError('Invalid microstructure research parameters')
    rows = validate_buckets(buckets)
    expected = _hash_json(rows)
    if aggression.get('source_buckets_sha256') != expected:
        raise DiscoveryError('Aggression cohort built from a different Replay')
    segments = defaultdict(list)
    for r in rows:
        segments[r['segment_id']].append(r)
    lookup = {}
    for seg, sr in segments.items():
        lookup[seg] = (sr, [int(r['second_start_ms']) for r in sr])
    used_ids = set()
    result = []
    for ev in aggression['events']:
        eid = ev['event_id']
        if eid in used_ids:
            raise DiscoveryError('Duplicate aggressive impulse ID')
        used_ids.add(eid)
        seg = int(ev['segment_id'])
        if seg not in lookup or ev['pressure_side'] not in ('BUY', 'SELL'):
            raise DiscoveryError('Impulse segment/side inconsistent')
        sr, starts = lookup[seg]
        ms = int(ev['impulse']['observed_second_start_ms'])
        idx = bisect_left(starts, ms)
        if idx == len(starts) or starts[idx] != ms:
            raise DiscoveryError('Impulse does not map to original Replay')
        anchor = sr[idx]
        avail_ns = int(ev['impulse']['available_wall_ns'])
        if (int(anchor['available_wall_ns']) != avail_ns or
                int(anchor['last_seq']) != int(ev['impulse']['last_seq']) or
                float(anchor['close']) != float(ev['impulse']['observed_close'])):
            raise DiscoveryError('Impulse published data differs from source bucket')
        direction = 1 if ev['pressure_side'] == 'BUY' else -1
        window_features = {}
        for seconds in windows_seconds:
            snapshot = _snapshot(sr, starts, idx, seconds, direction,
                 min_coverage=min_coverage,
                 max_gap_seconds=max_gap_seconds, min_net_delta_base_qty=min_net_delta_base_qty)
            if snapshot['available_wall_ns'] > avail_ns:
                raise DiscoveryError('Feature computed from unpublished bucket')
            window_features[str(seconds) + 's'] = snapshot
        result.append({
            'event_id': eid, 'segment_id': seg, 'pressure_side': ev['pressure_side'],
            'hypothesis_direction': ev['hypothesis_direction'],
            'signal_observed_second_start_ms': ms,
            'signal_available_wall_ns': avail_ns,
            'signal_available_ms': _ceil_ms(avail_ns),
            'signal_first_seq': ev['impulse']['first_seq'],
            'signal_last_seq': ev['impulse']['last_seq'],
            'windows': window_features,
            # Future classifications are NOT copied into this per-impulse feature catalog.
            'data_contract':'ONLY_BUCKETS_PUBLISHED_BY_IMPULSE_SIGNAL',
        })
    return {
        'schema': SCHEMA,
        'source_buckets_sha256': expected,
        'source_aggression_schema': aggression['schema'],
        'symbol': rows[0]['symbol'] if rows else None,
        'source_aggression_parameters_sha256': _hash_json(aggression['parameters']),
        'parameters': dict(windows_seconds=list(windows_seconds),min_coverage=min_coverage,
                           max_gap_seconds=max_gap_seconds,min_net_delta_base_qty=min_net_delta_base_qty,
                           rejection_threshold_bps=2.0),
        'features': result,
        'summary': {'impulses':len(result),
                    'by_window':{k:{'observed':sum(e['windows'][k]['state']=='OBSERVED' for e in result),
                                    'unavailable':sum(e['windows'][k]['state']!='OBSERVED' for e in result)}
                                 for k in (str(v)+'s' for v in windows_seconds)}},
    }


def read_footprint_from_closed_raw(session, feature_row, *, window_seconds=15, max_trades=200_000,
                                   max_display_levels=100):
    """On-demand price-level tape scan; digest-checked, read-only, no synthetic ticks.

    Use the *entire* selected window's Replay seq bounds. `immutable=1` is
    only safe for the already CLOSED, checkpointed V3 SQLite source.
    """
    if not 1 <= max_display_levels <= 200 or max_trades < 1:
        raise DiscoveryError('Invalid footprint limits')
    window = feature_row['windows'].get(f'{window_seconds}s')
    if not window or window['state'] != 'OBSERVED':
        raise DiscoveryError('No sufficiently observed causal feature window')
    raw = Path(session['tape_path'])
    if not raw.is_file() or raw.stat().st_size <= 0:
        raise DiscoveryError('RAW SQLite unavailable/empty; footprint cannot be reconstructed')
    from research.micro_flow_v3.tape import replay_digest
    count, digest = replay_digest(raw)
    summary = session['summary']
    if (count != summary['source_tape_rows'] or digest != summary['source_tape_digest_sha256']):
        raise DiscoveryError('RAW does not match the Replay logical digest')
    from_seq = int(window['first_seq'])
    to_seq = int(window['last_seq'])
    uri = raw.resolve().as_uri() + '?mode=ro&immutable=1'
    con = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        last = con.execute('SELECT kind, reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        if last != ('MARKER', 'PROCESS_STOP'):
            raise DiscoveryError('RAW SQLite not safely closed')
        tape = con.execute('''SELECT seq, price, quantity, buyer_is_maker, recv_wall_ns
                              FROM tape WHERE seq BETWEEN ? AND ? AND kind='TRADE'
                              ORDER BY seq LIMIT ?''', (from_seq, to_seq, max_trades+1)).fetchall()
    finally:
        con.close()
    if len(tape) > max_trades:
        raise DiscoveryError('Footprint exceeds bounded trade limit; no silent truncation')
    levels = defaultdict(lambda:[Decimal(0), Decimal(0), 0])
    buy = sell = Decimal(0)
    for seq,price,quantity,buyer_is_maker,recv_ns in tape:
        if int(recv_ns) > feature_row['signal_available_wall_ns']:
            raise DiscoveryError('RAW trade after impulse availability in selected seq range')
        p, q = Decimal(price), Decimal(quantity)
        if p <= 0 or q <= 0 or buyer_is_maker not in (0,1):
            raise DiscoveryError('Invalid RAW trade or aggressor side')
        ix = 1 if buyer_is_maker else 0  # maker BUY => aggressive SELL.
        levels[p][ix] += q
        levels[p][2] += 1
        if ix: sell += q
        else: buy += q
    if not tape or abs(float(buy)-window['buy_qty']) > 1e-5 or abs(float(sell)-window['sell_qty']) > 1e-5:
        raise DiscoveryError('Footprint RAW volume differs from original Replay window')
    selected = sorted(levels.items(), key=lambda v:(-(v[1][0]+v[1][1]),v[0]))[:max_display_levels]
    display = sorted(selected, key=lambda v:v[0],reverse=True)
    return {
        'event_id':feature_row['event_id'], 'window_seconds':window_seconds,
        'signal_available_wall_ns':feature_row['signal_available_wall_ns'],
        'source_tape':session['tape'],'raw_logical_digest_sha256':digest,
        'source_seq_range':[from_seq,to_seq],
        'first_seq_included':tape[0][0], 'last_seq_included':tape[-1][0],
        'trade_rows':len(tape), 'levels_total':len(levels),
        'levels_displayed':len(display),'levels_omitted':len(levels)-len(display),
        'buy_qty':str(buy), 'sell_qty':str(sell),
        'levels':[{'price':str(p),'buy_qty':str(v[0]),'sell_qty':str(v[1]),
                   'delta_qty':str(v[0]-v[1]),'trades':v[2]} for p,v in display],
        'interpretation':'OBSERVED_EXECUTED_TRADE_FOOTPRINT_NOT_ORDER_BOOK_ABSORPTION',
    }


def save_feature_run(root, session, aggression, catalog):
    """Write a new immutable research run; require original RAW match and replay hash.

    Manifest is written last with verified source + algorithm checksums. All
    saved feature windows are impulse-time only; future outcomes stay in their
    own prior runs. This never writes a source file or overwrites a run.
    """
    if catalog['schema'] != SCHEMA or len(catalog['features']) != len(aggression['events']):
        raise DiscoveryError('Feature catalog mismatch')
    if catalog['source_buckets_sha256'] != aggression['source_buckets_sha256']:
        raise DiscoveryError('Aggression and feature input Replay differ')
    summary = session['summary']
    replay = Path(session['replay_path'])
    src_summary = Path(session['summary_path'])
    raw = Path(session['tape_path'])
    if file_sha256(replay) != summary['replay_jsonl_sha256']:
        raise DiscoveryError('Replay file SHA256 mismatch')
    from research.micro_flow_v3.tape import replay_digest
    count,digest = replay_digest(raw)
    if count != summary['source_tape_rows'] or digest != summary['source_tape_digest_sha256']:
        raise DiscoveryError('RAW logical SHA256 mismatch')
    # Recompute source-derived digest (including cohort IDs) before saving.
    recomputed = build_exhaustion_feature_catalog(session['buckets'],aggression,
        **{k:tuple(v) if k=='windows_seconds' else v
           for k,v in catalog['parameters'].items() if k!='rejection_threshold_bps'})
    if _hash_json(recomputed) != _hash_json(catalog):
        raise DiscoveryError('Feature catalog differs from deterministic recomputation')
    root = Path(root)
    target = root/'reports'/'micro_flow_v3'/'microstructure_features'/'runs'/(
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:12])
    target.mkdir(parents=True, exist_ok=False)
    artifact = target/'impulse_features.jsonl'
    with artifact.open('x', encoding='utf-8') as f:
        for obj in catalog['features']:
            f.write(json.dumps(obj,sort_keys=True,allow_nan=False,separators=(',',':'))+'\n')
    manifest = {
        'schema':SCHEMA,'research_only':True,'run_id':target.name,
        'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'symbol':summary['symbol'],'source_session':session['tape'],
        'source_raw':str(raw),'raw_tape_rows':count,
        'raw_logical_sha256':digest,'raw_file_sha256':file_sha256(raw),
        'source_replay':str(replay),'replay_sha256':file_sha256(replay),
        'source_replay_summary':str(src_summary),
        'replay_summary_sha256':file_sha256(src_summary),
        'source_buckets_sha256':catalog['source_buckets_sha256'],
        'source_aggression_parameters_sha256':catalog['source_aggression_parameters_sha256'],
        'parameters':catalog['parameters'],'summary':catalog['summary'],
        'algorithm_sha256':{
            'research/micro_flow_v3/microstructure_exhaustion_features.py':file_sha256(Path(__file__)),
            'research/micro_flow_v3/aggression_exhaustion_discovery.py':file_sha256(
                root/'research/micro_flow_v3/aggression_exhaustion_discovery.py'),
            'research/micro_flow_v3/aggression_features.py':file_sha256(
                root/'research/micro_flow_v3/aggression_features.py'),
            'research/micro_flow_v3/replay.py':file_sha256(
                root/'research/micro_flow_v3/replay.py'),
        },
        'artifacts_sha256':{'impulse_features.jsonl':file_sha256(artifact)},
        'research_contract':{
            'event_cohort':'every original AE impulse, no hindsight-selection',
            'features_clock':'only buckets already published at impulse availability',
            'source_clock':'recv_wall_ns (observed tape), not reconstructed exchange history',
            'windows':'calendar windows of observed 1s, explicit gap/coverage states',
            'rejection':'extreme from a prior observed second; same-second ordering unknown',
            'footprint':'optional independent RAW per-price inspection; not contained in these JSONL rows',
            'outcomes':'none in this layer; always separate from causal features',
            'execution':'NO fills / NO trading strategy',
        },
    }
    with (target/'manifest.json').open('x',encoding='utf-8') as f:
        json.dump(manifest,f,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    return target
