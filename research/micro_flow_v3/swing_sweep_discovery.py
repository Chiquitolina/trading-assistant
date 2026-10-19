"""Causal, read-only 1m/5m swing-sweep DISCOVERY over V3 observed 1s buckets.

No orders, no parameter optimization, no market-data backfill. It is NOT a
trading strategy. Each fact is indexed by the time at which the replay made
it observable, and outcomes are computed separately from causal milestones.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import math
import uuid

from engine.live.research.swing_detector import SwingDetector

SCHEMA = 'micro-flow-v3-swing-sweep-discovery-v1'
HORIZONS_S = (30, 60, 180, 300)


class DiscoveryError(ValueError):
    pass


def _number(value, name):
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise DiscoveryError(f'Invalid {name}') from exc
    if not math.isfinite(n):
        raise DiscoveryError(f'Nonfinite {name}')
    return n


def _millisecond_ceil(ns):
    return (int(ns) + 999_999) // 1_000_000


def validate_buckets(buckets):
    """Full scan; refuse interleaved segments, backwards time or future access."""
    rows = [dict(row) for row in buckets]
    last_seg, seen = None, set()
    last_start, last_avail = -1, -1
    symbol = None
    for r in rows:
        if r.get('event_type') != 'BUCKET_1S':
            raise DiscoveryError('Unknown replay event type')
        seg = int(r['segment_id'])
        start = int(r['second_start_ms'])
        end = int(r['second_end_ms_exclusive'])
        available_ns = int(r['available_wall_ns'])
        if seg < 1 or end - start != 1000 or start % 1000 != 0:
            raise DiscoveryError('Invalid second boundary or segment')
        if available_ns < end * 1_000_000:
            raise DiscoveryError('Replay bucket published before it was complete')
        if last_seg != seg:
            if seg in seen:
                raise DiscoveryError('Interleaved causal segments')
            seen.add(seg)
            last_seg = seg
            last_start, last_avail = -1, -1
        if start <= last_start or available_ns <= last_avail:
            raise DiscoveryError('Non-monotonic bucket/publication time')
        last_start, last_avail = start, available_ns
        if symbol is None:
            symbol = r['symbol']
        elif symbol != r['symbol']:
            raise DiscoveryError('Mixed symbols')
        for k in ('open', 'high', 'low', 'close', 'buy_qty', 'sell_qty'):
            r[k] = _number(r[k], k)
        if not (r['low'] <= min(r['open'],r['close']) <= max(r['open'],r['close']) <= r['high']):
            raise DiscoveryError('Incoherent OHLC')
        r['second_start_ms'] = start
        r['second_end_ms_exclusive'] = end
        r['available_wall_ns'] = available_ns
        r['segment_id'] = seg
    return rows


def _segments(rows):
    result = defaultdict(list)
    for row in rows:
        result[row['segment_id']].append(row)
    return result


def build_observed_candles(buckets, timeframe_minutes):
    """Aggregate only complete, observed receive-time minute windows.

    The first partial window and the last window without a later observed
    bucket are excluded. Missing *seconds* aren't filled; minutes without
    any observed trade are gaps that interrupt the swing sequence. Candle
    availability waits until an observed bucket from a subsequent minute.
    """
    tf = int(timeframe_minutes)
    if tf not in (1, 5):
        raise DiscoveryError('Only 1m and 5m supported')
    width = tf * 60_000
    candles = []
    for seg, rows in _segments(buckets).items():
        starts = [r['second_start_ms'] for r in rows]
        minute_groups = defaultdict(list)
        for r in rows:
            minute_groups[r['second_start_ms'] // width * width].append(r)
        for beginning, group in sorted(minute_groups.items()):
            end = beginning + width
            # A segment began mid-window => left-censored.
            if starts[0] >= beginning:
                continue
            # There must be later observations in this causal segment.
            next_idx = bisect_left(starts, end)
            if next_idx >= len(rows):
                continue
            nxt = rows[next_idx]
            # Conservatively: only know the full candle when the later 1s
            # bucket itself is published. No wall-clock guesses.
            avail_ns = max(max(r['available_wall_ns'] for r in group),
                           nxt['available_wall_ns'], end * 1_000_000)
            candles.append({
                'segment_id': seg, 'timestamp': beginning,
                'end_ms_exclusive': end, 'available_wall_ns': avail_ns,
                'available_ms': _millisecond_ceil(avail_ns),
                'open': group[0]['open'], 'high': max(r['high'] for r in group),
                'low': min(r['low'] for r in group), 'close': group[-1]['close'],
                'buy_qty': sum(r['buy_qty'] for r in group),
                'sell_qty': sum(r['sell_qty'] for r in group),
                'delta_qty': sum(r['buy_qty']-r['sell_qty'] for r in group),
                'trades': sum(int(r['trades']) for r in group),
                'seconds_observed': len(group), 'seconds_in_window': tf*60,
                'timeframe_minutes': tf,
                'first_seq': group[0]['first_seq'], 'last_seq': group[-1]['last_seq'],
            })
    return sorted(candles, key=lambda r: (r['segment_id'], r['timestamp']))


def detect_causal_swings(candles, bars):
    """Reuse frozen SwingDetector; map confirmer's START to its actual availability."""
    n = int(bars)
    if n not in (2, 3, 5):
        raise DiscoveryError('Swing size must be 2, 3 or 5')
    groups = defaultdict(list)
    for c in candles:
        groups[c['segment_id']].append(c)
    events = []
    for seg, data in groups.items():
        data.sort(key=lambda c: c['timestamp'])
        # Do not bridge an unobserved minute, even within a good segment.
        sequences, current = [], []
        for c in data:
            if current and current[-1]['end_ms_exclusive'] != c['timestamp']:
                sequences.append(current)
                current = []
            current.append(c)
        if current:
            sequences.append(current)
        for seq in sequences:
            by_start = {c['timestamp']:c for c in seq}
            for swing in SwingDetector(left_bars=n, right_bars=n).detect_all(seq):
                confirmation = by_start[swing.confirmed_timestamp]
                avail_ns = confirmation['available_wall_ns']
                events.append({
                    'swing_id': f"{seg}:{seq[0]['timeframe_minutes']}m:{n}x{n}:"
                                f"{swing.side}:{swing.pivot_timestamp}",
                    'segment_id': seg, 'timeframe_minutes': seq[0]['timeframe_minutes'],
                    'bars': n, 'side': swing.side, 'level': float(swing.price),
                    'pivot_ms': swing.pivot_timestamp,
                    'confirm_candle_start_ms': swing.confirmed_timestamp,
                    'confirmed_available_wall_ns': avail_ns,
                    'confirmed_available_ms': _millisecond_ceil(avail_ns),
                    'prominence_pct': swing.prominence_pct,
                })
    return sorted(events, key=lambda s: (s['confirmed_available_wall_ns'], s['swing_id']))


