#!/usr/bin/env python3
"""Offline PRE-Reaction Phase 2: as-of-departure features, isolated labels.

Reads frozen pilot, canonical parity report and isolated 1m candle cache.
Writes a NEW immutable directory containing:
  * features_at_departure_PRE.csv (strictly causal; NO outcome columns)
  * first_touch_labels_SEPARATE.csv (post-departure research labels only)
  * manifest.json with source/outputs SHA256 and limitations.

No exchange, Redis, web, Streamlit, trading engine, or network operations.
This does NOT calculate profitable trades or fit a prediction model.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
import csv
import json
import math
from pathlib import Path

from pre_reaction_opportunity_lab import ONE, load_native_1m, iso_ms, sha

VERSION = 'pre-reaction-snapshot-at-departure-v1.0'
BASE = Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0')
PILOT = BASE / 'pilot_20261012_20261013_HANAUSDT_TAKEUSDT_ETHUSDT'
PARITY = BASE / 'canonical_replay_parity_v1'
CACHE = BASE / 'native_1m_pilot_cache'
OUTPUT = BASE / 'departure_snapshots_v1'
FEATURES = [
    'opportunity_key','symbol','side','pivot_ts','actionable_ts',
    'departure_ts','snapshot_cutoff_exclusive_ts','last_closed_1m_open_ts',
    'pivot_price','minutes_from_action_to_departure',
    'departure_excursion_pct','close_away_from_pivot_pct',
    'relative_volume_30m','close_strength_side','range_1m_pct',
    'return_5m_side_pct','return_15m_side_pct','atr14_1m_pct',
]
LABELS = [
    'opportunity_key','symbol','side','status_first_touch','label_validation',
    'first_touch_ts','minutes_to_first_touch_from_action',
    'minutes_to_first_touch_from_departure_known',
]
ALLOWED_STATUSES = {
    'REACTION','TOUCH_FAILED','TOUCH_INDECISIVE','ARMED_NO_RETEST',
    'NO_DEPARTURE','UNKNOWN_GAP',
}
CONTACT_STATUSES = {'REACTION','TOUCH_FAILED','TOUCH_INDECISIVE'}


def csv_rows(path):
    with path.open(encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


def ms(value):
    if value is None or value == '':
        return None
    return int(float(value))


def number(value):
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f'Nonfinite market value: {value}')
    return v


def read_sources(root, pilot, parity):
    manifest_file = pilot / 'manifest.json'
    manifest = json.loads(manifest_file.read_text())
    rows_path = pilot / 'opportunities_PRE.csv'
    if sha(rows_path) != manifest['opportunities_sha256']:
        raise RuntimeError('Frozen pilot opportunities SHA256 differs')
    for source, digest in manifest.get('source_hashes', {}).items():
        path = Path(source)
        if not path.is_file():
            path = root / source
        if not path.is_file() or sha(path) != digest:
            raise RuntimeError(f'Frozen market data missing or changed: {source}')
    rows = csv_rows(rows_path)
    if len(rows) != int(manifest['total']):
        raise RuntimeError('Pilot row count mismatch')
    if len({r['opportunity_key'] for r in rows}) != len(rows):
        raise RuntimeError('Duplicated opportunity keys')
    provenance_file = parity / 'canonical_replay_parity_report.json'
    provenance = json.loads(provenance_file.read_text())
    comparison_file = parity / 'first_touch_canonical_comparison.csv'
    only_file = parity / 'canonical_only.csv'
    if sha(comparison_file) != provenance['outputs_sha256']['comparison']:
        raise RuntimeError('Canonical parity CSV changed')
    if sha(only_file) != provenance['outputs_sha256']['canonical_only']:
        raise RuntimeError('Canonical-only CSV changed')
    if sha(rows_path) != provenance['inputs_sha256']['frozen_pilot']:
        raise RuntimeError('Canonical parity refers to a different frozen pilot')
    comparisons = csv_rows(comparison_file)
    summary = provenance['summary']
    if (int(summary['hard_disagreements']) != 0 or
        int(summary['canonical_only_touches']) != 0 or
        int(summary['exact_first_touches']) != int(summary['pilot_touches'])):
        raise RuntimeError('Canonical first-contact replay has unresolved hard disagreements')
    by_key = {r['opportunity_key']:r['parity_status'] for r in comparisons}
    if len(comparisons) != len(rows) or len(by_key) != len(rows):
        raise RuntimeError('Parity rows do not cover the frozen opportunity cohort')
    if set(by_key) != {r['opportunity_key'] for r in rows}:
        raise RuntimeError('Canonical parity keys differ from pilot')
    for r in rows:
        status = r['status']
        if status not in ALLOWED_STATUSES:
            raise RuntimeError(f'Unexpected pilot status: {status}')
        parity_status = by_key[r['opportunity_key']]
        action = ms(r['actionable_ts'])
        depart = ms(r.get('departure_ts'))
        known = ms(r.get('departure_known_ts'))
        touch = ms(r.get('first_touch_ts'))
        if depart is not None and (known != depart + ONE or depart < action):
            raise RuntimeError(f'Noncausal departure time: {r["opportunity_key"]}')
        if status in CONTACT_STATUSES and (touch is None or known is None or touch < known):
            raise RuntimeError(f'Contact lacks prior known departure: {r["opportunity_key"]}')
        if status not in CONTACT_STATUSES and touch is not None:
            raise RuntimeError(f'Contact timestamp present without contact status: {r["opportunity_key"]}')
        if status in CONTACT_STATUSES and parity_status != 'EXACT_FIRST_TOUCH_PARITY':
            raise RuntimeError(f'Contact not canonically confirmed: {r["opportunity_key"]}')
        if status in ('ARMED_NO_RETEST','NO_DEPARTURE') and parity_status != 'NO_TOUCH_BOTH_UNRESOLVED_CAUSAL_IDENTITY':
            raise RuntimeError(f'Unexpected no-touch parity: {r["opportunity_key"]}')
        if status == 'UNKNOWN_GAP' and parity_status != 'UNKNOWN_GAP_NO_PARITY':
            raise RuntimeError(f'Unexpected gap parity: {r["opportunity_key"]}')
    return manifest, provenance, rows, by_key


def asof_features(bars, side, pivot, action_ts, departure_ts, departure_price, known_ts):
    """Features available *strictly before* known_ts (open of next 1m candle).

    'bars' may include arbitrarily much future data; it is always sliced off.
    Any gap in the 31-bar lookback makes the snapshot ineligible.
    """
    if side not in ('LONG','SHORT'):
        raise ValueError('Unexpected trade side')
    if known_ts != departure_ts + ONE or departure_ts < action_ts:
        raise ValueError('Causal departure timestamps invalid')
    stamps = [ms(x['timestamp']) for x in bars]
    i = bisect_left(stamps, known_ts)
    if i == 0 or stamps[i-1] != known_ts-ONE:
        raise ValueError('Departure bar was not closed before the feature cutoff')
    if i < 31:
        raise ValueError('Insufficient warmup for PRE features: require 31 bars')
    past = bars[i-31:i]
    if any(ms(b['timestamp']) != known_ts-(31-j)*ONE for j,b in enumerate(past)):
        raise ValueError('Gap in 31-bar causal lookback')
    for bar in past:
        for name in ('open','high','low','close','volume'):
            number(bar[name])
    last = past[-1]
    opn, h, l, c, vol = [number(last[x]) for x in ('open','high','low','close','volume')]
    if min(opn,h,l,c,pivot) <= 0 or l > min(opn,c,h) or h < max(opn,c,l) or vol < 0:
        raise ValueError('Invalid OHLCV snapshot candle')
    orient = 1 if side == 'LONG' else -1
    v_prior = [number(x['volume']) for x in past[-31:-1]]
    avg_vol = sum(v_prior) / 30
    if avg_vol <= 0:
        rv = ''
    else:
        rv = vol / avg_vol
    close_strength = (c-l)/(h-l) if h>l else 0.5
    if side == 'SHORT':
        close_strength = 1-close_strength
    atr_ranges = []
    for j in range(17,31):  # exactly 14 true ranges; each has previous close
        bar = past[j]
        previous_close = number(past[j-1]['close'])
        atr_ranges.append(max(number(bar['high'])-number(bar['low']),
                              abs(number(bar['high'])-previous_close),
                              abs(number(bar['low'])-previous_close)))
    if departure_price is None:
        raise ValueError('Departure price missing')
    excursion = orient * (number(departure_price)-pivot)/pivot*100
    if excursion < -1e-9:
        raise ValueError('Departure excursion points to wrong side')
    return {
        'departure_ts':departure_ts,
        'snapshot_cutoff_exclusive_ts':known_ts,
        'last_closed_1m_open_ts':ms(last['timestamp']),
        'pivot_price':pivot,
        'minutes_from_action_to_departure':(departure_ts-action_ts)//ONE,
        'departure_excursion_pct':excursion,
        'close_away_from_pivot_pct':orient*(c-pivot)/pivot*100,
        'relative_volume_30m':rv,
        'close_strength_side':close_strength,
        'range_1m_pct':(h-l)/pivot*100,
        'return_5m_side_pct':orient*(c/number(past[-6]['close'])-1)*100,
        'return_15m_side_pct':orient*(c/number(past[-16]['close'])-1)*100,
        'atr14_1m_pct':sum(atr_ranges)/14/pivot*100,
    }


def label_row(row, parity_status):
    status = row['status']
    if status in CONTACT_STATUSES:
        flag = 'CANONICAL_FIRST_TOUCH_VALIDATED'
    elif status in ('ARMED_NO_RETEST','NO_DEPARTURE'):
        flag = 'NO_TOUCH_CAUSAL_IDENTITY_UNRESOLVED'
    else:
        flag = 'UNKNOWN_GAP_EXCLUDE'
    first = ms(row.get('first_touch_ts'))
    action = ms(row['actionable_ts'])
    known = ms(row.get('departure_known_ts'))
    return {
        'opportunity_key':row['opportunity_key'],
        'symbol':row['symbol'],'side':row['side'],
        'status_first_touch':status,
        'label_validation':flag,
        'first_touch_ts':first if first is not None else '',
        'minutes_to_first_touch_from_action':(first-action)//ONE if first is not None else '',
        'minutes_to_first_touch_from_departure_known':(first-known)//ONE if first is not None and known is not None else '',
    }


def save_csv(path, rows, columns):
    with path.open('w', encoding='utf-8', newline='') as file:
        writer = csv.DictWriter(file,fieldnames=columns,extrasaction='raise')
        writer.writeheader()
        writer.writerows(rows)


def execute(root, pilot, parity, cache, output, plan=False):
    manifest, provenance, rows, by_key = read_sources(root, pilot, parity)
    if plan:
        print('PRE_REACTION_SNAPSHOT_PLAN',json.dumps({
            'version':VERSION,'pilot_total':len(rows),
            'statuses':dict(Counter(r['status'] for r in rows)),
            'frozen_pilot':str(pilot),'parity':str(parity),'cache':str(cache),
            'output':str(output),'read_only_sources':True,'network':False,
            'feature_cutoff':'departure_known_ts (exclusive)',
            'financial_outcomes':False,
        },sort_keys=True,indent=2))
        return
    if output.exists():
        raise RuntimeError(f'Immutable output already exists: {output}')
    start = iso_ms(manifest['start_utc'])
    end = iso_ms(manifest['end_exclusive_utc'])
    warm = start-180*ONE
    finish = end+361*ONE
    frames={}
    for symbol in manifest['symbols']:
        data, _ = load_native_1m(root, cache.relative_to(root) if cache.is_absolute() else cache,
                                 symbol, warm, finish)
        expected = (finish-warm)//ONE
        if len(data)!=expected or int(data['timestamp'].iloc[0]) != warm or int(data['timestamp'].iloc[-1]) != finish-ONE:
            raise RuntimeError(f'Incomplete 1m history: {symbol}, got {len(data)}, expected {expected}')
        times = data['timestamp'].astype('int64').to_list()
        if any(times[j+1]-times[j]!=ONE for j in range(len(times)-1)):
            raise RuntimeError(f'1m history gap: {symbol}')
        frames[symbol] = data.to_dict('records')
    features=[]; labels=[]; skipped=[]
    for row in rows:
        labels.append(label_row(row,by_key[row['opportunity_key']]))
        known = ms(row.get('departure_known_ts'))
        if known is None:
            skipped.append({'opportunity_key':row['opportunity_key'],'reason':'NO_DEPARTURE_SNAPSHOT'})
            continue
        action=ms(row['actionable_ts']); depart=ms(row['departure_ts'])
        pivot=number(row['pivot_price'])
        partial=asof_features(frames[row['symbol']], row['side'], pivot, action,
                              depart, number(row['departure_price']), known)
        value={'opportunity_key':row['opportunity_key'],'symbol':row['symbol'],
               'side':row['side'],'pivot_ts':ms(row['pivot_ts']),
               'actionable_ts':action, **partial}
        if set(value)!=set(FEATURES):
            raise RuntimeError('PRE feature schema unexpected')
        features.append(value)
    output.mkdir(parents=True,exist_ok=False)
    feature_path=output/'features_at_departure_PRE.csv'
    label_path=output/'first_touch_labels_SEPARATE.csv'
    save_csv(feature_path, features, FEATURES)
    save_csv(label_path, labels, LABELS)
    counts=dict(Counter(x['status_first_touch'] for x in labels))
    report={
        'version':VERSION,'source_sha256':{
            'pilot_csv':sha(pilot/'opportunities_PRE.csv'),
            'pilot_manifest':sha(pilot/'manifest.json'),
            'parity_csv':sha(parity/'first_touch_canonical_comparison.csv'),
            'parity_report':sha(parity/'canonical_replay_parity_report.json'),
        },'outputs_sha256':{'features':sha(feature_path),'labels':sha(label_path)},
        'counts':{'pilot_opportunities':len(rows),'pre_feature_snapshots':len(features),
                  'labels':len(labels),'no_departure_snapshots':len(skipped),
                  'outcome_statuses':counts},
        'feature_schema':FEATURES,'labels_schema':LABELS,
        'sample_cutoff':'All market OHLCV features sampled strictly from 1m bars with open_ts < departure_known_ts.',
        'limitations':[
            'No TP/SL outcomes, predictive model, P&L or trading recommendations.',
            'Pilot cohort: 3 symbols × 1 day, far too small for generalizable prediction.',
            'No-touch swing identity is not independently validated by event-only canonical API.',
            'Labels are first touch outcomes; indecisive does not mean eventual failure.',
            'Data are isolated PARTIAL-month 1m partitions; never use as complete months.',
            'No BTC context, 4h strength, HTF room, or sector features at this phase.',
            'No-departure swings have no departure snapshot; label retained separately.',
        ],
    }
    (output/'manifest.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print('PRE_REACTION_SNAPSHOTS_DONE',json.dumps(report['counts'],sort_keys=True),flush=True)
    print('PRE_REACTION_SNAPSHOTS_PATH',output,flush=True)


def audit(root, pilot, parity, output):
    report = json.loads((output/'manifest.json').read_text())
    for label,path in (
        ('pilot_csv',pilot/'opportunities_PRE.csv'),
        ('pilot_manifest',pilot/'manifest.json'),
        ('parity_csv',parity/'first_touch_canonical_comparison.csv'),
        ('parity_report',parity/'canonical_replay_parity_report.json'),
    ):
        if sha(path)!=report['source_sha256'][label]:
            raise RuntimeError(f'Frozen source modified: {path}')
    for label,path in (
        ('features',output/'features_at_departure_PRE.csv'),
        ('labels',output/'first_touch_labels_SEPARATE.csv'),
    ):
        if sha(path)!=report['outputs_sha256'][label]:
            raise RuntimeError(f'Output modified: {path}')
    features = csv_rows(output/'features_at_departure_PRE.csv')
    labels = csv_rows(output/'first_touch_labels_SEPARATE.csv')
    if (len(features)!=report['counts']['pre_feature_snapshots'] or
        len(labels)!=report['counts']['labels']):
        raise RuntimeError('Snapshot/label row counts have changed')
    if (set(features[0]) if features else set(FEATURES)) != set(FEATURES):
        raise RuntimeError('Features schema altered')
    if (set(labels[0]) if labels else set(LABELS)) != set(LABELS):
        raise RuntimeError('Labels schema altered')
    if len({f['opportunity_key'] for f in features})!=len(features):
        raise RuntimeError('Duplicate PRE features')
    if len({f['opportunity_key'] for f in labels})!=len(labels):
        raise RuntimeError('Duplicate labels')
    if not set(f['opportunity_key'] for f in features).issubset(set(l['opportunity_key'] for l in labels)):
        raise RuntimeError('Features not subset of first-touch labels')
    for f in features:
        if ms(f['snapshot_cutoff_exclusive_ts']) != ms(f['departure_ts'])+ONE:
            raise RuntimeError('Departure snapshot cutoff wrong')
        if ms(f['last_closed_1m_open_ts'])+ONE != ms(f['snapshot_cutoff_exclusive_ts']):
            raise RuntimeError('Snapshot includes future bar')
        if ms(f['actionable_ts'])>ms(f['departure_ts']):
            raise RuntimeError('Departure earlier than actionability')
    result={'success':True, **report['counts'], 'folder':str(output)}
    print('PRE_REACTION_SNAPSHOTS_AUDIT',json.dumps(result,sort_keys=True),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    for cmd in ('plan','run','audit'):
        group.add_argument('--'+cmd,action='store_true')
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--pilot',type=Path,default=PILOT)
    parser.add_argument('--parity',type=Path,default=PARITY)
    parser.add_argument('--cache',type=Path,default=CACHE)
    parser.add_argument('--output',type=Path,default=OUTPUT)
    args=parser.parse_args()
    root=args.project.resolve()
    def resolve(p):
        return p.resolve() if p.is_absolute() else (root/p).resolve()
    pilot, parity, cache, output=map(resolve,(args.pilot,args.parity,args.cache,args.output))
    if args.audit:
        audit(root,pilot,parity,output)
    else:
        execute(root,pilot,parity,cache,output,args.plan)

if __name__=='__main__':
    main()
