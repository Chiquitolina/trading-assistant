#!/usr/bin/env python3
"""HLL Unified V1.1: enrichment exacto familia Discovery PRE + geometría + OPEN + resultado.

Solo lectura; NO recalcula señales, TP/SL, entradas ni reagrupa procesos.
Inputs V1, Discovery frozen, overlap frozen; salida create-only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = 'HLL_UNIFIED_FAMILY_LIFECYCLE_V1_1'
ORIGINAL_VERSION = 'HLL_UNIFIED_STRUCTURE_LIFECYCLE_V1'
DISCOVERY_VERSION = 'HLL_STRUCTURAL_DISCOVERY_V1'
MAP_VERSION = 'HLL_OVERLAP_AUDIT_V1'
FIELDS = ('family_0p5','family_1p0','family_1p5','scale_agreement','discovery_status')
FAMILIES = {
    'ASCENSO_ESCALONADO','RECONSTRUCCION_ENTRE_CONTACTOS',
    'OSCILACION_EN_RESISTENCIA','REBOTE_TRAS_CORRECCION',
    'DETERIORO_DESDE_MINIMO','CORRECCION_DOMINANTE',
    'SECUENCIA_MIXTA_INDETERMINADA','INDETERMINADA_SIN_MINIMO',
}
ROUTES = ('FLIP_1M','V29_CONTINUATION_30M','V29_RECLAIM_30M')
DEFAULT_V1='reports/horizontal_level_unified_lifecycle_v1/pilot15_seed20261103_v1'
DEFAULT_DISCOVERY='reports/horizontal_level_structural_discovery_v1/full290_v1'
DEFAULT_MAP='reports/horizontal_level_structural_overlap_audit_v1/full290_v1'
DEFAULT_OUTPUT='reports/horizontal_level_unified_lifecycle_v11/pilot15_seed20261103_v11'


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20), b''):h.update(chunk)
    return h.hexdigest()


def exact_ts(v):
    try:
        n=Decimal(str(v).strip())
        if not n.is_finite() or n!=n.to_integral_value() or int(n)<=0 or int(n)%1_800_000:raise ValueError
        return int(n)
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError(f'Invalid exact PRE timestamp: {v!r}') from None


def assert_decimal_match(a,b,field):
    try:
        x,y=Decimal(str(a).strip()),Decimal(str(b).strip())
        if not x.is_finite() or not y.is_finite() or x<=0 or y<=0 or abs(x-y)>max(Decimal('1e-12'),abs(x)*Decimal('1e-8')):
            raise ValueError
    except (InvalidOperation,ValueError):
        raise ValueError(f'Discovery mismatch {field}: {a!r} vs {b!r}') from None


def json_read(p):return json.loads(Path(p).read_text(encoding='utf-8'))


def csv_read(p,required=()):
    with Path(p).open('r',encoding='utf-8-sig',newline='') as f:
        rd=csv.DictReader(f)
        cols=rd.fieldnames or []
        missing=set(required)-set(cols)
        if missing:raise ValueError(f'Missing columns {sorted(missing)} in {p}')
        return list(rd),list(cols)


def index(rows,key,what):
    out={}
    for r in rows:
        k=r.get(key,'')
        if not k or k in out:raise ValueError(f'{what}: null/duplicate {key}={k!r}')
        out[k]=r
    return out


def verify_v1(folder):
    m=json_read(folder/'manifest.json')
    if m.get('version')!=ORIGINAL_VERSION or m.get('entry_scope')!='FIRST_PER_LEVEL' or m.get('fingerprint_scope')!='EPISODE_FIRST_ONLY':
        raise ValueError('V1 version/scope changed')
    for name,expected in m.get('outputs_sha256',{}).items():
        if not (folder/name).is_file() or sha(folder/name)!=expected:
            raise ValueError(f'Unified V1 hash mismatch: {name}')
    needed={'unified_attempts.csv','unified_entries.csv','entry_outcomes_1m.csv','summary.json'}
    if not needed<=set(m.get('outputs_sha256',{})):
        raise ValueError('V1 missing required input manifest fields')
    sm=json_read(folder/'summary.json')
    if sm.get('version')!=ORIGINAL_VERSION:raise ValueError('V1 summary changed')
    return m,sm


def verify_discovery(folder, map_folder, v1_manifest):
    dm=json_read(folder/'manifest.json')
    mm=json_read(map_folder/'manifest.json')
    if dm.get('version')!=DISCOVERY_VERSION or mm.get('version')!=MAP_VERSION:
        raise ValueError('Unexpected discovery/map version')
    if dm.get('scope')!='CROSS_UP_CONDITIONED_PRE_ONLY' or dm.get('reads_post') is not False:
        raise ValueError('Discovery not guaranteed PRE-only')
    if dm.get('attempts')!=55717 or mm.get('attempts')!=55717:
        raise ValueError('Unexpected discovery/map universe')
    if dm.get('output_sha256',{}).get('attempts_pre.csv')!=mm.get('source_sha256',{}).get('discovery'):
        raise ValueError('Map and discovery from different runs')
    if dm.get('source_sha256',{}).get('cohort')!=v1_manifest.get('source_sha256',{}).get('pre'):
        raise ValueError('PRE frozen cohort differs from V1')
    if mm.get('outputs_sha256',{}).get('attempt_episode_map.csv')!=v1_manifest.get('source_sha256',{}).get('map'):
        raise ValueError('Map source differs from V1')
    for path,expected in ((folder/'attempts_pre.csv',dm['output_sha256']['attempts_pre.csv']),
                          (map_folder/'attempt_episode_map.csv',mm['outputs_sha256']['attempt_episode_map.csv'])):
        if not path.is_file() or sha(path)!=expected:
            raise ValueError(f'Frozen input hash mismatch: {path}')
    return dm,mm


def valid_family_row(r):
    status=r.get('status','')
    fs=[r.get('family_0p5',''),r.get('family_1p0',''),r.get('family_1p5','')]
    agreement=r.get('scale_agreement','')
    if status=='COMPLETE_PRE':
        if any(f not in FAMILIES for f in fs):raise ValueError(f'Unknown family: {fs}')
        expected='IGUAL_3_ATR' if len(set(fs))==1 else 'DISCREPA_ATR'
        if agreement!=expected:raise ValueError('Scale agreement inconsistent with families')
    elif status=='INCOMPLETE_PRE_96':
        if agreement!='SIN_DATOS' or any(f not in ('','SIN_DATOS') for f in fs):
            raise ValueError('Incomplete PRE must not invent a family')
    else:raise ValueError(f'Unknown discovery status {status!r}')


def enrich(attempts,entries,outcomes,discovery,map_rows,v1_summary,*,expected_universe=55717):
    aidx=index(attempts,'attempt_id','V1 attempts')
    eidx=index(entries,'entry_id','V1 entries')
    drows=index(discovery,'attempt_id','Discovery')
    midx=index(map_rows,'attempt_id','Map')
    if len(drows)!=expected_universe or len(midx)!=expected_universe:raise ValueError('Universe count mismatch')
    if len(attempts)!=int(v1_summary['pilot_attempts']) or len(entries)!=int(v1_summary['verified_entry_events']):
        raise ValueError('V1 cohort cardinality mismatch')
    if len({a['episode_id'] for a in attempts})!=int(v1_summary['pilot_episodes']):
        raise ValueError('V1 episodes mismatch')
    if len(outcomes)!=int(v1_summary['outcome_scenarios']):raise ValueError('Outcome count mismatch')
    enriched_attempts=[]; family_for={}
    for a in attempts:
        aid=a['attempt_id']
        if aid not in drows or aid not in midx:raise ValueError('Missing exact attempt in Discovery/Map: '+aid)
        d,m=drows[aid],midx[aid]
        for sr in (d,m):
            for k in ('symbol','level_id'):
                if a[k]!=sr[k]:raise ValueError(f'Wrong {k} for attempt {aid}')
            if exact_ts(a['pre_closed_ts'])!=exact_ts(sr['pre_closed_ts']):
                raise ValueError(f'Different PRE cut for attempt {aid}')
            for k in ('zone_low','zone_high'):
                assert_decimal_match(a[k],sr[k],k)
        for k in ('episode_id','episode_ordinal'):
            if a[k]!=m[k]:raise ValueError(f'Map {k} mismatch: {aid}')
        if a['is_episode_first']!=m['is_first_in_episode']:
            raise ValueError('Episode-first identity mismatch')
        if d['status']!=m['status']:
            raise ValueError(f'Original map status differs from Discovery: {aid}')
        if d['status']=='COMPLETE_PRE':
            if d['family_1p0']!=m['family_1p0']:
                raise ValueError(f'Original map differs from Discovery: {aid}')
        elif d.get('family_1p0','') in ('','SIN_DATOS') and m['family_1p0']=='SIN_DATOS':
            pass  # Discovery stores an empty CSV cell for incomplete; Map stores SIN_DATOS.
        else:
            raise ValueError(f'Original map differs from Discovery: {aid}')
        valid_family_row(d)
        new={**a,**{'family_0p5':d.get('family_0p5',''),
                      'family_1p0':d.get('family_1p0',''),
                      'family_1p5':d.get('family_1p5',''),
                      'scale_agreement':d['scale_agreement'],
                      'discovery_status':d['status']}}
        family_for[aid]={k:new[k] for k in FIELDS}
        enriched_attempts.append(new)
    enriched_entries=[]
    for e in entries:
        a=aidx.get(e['attempt_id'])
        if a is None:raise ValueError(f'Entry without source attempt: {e["entry_id"]}')
        for k in ('symbol','level_id','episode_id','split','pre_pattern_96','pre_closed_ts'):
            if e[k]!=a[k]:raise ValueError(f'Entry vs attempt {k}: {e["entry_id"]}')
        if e.get('entry_1m_verified')!='YES_SOURCE_ENTRY_VERIFIED':
            raise ValueError('Non verified entry cannot be enriched')
        if e['route'] not in ROUTES:raise ValueError('Unknown entry route')
        enriched_entries.append({**e,**family_for[e['attempt_id']]})
    enriched_outcomes=[]; scenario_ids=set()
    for o in outcomes:
        e=eidx.get(o['entry_id'])
        if e is None:raise ValueError('Outcome without verified entry')
        for k in ('attempt_id','symbol','episode_id','split','route','entry_ts','pre_pattern_96'):
            if o[k]!=e[k]:raise ValueError(f'Outcome vs entry mismatch {k}')
        key=(o['entry_id'],o['scenario'])
        if key in scenario_ids:raise ValueError('Duplicate outcome scenario')
        scenario_ids.add(key)
        enriched_outcomes.append({**o,**family_for[o['attempt_id']]})
    return enriched_attempts,enriched_entries,enriched_outcomes


def outputs_from_enriched(attempts,entries,outcomes,old_pairs):
    """No outcome-based features; all status bins remain explicit. POST only as grouping target."""
    coverage=defaultdict(list)
    for a in attempts:
        key=(a['split'],a['pre_pattern_96'],a['family_1p0'],a['scale_agreement'])
        coverage[key].append(a)
    coverage_rows=[]
    for (split,geometry,fam,agreement), group in sorted(coverage.items()):
        r={'split':split,'pre_pattern_96':geometry,'family_1p0':fam,'scale_agreement':agreement,
           'attempts':len(group),'episodes':len({x['episode_id'] for x in group}),
           'symbols':len({x['symbol'] for x in group}),
           'fingerprints_exact_episode_first':sum(x['is_episode_first']=='1' for x in group)}
        for route in ROUTES:
            states=Counter(x['coverage_'+route] for x in group)
            if any(s not in ('ENTRY_VERIFIED','NO_SIGNAL','NOT_COVERED','SIGNAL_NO_OPEN','SIGNAL_WITHOUT_OPEN','NO_OPEN') for s in states):
                raise ValueError('Unknown coverage status: '+str(states))
            r[f'{route}_ENTRY_VERIFIED']=states['ENTRY_VERIFIED']
            r[f'{route}_NO_SIGNAL']=states['NO_SIGNAL']
            r[f'{route}_NOT_COVERED']=states['NOT_COVERED']
            r[f'{route}_OTHER']=len(group)-sum(r[f'{route}_{k}'] for k in ('ENTRY_VERIFIED','NO_SIGNAL','NOT_COVERED'))
        coverage_rows.append(r)
    out_groups=defaultdict(list)
    for o in outcomes:
        for scale in ('0p5','1p0','1p5'):
            key=(o['split'],o['route'],o['scenario'],scale,o['pre_pattern_96'],o['family_'+scale],o['scale_agreement'])
            out_groups[key].append(o)
    matrix=[]
    for (split,route,scenario,scale,geometry,family,agree),group in sorted(out_groups.items()):
        counts=Counter(x['state'] for x in group)
        tp=counts['TP_FIRST']+counts['TP_GAP_OPEN']; sl=counts['SL_FIRST']+counts['SL_GAP_OPEN']; resolved=tp+sl
        if any(v not in ('TP_FIRST','TP_GAP_OPEN','SL_FIRST','SL_GAP_OPEN','TIMEOUT','AMBIGUOUS_1M','CENSORED_GAP_OR_TAIL','CENSORED_NO_OPEN','CENSORED_GAP','CENSORED_TAIL') for v in counts):
            raise ValueError('Unknown outcome state: '+str(counts))
        residual=len(group)-resolved-counts['TIMEOUT']-counts['AMBIGUOUS_1M']
        if residual<0:raise ValueError('Outcome count invalid')
        matrix.append({'split':split,'route':route,'scenario':scenario,'scale':scale,
            'pre_pattern_96':geometry,'family':family,'scale_agreement':agree,
            'entries':len(group),'episodes':len({x['episode_id'] for x in group}),
            'symbols':len({x['symbol'] for x in group}),
            'tp_first':tp,'sl_first':sl,'timeout':counts['TIMEOUT'],
            'ambiguous':counts['AMBIGUOUS_1M'],'censored':residual,
            'resolved':resolved,'tp_pct_resolved':round(100*tp/resolved,3) if resolved else '',
            'net_pct_mean_all_known':(round(sum(float(x['net_pct']) for x in group if x['net_pct'] not in ('','nan'))/
                  sum(x['net_pct'] not in ('','nan') for x in group),6)
                   if any(x['net_pct'] not in ('','nan') for x in group) else ''),
            'interpretation':'RETROSPECTIVE_DESCRIPTIVE_DEPENDENT_EPISODES_NO_OOS'})
    by_ep=defaultdict(list)
    for a in attempts:by_ep[a['episode_id']].append(a)
    evol=[]
    for eid,group in sorted(by_ep.items()):
        group=sorted(group,key=lambda a:(exact_ts(a['pre_closed_ts']),int(a['episode_ordinal']),a['attempt_id']))
        if [int(x['episode_ordinal']) for x in group]!=list(range(1,len(group)+1)):
            raise ValueError(f'Non-contiguous episode ordinal: {eid}')
        if len({x['symbol'] for x in group})!=1 or len({x['level_id'] for x in group})!=1:
            raise ValueError('Episode crosses symbol or level')
        first=group[0]
        r={'episode_id':eid,'symbol':first['symbol'],'level_id':first['level_id'],
           'split':first['split'],'first_attempt_id':first['attempt_id'],
           'first_pre_closed_ts':first['pre_closed_ts'],'attempts_retrospective':len(group),
           'geometry_first':first['pre_pattern_96'],
           'family_first_0p5':first['family_0p5'],'family_first_1p0':first['family_1p0'],
           'family_first_1p5':first['family_1p5']}
        for scale in ('0p5','1p0','1p5'):
            seq=[x['family_'+scale] for x in group]
            r[f'family_sequence_{scale}_retrospective']=' > '.join(seq)
            r[f'family_changes_{scale}_retrospective']=sum(a!=b for a,b in zip(seq,seq[1:]))
            r[f'family_distinct_{scale}_retrospective']=len(set(seq))
        r['selection_warning']='FUTURE_ATTEMPTS_RETROSPECTIVE_NOT_AVAILABLE_AT_FIRST_CUT'
        evol.append(r)
    amap=index(attempts,'attempt_id','Enriched attempts')
    eidmap=index(entries,'entry_id','Enriched entries')
    annotated=[]
    for p in old_pairs:
        winner=eidmap.get(p['winner_entry_id']);loser=eidmap.get(p['loser_entry_id'])
        if not winner or not loser:raise ValueError('V1 matched pair missing entry')
        if winner['attempt_id']!=p['winner_attempt_id'] or loser['attempt_id']!=p['loser_attempt_id']:
            raise ValueError('V1 matched pair mismatch')
        annotated.append({**p,'winner_family_1p0':winner['family_1p0'],
           'loser_family_1p0':loser['family_1p0'],
           'winner_scale_agreement':winner['scale_agreement'],
           'loser_scale_agreement':loser['scale_agreement'],
           'same_family_1p0':int(winner['family_1p0']==loser['family_1p0']),
           'note':'PAIR_SELECTED_RETROSPECTIVELY_NOT_OOS'})
    # New family-matched pairs: same PRE geometry/family (1 ATR), distinct symbol and episode.
    target={o['entry_id']:o for o in outcomes if o['scenario']=='TP3_SL1P5_48H'}
    eligible=[e for e in entries if e['route']=='FLIP_1M' and e['split']=='DISCOVERY_SYMBOL'
              and e['fingerprint_status']=='COMPLETE_PRE_96_288'
              and e['family_1p0'] in FAMILIES and e['pre_pattern_96'] not in ('','UNKNOWN')
              and e['entry_id'] in target]
    win=[e for e in eligible if target[e['entry_id']]['state']=='TP_FIRST']
    lost=[e for e in eligible if target[e['entry_id']]['state']=='SL_FIRST']
    used=set();pairs=[]
    weights=(('geo_low_up_share_96',.25),('rebuild_last_low_step_atr_96',.8),
             ('level_near_share_24_96',.25),('movement_signed_efficiency_24_96',.3))
    def dist(a,b):
        vals=[]
        for key,denom in weights:
            try:
                x,y=float(a[key]),float(b[key])
                if math.isfinite(x) and math.isfinite(y): vals.append(abs(x-y)/denom)
            except (TypeError,ValueError,KeyError):pass
        return sum(vals)/len(vals) if len(vals)>=2 else math.inf
    for a in sorted(win,key=lambda x:(x['symbol'],x['entry_ts_int'],x['entry_id'])):
        if len(pairs)>=30:break
        options=[b for b in lost if b['entry_id'] not in used and b['symbol']!=a['symbol']
                 and b['episode_id']!=a['episode_id']
                 and b['pre_pattern_96']==a['pre_pattern_96']
                 and b['family_1p0']==a['family_1p0']]
        if not options:continue
        b=min(options,key=lambda x:(dist(a,x),x['entry_id']))
        d=dist(a,b)
        if not math.isfinite(d):continue
        used.add(b['entry_id'])
        pairs.append({'winner_entry_id':a['entry_id'],'loser_entry_id':b['entry_id'],
          'winner_attempt_id':a['attempt_id'],'loser_attempt_id':b['attempt_id'],
          'winner_symbol':a['symbol'],'loser_symbol':b['symbol'],
          'winner_episode_id':a['episode_id'],'loser_episode_id':b['episode_id'],
          'pre_pattern_96':a['pre_pattern_96'],'family_1p0':a['family_1p0'],
          'fixed_pre_distance':round(d,6),
          'selection':'POST_SELECTED_REVIEW_PAIR_PRE_DISTANCE_ONLY_NO_OOS'})
    return {'unified_attempts_family_v11.csv':attempts,
            'unified_entries_family_v11.csv':entries,
            'entry_outcomes_family_v11.csv':outcomes,
            'by_geometry_family_coverage_v11.csv':coverage_rows,
            'by_geometry_family_route_outcomes_v11.csv':matrix,
            'episode_family_evolution_v11.csv':evol,
            'v1_pairs_family_annotated_v11.csv':annotated,
            'family_geometry_flip_review_pairs_v11.csv':pairs}


def csv_write(p,records,columns):
    with Path(p).open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='raise')
        w.writeheader();w.writerows(records)


def paths_for(root, v1=DEFAULT_V1, discovery=DEFAULT_DISCOVERY, mapping=DEFAULT_MAP, output=DEFAULT_OUTPUT):
    def resolve(p):
        path=Path(p)
        return path.resolve() if path.is_absolute() else (Path(root)/path).resolve()
    return {k:resolve(v) for k,v in {'v1':v1,'discovery':discovery,'map':mapping,'output':output}.items()}


def build(paths,*,emit=True):
    v1,disc,mp=paths['v1'],paths['discovery'],paths['map']
    vm,vs=verify_v1(v1)
    dm,mm=verify_discovery(disc,mp,vm)
    attempts,_=csv_read(v1/'unified_attempts.csv',('attempt_id','split','symbol','level_id','episode_id','pre_closed_ts'))
    entries,_=csv_read(v1/'unified_entries.csv',('entry_id','attempt_id','route','entry_1m_verified'))
    outcomes,_=csv_read(v1/'entry_outcomes_1m.csv',('entry_id','scenario','state'))
    old_pairs,_=csv_read(v1/'matched_flip_review_pairs.csv',('winner_entry_id','loser_entry_id'))
    discovery,_=csv_read(disc/'attempts_pre.csv',('attempt_id','symbol','level_id','pre_closed_ts','status',
                    'family_0p5','family_1p0','family_1p5','scale_agreement','zone_low','zone_high'))
    map_rows,_=csv_read(mp/'attempt_episode_map.csv',('attempt_id','symbol','level_id','pre_closed_ts',
                    'episode_id','episode_ordinal','is_first_in_episode','status','family_1p0'))
    at,en,ou=enrich(attempts,entries,outcomes,discovery,map_rows,vs)
    artifacts=outputs_from_enriched(at,en,ou,old_pairs)
    stats={name:len(records) for name,records in artifacts.items()}
    summary={'version':VERSION,'source_unified_v1':str(v1),'source_discovery':str(disc),
        'scope':'PILOT15_ALL_ATTEMPTS_EXACT_CUT','source_attempts':len(at),
        'source_entries_verified':len(en),'source_outcome_rows_unchanged':len(ou),
        'episodes':len({a['episode_id'] for a in at}),
        'families_1p0':dict(Counter(x['family_1p0'] for x in at)),
        'agreement':dict(Counter(x['scale_agreement'] for x in at)),
        'output_rows':stats,'split':dict(Counter(x['split'] for x in at)),
        'frozen_detectors_modified':False,'source_v1_modified':False,
        'discovery_pre_features_only':True,'no_live':True,
        'outcome_origin':'VERIFIED_ARCHIVED_V1_OPEN_1M_HISTORICAL',
        'interpretation':['Family and geometry are two complementary PRE classifications at each EXACT attempt cut',
          'Family changes across attempts are retrospective when seen from episode first attempt',
          'FIRST_PER_LEVEL not covered is not NO_SIGNAL',
          'Fingerprint 35 features belong only to first attempt; do not propagate',
          'Do not tune parameters on HOLDOUT or use frozen detector outcome as entry-time feature',
          'Physical process membership is retrospective only; no feature leakage',
          'Paired winners and losers are selected after outcomes only for visual review',
          'Same episode and same underlying level can produce dependent attempts',
          'Comparisons are descriptive, not win rate out-of-sample or realized trading performance']}
    manifest={'version':VERSION,'sources_sha256':{
        'v1_manifest':sha(v1/'manifest.json'),
        'discovery_manifest':sha(disc/'manifest.json'),
        'discovery_attempts_pre':sha(disc/'attempts_pre.csv'),
        'map_manifest':sha(mp/'manifest.json'),
        'v1_entries':sha(v1/'unified_entries.csv'),
        'v1_outcomes':sha(v1/'entry_outcomes_1m.csv')},
        'outcomes_copied_no_recompute':True,'input_v1_hashes_verified':True,
        'input_discovery_hashes_verified':True,'scope_first_per_level':True,
        'output_sha256':{}}
    return artifacts,summary,manifest


def run(paths):
    out=paths['output']
    if any(out==paths[k] or paths[k] in out.parents for k in ('v1','discovery','map')):
        raise ValueError('Refuse output inside source folder')
    if out.exists():raise FileExistsError(f'Output already exists (no overwrite): {out}')
    artifacts,summary,manifest=build(paths)
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.hll-family-v11-',dir=out.parent))
    try:
        for name,recs in artifacts.items():
            if name.startswith('unified_attempts'):
                cols=list(csv_read(paths['v1']/'unified_attempts.csv')[1])+list(FIELDS)
            elif name.startswith('unified_entries'):
                cols=list(csv_read(paths['v1']/'unified_entries.csv')[1])+list(FIELDS)
            elif name.startswith('entry_outcomes'):
                cols=list(csv_read(paths['v1']/'entry_outcomes_1m.csv')[1])+list(FIELDS)
            else:
                if recs:
                    cols=list(recs[0])
                elif name=='family_geometry_flip_review_pairs_v11.csv':
                    cols=['winner_entry_id','loser_entry_id','winner_attempt_id','loser_attempt_id',
                          'winner_symbol','loser_symbol','winner_episode_id','loser_episode_id',
                          'pre_pattern_96','family_1p0','fixed_pre_distance','selection']
                elif name=='v1_pairs_family_annotated_v11.csv':
                    cols=['winner_entry_id','loser_entry_id','winner_attempt_id','loser_attempt_id',
                          'winner_symbol','loser_symbol','winner_episode_id','loser_episode_id',
                          'pre_pattern_96','fixed_pre_distance','selection','winner_family_1p0',
                          'loser_family_1p0','winner_scale_agreement','loser_scale_agreement',
                          'same_family_1p0','note']
                else:
                    raise ValueError(f'Unexpected empty report: {name}')
            csv_write(tmp/name,recs,cols)
            manifest['output_sha256'][name]=sha(tmp/name)
        (tmp/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        manifest['output_sha256']['summary.json']=sha(tmp/'summary.json')
        (tmp/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.rename(tmp,out)
    except Exception:
        shutil.rmtree(tmp,ignore_errors=True)
        raise
    result={'status':'DONE','version':VERSION,'output':str(out),
            'attempts':summary['source_attempts'],'verified_entries':summary['source_entries_verified'],
            'outcomes':summary['source_outcome_rows_unchanged'],
            'families':len(summary['families_1p0']),
            'matrix_rows':summary['output_rows']['by_geometry_family_route_outcomes_v11.csv'],
            'family_matched_flip_pairs':summary['output_rows']['family_geometry_flip_review_pairs_v11.csv']}
    print('HLL_UNIFIED_V11_FINAL '+json.dumps(result,ensure_ascii=False),flush=True)
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--v1',default=DEFAULT_V1);p.add_argument('--discovery',default=DEFAULT_DISCOVERY)
    p.add_argument('--map',default=DEFAULT_MAP);p.add_argument('--output',default=DEFAULT_OUTPUT)
    args=p.parse_args(argv)
    paths=paths_for(args.root,args.v1,args.discovery,args.map,args.output)
    print('HLL_UNIFIED_V11_PLAN '+json.dumps({'version':VERSION,'sources_present':{k:paths[k].is_dir() for k in ('v1','discovery','map')},
      'output':str(paths['output']),'output_exists':paths['output'].exists(),
      'writes_sources':False,'needs_full290_discovery':True,'intended_attempts':2780,
      'verifies_all_hashes_during_run':True},ensure_ascii=False),flush=True)
    if args.plan:return 0
    run(paths)
    return 0

if __name__=='__main__':raise SystemExit(main())