def _aggression_at(rows, idx, side, window_seconds=5, baseline_buckets=30):
    current = rows[idx]
    cutoff = current['second_start_ms'] - window_seconds * 1000
    observed = []
    j = idx
    while j >= 0 and rows[j]['second_start_ms'] > cutoff:
        observed.append(rows[j]); j -= 1
    observed.reverse()
    buy = sum(r['buy_qty'] for r in observed)
    sell = sum(r['sell_qty'] for r in observed)
    volume = buy + sell
    imbalance = (buy-sell)/volume if volume else None
    prev = rows[max(0,j-baseline_buckets+1):j+1]
    # Historical volume is normalized per observed 1s bucket, not wall time.
    prior_mean = (sum(r['buy_qty']+r['sell_qty'] for r in prev)/len(prev)) if prev else None
    relative = (volume/len(observed)/prior_mean) if prior_mean and prior_mean > 0 else None
    return {
        'aggression_window_seconds': window_seconds,
        'aggression_observed_seconds': len(observed),
        'aggression_baseline_buckets': len(prev),
        'aggressive_buy_qty': buy, 'aggressive_sell_qty': sell,
        'aggressive_delta_qty': buy-sell, 'aggressive_imbalance': imbalance,
        'aggressive_volume_relative': relative,
        'aggression_aligned': bool(imbalance is not None and
                                     ((side == 'HIGH' and imbalance > 0) or
                                      (side == 'LOW' and imbalance < 0))),
    }


