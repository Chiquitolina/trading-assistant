#!/usr/bin/env python3
"""Offline band execution triggers: closed 1m evidence and fresh 5m closes.

Research events and next-minute price references only. No exchange imports,
orders, position sizing, TP/SL, net PnL, or rewriting of structural parents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_structural_behavior_v13 import StageIndex
from btc_multiswing_60d_explorer_v1 import load_snapshot,FREEZE_REL,CATALOG_REL

M=60_000
BASE=Path('reports/reaction_lab/btc_multiswing_60d_v1')
V13=BASE/'btc_sep09_nov08_structural_behavior_fixed_vs_asof_v13'
V12=BASE/'btc_sep09_nov08_structural_behavior_15m_1h_v12'
V121=BASE/'btc_sep09_nov08_structural_behavior_pre_safety_v121'
DEFAULT_OUTPUT=BASE/'btc_sep09_nov08_execution_1m_5m_v01'
VERSION='btc-multiswing-execution-closed-1m-fresh-5m-v0.1'
PARENT_VERSION='btc-multiswing-structural-behavior-fixed-vs-causal-stage-v1.3'
SELECTED_CASE='msv2_e27b91e389760550bc49|V16|1793235780000'


@dataclass(frozen=True)
class Config:
    trigger_minutes:int=5
    confirm_closes:int=2
    inside_closes:int=3
    horizon_minutes:int=180
    outcome_horizons:tuple=(30,60,180)

    def validate(self):
        if self.trigger_minutes!=5 or self.confirm_closes!=2 or self.inside_closes!=3:
            raise ValueError('V01_FIXED_BASELINE_5M_2C_3INSIDE')
        if self.horizon_minutes<=0 or any(int(x)<=0 for x in self.outcome_horizons):
            raise ValueError('INVALID_HORIZON')


def utc(ts):
    return pd.to_datetime(int(ts),unit='ms',utc=True).strftime('%Y-%m-%d %H:%M UTC')


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def ident(prefix,values):
    return prefix+hashlib.sha256(json.dumps(values,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()[:24]


def bounds(s):
    return None if s is None else (float(s['band_low']),float(s['band_high']))


def position(price,s):
    if s is None or not np.isfinite(float(price)):return 'UNKNOWN'
    return 'BELOW' if price<float(s['band_low']) else 'ABOVE' if price>float(s['band_high']) else 'INSIDE'


def opposite(side):
    return {'ABOVE':'BELOW','BELOW':'ABOVE'}.get(side,'INSIDE')


def closed_bars(full,width_minutes,start,end):
    """Complete epoch-aligned UTC buckets, with integer-millisecond clocks."""
    step=int(width_minutes)*M
    key=(full.timestamp.astype('int64')//step)*step
    out=full.groupby(key,sort=True).agg(open=('open','first'),high=('high','max'),
        low=('low','min'),close=('close','last'),count=('timestamp','size'))
    out=out.loc[out['count'].eq(int(width_minutes))].drop(columns='count')
    out.index.name='timestamp';out=out.reset_index();out['closed_ts']=out.timestamp+step
    return out.loc[out.closed_ts.gt(int(start))&out.closed_ts.le(int(end))].reset_index(drop=True)


class Market:
    def __init__(self,minute,stages):
        self.frame=minute
        self.stages=stages
        self.opens=minute.timestamp.to_numpy(dtype='int64')
        if not len(self.opens) or np.any(np.diff(self.opens)!=M):raise ValueError('NONCONTIGUOUS_1M_SOURCE')
        self.closes=self.opens+M
        self.prices={k:minute[k].to_numpy(dtype=float) for k in ('open','high','low','close')}
        if not np.isfinite(np.column_stack(list(self.prices.values()))).all():raise ValueError('NONFINITE_1M_SOURCE')
        if (np.any(self.prices['low']>np.minimum(self.prices['open'],self.prices['close'])) or
            np.any(self.prices['high']<np.maximum(self.prices['open'],self.prices['close'])) or
            np.any(self.prices['low']<=0)):raise ValueError('INVALID_1M_OHLC')
        frame=closed_bars(minute,5,int(self.opens[0]),int(self.closes[-1]))
        self.five={int(r.closed_ts):r for r in frame.itertuples(index=False)}


def replay_visit(market,visit,config=Config(),available_end=None):
    config.validate()
    vid=str(visit['event_id']);region=str(visit['region_id']);arrival=int(visit['pre_cutoff_ts'])
    if arrival%M:raise ValueError('ARRIVAL_NOT_ON_MINUTE_OPEN')
    target=arrival+config.horizon_minutes*M
    source_end=int(market.closes[-1])
    end=min(target,source_end,source_end if available_end is None else int(available_end))
    if end<arrival:raise ValueError('AVAILABLE_PREFIX_BEFORE_ARRIVAL')
    rows=[];contact=None;active=None;last_defense=None;defended=False;free_separated=False
    counters=dict(ABOVE=0,BELOW=0,INSIDE=0)
    origin='INSIDE';last_tf='UNKNOWN';regime_start=arrival;regime_bounds=None
    stopped=None;geometry_changes=0;last_observed=arrival
    start_i=int(np.searchsorted(market.opens,arrival));stop_i=int(np.searchsorted(market.closes,end,side='right'))
    if start_i>=len(market.opens) or market.opens[start_i]!=arrival:raise ValueError('ARRIVAL_OUTSIDE_SOURCE')
    initial=market.stages.at(region,arrival)
    if start_i>0:origin=position(market.prices['close'][start_i-1],initial)
    regime_bounds=bounds(initial)

    def emit(kind,when,stage,side='',bar_open=None,parent=None,family='',touch=None,note=''):
        pid=parent['event_id'] if parent else ''
        physical_parent=parent['physical_event_id'] if parent else ''
        geometry=bounds(stage)
        physical=ident('mexphys_',[VERSION,region,kind,int(when),side,geometry,physical_parent])
        event_id=ident('mex_',[VERSION,vid,kind,int(when),pid,geometry])
        i=int(np.searchsorted(market.closes,int(when),side='right'))-1
        row=dict(event_id=event_id,physical_event_id=physical,visit_id=vid,episode_id=str(visit['episode_id']),
            region_id=region,arrival_ts=arrival,event_type=kind,direction=side,
            event_ts=int(when),available_ts=int(when),decision_ts=int(when),available_utc=utc(when),
            bar_open_ts=int(bar_open if bar_open is not None else int(when)-M),
            source_tf='M1_CLOSED' if kind in ('CONTACT','RETEST_CONTACT','GEOMETRY_REVALIDATE') else 'M5_CLOSED',
            confirmation_close=float(market.prices['close'][i]) if i>=0 else None,
            band_low=float(stage['band_low']) if stage else None,band_high=float(stage['band_high']) if stage else None,
            stage_known_from=int(stage['known_from']) if stage else None,regime_start_ts=regime_start,
            origin_position=parent['origin_position'] if parent else origin,parent_event_id=pid,parent_physical_event_id=physical_parent,
            contact_event_id=touch['event_id'] if touch else '',family=family,data_complete=True,
            evidence_status='CLOSED_1M_RANGE_NO_INTRAMINUTE_ORDER' if kind in ('CONTACT','RETEST_CONTACT') else 'CLOSED_FRESH_BAR_EVIDENCE',
            note=note,data_role='EXPERIMENTAL_TRIGGER_NOT_AN_ORDER_OR_PRE_FEATURE')
        rows.append(row)
        return row

    def reset_geometry(when,stage,prior_close):
        nonlocal contact,active,last_defense,defended,free_separated,counters,origin,last_tf,regime_start,regime_bounds,geometry_changes
        previous_root=active['root'] if active else None
        regime_start=int(when);regime_bounds=bounds(stage);geometry_changes+=1
        emit('GEOMETRY_REVALIDATE',when,stage,parent=previous_root,note='PENDING_CHAIN_CLOSED_BY_CHANGED_BOUNDS')
        contact=None;active=None;last_defense=None;defended=False;free_separated=False
        counters=dict(ABOVE=0,BELOW=0,INSIDE=0);last_tf='UNKNOWN'
        origin=position(prior_close,stage)

    def start_break(side,when,stage,bar,from_side,relation=''):
        nonlocal active,origin,last_defense,defended,free_separated
        kind='EXIT_CONFIRMED' if from_side=='INSIDE' else ('BREAK_UP_CONFIRMED' if side=='ABOVE' else 'BREAK_DOWN_CONFIRMED')
        old_origin=origin;origin=from_side
        root=emit(kind,when,stage,side,bar_open=bar.timestamp,
                  family='BREAK_DIRECT' if from_side in ('ABOVE','BELOW') else '',touch=contact,note=relation)
        origin=old_origin
        if last_defense is not None and last_defense['direction']!=side:
            emit('DEFENSE_FAILED',when,stage,side,bar_open=bar.timestamp,parent=last_defense,touch=contact)
        last_defense=None;defended=False;free_separated=False
        active=dict(root=root,far=side,origin=from_side,separated=False,retest=None,ever_retested=False,
                    hold_run=0,return_run=0,inside_run=0,outside_run=0,accepted=False)
        return root

    for i in range(start_i,stop_i):
        op=int(market.opens[i]);cl=op+M
        opened=market.stages.at(region,op);closed=market.stages.at(region,cl)
        if opened is None or closed is None:
            stopped='UNKNOWN_GEOMETRY';break
        prior_close=float(market.prices['close'][i-1]) if i else float(market.prices['open'][i])
        if bounds(opened)!=regime_bounds:reset_geometry(op,opened,prior_close)
        lo,hi=float(opened['band_low']),float(opened['band_high'])
        touch=market.prices['high'][i]>=lo and market.prices['low'][i]<=hi
        separated_above=market.prices['low'][i]>hi
        separated_below=market.prices['high'][i]<lo
        if active is None:
            new_contact=contact is None or (defended and free_separated)
            if touch and new_contact:
                origin=position(prior_close,opened)
                contact=emit('CONTACT',cl,opened,origin,bar_open=op)
                counters=dict(ABOVE=0,BELOW=0,INSIDE=0);last_tf='UNKNOWN';defended=False;free_separated=False
            elif defended and ((origin=='ABOVE' and separated_above) or (origin=='BELOW' and separated_below)):
                free_separated=True
        elif op>=int(active['root']['available_ts']):
            if active['retest'] is None:
                if active['separated'] and touch:
                    retest=emit('RETEST_CONTACT',cl,opened,active['far'],bar_open=op,parent=active['root'])
                    active.update(retest=retest,separated=False,ever_retested=True,hold_run=0)
                elif (active['far']=='ABOVE' and separated_above) or (active['far']=='BELOW' and separated_below):
                    active['separated']=True
        if bounds(closed)!=regime_bounds:reset_geometry(cl,closed,float(market.prices['close'][i]))
        last_observed=cl
        if cl%(config.trigger_minutes*M):continue
        bar=market.five.get(cl)
        if bar is None or contact is None or int(bar.timestamp)<max(regime_start,int(contact['available_ts'])):continue
        current=position(float(bar.close),closed)
        for side in counters:counters[side]=counters[side]+1 if current==side else 0
        if counters['INSIDE']==config.inside_closes:emit('ACCEPT_INSIDE',cl,closed,'INSIDE',bar_open=bar.timestamp,touch=contact)
        if active is None:
            if origin in ('ABOVE','BELOW') and counters[opposite(origin)]==config.confirm_closes:
                start_break(opposite(origin),cl,closed,bar,origin)
            elif origin=='INSIDE':
                for side in ('ABOVE','BELOW'):
                    if counters[side]==config.confirm_closes:
                        start_break(side,cl,closed,bar,'INSIDE');break
            elif counters[origin]==config.confirm_closes and not defended:
                last_defense=emit('DEFENSE_CONFIRMED',cl,closed,origin,bar_open=bar.timestamp,family='DEFENSE',touch=contact)
                defended=True
        else:
            a=active;far=a['far'];root=a['root']
            if current=='INSIDE' and last_tf!='INSIDE':emit('REENTER_BAND',cl,closed,'INSIDE',bar_open=bar.timestamp,parent=root)
            if int(bar.timestamp)>=int(root['available_ts']):
                a['return_run']=a['return_run']+1 if current==opposite(far) else 0
                a['inside_run']=a['inside_run']+1 if current=='INSIDE' else 0
                a['outside_run']=a['outside_run']+1 if current==far else 0
            if a['return_run']==config.confirm_closes:
                emit('EXIT_REVERSED' if a['origin']=='INSIDE' else 'BREAK_FAILED',cl,closed,opposite(far),
                     bar_open=bar.timestamp,parent=root,touch=a['retest'])
                start_break(opposite(far),cl,closed,bar,far,'REVERSAL_AFTER_FAILURE_OR_EXIT')
            elif a['origin']=='INSIDE' and a['inside_run']==config.confirm_closes:
                emit('EXIT_FAILED_RETURN_INSIDE',cl,closed,'INSIDE',bar_open=bar.timestamp,parent=root)
                active=None;contact=None;origin='INSIDE';counters=dict(ABOVE=0,BELOW=0,INSIDE=0)
            else:
                if a['retest'] is not None and int(bar.timestamp)>=int(a['retest']['available_ts']):
                    a['hold_run']=a['hold_run']+1 if current==far else 0
                    if a['hold_run']==config.confirm_closes:
                        emit('RETEST_HOLD_CONFIRMED',cl,closed,far,bar_open=bar.timestamp,parent=root,
                             family='BREAK_RETEST',touch=a['retest'],note='TWO_FULL_NEW_5M_BARS_AFTER_RETEST')
                        a['retest']=None;a['hold_run']=0;a['separated']=False
                if not a['ever_retested'] and a['outside_run']==config.confirm_closes and not a['accepted']:
                    emit('ACCEPT_OUTSIDE',cl,closed,far,bar_open=bar.timestamp,parent=root)
                    a['accepted']=True
        last_tf=current
    health=dict(visit_id=vid,region_id=region,arrival_ts=arrival,horizon_target_ts=target,
        observed_until_ts=last_observed,data_complete=stopped is None,
        right_censored=last_observed<target,terminal_reason=stopped or
            ('AVAILABLE_PREFIX_END' if available_end is not None and int(available_end)<min(target,source_end) else 'SOURCE_END' if end<target else 'WINDOW_COMPLETE'),
        event_count=len(rows),trigger_count=sum(bool(r['family']) for r in rows),geometry_changes=geometry_changes,
        contact_observed=any(r['event_type']=='CONTACT' for r in rows))
    return rows,health


def input_paths(root):
    paths=[V13/'manifest.json',V12/'manifest.json',V121/'manifest.json',
           V12/'visit_pre_v12.csv',V121/'geometric_stage_history_v121.csv',V13/'asof_transition_observations_v13.csv',
           FREEZE_REL/'manifest.json',FREEZE_REL/'BTCUSDT_1m.csv',FREEZE_REL/'BTCUSDT_1m_warmup.csv',
              CATALOG_REL/'manifest.json',CATALOG_REL/'confirmed_swings.csv',
              Path('btc_multiswing_structural_behavior_v13.py'),Path('btc_multiswing_60d_explorer_v1.py')]
    return {str(p):root/p for p in paths}


def load_inputs(root):
    root=Path(root).resolve()
    manifests={name:json.loads((root/path/'manifest.json').read_text()) for name,path in [('v13',V13),('v12',V12),('v121',V121)]}
    if manifests['v13'].get('version')!=PARENT_VERSION:raise ValueError('WRONG_V13_PARENT')
    for name,m in manifests.items():
        if not m.get('immutable_run') or not m.get('no_live_imports_or_network'):raise ValueError('UNVERIFIED_IMMUTABLE_PARENT: '+name)
    for name,path in [('v12',V12),('v121',V121)]:
        if manifests['v13']['parent_manifest_sha256'].get(name)!=digest(root/path/'manifest.json'):
            raise ValueError('V13_PARENT_CHAIN_MISMATCH: '+name)
    if manifests['v121'].get('parent_v12_manifest_sha256')!=digest(root/V12/'manifest.json'):
        raise ValueError('V121_PARENT_CHAIN_MISMATCH')
    def csv(path,name,label):
        if digest(root/path/name)!=manifests[label]['files_sha256'].get(name):raise ValueError('PARENT_CSV_HASH_MISMATCH: '+name)
        return pd.read_csv(root/path/name,low_memory=False)
    visits=csv(V12,'visit_pre_v12.csv','v12')
    stages=csv(V121,'geometric_stage_history_v121.csv','v121')
    legacy=csv(V13,'asof_transition_observations_v13.csv','v13')
    if visits.event_id.duplicated().any():raise ValueError('DUPLICATE_VISIT_ID')
    full,_,source=load_snapshot(root)
    for m in manifests.values():
        for key in ('start','end','source_sha256','catalog_sha256'):
            if m['source_provenance'].get(key)!=source[key]:raise ValueError('FROZEN_SOURCE_MISMATCH: '+key)
    fingerprints={key:digest(path) for key,path in input_paths(root).items()}
    market=Market(full,StageIndex(stages))
    return dict(root=root,visits=visits,stages=stages,legacy=legacy,market=market,source=source,fingerprints=fingerprints)


def entry_references(market,triggers):
    rows=[]
    for t in triggers.to_dict('records'):
        i=int(np.searchsorted(market.opens,int(t['available_ts']),side='right'))
        present=i<len(market.opens)
        rows.append(dict(event_id=t['event_id'],physical_event_id=t['physical_event_id'],visit_id=t['visit_id'],
            region_id=t['region_id'],family=t['family'],direction=t['direction'],available_ts=int(t['available_ts']),
            reference_open_ts=int(market.opens[i]) if present else None,
            reference_open_price=float(market.prices['open'][i]) if present else None,
            reference_status='REFERENCE_ONLY_EXITS_COSTS_UNSET' if present else 'SOURCE_END_NO_REFERENCE',
            model='NEXT_1M_OPEN_STRICTLY_AFTER_AVAILABLE_NOT_A_FILL',net_pnl_available=False))
    out=pd.DataFrame(rows,columns=['event_id','physical_event_id','visit_id','region_id','family','direction','available_ts',
        'reference_open_ts','reference_open_price','reference_status','model','net_pnl_available'])
    out['reference_open_ts']=out.reference_open_ts.astype('Int64')
    return out


def path_outcomes(market,references,horizons):
    rows=[]
    for r in references.to_dict('records'):
        present=pd.notna(r['reference_open_ts'])
        for horizon in horizons:
            target=int(r['reference_open_ts'])+int(horizon)*M if present else None
            start=int(np.searchsorted(market.opens,int(r['reference_open_ts']))) if present else 0
            stop=int(np.searchsorted(market.closes,target,side='right')) if present else 0
            complete=bool(present and stop-start==int(horizon))
            mfe=mae=markout=None
            observed_mfe=observed_mae=None
            if present and stop>start:
                p=float(r['reference_open_price']);hi=float(np.max(market.prices['high'][start:stop]));lo=float(np.min(market.prices['low'][start:stop]))
                sign=1 if r['direction']=='ABOVE' else -1
                observed_mfe=max(0.,(hi-p if sign==1 else p-lo)/p*100)
                observed_mae=max(0.,(p-lo if sign==1 else hi-p)/p*100)
                if complete:
                    mfe=observed_mfe;mae=observed_mae
                    markout=(float(market.prices['close'][stop-1])-p)/p*100*sign
            rows.append(dict(event_id=r['event_id'],physical_event_id=r['physical_event_id'],visit_id=r['visit_id'],
                family=r['family'],direction=r['direction'],reference_open_ts=r['reference_open_ts'],horizon_minutes=int(horizon),
                evaluation_target_ts=target,evaluation_available_ts=target if complete else None,
                status='COMPLETE_GROSS_PATH' if complete else 'RIGHT_CENSORED' if present else 'NO_REFERENCE_OPEN',
                observed_minutes=max(0,stop-start),mfe_pct=mfe,mae_pct=mae,gross_markout_pct=markout,
                observed_mfe_pct=observed_mfe,observed_mae_pct=observed_mae,
                data_role='POST_PATH_DESCRIPTOR_NOT_TRADE_PNL_OR_A_PRE_FEATURE'))
    return pd.DataFrame(rows,columns=['event_id','physical_event_id','visit_id','family','direction','reference_open_ts','horizon_minutes',
        'evaluation_target_ts','evaluation_available_ts','status','observed_minutes','mfe_pct','mae_pct','gross_markout_pct',
        'observed_mfe_pct','observed_mae_pct','data_role'])


def trigger_pre_context(market,triggers):
    tf_frames={1:market.frame.assign(closed_ts=market.closes)}
    for tf in (5,15,60,240):tf_frames[tf]=closed_bars(market.frame,tf,int(market.opens[0]),int(market.closes[-1]))
    rows=[]
    for t in triggers.to_dict('records'):
        cutoff=int(t['bar_open_ts']);s=market.stages.at(t['region_id'],cutoff)
        row=dict(event_id=t['event_id'],visit_id=t['visit_id'],region_id=t['region_id'],pre_cutoff_ts=cutoff,
                 pre_stage_low=float(s['band_low']) if s else None,pre_stage_high=float(s['band_high']) if s else None,
                 pre_stage_known_from=int(s['known_from']) if s else None,data_role='BEFORE_CONFIRMING_5M_BAR_NO_LABELS')
        for tf,frame in tf_frames.items():
            j=int(np.searchsorted(frame.closed_ts.to_numpy(dtype='int64'),cutoff,side='right'))-1
            row[f'pre_{tf}m_closed_ts']=int(frame.closed_ts.iloc[j]) if j>=0 else None
            row[f'pre_{tf}m_close']=float(frame.close.iloc[j]) if j>=0 else None
            row[f'pre_{tf}m_position']=position(float(frame.close.iloc[j]),s) if j>=0 else 'UNKNOWN'
        rows.append(row)
    columns=['event_id','visit_id','region_id','pre_cutoff_ts','pre_stage_low','pre_stage_high','pre_stage_known_from','data_role']
    columns += [f'pre_{tf}m_{field}' for tf in (1,5,15,60,240) for field in ('closed_ts','close','position')]
    out=pd.DataFrame(rows,columns=columns)
    for field in ['pre_stage_known_from']+[f'pre_{tf}m_closed_ts' for tf in (1,5,15,60,240)]:out[field]=out[field].astype('Int64')
    return out


EVENT_COLUMNS=['event_id','physical_event_id','visit_id','episode_id','region_id','arrival_ts','event_type','direction',
    'event_ts','available_ts','decision_ts','available_utc','bar_open_ts','source_tf','confirmation_close','band_low','band_high',
    'stage_known_from','regime_start_ts','origin_position','parent_event_id','parent_physical_event_id','contact_event_id',
    'family','data_complete','evidence_status','note','data_role']


def compute(study,config=Config(),progress=False):
    config.validate();market=study['market'];events=[];health=[]
    for n,visit in enumerate(study['visits'].to_dict('records'),1):
        rows,h=replay_visit(market,visit,config)
        events.extend(rows);health.append(h)
        if progress and n%400==0:print('MS_EXECUTION_V01_PROGRESS',json.dumps({'visits':n,'total':len(study['visits'])}),flush=True)
    observations=pd.DataFrame(events,columns=EVENT_COLUMNS)
    if observations.event_id.duplicated().any():raise ValueError('DUPLICATE_EXECUTION_EVENT_ID')
    physical=observations.drop_duplicates('physical_event_id').copy()
    triggers=observations.loc[observations.family.ne('')].copy()
    if len(triggers):
        triggers['clock_group_id']=[ident('mexclock_',[int(t),f,d]) for t,f,d in zip(triggers.available_ts,triggers.family,triggers.direction)]
        triggers['concurrent_bands']=triggers.groupby('clock_group_id',dropna=False).region_id.transform('nunique')
    else:triggers=triggers.assign(clock_group_id=pd.Series(dtype=str),concurrent_bands=pd.Series(dtype=int))
    references=entry_references(market,triggers)
    outcomes=path_outcomes(market,references,config.outcome_horizons)
    pre=trigger_pre_context(market,triggers)
    health=pd.DataFrame(health)
    summary=dict(version=VERSION,scope='ALL_CANONICAL_PARENT_VISITS_NEW_WINDOWS_NOT_V13_EVENT_RELABELING',
        counts=dict(visits=len(study['visits']),regions_with_visits=int(study['visits'].region_id.nunique()),
            geometric_regions=len(market.stages.groups),event_observations=len(observations),physical_events=len(physical),
            trigger_observations=len(triggers),physical_triggers=int(triggers.physical_event_id.nunique()),
            visits_with_contact=int(health.contact_observed.sum()),visits_with_trigger=int(health.trigger_count.gt(0).sum()),
            incomplete_visits=int((~health.data_complete).sum()),censored_visits=int(health.right_censored.sum())),
        families={family:dict(observations=len(g),physical_events=int(g.physical_event_id.nunique()),visits=int(g.visit_id.nunique()))
                  for family,g in triggers.groupby('family',sort=True)},
        outcome_statuses=outcomes.status.value_counts().to_dict(),
        interpretation='ACTIVATION_AND_GROSS_PATH_DESCRIPTORS_ONLY_NO_TRADES_OR_NET_PNL; VISITS_AND_BANDS_CAN_SHARE_MOVEMENTS')
    return {'execution_events_v01.csv':observations,'physical_execution_events_v01.csv':physical,
            'trigger_candidates_v01.csv':triggers,'entry_price_references_v01.csv':references,
            'trigger_pre_context_v01.csv':pre,'trigger_path_outcomes_v01.csv':outcomes,
            'visit_health_v01.csv':health},summary


def verified_output(folder):
    m=json.loads((Path(folder)/'manifest.json').read_text())
    if m.get('version')!=VERSION or not m.get('immutable_run') or not m.get('no_live_imports_or_network'):
        raise ValueError('INVALID_EXECUTION_RUN')
    for name,expected in m['files_sha256'].items():
        if Path(name).name!=name or digest(Path(folder)/name)!=expected:raise ValueError('EXECUTION_OUTPUT_HASH_MISMATCH: '+name)
    return m


def run(root,output,config=Config()):
    root=Path(root).resolve();output=Path(output).resolve()
    if output.exists():
        m=verified_output(output)
        if m.get('config')!=json.loads(json.dumps(asdict(config))) or m.get('implementation_sha256')!=digest(__file__):
            raise ValueError('OUTPUT_EXISTS_WITH_DIFFERENT_IMPLEMENTATION_OR_CONFIG')
        for name,expected in m['input_sha256'].items():
            if digest(root/name)!=expected:raise ValueError('EXISTING_RUN_INPUT_DRIFT: '+name)
        print('MS_EXECUTION_V01_ALREADY_COMPLETE',json.dumps({'output':str(output),'summary':m['summary']}),flush=True)
        return m['summary']
    study=load_inputs(root)
    frames,summary=compute(study,config,progress=True)
    for name,expected in study['fingerprints'].items():
        if digest(root/name)!=expected:raise ValueError('SOURCE_CHANGED_DURING_REPLAY: '+name)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='execution_v01_',dir=output.parent) as temporary:
        stage=Path(temporary)/'result';stage.mkdir()
        for name,frame in frames.items():frame.to_csv(stage/name,index=False)
        (stage/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
        m=dict(version=VERSION,immutable_run=True,no_live_imports_or_network=True,config=asdict(config),
               implementation_sha256=digest(__file__),input_sha256=study['fingerprints'],
               source_provenance=study['source'],summary=summary,runtime=dict(python=sys.version.split()[0],numpy=np.__version__,pandas=pd.__version__),
               files_sha256={p.name:digest(p) for p in sorted(stage.iterdir())})
        (stage/'manifest.json').write_text(json.dumps(m,indent=2,ensure_ascii=False)+'\n')
        if output.exists():raise ValueError('OUTPUT_APPEARED_DURING_REPLAY')
        stage.rename(output)
    print('MS_EXECUTION_V01_FINISHED',json.dumps({'output':str(output),'summary':summary}),flush=True)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--horizon-minutes',type=int,default=180)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    args=parser.parse_args();root=args.root.resolve();output=args.output or root/DEFAULT_OUTPUT
    config=Config(horizon_minutes=args.horizon_minutes);config.validate()
    print('MS_EXECUTION_V01_PLAN',json.dumps(dict(version=VERSION,root=str(root),output=str(output),config=asdict(config),
        families=['DEFENSE','BREAK_DIRECT','BREAK_RETEST'],scope='CANONICAL_BTC_VISITS',no_live=True,no_orders=True,no_net_pnl=True)),flush=True)
    if args.run:run(root,output,config)


if __name__=='__main__':main()
