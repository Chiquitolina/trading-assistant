#!/usr/bin/env python3
"""Canonical, independent replay-parity for frozen PRE-Reaction Opportunity Lab pilot.

READ ONLY: frozen pilot, isolated native 1m cache and original dashboard/app.py.
Uses AST extraction in reaction_lab_continuous_scanner.compile_canonical_scanner.
Never starts Streamlit, Redis, LIVE scanner or Binance connections.
Writes only a new immutable report directory.

A comparison with the observational scanner journal is *not* complete replay parity.
This script calls the canonical detection directly against complete historical bars.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path

from pre_reaction_opportunity_lab import (
    contiguous_15m_from_1m, load_native_1m, iso_ms, sha,
)

VERSION='pre-reaction-canonical-replay-parity-v1.0'
ONE=60_000
FIFTEEN=15*ONE
ROOT_OUT=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/canonical_replay_parity_v1')
PILOT=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/pilot_20261012_20261013_HANAUSDT_TAKEUSDT_ETHUSDT')
CACHE=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/native_1m_pilot_cache')


def checked_manifest(root,pilot):
    m=json.loads((pilot/'manifest.json').read_text())
    if sha(pilot/'opportunities_PRE.csv')!=m['opportunities_sha256']:
        raise RuntimeError('Frozen pilot SHA256 mismatch')
    for raw,digest in m.get('source_hashes',{}).items():
        p=Path(raw)
        if not p.is_file():p=root/raw
        if not p.is_file() or sha(p)!=digest:
            raise RuntimeError(f'Historical native 1m changed or missing: {p}')
    symbols=list(m['symbols'])
    if len(set(symbols))!=len(symbols):raise RuntimeError('Duplicate symbols in frozen pilot')
    return m


def read_csv(path):
    with path.open(newline='',encoding='utf-8') as fh:return list(csv.DictReader(fh))


def as_ts(v):
    if v in ('',None):return None
    if isinstance(v,float) and math.isnan(v):return None
    return int(float(v))


def key(symbol,side,pivot_ts,action_ts):
    return (str(symbol).upper(),str(side).upper(),int(pivot_ts),int(action_ts))


def pilot_key(row):
    return key(row['symbol'],row['side'],as_ts(row['pivot_ts']),as_ts(row['actionable_ts']))


def canonical_key(row):
    return key(row['symbol'],row['signal'],as_ts(row['pivot_timestamp']),as_ts(row['actionable_timestamp']))


def classification(row,canonical):
    """Compare first eligible 1m contact. Never classify no-contact as an outcome win/loss."""
    pilot_status=str(row.get('status',''))
    if canonical is None:
        if pilot_status in ('ARMED_NO_RETEST','NO_DEPARTURE'):
            return 'NO_TOUCH_BOTH_UNRESOLVED_CAUSAL_IDENTITY'
        if pilot_status=='UNKNOWN_GAP':return 'UNKNOWN_GAP_NO_PARITY'
        return 'PILOT_TOUCH_ABSENT_CANONICAL'
    if pilot_status in ('ARMED_NO_RETEST','NO_DEPARTURE','UNKNOWN_GAP'):
        return 'CANONICAL_TOUCH_PILOT_NO_TOUCH'
    canonical_status=str(canonical['status'])
    pt=as_ts(row['first_touch_ts'])
    ct=as_ts(canonical['retest_timestamp'])
    if pt!=ct:return 'FIRST_TOUCH_TS_MISMATCH'
    if canonical_status!=pilot_status:return 'FIRST_TOUCH_STATUS_MISMATCH'
    return 'EXACT_FIRST_TOUCH_PARITY'


def summarize(rows,can_only):
    counts=Counter(r['parity_status'] for r in rows)
    bysymbol={}
    for sym in sorted(set(r['symbol'] for r in rows)):
        ss=[r for r in rows if r['symbol']==sym]
        bysymbol[sym]={'pilot':len(ss),'categories':dict(Counter(r['parity_status'] for r in ss))}
    return {'pilot_total':len(rows),'pilot_touches':sum(r['pilot_status'] in ('REACTION','TOUCH_FAILED','TOUCH_INDECISIVE') for r in rows),
            'pilot_reactions':sum(r['pilot_status']=='REACTION' for r in rows),
            'exact_first_touches':counts['EXACT_FIRST_TOUCH_PARITY'],
            'hard_disagreements':sum(counts[c] for c in ('PILOT_TOUCH_ABSENT_CANONICAL','CANONICAL_TOUCH_PILOT_NO_TOUCH','FIRST_TOUCH_TS_MISMATCH','FIRST_TOUCH_STATUS_MISMATCH')),
            'canonical_only_touches':len(can_only), 'categories':dict(counts), 'by_symbol':bysymbol}


def write_rows(path,rows,fields):
    with path.open('w',encoding='utf-8',newline='') as h:
        w=csv.DictWriter(h,fieldnames=fields,extrasaction='ignore')
        w.writeheader();w.writerows(rows)


def run(project,pilot,cache,out,plan=False):
    m=checked_manifest(project,pilot)
    app=project/'dashboard/app.py'
    scanner=project/'reaction_lab_continuous_scanner.py'
    if not app.is_file() or not scanner.is_file():
        raise FileNotFoundError('Need dashboard/app.py and reaction_lab_continuous_scanner.py already present on VPS')
    start=iso_ms(m['start_utc']);end=iso_ms(m['end_exclusive_utc'])
    if plan:
        print('PRE_REACTION_CANONICAL_PLAN',json.dumps({'version':VERSION,'pilot':str(pilot),
            'cache':str(cache),'output':str(out),'symbols':m['symbols'],
            'dashboard_app_sha256':sha(app),'canonical_scanner_module_sha256':sha(scanner),
            'start':m['start_utc'],'end_exclusive':m['end_exclusive_utc'],
            'read_only_sources':True,'no_network':True,'will_replay_first_touches':True},indent=2))
        return
    if out.exists():raise RuntimeError(f'Immutable output exists: {out}')
    from reaction_lab_continuous_scanner import compile_canonical_scanner
    candles={}
    metadata=[]
    # Replay data remain complete within the available pilot horizon; no synthetic bars.
    for sym in m['symbols']:
        warm=start-3*60*ONE; finish=end+360*ONE+ONE
        one,files=load_native_1m(project,cache.relative_to(project) if cache.is_absolute() else cache,sym,warm,finish)
        expected=(finish-warm)//ONE
        if len(one)!=expected or int(one['timestamp'].min())!=warm or int(one['timestamp'].max())!=finish-ONE:
            raise RuntimeError(f'1m coverage incomplete for {sym}: got={len(one)} expected={expected}')
        timestamps=one['timestamp'].astype('int64').to_numpy()
        if any(int(timestamps[i+1])-int(timestamps[i])!=ONE for i in range(len(timestamps)-1)):
            raise RuntimeError(f'Noncontiguous 1m: {sym}')
        fifteen=contiguous_15m_from_1m(one)
        if len(fifteen)<15:raise RuntimeError(f'Not enough 15m for {sym}')
        # Cache source file hashes are checked separately in frozen manifest.
        candles[sym]={'1m':one,'15m':fifteen}
        metadata.append({'symbol':sym,'1m_rows':len(one),'15m_rows':len(fifteen),'files':[str(f) for f in files]})
    def loader(symbol,timeframe,limit):
        tf=str(timeframe);sym=str(symbol).upper()
        d=candles.get(sym,{}).get(tf)
        if d is None:raise RuntimeError(f'Requested unsupported history {sym} {tf}')
        return d.tail(int(limit)).copy()
    scan, fingerprint=compile_canonical_scanner(app,loader)
    canonical=scan(symbols=tuple(m['symbols']),swing_timeframes=('15m',),
        swing_detector_items=(('15m','3x3'),),min_swing_prominence_pct=0.0,
        retest_tolerance_pct=0.10,min_departure_pct=0.20,
        max_age_minutes=360,max_retest_age_minutes=4320)
    canon={}
    if canonical is not None and not canonical.empty:
        for rec in canonical.to_dict('records'):
            action=as_ts(rec['actionable_timestamp'])
            if action is None or not start<=action<end:continue
            k=canonical_key(rec)
            if k in canon:
                # canonical can have redundant events? force investigation, not arbitrary selection
                raise RuntimeError(f'Canonical duplicate swing-event key: {k}')
            if str(rec['status']) not in ('REACTION','TOUCH_FAILED','TOUCH_INDECISIVE'):
                raise RuntimeError(f'Unexpected canonical status {rec["status"]}')
            canon[k]=rec
    rows=read_csv(pilot/'opportunities_PRE.csv')
    if len(rows)!=int(m['total']):raise RuntimeError('Frozen manifest pilot row count changed')
    out_rows=[];seen=set()
    for r in rows:
        k=pilot_key(r)
        if k in seen:raise RuntimeError(f'Duplicate pilot key: {k}')
        seen.add(k)
        found=canon.get(k)
        status=classification(r,found)
        out_rows.append({'opportunity_key':r['opportunity_key'],'symbol':r['symbol'],'side':r['side'],
            'pivot_ts':r['pivot_ts'],'actionable_ts':r['actionable_ts'],
            'pilot_status':r['status'],'pilot_first_touch_ts':r.get('first_touch_ts',''),
            'canonical_status':str(found['status']) if found is not None else '',
            'canonical_first_touch_ts':int(found['retest_timestamp']) if found is not None else '',
            'parity_status':status})
    can_only=[]
    for k,c in canon.items():
        if k not in seen:
            can_only.append({'symbol':k[0],'side':k[1],'pivot_ts':k[2],
                'actionable_ts':k[3],'canonical_status':c['status'],
                'canonical_first_touch_ts':int(c['retest_timestamp'])})
    out_rows.sort(key=lambda x:(x['symbol'],int(x['actionable_ts']),x['side']))
    can_only.sort(key=lambda x:(x['symbol'],x['actionable_ts'],x['side']))
    result=summarize(out_rows,can_only)
    out.mkdir(parents=True)
    base_file=out/'first_touch_canonical_comparison.csv';only_file=out/'canonical_only.csv'
    write_rows(base_file,out_rows,['opportunity_key','symbol','side','pivot_ts','actionable_ts','pilot_status','pilot_first_touch_ts','canonical_status','canonical_first_touch_ts','parity_status'])
    write_rows(only_file,can_only,['symbol','side','pivot_ts','actionable_ts','canonical_status','canonical_first_touch_ts'])
    report={'version':VERSION,'audit_label':'independent_full_historical_AST_canonical_replay',
        'canonical_fingerprint':fingerprint,'replay_parameters':{'timeframe':'15m','swings':'3x3',
            'min_prominence_pct':0,'departure_pct':0.20,'tolerance_pct':0.10,'max_age_minutes':360},
        'window':{'start':m['start_utc'],'end_exclusive':m['end_exclusive_utc']},
        'historical_coverage':metadata,'inputs_sha256':{'frozen_pilot':sha(pilot/'opportunities_PRE.csv'),
            'dashboard_app':sha(app),'canonical_scanner_module':sha(scanner)},
        'summary':result,'outputs_sha256':{'comparison':sha(base_file),'canonical_only':sha(only_file)},
        'limitations':['This compares independent implementations on the same historical candles, not a forward prediction.',
            'Untouched swings without a first retest cannot be proved equivalent by an event-only canonical API.',
            'Canonical scanner uses 3x3 swings and first-touch path with same frozen settings.',
            'Historical pilot 1m monthly files are partial-month and isolated; never reuse as full-month cache.']}
    (out/'canonical_replay_parity_report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print('PRE_REACTION_CANONICAL_PARITY',json.dumps(result,sort_keys=True),flush=True)
    print('PRE_REACTION_CANONICAL_PATH',out)


def audit(out,project,pilot):
    meta=json.loads((out/'canonical_replay_parity_report.json').read_text())
    sources={'dashboard_app':project/'dashboard/app.py',
        'canonical_scanner_module':project/'reaction_lab_continuous_scanner.py',
        'frozen_pilot':pilot/'opportunities_PRE.csv'}
    for name,source in sources.items():
        if not source.is_file() or sha(source)!=meta['inputs_sha256'][name]:
            raise RuntimeError(f'Frozen provenance changed: {name}: {source}')
    for label,name in (('comparison','first_touch_canonical_comparison.csv'),('canonical_only','canonical_only.csv')):
        if sha(out/name)!=meta['outputs_sha256'][label]:raise RuntimeError(f'Report file changed: {name}')
    rows=read_csv(out/'first_touch_canonical_comparison.csv');other=read_csv(out/'canonical_only.csv')
    if len(rows)!=meta['summary']['pilot_total'] or len(other)!=meta['summary']['canonical_only_touches']:
        raise RuntimeError('Report row-count mismatch')
    agg=summarize(rows,other)
    if agg!=meta['summary']:raise RuntimeError('Report distribution mismatch')
    print('PRE_REACTION_CANONICAL_AUDIT',json.dumps({'success':True,**agg,'folder':str(out)},sort_keys=True))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    g=ap.add_mutually_exclusive_group(required=True)
    for name in ('plan','run','audit'):g.add_argument('--'+name,action='store_true')
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--pilot',type=Path,default=PILOT)
    ap.add_argument('--cache',type=Path,default=CACHE)
    ap.add_argument('--output',type=Path,default=ROOT_OUT)
    a=ap.parse_args(); root=a.project.resolve()
    def abs_path(p):return (p if p.is_absolute() else root/p).resolve()
    pilot=abs_path(a.pilot);cache=abs_path(a.cache);out=abs_path(a.output)
    if a.audit:audit(out,root,pilot)
    else:run(root,pilot,cache,out,plan=a.plan)

if __name__=='__main__':main()