def _after(rows, idx, max_seconds):
    initial = rows[idx]['second_start_ms']
    j = idx+1
    while j < len(rows) and rows[j]['second_start_ms'] - initial <= max_seconds*1000:
        yield j, rows[j]
        j += 1


def _stage(row, label):
    return {'stage': label, 'observed_second_start_ms':row['second_start_ms'],
            'available_wall_ns':row['available_wall_ns'],
            'available_ms':_millisecond_ceil(row['available_wall_ns']),
            'observed_close':row['close'],
            'first_seq':row['first_seq'], 'last_seq':row['last_seq']}


def _outcomes(rows, milestone, side, horizons=HORIZONS_S):
    """Post-retest DESCRIPTIVE observations, never used to decide milestones.

    No executable fills: reference is retest bucket close observed at `available`.
    A horizon is censored if no later observed price within 3s of target.
    """
    if milestone is None:
        return {}
    ts = milestone['observed_second_start_ms']
    avail_ns = milestone['available_wall_ns']
    starts = [r['second_start_ms'] for r in rows]
    start = bisect_left(starts, ts)
    direction = -1 if side == 'HIGH' else 1
    price = milestone['observed_close']
    result = {}
    for h in horizons:
        target = ts + h*1000
        j = bisect_left(starts, target)
        eligible = (j < len(rows) and 0 <= starts[j]-target <= 3000 and
                    rows[j]['available_wall_ns'] > avail_ns)
        prefix = f'outcome_{h}s_'
        if not eligible:
            result[prefix+'state'] = 'CENSORED'
            result[prefix+'return_directional_pct'] = None
            result[prefix+'mfe_directional_pct'] = None
            result[prefix+'mae_directional_pct'] = None
            result[prefix+'observed_seconds'] = None
            result[prefix+'coverage_fraction'] = None
            continue
        path = [r for r in rows[start+1:j+1] if r['available_wall_ns'] > avail_ns]
        last = rows[j]['close']
        excursions = ([direction*(r['high']/price-1)*100 for r in path] +
                      [direction*(r['low']/price-1)*100 for r in path])
        result[prefix+'state'] = 'OBSERVED'
        result[prefix+'return_directional_pct'] = direction*(last/price-1)*100
        result[prefix+'mfe_directional_pct'] = max([0.]+excursions)
        result[prefix+'mae_directional_pct'] = min([0.]+excursions)
        result[prefix+'observed_ms'] = starts[j]
        result[prefix+'observed_seconds'] = len(path)
        result[prefix+'coverage_fraction'] = min(1.0, len(path) / (h + 1))
    return result


