#!/usr/bin/env python3
"""HLL Structural Behavior V2.2-A: frozen as-of, read-only physical test episodes.

Does NOT infer acceptance, flip, profitable trades or OHLC-based defenses.
Uses Lifecycle's original test_id and confirmed resolution, then deduplicates
coincident member-line tests into physical zone episodes. Historical zone
identity is valid only from the current zone's confirmed creation timestamp.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from hll_hierarchical_levels_v1 import TF, sha
from hll_structural_zones_v2 import verified_source, load_v2, snapshot_zones

VERSION = 'hll-structural-behavior-v2.2a-offline'
TESTS = {'TEST_FROM_ABOVE', 'TEST_FROM_BELOW'}
OUTCOMES = {'DEFENSE_CONFIRMED', 'LOSS_CONFIRMED', 'TEST_TIMEOUT'}
EP_COLS = ['episode_id','zone_id','zone_created_at','side','first_test_closed_ts',
           'last_test_closed_ts','resolved_at','state','test_count_unique','member_test_count',
           'member_count','member_ids','test_ids','outcomes_defended','outcomes_lost',
           'outcomes_timeout','outcomes_pending','ambiguous_mixed_resolution']
TR_COLS = ['episode_id','zone_id','level_id','test_id','event_id','event_type',
           'observed_closed_ts','test_closed_ts','side','transition_role']
SUMMARY_COLS = ['zone_id','created_at','episodes','resolved','pending','mixed',
                'defended_only','lost_only','timeout_only','other_resolved',
                'tests_unique','member_tests']


def _hash(*items):
    return hashlib.sha256(json.dumps(items,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:20]


def reconstruct_episode_tables(zones:pd.DataFrame, events:pd.DataFrame,
                               cutoff:int, gap_bars:int=4):
    """One anchored time cluster per side, preserving conflicting member outcomes.

    As-of filtering is on observed CLOSED timestamps. A response is matched by
    original test_id; future responses are not used even if in frozen input.
    No retroactive attribution to a zone before its created_at.
    """
    if cutoff % TF or not 1 <= gap_bars <= 48:
        raise ValueError('Invalid closed cutoff or separation')
    fields = {'zone_id','created_at','level_ids','members'}
    if not fields.issubset(zones.columns):
        raise ValueError('Missing zone identity columns')
    required = {'level_id','event_type','observed_closed_ts','test_id','event_id'}
    if not required.issubset(events.columns):
        raise ValueError('Missing Lifecycle event fields')
    if zones.zone_id.astype(str).duplicated().any():
        raise ValueError('Duplicate zone identity')
    e=events.copy()
    e['observed_closed_ts']=pd.to_numeric(e.observed_closed_ts,errors='raise').astype('int64')
    if (e.observed_closed_ts % TF).any():
        raise ValueError('Unclosed event timestamp')
    e=e.loc[e.observed_closed_ts.le(cutoff) & e.event_type.isin(TESTS | OUTCOMES)].copy()
    eps=[]; transitions=[]; summaries=[]
    for zone in zones.itertuples(index=False):
        zid=str(zone.zone_id)
        members=json.loads(zone.level_ids) if isinstance(zone.level_ids,str) else zone.level_ids
        if not isinstance(members,list) or not members or len(set(members))!=len(members) or len(members)!=int(zone.members):
            raise ValueError('Invalid member identities')
        birth=int(zone.created_at)
        if birth%TF or birth>cutoff: raise ValueError('Invalid zone birth')
        ze=e.loc[e.level_id.astype(str).isin(members) & e.observed_closed_ts.ge(birth)].copy()
        tests=ze.loc[ze.event_type.isin(TESTS)].sort_values(['observed_closed_ts','level_id','event_id'],kind='stable')
        # Restrict to tests actually generated AFTER the current zone existed.
        outcomes=ze.loc[ze.event_type.isin(OUTCOMES)].copy()
        by_test={}
        for r in outcomes.itertuples(index=False):
            tid=str(r.test_id)
            if not tid or tid=='nan':continue
            by_test.setdefault((str(r.level_id),tid),[]).append(r)
        # A test ID must resolve at most once within a member line.
        for group in by_test.values():
            if len(group)>1:raise ValueError('Duplicate test resolution')
        grouped=[]
        for side in ('ABOVE','BELOW'):
            side_tests=tests.loc[tests.event_type.eq('TEST_FROM_'+side)]
            clusters=[]
            for r in side_tests.itertuples(index=False):
                ts=int(r.observed_closed_ts)
                if not clusters or ts-clusters[-1][0] >= gap_bars*TF:
                    clusters.append((ts,[r]))
                else:clusters[-1][1].append(r)
            grouped.extend((side,anchor,group) for anchor,group in clusters)
        grouped.sort(key=lambda x:(x[1],x[0]))
        zone_records=[]
        for side,anchor,group in grouped:
            eid='BE22_'+_hash(zid,side,anchor)
            unique={}
            for r in group:
                key=(str(r.level_id),str(r.test_id))
                if key in unique:raise ValueError('Duplicate original test')
                if not key[1] or key[1]=='nan':raise ValueError('Test missing test_id')
                unique[key]=r
            counts={k:0 for k in OUTCOMES};pending=0
            resolved_times=[]
            for (level_id,tid),r in unique.items():
                transitions.append(dict(episode_id=eid,zone_id=zid,level_id=level_id,
                    test_id=tid,event_id=str(r.event_id),event_type=str(r.event_type),
                    observed_closed_ts=int(r.observed_closed_ts),test_closed_ts=int(r.observed_closed_ts),
                    side=side,transition_role='TEST'))
                match=by_test.get((level_id,tid),[])
                if not match:pending+=1
                for out in match:
                    if int(out.observed_closed_ts)<=int(r.observed_closed_ts):
                        raise ValueError('Response not later than test')
                    typ=str(out.event_type);counts[typ]+=1
                    resolved_times.append(int(out.observed_closed_ts))
                    transitions.append(dict(episode_id=eid,zone_id=zid,level_id=level_id,
                        test_id=tid,event_id=str(out.event_id),event_type=typ,
                        observed_closed_ts=int(out.observed_closed_ts),test_closed_ts=int(r.observed_closed_ts),
                        side=side,transition_role='RESOLUTION'))
            kinds=sum(counts[k]>0 for k in OUTCOMES)
            mixed=kinds>1
            state=('PENDING' if pending==len(unique) else 'PARTIAL_PENDING' if pending else
                   'MIXED_RESOLUTION' if mixed else
                   'DEFENDED' if counts['DEFENSE_CONFIRMED'] else
                   'LOST' if counts['LOSS_CONFIRMED'] else 'TIMED_OUT')
            ep=dict(episode_id=eid,zone_id=zid,zone_created_at=birth,side=side,
              first_test_closed_ts=anchor,last_test_closed_ts=max(int(r.observed_closed_ts) for r in group),
              resolved_at=max(resolved_times) if resolved_times else None,
              state=state,test_count_unique=len(set(int(r.observed_closed_ts) for r in group)),
              member_test_count=len(group),member_count=len(set(str(r.level_id) for r in group)),
              member_ids=json.dumps(sorted(set(str(r.level_id) for r in group))),
              test_ids=json.dumps(sorted(str(r.test_id) for r in group)),
              outcomes_defended=counts['DEFENSE_CONFIRMED'],outcomes_lost=counts['LOSS_CONFIRMED'],
              outcomes_timeout=counts['TEST_TIMEOUT'],outcomes_pending=pending,
              ambiguous_mixed_resolution=mixed)
            eps.append(ep);zone_records.append(ep)
        summaries.append(dict(zone_id=zid,created_at=birth,episodes=len(zone_records),
            resolved=sum(r['outcomes_pending']==0 for r in zone_records),
            pending=sum(r['outcomes_pending']>0 for r in zone_records),
            mixed=sum(r['ambiguous_mixed_resolution'] for r in zone_records),
            defended_only=sum(r['state']=='DEFENDED' for r in zone_records),
            lost_only=sum(r['state']=='LOST' for r in zone_records),
            timeout_only=sum(r['state']=='TIMED_OUT' for r in zone_records),
            other_resolved=sum(r['state']=='MIXED_RESOLUTION' for r in zone_records),
            tests_unique=sum(r['test_count_unique'] for r in zone_records),
            member_tests=sum(r['member_test_count'] for r in zone_records)))
    return (pd.DataFrame(eps,columns=EP_COLS),
            pd.DataFrame(transitions,columns=TR_COLS),
            pd.DataFrame(summaries,columns=SUMMARY_COLS))


def build(root:Path,v1_dir:Path,v2_dir:Path,tag:str,*,run=False,min_free_gb=10):
    root=Path(root).resolve()
    v1_dir=Path(v1_dir);v2_dir=Path(v2_dir)
    if not v1_dir.is_absolute():v1_dir=root/v1_dir
    if not v2_dir.is_absolute():v2_dir=root/v2_dir
    manifest,f,e,ends,bars=verified_source(v1_dir)
    v2_manifest,links=load_v2(v2_dir,v1_dir/'manifest.json')
    cutoff=int(manifest['cutoff_ms'])
    zones,_=snapshot_zones(f,e,ends,bars,links,cutoff)
    ep,trans,summary=reconstruct_episode_tables(zones,e,cutoff)
    if not tag or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in tag) or len(tag)>80:
        raise ValueError('Invalid run tag')
    out=root/'reports/horizontal_level_structural_behavior_v22/runs'/tag
    plan=dict(version=VERSION,mode='RUN_OFFLINE' if run else 'PLAN_READ_ONLY',
        symbol=str(manifest['symbol']),cutoff_ms=cutoff,v1_dir=str(v1_dir.resolve()),
        v2_dir=str(v2_dir.resolve()),v1_manifest_sha256=sha(v1_dir/'manifest.json'),
        v2_manifest_sha256=sha(v2_dir/'manifest.json'),zones=len(zones),
        episodes=len(ep),transitions=len(trans),pending=int((ep.outcomes_pending>0).sum()) if len(ep) else 0,
        output_dir=str(out),scope='CURRENT_CUTOFF_ZONE_IDENTITIES_ONLY',
        limitations=['No acceptance or flip inference','No OHLC forward performance',
                     'No parent-zone historical episodes assigned to merged child'])
    if not run:return plan
    if shutil.disk_usage(root).free/1024**3<min_free_gb:raise RuntimeError('DISK_GUARD')
    if out.exists():raise FileExistsError(str(out))
    out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent,prefix='.behavior-v22a-') as tmp:
        t=Path(tmp)
        for filename,df in [('episodes.csv',ep),('episode_transitions.csv',trans),('behavior_summary.csv',summary)]:
            df.to_csv(t/filename,index=False)
        plan['files_sha256']={name:sha(t/name) for name in
             ('episodes.csv','episode_transitions.csv','behavior_summary.csv')}
        (t/'manifest.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        t.rename(out)
    return plan


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.')
    p.add_argument('--v1-run',required=True)
    p.add_argument('--v2-run',required=True)
    p.add_argument('--tag',required=True)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--min-free-gb',type=float,default=10)
    a=p.parse_args(argv)
    print('HLL_BEHAVIOR_V22A',json.dumps(build(Path(a.project),Path(a.v1_run),
       Path(a.v2_run),a.tag,run=a.run,min_free_gb=a.min_free_gb),ensure_ascii=False))

if __name__=='__main__':main()
