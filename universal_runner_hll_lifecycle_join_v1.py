#!/usr/bin/env python3
"""Research-only causal join: Reaction First Touch + real HLL V2.1 lifecycle lines.

This is intentionally NOT the separate persistent-horizontal detector. It scans
HLL 30m levels from frozen, verified offline candles and joins each original
15m/3x3 first retest to levels formed and still alive STRICTLY BEFORE the
first-retest 1m candle. No outcomes, BTC future, Redis, orders, or LIVE writes.

The existing persistent-horizontal PRE label is retained as a separate column
so agreement/disagreement can be audited. An HLL zone matches a swing by its
frozen swing price, not by later excursion or the future outcome.

Example (VPS):
 python3 universal_runner_hll_lifecycle_join_v1.py --plan --symbols AAVEUSDT,BTCUSDT
 python3 -u universal_runner_hll_lifecycle_join_v1.py --run --symbols AAVEUSDT,BTCUSDT \
     --run-tag pilot_hll_20261108
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

import pandas as pd

VERSION = 'universal-runner-hll-lifecycle-join-v1.0'
PROJECT = Path('/home/multiservices-1m')
LABEL_DIR = Path('reports/reaction_lab/horizontal_first_touch_research_v1/runs/20261108T025001Z')
OFFLINE_DIR = Path('reports/reaction_lab/universal_runner_horizontal_history_v1/run_20261022T0000_20261108T0100')
TOUCH_DB = Path('reports/reaction_lab/scanner_first_touches_v1.sqlite3')
OUT_BASE = Path('reports/reaction_lab/universal_runner_hll_lifecycle_join_v1/runs')
TF30 = 1_800_000
MATCH_NEAR_ATR = 0.5  # predeclared descriptive band, not outcome-tuned
EXPORT_ATR = 1.0
# Full-HLL absence requires enough historical backfill to rule out an older
# still-active 30m level formed from a pivot pair before source start.
# Use maximum level lifetime + maximum pair span + confirmation and a buffer.
WARMUP_EXTRA_BARS = 2

EVENT_COLUMNS = (
    'touch_key','symbol','side','status','retest_ts','known_ts','swing_price',
    'persistent_group_pre','persistent_level_id','persistent_zone_low','persistent_zone_high',
    'hll_coverage','hll_level_count_asof','hll_near_05atr_count',
    'hll_near_1atr_count','hll_nearest_level_id','hll_nearest_origin',
    'hll_nearest_zone_low','hll_nearest_zone_high','hll_nearest_center',
    'hll_nearest_formation_closed_ts','hll_nearest_age_30m',
    'hll_nearest_gap_atr','hll_nearest_gap_pct','hll_nearest_in_zone',
    'hll_nearest_quality','hll_nearest_prior_tests','hll_nearest_prior_defenses',
    'hll_nearest_prior_losses','hll_nearest_prior_reconfirms',
    'hll_nearest_prior_cross_up','hll_nearest_prior_cross_down',
    'hll_nearest_prior_returns','hll_nearest_persistent_band_overlap',
    'hll_persistent_group','asof_cutoff_exclusive','reason'
)
CANDIDATE_COLUMNS = (
    'touch_key','symbol','status','retest_ts','level_id','origin','center','zone_low',
    'zone_high','formation_closed_ts','age_30m','gap_atr','gap_pct','in_zone',
    'quality_flag','prior_tests','prior_defenses','prior_losses','prior_reconfirms',
    'prior_cross_up','prior_cross_down','prior_returns','persistent_band_overlap'
)


def resolve(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def ro_sqlite(path: Path):
    if not path.is_file():
        raise FileNotFoundError(str(path))
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    return db


def read_frozen_labels(folder: Path) -> tuple[list[dict],dict]:
    audit_path = folder/'audit.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    if audit.get('source_mode') != 'OFFLINE_FROZEN':
        raise ValueError('Expected OFFLINE_FROZEN labels only')
    if not audit.get('no_outcomes_used_for_labels'):
        raise ValueError('Frozen labels unexpectedly used future outcomes')
    for f in ('event_labels.csv','candidate_levels.csv'):
        if sha(folder/f) != audit['sha256'][f]:
            raise ValueError(f'Frozen label hash changed: {f}')
    if audit.get('source_error_symbols'):
        raise ValueError('Source errors in frozen labels')
    with (folder/'event_labels.csv').open(newline='',encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows or len({r['touch_key'] for r in rows}) != len(rows):
        raise ValueError('Missing or duplicate frozen touch_key')
    if any(r.get('coverage_all') not in ('1','True') or
           r.get('group_pre') == 'UNKNOWN_HISTORY' for r in rows):
        raise ValueError('Uncovered event in frozen labels: cannot classify as no-level')
    return rows,audit


def parse_causal_price(payload: str) -> float | None:
    raw = json.loads(payload)
    for field in ('swing_price','pivot_price'):
        v = raw.get(field)
        try:
            p = float(v)
            if math.isfinite(p) and p > 0:
                return p
        except (ValueError,TypeError):
            pass
    return None


def read_prices(db_path: Path, symbols: set[str]) -> dict[str,dict]:
    by_key = {}
    with ro_sqlite(db_path) as db:
        for sym in sorted(symbols):
            for key,side,status,retest,known,causal in db.execute(
                'SELECT touch_key,side,status,retest_ts,known_ts,causal_json '
                'FROM first_touches WHERE symbol=?', (sym,)):
                if key in by_key:
                    raise ValueError('Duplicate touch_key in journal')
                if int(known) != int(retest)+60_000:
                    raise ValueError(f'Noncausal journal known_ts: {key}')
                by_key[key] = dict(side=side,status=status,retest_ts=int(retest),
                                   known_ts=int(known),swing_price=parse_causal_price(causal))
    return by_key


def gap(price: float, lo: float, hi: float) -> float:
    return max(0., lo-price, price-hi)


def bands_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return max(lo1,lo2) <= min(hi1,hi2)


def persistent_band(r: dict):
    if r['group_pre'] != 'PERSISTENT_LEVEL_NEAR_PRE':
        return None
    try:
        lo=float(r['chosen_zone_low']); hi=float(r['chosen_zone_high'])
        if not (math.isfinite(lo) and math.isfinite(hi) and 0<lo<=hi):
            raise ValueError('Invalid frozen persistent zone')
        return lo,hi
    except (ValueError,TypeError) as exc:
        raise ValueError('Near persistent label is missing its zone') from exc


def event_counts(ev: list[dict], cutoff: int) -> dict:
    """All HLL observations must have closed STRICTLY before retest OPEN."""
    counts = Counter()
    for row in ev:
        ts = int(row['observed_closed_ts'])
        if ts >= cutoff:
            break
        kind = row['event_type']
        if kind.startswith('TEST_FROM_'):
            counts['prior_tests'] += 1
        elif kind == 'DEFENSE_CONFIRMED':
            counts['prior_defenses'] += 1
        elif kind == 'LOSS_CONFIRMED':
            counts['prior_losses'] += 1
        elif kind == 'LEVEL_RECONFIRMED':
            counts['prior_reconfirms'] += 1
        elif kind == 'CROSS_UP':
            counts['prior_cross_up'] += 1
        elif kind == 'CROSS_DOWN':
            counts['prior_cross_down'] += 1
        elif kind == 'RETURN_INSIDE':
            counts['prior_returns'] += 1
    return {k:counts[k] for k in ('prior_tests','prior_defenses','prior_losses',
             'prior_reconfirms','prior_cross_up','prior_cross_down','prior_returns')}


def active_at(lv: dict, end: dict | None, cutoff: int, max_age_bars: int) -> bool:
    formed = int(lv['formation_closed_ts'])
    # Strict PRE: exclude HLL levels formed during the touch itself.
    if formed >= cutoff:
        return False
    # TTL is a property of the frozen HLL detector. Do not continue a level
    # after its configured 30m lifetime, even if the archive extends further.
    if cutoff - formed > (max_age_bars+1)*TF30:
        return False
    if end and end['end_state'] != 'ACTIVE_AT_END':
        closed = end.get('end_closed_ts')
        if closed not in ('',None) and int(closed) < cutoff:
            return False
    return True


def join_symbol(symbol: str, labels: list[dict], touch_data: dict[str,dict],
                formations: list[dict], observations: list[dict], ends: list[dict],
                *, max_age_bars: int = 160, warmup_until_ms: int = 0) -> tuple[list[dict],list[dict]]:
    """Pure PRE join; accepts frozen HLL scan outputs as testable inputs."""
    f_by_id = {x['level_id']:x for x in formations}
    if len(f_by_id) != len(formations):
        raise ValueError(f'Duplicate HLL level_id: {symbol}')
    by_level=defaultdict(list)
    for x in observations:
        if x['level_id'] not in f_by_id:
            raise ValueError('HLL event for nonexistent level')
        by_level[x['level_id']].append(x)
    for v in by_level.values():
        v.sort(key=lambda x:(int(x['observed_closed_ts']),x['event_id']))
    by_end={x['level_id']:x for x in ends}
    if len(by_end) != len(ends):
        raise ValueError(f'Duplicate HLL end record: {symbol}')
    # Sorted for deterministic output. The set of levels comes from the HLL
    # lifecycle scan; matching is on the original swing price, not POST price.
    ordered=sorted(formations,key=lambda x:(int(x['formation_closed_ts']),x['level_id']))
    times=[int(x['formation_closed_ts']) for x in ordered]
    out=[];candidates=[]
    for r in sorted(labels,key=lambda x:(int(x['retest_ts']),x['touch_key'])):
        key=r['touch_key']; t=touch_data.get(key)
        if t is None:
            raise ValueError(f'Frozen touch missing from journal: {key}')
        if (r['symbol']!=symbol or t['side']!=r['side'] or t['status']!=r['status'] or
            int(r['retest_ts'])!=t['retest_ts'] or int(r['known_ts'])!=t['known_ts']):
            raise ValueError(f'Frozen/journal identity mismatch: {key}')
        cutoff = int(r['retest_ts'])
        price=t['swing_price']
        pband=persistent_band(r)
        asof = [lv for lv in ordered[:bisect_left(times,cutoff)]
                if active_at(lv,by_end.get(lv['level_id']),cutoff,max_age_bars)]
        row = dict(touch_key=key,symbol=symbol,side=r['side'],status=r['status'],
            retest_ts=cutoff,known_ts=t['known_ts'],swing_price=price or '',
            persistent_group_pre=r['group_pre'],
            persistent_level_id=r.get('structural_level_id',''),
            persistent_zone_low=r.get('chosen_zone_low',''),
            persistent_zone_high=r.get('chosen_zone_high',''),
            hll_coverage='COMPLETE',hll_level_count_asof=len(asof),
            hll_near_05atr_count=0,hll_near_1atr_count=0,
            hll_nearest_level_id='',hll_nearest_origin='',
            hll_nearest_zone_low='',hll_nearest_zone_high='',hll_nearest_center='',
            hll_nearest_formation_closed_ts='',hll_nearest_age_30m='',
            hll_nearest_gap_atr='',hll_nearest_gap_pct='',hll_nearest_in_zone='',
            hll_nearest_quality='',hll_nearest_prior_tests='',
            hll_nearest_prior_defenses='',hll_nearest_prior_losses='',
            hll_nearest_prior_reconfirms='',hll_nearest_prior_cross_up='',
            hll_nearest_prior_cross_down='',hll_nearest_prior_returns='',
            hll_nearest_persistent_band_overlap='',hll_persistent_group='',
            asof_cutoff_exclusive=cutoff,reason='')
        if cutoff < warmup_until_ms:
            # Partial reconstructed HLL atlas: a missing match cannot be read as
            # genuine absence. Even positive matches are withheld from the
            # comparable full-coverage cohort. Keep candidates for inspection.
            row.update(hll_coverage='UNKNOWN_HLL_WARMUP',
                       hll_persistent_group='UNKNOWN_HLL_WARMUP',
                       reason='HLL_ATLAS_LEFT_CENSORED')
            out.append(row);continue
        if price is None:
            row.update(hll_coverage='UNKNOWN_SWING_PRICE',
                       hll_persistent_group='UNKNOWN_SWING_PRICE',
                       reason='MISSING_VALID_SWING_PRICE')
            out.append(row);continue
        ranked=[]
        for lv in asof:
            lo=float(lv['zone_low']); hi=float(lv['zone_high']);
            atr=float(lv['atr_at_formation']); center=float(lv['center'])
            if not (0<lo<=hi and atr>0 and math.isfinite(atr)):
                raise ValueError(f'Invalid HLL level OHLC geometry: {lv["level_id"]}')
            g=gap(price,lo,hi)
            d=g/atr; gap_pct=100*g/price
            overlap=int(bool(pband and bands_overlap(lo,hi,*pband)))
            info=dict(touch_key=key,symbol=symbol,status=r['status'],retest_ts=cutoff,
                level_id=lv['level_id'],origin=lv['origin'],center=center,
                zone_low=lo,zone_high=hi,
                formation_closed_ts=int(lv['formation_closed_ts']),
                age_30m=(cutoff-int(lv['formation_closed_ts']))//TF30,
                gap_atr=round(d,7),gap_pct=round(gap_pct,7),
                in_zone=int(g==0),quality_flag=lv['quality_flag'],
                persistent_band_overlap=overlap)
            if d<=EXPORT_ATR:
                info.update(event_counts(by_level.get(lv['level_id'],[]),cutoff))
                candidates.append(info)
            ranked.append((d,-int(lv['formation_closed_ts']),lv['level_id'],info))
        ranked.sort(key=lambda x:(x[0],x[1],x[2]))
        row['hll_near_05atr_count']=sum(q[0]<=MATCH_NEAR_ATR for q in ranked)
        row['hll_near_1atr_count']=sum(q[0]<=EXPORT_ATR for q in ranked)
        if ranked:
            best=ranked[0][3]
            cnt=event_counts(by_level.get(best['level_id'],[]),cutoff)
            row.update(hll_nearest_level_id=best['level_id'],
                hll_nearest_origin=best['origin'],
                hll_nearest_zone_low=best['zone_low'],
                hll_nearest_zone_high=best['zone_high'],
                hll_nearest_center=best['center'],
                hll_nearest_formation_closed_ts=best['formation_closed_ts'],
                hll_nearest_age_30m=best['age_30m'],
                hll_nearest_gap_atr=best['gap_atr'],
                hll_nearest_gap_pct=best['gap_pct'],
                hll_nearest_in_zone=best['in_zone'],
                hll_nearest_quality=best['quality_flag'],
                hll_nearest_persistent_band_overlap=best['persistent_band_overlap'])
            for k,v in cnt.items():
                row['hll_nearest_'+k]=v
        near=bool(ranked and ranked[0][0]<=MATCH_NEAR_ATR)
        pnear=r['group_pre']=='PERSISTENT_LEVEL_NEAR_PRE'
        overlap_near=any(q[0]<=MATCH_NEAR_ATR and q[3]['persistent_band_overlap']
                         for q in ranked)
        row['hll_persistent_group']=(
            ('BOTH_NEAR_OVERLAPPING_BANDS' if overlap_near else
             'BOTH_NEAR_DIFFERENT_BANDS') if near and pnear else
            'HLL_ONLY' if near else
            'PERSISTENT_ONLY' if pnear else 'NEITHER_NEAR')
        row['reason']='STRICTLY_PRE;HLL_V21_CAUSAL;NO_OUTCOMES'
        out.append(row)
    return out,candidates


def write_csv(path: Path, rows: list[dict], cols: tuple):
    with path.open('w',encoding='utf-8',newline='') as f:
        wr=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');wr.writeheader()
        wr.writerows(rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    parser.add_argument('--project',type=Path,default=PROJECT)
    parser.add_argument('--label-dir',type=Path,default=LABEL_DIR)
    parser.add_argument('--offline-history-dir',type=Path,default=OFFLINE_DIR)
    parser.add_argument('--symbols',default='')
    parser.add_argument('--run-tag',default='pilot_hll_v1')
    parser.add_argument('--min-free-gb',type=float,default=10.)
    args=parser.parse_args(argv)
    root=args.project.resolve()
    lab=resolve(root,args.label_dir)
    history=resolve(root,args.offline_history_dir)
    label_rows, audit=read_frozen_labels(lab)
    by_symbol=defaultdict(list)
    for x in label_rows:by_symbol[x['symbol']].append(x)
    syms=sorted(by_symbol)
    if args.symbols:
        selected={s.strip().upper() for s in args.symbols.split(',') if s.strip()}
        if not selected or not selected.issubset(by_symbol):
            raise ValueError(f'Unknown requested symbols: {selected-set(by_symbol)}')
        syms=[s for s in syms if s in selected]
    if not args.run_tag or not all(c.isalnum() or c in '._-' for c in args.run_tag):
        raise ValueError('Bad --run-tag')
    dst=root/OUT_BASE/args.run_tag
    info=dict(version=VERSION,mode='PLAN_READ_ONLY' if args.plan else 'RUN_OFFLINE',
         label_sha256=audit['sha256']['event_labels.csv'],
         labels=len(label_rows),symbols=len(syms),
         requested_events=sum(len(by_symbol[x]) for x in syms),
         label_dir=str(lab),history_dir=str(history),output_dir=str(dst),
         detector='HLL_LIFECYCLE_V21_30M_TWO_CONFIRMED_2x2_PIVOTS',
         match_atr=MATCH_NEAR_ATR,history_complete_required=True,
         asof='strictly before original retest 1m OPEN',
         read_only_existing_sources=True,no_outcomes=True)
    print('UNIVERSAL_HLL_JOIN_PLAN',json.dumps(info,ensure_ascii=False),flush=True)
    if args.plan:return 0
    import shutil
    if shutil.disk_usage(root).free/1024**3 < args.min_free_gb:
        raise RuntimeError(f'Insufficient free disk (<{args.min_free_gb} GB)')
    if dst.exists():
        raise ValueError(f'Run tag exists (will never overwrite): {dst}')
    # Delayed HLL import: --plan can inspect without loading HLL dependencies.
    import horizontal_level_lifecycle_v21 as hll
    import universal_runner_horizontal_history_v1 as source
    if hll.VERSION!='horizontal-level-lifecycle-v2.1-geometry':
        raise RuntimeError('Unexpected HLL lifecycle engine version')
    prices=read_prices(root/TOUCH_DB,set(syms))
    dst.mkdir(parents=True,exist_ok=False)
    all_events=[];all_candidates=[];summaries=[];source_hashes={}
    source_bounds=None
    for idx,symbol in enumerate(syms,1):
        meta_path=history/'symbols'/symbol/'manifest.json'
        if not meta_path.is_file():
            raise RuntimeError(f'Missing offline manifest: {symbol}')
        observed=json.loads(meta_path.read_text(encoding='utf-8'))
        start,end=int(observed['start']),int(observed['end'])
        if source_bounds is None:
            source_bounds=(start,end)
        elif source_bounds!=(start,end):
            raise RuntimeError(f'Inconsistent offline source date range: {symbol}')
        meta=source.verify_symbol(history,symbol,start,end)
        if meta is None or any(meta['timeframes'][tf]['validation']['status']!='FULL'
                               for tf in ('15m','30m')):
            raise RuntimeError(f'Incomplete verified horizontal history: {symbol}')
        source_hashes[symbol]=sha(history/'symbols'/symbol/'manifest.json')
        candles=pd.read_csv(history/'symbols'/symbol/'30m.csv',encoding='utf-8-sig')
        formations,observations,ends,diag=hll.scan_symbol(
            symbol,candles,hll.Config(),source='UNIVERSAL_OFFLINE_VERIFIED_30M')
        if diag['status']!='COMPLETE' or diag['gaps30']!=0:
            raise RuntimeError(f'HLL scan failed or gapped: {symbol} {diag}')
        if any(int(x['retest_ts']) >= end for x in by_symbol[symbol]):
            raise RuntimeError(f'Touch after frozen history end: {symbol}')
        cfg=hll.Config()
        warmup_until=start+(cfg.max_age_bars+cfg.max_pivot_span_bars+
                            cfg.right+WARMUP_EXTRA_BARS)*TF30
        events,candidates=join_symbol(symbol,by_symbol[symbol],prices,
                                     formations,observations,ends,
                                     max_age_bars=cfg.max_age_bars,
                                     warmup_until_ms=warmup_until)
        all_events.extend(events);all_candidates.extend(candidates)
        summaries.append(dict(symbol=symbol,events=len(events),levels=len(formations),
            near=sum(bool(e['hll_near_05atr_count']) for e in events),
            unknown=sum(e['hll_coverage']!='COMPLETE' for e in events)))
        if idx%5==0 or idx==len(syms):
            print('UNIVERSAL_HLL_JOIN_PROGRESS',json.dumps(dict(
                symbols_done=idx,total_symbols=len(syms),events_done=len(all_events),
                hll_near=sum(e['hll_near_05atr_count']>0 for e in all_events),
                unknown=sum(e['hll_coverage']!='COMPLETE' for e in all_events)),
                ensure_ascii=False),flush=True)
    write_csv(dst/'event_hll_labels.csv',all_events,EVENT_COLUMNS)
    write_csv(dst/'hll_candidates_near_1atr.csv',all_candidates,CANDIDATE_COLUMNS)
    write_csv(dst/'symbol_summary.csv',summaries,('symbol','events','levels','near','unknown'))
    groups=Counter(e['hll_persistent_group'] for e in all_events)
    audit_out=dict(version=VERSION,symbols=len(syms),events=len(all_events),
        statuses=dict(Counter(e['status'] for e in all_events)),
        cross_groups=dict(groups),
        hll_near_05atr=sum(e['hll_near_05atr_count']>0 for e in all_events),
        coverage=dict(Counter(e['hll_coverage'] for e in all_events)),
        candidate_records=len(all_candidates),
        source_label_sha256=audit['sha256']['event_labels.csv'],
        source_labels=str(lab),offline_history_dir=str(history),
        offline_manifest_sha256=source_hashes,
        hll_scanner=hll.VERSION,config=hll.Config().__dict__,
        source_hashes={name:sha(dst/name) for name in
            ('event_hll_labels.csv','hll_candidates_near_1atr.csv','symbol_summary.csv')},
        no_post_outcomes_read=True,read_only_to_project=True,
        analysis_status='PRE_GEOMETRY_ONLY_NOT_AN_EDGE',
        output_dir=str(dst))
    (dst/'audit.json').write_text(json.dumps(audit_out,indent=2,ensure_ascii=False),encoding='utf-8')
    print('UNIVERSAL_HLL_JOIN_RESULT',json.dumps({k:v for k,v in audit_out.items()
          if k not in ('offline_manifest_sha256','source_hashes','config')},ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