def discover(buckets, *, timeframe_minutes=1, swing_bars=3,
             max_swing_age_minutes=30, reclaim_seconds=300,
             departure_pct=0.03, retest_tolerance_pct=0.02,
             retest_seconds=600, aggression_window_seconds=5):
    """Return the complete, unfiltred structure population + causal timeline.

    Each confirmed swing may contribute at most one first sweep. Every sweep
    remains included regardless of reclaim/retest/outcome (avoids cherry-picking).
    """
    if max_swing_age_minutes < 1 or reclaim_seconds < 1 or retest_seconds < 1:
        raise DiscoveryError('Invalid observation horizon')
    if not 0 <= departure_pct <= 5 or not 0 <= retest_tolerance_pct <= 5:
        raise DiscoveryError('Invalid exploratory percentages')
    rows = validate_buckets(buckets)
    candles = build_observed_candles(rows, timeframe_minutes)
    swings = detect_causal_swings(candles, swing_bars)
    groups = _segments(rows)
    events = []
    for sw in swings:
        data = groups[sw['segment_id']]
        starts = [r['second_start_ms'] for r in data]
        # Require a FULL second starting strictly after knowledge of swing.
        min_second = ((sw['confirmed_available_wall_ns'] + 999_999_999) // 1_000_000_000) * 1000
        idx = bisect_left(starts, min_second)
        level = sw['level']
        high = sw['side'] == 'HIGH'
        end_ms = min_second + max_swing_age_minutes * 60_000
        sweep_idx = None
        for i in range(max(idx,1),len(data)):
            b = data[i]
            if b['second_start_ms'] > end_ms:
                break
            # Require in-range previous observed close; no second-by-second
            # intra-bucket ordering is inferred from OHLC.
            prior = data[i-1]
            # No invented crossing over a long silence with no 1s observations.
            if b['second_start_ms'] - prior['second_start_ms'] > 30_000:
                continue
            if (high and prior['close'] <= level and b['high'] > level) or (
                not high and prior['close'] >= level and b['low'] < level):
                sweep_idx = i
                break
        if sweep_idx is None:
            continue
        b = data[sweep_idx]
        sweep = _stage(b,'SWEEP_OBSERVED')
        penetration = ((b['high']/level-1)*100 if high else
                       (1-b['low']/level)*100)
        event = {
            'initial_sweep_penetration_pct':penetration,
            'event_id': f"{sw['swing_id']}:SWEEP:{b['second_start_ms']}",
            'swing_id': sw['swing_id'], 'segment_id': sw['segment_id'],
            'timeframe_minutes': int(timeframe_minutes), 'swing_bars': int(swing_bars),
            'swing_side':sw['side'], 'hypothesis_direction': 'SHORT' if high else 'LONG',
            'level':level, 'pivot_ms':sw['pivot_ms'],
            'swing_confirm_available_wall_ns':sw['confirmed_available_wall_ns'],
            'swing_confirm_available_ms':sw['confirmed_available_ms'],
            'sweep':sweep, 'reclaim':None, 'departure':None, 'retest':None,
            'max_penetration_before_reclaim_pct': penetration,
            'status':'SWEEP_ONLY',
            'preceding_observation_gap_ms':b['second_start_ms']-data[sweep_idx-1]['second_start_ms'],
            **_aggression_at(data,sweep_idx,sw['side'],
                             window_seconds=int(aggression_window_seconds)),
        }
        # Reclaim cannot occur in the sweep bucket: OHLC has no intra-second order.
        reclaim_idx = None
        for j,r in _after(data,sweep_idx,reclaim_seconds):
            penetration = max(penetration,
                (r['high']/level-1)*100 if high else (1-r['low']/level)*100)
            if (r['close'] < level if high else r['close'] > level):
                reclaim_idx = j
                event['reclaim'] = _stage(r,'RECLAIM_OBSERVED')
                event['status'] = 'RECLAIMED'
                break
        event['max_penetration_before_reclaim_pct'] = penetration
        if reclaim_idx is not None:
            dep_idx = None
            # A meaningful displacement away from the reclaimed level must
            # be observed in a *later* bucket before seeking a retest.
            for j,r in _after(data,reclaim_idx,retest_seconds):
                departed = (r['low'] <= level*(1-departure_pct/100) if high else
                            r['high'] >= level*(1+departure_pct/100))
                if departed:
                    dep_idx = j
                    event['departure'] = _stage(r,'DEPARTURE_OBSERVED')
                    event['status'] = 'DEPARTED'
                    break
            if dep_idx is not None:
                # Deadline remains anchored on reclaim (not reset by departure).
                deadline = data[reclaim_idx]['second_start_ms']+retest_seconds*1000
                for j,r in _after(data,dep_idx,retest_seconds):
                    if r['second_start_ms'] > deadline:
                        break
                    touches = (r['high'] >= level*(1-retest_tolerance_pct/100)
                               if high else r['low'] <= level*(1+retest_tolerance_pct/100))
                    holds = r['close'] < level if high else r['close'] > level
                    if touches and holds:
                        event['retest'] = _stage(r,'RETEST_OBSERVED')
                        event['status'] = 'RETEST_OBSERVED'
                        break
        event.update(_outcomes(data,event['retest'],sw['side']))
        events.append(event)
    events.sort(key=lambda e: (e['sweep']['available_wall_ns'],e['event_id']))
    return {'schema': SCHEMA,
            'parameters': {'timeframe_minutes':int(timeframe_minutes),
                 'swing_bars':int(swing_bars),
                 'max_swing_age_minutes':int(max_swing_age_minutes),
                 'reclaim_seconds':int(reclaim_seconds),
                 'departure_pct':float(departure_pct),
                 'retest_tolerance_pct':float(retest_tolerance_pct),
                 'retest_seconds':int(retest_seconds),
                 'aggression_window_seconds':int(aggression_window_seconds)},
            'candles':candles,'swings':swings,'events':events,
            'summary':{'buckets':len(rows),'candles':len(candles),
                 'swings':len(swings),'sweeps':len(events),
                 'reclaimed':sum(e['reclaim'] is not None for e in events),
                 'departed':sum(e['departure'] is not None for e in events),
                 'retested':sum(e['retest'] is not None for e in events)}}


def file_sha256(path):
    h = sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def save_immutable_run(project_root, session, result, *, verify_raw=True):
    """Save new manifest + all milestones with source digests; NEVER edit sources.

    UI session is produced by load_replay_sessions. If raw has been lost,
    default fail closed (do not claim independent reproducibility).
    """
    replay = Path(session['replay_path'])
    summary_path = Path(session['summary_path'])
    stored = session['summary']
    if file_sha256(replay) != stored['replay_jsonl_sha256']:
        raise DiscoveryError('Replay digest mismatch')
    if int(stored['buckets_emitted']) != result['summary']['buckets']:
        raise DiscoveryError('Replay buckets count mismatch')
    raw_status = 'NOT_REVERIFIED'
    raw_file_digest = None
    if verify_raw:
        from research.micro_flow_v3.tape import replay_digest
        raw = Path(session['tape_path'])
        count, digest = replay_digest(raw)
        if count != stored['source_tape_rows'] or digest != stored['source_tape_digest_sha256']:
            raise DiscoveryError('RAW source no longer matches Replay')
        raw_status = 'VERIFIED_LOGICAL_DIGEST'
        raw_file_digest = file_sha256(raw)
    root = Path(project_root)
    runs = root/'reports'/'micro_flow_v3'/'swing_sweep_discovery'/'runs'
    runs.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:12]
    target = runs / run_id
    target.mkdir(exist_ok=False)
    hashes = {}
    for key in ('candles','swings','events'):
        path = target/(key+'.jsonl')
        with path.open('x',encoding='utf-8') as out:
            for r in result[key]:
                out.write(json.dumps(r,sort_keys=True,allow_nan=False)+'\n')
        hashes[path.name] = file_sha256(path)
    manifest = {
        'schema':SCHEMA,'created_utc':datetime.now(timezone.utc).isoformat(),
        'run_id':run_id,'research_role':'DISCOVERY_NOT_PROSPECTIVE',
        'source_session':session['tape'],
        'source_sqlite':str(session['tape_path']),
        'source_tape_logical_digest_sha256':stored['source_tape_digest_sha256'],
        'source_tape_rows':stored['source_tape_rows'],
        'source_sqlite_file_sha256':raw_file_digest,
        'raw_status_at_save':raw_status,
        'source_replay':str(replay), 'source_replay_sha256':file_sha256(replay),
        'source_replay_summary':str(summary_path),
        'source_replay_summary_sha256':file_sha256(summary_path),
        'algorithm_files_sha256':{
            'research/micro_flow_v3/swing_sweep_discovery.py':file_sha256(Path(__file__)),
            'engine/live/research/swing_detector.py':file_sha256(
                root/'engine/live/research/swing_detector.py'),
            'research/micro_flow_v3/replay.py':file_sha256(
                root/'research/micro_flow_v3/replay.py'),
            'research/micro_flow_v3/tape.py':file_sha256(
                root/'research/micro_flow_v3/tape.py')},
        'parameters':result['parameters'], 'counts':result['summary'],
        'artifacts_sha256':hashes,
        'research_contract':{
            'candle_clock':'observed receive seconds and later published 1s bucket',
            'swing_clock':'right confirming candle CLOSED and available',
            'sweep_clock':'first full second after confirmation availability',
            'milestone_clock':'recorded replay available_wall_ns',
            'outcomes':'descriptive OBSERVED only, with explicit observed-seconds coverage; NEVER trading execution or trigger',
            'sessions':'no gap, segment or tape crossings',
            'source':'V3 closed SQLite logical digest and complete observed replay',
        }
    }
    with (target/'manifest.json').open('x',encoding='utf-8') as f:
        json.dump(manifest,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    return target
