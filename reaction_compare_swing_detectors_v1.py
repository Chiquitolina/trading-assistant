#!/usr/bin/env python3
"""Offline causal 2x2 vs 3x3 vs 5x5 on identical symbols and native 1m bars.

Never imports dashboard.app, uses existing safe AST canonical scanner extraction.
Does not query Binance, Redis, SQLite, modify caches or place orders.
Outputs immutable reports only. Based on frozen PRE-Reaction v0.1 rules.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from pre_reaction_opportunity_lab import (
    ONE, FIFTEEN, contiguous_15m_from_1m, load_native_1m,
    scan_opportunity, iso_ms, sha, utc,
)

VERSION = 'reaction-swing-comparison-v1.0'
DETECTORS=(2,3,5)
DEFAULT_CACHE=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/native_1m_pilot_cache')
DEFAULT_OUT=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/detector_comparison_2_3_5_v1')
DEFAULT_BASELINE=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0/pilot_20261012_20261013_HANAUSDT_TAKEUSDT_ETHUSDT')
TOUCHES=('REACTION','TOUCH_FAILED','TOUCH_INDECISIVE')
RATES=((1.0,1.0),(2.0,1.0))
HORIZONS=(180,360)


def csv_write(path,rows,columns):
    with path.open('w',encoding='utf-8',newline='') as handle:
        w=csv.DictWriter(handle,fieldnames=columns,extrasaction='ignore')
        w.writeheader();w.writerows(rows)


def as_int(value):
    if value is None or value=='':return None
    if isinstance(value,float) and math.isnan(value):return None
    return int(float(value))


def assert_full_minute_coverage(frame,start,end_exclusive,symbol):
    expected=(end_exclusive-start)//ONE
    if end_exclusive<=start or (end_exclusive-start)%ONE:
        raise ValueError('Nonminute-aligned boundaries')
    if len(frame)!=expected:
        raise RuntimeError(f'{symbol} incomplete 1m: {len(frame)}/{expected}')
    ts=frame.timestamp.astype('int64').to_numpy()
    if not len(ts) or int(ts[0])!=start or int(ts[-1])!=end_exclusive-ONE:
        raise RuntimeError(f'{symbol} incorrect historical boundaries')
    if any(int(ts[i+1])-int(ts[i])!=ONE for i in range(len(ts)-1)):
        raise RuntimeError(f'{symbol} gap/duplicate 1m detected')


def find_variant_opportunities(sym,one,fif,start,end,n):
    """SwingDetector uses strictly n preceding/n following completed 15m candles.

    Swing actionable only AFTER rightmost confirmation 15m candle has closed.
    One-minute opportunity detector is identical for all 3 variants.
    """
    from engine.live.research.swing_detector import SwingDetector
    bars=fif.to_dict('records')
    timestamps={int(r['timestamp']):i for i,r in enumerate(bars)}
    one_rows=one.to_dict('records')
    minute_idx={int(r['timestamp']):i for i,r in enumerate(one_rows)}
    result=[]
    for point in SwingDetector(n,n,0.0).detect_all(bars):
        action=int(point.confirmed_timestamp)+FIFTEEN
        if not start<=action<end:continue
        pivot_index=timestamps.get(int(point.pivot_timestamp))
        confirm_index=timestamps.get(int(point.confirmed_timestamp))
        if pivot_index is None or confirm_index!=pivot_index+n or pivot_index<n:
            continue
        segment=[int(r['timestamp']) for r in bars[pivot_index-n:confirm_index+1]]
        if len(segment)!=2*n+1 or any(b-a!=FIFTEEN for a,b in zip(segment,segment[1:])):
            continue
        idx=minute_idx.get(action)
        opportunity=scan_opportunity(one_rows[idx:idx+361] if idx is not None else [],point)
        opportunity.update(symbol=sym,detector=f'{n}x{n}',
            pivot_identity=f'{sym}|{opportunity["side"]}|{int(point.pivot_timestamp)}',
            opportunity_key=f'{sym}|{n}x{n}|{opportunity["side"]}|{int(point.pivot_timestamp)}|{action}',
            prominence_pct=point.prominence_pct)
        result.append(opportunity)
    return result


def first_touch_trade_path(one_lookup,side,touch_ts,tp_pct,sl_pct,horizon):
    """Theoretical next-minute OPEN; OHLC-first barrier; both hits => AMBIGUOUS.

    Does not interpret censored histories as losses or as NO_HIT.
    The first-touch bar itself is NEVER an entry bar.
    """
    entry_ts=int(touch_ts)+ONE
    entry=one_lookup.get(entry_ts)
    if entry is None:
        return {'status':'CENSORED_NO_ENTRY','entry_ts':entry_ts,'entry':None,'exit_ts':None,'bars_observed':0}
    price=float(entry['open'])
    if not price>0:raise ValueError('Invalid entry open')
    if side=='LONG':
        target=price*(1+tp_pct/100);stop=price*(1-sl_pct/100)
    else:
        target=price*(1-tp_pct/100);stop=price*(1+sl_pct/100)
    observed=0
    for i in range(horizon):
        ts=entry_ts+i*ONE
        candle=one_lookup.get(ts)
        if candle is None:
            return {'status':'CENSORED_GAP_OR_END','entry_ts':entry_ts,'entry':price,
                'exit_ts':None,'bars_observed':observed}
        high=float(candle['high']);low=float(candle['low'])
        tp=(high>=target if side=='LONG' else low<=target)
        sl=(low<=stop if side=='LONG' else high>=stop)
        observed+=1
        if tp and sl:status='AMBIGUOUS_BOTH_IN_1M'
        elif tp:status='TP_FIRST'
        elif sl:status='SL_FIRST'
        else:continue
        return {'status':status,'entry_ts':entry_ts,'entry':price,'exit_ts':ts,
            'bars_observed':observed}
    return {'status':'NO_HIT','entry_ts':entry_ts,'entry':price,'exit_ts':None,'bars_observed':observed}


def evaluate_outcomes(opportunities,minute_lookups):
    out=[]
    for r in opportunities:
        if r['status']!='REACTION':continue
        lookup=minute_lookups[r['symbol']]
        for tp,sl in RATES:
            for minutes in HORIZONS:
                v=first_touch_trade_path(lookup,r['side'],r['first_touch_ts'],tp,sl,minutes)
                out.append(dict(symbol=r['symbol'],detector=r['detector'],side=r['side'],
                    pivot_identity=r['pivot_identity'],opportunity_key=r['opportunity_key'],
                    actionable_ts=r['actionable_ts'],first_touch_ts=r['first_touch_ts'],
                    tp_pct=tp,sl_pct=sl,horizon_minutes=minutes,**v))
    return out


def compare_canonical(opportunities,canonical_records,n):
    """Canonical scanner emits touch events, not the complete no-touch swing census."""
    recorded={}
    for r in canonical_records:
        k=(str(r['symbol']),str(r['signal']),as_int(r['pivot_timestamp']),as_int(r['actionable_timestamp']))
        if k in recorded:raise RuntimeError(f'Canonical duplicate: {k}')
        recorded[k]=r
    found=set();by_key={}
    for r in opportunities:
        key=(r['symbol'],r['side'],int(r['pivot_ts']),int(r['actionable_ts']))
        canonical=recorded.get(key)
        if canonical is None:
            status='BOTH_NO_TOUCH_UNRESOLVED' if r['status'] not in TOUCHES else 'MISSING_CANONICAL_TOUCH'
        else:
            found.add(key)
            if r['status'] not in TOUCHES:status='PILOT_MISSED_TOUCH'
            elif as_int(r['first_touch_ts'])!=as_int(canonical['retest_timestamp']):status='TOUCH_TS_DIFFERENCE'
            elif r['status']!=str(canonical['status']):status='TOUCH_CLASS_DIFFERENCE'
            else:status='EXACT_TOUCH'
        by_key[r['opportunity_key']]=status
    missing=set(recorded)-found
    return by_key, sorted(missing)


def match_pivots(opportunities):
    grouped=defaultdict(dict)
    for r in opportunities:
        k=r['pivot_identity'];n=r['detector']
        if n in grouped[k]:raise RuntimeError(f'Duplicate detector pivot: {k} {n}')
        grouped[k][n]=r
    rows=[]
    for key,group in grouped.items():
        present=[f'{n}x{n}' for n in DETECTORS if f'{n}x{n}' in group]
        row={'pivot_identity':key,'symbol':key.split('|')[0], 'side':key.split('|')[1],
            'pivot_ts':int(key.split('|')[2]),'detectors_present':','.join(present)}
        for n in DETECTORS:
            det=f'{n}x{n}';r=group.get(det)
            row.update({f'action_{det}':r['actionable_ts'] if r else '',
                f'status_{det}':r['status'] if r else '',
                f'touch_{det}':r['first_touch_ts'] if r and r['first_touch_ts'] is not None else ''})
        if '2x2' in group and '5x5' in group:
            row['confirmation_delay_5_minus_2_minutes']=(group['5x5']['actionable_ts']-group['2x2']['actionable_ts'])//ONE
            first2=group['2x2']['first_touch_ts']
            row['2x2_touch_before_5x5_action']=bool(first2 is not None and first2<group['5x5']['actionable_ts'])
        else:
            row['confirmation_delay_5_minus_2_minutes']=''
            row['2x2_touch_before_5x5_action']=''
        rows.append(row)
    return sorted(rows,key=lambda r:(r['symbol'],r['pivot_ts'],r['side']))


def summaries(opps,outcomes,matches,canonical_only):
    counts={};futures={}
    for n in DETECTORS:
        name=f'{n}x{n}';rows=[x for x in opps if x['detector']==name]
        cats=Counter(x['status'] for x in rows)
        counts[name]={'all_actionable':len(rows),'by_status':dict(sorted(cats.items())),
            'reaction_per_actionable':round(cats['REACTION']/len(rows),6) if rows else None,
            'any_first_touch_per_actionable':round(sum(cats[s] for s in TOUCHES)/len(rows),6) if rows else None,
            'by_direction':{s:{'all':sum(x['side']==s for x in rows),
                'reactions':sum(x['side']==s and x['status']=='REACTION' for x in rows)} for s in ('LONG','SHORT')},
            'canonical_parity':dict(sorted(Counter(x['canonical_parity'] for x in rows).items())),
            'canonical_only_touches':len(canonical_only.get(name,[]))}
        futures[name]={}
        for tp,sl in RATES:
            for h in HORIZONS:
                subset=[x for x in outcomes if x['detector']==name and x['tp_pct']==tp and x['sl_pct']==sl and x['horizon_minutes']==h]
                key=f'TP{tp:g}_SL{sl:g}_{h}m'
                stats=Counter(x['status'] for x in subset)
                resolved=stats['TP_FIRST']+stats['SL_FIRST']
                futures[name][key]={'all_reaction_entries':len(subset), 'statuses':dict(sorted(stats.items())),
                    'tp_first_rate_resolved_only':round(stats['TP_FIRST']/resolved,6) if resolved else None,
                    'resolved_tp_sl':resolved,'no_fee_no_slippage':True}
    return {'detectors':counts,'outcomes':futures,'pivot_overlap':dict(sorted(Counter(x['detectors_present'] for x in matches).items()))}


def run(root,cache,out,start,end,symbols,mode,baseline):
    sources={};cache=cache if cache.is_absolute() else root/cache
    app=root/'dashboard/app.py'; scanner_file=root/'reaction_lab_continuous_scanner.py'
    if not app.is_file() or not scanner_file.is_file():
        raise FileNotFoundError('Need current dashboard/app.py and reaction_lab_continuous_scanner.py')
    warm=start-3*60*ONE
    # 360m for opportunity classification, 360m extra for outcomes.
    # The pilot cache only has the first 360m extra; late outcomes are censored,
    # never silently treated as NO_HIT.
    finish=end+360*ONE+ONE
    for sym in symbols:
        if mode=='plan':
            sources[sym]={'monthly_partition':str(cache/sym/(datetime.fromtimestamp(start/1000,timezone.utc).strftime('%Y-%m')+'.pkl.gz')),
                'present':(cache/sym/(datetime.fromtimestamp(start/1000,timezone.utc).strftime('%Y-%m')+'.pkl.gz')).is_file()}
        else:
            one,files=load_native_1m(root,cache.relative_to(root) if cache.is_relative_to(root) else cache,sym,warm,finish)
            assert_full_minute_coverage(one,warm,finish,sym)
            fifteen=contiguous_15m_from_1m(one)
            if len(fifteen)<15:raise RuntimeError(f'Insufficient 15m coverage: {sym}')
            sources[sym]={'1m':one,'15m':fifteen,'files':[str(f) for f in files]}
    if mode=='plan':
        print('REACTION_DETECTOR_COMPARE_PLAN',json.dumps({'version':VERSION,'symbols':symbols,
            'window':[utc(start),utc(end)],'required_native_1m':[utc(warm),utc(finish)],
            'detectors':[f'{n}x{n}' for n in DETECTORS], 'source':sources,
            'output':str(out),'no_network':True,'read_only_inputs':True,
            'financial_outcome_horizons_minutes':list(HORIZONS),
            'late_post_reaction_paths_may_be_censored':True},indent=2));return
    if out.exists():raise RuntimeError(f'Immutable output exists: {out}')
    from reaction_lab_continuous_scanner import compile_canonical_scanner
    def loader(symbol,timeframe,limit):
        frame=sources[str(symbol).upper()][str(timeframe)]
        return frame.tail(int(limit)).copy()
    scan,fingerprint=compile_canonical_scanner(app,loader)
    all_opps=[];canonical_only={}
    for n in DETECTORS:
        name=f'{n}x{n}'
        canon=scan(symbols=tuple(symbols),swing_timeframes=('15m',),swing_detector_items=(('15m',name),),
            min_swing_prominence_pct=0.0,retest_tolerance_pct=0.10,min_departure_pct=0.20,
            max_age_minutes=360,max_retest_age_minutes=4320)
        by_symbol=defaultdict(list)
        if canon is not None and not canon.empty:
            for rec in canon.to_dict('records'):
                action=as_int(rec['actionable_timestamp'])
                if action is not None and start<=action<end:
                    by_symbol[str(rec['symbol'])].append(rec)
        for sym in symbols:
            local=find_variant_opportunities(sym,sources[sym]['1m'],sources[sym]['15m'],start,end,n)
            parity,missing=compare_canonical(local,by_symbol[sym],n)
            canonical_only[name+'_'+sym]=missing
            for row in local:
                row['canonical_parity']=parity[row['opportunity_key']]
            all_opps.extend(local)
            print('REACTION_DETECTOR_SYMBOL',json.dumps({'detector':name,'symbol':sym,
                'opportunities':len(local),'statuses':dict(Counter(r['status'] for r in local)),
                'parity':dict(Counter(r['canonical_parity'] for r in local)),
                'canonical_only_touches':len(missing)},sort_keys=True),flush=True)
    all_opps.sort(key=lambda r:(r['symbol'],r['detector'],r['actionable_ts'],r['side'],r['pivot_ts']))
    # Regress the frozen original 3x3 pilot, if present; never substitute a changed base.
    baseline_file=baseline/'opportunities_PRE.csv'
    baseline_hash=sha(baseline_file) if baseline_file.is_file() else None
    if baseline_file.is_file():
        with baseline_file.open(newline='',encoding='utf-8') as h:baseline_rows=list(csv.DictReader(h))
        if set(symbols)=={'HANAUSDT','TAKEUSDT','ETHUSDT'} and start==iso_ms('2026-10-12T00:00:00Z') and end==iso_ms('2026-10-13T00:00:00Z'):
            local={(r['symbol'],r['side'],r['pivot_ts'],r['actionable_ts']):r for r in all_opps if r['detector']=='3x3'}
            previous={(r['symbol'],r['side'],int(r['pivot_ts']),int(r['actionable_ts'])):r for r in baseline_rows}
            if local.keys()!=previous.keys():raise RuntimeError('Frozen 3x3 pivot census diverged from previous 53 opportunities')
            for k,v in local.items():
                p=previous[k]
                if v['status']!=p['status'] or as_int(v['first_touch_ts'])!=as_int(p['first_touch_ts']):
                    raise RuntimeError(f'Frozen 3x3 classification diverged: {k}')
    lookups={sym:{int(r['timestamp']):r for r in sources[sym]['1m'].to_dict('records')} for sym in symbols}
    outcomes=evaluate_outcomes(all_opps,lookups)
    matches=match_pivots(all_opps)
    report=summaries(all_opps,outcomes,matches,canonical_only)
    files=[('detector_opportunities.csv',all_opps,['symbol','detector','side','pivot_identity','opportunity_key',
            'pivot_ts','confirm_open_ts','actionable_ts','pivot_price','prominence_pct','departure_ts','departure_known_ts',
            'first_touch_ts','first_touch_status','status','retest_minutes','observed_minutes','canonical_parity']),
        ('detector_trade_outcomes.csv',outcomes,['symbol','detector','side','pivot_identity','opportunity_key',
            'actionable_ts','first_touch_ts','tp_pct','sl_pct','horizon_minutes','status','entry_ts','entry','exit_ts','bars_observed']),
        ('matched_pivots.csv',matches,['pivot_identity','symbol','side','pivot_ts','detectors_present',
            'action_2x2','status_2x2','touch_2x2','action_3x3','status_3x3','touch_3x3',
            'action_5x5','status_5x5','touch_5x5','confirmation_delay_5_minus_2_minutes','2x2_touch_before_5x5_action'])]
    out.mkdir(parents=True)
    for name,rows,cols in files:csv_write(out/name,rows,cols)
    bad={k:v for k,v in report['detectors'].items() if any(cat in v['canonical_parity'] for cat in
        ('MISSING_CANONICAL_TOUCH','PILOT_MISSED_TOUCH','TOUCH_TS_DIFFERENCE','TOUCH_CLASS_DIFFERENCE')) or v['canonical_only_touches']}
    manifest={'version':VERSION,'window':{'start':utc(start),'end_exclusive':utc(end),'symbols':symbols},
        'settings':{'tf':'15m','detectors':['2x2','3x3','5x5'],'swing_prominence_pct':0.0,
            'departure_pct':0.20,'retest_tolerance_pct':0.10,'opportunity_horizon_min':360,
            'outcome_entry':'next_1m_OPEN_after_retest','outcome_grid':[[tp,sl,h] for tp,sl in RATES for h in HORIZONS],
            'ambiguous_both_touch':'AMBIGUOUS_BOTH_IN_1M','no_fees_or_slippage':True},
        'source_hashes':{sym:{str(path):sha(Path(path)) for path in v['files']} for sym,v in sources.items()},
        'scanner_sha256':sha(scanner_file),'dashboard_sha256':sha(app),'scanner_AST_fingerprint':fingerprint,
        'baseline_3x3_sha256':baseline_hash,'baseline_3x3_path':str(baseline_file),
        'summary':report,'canonical_parity_no_hard_disagreements':not bool(bad),
        'limitations':['Same historical symbols and bar files; denominator for actionable opportunities differs by detector',
          'A no-touch event cannot be verified by event-only canonical API',
          'Outcomes use theoretical next 1m open, raw OHLC without fees, funding or slippage',
          'Simultaneous TP and SL in one 1m candle are ambiguous, NOT wins',
          'Late post-retest outcome paths can be censored because pilot cache ends 6h after day',
          'Multiple correlated pivots and overlapping trades are NOT an executable portfolio',
          'A within-day observational comparison is not out-of-sample predictive validation'],
        'file_hashes':{name:sha(out/name) for name,_,__ in files}}
    (out/'summary.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False))
    print('REACTION_DETECTOR_COMPARE_RESULT',json.dumps(report,ensure_ascii=False,sort_keys=True),flush=True)
    print('REACTION_DETECTOR_COMPARE_PARITY',json.dumps({'success':not bool(bad),'hard_disagreements':list(bad)},sort_keys=True),flush=True)
    print('REACTION_DETECTOR_COMPARE_PATH',out,flush=True)


def audit(root,out):
    manifest=json.loads((out/'summary.json').read_text())
    for name,expected in manifest['file_hashes'].items():
        if sha(out/name)!=expected:raise RuntimeError('Output hash mismatch: '+name)
    for sym,files in manifest['source_hashes'].items():
        for raw,expected in files.items():
            if sha(Path(raw))!=expected:raise RuntimeError(f'Source changed: {sym}: {raw}')
    if manifest.get('baseline_3x3_sha256') and sha(Path(manifest['baseline_3x3_path']))!=manifest['baseline_3x3_sha256']:
        raise RuntimeError('Frozen 3x3 pilot changed')
    if sha(root/'dashboard/app.py')!=manifest['dashboard_sha256']:raise RuntimeError('Dashboard changed')
    if sha(root/'reaction_lab_continuous_scanner.py')!=manifest['scanner_sha256']:raise RuntimeError('Scanner changed')
    with (out/'detector_opportunities.csv').open(newline='',encoding='utf-8') as f:opps=list(csv.DictReader(f))
    with (out/'detector_trade_outcomes.csv').open(newline='',encoding='utf-8') as f:outcomes=list(csv.DictReader(f))
    with (out/'matched_pivots.csv').open(newline='',encoding='utf-8') as f:matches=list(csv.DictReader(f))
    if len({r['opportunity_key'] for r in opps})!=len(opps):raise RuntimeError('Duplicate opportunity')
    if any(int(r['actionable_ts'])!=int(r['confirm_open_ts'])+FIFTEEN for r in opps):raise RuntimeError('Premature action')
    if any(r['first_touch_ts'] and int(r['first_touch_ts'])<=int(r['departure_ts']) for r in opps):raise RuntimeError('Premature touch')
    if any(r['status'] not in TOUCHES+('ARMED_NO_RETEST','NO_DEPARTURE','UNKNOWN_GAP') for r in opps):raise RuntimeError('Unknown status')
    for row in outcomes:
        if int(row['entry_ts'])!=int(row['first_touch_ts'])+ONE:raise RuntimeError('Early outcome entry')
    result=summaries(opps,[{**r,'tp_pct':float(r['tp_pct']),'sl_pct':float(r['sl_pct']),
        'horizon_minutes':int(r['horizon_minutes'])} for r in outcomes],matches,{})
    # Canonical-only stored in summary (not reproducible from the event-only CSV)
    for n in DETECTORS:
        name=f'{n}x{n}'
        if result['detectors'][name]['all_actionable']!=manifest['summary']['detectors'][name]['all_actionable']:
            raise RuntimeError(f'{name} count mismatch')
        if result['detectors'][name]['by_status']!=manifest['summary']['detectors'][name]['by_status']:
            raise RuntimeError(f'{name} status mismatch')
        if result['outcomes'][name]!=manifest['summary']['outcomes'][name]:
            raise RuntimeError(f'{name} outcomes mismatch')
    if not manifest['canonical_parity_no_hard_disagreements']:
        raise RuntimeError('Canonical replay mismatch: inspect detector_opportunities.csv and summary.json before scaling')
    print('REACTION_DETECTOR_COMPARE_AUDIT',json.dumps({'success':True,
        'canonical_parity_no_hard_disagreements':manifest['canonical_parity_no_hard_disagreements'],
        'detectors':{name:manifest['summary']['detectors'][name] for name in ('2x2','3x3','5x5')},
        'folder':str(out)},sort_keys=True),flush=True)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    modes=ap.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true');modes.add_argument('--run',action='store_true');modes.add_argument('--audit',action='store_true')
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--cache',type=Path,default=DEFAULT_CACHE)
    ap.add_argument('--output',type=Path,default=DEFAULT_OUT)
    ap.add_argument('--baseline',type=Path,default=DEFAULT_BASELINE)
    ap.add_argument('--start',default='2026-10-12T00:00:00Z');ap.add_argument('--end',default='2026-10-13T00:00:00Z')
    ap.add_argument('--symbols',default='HANAUSDT,TAKEUSDT,ETHUSDT')
    a=ap.parse_args(argv)
    root=a.project.resolve()
    cache=a.cache if a.cache.is_absolute() else root/a.cache
    out=a.output if a.output.is_absolute() else root/a.output
    baseline=a.baseline if a.baseline.is_absolute() else root/a.baseline
    symbols=list(dict.fromkeys(x.strip().upper() for x in a.symbols.split(',') if x.strip()))
    if not symbols or len(symbols)>30:ap.error('Need 1-30 symbols')
    start=iso_ms(a.start);end=iso_ms(a.end)
    if start>=end or end-start>31*24*60*ONE:ap.error('Invalid or >31d range')
    mode='plan' if a.plan else 'run' if a.run else 'audit'
    if mode=='audit':audit(root,out)
    else:run(root,cache,out,start,end,symbols,mode,baseline)

if __name__=='__main__':main()
