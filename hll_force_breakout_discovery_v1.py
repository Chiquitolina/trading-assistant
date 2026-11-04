#!/usr/bin/env python3
"""HLL Force Breakout Discovery V1 — offline, unbiased clock-grid force study.

Research universe: every aligned 1m OPEN (15m cadence by default), for ALL
selected symbols, independently of whether HLL later recorded a CROSS_UP.
Only 1m candles CLOSED before this OPEN are used for candidate features.
The archived HLL CROSS_UP/PRE-family map is a separate descriptive overlay:
never used to admit candidates or calculate force. No LIVE changes.

Outcomes are retrospective first-barrier scenarios and NEVER predictors.
Symbol-held-out outcomes are not exported. Repeated time snapshots and
multiple HLL attempts per symbol/level are dependent observations.
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
from datetime import datetime, timezone
from pathlib import Path

VERSION = 'HLL_FORCE_BREAKOUT_DISCOVERY_V1'
TF1 = 60_000
TF30 = 30 * TF1
BASE_BARS = 240
BASE_EXCLUDE_RECENT_BARS = 30
FEE_ROUNDTRIP_PCT = 0.10
COHORT = 'reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv'
MAP = 'reports/horizontal_level_structural_overlap_audit_v1/full290_v1/attempt_episode_map.csv'
MINUTE = 'reports/horizontal_level_full290_v301/minutes'
MINUTE_MANIFEST = 'reports/horizontal_level_full290_v301/minutes_manifest'
OUT_PARENT = 'reports/horizontal_level_force_breakout_discovery_v1'
PILOT15 = ('AAVEUSDT','ARIAUSDT','BIGTIMEUSDT','BSVUSDT','CAKEUSDT',
           'COLLECTUSDT','CVXUSDT','DYDXUSDT','ESPORTSUSDT','FIGHTUSDT',
           'IOUSDT','VELVETUSDT','WAXPUSDT','XLMUSDT','XMRUSDT')
LEGACY_RESERVED = {'AAVEUSDT','FIGHTUSDT'}
LEGACY_EXAMINED = set(PILOT15) - LEGACY_RESERVED
# These scenarios are declared BEFORE seeing the results of this study.
SCENARIOS = (('TP2_SL1P5_6H',2.0,1.5,360),
             ('TP3_SL1P5_24H',3.0,1.5,1440),
             ('TP5_SL1P5_48H',5.0,1.5,2880))
SFX = ('w15_net_pct','w15_eff_signed','w15_drawdown_pct',
       'w30_net_pct','w30_eff_signed','w30_drawdown_pct',
       'w60_net_pct','w60_eff_signed','w60_drawdown_pct',
       'eff_delta_last15_vs_prev15','net_delta_last15_vs_prev15_pp',
       'base_high_prior_4h_ex_last30','base_low_prior_4h_ex_last30',
       'base_range_pct','recent60_range_vs_base','distance_last_close_to_base_high_pct',
       'last_close_above_base_high_pct','recovery_last60_from_low_pct',
       'last60_high_beyond_base_high_pct','last30_closed_above_base_high',
       'last30_low_vs_base_high_pct','last_close','last15_direction',
       'force_phase','base_position')
SNAPSHOT_FIELDS = ('snapshot_id','symbol','snapshot_ts','split','quality','cadence_min',
                   'entry_open_reference','source_1m_file','no_hll_conditioning',
                   *SFX)
LINK_FIELDS = ('attempt_id','symbol','episode_id','episode_ordinal',
               'is_first_in_episode','level_id','pre_closed_ts','breakout_closed_ts',
               'snapshot_id','snapshot_link_status','pre_pattern_24','pre_pattern_48',
               'pre_pattern_96','family_1p0','zone_low','zone_high','atr_pre',
               'link_use','no_future_family_propagation')
OUTCOME_FIELDS = ('snapshot_id','symbol','snapshot_ts','split','scenario',
                  'tp_pct','sl_pct','horizon_min','entry_open_reference',
                  'state','hit_ts','minutes_observed','net_pct',
                  'force_phase','base_position','label_scope')
CROSS_CASE_FIELDS = ('attempt_id','episode_id','symbol','snapshot_id','snapshot_ts',
                     'pre_pattern_96','family_1p0','force_phase','base_position',
                     'w15_eff_signed','w30_eff_signed','eff_delta_last15_vs_prev15',
                     'w15_net_pct','distance_last_close_to_base_high_pct',
                     'scenario','state','case_scope')
CROSS_GROUP_FIELDS = ('pre_pattern_96','family_1p0','force_phase','scenario',
                      'n_first_episode_attempts','distinct_snapshots','distinct_episodes',
                      'symbols','tp_first','sl_first','other','tp_rate_resolved_pct',
                      'warning')
GROUP_FIELDS = ('scenario','force_phase','base_position','snapshots',
                'symbols','tp_first','sl_first','ambiguous_1m','timeout',
                'censored_or_missing','tp_rate_resolved_pct',
                'warning')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):
            h.update(b)
    return h.hexdigest()


def json_read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def csv_rows(path, required=()):
    with Path(path).open('r',newline='',encoding='utf-8-sig') as f:
        reader=csv.DictReader(f)
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError('Missing CSV columns in '+str(path)+': '+str(set(required)-set(reader.fieldnames or [])))
        yield from reader


def stamp(value, step=TF1):
    try:
        if str(value).strip()!=str(int(value)):
            raise ValueError
        t=int(value)
    except (TypeError,ValueError,OverflowError):
        raise ValueError('Non-exact millisecond timestamp '+repr(value)) from None
    if t<=0 or t%step: raise ValueError('Unaligned timestamp '+repr(value))
    return t


def positive(value):
    try:v=float(value)
    except (TypeError,ValueError,OverflowError):raise ValueError('Bad price '+repr(value)) from None
    if not math.isfinite(v) or v<=0:raise ValueError('Bad positive price '+repr(value))
    return v


def pct(v):
    return round(v,6)


def split_for_symbol(symbol):
    if symbol in LEGACY_RESERVED:return 'LEGACY_RESERVED'
    if symbol in LEGACY_EXAMINED:return 'LEGACY_EXAMINED_DISCOVERY'
    # Frozen deterministic symbol split; no price/outcome input.
    bucket=int(hashlib.sha256(('HLL_FORCE_BREAKOUT_V1|'+symbol).encode()).hexdigest()[:8],16)%100
    return 'EXPLORATION' if bucket<70 else ('VALIDATION_RESERVED' if bucket<85 else 'TEST_RESERVED')


def is_discovery(split):
    return split in ('LEGACY_EXAMINED_DISCOVERY','EXPLORATION')


def paths(root, args):
    root=Path(root).resolve()
    def absolute(value):
        p=Path(value)
        return p.resolve() if p.is_absolute() else (root/p).resolve()
    folder=args.output or (OUT_PARENT+'/'+args.scope+'_c'+str(args.cadence_min)+'_v1')
    return {'cohort':absolute(args.cohort),'map':absolute(args.map),
            'minute':absolute(args.minute),'minute_manifest':absolute(args.minute_manifest),
            'output':absolute(folder)}


def parse_symfile(scope, symbols_arg):
    if symbols_arg:
        ss=tuple(sorted(set(x.strip() for x in symbols_arg.split(',') if x.strip())))
        if not ss:raise ValueError('Empty --symbols')
        return ss
    if scope=='pilot15':return PILOT15
    return None # derive complete 290 from immutable source


def source_check(pp, symbol_subset, *, require_all=True):
    pre_manifest=json_read(pp['cohort'].with_name('manifest.json'))
    mp_manifest=json_read(pp['map'].with_name('manifest.json'))
    if (pre_manifest.get('version')!='HLL_V282_PRE_COHORT'
          or pre_manifest.get('mode')!='OFFLINE_READ_ONLY'
          or mp_manifest.get('version')!='HLL_OVERLAP_AUDIT_V1'):
        raise ValueError('Unexpected HLL frozen source provenance')
    expected_sha=mp_manifest.get('source_sha256',{}).get('cohort')
    if not expected_sha or expected_sha!=sha(pp['cohort']):
        raise ValueError('Cohort hash mismatches frozen episode map')
    expected_map=mp_manifest.get('outputs_sha256',{}).get('attempt_episode_map.csv')
    if not expected_map or expected_map!=sha(pp['map']):
        raise ValueError('Episode map hash mismatch')
    pre={}
    for r in csv_rows(pp['cohort'],('attempt_id','symbol','level_id','pre_closed_ts',
                                  'breakout_closed_ts','pre_pattern_96')):
        aid=r['attempt_id']
        if not aid or aid in pre:raise ValueError('Duplicate/null cohort ID: '+repr(aid))
        pre[aid]=r
    if len(pre)!=int(pre_manifest['original_attempts']):
        raise ValueError('Frozen cohort cardinality changed')
    if require_all and len(pre)!=55717:raise ValueError('Unexpected full290 attempt count')
    events=defaultdict(lambda:defaultdict(list))
    found=set();all_symbols=set()
    for r in csv_rows(pp['map'],('attempt_id','symbol','level_id','episode_id',
                                 'breakout_closed_ts','pre_closed_ts','is_first_in_episode','family_1p0')):
        aid=r['attempt_id']
        if aid in found:raise ValueError('Duplicated attempt in episode map')
        found.add(aid)
        p=pre.get(aid)
        if p is None:raise ValueError('Orphan episode map attempt')
        for k in ('symbol','level_id','breakout_closed_ts','pre_closed_ts'):
            if r[k]!=p[k]:raise ValueError('Cohort/episode mismatch '+aid+' '+k)
        if stamp(r['breakout_closed_ts'],TF30)!=stamp(r['pre_closed_ts'],TF30)+TF30:
            raise ValueError('Invalid CROSS_UP causal time')
        all_symbols.add(r['symbol'])
        if symbol_subset is None or r['symbol'] in symbol_subset:
            events[r['symbol']][stamp(r['breakout_closed_ts'],TF30)].append({
                **{k:r.get(k,'') for k in LINK_FIELDS if k in r},
                **{k:p.get(k,'') for k in ('pre_pattern_24','pre_pattern_48','pre_pattern_96')},
            })
    if found!=set(pre):raise ValueError('Episode map not identical to cohort')
    if len(all_symbols)!=int(pre_manifest['symbols']):
        raise ValueError('Frozen symbol cardinality changed')
    selected=tuple(sorted(all_symbols)) if symbol_subset is None else tuple(sorted(symbol_subset))
    if not set(selected).issubset(all_symbols):raise ValueError('Unknown requested HLL symbols')
    return selected,events,expected_sha,expected_map


def load_tape(file):
    t=[];op=[];hi=[];lo=[];cl=[]
    last=None
    for r in csv_rows(file,('timestamp','open','high','low','close')):
        ts=stamp(r['timestamp'])
        vals=[positive(r[x]) for x in ('open','high','low','close')]
        if vals[2]>min(vals[0],vals[3]) or vals[1]<max(vals[0],vals[3]) or vals[1]<vals[2]:
            raise ValueError('Invalid 1m OHLC '+str(file)+' at '+str(ts))
        if last is not None and ts<=last:raise ValueError('Duplicate/nonmonotonic 1m tape')
        t.append(ts);op.append(vals[0]);hi.append(vals[1]);lo.append(vals[2]);cl.append(vals[3]);last=ts
    if not t:raise ValueError('Empty minute tape '+str(file))
    return t,(op,hi,lo,cl)


def continuous_runs(t):
    starts=[0]*len(t);ends=[0]*len(t)
    for i in range(1,len(t)):
        starts[i]=starts[i-1] if t[i]==t[i-1]+TF1 else i
    ends[-1]=len(t)-1
    for i in range(len(t)-2,-1,-1):
        ends[i]=ends[i+1] if t[i+1]==t[i]+TF1 else i
    return starts,ends


class Extremes:
    """Range extrema + earliest future barrier hit, O(log N) per query."""
    def __init__(self, highs, lows):
        self.n=len(highs)
        n=1
        while n<self.n:n*=2
        self.padded=n
        self.h=[float('-inf')]*(2*n)
        self.l=[float('inf')]*(2*n)
        for i,(hi,lo) in enumerate(zip(highs,lows)):
            self.h[n+i]=hi;self.l[n+i]=lo
        for k in range(n-1,0,-1):
            self.h[k]=max(self.h[k*2],self.h[k*2+1])
            self.l[k]=min(self.l[k*2],self.l[k*2+1])

    def range(self,start,end):
        if not (0<=start<end<=self.n):raise ValueError('Invalid range')
        left=start+self.padded;right=end+self.padded
        h=float('-inf');l=float('inf')
        while left<right:
            if left&1:h=max(h,self.h[left]);l=min(l,self.l[left]);left+=1
            if right&1:right-=1;h=max(h,self.h[right]);l=min(l,self.l[right])
            left//=2;right//=2
        return h,l

    def first_barrier(self,start,end,tp,sl):
        if not (0<=start<=end<=self.n):raise ValueError('Invalid query range')
        def find(node,a,b):
            if b<=start or a>=end or (self.h[node]<tp and self.l[node]>sl):return None
            if b-a==1:return a
            mid=(a+b)//2
            left=find(node*2,a,mid)
            return left if left is not None else find(node*2+1,mid,b)
        return find(1,0,self.padded)


def rolling_force(closes):
    """Anchor close plus W following closes, all ALREADY CLOSED."""
    diffs=[b-a for a,b in zip(closes,closes[1:])]
    ups=sum(max(x,0) for x in diffs)
    dns=sum(max(-x,0) for x in diffs)
    total=ups+dns
    top=closes[0];drawdown=0.0
    for x in closes:
        top=max(top,x)
        drawdown=max(drawdown,100*(top-x)/top)
    return (pct(100*(closes[-1]/closes[0]-1)),
            pct((closes[-1]-closes[0])/total) if total>0 else 0.0,
            pct(drawdown))


def feature_at(i,t,values,ext,cadence_min):
    """Full 240 closed minute-bar prefix required; current entry bar excluded."""
    op,hi,lo,cl=values
    if i<BASE_BARS:raise ValueError('Insufficient causal 1m history')
    if t[i]%(cadence_min*TF1):raise ValueError('Non-cadence checkpoint')
    w={}
    for width in (15,30,60):
        ret,eff,dd=rolling_force(cl[i-width-1:i])
        w[f'w{width}_net_pct']=ret
        w[f'w{width}_eff_signed']=eff
        w[f'w{width}_drawdown_pct']=dd
    previous=rolling_force(cl[i-31:i-15])
    w['eff_delta_last15_vs_prev15']=pct(w['w15_eff_signed']-previous[1])
    w['net_delta_last15_vs_prev15_pp']=pct(w['w15_net_pct']-previous[0])
    base_hi,base_lo=ext.range(i-BASE_BARS,i-BASE_EXCLUDE_RECENT_BARS)
    recent_hi,recent_lo=ext.range(i-60,i)
    last=cl[i-1]
    if base_hi<base_lo:raise ValueError('Inverted historical base range')
    width=100*(base_hi/base_lo-1)
    ratio=pct((recent_hi-recent_lo)/(base_hi-base_lo)) if base_hi>base_lo else ''
    w.update(base_high_prior_4h_ex_last30=pct(base_hi),
        base_low_prior_4h_ex_last30=pct(base_lo),
        base_range_pct=pct(width),
        recent60_range_vs_base=ratio,
        distance_last_close_to_base_high_pct=pct(100*(last/base_hi-1)),
        last_close_above_base_high_pct=pct(100*max(last/base_hi-1,0)),
        recovery_last60_from_low_pct=pct(100*(last/recent_lo-1)),
        last60_high_beyond_base_high_pct=pct(100*(recent_hi/base_hi-1)),
        last30_closed_above_base_high=sum(x>base_hi for x in cl[i-30:i]),
        last30_low_vs_base_high_pct=pct(100*(min(lo[i-30:i])/base_hi-1)),
        last_close=pct(last))
    w['base_position']=('ESCAPED_BASE' if last>base_hi else
                        ('NEAR_BASE_EDGE' if last>=base_hi*0.995 else 'BELOW_EDGE'))
    w['last15_direction']='UP' if w['w15_net_pct']>0 else ('DOWN' if w['w15_net_pct']<0 else 'FLAT')
    w['force_phase']=('ADVANCING_EFFICIENCY' if w['w15_net_pct']>0 and w['eff_delta_last15_vs_prev15']>0
                       else ('ADVANCING_LOSING_EFFICIENCY' if w['w15_net_pct']>0 else
                            ('DIRECTION_DOWN_OR_FLAT')))
    return w


def label_at(i,t,values,ends,ext,scenario):
    """Historical OPEN execution with conservative same-minute tie state."""
    op,hi,lo,cl=values
    name,tp_pct,sl_pct,horizon=scenario
    entry=op[i]
    tp=entry*(1+tp_pct/100)
    sl=entry*(1-sl_pct/100)
    end=min(i+horizon,ends[i]+1)
    hit=ext.first_barrier(i,end,tp,sl)
    if hit is None:
        if end==i+horizon:
            return 'TIMEOUT','',horizon,round(100*(cl[end-1]/entry-1)-FEE_ROUNDTRIP_PCT,6)
        return ('CENSORED_TAIL' if ends[i]==len(t)-1 else 'CENSORED_GAP'),'',end-i,''
    # OPEN price is the first possible observation on a subsequent minute;
    # gaps across threshold are handled before conflicting high/low candles.
    if hit>i and op[hit]>=tp:
        return 'TP_GAP_OPEN',t[hit],hit-i+1,pct(100*(op[hit]/entry-1)-FEE_ROUNDTRIP_PCT)
    if hit>i and op[hit]<=sl:
        return 'SL_GAP_OPEN',t[hit],hit-i+1,pct(100*(op[hit]/entry-1)-FEE_ROUNDTRIP_PCT)
    tp_hit=hi[hit]>=tp
    sl_hit=lo[hit]<=sl
    if tp_hit and sl_hit:return 'AMBIGUOUS_1M',t[hit],hit-i+1,''
    if tp_hit:return 'TP_FIRST',t[hit],hit-i+1,pct(tp_pct-FEE_ROUNDTRIP_PCT)
    if sl_hit:return 'SL_FIRST',t[hit],hit-i+1,pct(-sl_pct-FEE_ROUNDTRIP_PCT)
    raise AssertionError('Extrema tree indicated barrier without hit')


def id_for(symbol,ts):
    return hashlib.sha256((symbol+'|'+str(ts)+'|FBD_V1').encode()).hexdigest()[:24]


def writer(path,fields):
    f=path.open('x',newline='',encoding='utf-8')
    w=csv.DictWriter(f,fieldnames=fields,extrasaction='raise')
    w.writeheader()
    return f,w


def run(pp,symbols,links,pre_sha,map_sha,cadence):
    dest=pp['output']
    if dest.exists():raise FileExistsError('Output exists; no overwrite: '+str(dest))
    for k in ('cohort','map','minute','minute_manifest'):
        src=pp[k]
        if src==dest or src in dest.parents or dest in src.parents:
            raise ValueError('Output overlaps immutable HLL input: '+str(src))
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.hll-fbd-v1-',dir=dest.parent))
    stats=Counter()
    group=defaultdict(lambda:{'states':Counter(),'symbols':set()})
    cross_groups=defaultdict(lambda:{'states':Counter(),'symbols':set(),'snapshots':set(),'episodes':set()})
    hashes_minutes={}
    hashes_manifests={}
    try:
        with (tmp/'force_snapshots_CAUSAL.csv').open('x',newline='',encoding='utf-8') as fs, \
             (tmp/'force_outcomes_DISCOVERY_ONLY.csv').open('x',newline='',encoding='utf-8') as fo, \
             (tmp/'hll_crossup_links_CONTEXT_ONLY.csv').open('x',newline='',encoding='utf-8') as fl, \
             (tmp/'hll_crossup_force_cases_DISCOVERY_ONLY.csv').open('x',newline='',encoding='utf-8') as fc:
            sw=csv.DictWriter(fs,fieldnames=SNAPSHOT_FIELDS);sw.writeheader()
            ow=csv.DictWriter(fo,fieldnames=OUTCOME_FIELDS);ow.writeheader()
            lw=csv.DictWriter(fl,fieldnames=LINK_FIELDS);lw.writeheader()
            cw=csv.DictWriter(fc,fieldnames=CROSS_CASE_FIELDS);cw.writeheader()
            for nr,sym in enumerate(symbols,1):
                mf=pp['minute_manifest']/(sym+'.json')
                file=pp['minute']/(sym+'_1m.csv')
                if not file.is_file() or not mf.is_file():raise FileNotFoundError('Missing verified tape: '+sym)
                meta=json_read(mf)
                h=sha(file)
                if h!=meta.get('sha256') or meta.get('source_sha256')!=pre_sha:
                    raise ValueError('Frozen 1m tape/source hash mismatch for '+sym)
                hashes_minutes[sym]=h
                hashes_manifests[sym]=sha(mf)
                t,arrays=load_tape(file)
                if len(t)!=int(meta['bars']):raise ValueError('Minute row-count mismatch for '+sym)
                run_starts,run_ends=continuous_runs(t)
                extrema=Extremes(arrays[1],arrays[2])
                snap_by_time={}
                features_by_time={}
                labels_by_time={}
                split=split_for_symbol(sym)
                scanned=0;eligible=0
                for i,ts in enumerate(t):
                    if ts%(cadence*TF1):continue
                    scanned+=1
                    # Ensure current OPEN and previous 240 CLOSED bars have an
                    # exactly continuous 1m prefix. Missing history is omitted
                    # from the candidate panel, NEVER treated as a failure.
                    if i<BASE_BARS or run_starts[i-1]>i-BASE_BARS or t[i-1]!=ts-TF1:
                        stats['excluded_missing_prefix']+=1
                        continue
                    eligible+=1
                    sid=id_for(sym,ts)
                    snap_by_time[ts]=sid
                    feat=feature_at(i,t,arrays,extrema,cadence)
                    features_by_time[ts]=feat
                    sw.writerow(dict(snapshot_id=sid,symbol=sym,snapshot_ts=ts,
                                     split=split,quality='COMPLETE_PRE_240_1M',
                                     cadence_min=cadence,entry_open_reference=arrays[0][i],
                                     source_1m_file=sym+'_1m.csv',no_hll_conditioning='YES',**feat))
                    stats['snapshots']+=1
                    stats['snapshots_'+split]+=1
                    if is_discovery(split):
                        for sc in SCENARIOS:
                            state,hit,minobs,net=label_at(i,t,arrays,run_ends,extrema,sc)
                            row=dict(snapshot_id=sid,symbol=sym,snapshot_ts=ts,split=split,
                                     scenario=sc[0],tp_pct=sc[1],sl_pct=sc[2],horizon_min=sc[3],
                                     entry_open_reference=arrays[0][i],state=state,
                                     hit_ts=hit,minutes_observed=minobs,net_pct=net,
                                     force_phase=feat['force_phase'],base_position=feat['base_position'],
                                     label_scope='RETROSPECTIVE_NOT_CAUSAL_FEATURE')
                            ow.writerow(row)
                            labels_by_time[(ts,sc[0])]=state
                            stats['exported_outcomes']+=1
                            key=(sc[0],feat['force_phase'],feat['base_position'])
                            group[key]['states'][state]+=1
                            group[key]['symbols'].add(sym)
                    else:
                        stats['sealed_outcome_snapshots']+=1
                for ts,items in sorted(links.get(sym,{}).items()):
                    for e in items:
                        out={k:e.get(k,'') for k in LINK_FIELDS}
                        out.update(snapshot_id=snap_by_time.get(ts,''),
                                   snapshot_link_status=('MATCHED_CLOCK_GRID' if ts in snap_by_time
                                                         else 'NO_ELIGIBLE_SNAPSHOT'),
                                   link_use='RETROSPECTIVE_CROSSUP_CONTEXT_NOT_CANDIDATE_SELECTION',
                                   no_future_family_propagation='YES_ATTEMPT_PRE_ONLY')
                        lw.writerow(out)
                        if (ts in snap_by_time and is_discovery(split)
                                and e.get('is_first_in_episode')=='1'):
                            feat=features_by_time[ts]
                            for sc in SCENARIOS:
                                state=labels_by_time[(ts,sc[0])]
                                case=dict(attempt_id=e['attempt_id'],episode_id=e['episode_id'],
                                    symbol=sym,snapshot_id=snap_by_time[ts],snapshot_ts=ts,
                                    pre_pattern_96=e.get('pre_pattern_96',''),family_1p0=e.get('family_1p0',''),
                                    force_phase=feat['force_phase'],base_position=feat['base_position'],
                                    w15_eff_signed=feat['w15_eff_signed'],
                                    w30_eff_signed=feat['w30_eff_signed'],
                                    eff_delta_last15_vs_prev15=feat['eff_delta_last15_vs_prev15'],
                                    w15_net_pct=feat['w15_net_pct'],
                                    distance_last_close_to_base_high_pct=feat['distance_last_close_to_base_high_pct'],
                                    scenario=sc[0],state=state,
                                    case_scope='RETROSPECTIVE_CROSSUP_FIRST_EPISODE_ONLY')
                                cw.writerow(case)
                                group_key=(case['pre_pattern_96'],case['family_1p0'],
                                           case['force_phase'],case['scenario'])
                                g=cross_groups[group_key]
                                g['states'][state]+=1;g['symbols'].add(sym)
                                g['episodes'].add(case['episode_id']);g['snapshots'].add(case['snapshot_id'])
                                stats['discovery_crossup_scenario_cases']+=1
                        stats['crossup_links']+=1
                        if ts in snap_by_time:stats['matched_crossup_links']+=1
                print('HLL_FBD_SYMBOL '+json.dumps({'done':nr,'of':len(symbols),'symbol':sym,
                    'aligned_grid_points':scanned,'eligible_snapshots':eligible,
                    'split':split,'crossup_events':sum(map(len,links.get(sym,{}).values()))},ensure_ascii=False),flush=True)
        grows=[]
        for (sc,phase,position),g in sorted(group.items()):
            ss=g['states'];tp=ss['TP_FIRST']+ss['TP_GAP_OPEN'];sl=ss['SL_FIRST']+ss['SL_GAP_OPEN']
            resolved=tp+sl
            grows.append(dict(scenario=sc,force_phase=phase,base_position=position,
                snapshots=sum(ss.values()),symbols=len(g['symbols']),tp_first=tp,sl_first=sl,
                ambiguous_1m=ss['AMBIGUOUS_1M'],timeout=ss['TIMEOUT'],
                censored_or_missing=sum(v for k,v in ss.items() if k.startswith('CENSORED') or k.startswith('NO_')),
                tp_rate_resolved_pct=pct(100*tp/resolved) if resolved else '',
                warning='EXPLORATORY_NONINDEPENDENT_SNAPSHOTS_NOT_A_TRADING_EDGE'))
        with (tmp/'force_groups_DISCOVERY_ONLY.csv').open('x',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=GROUP_FIELDS);w.writeheader();w.writerows(grows)
        hll_groups=[]
        for (geo,family,phase,sc),g in sorted(cross_groups.items()):
            ss=g['states'];tp=ss['TP_FIRST']+ss['TP_GAP_OPEN'];sl=ss['SL_FIRST']+ss['SL_GAP_OPEN']
            other=sum(ss.values())-tp-sl
            hll_groups.append(dict(pre_pattern_96=geo,family_1p0=family,
                force_phase=phase,scenario=sc,n_first_episode_attempts=sum(ss.values()),
                distinct_snapshots=len(g['snapshots']),distinct_episodes=len(g['episodes']),
                symbols=len(g['symbols']),tp_first=tp,sl_first=sl,other=other,
                tp_rate_resolved_pct=pct(100*tp/(tp+sl)) if tp+sl else '',
                warning='RETROSPECTIVE_CROSSUP_CONDITIONED_SMALL_DEPENDENT_GROUPS'))
        with (tmp/'hll_structure_force_groups_DISCOVERY_ONLY.csv').open('x',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=CROSS_GROUP_FIELDS);w.writeheader();w.writerows(hll_groups)
        info={'version':VERSION,'scope':'CONTINUOUS_CLOCK_GRID_NOT_CONDITIONED_ON_CROSSUP',
              'symbols':len(symbols),'cadence_min':cadence,'base_lookback_closed_1m':BASE_BARS,
              'base_exclude_recent_min':BASE_EXCLUDE_RECENT_BARS,
              'scenarios':[dict(name=x[0],tp_pct=x[1],sl_pct=x[2],horizon_min=x[3]) for x in SCENARIOS],
              'fee_roundtrip_pct':FEE_ROUNDTRIP_PCT,'counts':dict(stats),
              'only_1m_before_open_for_features':True,'holdouts_outcomes_never_exported':True,
              'crossup_structures_link_only_descriptive':True,
              'caveats':['Continuously sampled timepoints on same symbol are highly dependent.',
                  'Legacy examined pilot symbols are NOT independent validation.',
                  'HLL geometry and family only exist for historical CROSS_UP attempts; never propagate them to earlier timepoints.',
                  'No synthetic fill of 1m gaps; unresolved outcomes stay censored.',
                  'Theoretical barriers ignore funding, execution slippage and position overlap.',
                  '30m/15m clock grid can miss intrabar activations; this is a discovery study, not LIVE signals.']}
        (tmp/'summary.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        out_hashes={p.name:sha(p) for p in tmp.glob('*') if p.is_file()}
        manifest={'version':VERSION,'source_sha256':{'cohort':pre_sha,'episode_map':map_sha,
                    'minute_tapes':hashes_minutes,'minute_manifests':hashes_manifests},
                  'output_sha256':out_hashes,'holdout_sealed':True,'no_live':True,
                  'no_detector_modification':True}
        (tmp/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.rename(tmp,dest)
    except Exception:
        shutil.rmtree(tmp,ignore_errors=True)
        raise
    print('HLL_FBD_FINAL '+json.dumps({'status':'DONE','output':str(dest),
        'symbols':len(symbols),'snapshots':stats['snapshots'],
        'outcomes_exported_discovery_only':stats['exported_outcomes'],
        'sealed_holdout_snapshots':stats['sealed_outcome_snapshots'],
        'crossup_links_matched':stats['matched_crossup_links'],
        'crossup_links_total':stats['crossup_links'],
        'discovery_crossup_scenario_cases':stats['discovery_crossup_scenario_cases']},ensure_ascii=False),flush=True)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    modes=ap.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true')
    modes.add_argument('--run',action='store_true')
    ap.add_argument('--root',default='/home/multiservices-1m')
    ap.add_argument('--scope',choices=('pilot15','full290'),default='pilot15')
    ap.add_argument('--cadence-min',type=int,choices=(5,15,30),default=15)
    ap.add_argument('--symbols',help='Comma-separated symbols, requires --output')
    ap.add_argument('--output',help='New output directory, never overwrites')
    ap.add_argument('--cohort',default=COHORT)
    ap.add_argument('--map',default=MAP)
    ap.add_argument('--minute',default=MINUTE)
    ap.add_argument('--minute-manifest',default=MINUTE_MANIFEST)
    args=ap.parse_args(argv)
    if args.symbols and not args.output:ap.error('--symbols requires a distinct --output')
    pp=paths(args.root,args)
    subset=parse_symfile(args.scope,args.symbols)
    if args.plan:
        missing_minute=([s for s in subset if not (pp['minute']/(s+'_1m.csv')).is_file()]
                        if subset is not None else 'CHECK_DURING_RUN')
        missing_meta=([s for s in subset if not (pp['minute_manifest']/(s+'.json')).is_file()]
                      if subset is not None else 'CHECK_DURING_RUN')
        print('HLL_FBD_PLAN '+json.dumps({'version':VERSION,'scope':args.scope,
            'cadence_min':args.cadence_min,'selected_symbols':list(subset) if subset else 'ALL_FROM_FROZEN_290',
            'missing_minute_symbols':missing_minute,'missing_minute_manifest_symbols':missing_meta,
            'input_presence':{k:pp[k].exists() for k in ('cohort','map','minute','minute_manifest')},
            'output':str(pp['output']),'output_exists':pp['output'].exists(),
            'scenarios':[x[0] for x in SCENARIOS],
            'no_hll_crossup_conditioning':True,'no_live':True,
            'holdout_labels_sealed':True,'reads_1m':True},ensure_ascii=False))
        return 0
    selected,events,source_hash,map_hash=source_check(pp,subset)
    run(pp,selected,events,source_hash,map_hash,args.cadence_min)
    return 0

if __name__=='__main__':raise SystemExit(main())
