#!/usr/bin/env python3
"""HLL DETECTOR MISS AUDIT V1 — independent POST opportunity census (offline, read-only).

Investigates whether frozen Flip V2.6 / Direct Continuation V2.9 /
Reclaim V2.9 fail to recognize historical *reference-open* favorable paths.
A reference-open outcome is NOT a detector entry, trade, or attainable PnL.
No detector is rerun or modified. No networking, LIVE, or 1m downloads.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

TF = 30 * 60 * 1000
VERSION = 'HLL_DETECTOR_MISS_AUDIT_V1'
PRE = Path('reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv')
V29 = Path('reports/horizontal_level_continuation_v29/runs/sep290_full_v301/continuation_events.csv')
CANDLES = Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')
EPISODES = Path('reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv')
OUTPUT = Path('reports/horizontal_level_detector_miss_audit_v1')
BRANCHES = {'BREAKOUT_CONTINUATION', 'RECLAIM_CONTINUATION'}
SCENARIOS = (
    ('TP2_SL1_24', 24, 2.0, 1.0),
    ('TP3_SL1_48', 48, 3.0, 1.0),
    ('TP3_SL1p5_96', 96, 3.0, 1.5),
    ('TP5_SL2_96', 96, 5.0, 2.0),
)
PRE_REQUIRED = {'attempt_id', 'symbol', 'level_id', 'breakout_closed_ts',
                'pre_closed_ts', 'v26_status', 'zone_low', 'zone_high'}
V29_REQUIRED = {'attempt_id', 'origin_symbol', 'branch', 'status', 'confirmed_ts'}
CANDLE_REQUIRED = {'timestamp', 'open', 'high', 'low', 'close'}


def read_rows(path):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def header(path):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
        return set(next(csv.reader(f), []))


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def millis(value):
    try:
        n = Decimal(str(value).strip())
        if not n.is_finite() or n != n.to_integral_value():
            raise ValueError('Fractional timestamp')
        return int(n)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f'Invalid epoch milliseconds {value!r}') from e


def utc(value):
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def source_check(root, *, check_hash=False):
    paths = {'pre': root / PRE, 'continuation': root / V29,
             'episodes': root / EPISODES, 'candles': root / CANDLES}
    missing = [name for name, p in paths.items() if not p.exists()]
    if missing:
        raise FileNotFoundError('Missing canonical source(s): ' + ', '.join(missing))
    for name, fields in [('pre', PRE_REQUIRED), ('continuation', V29_REQUIRED)]:
        absent = fields - header(paths[name])
        if absent:
            raise ValueError(f'{name} missing columns: {sorted(absent)}')
    if {'attempt_id', 'episode_id'} - header(paths['episodes']):
        raise ValueError('Missing episode map identity columns')
    continuation_manifest = root / V29.parent / 'manifest.json'
    if not continuation_manifest.is_file():
        raise FileNotFoundError(continuation_manifest)
    meta = json.loads(continuation_manifest.read_text(encoding='utf-8'))
    if meta.get('attempts') != 55717 or meta.get('symbols') != 290:
        raise ValueError('Unexpected frozen V2.9 universe')
    proof = None
    if check_hash:
        proof = sha256(paths['pre'])
        if meta.get('source_sha256') != proof:
            raise ValueError('V2.9 source SHA-256 differs from frozen PRE')
    return paths, {'source_prehash_verified': bool(check_hash),
                   'pre_sha256': proof, 'v29_manifest_version': meta.get('version'),
                   'v29_attempts': meta.get('attempts'),
                   'v29_symbols': meta.get('symbols')}


def select_symbols(all_symbols, size, seed, includes):
    unique = sorted(set(all_symbols))
    if not unique:
        raise ValueError('No symbols available')
    if size <= 0 or size > len(unique):
        raise ValueError('Invalid pilot size')
    wanted = [x.strip().upper() for x in includes.split(',') if x.strip()]
    selected = list(dict.fromkeys(x for x in wanted if x in unique))
    if len(selected) > size:
        raise ValueError('More visual controls than pilot slots')
    rng = random.Random(seed)
    remaining = sorted(set(unique) - set(selected))
    selected += rng.sample(remaining, size - len(selected))
    return sorted(selected), sorted(set(wanted) - set(unique)), sorted(set(wanted) & set(selected))


def load_bars(path):
    if not path.is_file():
        raise FileNotFoundError(path)
    if CANDLE_REQUIRED - header(path):
        raise ValueError(f'Missing 30m OHLC columns: {path}')
    bars = {}
    last = None
    for row in read_rows(path):
        t = millis(row['timestamp'])
        if t % TF or (last is not None and t <= last):
            raise ValueError(f'Invalid/nonascending 30m timestamp: {path} {t}')
        o,h,l,c = (float(row[k]) for k in ('open','high','low','close'))
        if not all(math.isfinite(x) for x in (o,h,l,c)) or not (0 < l <= min(o,c) <= h and l <= max(o,c) <= h):
            raise ValueError(f'Bad OHLC: {path} {t}')
        bars[t] = (o,h,l,c)
        last = t
    return bars


def first_barrier(bars, reference_ts, horizon, target_pct, stop_pct):
    """Risk-aware *reference-open* path, not a live trade or signal simulation.

    TP and SL touched in same 30m candle => unknown intrabar ordering.
    An already observed barrier hit remains known if data end later.
    A missing 30m candle before any hit => censored, never NO_HIT.
    """
    first = bars.get(reference_ts)
    if first is None:
        return {'state': 'NO_REFERENCE_OPEN', 'reference_open': '', 'mfe_pct': '',
                'mae_pct': '', 'observed_bars': 0, 'first_barrier_bars': '',
                'first_barrier_ts': ''}
    ref = first[0]
    if not (ref > 0 and 0 < stop_pct < 100 and target_pct > 0 and horizon > 0):
        raise ValueError('Bad reference/scenario')
    tp = ref * (1.0 + target_pct/100.0)
    sl = ref * (1.0 - stop_pct/100.0)
    max_hi = ref
    min_lo = ref
    seen = 0
    for i in range(horizon):
        ts = reference_ts + i * TF
        bar = bars.get(ts)
        if bar is None:
            return {'state': 'INSUFFICIENT_POST', 'reference_open': ref,
                    'mfe_pct': round(100*(max_hi/ref-1),5),
                    'mae_pct': round(100*(min_lo/ref-1),5),
                    'observed_bars': seen, 'first_barrier_bars': '',
                    'first_barrier_ts': ''}
        _op,hi,lo,_cl = bar
        seen += 1
        max_hi = max(max_hi, hi)
        min_lo = min(min_lo, lo)
        t = hi >= tp
        s = lo <= sl
        if t or s:
            state = 'SAME_BAR_AMBIGUOUS' if t and s else ('TP_FIRST' if t else 'SL_FIRST')
            return {'state': state, 'reference_open': ref,
                    'mfe_pct': round(100*(max_hi/ref-1),5),
                    'mae_pct': round(100*(min_lo/ref-1),5),
                    'observed_bars': seen, 'first_barrier_bars': i+1,
                    'first_barrier_ts': ts}
    return {'state': 'NO_HIT', 'reference_open': ref,
            'mfe_pct': round(100*(max_hi/ref-1),5),
            'mae_pct': round(100*(min_lo/ref-1),5),
            'observed_bars': seen, 'first_barrier_bars': '',
            'first_barrier_ts': ''}


def classified_routes(v26, direct, reclaim):
    """Frozen outcomes, no reclassification. FLIP_CONFIRMED_LATER_LOST still confirmed."""
    v = str(v26 or '')
    flip = v.startswith('FLIP_')
    d = direct == 'CONFIRMED'
    r = reclaim == 'CONFIRMED'
    v_ind = (v.startswith('CENSORED_') or v.endswith('_CENSORED')
             or v in ('UNRESOLVED_FIRST4', 'RETEST_UNRESOLVED', '', 'UNKNOWN'))
    direct_ind = direct.startswith('CENSORED_') or direct in ('', 'ANCHOR_MISSING')
    reclaim_ind = reclaim.startswith('CENSORED_') or reclaim in ('', 'FAILURE_CANDLE_MISSING')
    return {'flip_confirmed': flip, 'continuation_confirmed': d,
            'reclaim_confirmed': r, 'any_confirmed': flip or d or r,
            'detection_indeterminate': v_ind or direct_ind or (reclaim_ind and reclaim != 'NO_ORIGINAL_FAILURE'),
            'reclaim_eligible': reclaim not in ('NO_ORIGINAL_FAILURE','NOT_CONFIRMED_LOSS','FAILURE_CANDLE_MISSING')}


def candidate_bucket(label, flags):
    if label != 'TP_FIRST':
        return ('AMBIGUOUS_PATH' if label == 'SAME_BAR_AMBIGUOUS' else
                'INCOMPLETE_PATH' if label in ('INSUFFICIENT_POST', 'NO_REFERENCE_OPEN') else
                'RECOGNIZED_NO_TP_FIRST' if flags['any_confirmed'] else 'NO_TP_FIRST')
    if flags['any_confirmed']:
        return 'TP_FIRST_WITH_MECHANISM'
    if flags['detection_indeterminate']:
        return 'TP_FIRST_DETECTOR_INDETERMINATE'
    return 'TP_FIRST_WITHOUT_ANY_MECHANISM'


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def load_episodes(path, wanted):
    out = {}
    for r in read_rows(path):
        if r['attempt_id'] in wanted:
            if r['attempt_id'] in out:
                raise ValueError('Duplicate episode attempt ID')
            out[r['attempt_id']] = r['episode_id']
    if len(out) != len(wanted):
        raise ValueError('Incomplete episode mapping')
    return out


def read_selected(pre, v29, wanted_symbols):
    attempts = {}
    for r in read_rows(pre):
        if r['symbol'] in wanted_symbols:
            aid = r['attempt_id']
            if not aid or aid in attempts:
                raise ValueError('Duplicate PRE attempt ID')
            attempts[aid] = r
    routes = defaultdict(dict)
    for r in read_rows(v29):
        aid = r['attempt_id']
        if aid not in attempts:
            continue
        branch = r['branch']
        if branch not in BRANCHES or branch in routes[aid]:
            raise ValueError(f'Duplicate/unknown V29 route: {aid} {branch}')
        if r['origin_symbol'] != attempts[aid]['symbol']:
            raise ValueError(f'V29 symbol mismatch: {aid}')
        routes[aid][branch] = r
    if any(set(routes.get(aid, {})) != BRANCHES for aid in attempts):
        raise ValueError('V29 does not have exactly two branches per attempt')
    return attempts, routes


def review_sample(rows, seed, per_bucket=18):
    """Review cases across distinct episodes where possible, including controls."""
    rng = random.Random(seed)
    by = defaultdict(list)
    for r in rows:
        if r['scenario'] == 'TP3_SL1_48':
            by[r['category']].append(r)
    chosen = []
    selected_episodes = set()
    for category in ('TP_FIRST_WITHOUT_ANY_MECHANISM', 'TP_FIRST_DETECTOR_INDETERMINATE',
                     'TP_FIRST_WITH_MECHANISM', 'RECOGNIZED_NO_TP_FIRST',
                     'NO_TP_FIRST', 'AMBIGUOUS_PATH', 'INCOMPLETE_PATH'):
        entries = by[category]
        rng.shuffle(entries)
        n = 0
        for r in entries:
            if r['episode_id'] in selected_episodes:
                continue
            chosen.append(r)
            selected_episodes.add(r['episode_id'])
            n += 1
            if n >= per_bucket:
                break
    return chosen


def run(root, size, seed, controls, full, output_dir=None):
    paths, meta = source_check(root, check_hash=True)
    all_symbols = sorted({r['symbol'] for r in read_rows(paths['pre'])})
    if len(all_symbols) != 290:
        raise ValueError(f'Unexpected PRE symbol universe: {len(all_symbols)}')
    if full:
        selected, missing_controls, present_controls = all_symbols, [], []
        mode = 'FULL290'
    else:
        selected, missing_controls, present_controls = select_symbols(all_symbols,size,seed,controls)
        mode = f'PILOT{len(selected)}'
    missing_bars = [s for s in selected if not (paths['candles']/f'{s}_30m.csv').is_file()]
    if missing_bars:
        raise FileNotFoundError('Missing 30m candle archives: ' + ','.join(missing_bars[:10]))
    attempts, routes = read_selected(paths['pre'], paths['continuation'], set(selected))
    episodes = load_episodes(paths['episodes'], set(attempts))
    dest = (Path(output_dir) if output_dir is not None else root/OUTPUT/(('full290_v1' if full else f'pilot{len(selected)}_seed{seed}_v1')))
    if dest.exists():
        raise FileExistsError(f'Output exists; do not overwrite audit run: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    scenario_rows = []
    errors = []
    counters = Counter()
    missed_reasons = Counter()
    try:
        dest.mkdir()
        for i,symbol in enumerate(selected,1):
            candles = load_bars(paths['candles']/f'{symbol}_30m.csv')
            for aid, r in attempts.items():
                if r['symbol'] != symbol:
                    continue
                event = routes[aid]
                direct = event['BREAKOUT_CONTINUATION']
                reclaim = event['RECLAIM_CONTINUATION']
                flags = classified_routes(r['v26_status'], direct['status'], reclaim['status'])
                t = millis(r['breakout_closed_ts'])
                if t % TF:
                    raise ValueError(f'Breakout timestamp not aligned: {aid}')
                for sc_name,window,tp,sl in SCENARIOS:
                    result = first_barrier(candles,t,window,tp,sl)
                    category = candidate_bucket(result['state'], flags)
                    row = {
                        'attempt_id': aid, 'symbol':symbol, 'level_id':r['level_id'],
                        'episode_id':episodes[aid], 'breakout_closed_ts':t,
                        'breakout_utc':utc(t), 'reference_open_ts':t,
                        'reference_open':result['reference_open'], 'scenario':sc_name,
                        'window_bars':window,'tp_pct':tp,'sl_pct':sl,
                        'path_state':result['state'],'category':category,
                        'first_barrier_bars':result['first_barrier_bars'],
                        'first_barrier_ts':result['first_barrier_ts'],
                        'observed_bars':result['observed_bars'],
                        'mfe_until_first_barrier_pct':result['mfe_pct'],
                        'mae_until_first_barrier_pct':result['mae_pct'],
                        'v26_status':r['v26_status'],
                        'continuation_status':direct['status'],
                        'reclaim_status':reclaim['status'],
                        'flip_confirmed':int(flags['flip_confirmed']),
                        'continuation_confirmed':int(flags['continuation_confirmed']),
                        'reclaim_confirmed':int(flags['reclaim_confirmed']),
                        'any_confirmed':int(flags['any_confirmed']),
                        'detection_indeterminate':int(flags['detection_indeterminate']),
                        'reclaim_eligible':int(flags['reclaim_eligible']),
                        'continuation_confirmed_ts':direct['confirmed_ts'],
                        'reclaim_confirmed_ts':reclaim['confirmed_ts'],
                        'flip_confirmed_closed_ts':r.get('flip_confirmed_closed_ts',''),
                        'cohort':'VISUAL_CONTROL' if symbol in present_controls else 'RANDOM_PILOT' if not full else 'FULL290',
                    }
                    scenario_rows.append(row)
                    counters[(sc_name,category)] += 1
                    if sc_name == 'TP3_SL1_48' and category == 'TP_FIRST_WITHOUT_ANY_MECHANISM':
                        missed_reasons[('v26',r['v26_status'])] += 1
                        missed_reasons[('continuation',direct['status'])] += 1
                        missed_reasons[('reclaim',reclaim['status'])] += 1
            print('HLL_MISS_SYMBOL',json.dumps({'i':i,'total':len(selected),'symbol':symbol}),flush=True)
        if len(scenario_rows) != len(attempts)*len(SCENARIOS):
            raise ValueError('Scenario cardinality mismatch')
        cols = list(scenario_rows[0]) if scenario_rows else []
        write_csv(dest/'attempt_scenarios.csv',scenario_rows,cols)
        reviewed = review_sample(scenario_rows,seed)
        write_csv(dest/'review_candidates.csv',reviewed,cols)
        summary = {
            'version':VERSION, 'mode':mode, 'selected_symbols':selected,
            'visual_controls_present':present_controls, 'visual_controls_missing':missing_controls,
            'attempts':len(attempts), 'episodes':len(set(episodes.values())),
            'scenarios':len(SCENARIOS), 'scenario_rows':len(scenario_rows),
            'review_rows':len(reviewed), 'by_scenario_category':{
                sc:{k:v for (s,k),v in sorted(counters.items()) if s==sc} for sc,_,_,_ in SCENARIOS
            },
            'missed_primary_scenario_reason_counts':{
                route:{status:n for (rte,status),n in sorted(missed_reasons.items()) if rte==route}
                for route in ('v26','continuation','reclaim')
            },
            'interpretation':'TP_FIRST refers only to hypothetical 30m OPEN immediately after CROSS_UP. It is NOT a detected/verified entry or a strategy backtest. Categories are diagnostic, not confirmed bugs.',
            'dependencies':'Repeated levels/episodes/symbols are non-independent. Pilot includes visual controls; do not estimate population performance from pilot.',
            'censoring':'Missing bars and same-bar TP/SL are never counted as favorable misses. Unresolved detector statuses excluded from confirmed-negative candidate class.',
        }
        (dest/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
        manifest = {
            'version':VERSION,'mode':'OFFLINE_NO_LIVE','source_prehash_verified':True,
            'pre_sha256':meta['pre_sha256'],'v29_sha256':sha256(paths['continuation']),
            'episode_map_sha256':sha256(paths['episodes']),
            'scenarios':[{ 'name':n,'window_bars':w,'tp_pct':tp,'sl_pct':sl} for n,w,tp,sl in SCENARIOS],
            'seed':seed,'visual_controls':present_controls,'symbols':selected,
            'output_files_sha256':{name:sha256(dest/name) for name in ('attempt_scenarios.csv','review_candidates.csv','summary.json')},
            'source_read_only':True, 'uses_post_to_define_labels':True,
            'changes_pre_or_detectors':False,'trade_entry_backtest':False,
        }
        (dest/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    except BaseException:
        # Keep failed run artifacts untouched for forensic inspection; no destructive cleanup.
        raise
    result={'status':'DONE','mode':mode,'attempts':len(attempts),'scenario_rows':len(scenario_rows),
            'summary':summary['by_scenario_category'],'output':str(dest)}
    print('HLL_MISS_FINAL',json.dumps(result,ensure_ascii=False),flush=True)
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true')
    m.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=Path('.'))
    p.add_argument('--pilot-size',type=int,default=15)
    p.add_argument('--seed',type=int,default=20261103)
    p.add_argument('--visual-controls',default='WAXPUSDT,AAVEUSDT,BSVUSDT')
    p.add_argument('--full',action='store_true',help='All 290 symbols; do pilot first')
    p.add_argument('--output-dir',type=Path,default=None)
    args=p.parse_args(argv)
    root=args.root.resolve()
    if args.plan:
        paths, meta = source_check(root,check_hash=False)
        info={'version':VERSION,'mode':'OFFLINE_NO_LIVE',
              'sources':{k:str(v.relative_to(root)) for k,v in paths.items()},
              'source_info':meta, 'pilot_size':args.pilot_size, 'seed':args.seed,
              'visual_controls':args.visual_controls.split(','),
              'scenarios':[x[0] for x in SCENARIOS],
              'writes':False, 'full_290_requested':args.full}
        print('HLL_MISS_PLAN',json.dumps(info,ensure_ascii=False),flush=True)
    else:
        run(root,args.pilot_size,args.seed,args.visual_controls,args.full,args.output_dir)


if __name__=='__main__':
    main()
