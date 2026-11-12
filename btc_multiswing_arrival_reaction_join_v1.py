#!/usr/bin/env python3
"""Multi-Swing Arrival × REACTION First Touch V1, OFFLINE and causally separated.

Reads immutable Arrival V1 research outputs and canonical read-only First Touch
journal. No Streamlit, trading, Redis, network or modifications to production.

The geometric relationship means a REACTION swing level lies near the *stage*
known at the arrival PRE cutoff. It does NOT prove that both signals refer to
an identical physical touch or that REACTION is a validated trade trigger.

PRE: status and swing-level known_ts <= arrival bar OPEN; no subsequent event.
POST: new First Touch classifications (REACTION/TOUCH_INDECISIVE/TOUCH_FAILED)
with known_ts > arrival bar OPEN and <= fixed horizon, separately exported.
A First Touch status becomes known after its retest candle CLOSE (known_ts).
The retrospective arrival selection is NOT an advance detection signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from btc_multiswing_arrival_lab_v1 import VERSION as ARRIVAL_VERSION

VERSION = 'multiswing-arrival-reaction-first-touch-join-v1'
M = 60_000
DEFAULT_ARRIVAL = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_arrival_lab_v1')
DEFAULT_JOURNAL = Path('reports/reaction_lab/scanner_first_touches_v1.sqlite3')
DEFAULT_OUT = Path('reports/reaction_lab/btc_multiswing_60d_v1/btc_sep09_nov08_arrival_reaction_join_v1')
STATUSES = ('REACTION', 'TOUCH_INDECISIVE', 'TOUCH_FAILED')
PRE_LOOKBACK_MINUTES = (60, 240)
POST_HORIZON_MINUTES = 180
DISTANCE_TOLERANCE_PCT = 0.10  # predeclared descriptive tolerance, not optimized
PRE_REQUIRED = {
    'event_id', 'region_id', 'visit_number', 'side', 'pre_cutoff_ts',
    'stage_present_at_pre', 'current_stage_low', 'current_stage_high',
    'reference_mismatch_possible', 'execution_eligibility'
}
JOIN_PRE_COLUMNS = [
    'event_id','region_id','visit_number','side','pre_cutoff_ts',
    'stage_present_at_pre','reference_mismatch_possible',
    'spatial_reference','matching_status',
    'pre_near_known_60m','pre_near_known_240m',
    'pre_aligned_reaction_240m','pre_aligned_indecisive_240m',
    'pre_aligned_failed_240m','pre_opposite_known_240m',
    'pre_last_near_known_ts','pre_last_near_touch_key',
    'pre_near_unpriced_events_240m',
    'research_note'
]
POST_SUMMARY_COLUMNS = [
    'event_id','region_id','visit_number','pre_cutoff_ts',
    'stage_present_at_pre','post_available_minutes','post_180m_coverage',
    'post_near_5m','post_near_15m','post_near_30m',
    'post_near_60m','post_near_180m',
    'post_first_near_known_ts','post_first_aligned_reaction_known_ts',
    'post_near_unpriced_events_180m',
]

JOIN_POST_COLUMNS = [
    'event_id','region_id','visit_number','side','pre_cutoff_ts',
    'reaction_touch_key','reaction_side','reaction_status','retest_ts',
    'reaction_known_ts','minutes_after_arrival_open','swing_price',
    'stage_low_at_arrival_pre','stage_high_at_arrival_pre',
    'price_gap_from_stage_pct','price_relation','direction_relation',
    'matched_by','research_note'
]


def _sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def _bool_strict(value, field: str) -> bool:
    if isinstance(value, (bool,np.bool_)):
        return bool(value)
    if str(value).strip().lower() in ('true','1'):
        return True
    if str(value).strip().lower() in ('false','0'):
        return False
    raise ValueError('ARRIVAL_INVALID_BOOLEAN: '+field)


def _check_arrivals(pre: pd.DataFrame, *, freeze_end: int | None = None) -> pd.DataFrame:
    miss=PRE_REQUIRED-set(pre.columns)
    if miss:
        raise ValueError('ARRIVAL_PRE_SCHEMA_MISSING: '+repr(sorted(miss)))
    if pre.event_id.duplicated().any() or pre[['region_id','visit_number']].duplicated().any():
        raise ValueError('ARRIVAL_DUPLICATED_EVENT_OR_VISIT')
    p=pre.copy()
    if p.empty:
        raise ValueError('ARRIVAL_EMPTY_NOT_AN_ABSENCE_OF_SIGNAL')
    p['pre_cutoff_ts']=pd.to_numeric(p.pre_cutoff_ts,errors='raise').astype('int64')
    if (p.pre_cutoff_ts % M).any() or (p.pre_cutoff_ts<0).any():
        raise ValueError('ARRIVAL_INVALID_CUTOFF')
    if freeze_end is not None and (p.pre_cutoff_ts>=freeze_end).any():
        raise ValueError('ARRIVAL_CUTOFF_AFTER_FREEZE')
    p['stage_present_at_pre']=p.stage_present_at_pre.map(
        lambda x:_bool_strict(x,'stage_present_at_pre'))
    p['reference_mismatch_possible']=p.reference_mismatch_possible.map(
        lambda x:_bool_strict(x,'reference_mismatch_possible'))
    if not p.side.isin(['LOW','HIGH']).all():
        raise ValueError('ARRIVAL_SIDE_INVALID')
    for _,r in p.iterrows():
        lo,hi=r.current_stage_low,r.current_stage_high
        if bool(r.stage_present_at_pre):
            if pd.isna(lo) or pd.isna(hi) or not (0<float(lo)<=float(hi) and
                    math.isfinite(float(lo)) and math.isfinite(float(hi))):
                raise ValueError('ARRIVAL_MISSING_PRESENT_STAGE')
        elif pd.notna(lo) or pd.notna(hi):
            raise ValueError('ARRIVAL_FALSE_STAGE_HAS_GEOMETRY')
    return p.sort_values(['pre_cutoff_ts','event_id'],kind='mergesort').reset_index(drop=True)


def _parse_price(data: str | None):
    """Parse explicitly causal fields only, never a future price outcome."""
    if not data:
        return None
    try:
        d=json.loads(data)
    except (ValueError,TypeError):
        raise ValueError('REACTION_INVALID_CAUSAL_JSON')
    if not isinstance(d,dict):
        raise ValueError('REACTION_INVALID_CAUSAL_JSON')
    for name in ('swing_price','pivot_price'):
        if name in d and d[name] is not None:
            try:
                value=float(d[name])
            except (TypeError,ValueError):
                continue
            if math.isfinite(value) and value>0:
                return value
    return None


def read_reaction_journal(path:Path, *, freeze_end:int, symbol:str='BTCUSDT',
                          lookback_minutes:int=240) -> tuple[pd.DataFrame,dict]:
    """Read canonical first_touches journal in SQLite *read only*.

    No REACTION classification is treated as PRE before known_ts (retest close).
    """
    if symbol!='BTCUSDT':
        raise ValueError('REACTION_BTC_FROZEN_SCOPE_ONLY')
    if not path.is_file():
        raise FileNotFoundError(f'REACTION_JOURNAL_NOT_FOUND: {path}')
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
    try:
        con.execute('PRAGMA query_only=ON')
        names={r[1] for r in con.execute('PRAGMA table_info(first_touches)')}
        needed={'touch_key','symbol','side','status','retest_ts','known_ts','causal_json'}
        if not needed.issubset(names):
            raise ValueError('REACTION_JOURNAL_SCHEMA_MISSING: '+repr(sorted(needed-names)))
        # Read the whole BTC-universe up to freeze end and slightly earlier than
        # start (symbol count is bounded). Avoid SQLite writes, side effects.
        rows=con.execute('SELECT touch_key,symbol,side,status,retest_ts,known_ts,causal_json '
                         'FROM first_touches WHERE symbol=? AND known_ts<=? '
                         'ORDER BY known_ts,touch_key',(symbol,int(freeze_end))).fetchall()
    finally:
        con.close()
    entries=[]
    for key,sym,side,status,retest,known,payload in rows:
        entries.append(dict(touch_key=str(key),symbol=str(sym),side=str(side),
                            status=str(status),retest_ts=int(retest),known_ts=int(known),
                            swing_price=_parse_price(payload)))
    return _check_reactions(pd.DataFrame(entries,columns=['touch_key','symbol','side',
        'status','retest_ts','known_ts','swing_price']),freeze_end=freeze_end),{
        'source':'CANONICAL_SCANNER_FIRST_TOUCHES_SQLITE_READONLY',
        'source_path':str(path.resolve()),'observations_loaded':len(entries),
        'snapshot_sha256':hashlib.sha256(json.dumps(entries,sort_keys=True,
            separators=(',',':')).encode('utf-8')).hexdigest(),
    }


def _check_reactions(events:pd.DataFrame, *, freeze_end:int|None=None)->pd.DataFrame:
    required={'touch_key','symbol','side','status','retest_ts','known_ts','swing_price'}
    missing=required-set(events.columns)
    if missing:
        raise ValueError('REACTION_SCHEMA_MISSING: '+repr(sorted(missing)))
    x=events.copy()
    if x.touch_key.isna().any() or x.touch_key.astype(str).eq('').any() or x.touch_key.duplicated().any():
        raise ValueError('REACTION_DUPLICATED_OR_EMPTY_TOUCH_KEY')
    if not x.side.isin(['LONG','SHORT']).all() or not x.status.isin(STATUSES).all() or x.symbol.ne('BTCUSDT').any():
        raise ValueError('REACTION_INVALID_SIDE_STATUS_OR_SYMBOL')
    for key in ('retest_ts','known_ts'):
        x[key]=pd.to_numeric(x[key],errors='raise').astype('int64')
    if ((x.retest_ts%M)!=0).any() or ((x.known_ts-x.retest_ts)!=M).any():
        raise ValueError('REACTION_INVALID_CLOSED_BAR_KNOWN_TS')
    if freeze_end is not None and (x.known_ts>freeze_end).any():
        raise ValueError('REACTION_FUTURE_AFTER_FREEZE')
    x['swing_price']=pd.to_numeric(x.swing_price,errors='coerce')
    if ((x.swing_price.notna()) &
            ((x.swing_price<=0) | (~np.isfinite(x.swing_price)))).any():
        raise ValueError('REACTION_INVALID_SWING_PRICE')
    return x.sort_values(['known_ts','touch_key'],kind='mergesort').reset_index(drop=True)


def _gap_pct(price:float,low:float,high:float)->float:
    center=(low+high)/2
    return max(0.0,low-price,price-high)/center*100


def _relation(price:float,low:float,high:float)->str:
    return 'BELOW' if price<low else ('ABOVE' if price>high else 'INSIDE')


def join_arrivals_to_reactions(arrival_pre:pd.DataFrame,events:pd.DataFrame,
                               *, freeze_end:int,
                               tolerance_pct:float=DISTANCE_TOLERANCE_PCT,
                               post_horizon:int=POST_HORIZON_MINUTES):
    """Pure deterministic PRE and POST split, no subjective REACTION renaming.

    The post match is a *spatial/temporal proximity* to a prior-confirmed stage,
    NOT a shared physical contact identity, trade entry or REACTION causation.
    """
    if not (0<=tolerance_pct<=2.0) or not isinstance(post_horizon,int) or not (1<=post_horizon<=1440):
        raise ValueError('REACTION_JOIN_INVALID_FIXED_PARAMETERS')
    if freeze_end%M or freeze_end<=0:
        raise ValueError('REACTION_JOIN_INVALID_FREEZE_END')
    p=_check_arrivals(arrival_pre,freeze_end=freeze_end)
    rx=_check_reactions(events,freeze_end=freeze_end)
    times=rx.known_ts.to_numpy(dtype=np.int64)
    out=[]; matches=[]; post_summaries=[]
    for r in p.itertuples(index=False):
        cutoff=int(r.pre_cutoff_ts)
        entry={k:None for k in JOIN_PRE_COLUMNS}
        post_entry={k:None for k in POST_SUMMARY_COLUMNS}
        post_entry.update(event_id=r.event_id,region_id=r.region_id,
                          visit_number=int(r.visit_number),pre_cutoff_ts=cutoff,
                          stage_present_at_pre=bool(r.stage_present_at_pre))
        entry.update(event_id=r.event_id,region_id=r.region_id,
                    visit_number=int(r.visit_number),side=r.side,pre_cutoff_ts=cutoff,
                    stage_present_at_pre=bool(r.stage_present_at_pre),
                    reference_mismatch_possible=bool(r.reference_mismatch_possible),
                    spatial_reference='ASOF_CURRENT_MULTI_SWING_STAGE',
                    matching_status='UNKNOWN_NO_PRE_STAGE',
                    research_note='No original REACTION identity or trade trigger is assumed')
        if not r.stage_present_at_pre:
            out.append(entry)
            post_summaries.append(post_entry)
            continue
        low=float(r.current_stage_low);high=float(r.current_stage_high)
        desired='LONG' if r.side=='LOW' else 'SHORT'
        pre_left=int(np.searchsorted(times,cutoff-max(PRE_LOOKBACK_MINUTES)*M,side='right'))
        pre_right=int(np.searchsorted(times,cutoff,side='right'))
        post_right=int(np.searchsorted(times,min(freeze_end,cutoff+post_horizon*M),side='right'))
        # Missing swing price is UNKNOWN proximity, not a proven negative.
        available_minutes=min(post_horizon,max(0,(freeze_end-cutoff)//M))
        post_entry['post_available_minutes']=available_minutes
        post_entry['post_180m_coverage']=('FULL_HORIZON' if available_minutes>=180
                                     else 'CUTOFF_CENSORED')
        earlier=rx.iloc[pre_left:pre_right]
        future=rx.iloc[pre_right:post_right]
        priced_pre=earlier.loc[earlier.swing_price.notna()].copy()
        priced_post=future.loc[future.swing_price.notna()].copy()
        for df in (priced_pre,priced_post):
            if not df.empty:
                df['gap_pct']=df.swing_price.map(lambda price:_gap_pct(float(price),low,high))
        near_pre=(priced_pre.loc[priced_pre.gap_pct<=tolerance_pct].copy()
                  if not priced_pre.empty else priced_pre)
        near_post=(priced_post.loc[priced_post.gap_pct<=tolerance_pct].copy()
                   if not priced_post.empty else priced_post)
        recent_pre=near_pre.loc[near_pre.known_ts>cutoff-60*M]
        entry.update(
            matching_status='STAGE_PRESENT_SPATIAL_MATCH_ONLY',
            pre_near_known_60m=len(recent_pre),pre_near_known_240m=len(near_pre),
            pre_aligned_reaction_240m=int(((near_pre.side==desired)&(near_pre.status=='REACTION')).sum()),
            pre_aligned_indecisive_240m=int(((near_pre.side==desired)&(near_pre.status=='TOUCH_INDECISIVE')).sum()),
            pre_aligned_failed_240m=int(((near_pre.side==desired)&(near_pre.status=='TOUCH_FAILED')).sum()),
            pre_opposite_known_240m=int(near_pre.side.ne(desired).sum()),
            pre_near_unpriced_events_240m=int(earlier.swing_price.isna().sum()),
            )
        post_entry['post_near_unpriced_events_180m']=int(future.swing_price.isna().sum())
        for h in (5,15,30,60,180):
            if h<=post_horizon and cutoff+h*M<=freeze_end:
                post_entry[f'post_near_{h}m']=int((near_post.known_ts<=cutoff+h*M).sum())
        if not near_pre.empty:
            last=near_pre.iloc[-1]
            entry['pre_last_near_known_ts']=int(last.known_ts)
            entry['pre_last_near_touch_key']=str(last.touch_key)
        if not near_post.empty:
            post_entry['post_first_near_known_ts']=int(near_post.iloc[0].known_ts)
        aligned=near_post.loc[(near_post.side==desired)&(near_post.status=='REACTION')]
        if not aligned.empty:
            post_entry['post_first_aligned_reaction_known_ts']=int(aligned.iloc[0].known_ts)
        out.append(entry)
        post_summaries.append(post_entry)
        for event in near_post.itertuples(index=False):
            matches.append(dict(
                event_id=r.event_id,region_id=r.region_id,visit_number=int(r.visit_number),
                side=r.side,pre_cutoff_ts=cutoff,
                reaction_touch_key=event.touch_key,reaction_side=event.side,
                reaction_status=event.status,retest_ts=int(event.retest_ts),
                reaction_known_ts=int(event.known_ts),
                minutes_after_arrival_open=int((int(event.known_ts)-cutoff)//M),
                swing_price=float(event.swing_price),
                stage_low_at_arrival_pre=low,stage_high_at_arrival_pre=high,
                price_gap_from_stage_pct=float(event.gap_pct),
                price_relation=_relation(float(event.swing_price),low,high),
                direction_relation='ALIGNED' if event.side==desired else 'OPPOSITE',
                matched_by='SWING_PRICE_NEAR_PRIOR_STAGE_NO_SHARED_TOUCH_ASSERTION',
                research_note='POST ONLY: known at retest close; no order/fill implied'))
    pre_df=pd.DataFrame(out,columns=JOIN_PRE_COLUMNS)
    post_summary_df=pd.DataFrame(post_summaries,columns=POST_SUMMARY_COLUMNS)
    post_df=pd.DataFrame(matches,columns=JOIN_POST_COLUMNS)
    if (len(pre_df)!=len(p) or len(post_summary_df)!=len(p) or
        pre_df.event_id.duplicated().any() or post_summary_df.event_id.duplicated().any()):
        raise ValueError('REACTION_JOIN_LOST_ARRIVAL_ROWS')
    if not post_df.empty and (
            (post_df.reaction_known_ts<=post_df.pre_cutoff_ts).any() or
            ((post_df.reaction_known_ts-post_df.pre_cutoff_ts)>post_horizon*M).any()):
        raise ValueError('REACTION_POST_CROSSED_PRE_BOUNDARY')
    counts={
        'arrival_rows':len(p), 'reaction_event_rows_in_scope':len(rx),
        'reaction_price_missing':int(rx.swing_price.isna().sum()),
        'stage_known_at_pre':int(p.stage_present_at_pre.sum()),
        'arrival_full_post_180m':int(post_summary_df['post_180m_coverage'].eq('FULL_HORIZON').sum()),
        'arrival_post_180m_censored':int(post_summary_df['post_180m_coverage'].eq('CUTOFF_CENSORED').sum()),
        'arrival_rows_with_post_near_event_full_180m':int((pd.to_numeric(
            post_summary_df['post_near_180m'],errors='coerce')>0).sum()),
        'post_spatial_pair_rows':len(post_df),
        'post_unique_touch_keys':int(post_df.reaction_touch_key.nunique()) if len(post_df) else 0,
        'post_status_counts':{s:int(post_df.reaction_status.eq(s).sum()) for s in STATUSES} if len(post_df) else {s:0 for s in STATUSES},
        'post_aligned_reaction_pair_rows':int(((post_df.reaction_status=='REACTION')&
                                             (post_df.direction_relation=='ALIGNED')).sum()) if len(post_df) else 0,
    }
    return pre_df,post_summary_df,post_df,counts


def load_arrival_run(root:Path):
    """Verify the original immutable Arrival files; no rerun or overwrite."""
    mp=root/'manifest.json'
    if not mp.is_file():
        raise FileNotFoundError(f'ARRIVAL_V1_NOT_RUN: {mp}')
    manifest=json.loads(mp.read_text(encoding='utf-8'))
    if (manifest.get('version')!=ARRIVAL_VERSION or
        manifest.get('immutable_run') is not True or
        manifest.get('research_only') is not True or
        manifest.get('no_live') is not True):
        raise ValueError('ARRIVAL_V1_MANIFEST_INVALID')
    expected=('arrival_pre_v1.csv','arrival_post_outcomes_v1.csv','summary_v1.json')
    for name in expected:
        if manifest.get('sha256',{}).get(name)!=_sha(root/name):
            raise ValueError(f'ARRIVAL_V1_HASH_CHANGED: {name}')
    summary=json.loads((root/'summary_v1.json').read_text(encoding='utf-8'))
    if (summary.get('version')!=ARRIVAL_VERSION or
        summary.get('source_provenance')!=manifest.get('source_provenance')):
        raise ValueError('ARRIVAL_V1_SUMMARY_OR_PROVENANCE_INVALID')
    pre=pd.read_csv(root/'arrival_pre_v1.csv',low_memory=False)
    end=int(summary['freeze_end_exclusive'])
    p=_check_arrivals(pre,freeze_end=end)
    if len(p)!=int(manifest['counts']['arrivals']):
        raise ValueError('ARRIVAL_V1_COUNT_DRIFT')
    return p,manifest,summary


def _write_immutable(out:Path,pre:pd.DataFrame,post_summary:pd.DataFrame,
                     post:pd.DataFrame,counts:dict,
                     arrival_path:Path,arrival_manifest:dict,journal_meta:dict,
                     settings:dict):
    if out.exists():
        raise FileExistsError(f'REACTION_JOIN_OUTPUT_EXISTS_IMMUTABLE: {out}')
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.reaction_join_tmp_',dir=out.parent))
    try:
        pre.to_csv(tmp/'arrival_reaction_pre_v1.csv',index=False,float_format='%.12g')
        post_summary.to_csv(tmp/'arrival_reaction_post_summary_v1.csv',index=False,float_format='%.12g')
        post.to_csv(tmp/'arrival_reaction_post_matches_v1.csv',index=False,float_format='%.12g')
        summary={'version':VERSION,'counts':counts,'settings':settings,
                 'arrival_parent':str(arrival_path),
                 'arrival_parent_manifest_sha256':_sha(arrival_path/'manifest.json'),
                 'reaction_journal_snapshot':journal_meta,
                 'limitations':[
                     'Not all historical birth-band arrivals touch the geometrical stage present at PRE.',
                     'REACTION spatial proximity to stage is NOT an identical touch nor a proven trigger.',
                     'PRE only counts already known first touches at or before arrival OPEN.',
                     'POST is separate; a REACTION at retest close does not authorize entry before close.',
                     'Reaction source is a read-only evolving journal snapshot, not a frozen cohort.',
                     'POST windows truncated by the freeze carry CUTOFF_CENSORED, not zero outcomes.',
                     'Missing swing prices are UNKNOWN proximity, not evidence of zero confluence.',
                     'Duplicated spatial matches across bands are dependent, not independent successes.',
                     'No HLL, fill/TP/SL, PnL or LIVE execution is implemented.',
                 ]}
        (tmp/'summary_v1.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False,
                      sort_keys=True)+'\n',encoding='utf-8')
        payloads=('arrival_reaction_pre_v1.csv','arrival_reaction_post_summary_v1.csv',
                  'arrival_reaction_post_matches_v1.csv','summary_v1.json')
        manifest={'version':VERSION,'immutable_run':True,'no_live':True,
                  'arrival_parent_manifest_sha256':_sha(arrival_path/'manifest.json'),
                  'reaction_journal_sha256':journal_meta['snapshot_sha256'],
                  'sha256':{f:_sha(tmp/f) for f in payloads}}
        (tmp/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        os.rename(tmp,out)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    parser.add_argument('--project-root',type=Path,default=Path('.'))
    parser.add_argument('--arrival-run',type=Path,default=DEFAULT_ARRIVAL)
    parser.add_argument('--reaction-db',type=Path,default=DEFAULT_JOURNAL)
    parser.add_argument('--output-dir',type=Path,default=DEFAULT_OUT)
    args=parser.parse_args(argv)
    root=args.project_root.resolve()
    def resolve(path):return path if path.is_absolute() else root/path
    arrival,db,dest=map(resolve,(args.arrival_run,args.reaction_db,args.output_dir))
    print('ARRIVAL_REACTION_JOIN_V1_PLAN',json.dumps({
        'mode':'RUN' if args.run else 'PLAN','arrival':str(arrival),
        'reaction_db':str(db),'output_dir':str(dest),
        'tolerance_pct':DISTANCE_TOLERANCE_PCT,
        'pre_lookback_minutes':list(PRE_LOOKBACK_MINUTES),
        'post_horizon_minutes':POST_HORIZON_MINUTES,
        'immutable':True,'no_live':True,
        'arrival_present':(arrival/'manifest.json').is_file(),
        'reaction_db_present':db.is_file()},sort_keys=True),flush=True)
    if args.plan:
        return 0
    if dest.exists():
        raise FileExistsError(f'REACTION_JOIN_OUTPUT_EXISTS_IMMUTABLE: {dest}')
    pre,manifest,summary=load_arrival_run(arrival)
    end=int(summary['freeze_end_exclusive'])
    reactions,meta=read_reaction_journal(db,freeze_end=end)
    if reactions.empty:
        raise ValueError('REACTION_NO_BTC_EVENTS_IN_FREEZE_CANNOT_DECLARE_ZERO')
    if not reactions.swing_price.notna().any():
        raise ValueError('REACTION_NO_CAUSAL_SWING_PRICE_CANNOT_SPATIALLY_JOIN')
    pre_join,post_summary,post_join,counts=join_arrivals_to_reactions(
        pre,reactions,freeze_end=end)
    _write_immutable(dest,pre_join,post_summary,post_join,counts,arrival,manifest,meta,
        {'tolerance_pct':DISTANCE_TOLERANCE_PCT,
         'pre_lookback_minutes':list(PRE_LOOKBACK_MINUTES),
         'post_horizon_minutes':POST_HORIZON_MINUTES})
    print('ARRIVAL_REACTION_JOIN_V1_FINISHED',json.dumps({'output':str(dest),
          'counts':counts},sort_keys=True),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
