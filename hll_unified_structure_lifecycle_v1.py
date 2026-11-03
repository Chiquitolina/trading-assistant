#!/usr/bin/env python3
"""HLL Unified Structure & Lifecycle Lab V1 | offline, auditable, no LIVE.

The cohort is the immutable 15-symbol Resistance Processes V2 pilot. Join
PRE on the exact selected attempt, fingerprints ONLY on episode-first attempt,
verified Entry Audit V3.0.1 FIRST_PER_LEVEL routes, and the retrospective
physical-process V2 link. Evaluate *verified* 1m opens and historical paths.

No POST-derived field is an eligibility filter or predictive PRE feature.
No existing file is modified. Source data/identifier disagreement fails closed.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = 'HLL_UNIFIED_STRUCTURE_LIFECYCLE_V1'
TF30 = 1_800_000
TF1 = 60_000
ROUTES = ('FLIP_1M', 'V29_CONTINUATION_30M', 'V29_RECLAIM_30M')
SCENARIOS = (
    ('TP2_SL1_24H', 2., 1., 1440),
    ('TP3_SL1P5_48H', 3., 1.5, 2880),
    ('TP3_SL2_48H', 3., 2., 2880),
)
DEVELOPMENT_MIN = (5, 15, 30, 60, 120, 360, 720, 1440, 2880)
PRE_FIELDS = (
    'pre_pattern_24', 'pre_pattern_48', 'pre_pattern_96',
    'pre_24_coverage', 'pre_48_coverage', 'pre_96_coverage',
)
DEFAULTS = {
 'map': 'reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv',
 'pre': 'reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv',
 'fingerprints': 'reports/horizontal_level_structural_similarity_v1/first_attempts_290_v1/fingerprints_first_pre.csv',
 'coverage': 'reports/horizontal_level_full290_v301/entries_FIRST_PER_LEVEL/attempt_route_coverage.csv',
 'entries': 'reports/horizontal_level_full290_v301/entries_FIRST_PER_LEVEL/entry_events.csv',
 'v29': 'reports/horizontal_level_continuation_v29/runs/sep290_full_v301/continuation_events.csv',
 'v2': 'reports/horizontal_level_resistance_process_cohort_v2/pilot15_seed20261103_v2',
 'minute': 'reports/horizontal_level_full290_v301/minutes',
 'minute_manifest': 'reports/horizontal_level_full290_v301/minutes_manifest',
 'output': 'reports/horizontal_level_unified_lifecycle_v1/pilot15_seed20261103_v1',
}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for buf in iter(lambda: f.read(1 << 20), b''):
            h.update(buf)
    return h.hexdigest()


def clock(x, *, step=TF1):
    """Decimal timestamp; never silently round float-ms strings."""
    try:
        d = Decimal(str(x).strip())
        if not d.is_finite() or d != d.to_integral_value():
            raise ValueError
        t = int(d)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Non-exact millisecond clock: {x!r}') from None
    if t <= 0 or t % step:
        raise ValueError(f'Unaligned millisecond clock: {x!r}')
    return t


def price(x):
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'Invalid price: {x!r}') from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f'Invalid price: {x!r}')
    return v


def maybe_number(x):
    if x is None or str(x).strip().lower() in ('', 'nan', 'none', 'null'):
        return ''
    try:
        v = float(x)
        return v if math.isfinite(v) else ''
    except (TypeError, ValueError):
        return ''


def rows(path: Path, required=()):
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not set(required).issubset(reader.fieldnames):
            raise ValueError(f'CSV missing required columns: {path}: {set(required)-set(reader.fieldnames or [])}')
        for row in reader:
            yield row


def unique_index(path, key, required=()):
    out = {}
    for row in rows(path, set(required) | {key}):
        k = row[key]
        if not k or k in out:
            raise ValueError(f'Null or duplicated {key} in {path}: {k}')
        out[k] = row
    return out


def expected_file_hash(path, expected, label):
    if not expected or digest(path) != expected:
        raise ValueError(f'{label} source hash mismatch: {path}')


def read_manifest(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding='utf-8'))


def file_paths(root, overrides=None):
    vals = {**DEFAULTS, **(overrides or {})}
    return {k: (Path(v) if Path(v).is_absolute() else root / v) for k, v in vals.items()}


def verify_sources(paths):
    req = ('map', 'pre', 'fingerprints', 'coverage', 'entries','v29')
    for k in req:
        if not paths[k].is_file():
            raise FileNotFoundError(paths[k])
    if not paths['v2'].is_dir():
        raise FileNotFoundError(paths['v2'])
    for file in ('physical_processes.csv', 'process_attempt_links.csv',
                 'process_checkpoints.csv', 'summary.json', 'manifest.json'):
        if not (paths['v2'] / file).is_file():
            raise FileNotFoundError(paths['v2'] / file)
    mm = read_manifest(paths['map'].parent/'manifest.json')
    pm = read_manifest(paths['pre'].parent/'manifest.json')
    fm = read_manifest(paths['fingerprints'].parent/'manifest.json')
    em = read_manifest(paths['entries'].parent/'manifest.json')
    v29m = read_manifest(paths['v29'].parent/'manifest.json')
    vm = read_manifest(paths['v2']/'manifest.json')
    sm = read_manifest(paths['v2']/'summary.json')
    expected_file_hash(paths['map'], mm.get('outputs_sha256', {}).get('attempt_episode_map.csv'), 'MAP')
    expected_file_hash(paths['fingerprints'], fm.get('fingerprints_sha256'), 'FINGERPRINT')
    if fm.get('source_map_sha256') != digest(paths['map']):
        raise ValueError('Fingerprint source MAP != current episode map')
    expected_pre = mm.get('source_sha256', {}).get('cohort')
    expected_file_hash(paths['pre'], expected_pre, 'PRE')
    if em.get('scope') != 'FIRST_PER_LEVEL' or em.get('source', {}).get('pre_sha256') != expected_pre:
        raise ValueError('Entry source not FIRST_PER_LEVEL or PRE mismatch')
    if v29m.get('source_sha256') != expected_pre:
        raise ValueError('Frozen V29 source differs from full PRE')
    if vm.get('no_detector_changes') is not True or vm.get('no_pre_rerank') is not True:
        raise ValueError('V2 cannot prove frozen detector/pre protection')
    if sm.get('version') != 'HLL_RESISTANCE_PROCESSES_CAUSAL_V2':
        raise ValueError('Wrong process V2 cohort')
    for name, sha in vm.get('files_sha256', {}).items():
        expected_file_hash(paths['v2']/name, sha, 'V2 '+name)
    if sm.get('unique_physical_processes_by_symbol_level') is None:
        raise ValueError('V2 physical-process count not declared')
    return {'map':mm, 'fingerprint':fm, 'entries':em, 'v29':v29m, 'v2':vm, 'v2summary':sm,
            'input_sha256':{k:digest(paths[k]) for k in req}}


def check_id(a, b, label, *fields):
    for k in fields:
        if b.get(k, '') not in ('', None) and a.get(k, '') != b.get(k, ''):
            raise ValueError(f'{label} {a.get("attempt_id")} mismatch {k}: {a.get(k)!r} != {b.get(k)!r}')


def check_cut(a, b, label):
    for k in ('pre_closed_ts', 'breakout_closed_ts'):
        if b.get(k, '') not in ('', None):
            if clock(a[k], step=TF30) != clock(b[k], step=TF30):
                raise ValueError(f'{label} mismatch {k}: {a["attempt_id"]}')


def load_core(paths, manifests):
    """One exact frozen attempt cohort; never infer missing data from neighbor attempts."""
    mm = unique_index(paths['map'], 'attempt_id', ('symbol', 'level_id', 'episode_id', 'pre_closed_ts', 'breakout_closed_ts'))
    allowed_symbols = set(manifests['v2']['candle_source_sha256'])
    if len(allowed_symbols) != manifests['v2summary']['symbols']:
        raise ValueError('V2 symbol inventory/cardinality mismatch')
    attempts = {aid:{**r} for aid, r in mm.items() if r['symbol'] in allowed_symbols}
    expected = int(manifests['v2summary']['source_attempts'])
    if len(attempts) != expected:
        raise ValueError(f'Pilot attempt count {len(attempts)} != V2 {expected}')
    if len({r['episode_id'] for r in attempts.values()}) != int(manifests['v2summary']['source_episodes']):
        raise ValueError('Pilot episode cardinality changed')
    for aid,a in attempts.items():
        if not a['episode_id'] or not a['level_id']:
            raise ValueError('Missing episode/level ID')
        clock(a['pre_closed_ts'], step=TF30)
        if clock(a['breakout_closed_ts'], step=TF30) != clock(a['pre_closed_ts'], step=TF30)+TF30:
            raise ValueError(f'Not an immediate CROSS_UP candle: {aid}')
        price(a['zone_low']); price(a['zone_high']);price(a['atr_pre'])
        if price(a['zone_high']) < price(a['zone_low']):raise ValueError('Inverted zone')
    seen_pre = set()
    for r in rows(paths['pre'], ('attempt_id','symbol','level_id','pre_closed_ts','breakout_closed_ts', 'v26_status')):
        aid = r['attempt_id']
        if aid not in attempts:continue
        if aid in seen_pre:raise ValueError(f'PRE duplicate attempt {aid}')
        seen_pre.add(aid)
        a=attempts[aid]
        check_id(a,r,'PRE','symbol','level_id')
        check_cut(a,r,'PRE')
        for k in ('zone_low','zone_high','atr_pre'):
            if r.get(k) not in (None,'') and not math.isclose(price(a[k]),price(r[k]), rel_tol=1e-8, abs_tol=1e-12):
                raise ValueError(f'PRE changed {k}: {aid}')
        a['pre'] = {k:r.get(k,'') for k in PRE_FIELDS}
        a['v26_status'] = r['v26_status']
        a['post_outcome_v26'] = r.get('post_outcome','')
    if seen_pre != set(attempts):
        raise ValueError(f'PRE missing {len(set(attempts)-seen_pre)} pilot attempts')
    fp_features = []
    fp_count = 0
    fp_seen = set()
    for r in rows(paths['fingerprints'], ('attempt_id','symbol','level_id','episode_id','pre_closed_ts','status')):
        aid=r['attempt_id']
        if aid not in attempts:continue
        if aid in fp_seen:raise ValueError(f'Duplicate fingerprint {aid}')
        fp_seen.add(aid)
        a=attempts[aid]
        if a.get('is_first_in_episode') != '1' or a['episode_first_attempt_id'] != aid:
            raise ValueError(f'Non-episode-first fingerprint {aid}')
        check_id(a,r,'FINGERPRINT','symbol','level_id','episode_id')
        if clock(a['pre_closed_ts'],step=TF30)!=clock(r['pre_closed_ts'],step=TF30):
            raise ValueError('Fingerprint PRE cutoff changed')
        names=sorted(k for k in r if k not in ('level_id',) and k.startswith(('geo_','rebuild_','compress_','level_','maturity_','movement_')))
        if not fp_features:fp_features=names
        if fp_features!=names:raise ValueError('Fingerprint features changed')
        a['fingerprint']={k:maybe_number(r[k]) for k in fp_features}
        a['fingerprint_status']=r['status']
        fp_count+=1
    expected_first=sum(a.get('is_first_in_episode')=='1' for a in attempts.values())
    if len(fp_features)!=35:
        raise ValueError(f'Unexpected fingerprint feature count {len(fp_features)}; expected 35')
    if fp_count!=expected_first:
        raise ValueError(f'Fingerprint first-episode cohort mismatch {fp_count}!={expected_first}')
    return attempts,fp_features



def load_v29(paths, attempts, manifests):
    """Two frozen V2.9 branches per attempt. POST outcome diagnostic, not PRE."""
    branches={'BREAKOUT_CONTINUATION':'v29_continuation_status_POST',
              'RECLAIM_CONTINUATION':'v29_reclaim_status_POST'}
    seen=set();total=0
    for r in rows(paths['v29'],('attempt_id','origin_symbol','level_id','branch','status','confirmed_ts','v26_status')):
        total+=1
        aid=r['attempt_id'];branch=r['branch']
        if aid not in attempts:continue
        if branch not in branches:raise ValueError('Unknown V29 branch')
        key=(aid,branch)
        if key in seen:raise ValueError(f'Duplicate frozen V29 branch: {key}')
        seen.add(key)
        a=attempts[aid]
        if r['origin_symbol']!=a['symbol'] or r['level_id']!=a['level_id']:
            raise ValueError(f'Frozen V29 identity changed: {aid}')
        if r['v26_status']!=a['v26_status']:
            raise ValueError(f'Frozen V29 V26 status differs from PRE: {aid}')
        if not r['status']:raise ValueError('Empty V29 status')
        if r['status']=='CONFIRMED':
            if r['confirmed_ts'] in ('',None,'nan') or clock(r['confirmed_ts'],step=TF30)<clock(a['breakout_closed_ts'],step=TF30):
                raise ValueError(f'Confirmed V29 without valid causal timestamp: {aid}')
        a[branches[branch]]=r['status']
        a[branches[branch].replace('_status_POST','_confirmed_ts_POST')]=r.get('confirmed_ts','')
    if manifests['v29'].get('total_rows') is not None and total!=int(manifests['v29']['total_rows']):
        raise ValueError('Frozen V29 cardinality changed')
    if len(seen)!=2*len(attempts):
        raise ValueError(f'Frozen V29 missing branches {len(seen)}/{len(attempts)*2}')
    return len(seen)


def load_processes(paths, attempts, manifests):
    pdir=paths['v2']
    physical=unique_index(pdir/'physical_processes.csv','process_id',('symbol','level_id','first_contact_known_closed_ts'))
    if len(physical)!=int(manifests['v2summary']['unique_physical_processes_by_symbol_level']):
        raise ValueError('V2 canonical process count mismatch')
    split_by_symbol={}
    for p in physical.values():
        symbol=p['symbol'];split=p.get('split','')
        if split not in ('DISCOVERY_SYMBOL','HOLDOUT_SYMBOL'):
            raise ValueError(f'Unknown V2 split {symbol}: {split!r}')
        if symbol in split_by_symbol and split_by_symbol[symbol]!=split:
            raise ValueError(f'V2 split changed inside symbol {symbol}')
        split_by_symbol[symbol]=split
    if set(split_by_symbol)!={a['symbol'] for a in attempts.values()}:
        raise ValueError('Not all pilot symbols have a physical process split')
    groups=defaultdict(list); link_rows=[]; seen=set()
    for r in rows(pdir/'process_attempt_links.csv',('process_id','attempt_id','symbol','level_id','episode_id')):
        pid,aid=r['process_id'],r['attempt_id']
        if (pid,aid) in seen:raise ValueError('Duplicate physical/attempt link')
        seen.add((pid,aid))
        if pid not in physical or aid not in attempts:raise ValueError('V2 link references unknown process/attempt')
        a=attempts[aid];p=physical[pid]
        check_id(a,r,'PROCESS','symbol','level_id','episode_id')
        if p['symbol']!=a['symbol'] or p['level_id']!=a['level_id']:
            raise ValueError('V2 process linked to wrong symbol/level')
        if r.get('attempt_v26_status') and r['attempt_v26_status']!=a['v26_status']:
            raise ValueError(f'Frozen detector state mismatch at {aid}')
        for source,dest in [('attempt_continuation_status','v29_continuation_status_POST'),
                            ('attempt_reclaim_status','v29_reclaim_status_POST')]:
            if r.get(source) and r[source]!=a.get(dest):
                raise ValueError(f'Frozen V29 process-link mismatch at {aid}:{source}')
        groups[aid].append(pid)
        link_rows.append({**r,'retrospective_only':'YES_NOT_CAUSAL_PREDICTOR'})
    if len(link_rows)!=int(manifests['v2summary']['physical_attempt_links']):
        raise ValueError('V2 links count mismatch')
    for aid in groups:
        groups[aid].sort()
    checkpoints=defaultdict(list)
    for c in rows(pdir/'process_checkpoints.csv', ('checkpoint_id','process_id','known_closed_ts','event_type')):
        pid=c['process_id']
        if pid not in physical:raise ValueError('Orphan V2 checkpoint')
        ts=clock(c['known_closed_ts'],step=TF30)
        checkpoints[pid].append((ts,c))
    if sum(map(len,checkpoints.values()))!=int(manifests['v2summary']['causal_checkpoint_rows']):
        raise ValueError('V2 checkpoints count mismatch')
    for pid in checkpoints:checkpoints[pid].sort(key=lambda c:c[0])
    return physical,groups,checkpoints,link_rows,split_by_symbol


def load_entry_records(paths, attempts, manifests):
    """Retain NOT_COVERED distinctly from verified entry and NO_SIGNAL."""
    coverage={}
    for r in rows(paths['coverage'],('attempt_id','symbol','level_id','route','status')):
        aid=r['attempt_id'];route=r['route']
        if aid not in attempts:continue
        if route not in ROUTES:raise ValueError(f'Unknown coverage route {route}')
        check_id(attempts[aid],r,'COVERAGE','symbol','level_id')
        key=(aid,route)
        if key in coverage:raise ValueError('Duplicated route coverage')
        coverage[key]=r
    # On covered attempts, coverage must be complete (all 3 routes).
    counted=Counter(aid for aid,route in coverage)
    if any(n!=3 for n in counted.values()):
        raise ValueError('Partial route coverage for an attempt')
    entries=[]; entry_ids=set();route_seen=set()
    total_rows=0
    for e in rows(paths['entries'],('entry_id','attempt_id','symbol','level_id','route','status','entry_ts','entry_price','signal_ts')):
        total_rows+=1
        aid=e['attempt_id']
        if aid not in attempts:continue
        a=attempts[aid]
        check_id(a,e,'ENTRY','symbol','level_id')
        if e['route'] not in ROUTES:raise ValueError('Unknown entry route')
        if e['status']!='ENTRY_VERIFIED':raise ValueError('Nonverified row in entry_events')
        if (aid,e['route']) in route_seen or e['entry_id'] in entry_ids or not e['entry_id']:
            raise ValueError('Duplicated entry/route ID')
        route_seen.add((aid,e['route']));entry_ids.add(e['entry_id'])
        if (aid,e['route']) not in coverage or coverage[(aid,e['route'])]['status']!='ENTRY_VERIFIED':
            raise ValueError('Entry not supported by FIRST_PER_LEVEL coverage')
        ts=clock(e['entry_ts'])
        if ts < clock(a['breakout_closed_ts'],step=TF30):
            raise ValueError('Entry before breakout')
        if e['signal_ts'] not in ('',None) and clock(e['signal_ts'])>ts:
            raise ValueError('Entry before signal known')
        if 'breakout_closed_ts' in e and e['breakout_closed_ts'] not in ('',None):
            check_cut(a,e,'ENTRY')
        entry_price=price(e['entry_price'])
        if e.get('pre_closed_ts') not in ('',None):
            if clock(e['pre_closed_ts'],step=TF30)!=clock(a['pre_closed_ts'],step=TF30):
                raise ValueError('Entry PRE clock mismatch')
        entries.append({**e,'entry_ts_int':ts,'entry_price_float':entry_price})
    if total_rows!=int(manifests['entries']['entries']):
        raise ValueError(f'Global entry file count changed {total_rows}')
    return coverage,entries


def read_minute_file(path):
    """One symbol at a time. Raw public 1m OHLC; zero filling prohibited."""
    if not path.is_file():return None,None
    stamps=[]; o=[];h=[];l=[];c=[]
    last=None
    for r in rows(path,('timestamp','open','high','low','close')):
        t=clock(r['timestamp']); op=price(r['open']); hi=price(r['high']); lo=price(r['low']);cl=price(r['close'])
        if lo>min(op,cl) or hi<max(op,cl) or lo>hi:
            raise ValueError(f'Invalid 1m OHLC {path}:{t}')
        if last is not None and t<=last:raise ValueError(f'Nonmonotonic 1m timestamp {path}:{t}')
        stamps.append(t);o.append(op);h.append(hi);l.append(lo);c.append(cl);last=t
    if not stamps:raise ValueError(f'Empty 1m file {path}')
    return stamps,(o,h,l,c)


def open_equal(observed,expected):
    return abs(observed-expected)<=max(1e-10,abs(expected)*1e-7)


def label_zone(close,low,high):
    return 'ABOVE' if close>high else ('BELOW' if close<low else 'IN_ZONE')


def score_entry(entry_ts, entry_price, times, arrays, scenario, *, roundtrip_fee_pct=0.1):
    """Historical 1m first-barrier long study; same-minute both is ambiguous."""
    name,tp,sl,horizon=scenario
    base={'scenario':name,'tp_pct':tp,'sl_pct':sl,'horizon_min':horizon,
          'fee_roundtrip_pct':roundtrip_fee_pct,'state':'NO_1M_FILE','exit_ts':'',
          'minutes_observed':0,'mfe_pct':'','mae_pct':'','gross_pct':'','net_pct':''}
    if times is None:return base
    ix=bisect.bisect_left(times,entry_ts)
    if ix==len(times) or times[ix]!=entry_ts:
        return {**base,'state':'NO_ENTRY_MINUTE'}
    op,hi,lo,cl=arrays
    if not open_equal(op[ix],entry_price):
        raise ValueError(f'ENTRY OPEN 1m mismatch: {entry_ts}: {op[ix]} vs {entry_price}')
    target=entry_price*(1+tp/100);stop=entry_price*(1-sl/100)
    running_hi=entry_price;running_lo=entry_price
    obs=0;last_close=entry_price
    for dt in range(horizon):
        j=ix+dt;expected=entry_ts+dt*TF1
        if j>=len(times) or times[j]!=expected:
            return {**base,'state':'CENSORED_GAP_OR_TAIL','minutes_observed':obs,
                    'mfe_pct':round(100*(running_hi/entry_price-1),6),
                    'mae_pct':round(100*(running_lo/entry_price-1),6)}
        running_hi=max(running_hi,hi[j]);running_lo=min(running_lo,lo[j]);obs+=1;last_close=cl[j]
        gross=None;state=None
        if dt>0 and op[j]>=target:state='TP_GAP_OPEN';gross=100*(op[j]/entry_price-1)
        elif dt>0 and op[j]<=stop:state='SL_GAP_OPEN';gross=100*(op[j]/entry_price-1)
        elif hi[j]>=target and lo[j]<=stop:
            return {**base,'state':'AMBIGUOUS_1M','minutes_observed':obs,
                    'exit_ts':expected,'mfe_pct':round(100*(running_hi/entry_price-1),6),
                    'mae_pct':round(100*(running_lo/entry_price-1),6)}
        elif hi[j]>=target:state='TP_FIRST';gross=tp
        elif lo[j]<=stop:state='SL_FIRST';gross=-sl
        if state:
            return {**base,'state':state,'exit_ts':expected,'minutes_observed':obs,
                    'mfe_pct':round(100*(running_hi/entry_price-1),6),
                    'mae_pct':round(100*(running_lo/entry_price-1),6),
                    'gross_pct':round(gross,6),'net_pct':round(gross-roundtrip_fee_pct,6)}
    gross=100*(last_close/entry_price-1)
    return {**base,'state':'TIMEOUT','exit_ts':entry_ts+horizon*TF1,
            'minutes_observed':obs,'mfe_pct':round(100*(running_hi/entry_price-1),6),
            'mae_pct':round(100*(running_lo/entry_price-1),6),
            'gross_pct':round(gross,6),'net_pct':round(gross-roundtrip_fee_pct,6)}


def development(entry_ts, entry_price, times, arrays, zone_low,zone_high):
    """POST-ONLY descriptions; never a predictive PRE/entry-time feature."""
    out=[]
    if times is None:return out
    ix=bisect.bisect_left(times,entry_ts)
    if ix==len(times) or times[ix]!=entry_ts:return out
    op,hi,lo,cl=arrays
    if not open_equal(op[ix],entry_price):raise ValueError('OPEN 1m changed during development')
    for horizon in DEVELOPMENT_MIN:
        last=ix+horizon-1
        available=last<len(times) and times[last]==entry_ts+(horizon-1)*TF1
        if not available:continue
        # Preserve continuity: no silent gaps in the observed interval.
        if any(times[j]!=entry_ts+(j-ix)*TF1 for j in range(ix,last+1)):
            continue
        pmax=max(hi[ix:last+1]);pmin=min(lo[ix:last+1]);
        out.append({'horizon_min':horizon,'known_at_ts':entry_ts+horizon*TF1,
                    'close_at_horizon':cl[last],
                    'close_change_pct':round(100*(cl[last]/entry_price-1),6),
                    'mfe_pct':round(100*(pmax/entry_price-1),6),
                    'mae_pct':round(100*(pmin/entry_price-1),6),
                    'relation_to_resistance':label_zone(cl[last],zone_low,zone_high),
                    'post_only':'YES_NOT_PRE_FEATURE'})
    return out


def fingerprint_flags(a):
    is_first=a.get('is_first_in_episode')=='1'
    if not is_first:return 'NOT_INDEXED_NONFIRST_ATTEMPT'
    if not a.get('fingerprint'):return 'MISSING_FIRST_FINGERPRINT'
    return a.get('fingerprint_status','UNKNOWN')


def select_review_pairs(entry_rows, outcomes):
    """Retrospective visual selection only; not a validation or signal filter."""
    out_by_entry={r['entry_id']:r for r in outcomes if r['scenario']=='TP3_SL1P5_48H'}
    winners=[];losers=[]
    for e in entry_rows:
        if e['route']!='FLIP_1M' or e['split']!='DISCOVERY_SYMBOL' or e['fingerprint_status']!='COMPLETE_PRE_96_288':continue
        result=out_by_entry.get(e['entry_id'],{})
        if result.get('state')=='TP_FIRST':winners.append(e)
        if result.get('state')=='SL_FIRST':losers.append(e)
    pairs=[];used=set()
    for good in sorted(winners,key=lambda x:(x['symbol'],x['entry_ts_int'])):
        if len(pairs)>=30:break
        candidates=[b for b in losers if b['entry_id'] not in used and b['episode_id']!=good['episode_id']
                    and b['symbol']!=good['symbol'] and b.get('pre_pattern_96')==good.get('pre_pattern_96')]
        if not candidates:continue
        # Fixed, PRE-only distance: no POST target or success term in matching.
        def distance(b):
            vals=[]
            for k,s in [('geo_low_up_share_96',.25),('rebuild_last_low_step_atr_96',.8),
                        ('level_near_share_24_96',.25),('movement_signed_efficiency_24_96',.3)]:
                x,y=good.get(k,''),b.get(k,'')
                if x!='' and y!='':vals.append(abs(float(x)-float(y))/s)
            return (sum(vals)/len(vals) if len(vals)>=2 else float('inf'),b['entry_id'])
        best=min(candidates,key=distance)
        d,_=distance(best)
        if not math.isfinite(d):continue
        used.add(best['entry_id'])
        pairs.append({'winner_entry_id':good['entry_id'],'loser_entry_id':best['entry_id'],
                      'winner_attempt_id':good['attempt_id'],'loser_attempt_id':best['attempt_id'],
                      'winner_symbol':good['symbol'],'loser_symbol':best['symbol'],
                      'winner_episode_id':good['episode_id'],'loser_episode_id':best['episode_id'],
                      'pre_pattern_96':good.get('pre_pattern_96',''),
                      'fixed_pre_distance':round(d,6),
                      'selection':'RETROSPECTIVE_VISUAL_REVIEW_NOT_PROSPECTIVE_ESTIMATE'})
    return pairs


def csv_write(path, items, cols):
    with path.open('x',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(cols),extrasaction='ignore')
        writer.writeheader();writer.writerows(items)


def build(paths, *, fee=0.1):
    manifests=verify_sources(paths)
    attempts,fp_fields=load_core(paths,manifests)
    v29_count=load_v29(paths,attempts,manifests)
    physical,links,checkpoints,link_rows,splits=load_processes(paths,attempts,manifests)
    coverage,entries=load_entry_records(paths,attempts,manifests)
    fp_cols=list(fp_fields)
    attempt_rows=[];entry_rows=[];outcomes=[];development_rows=[];context_rows=[]
    entries_by_symbol=defaultdict(list)
    for e in entries:entries_by_symbol[e['symbol']].append(e)
    # Coverage + morphology is one row for *every* pilot attempt, not just winners.
    for aid,a in sorted(attempts.items(),key=lambda x:(x[1]['symbol'],clock(x[1]['pre_closed_ts'],step=TF30),x[0])):
        common={'attempt_id':aid,'split':splits[a['symbol']],'symbol':a['symbol'],'level_id':a['level_id'],
                'episode_id':a['episode_id'],'episode_ordinal':a.get('episode_ordinal',''),
                'pre_closed_ts':clock(a['pre_closed_ts'],step=TF30),
                'breakout_closed_ts':clock(a['breakout_closed_ts'],step=TF30),
                'zone_low':a['zone_low'],'zone_high':a['zone_high'],'atr_pre':a['atr_pre'],
                'is_episode_first':a.get('is_first_in_episode',''),
                'fingerprint_status':fingerprint_flags(a),
                **a['pre'],
                'v26_status_POST':a['v26_status'],
                'v29_continuation_status_POST':a['v29_continuation_status_POST'],
                'v29_reclaim_status_POST':a['v29_reclaim_status_POST'],
                'v29_continuation_confirmed_ts_POST':a['v29_continuation_confirmed_ts_POST'],
                'v29_reclaim_confirmed_ts_POST':a['v29_reclaim_confirmed_ts_POST'],
                'post_outcome_v26_POST':a['post_outcome_v26'],
                'linked_physical_processes_retrospective':len(links[aid]),
                'linked_process_ids_retrospective':';'.join(links[aid]),
                'process_link_visibility':'RETROSPECTIVE_ONLY_NOT_ENTRY_FEATURE',
                **{k:a.get('fingerprint',{}).get(k,'') for k in fp_cols}}
        for route in ROUTES:
            common['coverage_'+route]=coverage.get((aid,route),{}).get('status','NOT_COVERED')
        attempt_rows.append(common)
    attempt_by_id={r['attempt_id']:r for r in attempt_rows}
    for sym in sorted(entries_by_symbol):
        ep=paths['minute']/f'{sym}_1m.csv'
        metap=paths['minute_manifest']/f'{sym}.json'
        if not ep.is_file() or not metap.is_file():
            raise FileNotFoundError(f'Previously verified 1m archive/manifest disappeared for {sym}')
        minute_meta=read_manifest(metap)
        if minute_meta.get('source_sha256')!=manifests['entries']['source']['pre_sha256']:
            raise ValueError(f'1m archive belongs to a different frozen PRE source: {sym}')
        expected_file_hash(ep,minute_meta.get('sha256'),f'1m archive {sym}')
        stamps,ohlc=read_minute_file(ep)
        if minute_meta.get('bars')!=len(stamps):
            raise ValueError(f'1m archive bar count differs from 1m manifest: {sym}')
        for e in sorted(entries_by_symbol[sym],key=lambda a:(a['entry_ts_int'],a['entry_id'])):
            a=attempt_by_id[e['attempt_id']]
            er={**a,'entry_id':e['entry_id'],'route':e['route'],
                'signal_ts':e['signal_ts'],'entry_ts':e['entry_ts_int'],
                'entry_ts_int':e['entry_ts_int'],'entry_price':e['entry_price_float'],
                'entry_1m_verified':'YES_SOURCE_ENTRY_VERIFIED',
                'entry_pre_known_only':'PRE_ONLY_PLUS_CLOSED_SIGNAL',
                'physical_links_retrospective_only':'YES',
                'v26_status_POST':a['v26_status_POST']}
            entry_rows.append(er)
            for scenario in SCENARIOS:
                result=score_entry(e['entry_ts_int'],e['entry_price_float'],stamps,ohlc,scenario,
                                   roundtrip_fee_pct=fee)
                outcomes.append({'entry_id':e['entry_id'],'split':a['split'],'attempt_id':e['attempt_id'],
                                 'symbol':sym,'level_id':e['level_id'],'episode_id':a['episode_id'],
                                 'route':e['route'],'entry_ts':e['entry_ts_int'],
                                 'entry_price':e['entry_price_float'],
                                 'pre_pattern_96':a.get('pre_pattern_96',''),
                                 'fingerprint_status':a['fingerprint_status'],**result})
            for d in development(e['entry_ts_int'],e['entry_price_float'],stamps,ohlc,
                                 price(a['zone_low']),price(a['zone_high'])):
                development_rows.append({'entry_id':e['entry_id'],'split':a['split'],'attempt_id':e['attempt_id'],
                                         'symbol':sym,'route':e['route'],'entry_ts':e['entry_ts_int'],**d})
            # V2 ids and regrouping are retrospective! NOT model features.
            for pid in links[e['attempt_id']]:
                p=physical[pid]
                for known,c in checkpoints[pid]:
                    context_rows.append({'entry_id':e['entry_id'],'attempt_id':e['attempt_id'],
                            'process_id':pid,'event_type':c['event_type'],
                            'known_closed_ts':known,'entry_ts':e['entry_ts_int'],
                            'relative_to_entry':'ALREADY_KNOWN' if known<=e['entry_ts_int'] else 'AFTER_ENTRY',
                            'link_is_retrospective':'YES_NOT_PREDICTIVE_FEATURE',
                            'physical_process_first_known_ts':p['first_contact_known_closed_ts']})
        print('HLL_UNIFIED_SYMBOL',json.dumps({'symbol':sym,'verified_entries':len(entries_by_symbol[sym])},ensure_ascii=False),flush=True)
    outcomes_by=(defaultdict(list))
    for o in outcomes:outcomes_by[(o['split'],o['route'],o['scenario'],o['pre_pattern_96'])].append(o)
    comparisons=[]
    for (split,route,scenario,pattern),groups in sorted(outcomes_by.items()):
        counts=Counter(g['state'] for g in groups)
        resolved=counts['TP_FIRST']+counts['SL_FIRST']+counts['TP_GAP_OPEN']+counts['SL_GAP_OPEN']
        # Timeouts and unknown states are not silently winners or losses.
        win=counts['TP_FIRST']+counts['TP_GAP_OPEN']
        comparisons.append({'split':split,'route':route,'scenario':scenario,'pre_pattern_96':pattern,
             'entries':len(groups),'unique_episodes':len(set(g['episode_id'] for g in groups)),
             'unique_symbols':len(set(g['symbol'] for g in groups)),
             'tp_first':win,'sl_first':counts['SL_FIRST']+counts['SL_GAP_OPEN'],
             'timeout':counts['TIMEOUT'],'ambiguous':counts['AMBIGUOUS_1M'],
             'censored_or_missing':len(groups)-resolved-counts['TIMEOUT']-counts['AMBIGUOUS_1M'],
             'resolved':resolved,'tp_share_resolved_pct':round(100*win/resolved,4) if resolved else '',
             'inference':'DESCRIPTIVE_ONLY_DEPENDENT_EPISODES_NOT_OOS'})
    pairs=select_review_pairs(entry_rows,outcomes)
    counts=Counter(x['route'] for x in entry_rows)
    results=Counter(o['state'] for o in outcomes if o['route']=='FLIP_1M' and o['scenario']=='TP3_SL1P5_48H')
    summary={'version':VERSION,'mode':'OFFLINE_RESEARCH_NO_LIVE',
      'pilot_symbols':len(set(a['symbol'] for a in attempts.values())),'pilot_attempts':len(attempt_rows),
      'pilot_episodes':len(set(a['episode_id'] for a in attempts.values())),
      'split_symbol_counts':dict(Counter(splits.values())),
      'frozen_v29_branch_rows':v29_count,
      'physical_processes_v2':len(physical),'physical_attempt_links_retrospective':len(link_rows),
      'attempts_with_process_links':sum(bool(links[aid]) for aid in attempts),
      'fingerprints_exact_episode_first':sum(a['fingerprint_status'] in ('COMPLETE_PRE_96_288','INCOMPLETE_PRE_288','INCOMPLETE_PRE_96') for a in attempt_rows),
      'attempts_without_exact_fingerprint':sum(a['fingerprint_status']=='NOT_INDEXED_NONFIRST_ATTEMPT' for a in attempt_rows),
      'entry_coverage_attempts':len(set(aid for aid,route in coverage)),
      'not_covered_attempts':sum(a['coverage_FLIP_1M']=='NOT_COVERED' for a in attempt_rows),
      'verified_entry_events':len(entry_rows),'verified_entries_by_route':dict(counts),
      'verified_entries_by_split':dict(Counter(e['split'] for e in entry_rows)),
      'outcome_scenarios':len(outcomes),'development_checkpoints_POST':len(development_rows),
      'retrospective_process_entry_context_rows':len(context_rows),
      'flip_reference_scenario':'TP3_SL1P5_48H',
      'flip_reference_state_counts':dict(results),
      'comparison_rows':len(comparisons),'retrospective_review_pairs':len(pairs),
      'roundtrip_fee_pct':fee,
      'limitations':[
         'FIRST_PER_LEVEL: not-covered attempts are NOT no-signals',
         'PRE full-attempt taxonomy; 35-feature fingerprints ONLY episode-first exact cut',
         'V2 physical-process membership grouped retrospectively; NEVER predictive feature',
         'Frozen detector status is POST/diagnostic, not a PRE explanatory variable',
         '1m OHLC first-barrier prices and fixed fees are hypothetical fills, no funding/slippage',
         'Within 1m TP+SL is AMBIGUOUS, missing data CENSORED, not a winner',
         'Discovery/Holdout is explicitly separated; HOLDOUT outcomes must not be used for tuning',
         'Repeated attempts and route entries are statistically dependent',
         'Retrospective review pairs are not independent proof or OOS validation',
         'No position sizing, capital, fill simulation or portfolio claim',
      ]}
    return {'unified_attempts.csv':attempt_rows, 'unified_entries.csv':entry_rows,
            'entry_outcomes_1m.csv':outcomes,'entry_development_post.csv':development_rows,
            'entry_process_context_RETROSPECTIVE.csv':context_rows,
            'physical_attempt_links_RETROSPECTIVE.csv':link_rows,
            'by_pre_structure_descriptive.csv':comparisons,
            'matched_flip_review_pairs.csv':pairs},summary,manifests


def run(root, overrides=None, fee=0.1):
    if not math.isfinite(fee) or fee<0 or fee>5:raise ValueError('Bad fee')
    paths=file_paths(root,overrides)
    output=paths['output'].resolve()
    existing_sources=[paths[k].resolve() for k in ('map','pre','fingerprints','coverage','entries','v29')]
    existing_sources.extend([paths['v2'].resolve(),paths['minute'].resolve(),paths['minute_manifest'].resolve()])
    if any(output==src or src in output.parents for src in existing_sources):
        raise ValueError('Refuse output inside a source directory/file')
    if output.exists():raise FileExistsError(f'Existing output, never overwrite: {output}')
    artifacts,summary,manifests=build(paths,fee=fee)
    output.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.hll-unified-v1-',dir=output.parent))
    try:
        sha={}
        cols={
          'unified_attempts.csv':('attempt_id','split','symbol','level_id','episode_id','episode_ordinal','pre_closed_ts','breakout_closed_ts','zone_low','zone_high','atr_pre','is_episode_first','fingerprint_status',*PRE_FIELDS,'v26_status_POST','post_outcome_v26_POST','v29_continuation_status_POST','v29_reclaim_status_POST','v29_continuation_confirmed_ts_POST','v29_reclaim_confirmed_ts_POST','coverage_FLIP_1M','coverage_V29_CONTINUATION_30M','coverage_V29_RECLAIM_30M','linked_physical_processes_retrospective','linked_process_ids_retrospective','process_link_visibility',*sorted(set(k for a in artifacts['unified_attempts.csv'] for k in a if k not in ('level_id',) and k.startswith(('geo_','rebuild_','compress_','level_','maturity_','movement_'))))),
          'unified_entries.csv':None,
          'entry_outcomes_1m.csv':('entry_id','split','attempt_id','symbol','level_id','episode_id','route','entry_ts','entry_price','pre_pattern_96','fingerprint_status','scenario','tp_pct','sl_pct','horizon_min','fee_roundtrip_pct','state','exit_ts','minutes_observed','mfe_pct','mae_pct','gross_pct','net_pct'),
          'entry_development_post.csv':('entry_id','split','attempt_id','symbol','route','entry_ts','horizon_min','known_at_ts','close_at_horizon','close_change_pct','mfe_pct','mae_pct','relation_to_resistance','post_only'),
          'entry_process_context_RETROSPECTIVE.csv':('entry_id','attempt_id','process_id','event_type','known_closed_ts','entry_ts','relative_to_entry','link_is_retrospective','physical_process_first_known_ts'),
          'physical_attempt_links_RETROSPECTIVE.csv':('process_id','attempt_id','symbol','level_id','episode_id','attempt_pre_closed_ts','attempt_observed_until','attempt_v26_status','attempt_continuation_status','attempt_reclaim_status','link_basis','retrospective_only'),
          'by_pre_structure_descriptive.csv':('split','route','scenario','pre_pattern_96','entries','unique_episodes','unique_symbols','tp_first','sl_first','timeout','ambiguous','censored_or_missing','resolved','tp_share_resolved_pct','inference'),
          'matched_flip_review_pairs.csv':('winner_entry_id','loser_entry_id','winner_attempt_id','loser_attempt_id','winner_symbol','loser_symbol','winner_episode_id','loser_episode_id','pre_pattern_96','fixed_pre_distance','selection'),
        }
        cols['unified_entries.csv']=(*cols['unified_attempts.csv'],'entry_id','route','signal_ts','entry_ts','entry_ts_int','entry_price','entry_1m_verified','entry_pre_known_only','physical_links_retrospective_only')
        for name,rs in artifacts.items():
            csv_write(tmp/name,rs,cols[name]);sha[name]=digest(tmp/name)
        (tmp/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        sha['summary.json']=digest(tmp/'summary.json')
        input_hashes={k:digest(paths[k]) for k in ('map','pre','fingerprints','entries','coverage','v29')}
        input_hashes.update({'v2/manifest.json':digest(paths['v2']/'manifest.json'),
                             'v2/process_attempt_links.csv':digest(paths['v2']/'process_attempt_links.csv')})
        # Symbol 1m archive hashes so later audits detect changed historical 1m tape.
        minute_sha={sym:(digest(paths['minute']/f'{sym}_1m.csv') if (paths['minute']/f'{sym}_1m.csv').is_file() else None)
                    for sym in sorted(set(e['symbol'] for e in artifacts['unified_entries.csv']))}
        man={'version':VERSION,'source_sha256':input_hashes,'minute_sha256':minute_sha, 'minute_manifest_sha256':{sym:digest(paths['minute_manifest']/f'{sym}.json') for sym in minute_sha},
             'outputs_sha256':sha,'entry_scope':'FIRST_PER_LEVEL','fingerprint_scope':'EPISODE_FIRST_ONLY',
             'physical_process_scope':'RETROSPECTIVE_BY_SYMBOL_LEVEL',
             'entry_reference_policy':'ARCHIVED_EXACT_1M_OPEN','no_live':True,'no_overwrite':True}
        (tmp/'manifest.json').write_text(json.dumps(man,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        if output.exists():raise FileExistsError(output)
        os.rename(tmp,output)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    print('HLL_UNIFIED_FINAL',json.dumps({'status':'DONE','output':str(output),
               'pilot_attempts':summary['pilot_attempts'],'verified_entries':summary['verified_entry_events'],
               'flips':summary['verified_entries_by_route'].get('FLIP_1M',0),
               'physical_processes':summary['physical_processes_v2'],
               'outcome_rows':summary['outcome_scenarios']},ensure_ascii=False),flush=True)
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--plan',action='store_true');g.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=Path.cwd())
    for k in DEFAULTS:
        p.add_argument('--'+k.replace('_','-'),default=None)
    p.add_argument('--roundtrip-fee-pct',type=float,default=.1)
    args=p.parse_args(argv)
    override={k:getattr(args,k) for k in DEFAULTS if getattr(args,k) is not None}
    paths=file_paths(args.root,override)
    if args.plan:
        print('HLL_UNIFIED_PLAN',json.dumps({'version':VERSION,'mode':'OFFLINE_NO_LIVE',
           'input_presence':{k:paths[k].exists() for k in ('map','pre','fingerprints','coverage','entries','v29','v2','minute','minute_manifest')},
           'output':str(paths['output']),'output_already_exists':paths['output'].exists(),
           'scope':'PILOT_V2_SYMBOLS_AND_ALL_ATTEMPTS',
           'entry_scope':'FIRST_PER_LEVEL',
           'fingerprint_scope':'EPISODE_FIRST_ONLY',
           'outcomes':'EXACT_1M_OPEN_TP_SL_NO_INTRA_MINUTE_ORDER'},ensure_ascii=False),flush=True)
        return 0
    run(args.root,override,args.roundtrip_fee_pct)
    return 0

if __name__=='__main__':raise SystemExit(main())
