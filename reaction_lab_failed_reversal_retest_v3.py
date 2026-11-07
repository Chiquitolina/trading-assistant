#!/usr/bin/env python3
"""FAILED Sweep -> Reversal -> Retest V3 (offline research, source read-only).

After an original TOUCH_FAILED known at the retest 1m close:
  1. Wait for an adverse sweep measured from failed retest CLOSE.
  2. On a LATER closed 1m candle, demand directional close beyond the prior
     candle HIGH (LONG) / LOW (SHORT) and a minimum rebound from the
     adverse extreme observed THROUGH that confirmation candle.
  3. From the NEXT 1m candle, test (a) immediate OPEN on confirmation or
     (b) a resting LIMIT at 25/50/75% retracement of the observed rebound.
The limit is computed ONLY from candles closed at confirmation; never from a
future pivot. If filled intrabar, favorable same-candle moves are unorderable
and not credited as certain TP. No-fill remains in the cohort denominator.

Comparators: immediate FAILED OPEN, V2 sweep rebound and V2 two-close
reclaim. Configurable percentage or structural stop behind the sweep extreme.
The FULL mode uses the same SHA/identity/prefix-verified 1m reader as V1/V2.
No exchanges, orders, LIVE, active SQLite writes, or background jobs.
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
import reaction_lab_failed_pullback_entry_v2 as v2
from reaction_lab_runner_discovery_all_touches_v1 import MINUTE, iter_events, recovery_index

SCHEMA = 'FAILED_SWEEP_REVERSAL_RETEST_V3'
OUTPUT = Path('reports/reaction_lab/failed_reversal_retest_v3')
DEFAULT_DEPTHS = (1., 2.)
DEFAULT_RETRACEMENTS = (25, 50, 75)
DEFAULT_SL_PCT = 1.
DEFAULT_STRUCT_BUFFER_PCT = .1
DEFAULT_MIN_LEG_PCT = .25
DEFAULT_BREAK_BUFFER_PCT = 0.
TARGETS = (2, 3, 5)
MODES = ('IMMEDIATE_ORIGINAL', 'V2_SWEEP_REBOUND', 'V2_RECLAIM_2_CLOSES',
         'REVERSAL_OPEN', 'RETEST_25', 'RETEST_50', 'RETEST_75')
EVENT_FIELDS = v1.EVENT_FIELDS
OUTCOME_FIELDS = (
    'touch_key','date_utc','symbol','side','observation_type','retest_ts',
    'path_source','path_count','metadata_state','mode','depth_pct','target_pct',
    'stop_mode','sl_pct','struct_buffer_pct','cohort_eligible','signal_state',
    'no_signal_reason','reference_close','swing_price','sweep_price',
    'sweep_bar_index','sweep_extreme','sweep_seen_pct','reversal_bar_index',
    'reversal_close_ts','reversal_close','reversal_leg_pct','retest_fraction_pct',
    'limit_price','entry_bar_index','entry_ts','entry_price','entry_resolution',
    'entry_improvement_vs_immediate_pct','stop_price','stop_distance_pct',
    'outcome','exit_min_after_entry','exit_ts','gross_pct','net_pct',
    'net_worst_pct','net_best_pct','entry_bar_uncertain','mfe_hold_pct','mae_hold_pct',
)
SUMMARY_FIELDS = (
    'group_type','group','mode','depth_pct','target_pct','stop_mode','sl_pct',
    'cohort_total','eligible_common','signal_n','signal_pct','no_signal_n',
    'no_sweep_n','no_reversal_n','no_fill_n','invalid_metadata_n','censored_n',
    'tp_first_n','sl_first_n','ambiguous_n','entry_bar_uncertain_n',
    'time_exit_n','struct_stop_gap_n','tp_rate_signalled_pct','net_mean_per_signal_pct',
    'net_mean_per_eligible_pct','net_worst_mean_per_signal_pct',
    'net_best_mean_per_signal_pct','entry_improvement_median_pct',
    'stop_distance_median_pct',
)


def parse_retracements(value):
    try:
        xs = tuple(sorted({int(x) for x in (value.split(',') if isinstance(value,str) else value)}))
    except (TypeError, ValueError) as exc:
        raise ValueError('Retracements must be integers, e.g. 25,50,75') from exc
    if xs != DEFAULT_RETRACEMENTS:
        raise ValueError('V3 frozen pilot supports exactly 25,50,75. No exploratory parameter optimization.')
    return xs


def config_dir(project,stop_mode='pct',sl_pct=1.,struct_buffer_pct=.1,out=None):
    if out is not None:return Path(out).resolve()
    root=Path(project).resolve()/OUTPUT
    if stop_mode=='structural':
        slug=f'{struct_buffer_pct:g}'.replace('.','p')
        return root/f'structural_buffer_{slug}pct'
    slug=f'{sl_pct:g}'.replace('.','p')
    return root/f'sl_{slug}pct'


def pnl(current,entry,side):
    return v2.pnl(current,entry,side)


def adverse_extreme(bars,side,last_ix):
    subset=bars[:last_ix+1]
    return min(b[3] for b in subset) if side=='LONG' else max(b[2] for b in subset)


def find_sweep_reversal(bars,side,reference,depth,max_wait=60,min_leg=.25,break_buffer=0.):
    """Only bars[0..j] can influence a confirmation at close of bar j.

    A sweep and the confirming reversal must be in different closed candles.
    Returns a descriptive trace even if the reversal never appears.
    """
    threshold=v2.sweep_level(reference,side,depth)
    result={'sweep_bar_index':None,'sweep_price':threshold,
            'sweep_extreme':None,'reversal_bar_index':None,
            'reversal_close':None,'reversal_leg_pct':None}
    if not bars:return result
    end=min(int(max_wait),len(bars))
    for j in range(end):
        bar=bars[j]
        if result['sweep_bar_index'] is None:
            if not v2.reached(bar,side,threshold):continue
            result['sweep_bar_index']=j
        if j<=result['sweep_bar_index']:continue
        prev=bars[j-1]
        crossed=(bar[4]>prev[2]*(1+break_buffer/100) if side=='LONG'
                 else bar[4]<prev[3]*(1-break_buffer/100))
        if not crossed or not v2.directional_candle(bar,side):continue
        extreme=adverse_extreme(bars,side,j)
        leg=pnl(bar[4],extreme,side)
        if leg+1e-9 < min_leg:continue
        result.update(sweep_extreme=extreme,reversal_bar_index=j,
                      reversal_close=bar[4],reversal_leg_pct=leg)
        return result
    if result['sweep_bar_index'] is not None:
        result['sweep_extreme']=adverse_extreme(bars,side,end-1)
    return result


def retest_limit(extreme,confirmation_close,side,fraction_pct):
    if side=='LONG':
        return confirmation_close-(confirmation_close-extreme)*fraction_pct/100
    return confirmation_close+(extreme-confirmation_close)*fraction_pct/100


def resting_limit_fill(bars,side,price,start,max_wait):
    """Resting limit activated immediately AFTER reversal close.

    When next bar opens beyond the limit, fill at OPEN; otherwise at LIMIT if
    OHLC crosses it. Intrabar fill keeps an explicit worst/best outcome bound.
    """
    for ix in range(start,min(max_wait,len(bars)-1)+1):
        _,op,hi,lo,_=bars[ix]
        opens_better=op<=price if side=='LONG' else op>=price
        if opens_better:return ix,op,'LIMIT_OPEN_BETTER'
        if lo<=price if side=='LONG' else hi>=price:
            return ix,price,'LIMIT_INTRABAR'
    return None


def find_entry(bars,side,reference,swing,depth,mode,max_wait=60,
               min_leg=.25,break_buffer=0.):
    if mode not in MODES:raise ValueError('Unsupported V3 mode '+str(mode))
    # Do not even inspect POST reversals for the immediate or V2 comparators.
    # Their outcomes and chart markers must not inherit future V3 diagnostics.
    trace={'sweep_price':v2.sweep_level(reference,side,depth),
           'sweep_bar_index':None,'sweep_extreme':None,
           'reversal_bar_index':None,'reversal_close':None,
           'reversal_leg_pct':None,'no_signal_reason':''}
    if mode=='IMMEDIATE_ORIGINAL':
        return trace,(0,bars[0][1],'OPEN_IMMEDIATE') if bars else None
    if mode in ('V2_SWEEP_REBOUND','V2_RECLAIM_2_CLOSES'):
        old=('SWEEP_REBOUND' if mode=='V2_SWEEP_REBOUND' else 'SWEEP_RECLAIM_2_CLOSES')
        s=v2.first_signal(bars,side,reference,swing,depth,old,max_wait)
        if s is not None:
            ix,sweep_ix,price,_=s
            trace['sweep_bar_index']=sweep_ix
            trace['sweep_extreme']=adverse_extreme(bars,side,ix-1)
            return trace,(ix,price,'NEXT_OPEN_V2')
        trace['no_signal_reason']='NO_V2_CONFIRMATION'
        return trace,None
    trace=find_sweep_reversal(bars,side,reference,depth,max_wait,min_leg,break_buffer)
    trace['no_signal_reason']=''
    if trace['sweep_bar_index'] is None:
        trace['no_signal_reason']='NO_SWEEP'
        return trace,None
    if trace['reversal_bar_index'] is None:
        trace['no_signal_reason']='NO_REVERSAL'
        return trace,None
    j=trace['reversal_bar_index']
    if j+1>=len(bars):
        trace['no_signal_reason']='NO_NEXT_OPEN'
        return trace,None
    if mode=='REVERSAL_OPEN':
        return trace,(j+1,bars[j+1][1],'NEXT_OPEN_REVERSAL')
    fraction=int(mode.split('_')[1])
    if fraction not in DEFAULT_RETRACEMENTS:raise ValueError('Unknown fraction')
    price=retest_limit(trace['sweep_extreme'],trace['reversal_close'],side,fraction)
    trace['limit_price']=price
    fill=resting_limit_fill(bars,side,price,j+1,max_wait)
    if fill is None:trace['no_signal_reason']='NO_FILL'
    return trace,fill


def stop_price_for(entry,side,stop_mode,sl,extreme,buffer):
    if stop_mode=='pct':
        return entry*(1-sl/100 if side=='LONG' else 1+sl/100)
    if extreme is None or extreme<=0:return None
    return extreme*(1-buffer/100 if side=='LONG' else 1+buffer/100)


def price_stop_execute(bars,ix,price,side,target_pct,stop_price,hold,
                       *,intrabar_entry=False):
    """Return (status,minute,gross_worst,gross_best,entry_uncertain).

    Structural stops retain their absolute causal price throughout the trade.
    For intrabar LIMIT fills, TP in fill candle could predate the fill; never
    count it as a certain winner. Same-candle TP/SL => conservative SL bound.
    """
    if ix+hold>len(bars):raise ValueError('Incomplete holding horizon')
    tp=price*(1+target_pct/100 if side=='LONG' else 1-target_pct/100)
    risk=-(pnl(stop_price,price,side))
    if risk<=0:raise ValueError('Stop not on loss side of filled entry')
    fill_bar_tp_unordered=False
    if intrabar_entry:
        b=bars[ix]
        hit_tp=b[2]>=tp if side=='LONG' else b[3]<=tp
        hit_sl=b[3]<=stop_price if side=='LONG' else b[2]>=stop_price
        # Both are present in the fill candle: worst SL, best TP.
        # A TP without SL might have happened BEFORE our limit order filled;
        # the defensible lower bound is the actual outcome from the NEXT bar,
        # not an invented -SL loss in a candle that never hit the stop.
        if hit_tp and hit_sl:
            return 'FILL_BAR_TP_UNORDERED',1,-risk,target_pct,True
        if hit_sl:
            return 'SL_FILL_BAR',1,-risk,-risk,False
        fill_bar_tp_unordered=hit_tp
        start=ix+1
    else:
        start=ix
    for j in range(start,ix+hold):
        _,op,hi,lo,cl=bars[j]
        if j>ix:
            if op<=stop_price if side=='LONG' else op>=stop_price:
                gross=min(-risk,pnl(op,price,side))
                return ('FILL_BAR_TP_UNORDERED' if fill_bar_tp_unordered else 'SL_GAP',
                        j-ix+1,gross,target_pct if fill_bar_tp_unordered else gross,
                        fill_bar_tp_unordered)
            if op>=tp if side=='LONG' else op<=tp:
                return 'TP_GAP',j-ix+1,target_pct,target_pct,False
        hit_tp=hi>=tp if side=='LONG' else lo<=tp
        hit_sl=lo<=stop_price if side=='LONG' else hi>=stop_price
        if hit_tp and hit_sl:
            return 'SL_AMBIGUOUS',j-ix+1,-risk,target_pct,True
        if hit_sl:
            return ('FILL_BAR_TP_UNORDERED' if fill_bar_tp_unordered else 'SL_FIRST',
                    j-ix+1,-risk,target_pct if fill_bar_tp_unordered else -risk,
                    fill_bar_tp_unordered)
        if hit_tp:return 'TP_FIRST',j-ix+1,target_pct,target_pct,False
    gross=pnl(bars[ix+hold-1][4],price,side)
    return ('FILL_BAR_TP_UNORDERED' if fill_bar_tp_unordered else 'TIME_EXIT',
            hold,gross,target_pct if fill_bar_tp_unordered else gross,
            fill_bar_tp_unordered)


def evaluate(base,bars,mode,depth,target,*,stop_mode='pct',sl=1.,buffer=.1,
             max_wait=60,hold=180,cost=.2,min_leg=.25,break_buffer=0.):
    out={k:None for k in OUTCOME_FIELDS}
    for k in ('touch_key','date_utc','symbol','side','observation_type','retest_ts',
              'path_source','path_count','metadata_state'):
        out[k]=base.get(k)
    out.update(mode=mode,depth_pct=depth,target_pct=target,stop_mode=stop_mode,
        sl_pct=sl if stop_mode=='pct' else None,struct_buffer_pct=buffer if stop_mode=='structural' else None,
        cohort_eligible=bool(base['eligible_common_180m']),
        reference_close=base.get('retest_close'),swing_price=base.get('swing_price'),
        entry_bar_uncertain=False)
    if not out['cohort_eligible']:
        out.update(signal_state='CENSORED_COMMON_HORIZON',outcome='CENSORED');return out
    if base['metadata_state']!='VALID_FAILED':
        out.update(signal_state='INVALID_METADATA',outcome='INVALID_METADATA');return out
    reference=v1.as_positive(base.get('retest_close'))
    swing=v1.as_positive(base.get('swing_price'))
    if reference is None or swing is None:
        out.update(signal_state='INVALID_METADATA',outcome='INVALID_METADATA');return out
    side=base['side']
    trace,entry=find_entry(bars,side,reference,swing,depth,mode,max_wait,min_leg,break_buffer)
    out.update(sweep_price=trace.get('sweep_price'),
        sweep_bar_index=trace.get('sweep_bar_index'),sweep_extreme=trace.get('sweep_extreme'),
        reversal_bar_index=trace.get('reversal_bar_index'),
        reversal_close=trace.get('reversal_close'),reversal_leg_pct=trace.get('reversal_leg_pct'),
        reversal_close_ts=(bars[trace['reversal_bar_index']][0]+MINUTE if trace.get('reversal_bar_index') is not None else None),
        retest_fraction_pct=(int(mode.split('_')[1]) if mode.startswith('RETEST_') else None),
        limit_price=trace.get('limit_price'),
        sweep_seen_pct=(v2.most_adverse_pct(bars,reference,side,trace['sweep_bar_index']) if trace.get('sweep_bar_index') is not None else None))
    if stop_mode=='structural' and mode=='IMMEDIATE_ORIGINAL':
        out.update(signal_state='NOT_APPLICABLE',no_signal_reason='STRUCTURAL_STOP_REQUIRES_OBSERVED_SWEEP',outcome='NO_TRADE');return out
    if entry is None:
        out.update(signal_state='NO_SIGNAL',no_signal_reason=trace['no_signal_reason'],outcome='NO_TRADE');return out
    ix,price,resolution=entry
    if ix<0 or ix>max_wait or ix+hold>len(bars):raise ValueError('Entry exceeds common horizon')
    if int(bars[ix][0])!=int(base['known_ts'])+ix*MINUTE:
        raise ValueError('Causal time mismatch '+str(base['touch_key']))
    if mode!='IMMEDIATE_ORIGINAL' and ix<1:
        raise ValueError('Lookahead: conditional entry in first POST candle')
    extreme=trace.get('sweep_extreme')
    stop=stop_price_for(price,side,stop_mode,sl,extreme,buffer)
    if stop is None:
        out.update(signal_state='NO_SIGNAL',no_signal_reason='NO_STRUCTURAL_ANCHOR',outcome='NO_TRADE');return out
    risk=-pnl(stop,price,side)
    if risk<=0:
        # A gap beyond the precommitted structural stop is NOT discarded as
        # a 'no signal' or a winner. Record it explicitly; conservative net
        # counts the fixed intended stop distance at the limit as a loss.
        if resolution=='LIMIT_OPEN_BETTER':
            committed=trace.get('limit_price') or price
            intended=max(.01,-pnl(stop,committed,side))
            loss=-max(intended,.1)
            out.update(signal_state='SIGNAL',entry_bar_index=ix,entry_ts=bars[ix][0],
                entry_price=price,entry_resolution=resolution,stop_price=stop,
                stop_distance_pct=intended,outcome='STRUCT_STOP_GAP_AT_FILL',
                exit_min_after_entry=1,exit_ts=bars[ix][0],gross_pct=loss,
                net_pct=loss-cost,net_worst_pct=loss-cost,net_best_pct=loss-cost,
                entry_bar_uncertain=True)
            return out
        raise ValueError('Structural stop must be adverse to entry')
    status,minute,worst,best,uncertain=price_stop_execute(
        bars,ix,price,side,target,stop,hold,intrabar_entry=(resolution=='LIMIT_INTRABAR'))
    mfe,mae=v2.excursions(bars,ix,price,side,hold)
    out.update(signal_state='SIGNAL',entry_bar_index=ix,entry_ts=bars[ix][0],
        entry_price=round(price,12),entry_resolution=resolution,
        entry_improvement_vs_immediate_pct=round(
          100*(bars[0][1]-price)/bars[0][1] if side=='LONG' else
          100*(price-bars[0][1])/bars[0][1],7),
        stop_price=round(stop,12),stop_distance_pct=round(risk,7),
        outcome=status,exit_min_after_entry=minute,exit_ts=bars[ix+minute-1][0],
        gross_pct=round(worst,7),net_pct=round(worst-cost,7),
        net_worst_pct=round(worst-cost,7),net_best_pct=round(best-cost,7),
        entry_bar_uncertain=uncertain,mfe_hold_pct=round(mfe,7),mae_hold_pct=round(mae,7))
    return out


def summarize(rows,events,group_type,group,mode,depth,tp,stop_mode,sl):
    def belongs(e):
        return group_type=='ALL' or (e['side'] if group_type=='SIDE' else
                  e['date_utc'] if group_type=='DATE' else e['observation_type'])==group
    keys={e['touch_key'] for e in events if belongs(e)}
    sub=[r for r in rows if r['touch_key'] in keys and r['mode']==mode and
         r['depth_pct']==depth and r['target_pct']==tp]
    eligible=[r for r in sub if r['cohort_eligible']]
    signalled=[r for r in eligible if r['signal_state']=='SIGNAL']
    counts=Counter(r['outcome'] for r in signalled)
    n=len(eligible);m=len(signalled)
    tps=counts['TP_FIRST']+counts['TP_GAP']
    sls=counts['SL_FIRST']+counts['SL_GAP']+counts['SL_FILL_BAR']
    amb=counts['SL_AMBIGUOUS']+counts['FILL_BAR_TP_UNORDERED']+counts['STRUCT_STOP_GAP_AT_FILL']
    def average(field,denom):
        return round(sum(float(r[field]) for r in signalled)/denom,7) if denom else None
    improvements=[r['entry_improvement_vs_immediate_pct'] for r in signalled if r['entry_improvement_vs_immediate_pct'] is not None]
    distances=[r['stop_distance_pct'] for r in signalled if r['stop_distance_pct'] is not None]
    return dict(group_type=group_type,group=group,mode=mode,depth_pct=depth,target_pct=tp,
        stop_mode=stop_mode,sl_pct=sl if stop_mode=='pct' else None,
        cohort_total=len(keys),eligible_common=n,signal_n=m,
        signal_pct=round(m/n*100,3) if n else None,
        no_signal_n=sum(r['signal_state']=='NO_SIGNAL' for r in eligible),
        no_sweep_n=sum(r['no_signal_reason']=='NO_SWEEP' for r in eligible),
        no_reversal_n=sum(r['no_signal_reason']=='NO_REVERSAL' for r in eligible),
        no_fill_n=sum(r['no_signal_reason']=='NO_FILL' for r in eligible),
        invalid_metadata_n=sum(r['signal_state']=='INVALID_METADATA' for r in eligible),
        censored_n=len(keys)-n,tp_first_n=tps,sl_first_n=sls,ambiguous_n=amb,
        entry_bar_uncertain_n=sum(bool(r['entry_bar_uncertain']) for r in signalled),
        time_exit_n=counts['TIME_EXIT'],struct_stop_gap_n=counts['STRUCT_STOP_GAP_AT_FILL'],
        tp_rate_signalled_pct=round(100*tps/m,3) if m else None,
        net_mean_per_signal_pct=average('net_pct',m),
        net_mean_per_eligible_pct=average('net_pct',n),
        net_worst_mean_per_signal_pct=average('net_worst_pct',m),
        net_best_mean_per_signal_pct=average('net_best_pct',m),
        entry_improvement_median_pct=round(statistics.median(improvements),7) if improvements else None,
        stop_distance_median_pct=round(statistics.median(distances),7) if distances else None)


def build(project,*,journal_db=None,first_touch_db=None,auto_db=None,
          recovery_root=None,sample_metadata=None,max_wait=60,hold=180,
          stop_mode='pct',sl_pct=1.,struct_buffer_pct=.1,
          depths=DEFAULT_DEPTHS,cost=.2,min_leg=.25,break_buffer=0.,out=None):
    depths=v2.parse_depths(depths)
    if depths!=DEFAULT_DEPTHS or min_leg!=DEFAULT_MIN_LEG_PCT or break_buffer!=DEFAULT_BREAK_BUFFER_PCT:
        raise ValueError('V3 frozen experiment: depths 1,2; min leg 0.25; break buffer 0')
    if not (stop_mode in ('pct','structural') and 1<=max_wait<=120 and
            15<=hold<=360 and max_wait+hold<=360 and
            math.isfinite(sl_pct) and .1<=sl_pct<=10 and
            math.isfinite(struct_buffer_pct) and .01<=struct_buffer_pct<=2 and
            math.isfinite(min_leg) and 0<=min_leg<=5 and
            math.isfinite(break_buffer) and 0<=break_buffer<=1 and
            math.isfinite(cost) and 0<=cost<=5):
        raise ValueError('Invalid V3 parameters')
    root=Path(project).resolve()
    hist=Path(recovery_root).resolve() if recovery_root else root/'reports/reaction_lab/first_touch_historical_recovery_v11'
    if sample_metadata:
        journal=v1.read_sample_journal(sample_metadata)
        iterator=v1.sample_paths(journal,hist)
        scope='SAMPLE_V11_ONLY_NO_ONLINE_PREFIX'
    else:
        journal=v1.read_journal(journal_db or root/'reports/reaction_lab/scanner_first_touches_v1.sqlite3')
        recoveries,_=recovery_index(hist)
        iterator=iter_events(root,first_touch_db=first_touch_db,auto_db=auto_db,
                             recovery=recoveries,include_bars=True,only_status='TOUCH_FAILED')
        scope='FULL_VPS_VERIFIED_ONLINE_AND_HISTORY'
    events=[];outcomes=[];seen=set()
    def consume(row,bars):
        key=row['touch_key']
        if key in seen:raise ValueError('Duplicate touch key '+key)
        seen.add(key)
        if row['first_touch_status']!='TOUCH_FAILED':raise ValueError('Wrong touch status')
        base=v1.event_base(row,bars,journal.get(key),max_wait,hold)
        events.append(base)
        for depth in depths:
            for mode in MODES:
                for tp in TARGETS:
                    outcomes.append(evaluate(base,bars,mode,depth,tp,stop_mode=stop_mode,sl=sl_pct,
                        buffer=struct_buffer_pct,max_wait=max_wait,hold=hold,cost=cost,
                        min_leg=min_leg,break_buffer=break_buffer))
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
    by_scenario=defaultdict(list)
    for r in outcomes:by_scenario[(r['mode'],r['depth_pct'],r['target_pct'])].append(r)
    summaries=[]
    for gtype,groups in [('ALL',['ALL']),('SIDE',['LONG','SHORT']),
                         ('DATE',sorted({e['date_utc'] for e in events})),
                         ('OBSERVATION',sorted({e['observation_type'] for e in events}))]:
        for group in groups:
            for depth in depths:
                for mode in MODES:
                    for tp in TARGETS:
                        summaries.append(summarize(by_scenario[(mode,depth,tp)],events,gtype,
                            group,mode,depth,tp,stop_mode,sl_pct))
    folder=config_dir(root,stop_mode,sl_pct,struct_buffer_pct,out)
    folder.mkdir(parents=True,exist_ok=True)
    manifest=dict(schema=SCHEMA,created_utc=datetime.now(timezone.utc).isoformat(),
        scope=scope,source_journal=str(sample_metadata or journal_db or root/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'),
        historical_root=str(hist),cohort_total=len(events),journal_total=len(journal),
        coverage_common=sum(e['eligible_common_180m'] for e in events),
        censored_common=sum(not e['eligible_common_180m'] for e in events),
        metadata_states=dict(Counter(e['metadata_state'] for e in events)),
        sources=dict(Counter(e['path_source'] for e in events)),
        observation_types=dict(Counter(e['observation_type'] for e in events)),
        modes=list(MODES),targets_pct=list(TARGETS),
        settings=dict(max_wait_min=max_wait,hold_min=hold,depths_pct=list(depths),
            retracements_pct=list(DEFAULT_RETRACEMENTS),stop_mode=stop_mode,
            sl_pct=sl_pct if stop_mode=='pct' else None,
            struct_buffer_pct=struct_buffer_pct if stop_mode=='structural' else None,
            reversal_min_leg_pct=min_leg,reversal_break_buffer_pct=break_buffer,
            cost_roundtrip_pct_points=cost,minimum_common_bars=max_wait+hold),
        notes=[
            'Sweep adverse to ORIGINAL direction, measured from FAILED retest CLOSE.',
            'Reversal uses a LATER closed bar crossing prior bar HIGH/LOW; it enters at NEXT OPEN.',
            'Retracement LIMIT is frozen at reversal close using ONLY known sweep extreme and close.',
            'Orders never fill on the confirmation candle and expire by the fixed max_wait after FAILED.',
            'Intrabar limit TP in fill candle is NOT credited; worst/best result bounds preserved.',
            'Structural stop anchored beyond extreme known at reversal close; gaps through stop tagged.',
            'No sweep, reversal, or fill remain in identical cohort denominator as zero exposure.',
            'V2 rebound/reclaim are controls, not replacements for V2 reports.',
            'Verified 1m sources and historical SHA/identity/prefix; no active SQLite or LIVE writes.',
            '0.20%-point constant exploratory cost; no slippage/funding/portfolio or OOS validation.',
        ])
    v1.write_csv_atomic(folder/'events.csv',events,EVENT_FIELDS)
    v1.write_csv_atomic(folder/'event_outcomes.csv',outcomes,OUTCOME_FIELDS)
    v1.write_csv_atomic(folder/'summary.csv',summaries,SUMMARY_FIELDS)
    v1.write_json_atomic(folder/'manifest.json',manifest)
    return {**manifest,'out_dir':str(folder)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.')
    p.add_argument('--journal-db');p.add_argument('--first-touch-db');p.add_argument('--auto-db')
    p.add_argument('--recovery-root');p.add_argument('--sample-metadata')
    p.add_argument('--max-wait',type=int,default=60)
    p.add_argument('--hold',type=int,default=180)
    p.add_argument('--stop-mode',choices=['pct','structural'],default='pct')
    p.add_argument('--sl-pct',type=float,default=DEFAULT_SL_PCT)
    p.add_argument('--struct-buffer-pct',type=float,default=DEFAULT_STRUCT_BUFFER_PCT)
    p.add_argument('--sweep-depths',default='1,2')
    p.add_argument('--min-leg-pct',type=float,default=DEFAULT_MIN_LEG_PCT)
    p.add_argument('--break-buffer-pct',type=float,default=DEFAULT_BREAK_BUFFER_PCT)
    p.add_argument('--cost-roundtrip-pct',type=float,default=.2)
    p.add_argument('--out')
    choice=p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--plan',action='store_true')
    choice.add_argument('--run',action='store_true')
    a=p.parse_args(argv)
    try:
        depths=v2.parse_depths(a.sweep_depths)
        if depths!=DEFAULT_DEPTHS or a.min_leg_pct!=DEFAULT_MIN_LEG_PCT or a.break_buffer_pct!=DEFAULT_BREAK_BUFFER_PCT:
            raise ValueError('V3 frozen experiment: sweep 1,2; min leg 0.25; break buffer 0')
        if not (1<=a.max_wait<=120 and 15<=a.hold<=360 and a.max_wait+a.hold<=360 and
                .1<=a.sl_pct<=10 and .01<=a.struct_buffer_pct<=2 and
                0<=a.min_leg_pct<=5 and 0<=a.break_buffer_pct<=1 and
                0<=a.cost_roundtrip_pct<=5):raise ValueError('Bad wait/hold/SL/buffer configuration')
    except ValueError as exc:p.error(str(exc))
    if a.plan:
        journal=v1.read_sample_journal(a.sample_metadata) if a.sample_metadata else v1.read_journal(
            a.journal_db or Path(a.project)/'reports/reaction_lab/scanner_first_touches_v1.sqlite3')
        print('FAILED_REVERSAL_V3_PLAN '+json.dumps(dict(schema=SCHEMA,mode='PLAN',
            journal_failed=len(journal),modes=list(MODES),depths_pct=list(depths),
            retracements_pct=list(DEFAULT_RETRACEMENTS),targets_pct=list(TARGETS),
            stop_mode=a.stop_mode,sl_pct=a.sl_pct if a.stop_mode=='pct' else None,
            struct_buffer_pct=a.struct_buffer_pct if a.stop_mode=='structural' else None,
            common_min_bars=a.max_wait+a.hold,read_only_sources=True,
            sample=bool(a.sample_metadata)),ensure_ascii=False))
        return 0
    result=build(a.project,journal_db=a.journal_db,first_touch_db=a.first_touch_db,
        auto_db=a.auto_db,recovery_root=a.recovery_root,sample_metadata=a.sample_metadata,
        max_wait=a.max_wait,hold=a.hold,stop_mode=a.stop_mode,sl_pct=a.sl_pct,
        struct_buffer_pct=a.struct_buffer_pct,depths=depths,cost=a.cost_roundtrip_pct,
        min_leg=a.min_leg_pct,break_buffer=a.break_buffer_pct,out=a.out)
    print('FAILED_REVERSAL_V3_RESULT '+json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
