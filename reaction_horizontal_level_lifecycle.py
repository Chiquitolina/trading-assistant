#!/usr/bin/env python3
"""PRE-only lifecycle of the SAME Atlas V2 zone selected by PRE proximity.

Research-only, read-only sources. No outcome labels, no REACTION candle, no
recalculation of Atlas levels. Uses CLOSED contiguous 15m/30m candles whose
close_ts <= the retest 1m open_ts. All states are hypotheses, not entry signals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import pandas as pd

from reaction_horizontal_level_research import build_pre_roles, finite

VERSION = 'horizontal-level-lifecycle-pre-v1.0'
OUTPUT = 'horizontal_level_lifecycle_pre_v1'
STEPS = {'15m': 900_000, '30m': 1_800_000}
ACTIVE = 'NIVEL_VIGENTE_PRE'
LOST = 'NIVEL_PERDIDO_PRE'
FAILED = 'RETEST_FALLIDO_PRE'
RECLAIMED = 'NIVEL_RECUPERADO_PRE'
UNKNOWN = 'INDETERMINADO_PRE'
NO_NEAR = 'SIN_NIVEL_IMPORTANTE_CERCANO_PRE'
LABELS = {
    ACTIVE: 'Nivel vigente (sin pérdida confirmada)',
    LOST: 'Nivel perdido / resistencia superada PRE',
    FAILED: 'Retest fallido tras pérdida PRE',
    RECLAIMED: 'Nivel recuperado PRE',
    UNKNOWN: 'Indeterminado / histórico insuficiente',
    NO_NEAR: 'Sin nivel importante cercano PRE',
}
FIELDS = [
    'tf_filter','event_key','symbol','side','date_utc','retest_ts','zone_id','timeframe',
    'zone_low','zone_high','role_pre','role_distance_atr','lifecycle_state',
    'history_quality','initial_side','current_confirmed_side',
    'initial_acceptance_close_ts','last_break_close_ts','last_reclaim_close_ts',
    'last_failed_retest_close_ts','last_pre_close_ts','last_pre_close',
    'bars_since_formation','independent_failed_retests','reason'
]


def _side_from_close(c, low, high):
    return 'ABOVE' if c > high else 'BELOW' if c < low else 'INSIDE'


def classify_zone_history(zone, raw_bars, retest_ts, *, complete=True):
    """Classify zone's price history with confirm-close at two HTF bars.

    Initial side: first two consecutive closes wholly outside on same side
    AFTER frozen level formed; transition requires two closes wholly across
    opposite boundary. Failed retest: a later closed candle wicks into the
    zone while closing back outside on the lost side. Initial role inferred
    from closes, not from original pivot HIGH/LOW or REACTION direction.
    """
    out = dict(lifecycle_state=UNKNOWN, history_quality='UNKNOWN',
       initial_side=None,current_confirmed_side=None, initial_acceptance_close_ts=None,
       last_break_close_ts=None,last_reclaim_close_ts=None,
       last_failed_retest_close_ts=None,last_pre_close_ts=None,last_pre_close=None,
       bars_since_formation=0,independent_failed_retests=0,reason='')
    def stop(reason):
        out['reason']=reason
        return out
    low,high,form = (finite(zone.get('zone_low')),finite(zone.get('zone_high')),
                     finite(zone.get('formation_closed_ts')))
    step=STEPS.get(str(zone.get('timeframe')))
    r=finite(retest_ts)
    if not complete or step is None or None in (low,high,form,r) or not (0<low<=high and form<r):
        return stop('INVALID_COVERAGE_LEVEL_OR_TIME')
    if raw_bars is None or raw_bars.empty or not set(('timestamp','open','high','low','close')).issubset(raw_bars.columns):
        return stop('NO_HISTORY')
    # Strictly closed before the 1m REACTION open; exclude the current 15/30m bar.
    df=raw_bars[(raw_bars.timestamp.astype('int64')+step<=int(r)) &
                (raw_bars.timestamp.astype('int64')+step>int(form))].copy().sort_values('timestamp')
    out['bars_since_formation']=len(df)
    if len(df)<2:
        return stop('LESS_THAN_TWO_POST_FORMATION_CLOSED_BARS')
    stamps=df.timestamp.to_numpy(dtype='int64')
    # Must include LAST closed bar before retest, otherwise a cached history gap
    # could create a false lost/reclaimed/vigente label.
    # Latest eligible candle closed at floor(retest_ts/step)*step.
    expected_last=(int(r)//step)*step
    if int(stamps[0])!=int(form):
        return stop('MISSING_FIRST_HTF_BAR_AFTER_FORMATION')
    if stamps[-1]+step!=expected_last:
        return stop('MISSING_FINAL_HTF_CLOSED_BAR')
    if len(df)>1 and (pd.Series(stamps).diff().dropna()!=step).any():
        return stop('GAPPED_HTF_HISTORY')
    for c in ('open','high','low','close'):
        if not all(pd.notna(df[c])) or not all(df[c].apply(lambda x:finite(x) is not None)):
            return stop('NAN_HTF_BAR')
    if (df[['open','high','low','close']]<=0).any().any() or (df.low>df.high).any() or (
        (df.open<df.low)|(df.open>df.high)|(df.close<df.low)|(df.close>df.high)).any():
        return stop('INVALID_HTF_OHLC')
    out['history_quality']='COMPLETE_POST_FORMATION'
    initial=None;stable=None;previous_side=None
    last_transition_i=None
    n_failed=0;last_failed_i=None
    last_break_i=None;loss_seen=False
    for i,row in enumerate(df.itertuples(index=False)):
        close_ts=int(row.timestamp)+step
        s=_side_from_close(float(row.close),low,high)
        prev=previous_side
        previous_side=s
        if s in ('ABOVE','BELOW') and prev==s and stable!=s:
            old=stable
            stable=s
            last_transition_i=i
            if initial is None:
                initial=s
                out['initial_side']=s
                out['initial_acceptance_close_ts']=close_ts
            elif s != initial:
                loss_seen=True
                last_break_i=i
                last_failed_i=None
                n_failed=0
                out['last_break_close_ts']=close_ts
            elif old != initial:
                out['last_reclaim_close_ts']=close_ts
        # The break confirmation candle is not also a new retest.
        if (initial and stable!=initial and last_break_i is not None
                and i>last_break_i and s==stable):
            touched=(row.high>=low if stable=='BELOW' else row.low<=high)
            rejected=(row.close<low if stable=='BELOW' else row.close>high)
            if touched and rejected:
                # Consecutive touch bars are a single episode.
                if last_failed_i is None or i-last_failed_i>1:
                    n_failed+=1
                last_failed_i=i
                out['last_failed_retest_close_ts']=close_ts
    last=df.iloc[-1]
    out['last_pre_close_ts']=int(last.timestamp)+step
    out['last_pre_close']=float(last.close)
    out['current_confirmed_side']=stable
    out['independent_failed_retests']=n_failed
    if initial is None or stable is None:
        return stop('NO_INITIAL_TWO_CLOSES_ACCEPTANCE')
    last_side=_side_from_close(float(last.close),low,high)
    if last_side!=stable:
        return stop('LAST_BAR_CONTESTS_CONFIRMED_SIDE')
    if stable==initial:
        out['lifecycle_state']=RECLAIMED if loss_seen else ACTIVE
    else:
        out['lifecycle_state']=FAILED if n_failed>0 else LOST
    out['reason']='CLASSIFIED_FROM_CLOSED_PRE_HTF_ONLY'
    return out


def classify_one(event,role,zone,history):
    result={k:None for k in FIELDS}
    result.update({k:event.get(k) for k in ('event_key','symbol','side','date_utc','retest_ts')})
    result.update(zone_id=role.get('role_zone_id'),timeframe=role.get('role_timeframe'),
       zone_low=role.get('role_zone_low'),zone_high=role.get('role_zone_high'),
       role_pre=role.get('pre_role'),role_distance_atr=role.get('role_distance_atr'))
    if pd.isna(result['zone_id']):
        result.update(lifecycle_state=NO_NEAR if role.get('pre_role')=='PRE_SIN_NIVEL_CERCANO' else UNKNOWN,
                      history_quality='NO_SELECTED_ZONE',reason='NO_QUALIFIED_NEAR_PRE_LEVEL')
        return result
    if zone is None or history is None:
        result.update(lifecycle_state=UNKNOWN,history_quality='MISSING_ZONE_OR_SOURCE',reason='MISSING_ZONE_OR_SOURCE')
        return result
    computed=classify_zone_history(zone,history,int(event['retest_ts']),complete=event.get('all_covered')==1)
    result.update(computed)
    return result


def build_lifecycle(events, levels, histories, tf='TODOS'):
    """Pure deterministic join, useful for offline tests and replay diagnostics.

    histories: dict (symbol,timeframe)->DataFrame timestamp/open/high/low/close
    """
    roles=build_pre_roles(events,levels,tf)
    by_level={(str(r['event_key']),str(r['zone_id'])):r for r in levels.to_dict('records')}
    by_role=roles.set_index('event_key')
    output=[]
    for ev in events.to_dict('records'):
        role=by_role.loc[ev['event_key']].to_dict()
        z=by_level.get((str(ev['event_key']),str(role.get('role_zone_id'))))
        hist=histories.get((str(ev['symbol']),str(role.get('role_timeframe')))) if z else None
        row=classify_one(ev,role,z,hist)
        row['tf_filter']=tf
        output.append(row)
    return pd.DataFrame(output,columns=FIELDS)


def _digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def _atomic_csv(path,frame):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.level_lifecycle_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            frame.to_csv(f,index=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def run(root):
    """Offline read-only to SQLite. Writes only to independent OUTPUT folder."""
    import reaction_structural_level_atlas_v2 as atlas
    from dashboard.views.reaction_structural_level_atlas_v2_view import load
    folder=Path(root)/atlas.v1.lineage.ROOT_REL/atlas.OUTPUT
    levels,events,_=load(root)
    # Stream one symbol at a time: avoid keeping all HTF histories resident in
    # RAM (VPS historically has limited memory and parallel LIVE processes).
    roles=[build_pre_roles(events,levels,tf) for tf in ('TODOS','15m','30m')]
    selected_frames=[r.loc[r.role_zone_id.notna()] for r in roles]
    selected_frames=[r for r in selected_frames if not r.empty]
    chosen=pd.concat(selected_frames,ignore_index=True) if selected_frames else roles[0].iloc[:0]
    chosen=chosen.merge(events[['event_key','symbol']],on='event_key')
    symbols=sorted(set(events.symbol))
    symbols_needing_history=set(chosen.symbol)
    paths=(Path(root)/atlas.v1.lineage.ROOT_REL/atlas.v1.lineage.CTX_NAME,
           Path(root)/atlas.v1.lineage.ROOT_REL/atlas.v1.lineage.HISTORY_NAME)
    cache=atlas.v1.lineage.ro(paths[1]) if paths[1].is_file() else None
    frames=[]
    try:
        with closing(atlas.v1.lineage.ro(paths[0])) as ctx:
            for j,sym in enumerate(symbols,1):
                history={}
                if sym in symbols_needing_history:
                    p15,p30,_=atlas.v1.v4._merge_tf(ctx,cache,sym)
                    history[(sym,'15m')]=p15.reset_index()
                    history[(sym,'30m')]=p30.reset_index()
                ev=events.loc[events.symbol.eq(sym)]
                lv=levels.loc[levels.symbol.eq(sym)]
                for tf_filter in ('TODOS','15m','30m'):
                    frames.append(build_lifecycle(ev,lv,history,tf_filter))
                if j%20==0 or j==len(symbols):
                    print(f'PROGRESS {j}/{len(symbols)} symbols',flush=True)
    finally:
        if cache is not None:cache.close()
    derived=pd.concat(frames,ignore_index=True)
    derived=derived.sort_values(['tf_filter','retest_ts','event_key']).reset_index(drop=True)
    output=Path(root)/atlas.v1.lineage.ROOT_REL/OUTPUT
    outpath=output/'event_lifecycle.csv'
    _atomic_csv(outpath,derived)
    meta={'version':VERSION,'timeframe_filters':['TODOS','15m','30m'],
          'events':len(events),'rows':len(derived),
          'counts':dict(Counter(derived.lifecycle_state)),
          'source_file_sha256':{fn:_digest(folder/fn) for fn in ('event_map.csv','candidate_levels.csv')},
          'derived_file_sha256':_digest(outpath),'closed_bar_policy':'timestamp + step <= retest_ts',
          'rules':['min 2 consecutive HTF closes outside frozen band to establish side and change side',
                   'failed retest only after confirmed break with wick overlap and return-side close',
                   'direction-agnostic history of a PRE chosen zone; no outcomes, no KNOWN bars',
                   'ties handled by previous nearest-zone selection'],
          'data_sources':[str(p) for p in paths if p.is_file()]}
    (output/'summary.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('LEVEL_LIFECYCLE_DONE',json.dumps({'events':len(events),'rows':len(derived),'counts':meta['counts']}),flush=True)
    return meta


def audit(root):
    import reaction_structural_level_atlas_v2 as atlas
    base=Path(root)/atlas.v1.lineage.ROOT_REL
    d=base/OUTPUT
    meta=json.loads((d/'summary.json').read_text(encoding='utf-8'))
    rows=pd.read_csv(d/'event_lifecycle.csv',low_memory=False)
    folder=base/atlas.OUTPUT
    ok=(meta['version']==VERSION and len(rows)==int(meta['rows'])
        and len(rows)==3*int(meta['events'])
        and not rows[['tf_filter','event_key']].duplicated().any()
        and set(rows.tf_filter)=={'TODOS','15m','30m'}
        and all(rows.groupby('tf_filter').size()==int(meta['events']))
        and rows.lifecycle_state.isin(LABELS).all()
        and _digest(d/'event_lifecycle.csv')==meta['derived_file_sha256']
        and all(_digest(folder/k)==v for k,v in meta['source_file_sha256'].items())
        and dict(Counter(rows.lifecycle_state))==meta['counts']
        and ((rows.last_pre_close_ts.dropna()<=rows.loc[rows.last_pre_close_ts.notna(),'retest_ts']).all()))
    result={'success':bool(ok),'events':int(meta['events']),
            'rows':len(rows),'counts_todos':dict(Counter(rows.loc[rows.tf_filter=='TODOS','lifecycle_state']))}
    print('LEVEL_LIFECYCLE_AUDIT',json.dumps(result,ensure_ascii=False),flush=True)
    if not ok:raise RuntimeError('Lifecycle cache changed / invalid / not causal')
    return result


def read_checked_cache(root, events, roles, tf='TODOS'):
    """Fail closed on stale outcomes-independent classification or changed Atlas.

    events = Atlas event_map selected as input. roles = the CURRENT PRE level
    selection for the SAME tf. Matching zone ID prevents a stale cache being
    silently joined to a newer model or a different level selection.
    """
    import reaction_structural_level_atlas_v2 as atlas
    from pathlib import Path
    base=Path(root)/atlas.v1.lineage.ROOT_REL
    dest=base/OUTPUT
    meta=json.loads((dest/'summary.json').read_text(encoding='utf-8'))
    if meta.get('version')!=VERSION:
        raise ValueError('Versión del estudio de ciclo de nivel no coincide')
    if any(_digest(base/atlas.OUTPUT/name)!=sha
           for name,sha in meta['source_file_sha256'].items()):
        raise ValueError('Atlas V2 cambió: regenerar el estudio PRE')
    path=dest/'event_lifecycle.csv'
    if _digest(path)!=meta['derived_file_sha256']:
        raise ValueError('CSV de ciclo de nivel cambiado sin auditar')
    rows=pd.read_csv(path,low_memory=False)
    if rows[['tf_filter','event_key']].duplicated().any():
        raise ValueError('Duplicados tf/evento en ciclo de nivel')
    if set(rows.tf_filter)!=set(meta['timeframe_filters']):
        raise ValueError('Falta algún timeframe del experimento')
    current=rows.loc[rows.tf_filter.eq(tf)].copy()
    if len(current)!=len(events) or set(current.event_key)!=set(events.event_key):
        raise ValueError('Eventos PRE no coinciden con Atlas V2')
    check=current[['event_key','zone_id']].merge(
        roles[['event_key','role_zone_id']],on='event_key',validate='one_to_one')
    mismatch=(check.zone_id.fillna('NO_ZONE').astype(str)!=
              check.role_zone_id.fillna('NO_ZONE').astype(str))
    if mismatch.any():
        raise ValueError('Cambio del nivel PRE elegido: regenerar el estudio')
    if not current.lifecycle_state.isin(LABELS).all():
        raise ValueError('Estados PRE desconocidos en CSV')
    return current


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--project-root',default='.')
    gp=ap.add_mutually_exclusive_group(required=True)
    gp.add_argument('--plan',action='store_true')
    gp.add_argument('--run',action='store_true')
    gp.add_argument('--audit',action='store_true')
    args=ap.parse_args()
    root=Path(args.project_root).resolve()
    if args.plan:
        from dashboard.views.reaction_structural_level_atlas_v2_view import load
        lev,events,summary=load(root)
        roles=build_pre_roles(events,lev,'TODOS')
        print('LEVEL_LIFECYCLE_PLAN',json.dumps({'version':VERSION,'events':len(events),
            'tf_filters':['TODOS','15m','30m'],
            'zone_selections_todos':int(roles.role_zone_id.notna().sum()),
            'unselected_todos':int(roles.role_zone_id.isna().sum()),
            'source_version':summary.get('version')},ensure_ascii=False),flush=True)
    elif args.run:run(root)
    else:audit(root)


if __name__=='__main__':main()
