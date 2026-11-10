#!/usr/bin/env python3
"""Multi-Swing V2.5A: causal, NON-DESTRUCTIVE relation audit (BTC 60D).

A: strict physical duplicate (same confirmed physical members).
B: spatial neighbors / contemporary distinct structures, including opposite sides.
C: possible temporal successor after an evidence-quiet interval; NOT a reactivation.

Compares the older region at the *younger region's birth timestamp*; neither
future geometry nor final physical membership can participate. Historical IDs
and parent frozen data remain unchanged. No scoring, LIVE, HLL or network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from btc_multiswing_60d_explorer_v1 import DAY, M, asof_rows, digest, load_snapshot
from btc_multiswing_structural_confluence_v2 import construct_regions, _unique_confirmed
from btc_multiswing_information_universe_v24 import birth_stage_universe

VERSION = 'btc-multiswing-canonicalization-v2.5a'
DEFAULT_PARENT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_information_universe_v24a')
DEFAULT_OUTPUT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_canonicalization_v25a')

PAIR_COLUMNS = [
    'older_region_id', 'younger_region_id', 'relation', 'subtype',
    'known_from_ts', 'known_from_utc', 'older_birth_ts', 'younger_birth_ts',
    'older_side', 'younger_side', 'older_band_low_asof', 'older_band_high_asof',
    'younger_band_low_birth', 'younger_band_high_birth', 'center_distance_pct',
    'intersection_width', 'iou', 'band_intersects', 'older_last_confirm_asof',
    'quiet_minutes_at_younger_birth', 'old_extrema_asof', 'new_extrema_at_birth',
    'common_physical_members', 'common_physical_ids', 'physical_jaccard_asof',
    'visual_redundancy_candidate', 'decision',
]
CANON_COLUMNS = [
    'region_id', 'side', 'birth_known_ts', 'band_low_birth', 'band_high_birth',
    'extrema_at_birth', 'canonical_region_id', 'strict_A_alias',
    'visual_family_anchor_REVIEW_ONLY', 'visual_family_assignment_ts',
    'possible_predecessors_C', 'B_same_side_pairs', 'B_opposite_side_pairs',
    'A_duplicate_pairs', 'C_successor_pairs', 'original_region_preserved',
]


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _as_positive_number(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'INVALID_{name}')
    return number


def _metrics(old: dict, new: dict) -> tuple[float, float, float, bool]:
    al, ah = old['low'], old['high']
    bl, bh = new['low'], new['high']
    ac, bc = (al+ah)/2, (bl+bh)/2
    dist = abs(ac-bc) / ((ac+bc)/2)*100
    overlap = max(0., min(ah,bh)-max(al,bl))
    union = max(ah,bh)-min(al,bl)
    iou = overlap/union if union > 0 else float(al == bl)
    intersects = max(al,bl) <= min(ah,bh)
    return dist, overlap, iou, intersects


def audit_relations(regions: pd.DataFrame, stages: pd.DataFrame,
                    first_confirmed_by_physical: dict[str, int], cutoff: int, *,
                    near_pct: float = 0.2, strict_iou: float = 0.6,
                    quiet_days: int = 7, successor_max_days: int = 14,
                    physical_jaccard_min: float = 0.8):
    """Return (pairwise PRE relations, 1:1 canonical review mapping, counts).

    The `regions.members` string is retrospective; only IDs with first as-of
    confirmation <= each pair's `known_from_ts` may enter calculations.
    Strict A requires >=2 shared physical IDs and substantial membership/price
    equivalence. B and C NEVER cause destructive merging. No transitive chaining:
    any proposed display family member must directly match its oldest anchor.
    """
    if not (0 < near_pct <= 2 and 0 < strict_iou <= 1 and
            1 <= quiet_days < successor_max_days <= 60 and
            0 < physical_jaccard_min <= 1):
        raise ValueError('INVALID_V25_SETTINGS')
    cutoff = int(cutoff)
    births = birth_stage_universe(regions, stages, cutoff)
    if births.empty:
        return pd.DataFrame(columns=PAIR_COLUMNS), pd.DataFrame(columns=CANON_COLUMNS), {
            'regions':0,'relations':0,'A':0,'B':0,'C':0,
            'strict_aliases':0,'visual_review_groups':0}
    if set(regions.region_id) != set(births.region_id) or regions.region_id.duplicated().any():
        raise ValueError('V25_UNIVERSE_ID_CONFLICT')
    stage_by_id: dict[str, list[dict]] = {}
    for rid, group in stages.groupby('region_id',sort=False):
        ordered = group.sort_values(['known_from','independent_extrema'], kind='mergesort')
        stage_by_id[str(rid)] = [
            {'ts':int(s.known_from), 'count':int(s.independent_extrema),
             'low':_as_positive_number(s.band_low,'STAGE_LOW'),
             'high':_as_positive_number(s.band_high,'STAGE_HIGH')}
            for s in ordered.itertuples(index=False)
        ]
    raw_members: dict[str, list[str]] = {}
    for r in regions.itertuples(index=False):
        rid = str(r.region_id)
        ids = [x for x in str(r.members).split(',') if x]
        if len(ids) != len(set(ids)) or len(ids) < 2:
            raise ValueError('V25_INVALID_PHYSICAL_MEMBERS')
        if any(pid not in first_confirmed_by_physical for pid in ids):
            raise ValueError('V25_UNKNOWN_PHYSICAL_MEMBER')
        raw_members[rid] = ids
    births = births.sort_values(['birth_known_ts','region_id'],kind='mergesort').reset_index(drop=True)
    records = [r._asdict() for r in births.itertuples(index=False)]

    def snapshot(rid: str, moment: int) -> tuple[dict,set[str]]:
        known = [s for s in stage_by_id[rid] if s['ts'] <= moment]
        if not known:
            raise ValueError('V25_STAGE_NOT_KNOWN_AT_PAIR_TIME')
        stage = known[-1]
        if stage['low'] > stage['high']:
            raise ValueError('V25_INVALID_STAGE_BAND')
        members = {pid for pid in raw_members[rid]
                   if int(first_confirmed_by_physical[pid]) <= moment}
        if len(members) != stage['count']:
            raise ValueError('V25_STAGE_MEMBER_ASOF_CONFLICT')
        return stage, members

    pairs: list[dict] = []
    for new_index, new in enumerate(records):
        newer = str(new['region_id'])
        t = int(new['birth_known_ts'])
        if t > cutoff:
            raise ValueError('V25_FUTURE_REGION_BIRTH')
        new_stage, new_members = snapshot(newer,t)
        # Including all same-minute confirmations is valid as of that minute.
        # Choosing the older ID only breaks ties deterministically.
        for old in records[:new_index]:
            older = str(old['region_id'])
            old_stage, old_members = snapshot(older,t)
            d, intersect_width, iou, intersects = _metrics(old_stage,new_stage)
            if not (d <= near_pct+1e-12 or intersects):
                continue
            shared = old_members & new_members
            same_side = old['side'] == new['side']
            jaccard = len(shared)/len(old_members | new_members)
            quiet = t - int(old_stage['ts'])
            if quiet < 0:
                raise ValueError('V25_NEGATIVE_PRE_QUIET_PERIOD')
            duplicate = (same_side and len(shared) >= 2 and
                         jaccard >= physical_jaccard_min and
                         (iou >= strict_iou or d <= near_pct/2))
            succession = (same_side and not duplicate and
                          quiet >= quiet_days*DAY and
                          quiet <= successor_max_days*DAY)
            strong_b = (same_side and not duplicate and not succession and
                        iou >= strict_iou and d <= near_pct/2)
            if duplicate:
                kind, subtype, decision = 'A','A_PHYSICAL_DUPLICATE','STRICT_ALIAS_CANDIDATE'
            elif succession:
                kind, subtype, decision = 'C','C_POSSIBLE_TEMPORAL_SUCCESSOR','KEEP_SEPARATE_LINEAGE_REVIEW'
            elif same_side:
                kind = 'B'
                subtype = ('B_SAME_SIDE_VISUAL_REVIEW' if strong_b else
                           'B_SAME_SIDE_DISTINCT_GEOMETRY')
                decision = 'KEEP_SEPARATE_VISUAL_REVIEW' if strong_b else 'KEEP_SEPARATE'
            else:
                kind, subtype, decision = 'B','B_OPPOSITE_SIDE_CONTEXT','KEEP_SEPARATE'
            pairs.append({
                'older_region_id':older,'younger_region_id':newer,
                'relation':kind,'subtype':subtype,'known_from_ts':t,
                'known_from_utc':_utc(t),
                'older_birth_ts':int(old['birth_known_ts']),
                'younger_birth_ts':t,'older_side':str(old['side']),
                'younger_side':str(new['side']),
                'older_band_low_asof':old_stage['low'],
                'older_band_high_asof':old_stage['high'],
                'younger_band_low_birth':new_stage['low'],
                'younger_band_high_birth':new_stage['high'],
                'center_distance_pct':d,'intersection_width':intersect_width,
                'iou':iou,'band_intersects':intersects,
                'older_last_confirm_asof':old_stage['ts'],
                'quiet_minutes_at_younger_birth':quiet//M,
                'old_extrema_asof':len(old_members),
                'new_extrema_at_birth':len(new_members),
                'common_physical_members':len(shared),
                'common_physical_ids':','.join(sorted(shared)),
                'physical_jaccard_asof':jaccard,
                'visual_redundancy_candidate':strong_b,
                'decision':decision,
            })
    pair_df = pd.DataFrame(pairs,columns=PAIR_COLUMNS)
    by_new = defaultdict(list)
    for p in pairs:
        by_new[p['younger_region_id']].append(p)
    # Non-transitive direct-anchor matching. A canonical ID is an alias ONLY
    # with robust shared identity. Strong B yields a visualization REVIEW family;
    # distinct physical IDs and all original rows are always retained.
    canonical_for: dict[str,str] = {}
    visual_for: dict[str,str] = {}
    mappings: list[dict] = []
    for record in records:
        rid = str(record['region_id'])
        related = by_new[rid]
        a = [p for p in related if p['relation']=='A' and
             canonical_for.get(p['older_region_id'])==p['older_region_id']]
        a.sort(key=lambda p:(-p['physical_jaccard_asof'],-p['iou'],p['older_birth_ts'],p['older_region_id']))
        canonical = a[0]['older_region_id'] if a else rid
        canonical_for[rid] = canonical
        b = [p for p in related if p['visual_redundancy_candidate'] and
             visual_for.get(p['older_region_id'])==p['older_region_id']]
        b.sort(key=lambda p:(-p['iou'],p['center_distance_pct'],p['older_birth_ts'],p['older_region_id']))
        visual = b[0]['older_region_id'] if b else rid
        visual_for[rid] = visual
        predecessors = sorted((p for p in related if p['relation']=='C'),
                              key=lambda p:(p['center_distance_pct'],-p['iou'],p['older_birth_ts']))
        mappings.append({
            'region_id':rid,'side':str(record['side']),
            'birth_known_ts':int(record['birth_known_ts']),
            'band_low_birth':float(record['band_low_birth']),
            'band_high_birth':float(record['band_high_birth']),
            'extrema_at_birth':int(record['extrema_at_birth']),
            'canonical_region_id':canonical,'strict_A_alias':canonical != rid,
            'visual_family_anchor_REVIEW_ONLY':visual,
            'visual_family_assignment_ts':int(record['birth_known_ts']),
            'possible_predecessors_C':','.join(p['older_region_id'] for p in predecessors),
            'B_same_side_pairs':sum(p['relation']=='B' and
                                    p['older_side']==p['younger_side'] for p in related),
            'B_opposite_side_pairs':sum(p['relation']=='B' and
                                        p['older_side']!=p['younger_side'] for p in related),
            'A_duplicate_pairs':sum(p['relation']=='A' for p in related),
            'C_successor_pairs':len(predecessors),
            'original_region_preserved':True,
        })
    canon_df = pd.DataFrame(mappings,columns=CANON_COLUMNS)
    if len(canon_df)!=len(regions) or set(canon_df.region_id)!=set(regions.region_id):
        raise ValueError('V25_CANONICAL_MAPPING_LOST_REGION')
    counts = Counter(p['relation'] for p in pairs)
    summary = {
        'regions':len(canon_df),'regions_high':int(canon_df.side.eq('HIGH').sum()),
        'regions_low':int(canon_df.side.eq('LOW').sum()),
        'relations':len(pair_df), 'A':counts['A'], 'B':counts['B'], 'C':counts['C'],
        'B_same_side':sum(p['relation']=='B' and p['older_side']==p['younger_side'] for p in pairs),
        'B_opposite_side':sum(p['relation']=='B' and p['older_side']!=p['younger_side'] for p in pairs),
        'B_visual_review_pairs':sum(p['visual_redundancy_candidate'] for p in pairs),
        'strict_aliases':int(canon_df.strict_A_alias.sum()),
        'visual_review_groups':int(canon_df.groupby('visual_family_anchor_REVIEW_ONLY').size().gt(1).sum()),
        'visual_review_nonanchors':int((canon_df.visual_family_anchor_REVIEW_ONLY!=canon_df.region_id).sum()),
        'regions_with_successor_predecessor_candidates':int(canon_df.C_successor_pairs.gt(0).sum()),
        'original_regions_preserved':True,'no_destructive_merges':True,
    }
    return pair_df,canon_df,summary


def _validate_parent(parent: Path, provenance: dict, regions:pd.DataFrame,
                     stages:pd.DataFrame, cutoff: int) -> dict:
    manifest_path=parent/'manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('version')!='btc-multiswing-information-universe-v2.4a'
        or not manifest.get('immutable_run')
        or manifest.get('config',{}).get('cutoff')!=cutoff):
        raise ValueError('V25_PARENT_VERSION_OR_CUTOFF_MISMATCH')
    p=manifest['source_provenance']
    if (p.get('source_sha256')!=provenance.get('source_sha256') or
        p.get('catalog_sha256')!=provenance.get('catalog_sha256')):
        raise ValueError('V25_PARENT_FREEZE_HASH_MISMATCH')
    file=parent/'regions_v24.csv'
    if digest(file)!=manifest['sha256']['regions_v24.csv']:
        raise ValueError('V25_PARENT_REGIONS_SHA_MISMATCH')
    parent_regions=pd.read_csv(file)
    births=birth_stage_universe(regions,stages,cutoff)
    if (len(parent_regions)!=len(births) or
            set(parent_regions.region_id)!=set(births.region_id)):
        raise ValueError('V25_PARENT_REGION_PARITY_FAILED')
    check=parent_regions.merge(births,on='region_id',validate='one_to_one',suffixes=('_p','_b'))
    for x,y in (('birth_known_ts_p','birth_known_ts_b'),
                ('band_low_birth_p','band_low_birth_b'),
                ('band_high_birth_p','band_high_birth_b')):
        if not (pd.to_numeric(check[x])-pd.to_numeric(check[y])).abs().le(1e-7).all():
            raise ValueError('V25_PARENT_BIRTH_GEOMETRY_CHANGED')
    return manifest


def _write_immutable(output: Path, pairs: pd.DataFrame, canonical:pd.DataFrame,
                     summary:dict, settings:dict, provenance:dict, parent_sha:str) -> None:
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():
        raise FileExistsError(f'V25_RUN_ALREADY_EXISTS: {output}')
    tmp=Path(tempfile.mkdtemp(prefix='.v25a-',dir=output.parent))
    try:
        for frame,name in ((pairs,'relations_v25.csv'),(canonical,'canonicalization_v25.csv')):
            frame.to_csv(tmp/name,index=False, float_format='%.12g')
        payload={'version':VERSION,'counts':summary,'config':settings,
                 'source_provenance':provenance,'parent_v24a_manifest_sha256':parent_sha,
                 'limitations':[
                    'A is strict shared-PHYSICAL-member identity; current V2 assigns a pivot to <=1 region.',
                    'B visual family is a review hint, not a physical duplicate or a merged structure.',
                    'C is possible geometric succession, not a proven structural inheritance or reactivation.',
                    'Opposite HIGH/LOW may coincide but are never merged.',
                    'All pair evidence is frozen as of younger birth; no future stages or visits are used.',
                    'This does not estimate information, quality, predictive power, rejection, or PnL.',
                 ]}
        (tmp/'summary_v25.json').write_text(json.dumps(payload,indent=2,sort_keys=True,ensure_ascii=False)+'\n',encoding='utf-8')
        shas={p.name:digest(p) for p in tmp.iterdir() if p.is_file()}
        meta={'version':VERSION,'immutable_run':True,'no_live_or_network':True,
              'parent_v24a_manifest_sha256':parent_sha,'source_provenance':provenance,
              'sha256':shas,'config':settings}
        (tmp/'manifest.json').write_text(json.dumps(meta,indent=2,sort_keys=True,ensure_ascii=False)+'\n',encoding='utf-8')
        if output.exists():
            raise FileExistsError(f'V25_RUN_ALREADY_EXISTS: {output}')
        os.rename(tmp,output)
    except BaseException:
        shutil.rmtree(tmp,ignore_errors=True)
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description='Multi-Swing V2.5A non-destructive canonicalization audit (offline)')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    parser.add_argument('--project-root',default='.')
    parser.add_argument('--parent-v24a',default=str(DEFAULT_PARENT))
    parser.add_argument('--output-dir',default=str(DEFAULT_OUTPUT))
    parser.add_argument('--near-pct',type=float,default=0.2)
    parser.add_argument('--strict-iou',type=float,default=0.6)
    parser.add_argument('--quiet-days',type=int,default=7)
    parser.add_argument('--successor-max-days',type=int,default=14)
    args=parser.parse_args(argv)
    root=Path(args.project_root).resolve()
    parent=Path(args.parent_v24a)
    output=Path(args.output_dir)
    if not parent.is_absolute(): parent=root/parent
    if not output.is_absolute(): output=root/output
    cfg={'near_pct':args.near_pct,'strict_iou':args.strict_iou,
         'quiet_days':args.quiet_days,'successor_max_days':args.successor_max_days}
    if not (0<cfg['near_pct']<=2 and 0<cfg['strict_iou']<=1 and
            1<=cfg['quiet_days']<cfg['successor_max_days']<=60):
        raise ValueError('INVALID_V25_SETTINGS')
    print('V25_PLAN '+json.dumps({'mode':'PLAN' if args.plan else 'RUN',
        'project_root':str(root),'parent_v24a':str(parent),'output_dir':str(output),
        'settings':cfg,'read_only_freeze':True,'no_live':True},sort_keys=True),flush=True)
    if args.plan:
        return 0
    if output.exists():
        raise FileExistsError(f'V25_RUN_ALREADY_EXISTS: {output}')
    _,catalog,provenance=load_snapshot(root)
    manifest=json.loads((parent/'manifest.json').read_text(encoding='utf-8'))
    parent_cfg=manifest['config']
    if (parent_cfg['geometry'] != {'lookback_days':7, 'separation_minutes':60, 'tolerance_pct':0.2}
        or parent_cfg['side']!='BOTH' or
        parent_cfg['timeframes']!=['15m','30m','1h','4h']):
        raise ValueError('V25_UNEXPECTED_PARENT_GEOMETRY')
    cutoff=int(provenance['end'])
    known=asof_rows(catalog,cutoff,tuple(parent_cfg['timeframes']),parent_cfg['side'])
    regions,stages,_=construct_regions(known,cutoff,tolerance_pct=0.2,
        lookback_days=7,min_separation_minutes=60,min_extrema=2)
    _validate_parent(parent,provenance,regions,stages,cutoff)
    event_first={x['physical_id']:int(x['known_ts']) for x in _unique_confirmed(known,cutoff)}
    pairs,canonical,counts=audit_relations(regions,stages,event_first,cutoff,
        near_pct=args.near_pct,strict_iou=args.strict_iou,
        quiet_days=args.quiet_days,successor_max_days=args.successor_max_days)
    settings={'cutoff':cutoff,'parent_geometry':parent_cfg['geometry'],
              'parent_timeframes':parent_cfg['timeframes'],**cfg}
    parent_sha=digest(parent/'manifest.json')
    _write_immutable(output,pairs,canonical,counts,settings,provenance,parent_sha)
    print('V25_FINISHED '+json.dumps({'output':str(output),'counts':counts,
        'parent_v24a_manifest_sha256':parent_sha},sort_keys=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
