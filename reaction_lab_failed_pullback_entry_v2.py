#!/usr/bin/env python3
"""FAILED Pullback/Sweep Entry Audit V2 — offline, causal, read-only.

Experiment: after an original 1m TOUCH_FAILED, the market sweeps FURTHER
against the ORIGINAL swing side by a configurable percentage measured from the
failed retest CLOSE.  Only *after* that sweep, evaluate a rebound/reclaim from
closed 1m bars and enter in the ORIGINAL direction at NEXT 1m OPEN.

Controls: immediate original OPEN and passive sweep-limit order (separate from
confirmed recoveries). A passive limit's execution-bar OHLC has unknown event
order, hence uncertain TP is never a guaranteed winner.

Inputs are exactly the verified 1m paths used by Runner Discovery, sources are
read-only; outputs are derived CSV/JSON. No LIVE trading, Binance or Redis.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics

import reaction_lab_failed_reclaim_audit_v1 as v1
from reaction_lab_runner_discovery_all_touches_v1 import MINUTE, iter_events, recovery_index
from reaction_lab_failed_entry_timing_v1 import execute, post_entry_excursions

SCHEMA = 'FAILED_SWEEP_ENTRY_AUDIT_V2'
OUTPUT = Path('reports/reaction_lab/failed_pullback_entry_v2')
DEFAULT_DEPTHS = (0.5, 1.0, 1.5, 2.0, 2.5)
DEFAULT_SL_PCT = 1.0
SUGGESTED_SL_PCTS = (1.0, 3.0, 4.0, 5.0)
TARGETS = (2, 3, 5)
MODES = ('IMMEDIATE_ORIGINAL', 'SWEEP_LIMIT', 'SWEEP_REBOUND',
         'SWEEP_RECLAIM_LEVEL', 'SWEEP_RECLAIM_2_CLOSES')
EVENT_FIELDS = v1.EVENT_FIELDS
OUTCOME_FIELDS = (
    'touch_key','date_utc','symbol','side','observation_type','retest_ts',
    'path_source','path_count','metadata_state','mode','depth_pct','target_pct','sl_pct',
    'cohort_eligible','signal_state','reference_close','swing_price','sweep_price',
    'sweep_bar_index','sweep_bar_ts','sweep_depth_seen_pct',
    'signal_close_ts','entry_bar_index','entry_ts','entry_price','entry_resolution',
    'entry_improvement_vs_immediate_pct','outcome','exit_min_after_entry',
    'exit_ts','mfe_hold_pct','mae_hold_pct','gross_pct','net_pct',
    'net_worst_pct','net_best_pct','entry_bar_uncertain'
)
SUMMARY_FIELDS = (
    'group_type','group','mode','depth_pct','target_pct','sl_pct','cohort_total',
    'eligible_common','coverage_pct','signal_n','signal_pct','no_signal_n',
    'invalid_metadata_n','censored_n','tp_first_n','sl_first_n','ambiguous_n',
    'entry_bar_uncertain_n','time_exit_n','tp_rate_eligible_pct',
    'tp_rate_signalled_pct','sl_rate_signalled_pct','net_mean_per_signal_pct',
    'net_mean_per_eligible_pct','net_worst_mean_per_signal_pct',
    'net_best_mean_per_signal_pct','entry_improvement_mean_pct',
    'entry_improvement_median_pct'
)


def output_dir_for_sl(project, sl_pct, out=None):
    """Keep each SL report separately to avoid showing stale results.

    The default 1% baseline retains the original output location; other SL
    settings get their own derived subfolder. Explicit --out always wins.
    """
    if out is not None:
        return Path(out).resolve()
    root = Path(project).resolve() / OUTPUT
    if math.isclose(float(sl_pct), DEFAULT_SL_PCT, abs_tol=1e-9):
        return root
    # Safe deterministic name: SL 3% -> sl_3pct; SL 2.5% -> sl_2p5pct.
    slug = f'{float(sl_pct):.6f}'.rstrip('0').rstrip('.').replace('.', 'p')
    return root / f'sl_{slug}pct'


def parse_depths(raw):
    items = raw.split(',') if isinstance(raw,str) else list(raw)
    try:
        depths = tuple(sorted(set(round(float(x), 6) for x in items)))
    except (ValueError, TypeError) as exc:
        raise ValueError('Bad sweep depths; example: 0.5,1,1.5,2') from exc
    if not depths or len(depths)>30 or any(not math.isfinite(x) or x<0.1 or x>10 for x in depths):
        raise ValueError('Sweep depths must be 0.1–10%, maximum 30')
    return depths


def sweep_level(close,side,depth):
    if close is None or close<=0 or side not in ('LONG','SHORT'):
        raise ValueError('Invalid retest close or side')
    return close * (1 - depth/100 if side=='LONG' else 1 + depth/100)


def reached(bar,side,threshold):
    return bar[3]<=threshold if side=='LONG' else bar[2]>=threshold


def recovered_close(bar,side,threshold,buffer_pct):
    return (bar[4]>=threshold*(1+buffer_pct/100) if side=='LONG'
            else bar[4]<=threshold*(1-buffer_pct/100))


def directional_candle(bar,side):
    return bar[4]>bar[1] if side=='LONG' else bar[4]<bar[1]


def most_adverse_pct(bars,reference,side,upto):
    window=bars[:upto+1]
    lo=min(b[3] for b in window);hi=max(b[2] for b in window)
    return max(0,100*(1-lo/reference) if side=='LONG' else 100*(hi/reference-1))


def first_signal(bars,side,reference,swing,depth,mode,max_wait,buffer_pct=.05):
    """Return (entry bar index, sweep bar index, entry_price, resolution).

    Decisions on post-fail bar j can only enter at j+1 OPEN. LIMIT is a
    separately-labelled pending order, assumed live at first POST OPEN.
    """
    if mode not in MODES:raise ValueError('Unknown mode '+str(mode))
    if not bars:return None
    if mode=='IMMEDIATE_ORIGINAL':
        return 0,None,bars[0][1],'OPEN'
    threshold=sweep_level(reference,side,depth)
    sweep_ix=None
    for j in range(min(max_wait,len(bars))):
        bar=bars[j]
        if sweep_ix is None and reached(bar,side,threshold):
            sweep_ix=j
        if sweep_ix is None:continue
        if mode=='SWEEP_LIMIT':
            opening_better=(bar[1]<=threshold if side=='LONG' else bar[1]>=threshold)
            price=bar[1] if opening_better else threshold
            return j,sweep_ix,price,'OPEN_AT_BETTER' if opening_better else 'INTRABAR_LIMIT'
        if j+1>=len(bars):break
        if mode=='SWEEP_REBOUND':
            confirmed=(recovered_close(bar,side,threshold,buffer_pct)
                       and directional_candle(bar,side))
        elif mode=='SWEEP_RECLAIM_LEVEL':
            confirmed=(swing is not None and
                       recovered_close(bar,side,swing,buffer_pct))
        else:
            confirmed=(swing is not None and j>0 and j>sweep_ix and
                       recovered_close(bars[j-1],side,swing,buffer_pct) and
                       recovered_close(bar,side,swing,buffer_pct))
        if confirmed:
            return j+1,sweep_ix,bars[j+1][1],'NEXT_OPEN_CONFIRMED'
    return None


def pnl(current,entry,side):
    return 100*(current/entry-1 if side=='LONG' else 1-current/entry)


def entry_bar_outcome(bars,entry_ix,price,side,tp,sl,hold):
    """Explicitly bracket unknown price order for an intrabar resting limit.

    A TP in the fill candle might PRECEDE the fill, so cannot count as TP_FIRST.
    SL is necessarily reached after the limit for a non-gap-through resting order;
    upper bound still reflects 1m ignorance of a favorable post-fill move.
    """
    if entry_ix+hold>len(bars):raise ValueError('Incomplete post-entry horizon')
    b=bars[entry_ix]
    target=price*(1+tp/100 if side=='LONG' else 1-tp/100)
    stop=price*(1-sl/100 if side=='LONG' else 1+sl/100)
    if b[2]>=target if side=='LONG' else b[3]<=target:
        return 'FILL_BAR_TP_UNORDERED',1,-sl,True
    if b[3]<=stop if side=='LONG' else b[2]>=stop:
        return 'SL_FILL_BAR',1,-sl,False
    for j in range(entry_ix+1,entry_ix+hold):
        _,op,hi,lo,cl=bars[j]
        if op<=stop if side=='LONG' else op>=stop:
            return 'SL_GAP',j-entry_ix+1,min(-sl,pnl(op,price,side)),False
        if op>=target if side=='LONG' else op<=target:
            return 'TP_GAP',j-entry_ix+1,tp,False
        tp_hit=hi>=target if side=='LONG' else lo<=target
        sl_hit=lo<=stop if side=='LONG' else hi>=stop
        if tp_hit and sl_hit:return 'SL_AMBIGUOUS',j-entry_ix+1,-sl,True
        if sl_hit:return 'SL_FIRST',j-entry_ix+1,-sl,False
        if tp_hit:return 'TP_FIRST',j-entry_ix+1,tp,False
    return 'TIME_EXIT',hold,pnl(bars[entry_ix+hold-1][4],price,side),False


def excursions(bars,ix,price,side,hold):
    window=bars[ix:ix+hold]
    if side=='LONG':
        return max(0,100*(max(b[2] for b in window)/price-1)),max(0,100*(1-min(b[3] for b in window)/price))
    return max(0,100*(1-min(b[3] for b in window)/price)),max(0,100*(max(b[2] for b in window)/price-1))


def evaluate(base,bars,mode,depth,target,*,sl=1.,max_wait=60,hold=180,cost=.2,buffer=.05):
    res={k:None for k in OUTCOME_FIELDS}
    for k in ('touch_key','date_utc','symbol','side','observation_type','retest_ts',
              'path_source','path_count','metadata_state'):
        res[k]=base.get(k)
    res.update(mode=mode,depth_pct=depth,target_pct=target,sl_pct=sl,
        cohort_eligible=bool(base['eligible_common_180m']),reference_close=base['retest_close'],
        swing_price=base.get('swing_price'),entry_bar_uncertain=False)
    if not base['eligible_common_180m']:
        res.update(signal_state='CENSORED_COMMON_HORIZON',outcome='CENSORED');return res
    if base['metadata_state']!='VALID_FAILED':
        res.update(signal_state='INVALID_METADATA',outcome='INVALID_METADATA');return res
    reference=v1.as_positive(base.get('retest_close'))
    swing=v1.as_positive(base.get('swing_price'))
    if reference is None or swing is None:
        res.update(signal_state='INVALID_METADATA',outcome='INVALID_METADATA');return res
    threshold=sweep_level(reference,base['side'],depth)
    res['sweep_price']=threshold
    sig=first_signal(bars,base['side'],reference,swing,depth,mode,max_wait,buffer)
    if sig is None:
        res.update(signal_state='NO_SIGNAL',outcome='NO_TRADE');return res
    ix,sweep_ix,price,resolution=sig
    if not (0<=ix<=max_wait and ix+hold<=len(bars)):
        raise ValueError('Entry outside common horizon')
    if int(bars[ix][0]) != int(base['known_ts']) + ix*MINUTE:
        raise ValueError('Causal timestamp mismatch: '+base['touch_key'])
    if mode not in ('IMMEDIATE_ORIGINAL','SWEEP_LIMIT') and (ix<1 or sweep_ix is None or ix<=sweep_ix):
        raise ValueError('Noncausal sweep confirmation')
    if mode=='SWEEP_LIMIT' and resolution=='INTRABAR_LIMIT':
        outcome,minute,gross,uncertain=entry_bar_outcome(bars,ix,price,base['side'],target,sl,hold)
    else:
        if not math.isclose(price,bars[ix][1],rel_tol=1e-12):
            raise ValueError('Expected OPEN entry')
        outcome,minute,gross=execute(bars,ix,base['side'],target,sl,hold)
        uncertain=outcome=='SL_AMBIGUOUS'
    upper=target if outcome in ('FILL_BAR_TP_UNORDERED','SL_AMBIGUOUS') else gross
    mfe,mae=excursions(bars,ix,price,base['side'],hold)
    res.update(signal_state='SIGNAL',sweep_bar_index=sweep_ix,
        sweep_bar_ts=(bars[sweep_ix][0] if sweep_ix is not None else None),
        sweep_depth_seen_pct=(round(most_adverse_pct(bars,reference,base['side'],sweep_ix),7) if sweep_ix is not None else None),
        signal_close_ts=(bars[ix-1][0]+MINUTE if resolution=='NEXT_OPEN_CONFIRMED' else base['known_ts']),
        entry_bar_index=ix,entry_ts=bars[ix][0],entry_price=round(price,12),
        entry_resolution=resolution,
        entry_improvement_vs_immediate_pct=round(pnl(bars[0][1],price,base['side'])*-1,7),
        outcome=outcome,exit_min_after_entry=minute,
        exit_ts=bars[ix+minute-1][0],
        mfe_hold_pct=round(mfe,7),mae_hold_pct=round(mae,7),
        gross_pct=round(gross,7),net_pct=round(gross-cost,7),
        net_worst_pct=round(gross-cost,7),net_best_pct=round(upper-cost,7),
        entry_bar_uncertain=uncertain)
    # Improvement: for LONG, buying below first post-open is positive;
    # for SHORT, selling above it is positive. Avoid ex-post comparisons to peaks.
    res['entry_improvement_vs_immediate_pct']=round(
        100*(bars[0][1]-price)/bars[0][1] if base['side']=='LONG' else
        100*(price-bars[0][1])/bars[0][1],7)
    return res


def summarize(rows,events,group_type,group,mode,depth,tp):
    def group_of(e):
        return ('ALL' if group_type=='ALL' else e['side'] if group_type=='SIDE' else
                e['date_utc'] if group_type=='DATE' else e['observation_type'])
    related={e['touch_key'] for e in events if group_type=='ALL' or group_of(e)==group}
    selected=[r for r in rows if r['touch_key'] in related and r['mode']==mode and
              r['depth_pct']==depth and r['target_pct']==tp]
    eligible=[r for r in selected if r['cohort_eligible']]
    traded=[r for r in eligible if r['signal_state']=='SIGNAL']
    n=len(eligible);m=len(traded);count=Counter(r['outcome'] for r in traded)
    tps=count['TP_FIRST']+count['TP_GAP']
    sls=count['SL_FIRST']+count['SL_GAP']+count['SL_FILL_BAR']
    ambiguous=count['SL_AMBIGUOUS']+count['FILL_BAR_TP_UNORDERED']
    vals=[r['entry_improvement_vs_immediate_pct'] for r in traded]
    def avg(field,denom):return round(sum(r[field] for r in traded)/denom,6) if denom else None
    return dict(group_type=group_type,group=group,mode=mode,depth_pct=depth,
       target_pct=tp,sl_pct=(selected[0]['sl_pct'] if selected else None),
       cohort_total=len(related),eligible_common=n,
       coverage_pct=round(100*n/len(related),3) if related else None,
       signal_n=m,signal_pct=round(100*m/n,3) if n else None,
       no_signal_n=sum(r['signal_state']=='NO_SIGNAL' for r in eligible),
       invalid_metadata_n=sum(r['signal_state']=='INVALID_METADATA' for r in eligible),
       censored_n=len(related)-n,tp_first_n=tps,sl_first_n=sls,
       ambiguous_n=ambiguous,entry_bar_uncertain_n=count['FILL_BAR_TP_UNORDERED'],
       time_exit_n=count['TIME_EXIT'],
       tp_rate_eligible_pct=round(100*tps/n,3) if n else None,
       tp_rate_signalled_pct=round(100*tps/m,3) if m else None,
       sl_rate_signalled_pct=round(100*sls/m,3) if m else None,
       net_mean_per_signal_pct=avg('net_pct',m),
       net_mean_per_eligible_pct=avg('net_pct',n),
       net_worst_mean_per_signal_pct=avg('net_worst_pct',m),
       net_best_mean_per_signal_pct=avg('net_best_pct',m),
       entry_improvement_mean_pct=round(statistics.fmean(vals),6) if vals else None,
       entry_improvement_median_pct=round(statistics.median(vals),6) if vals else None)


def build(project,*,journal_db=None,first_touch_db=None,auto_db=None,
          recovery_root=None,sample_metadata=None,max_wait=60,hold=180,
          sl_pct=DEFAULT_SL_PCT,depths=DEFAULT_DEPTHS,cost=.2,out=None):
    depths=parse_depths(depths)
    if not (1<=max_wait<=120 and 15<=hold<=360 and max_wait+hold<=360 and
            math.isfinite(sl_pct) and 0.1<=sl_pct<=10 and math.isfinite(cost) and 0<=cost<=5):
        raise ValueError('Invalid wait/hold/SL/cost')
    project=Path(project).resolve()
    root=(Path(recovery_root).resolve() if recovery_root else
          project/'reports/reaction_lab/first_touch_historical_recovery_v11')
    if sample_metadata:
        journal=v1.read_sample_journal(sample_metadata)
        iterator=v1.sample_paths(journal,root)
        scope='SAMPLE_V11_ONLY_NO_ONLINE_PREFIX'
    else:
        journal=v1.read_journal(journal_db or project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3')
        recovered,_=recovery_index(root)
        iterator=iter_events(project,first_touch_db=first_touch_db,auto_db=auto_db,
                             recovery=recovered,include_bars=True,only_status='TOUCH_FAILED')
        scope='FULL_VPS_VERIFIED_ONLINE_AND_HISTORY'
    events=[];outcomes=[];seen=set()
    def consume(row,bars):
        key=row['touch_key']
        if key in seen:raise ValueError('Duplicate touch key '+key)
        seen.add(key)
        if row['first_touch_status']!='TOUCH_FAILED':raise ValueError('Wrong first touch status')
        base=v1.event_base(row,bars,journal.get(key),max_wait,hold)
        events.append(base)
        for depth in depths:
            for mode in MODES:
                for target in TARGETS:
                    outcomes.append(evaluate(base,bars,mode,depth,target,
                        sl=sl_pct,max_wait=max_wait,hold=hold,cost=cost))
    for row,bars in iterator:consume(row,bars)
    for key in sorted(set(journal)-seen):
        raw=journal[key];meta=raw.get('meta') if isinstance(raw.get('meta'),dict) else {}
        ts=int(raw['retest_ts'])
        row=dict(touch_key=key,symbol=raw['symbol'],side=raw['side'],
                 first_touch_status='TOUCH_FAILED',retest_ts=ts,entry_ts=ts+MINUTE,
                 date_utc=v1.date_utc(ts),retest_close=meta.get('retest_close'),
                 path_source='JOURNAL_ONLY_AWAITING_PATH_COLLECTOR',
                 path_quality='NO_CONTIGUOUS_PATH',path_count=0)
        consume(row,[])
    # Avoid repeatedly scanning the complete (modes × sweeps × targets) grid
    # for every side/date/observation summary. Identical definitions, faster.
    by_scenario=defaultdict(list)
    for row in outcomes:
        by_scenario[(row['mode'],row['depth_pct'],row['target_pct'])].append(row)
    summaries=[]
    for group_type,groups in [('ALL',['ALL']),('SIDE',['LONG','SHORT']),
                              ('DATE',sorted({e['date_utc'] for e in events})),
                              ('OBSERVATION',sorted({e['observation_type'] for e in events}))]:
        for group in groups:
            for depth in depths:
                for mode in MODES:
                    for target in TARGETS:
                        summaries.append(summarize(by_scenario[(mode,depth,target)],events,
                                                   group_type,group,mode,depth,target))
    folder=output_dir_for_sl(project,sl_pct,out)
    folder.mkdir(parents=True,exist_ok=True)
    manifest=dict(schema=SCHEMA,created_utc=datetime.now(timezone.utc).isoformat(),
        scope=scope,source_journal=str(sample_metadata or journal_db or
                         project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'),
        historical_root=str(root),cohort_total=len(events),journal_total=len(journal),
        coverage_common=sum(e['eligible_common_180m'] for e in events),
        censored_common=sum(not e['eligible_common_180m'] for e in events),
        metadata_states=dict(Counter(e['metadata_state'] for e in events)),
        sources=dict(Counter(e['path_source'] for e in events)),
        observation_types=dict(Counter(e['observation_type'] for e in events)),
        modes=list(MODES),targets_pct=list(TARGETS),
        settings=dict(max_wait_min=max_wait,hold_min=hold,sl_pct=sl_pct,
            cost_roundtrip_pct_points=cost,minimum_common_bars=max_wait+hold,
            depths_pct=list(depths),rebound_buffer_pct=.05),
        notes=[
          'SWEEP is adverse movement AFTER the failed retest close, measured from its CLOSE, not from swing.',
          'The original direction is the original swing LONG/SHORT; no automatic reversal of side.',
          'REBOUND: adverse sweep seen, then a directional CLOSED candle recovers sweep threshold +0.05%; NEXT OPEN entry.',
          'RECLAIM_LEVEL: after sweep, a CLOSED 1m candle recovers original swing +/-0.05%; NEXT OPEN entry.',
          'RECLAIM_2_CLOSES: two consecutive CLOSED 1m candles on reclaimed side AFTER sweep; NEXT OPEN entry.',
          'LIMIT is a diagnostic passive order, not a confirmed reversal; uncertain fill-candle TP cannot be credited.',
          'All variants share max_wait+hold continuous 1m coverage denominator. No signals kept, no returns.',
          'TP+SL in same bar = SL_AMBIGUOUS; conservative net uses -SL, optimistic upper bound uses TP.',
          'V1 verified history reader checks SHA256/identity/OHLC prefix. No source writes, no LIVE changes.',
          'SL is configurable and every SL value is saved separately so prior results are not silently overwritten.',
          'No fee/funding calibration, slippage, latency, position overlap or forward validation.'
        ])
    v1.write_csv_atomic(folder/'events.csv',events,EVENT_FIELDS)
    v1.write_csv_atomic(folder/'event_outcomes.csv',outcomes,OUTCOME_FIELDS)
    v1.write_csv_atomic(folder/'summary.csv',summaries,SUMMARY_FIELDS)
    v1.write_json_atomic(folder/'manifest.json',manifest)
    return {**manifest, 'out_dir': str(folder)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.')
    p.add_argument('--journal-db');p.add_argument('--first-touch-db');p.add_argument('--auto-db')
    p.add_argument('--recovery-root');p.add_argument('--sample-metadata')
    p.add_argument('--max-wait',type=int,default=60)
    p.add_argument('--hold',type=int,default=180)
    p.add_argument('--sl-pct',type=float,default=DEFAULT_SL_PCT)
    p.add_argument('--sweep-depths',default=','.join(str(x) for x in DEFAULT_DEPTHS))
    p.add_argument('--cost-roundtrip-pct',type=float,default=.2)
    p.add_argument('--out')
    opts=p.add_mutually_exclusive_group(required=True)
    opts.add_argument('--plan',action='store_true');opts.add_argument('--run',action='store_true')
    args=p.parse_args(argv)
    try:
        depths=parse_depths(args.sweep_depths)
        if not (1<=args.max_wait<=120 and 15<=args.hold<=360 and
                args.max_wait+args.hold<=360 and .1<=args.sl_pct<=10 and
                0<=args.cost_roundtrip_pct<=5):raise ValueError('Bad wait/hold/SL/cost')
    except ValueError as e:p.error(str(e))
    if args.plan:
        journal=v1.read_sample_journal(args.sample_metadata) if args.sample_metadata else v1.read_journal(
            args.journal_db or Path(args.project)/'reports/reaction_lab/scanner_first_touches_v1.sqlite3')
        print('FAILED_SWEEP_PLAN '+json.dumps(dict(schema=SCHEMA,mode='PLAN',
            journal_failed=len(journal),modes=list(MODES),depths_pct=list(depths),
            targets_pct=list(TARGETS),sl_pct=args.sl_pct,common_min_bars=args.max_wait+args.hold,
            read_only_sources=True,sample=bool(args.sample_metadata)),ensure_ascii=False))
        return 0
    result=build(args.project,journal_db=args.journal_db,first_touch_db=args.first_touch_db,
        auto_db=args.auto_db,recovery_root=args.recovery_root,sample_metadata=args.sample_metadata,
        max_wait=args.max_wait,hold=args.hold,sl_pct=args.sl_pct,depths=depths,
        cost=args.cost_roundtrip_pct,out=args.out)
    print('FAILED_SWEEP_RESULT '+json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
