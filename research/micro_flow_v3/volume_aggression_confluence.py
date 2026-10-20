"""Research-only V3 candle-volume exhaustion + aggressor-flow confluence.

All inputs are immutable, observed 1s Replay buckets. Candle timestamps use
receive-wall-clock time (not Binance's official exchange kline timestamps).
Every causal milestone has its own *publication* time, distinct from observed
bucket/candle start. The matching cohort is RETROSPECTIVE/DESCRIPTIVE: a
VE_ONLY/AE_ONLY label requires waiting for the matching window to expire.
No order-book absorption, executable fills or OOS performance claims.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import math
import sqlite3
import uuid

from research.micro_flow_v3.swing_sweep_discovery import (
    DiscoveryError, build_observed_candles, file_sha256, validate_buckets,
)
from research.micro_flow_v3.sweep_entry_research import _next_open_reference, _entry_outcomes

SCHEMA = 'micro-flow-v3-ve-ae-confluence-exploratory-v1'
HORIZONS = (60, 180, 300, 600, 900)
COHORTS = ('VE_ONLY', 'AE_ONLY', 'CONFLUENCE_SAME_SIDE', 'CONFLUENCE_OPPOSITE_SIDE')


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()).hexdigest()


def _available_ms(ns):
    return (int(ns) + 999_999) // 1_000_000


def observed_volume_candles(buckets, timeframe_minutes):
    """Augment existing V3 candles; no OHLCV fabrication or missing-second fill."""
    rows = validate_buckets(buckets)
    candles = build_observed_candles(rows, timeframe_minutes)
    by_seg = defaultdict(list)
    for r in rows:
        by_seg[r['segment_id']].append(r)
    for c in candles:
        source = by_seg[c['segment_id']]
        starts = [r['second_start_ms'] for r in source]
        i = bisect_left(starts, c['timestamp'])
        j = bisect_left(starts, c['end_ms_exclusive'])
        covered = source[i:j]
        if not covered or c['first_seq'] != covered[0]['first_seq'] or c['last_seq'] != covered[-1]['last_seq']:
            raise DiscoveryError('Candle / original 1s sequence mismatch')
        c['volume_qty'] = c['buy_qty'] + c['sell_qty']
        c['cvd_qty_close'] = float(covered[-1]['cvd_qty'])
        c['seconds_coverage_fraction'] = c['seconds_observed'] / c['seconds_in_window']
        c['volume_source'] = 'SUM_OBSERVED_AGGTRADES_1S_NOT_OFFICIAL_KLINE'
        c['delta_source'] = 'AGGRESSIVE_BUY_MINUS_SELL_AGGTRADES'
    return candles


def _wilder_rsi(contiguous, period):
    """Return per-candle RSI, computed from closes available at that candle."""
    values = [None] * len(contiguous)
    gains, losses = [], []
    avg_gain, avg_loss = None, None
    for i in range(1, len(contiguous)):
        delta = contiguous[i]['close'] - contiguous[i-1]['close']
        gain, loss = max(delta, 0.0), max(-delta, 0.0)
        if i <= period:
            gains.append(gain)
            losses.append(loss)
            if i == period:
                avg_gain = sum(gains)/period
                avg_loss = sum(losses)/period
        else:
            avg_gain = (avg_gain*(period-1)+gain)/period
            avg_loss = (avg_loss*(period-1)+loss)/period
        if avg_gain is not None:
            if avg_gain == 0 and avg_loss == 0:
                values[i] = 50.0
            elif avg_loss == 0:
                values[i] = 100.0
            else:
                rs = avg_gain/avg_loss
                values[i] = 100 - 100/(1+rs)
    return values


def discover_volume_exhaustion(candles, *, timeframe_minutes=1,
            baseline_candles=30, rsi_period=14, high_volume_ratio=2.,
            oversold_rsi=35., overbought_rsi=65., min_coverage_fraction=.50,
            count_high_volume_in_last4=1, cooldown_candles=2):
    """A NEW explicit VE candidate, not asserted identical to legacy VE Collector.

    1m/5m fully published candle, relative volume vs *prior* 30 continuous
    covered candles, and RSI extreme known only at candle publication.
    Side BUY=up candle & RSI high; SELL=down candle & RSI low.
    Optional >=2 high-volume candles in last 4, all available by signal.
    """
    if not (timeframe_minutes in (1, 5) and baseline_candles >= rsi_period+1
            and rsi_period >= 2 and high_volume_ratio > 0
            and 0 <= oversold_rsi < 50 < overbought_rsi <= 100
            and 0 < min_coverage_fraction <= 1 and
            1 <= count_high_volume_in_last4 <= 4 and cooldown_candles >= 0):
        raise DiscoveryError('Invalid VE experiment parameters')
    series = defaultdict(list)
    for candle in candles:
        if candle['timeframe_minutes'] != timeframe_minutes:
            raise DiscoveryError('VE timeframe inconsistent with candle data')
        series[candle['segment_id']].append(candle)
    results = []
    eligible = 0
    no_coverage = 0
    for seg, candles_seg in series.items():
        candles_seg = sorted(candles_seg, key=lambda c:c['timestamp'])
        chunks, chunk = [], []
        for c in candles_seg:
            if chunk and chunk[-1]['end_ms_exclusive'] != c['timestamp']:
                chunks.append(chunk)
                chunk=[]
            chunk.append(c)
        if chunk:
            chunks.append(chunk)
        for seq in chunks:
            # Incomplete trade-second coverage cannot silently seed the baseline/RSI.
            run = []
            last_side_idx = {'BUY':-10**9, 'SELL':-10**9}
            idx_global = 0
            for c in seq:
                if c['seconds_coverage_fraction'] < min_coverage_fraction:
                    no_coverage += 1
                    run=[]
                    last_side_idx={'BUY':-10**9,'SELL':-10**9}
                    idx_global += 1
                    continue
                run.append(c)
                idx = len(run)-1
                if idx < baseline_candles:
                    idx_global += 1
                    continue
                rsi = _wilder_rsi(run, rsi_period)[idx]
                previous = run[idx-baseline_candles:idx]
                mean_qty = sum(x['volume_qty'] for x in previous)/baseline_candles
                if mean_qty <= 0 or rsi is None:
                    idx_global += 1
                    continue
                eligible += 1
                rel = c['volume_qty']/mean_qty
                # Prior trailing volume ratios, all calculated from earlier candles.
                recent = []
                for k in range(max(baseline_candles,idx-3),idx+1):
                    prior = run[k-baseline_candles:k]
                    base = sum(v['volume_qty'] for v in prior)/baseline_candles
                    recent.append((run[k]['volume_qty']/base) if base>0 else 0.)
                spikes = sum(x>=high_volume_ratio for x in recent)
                change_pct = (c['close']/c['open']-1)*100
                side = ('BUY' if change_pct>0 and rsi>=overbought_rsi
                        else 'SELL' if change_pct<0 and rsi<=oversold_rsi else None)
                if (side and rel>=high_volume_ratio and
                    spikes>=count_high_volume_in_last4 and
                    idx_global-last_side_idx[side]>=cooldown_candles):
                    last_side_idx[side]=idx_global
                    results.append({
                        'event_id':f"{seg}:VE:{timeframe_minutes}m:{side}:{c['timestamp']}",
                        'segment_id':seg,'timeframe_minutes':timeframe_minutes,
                        'pressure_side':side,
                        'hypothesis_direction':'SHORT' if side=='BUY' else 'LONG',
                        'candle_start_ms':c['timestamp'],
                        'candle_end_ms_exclusive':c['end_ms_exclusive'],
                        'available_wall_ns':c['available_wall_ns'],
                        'available_ms':c['available_ms'],
                        'first_seq':c['first_seq'],'last_seq':c['last_seq'],
                        'candle_open':c['open'],'candle_close':c['close'],
                        'volume_qty':c['volume_qty'],'buy_qty':c['buy_qty'],
                        'sell_qty':c['sell_qty'],'delta_qty':c['delta_qty'],
                        'cvd_qty_close':c['cvd_qty_close'],
                        'seconds_observed':c['seconds_observed'],
                        'seconds_coverage_fraction':c['seconds_coverage_fraction'],
                        'volume_ratio_prior_candles':rel,'baseline_candles':baseline_candles,
                        'rsi_wilder':rsi,'move_pct':change_pct,
                        'volume_spikes_last4':spikes,
                        'interpretation':'VOLUME_RSI_CANDIDATE_NOT_CONFIRMED_REVERSAL',
                    })
                idx_global += 1
    params = dict(timeframe_minutes=timeframe_minutes,baseline_candles=baseline_candles,
                  rsi_period=rsi_period,high_volume_ratio=high_volume_ratio,
                  oversold_rsi=oversold_rsi,overbought_rsi=overbought_rsi,
                  min_coverage_fraction=min_coverage_fraction,
                  count_high_volume_in_last4=count_high_volume_in_last4,
                  cooldown_candles=cooldown_candles)
    return {'parameters':params, 'candles':candles,'events':results,
            'summary':{'volume_exhaustion_events':len(results),
                       'eligible_candles':eligible,'low_coverage_candles':no_coverage,
                       'buy_events':sum(e['pressure_side']=='BUY' for e in results),
                       'sell_events':sum(e['pressure_side']=='SELL' for e in results)},
            'source_candles_sha256':_hash(candles),
            'detector_identity':'NEW_REPLAY_CANDLE_RSI_VOLUME_CANDIDATE_NOT_LEGACY_COLLECTOR_PARITY'}


def compare_confluence(buckets, ve_result, aggression, *, max_distance_seconds=60,
                       horizons=HORIZONS, freshness_seconds=3,
                       estimated_round_trip_cost_bps=12.):
    """One-to-one matched, descriptive cohort; entry reference after both are known.

    Match AE observed impulse within +/-N seconds of the *VE candle end*.
    Matching the entire 5m candle would create near-guaranteed coincidences
    and misleading confluence rates. Keep both event clocks in the case.
    Matching may use later information: signal=max(publication times).
    """
    if not (0 <= max_distance_seconds <= 600 and freshness_seconds >= 1
            and estimated_round_trip_cost_bps >= 0 and horizons and
            all(isinstance(h,int) and h>=1 for h in horizons)):
        raise DiscoveryError('Invalid confluence research parameters')
    if aggression['schema'] != 'micro-flow-v3-aggression-exhaustion-discovery-v1':
        raise DiscoveryError('Unknown AE input schema')
    rows = validate_buckets(buckets)
    if _hash(rows) != aggression['source_buckets_sha256']:
        raise DiscoveryError('AE cohort is not from same Replay')
    if _hash(ve_result['candles']) != ve_result['source_candles_sha256']:
        raise DiscoveryError('VE candle inputs mutated')
    seg_rows = defaultdict(list)
    for row in rows:
        seg_rows[row['segment_id']].append(row)
    ve_events = sorted(ve_result['events'], key=lambda e:(e['available_wall_ns'],e['event_id']))
    ae_events = sorted(aggression['events'], key=lambda e:(e['impulse']['available_wall_ns'],e['event_id']))
    used_ae = set()
    matched = {}
    padding = max_distance_seconds*1000
    for ve in ve_events:
        candidates=[]
        for ae in ae_events:
            if ae['event_id'] in used_ae or ae['segment_id'] != ve['segment_id']:
                continue
            observed = ae['impulse']['observed_second_start_ms']
            distance = abs(observed - ve['candle_end_ms_exclusive'])
            if distance <= padding:
                candidates.append((distance,ae['impulse']['available_wall_ns'],ae['event_id'],ae))
        if candidates:
            _, _, _, ae = min(candidates, key=lambda x:x[:3])
            used_ae.add(ae['event_id'])
            matched[ve['event_id']] = ae
    records=[]
    for ve in ve_events:
        ae = matched.get(ve['event_id'])
        cohort = ('VE_ONLY' if ae is None else 'CONFLUENCE_SAME_SIDE' if
                  ae['pressure_side']==ve['pressure_side'] else 'CONFLUENCE_OPPOSITE_SIDE')
        records.append(_research_case(ve,ae,cohort,seg_rows,horizons,
                         freshness_seconds,estimated_round_trip_cost_bps))
    for ae in ae_events:
        if ae['event_id'] not in used_ae:
            records.append(_research_case(None,ae,'AE_ONLY',seg_rows,horizons,
                         freshness_seconds,estimated_round_trip_cost_bps))
    records.sort(key=lambda x:(x['signal']['available_wall_ns'],x['case_id']))
    comparisons=[]
    for cohort in COHORTS:
        for side in ('BUY','SELL'):
            group=[r for r in records if r['cohort']==cohort and r['pressure_side']==side]
            for horizon in horizons:
                outcomes=[c['outcomes'][f'{horizon}s'] for c in group
                          if c['outcomes'].get(f'{horizon}s',{}).get('state')=='OBSERVED_PROXY']
                count=len(outcomes)
                comparisons.append({
                    'cohort':cohort,'pressure_side':side,'horizon_seconds':horizon,
                    'cases':len(group), 'entry_references':sum(c['entry_reference'] is not None for c in group),
                    'observed':count,'censored_or_no_reference':len(group)-count,
                    'mean_gross_pct':sum(o['return_directional_pct'] for o in outcomes)/count if count else None,
                    'mean_net_illustrative_pct':sum(o['net_after_illustrative_cost_pct'] for o in outcomes)/count if count else None,
                    'mean_mfe_pct':sum(o['mfe_directional_pct'] for o in outcomes)/count if count else None,
                    'mean_mae_pct':sum(o['mae_directional_pct'] for o in outcomes)/count if count else None,
                })
    return {'schema':SCHEMA,
            'parameters':{'max_distance_seconds':max_distance_seconds,
                          'horizons_seconds':list(horizons),
                          'freshness_seconds':freshness_seconds,
                          'estimated_round_trip_cost_bps':estimated_round_trip_cost_bps,
                          'matching_policy':'ONE_TO_ONE_NEAREST_AE_TO_VE_CANDLE_CLOSE_WITHIN_TOLERANCE_DESCRIPTIVE',
                          'entry_policy':'NEXT_PUBLISHED_1S_OPEN_REFERENCE_AFTER_LAST_SIGNAL',
                          'cohort_policy':'RETROSPECTIVE_NOT_ACTIONABLE_ONLY_LABEL',
                          'source_replay_policy':'SEGMENT_SEPARATE_RECEIVE_TIME_NOT_OFFICIAL_KLINE',
                          'research_role':'EXPLORATORY_NOT_PROSPECTIVE'},
            'source_buckets_sha256':_hash(rows),
            'source_ve_events_sha256':_hash(ve_events),
            'source_ae_events_sha256':_hash(ae_events),
            'source_candles_sha256':ve_result['source_candles_sha256'],
            'volume_detector_parameters':ve_result['parameters'],
            'aggression_detector_parameters':aggression['parameters'],
            'cases':records, 'comparisons':comparisons,
            'cases_sha256':_hash(records), 'comparisons_sha256':_hash(comparisons),
            'summary':{'ve_events':len(ve_events),'ae_events':len(ae_events),
                       'matched_events':len(matched),
                       'cohorts':{label:sum(r['cohort']==label for r in records) for label in COHORTS}},
            'warnings':['DESCRIPTIVE_MATCHING_RETROSPECTIVE_NO_FILLS',
                        'SIGNALS_NOT_INDEPENDENT_OR_RANDOMIZED',
                        'VE_COLLECTOR_PARITY_NOT_ESTABLISHED',
                        'EMPTY_SECONDS_NOT_FILLED; NO_ORDER_BOOK_ABSORPTION']}


def _research_case(ve,ae,cohort,seg_rows,horizons,freshness,cost_bps):
    anchor = ve if ve is not None else ae
    seg = anchor['segment_id']
    side = ve['pressure_side'] if ve is not None else ae['pressure_side']
    ns = max(ve['available_wall_ns'] if ve else 0,
             ae['impulse']['available_wall_ns'] if ae else 0)
    signal = {'available_wall_ns':ns,'available_ms':_available_ms(ns),
              'stage':'BOTH_SIGNALS_KNOWN' if ve and ae else 'SINGLE_SIGNAL_KNOWN'}
    data=seg_rows[seg]
    starts=[r['second_start_ms'] for r in data]
    i,entry=_next_open_reference(data,starts,signal,max_lag_seconds=freshness)
    outcomes={}
    if entry is not None:
        outcomes=_entry_outcomes(data,starts,i,entry['entry_reference_price'],
                          'HIGH' if side=='BUY' else 'LOW',horizons,freshness)
        for o in outcomes.values():
            o['net_after_illustrative_cost_pct'] = (o['return_directional_pct']-cost_bps/100
                  if o.get('state')=='OBSERVED_PROXY' else None)
    return {'case_id':f"{seg}:{cohort}:{ve['event_id'] if ve else 'NONE'}:{ae['event_id'] if ae else 'NONE'}",
            'segment_id':seg,'cohort':cohort,
            'pressure_side':side,'hypothesis_direction':'SHORT' if side=='BUY' else 'LONG',
            've_event_id':ve['event_id'] if ve else None,
            'ae_event_id':ae['event_id'] if ae else None,
            'same_pressure_side':(ve['pressure_side']==ae['pressure_side'] if ve and ae else None),
            've_candle_start_ms':ve['candle_start_ms'] if ve else None,
            'ae_impulse_observed_ms':ae['impulse']['observed_second_start_ms'] if ae else None,
            've_published_wall_ns':ve['available_wall_ns'] if ve else None,
            'ae_published_wall_ns':ae['impulse']['available_wall_ns'] if ae else None,
            'signal':signal,'entry_reference':entry,'outcomes':outcomes,
            'state':'REFERENCE_ONLY_NOT_FILL' if entry else 'NO_FRESH_REFERENCE'}


def save_confluence_run(root, session, ve, aggression, study, *, verify_raw=True):
    """Create-only provenance snapshot; never mutate sources or previous runs."""
    root=Path(root)
    if (study['schema']!=SCHEMA or _hash(ve['events'])!=study['source_ve_events_sha256']
            or _hash(aggression['events'])!=study['source_ae_events_sha256']
            or _hash(ve['candles'])!=study['source_candles_sha256']
            or study['volume_detector_parameters']!=ve['parameters']
            or study['aggression_detector_parameters']!=aggression['parameters']
            or _hash(validate_buckets(session['buckets']))!=study['source_buckets_sha256']
            or _hash(study['cases']) != study['cases_sha256']
            or _hash(study['comparisons']) != study['comparisons_sha256']):
        raise DiscoveryError('Research inputs changed after analysis')
    replay=Path(session['replay_path'])
    summary_path=Path(session['summary_path'])
    tape=Path(session['tape_path'])
    summ=session['summary']
    replay_hash=file_sha256(replay)
    if replay_hash!=summ['replay_jsonl_sha256'] or len(session['buckets'])!=int(summ['buckets_emitted']):
        raise DiscoveryError('Replay SHA256 or bucket count mismatch')
    raw_sha=None
    raw_status='NOT_REVERIFIED'
    if verify_raw:
        if not tape.is_file() or tape.stat().st_size == 0:
            raise DiscoveryError('RAW SQLite missing or empty; source cannot be verified')
        with sqlite3.connect(tape.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            last=db.execute('SELECT kind,reason FROM tape ORDER BY seq DESC LIMIT 1').fetchone()
        if last!=('MARKER','PROCESS_STOP'):
            raise DiscoveryError('RAW tape not closed')
        from research.micro_flow_v3.tape import replay_digest
        n,digest=replay_digest(tape)
        if n!=summ['source_tape_rows'] or digest!=summ['source_tape_digest_sha256']:
            raise DiscoveryError('RAW logical digest mismatch')
        raw_sha=file_sha256(tape)
        raw_status='VERIFIED_LOGICAL_DIGEST_AND_FILE_SHA256'
    created=datetime.now(timezone.utc)
    rid=created.strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:12]
    target=root/'reports/micro_flow_v3/volume_aggression_confluence/runs'/rid
    target.mkdir(parents=True,exist_ok=False)
    outputs={}
    for filename,content in (
        ('candles.jsonl',ve['candles']),
        ('volume_exhaustion_events.jsonl',ve['events']),
        ('aggression_exhaustion_events.jsonl',aggression['events']),
        ('confluence_cases.jsonl',study['cases']),
    ):
        with (target/filename).open('x',encoding='utf8') as f:
            for line in content:
                f.write(json.dumps(line,allow_nan=False,sort_keys=True)+'\n')
        outputs[filename]=file_sha256(target/filename)
    for filename,content in (
        ('cohort_comparison.json',study['comparisons']),
        ('research_summary.json',study['summary']),
    ):
        with (target/filename).open('x',encoding='utf8') as f:
            json.dump(content,f,indent=2,sort_keys=True,allow_nan=False)
            f.write('\n')
        outputs[filename]=file_sha256(target/filename)
    source_paths={
        'research/micro_flow_v3/volume_aggression_confluence.py':Path(__file__),
        'research/micro_flow_v3/aggression_exhaustion_discovery.py':root/'research/micro_flow_v3/aggression_exhaustion_discovery.py',
        'research/micro_flow_v3/swing_sweep_discovery.py':root/'research/micro_flow_v3/swing_sweep_discovery.py',
        'research/micro_flow_v3/sweep_entry_research.py':root/'research/micro_flow_v3/sweep_entry_research.py',
        'research/micro_flow_v3/replay.py':root/'research/micro_flow_v3/replay.py',
        'research/micro_flow_v3/tape.py':root/'research/micro_flow_v3/tape.py',
        'dashboard/micro_flow_v3_confluence_tab.py':root/'dashboard/micro_flow_v3_confluence_tab.py',
    }
    manifest = {'schema':SCHEMA,'run_id':rid,'created_utc':created.isoformat(),
        'research_role':'EXPLORATORY_NOT_OUT_OF_SAMPLE',
        'source_sqlite':str(tape),'source_tape_name':session['tape'],
        'source_sqlite_rows':summ['source_tape_rows'],
        'source_sqlite_logical_digest_sha256':summ['source_tape_digest_sha256'],
        'source_sqlite_file_sha256':raw_sha,'source_sqlite_verification':raw_status,
        'source_replay':str(replay),'source_replay_sha256':replay_hash,
        'source_replay_summary':str(summary_path),
        'source_replay_summary_sha256':file_sha256(summary_path),
        'source_buckets_sha256':study['source_buckets_sha256'],
        'source_ve_events_sha256':study['source_ve_events_sha256'],
        'source_ae_events_sha256':study['source_ae_events_sha256'],
        'cases_sha256':study['cases_sha256'],
        'comparisons_sha256':study['comparisons_sha256'],
        'volume_detector_parameters':ve['parameters'],
        'aggression_detector_parameters':aggression['parameters'],
        'confluence_parameters':study['parameters'],
        'summary':study['summary'],
        'algorithm_files_sha256':{name:file_sha256(path) for name,path in source_paths.items()},
        'artifacts_sha256':outputs,
        'contract':{'source':'REPLAY_1S_OBSERVED_RECEIVE_TIME_NOT_OFFICIAL_KLINES',
           'candle_volume':'SUM_ACTUAL_AGGTRADES_NO_IMPUTATION',
           'matching':'RETROSPECTIVE_COHORT_ASSIGNMENT_NOT_TRADE_TRIGGER',
           'signal_time':'MAX_OF_ALL_REQUIRED_INPUT_PUBLICATION_TIMES',
           'next_open_reference':'NOT_EXECUTABLE_FILL_KNOWN_ONLY_WHEN_BUCKET_PUBLISHED',
           'outcomes':'FUTURE_ONLY_NO_CROSS_SEGMENT_GAPS_CENSORED',
           'legacy_volume_exhaustion_parity':False,
           'not_mutated':['RAW_SQLITE','REPLAY_JSONL','PREVIOUS_RUNS'],
        }}
    with (target/'manifest.json').open('x',encoding='utf8') as f:
        json.dump(manifest,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    return target
