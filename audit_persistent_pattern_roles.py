#!/usr/bin/env python3
"""Offline PRE-level pattern study followed by strictly separate First Touch outcome join.

Works on a previously audited persistent-horizontal-v1 run. Does not change its
shards, scanner, Live, SQLite, or original event_labels/candidate_levels data.

Exploratory categorization chosen from visual level hypothesis: LONG at support
(LOW), SHORT at resistance (HIGH), 2+ PRE responses, within 0.5 ATR, optionally
20+ real hours of persistence (15m/30m normalized). INSIDE is retained, not
misclassified as failure. No attempt here to prove a level remains unbroken.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import pandas as pd

from audit_persistent_first_touch import summarize, require_cols

NEAR='PERSISTENT_LEVEL_NEAR_PRE'
FAR='NO_PERSISTENT_LEVEL_NEAR_PRE'
UNKNOWN='UNKNOWN_HISTORY'
PAT_LONG='NEAR_COMPATIBLE_20H_PRE'
PAT_SHORT='NEAR_COMPATIBLE_UNDER20H_PRE'
PAT_OTHER='NEAR_NO_COMPATIBLE_LEVEL_PRE'
G_FAR='NO_PERSISTENT_LEVEL_NEAR_PRE'
G_UNKNOWN='UNKNOWN_HISTORY'
GROUPS=(PAT_LONG,PAT_SHORT,PAT_OTHER,G_FAR,G_UNKNOWN)
MIN_HOURS=20.0
NEAR_ATR=.5
TF_HOURS={'15m':.25,'30m':.5}


def digest(file):
    h=hashlib.sha256()
    with Path(file).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):
            h.update(block)
    return h.hexdigest()


def read_pre(run_folder):
    run_folder=Path(run_folder)
    audit=json.loads((run_folder/'audit.json').read_text())
    if not audit.get('success') or audit.get('audit_rule')!='strict_response_close_before_retest_v1.1':
        raise ValueError('Expected successful strict-PRE v1.1 audit')
    paths={name:run_folder/name for name in ('event_labels.csv','candidate_levels.csv')}
    for name,path in paths.items():
        if not path.is_file() or digest(path)!=audit.get('outputs_sha256',{}).get(name):
            raise ValueError(f'Invalid or changed audited input: {name}')
    events=pd.read_csv(paths['event_labels.csv'],dtype={'event_key':str},low_memory=False)
    zones=pd.read_csv(paths['candidate_levels.csv'],dtype={'event_key':str},low_memory=False)
    require_cols(events,['event_key','symbol','side','retest_ts','group_pre','observation_type','chosen_role_pre','chosen_kind'], 'PRE events')
    require_cols(zones,['event_key','retest_ts','timeframe','zone_id','origin_kind','age_bars','independent_visits','confirmed_responses','distance_pre_atr','relation_pre','formation_closed_ts','response_timestamps'], 'PRE zones')
    if events.event_key.isna().any() or events.event_key.duplicated().any() or len(events)!=audit.get('events'):
        raise ValueError('PRE events not unique / audit size mismatch')
    if not events.group_pre.isin([NEAR,FAR,UNKNOWN]).all():
        raise ValueError('Unexpected PRE group')
    if not events.side.isin(['LONG','SHORT']).all():
        raise ValueError('Unexpected direction')
    if not events.observation_type.eq('LIVE_OBSERVED').all():
        raise ValueError('This report expects all-live')
    if zones.zone_id.duplicated().any():
        raise ValueError('Duplicate zone_id')
    if not zones.event_key.isin(events.event_key).all():
        raise ValueError('Orphan level event key')
    if not zones.timeframe.isin(TF_HOURS).all():
        raise ValueError('Unsupported timeframe in frozen candidates')
    numeric=['age_bars','independent_visits','confirmed_responses','distance_pre_atr','retest_ts','formation_closed_ts']
    for c in numeric:
        zones[c]=pd.to_numeric(zones[c],errors='coerce')
    if zones[numeric].isna().any().any():
        raise ValueError('Invalid frozen candidate numeric field')
    if (zones.formation_closed_ts>=zones.retest_ts).any():
        raise ValueError('Future-formed candidate zone')
    for vals in zones[['response_timestamps','retest_ts','confirmed_responses']].itertuples(index=False):
        try:ts=[int(v) for v in str(vals[0]).split(';') if v]
        except ValueError as exc:raise ValueError('Invalid historical response timestamp') from exc
        if len(ts)!=int(vals[2]) or len(ts)<2 or any(t>=int(vals[1]) for t in ts):
            raise ValueError('Noncausal or incomplete candidate response')
    return events,zones,audit


def classify_pre(events,zones):
    """Pure PRE classification. Does not accept First Touch data or outcomes."""
    events=events.copy()
    zones=zones.copy()
    require_cols(events,['event_key','symbol','side','group_pre','chosen_role_pre','chosen_kind'], 'events')
    require_cols(zones,['event_key','timeframe','origin_kind','age_bars','independent_visits','confirmed_responses','distance_pre_atr','relation_pre','zone_id'], 'zones')
    if zones.empty:
        zones=zones.assign(age_hours=pd.Series(dtype='float64'),compatible=pd.Series(dtype='bool'),inside=pd.Series(dtype='bool'))
    else:
        zones['age_hours']=pd.to_numeric(zones.age_bars,errors='raise')*zones.timeframe.map(TF_HOURS)
        reference=zones.merge(events[['event_key','side','group_pre']].rename(columns={'side':'event_side'}),on='event_key',how='left',validate='many_to_one')
        if 'side' in reference and not reference.side.eq(reference.event_side).all():
            raise ValueError('Zone side differs from PRE event side')
        expected=(reference.event_side.eq('LONG')&reference.origin_kind.eq('LOW')) | (reference.event_side.eq('SHORT')&reference.origin_kind.eq('HIGH'))
        correct_outside=(reference.event_side.eq('LONG')&reference.relation_pre.eq('ABOVE')) | (reference.event_side.eq('SHORT')&reference.relation_pre.eq('BELOW'))
        inside=reference.relation_pre.eq('INSIDE')
        within=pd.to_numeric(reference.distance_pre_atr,errors='raise') <= NEAR_ATR+1e-9
        history=(pd.to_numeric(reference.independent_visits,errors='raise')>=2)&(pd.to_numeric(reference.confirmed_responses,errors='raise')>=2)
        reference['compatible']=expected&(correct_outside|inside)&within&history&reference.group_pre.eq(NEAR)
        reference['inside']=inside
        zones=reference.drop(columns=['event_side','group_pre'])
    matches=zones[zones.compatible].copy()
    # One deterministic representative without looking at outcome: prefer longer
    # persistence, then nearest compatible zone and stronger historical responses.
    if not matches.empty:
        matches['long_age']=matches.age_hours>=MIN_HOURS
        matches=matches.sort_values(['event_key','long_age','distance_pre_atr','confirmed_responses','age_hours','timeframe','zone_id'],
                ascending=[True,False,True,False,False,True,True],kind='stable')
        picked=matches.drop_duplicates('event_key',keep='first').copy()
        picked=picked.rename(columns={'zone_id':'pattern_zone_id','timeframe':'pattern_tf',
            'age_hours':'pattern_age_hours','independent_visits':'pattern_prior_visits',
            'distance_pre_atr':'pattern_distance_atr','inside':'pattern_inside'})
        picked=picked[['event_key','pattern_zone_id','pattern_tf','pattern_age_hours','pattern_prior_visits','pattern_distance_atr','pattern_inside']]
    else:
        picked=pd.DataFrame(columns=['event_key','pattern_zone_id','pattern_tf','pattern_age_hours','pattern_prior_visits','pattern_distance_atr','pattern_inside'])
    labels=events.merge(picked,on='event_key',how='left',validate='one_to_one')
    has=labels.pattern_zone_id.notna()
    labels['pattern_pre']=G_UNKNOWN
    labels.loc[labels.group_pre==FAR,'pattern_pre']=G_FAR
    labels.loc[(labels.group_pre==NEAR)&~has,'pattern_pre']=PAT_OTHER
    labels.loc[(labels.group_pre==NEAR)&has,'pattern_pre']=PAT_SHORT
    labels.loc[(labels.group_pre==NEAR)&has&(labels.pattern_age_hours>=MIN_HOURS),'pattern_pre']=PAT_LONG
    if not labels.pattern_pre.isin(GROUPS).all():
        raise ValueError('Unclassified pattern label')
    # A 3-touch *chart* generally means 2 earlier PRE visits + REACTION touch;
    # this report never increments PRE visits using the current REACTION.
    labels['pattern_two_or_more_previous_visits']=has & (labels.pattern_prior_visits>=2)
    return labels


def join_first_touch(labels,ft_path):
    ft_path=Path(ft_path)
    head=pd.read_csv(ft_path,nrows=0)
    fields=['event_key','symbol','side','observation_type','entry_mode','tp_pct','sl_pct','result_180m','result_360m']
    require_cols(head,fields,'First Touch')
    ft=pd.read_csv(ft_path,usecols=fields,dtype={'event_key':str},low_memory=False)
    ft=ft[(ft.observation_type=='LIVE_OBSERVED')&(ft.entry_mode=='OBSERVED_OPEN')&
          (pd.to_numeric(ft.tp_pct,errors='coerce').sub(1).abs()<1e-9)&
          (pd.to_numeric(ft.sl_pct,errors='coerce').sub(1).abs()<1e-9)].copy()
    if ft.event_key.isna().any() or ft.event_key.duplicated().any():
        raise ValueError('First Touch duplicate/empty event keys for target scenario')
    merged=labels.merge(ft[['event_key','symbol','side','result_180m','result_360m']],on='event_key',how='left',
                        suffixes=('_pre','_ft'),indicator=True,validate='one_to_one')
    mask=merged._merge.eq('both')
    if ((merged.loc[mask,'symbol_pre']!=merged.loc[mask,'symbol_ft'])|
        (merged.loc[mask,'side_pre']!=merged.loc[mask,'side_ft'])).any():
        raise ValueError('First Touch symbol/side mismatch')
    return merged


def report(labels,merged):
    known=merged[merged._merge.eq('both')]
    result={'n_events':len(labels),
        'n_first_touch_matched':int(merged._merge.eq('both').sum()),
        'n_first_touch_missing':int(merged._merge.ne('both').sum()),
        'n_unknown_history':int(labels.group_pre.eq(UNKNOWN).sum()),
        'groups_all_pre':{g:int(labels.pattern_pre.eq(g).sum()) for g in GROUPS},
        'groups_missing_first_touch':{g:int(((merged.pattern_pre==g)&(merged._merge!='both')).sum()) for g in GROUPS},
        'chosen_role_pre':labels[labels.group_pre==NEAR].chosen_role_pre.value_counts().to_dict(),
        'side_pre_counts':{g:labels[labels.pattern_pre==g].side.value_counts().to_dict() for g in GROUPS},
        'three_or_more_prior_visits_by_group':{g:int(((labels.pattern_pre==g)&(labels.pattern_prior_visits>=3)).sum()) for g in (PAT_LONG,PAT_SHORT)},
        'any_compatible_count':int(labels.pattern_pre.isin([PAT_LONG,PAT_SHORT]).sum()),
        'by_horizon':{},'by_side':{}, 'by_inside_for_compatible':{},'by_prior_visits':{}}
    for h in (180,360):
        result['by_horizon'][str(h)]={g:summarize(known[known.pattern_pre==g],h) for g in GROUPS if g!=G_UNKNOWN}
        result['by_side'][str(h)]={g:{side:summarize(known[(known.pattern_pre==g)&(known.side_pre==side)],h)
            for side in ('LONG','SHORT')} for g in (PAT_LONG,PAT_SHORT,PAT_OTHER,G_FAR)}
        selected=known[known.pattern_pre.isin([PAT_LONG,PAT_SHORT])]
        result['by_inside_for_compatible'][str(h)]={
            str(inside):summarize(selected[selected.pattern_inside==inside],h) for inside in (True,False)}
        result['by_prior_visits'][str(h)]={
            str(bucket):summarize(selected[(selected.pattern_prior_visits>=bucket)&
                              (selected.pattern_prior_visits<bucket+1 if bucket==2 else True)],h)
            for bucket in (2,3)}
    return result


def run(run_folder,ft_path,output_folder):
    events,zones,audit=read_pre(run_folder)
    labels=classify_pre(events,zones)
    merged=join_first_touch(labels,ft_path)
    result=report(labels,merged)
    output_folder=Path(output_folder)
    output_folder.mkdir(parents=True,exist_ok=True)
    # Output only PRE fields; result columns are never mixed into event-level labels.
    cols=['event_key','symbol','side','date_utc','retest_ts','observation_type','group_pre',
          'chosen_role_pre','chosen_kind','chosen_timeframe','chosen_visits','chosen_age_bars',
          'pattern_pre','pattern_zone_id','pattern_tf','pattern_age_hours','pattern_prior_visits','pattern_distance_atr','pattern_inside']
    require_cols(labels,cols,'V2 PRE labels')
    prefile=output_folder/'pattern_labels_pre.csv'
    labels[cols].to_csv(prefile,index=False)
    result['provenance']={
        'input_folder':str(run_folder),'pre_audit_rule':audit['audit_rule'],
        'pre_event_labels_sha256':audit['outputs_sha256']['event_labels.csv'],
        'pre_candidate_levels_sha256':audit['outputs_sha256']['candidate_levels.csv'],
        'output_labels_sha256':digest(prefile),'scenario':'LIVE_OBSERVED|OBSERVED_OPEN|TP1_SL1',
        'note':'Historical exploratory sample; no placebo, no holdout, no P&L. 20h cutoff is exploratory. Level unbroken status not verified.'}
    (output_folder/'pattern_report.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,default=str),encoding='utf-8')
    print('=== PRE CLASSIFICATION (NO OUTCOMES) ===')
    print(json.dumps(result['groups_all_pre'],ensure_ascii=False))
    print('BY_SIDE:',json.dumps(result['side_pre_counts'],ensure_ascii=False))
    print('THREE_OR_MORE_PREVIOUS_VISITS:',json.dumps(result['three_or_more_prior_visits_by_group'],ensure_ascii=False))
    print('=== JOIN COVERAGE ===')
    print(json.dumps({k:result[k] for k in ('n_events','n_first_touch_matched','n_first_touch_missing','n_unknown_history','groups_missing_first_touch')},ensure_ascii=False))
    for h in ('180','360'):
        print(f'=== {h} MIN TP1 / SL1 / OBSERVED_OPEN ===')
        for g,row in result['by_horizon'][h].items():
            print(json.dumps({'group':g,**row},ensure_ascii=False))
        print('BY_SIDE (20H+ compatible vs comparable groups):')
        for g in (PAT_LONG,PAT_SHORT,PAT_OTHER,G_FAR):
            for side,item in result['by_side'][h][g].items():
                print(json.dumps({'group':g,'side':side,**item},ensure_ascii=False))
        print('COMPATIBLE INSIDE vs OUTSIDE:')
        for z,item in result['by_inside_for_compatible'][h].items():
            print(json.dumps({'inside_zone':z,**item},ensure_ascii=False))
    print(f'REPORT_PATH {output_folder/"pattern_report.json"}')
    print('CAUTION: exploratory retrospective analysis; 20h is not a validated trading threshold; no sustained-level check yet.')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    parser.add_argument('--run-folder',type=Path)
    parser.add_argument('--first-touch',type=Path)
    parser.add_argument('--output',type=Path)
    a=parser.parse_args()
    root=a.root.resolve()
    folder=a.run_folder or root/'reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe'
    ft=a.first_touch or root/'reports/reaction_lab/reaction_first_touch_lab_v1/first_touch_scenarios.csv'
    out=a.output or folder/'pattern_roles_v2'
    run(folder,ft,out)
if __name__=='__main__':main()
