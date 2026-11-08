#!/usr/bin/env python3
"""HLL Structural Zones V2 — append-only causal subzone links, offline research.

Consumes a verified, immutable HLL Hierarchical Levels V1 run. Never mutates
HLL Lifecycle V2.1, level_id, original events, orders, or LIVE state. Pair links
require independent same-side tests at the same *closed* 30m candle. Joins are
monotonic, complete-link and non-transitive (no chains via a weak bridge).
Grades are inherited descriptive V1 scores at the requested as-of cutoff;
not a predictor. Flip/acceptance are deliberately NOT inferred from origin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from hll_hierarchical_levels_v1 import TF, ROOT, sha, score_asof

VERSION='hll-structural-zones-v2.0-research'
RUNS=Path('reports/horizontal_level_structural_zones_v2/runs')
TESTS=('TEST_FROM_ABOVE','TEST_FROM_BELOW')
RESPONSES=('DEFENSE_CONFIRMED','LOSS_CONFIRMED')
REQUIRED=('manifest.json','formations.csv','events.csv','ends.csv','candles_30m.csv','snapshot_asof.csv')
CONFIG=dict(min_shared_tests=3,min_separation_bars=4,min_band_overlap=0.20,
            max_center_pct=0.28,max_center_atr=1.1,max_cluster_span_pct=0.45,
            max_cluster_span_atr=1.7)


def as_utc(ms):
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def tag_path(root,tag):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{2,80}',tag):
        raise ValueError('Nombre de corrida inválido')
    return Path(root)/RUNS/tag


def verified_source(folder:Path):
    folder=Path(folder).resolve()
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if m.get('version')!='hll-hierarchical-levels-v1.0-exploratory':
        raise ValueError('No es corrida HLL Hierarchy V1')
    for filename in REQUIRED[1:]:
        expected=m.get('files_sha256',{}).get(filename)
        if not expected or not (folder/filename).is_file() or sha(folder/filename)!=expected:
            raise ValueError('Hash/cobertura de HLL V1 inválido: '+filename)
    f=pd.read_csv(folder/'formations.csv',low_memory=False)
    e=pd.read_csv(folder/'events.csv',low_memory=False)
    x=pd.read_csv(folder/'ends.csv',low_memory=False)
    b=pd.read_csv(folder/'candles_30m.csv',low_memory=False,float_precision='round_trip')
    if len(f)!=m['levels_detected'] or len(e)!=m['events_detected'] or len(b)!=m['bars_30m']:
        raise ValueError('Cardinalidades HLL V1 no coinciden')
    time=b.timestamp.to_numpy(dtype='int64')
    if len(time)<260 or (np.diff(time)!=TF).any() or (time%TF).any() or int(time[-1])+TF!=int(m['cutoff_ms']):
        raise ValueError('Velas 30m incompletas, huecos o final inválido')
    if f.level_id.duplicated().any() or (pd.to_numeric(f.formation_closed_ts)<time[0]).any():
        raise ValueError('Formaciones duplicadas o anteriores al histórico')
    ids=set(f.level_id.astype(str))
    if not set(e.level_id.astype(str)).issubset(ids):
        raise ValueError('Evento refiere a línea inexistente')
    for col in ('event_type','observed_closed_ts','level_id'):
        if col not in e:raise ValueError('Faltan campos HLL V1 de eventos')
    evtime=pd.to_numeric(e.observed_closed_ts,errors='raise').to_numpy(dtype='int64')
    if ((evtime%TF)!=0).any() or (evtime>int(m['cutoff_ms'])).any():
        raise ValueError('Eventos HLL fuera de barras cerradas')
    return m,f,e,x,b


def band_pair(a,b,config=CONFIG):
    a0,a1=float(a.zone_low),float(a.zone_high)
    b0,b1=float(b.zone_low),float(b.zone_high)
    wa=a1-a0;wb=b1-b0
    if min(wa,wb)<=0:return False
    ov=max(0.0,min(a1,b1)-max(a0,b0))/min(wa,wb)
    midpoint=(float(a.center)+float(b.center))/2
    dist=abs(float(a.center)-float(b.center))
    atr=(float(a.atr_at_formation)+float(b.atr_at_formation))/2
    if midpoint<=0 or atr<=0:return False
    return (ov>=config['min_band_overlap'] and
            dist/midpoint*100<=config['max_center_pct'] and
            dist/atr<=config['max_center_atr'])


def independent_times(times:list[int],gap_ms:int)->list[int]:
    kept=[]
    for t in sorted(set(map(int,times))):
        if not kept or t-kept[-1]>=gap_ms:kept.append(t)
    return kept


def pair_links(formations:pd.DataFrame,events:pd.DataFrame,config=CONFIG)->pd.DataFrame:
    """First *causal* timestamp when two bands share >=3 independent tests.

    Matching only events with identical close timestamp and same test direction
    makes the first qualified time prefix-stable by construction.
    """
    test=events.loc[events.event_type.isin(TESTS),
                    ['level_id','event_type','observed_closed_ts']].copy()
    moments=defaultdict(set)
    for r in test.itertuples(index=False):
        moments[str(r.level_id)].add((int(r.observed_closed_ts),str(r.event_type)))
    ordered=formations.sort_values(['formation_closed_ts','level_id'],kind='stable').to_dict('records')
    links=[]
    for i,a in enumerate(ordered):
        for b in ordered[i+1:]:
            if not band_pair(pd.Series(a),pd.Series(b),config):continue
            aid=str(a['level_id']);bid=str(b['level_id'])
            events_match=sorted(moments[aid] & moments[bid])
            qualifying=independent_times([ts for ts,_ in events_match],TF*config['min_separation_bars'])
            if len(qualifying)<config['min_shared_tests']:continue
            formed=max(int(a['formation_closed_ts']),int(b['formation_closed_ts']))
            valid=[t for t in qualifying if t>=formed]
            if len(valid)<config['min_shared_tests']:continue
            aa,bb=sorted([aid,bid])
            links.append(dict(level_a=aa,level_b=bb,
               linked_at=int(valid[config['min_shared_tests']-1]),
               shared_independent_at_link=config['min_shared_tests']))
    fields=['level_a','level_b','linked_at','shared_independent_at_link']
    return pd.DataFrame(links,columns=fields).sort_values(['linked_at','level_a','level_b'],kind='stable').reset_index(drop=True)


def zone_id(members:list[str],symbol:str)->str:
    seed=json.dumps([symbol,*sorted(members)],separators=(',',':'))
    return 'SZ2_'+hashlib.sha256(seed.encode()).hexdigest()[:20]


def reconstruct_clusters(formations:pd.DataFrame,links:pd.DataFrame,cutoff:int,config=CONFIG):
    """Replay all eligible links chronologically; groups never split or bridge.

    Returns group members and immutable lineage births; each merge creates a
    new identifier and records the parents, but never rewrites past identity.
    """
    forms=formations.loc[formations.formation_closed_ts.astype('int64')<=cutoff].copy()
    forms=forms.sort_values(['formation_closed_ts','level_id'],kind='stable')
    byid={str(r.level_id):r for r in forms.itertuples(index=False)}
    clusters={s:{s} for s in byid}
    who={s:s for s in byid}
    parents=[]
    usable=links.loc[links.linked_at.astype('int64')<=cutoff].sort_values(
          ['linked_at','level_a','level_b'],kind='stable') if not links.empty else links
    qualified=set()
    symbol=str(forms.symbol.iloc[0]) if not forms.empty else 'UNKNOWN'
    for r in usable.itertuples(index=False):
        a,b=str(r.level_a),str(r.level_b)
        if a not in who or b not in who:continue
        qualified.add(tuple(sorted((a,b))))
        ca,cb=who[a],who[b]
        if ca==cb:continue
        left,right=clusters[ca],clusters[cb]
        mix=left|right
        # Complete-link (each cross-pair independently confirmed before merge).
        if any(tuple(sorted((x,y))) not in qualified for x in left for y in right):continue
        centers=[float(byid[x].center) for x in mix]
        low,high=min(centers),max(centers)
        atrs=[float(byid[x].atr_at_formation) for x in mix]
        span=high-low
        if (low<=0 or min(atrs)<=0 or
            span/((low+high)/2)*100>config['max_cluster_span_pct'] or
            span/(sum(atrs)/len(atrs))>config['max_cluster_span_atr']):
            continue
        # Record creation of new component. The old group identities survive
        # in immutable lineage, not relabeled retroactively.
        oldids=[zone_id(sorted(group),symbol) for group in (left,right)]
        newmembers=tuple(sorted(mix))
        new_id=zone_id(list(newmembers),symbol)
        parents.append(dict(zone_id=new_id,joined_at=int(r.linked_at),
               parent_zone_ids=json.dumps(sorted(oldids)),
               members=json.dumps(list(newmembers)),link_a=a,link_b=b))
        del clusters[ca];del clusters[cb]
        clusters[newmembers[0]]=set(mix)
        for name in mix:who[name]=newmembers[0]
    return [tuple(sorted(g)) for g in clusters.values()],parents


def compact_episodes(events,ids,cutoff,start=None,gap_bars=4):
    """Count conservatively deduplicated CLOSED 30m interaction episodes.

    Same-direction tests by multiple lines on one bar count once. Adjacent
    bars within gap are one interaction; different sides remain distinct.
    Returns count dictionary; never sums per-level counts as zone evidence.
    """
    if not ids or events.empty:return {k:0 for k in (*TESTS,*RESPONSES)}
    e=events.loc[(events.level_id.astype(str).isin(ids))&
                 (events.observed_closed_ts.astype('int64')<=cutoff)]
    if start is not None:e=e.loc[e.observed_closed_ts.astype('int64')>=start]
    counts={k:0 for k in (*TESTS,*RESPONSES)}
    for kind in counts:
        ts=e.loc[e.event_type.eq(kind),'observed_closed_ts'].astype('int64').tolist()
        counts[kind]=len(independent_times(ts,TF*gap_bars))
    return counts


def snapshot_zones(formations,events,ends,bars,links,cutoff:int,config=CONFIG):
    scores=score_asof(formations,events,ends,bars,cutoff)
    groups,births=reconstruct_clusters(formations,links,cutoff,config)
    byid={str(r.level_id):r for r in scores.itertuples(index=False)}
    frm={str(r.level_id):r for r in formations.itertuples(index=False)}
    born={r['zone_id']:r['joined_at'] for r in births}
    available=bars.loc[bars.timestamp.astype('int64')+TF<=cutoff]
    last_close=float(available.iloc[-1].close)
    out=[]
    for group in groups:
        s=[byid[x] for x in group]
        leader=sorted(s,key=lambda r:(-int(r.importance_score),int(r.formation_closed_ts),str(r.level_id)))[0]
        zid=zone_id(list(group),str(leader.symbol))
        formed=max(int(frm[x].formation_closed_ts) for x in group)
        created=born.get(zid,formed)
        lo=min(float(r.zone_low) for r in s)
        hi=max(float(r.zone_high) for r in s)
        if any(r.state_at_cutoff!='ACTIVE' for r in s):
            lifecycle='PARTIAL_EXPIRED' if any(r.state_at_cutoff=='ACTIVE' for r in s) else 'EXPIRED'
        else:lifecycle='ACTIVE_TTL'
        if last_close>hi:relation='ABOVE'
        elif last_close<lo:relation='BELOW'
        else:relation='INSIDE'
        # Strong/weak is descriptive only; a HIGH origin is NOT current resistance.
        counts=compact_episodes(events,set(group),cutoff,created,
                       gap_bars=config['min_separation_bars'])
        before=compact_episodes(events,set(group),cutoff,None,
                       gap_bars=config['min_separation_bars'])
        group_events=events.loc[(events.level_id.astype(str).isin(group)) &
            events.event_type.isin(RESPONSES) &
            (events.observed_closed_ts.astype('int64')<=cutoff) &
            (events.observed_closed_ts.astype('int64')>=created)]
        last_response='UNKNOWN'
        if not group_events.empty:
            recent=group_events.sort_values(['observed_closed_ts','event_type'],kind='stable').iloc[-1]
            last_response='DEFENDED' if recent.event_type=='DEFENSE_CONFIRMED' else 'LOST'
        out.append(dict(zone_id=zid,level_ids=json.dumps(list(group)),
            members=len(group),formed_at=int(formed),created_at=int(created),
            center=float(leader.center),low=lo,high=hi,origin_kind=(
             s[0].origin if len(set(r.origin for r in s))==1 else 'MIXED'),
            tier=str(leader.tier),importance_score=int(leader.importance_score),
            score_method='MAX_MEMBER_ASOF_NOT_ZONE_CALIBRATED',
            leader_level_id=str(leader.level_id),
            lifecycle=lifecycle,price_relation=relation,last_response=last_response,
            confirmed_flip='NOT_ASSESSED',acceptance='NOT_ASSESSED',
            tests_independent_pre=before['TEST_FROM_ABOVE']+before['TEST_FROM_BELOW'],
            tests_since_zone_birth=counts['TEST_FROM_ABOVE']+counts['TEST_FROM_BELOW'],
            defenses_since_birth=counts['DEFENSE_CONFIRMED'],
            losses_since_birth=counts['LOSS_CONFIRMED'],
            age_days=round((cutoff-created)/86_400_000,2)))
    return pd.DataFrame(out).sort_values(
        ['importance_score','center'],ascending=[False,False],kind='stable').reset_index(drop=True) if out else pd.DataFrame(),births



def zone_score_history(formations,events,ends,bars,zone_members,created_at,cutoff):
    """Daily V1 descriptive importance of members, using strictly closed bars.

    A zone is eligible only once it exists; no final-grade backpainting.
    Only the current zone's immutable members are examined (historical merges
    have distinct zone IDs and can be inspected separately via their lineage).
    """
    wanted=set(map(str,zone_members))
    closed=(bars.timestamp.astype('int64')+TF)
    slices=bars.loc[closed.ge(int(created_at)) & closed.le(int(cutoff))].copy()
    if slices.empty:return pd.DataFrame(columns=['cutoff_ms','score','tier','leader'])
    ticks=slices.groupby((slices.timestamp.astype('int64')//86_400_000),sort=True).tail(1)
    stamps=sorted(set(ticks.timestamp.astype('int64').add(TF).tolist()+[int(created_at),int(cutoff)]))
    rows=[]
    for t in stamps:
        snap=score_asof(formations,events,ends,bars,int(t))
        members=snap.loc[snap.level_id.astype(str).isin(wanted)].sort_values(
            ['importance_score','formation_closed_ts','level_id'],
            ascending=[False,True,True],kind='stable')
        if len(members)!=len(wanted):
            raise ValueError('Composición histórica imposible: falta línea al nacimiento de zona')
        best=members.iloc[0]
        rows.append(dict(cutoff_ms=int(t),score=int(best.importance_score),
                         tier=str(best.tier),leader=str(best.level_id)))
    return pd.DataFrame(rows)


def build(root:Path,v1_dir:Path,tag:str,*,run=False,min_free_gb=10):
    root=Path(root)
    inp=Path(v1_dir)
    inp=inp if inp.is_absolute() else root/inp
    m,f,e,x,b=verified_source(inp)
    tagdir=tag_path(root,tag)
    cutoff=int(m['cutoff_ms'])
    links=pair_links(f,e)
    scores,births=snapshot_zones(f,e,x,b,links,cutoff)
    plan=dict(version=VERSION,mode='EXPORT_OFFLINE' if run else 'PLAN_READ_ONLY',
          v1_run=str(inp.resolve()),v1_manifest_sha256=sha(inp/'manifest.json'),
          v1_source_sha256=m['source_sha256'],symbol=m['symbol'],cutoff_ms=cutoff,
          lines_original=len(f),links_confirmed=len(links),
          subzones_at_cutoff=len(scores),multi_line_zones=int((scores.members>1).sum()),
          lineage_merges=len(births),coverage_days=round(len(b)/48,2),
          output_dir=str(tagdir),configuration=CONFIG)
    if not run:return plan
    if shutil.disk_usage(root).free/1024**3<min_free_gb:
        raise RuntimeError('DISK_GUARD por debajo del mínimo')
    if tagdir.exists():raise FileExistsError('Corrida V2 ya existe, no sobrescribir')
    tagdir.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tagdir.parent,prefix='.hll-structural-v2-') as tmp:
        t=Path(tmp)
        links.to_csv(t/'pair_links.csv',index=False)
        scores.to_csv(t/'zones_final.csv',index=False)
        pd.DataFrame(births,columns=['zone_id','joined_at','parent_zone_ids','members','link_a','link_b']).to_csv(t/'zone_lineage.csv',index=False)
        plan['files_sha256']={name:sha(t/name) for name in ('pair_links.csv','zones_final.csv','zone_lineage.csv')}
        (t/'manifest.json').write_text(json.dumps(plan,indent=2,ensure_ascii=False),encoding='utf-8')
        t.rename(tagdir)
    return plan


def load_v2(v2_dir:Path,expected_v1_manifest:Path):
    v2_dir=Path(v2_dir)
    manifest=json.loads((v2_dir/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version')!=VERSION or manifest['v1_manifest_sha256']!=sha(Path(expected_v1_manifest)):
        raise ValueError('V2 no corresponde a esta corrida V1 o manifiesto inválido')
    for name,digest in manifest.get('files_sha256',{}).items():
        if sha(v2_dir/name)!=digest:raise ValueError('V2 alterado: '+name)
    links=pd.read_csv(v2_dir/'pair_links.csv')
    return manifest,links


def main(argv=None):
    p=argparse.ArgumentParser(description='HLL Structural Zones V2 offline, sin LIVE')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true')
    g.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--v1-run-dir',type=Path,required=True)
    p.add_argument('--run-tag',default='btc_oct_nov_structural_v2')
    p.add_argument('--min-free-gb',type=float,default=10)
    a=p.parse_args(argv)
    if a.min_free_gb<0:raise ValueError('Mínimo libre no válido')
    r=build(a.root,a.v1_run_dir,a.run_tag,run=a.run,min_free_gb=a.min_free_gb)
    print('HLL_STRUCTURAL_ZONES_'+('RESULT' if a.run else 'PLAN'),json.dumps(r,ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
