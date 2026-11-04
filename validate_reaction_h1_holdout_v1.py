#!/usr/bin/env python3
"""Frozen, read-only H1 holdout validation for Reaction 15m SHORT × HTF 30m/1h.

Research only. Does not calculate signals or modify LIVE. The inputs must be the
existing cohort V2 holdout files generated from the already-frozen cohort plan.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ID = 'REACTION_H1_HOLDOUT_V1'
FROZEN_PREREG_SHA256 = '8804529616dc68b47f2fbeb28703bd1760ca9ce288d40546a04daa3c4b1a82eb'
ALLOWED_CONTEXT = {'ACTIVE', 'ALL_CANDIDATES_INVALIDATED', 'NO_CONFIRMED_CANDIDATE', 'UNKNOWN_HISTORY'}


def sha256(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):
            h.update(b)
    return h.hexdigest()


def verify_inputs(plan_path:Path, prereg_path:Path, coverage_path:Path, events_path:Path, manifest_path:Path):
    if sha256(prereg_path)!=FROZEN_PREREG_SHA256:
        raise ValueError('The pre-registered H1 file was modified after freezing')
    freeze=json.loads(prereg_path.read_text(encoding='utf-8'))
    plan=json.loads(plan_path.read_text(encoding='utf-8'))
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if freeze.get('id')!=ID or freeze.get('status')!='FROZEN_BEFORE_HOLDOUT':
        raise ValueError('H1 pre-registration missing/not frozen')
    if freeze['discovery_plan_sha256']!=sha256(plan_path):
        raise ValueError('H1 freeze does not match the frozen cohort plan SHA256')
    if manifest.get('cohort_plan_sha256')!=sha256(plan_path):
        raise ValueError('Holdout pool manifest does not match the frozen cohort plan')
    if manifest.get('stage')!='holdout' or manifest.get('version')!='REACTION_MULTITF_COHORT_V2':
        raise ValueError('Not the V2 holdout cohort')
    cuts=plan.get('holdout_cutoffs_utc')
    if cuts!=freeze['holdout_cutoffs_utc'] or cuts!=['2026-09-25T14:00:00Z','2026-09-30T14:00:00Z']:
        raise ValueError('Holdout cutoffs modified')
    if len(set(plan['selected_symbols']))!=len(plan['selected_symbols']):
        raise ValueError('Duplicate frozen symbols')
    if any(x in plan['selected_symbols'] for x in ['DYDXUSDT','CAKEUSDT']):
        raise ValueError('Development symbols contaminate holdout')
    if freeze['rule']!={'original_side':'SHORT','original_status':'REACTION',
         'swing_timeframe':'15m','30m_group':'NEAR_ACTIVE_0.20',
         '1h_group_any':['NEAR_ACTIVE_0.20','FAR_ACTIVE']}:
        raise ValueError('H1 rule mismatch')
    hashes=manifest.get('output_sha256',{})
    if hashes.get('coverage')!=sha256(coverage_path) or hashes.get('htf')!=sha256(events_path):
        raise ValueError('Holdout pooled CSV digest mismatch')
    if not manifest.get('complete_cohort') or manifest.get('completed_cases')!=2*len(plan['selected_symbols']) or manifest.get('planned_cases')!=2*len(plan['selected_symbols']):
        raise ValueError('Holdout cohort not fully COMPLETE; no H1 decision')
    coverage=pd.read_csv(coverage_path)
    expected={(s,c) for s in plan['selected_symbols'] for c in cuts}
    got=set(zip(coverage.symbol,coverage.pre_cutoff_utc))
    if len(coverage)!=len(expected) or got!=expected or not coverage.state.eq('COMPLETE').all():
        raise ValueError('Holdout case coverage does not match locked cohort')
    frame=pd.read_csv(events_path)
    if frame.empty:raise ValueError('No first retests in holdout')
    if not set(frame.pre_cutoff_utc).issubset(set(cuts)) or not set(frame.symbol).issubset(set(plan['selected_symbols'])):
        raise ValueError('Holdout contains events from unplanned symbols/cutoffs')
    if frame[['symbol','pre_cutoff_utc','pivot_touch_id']].duplicated().any():
        raise ValueError('Duplicate retests detected')
    if (frame.original_known_ts.astype('int64')>frame.pre_cutoff_ms.astype('int64')).any():
        raise ValueError('Lookahead: event known after PRE cutoff')
    return freeze,plan,manifest,frame


def transform(frame:pd.DataFrame) -> tuple[pd.DataFrame,dict]:
    """Derive H1 strictly from causal HTF status, not the outcome."""
    q=frame[(frame.original_side=='SHORT') & (frame.original_status=='REACTION')].copy()
    if 'swing_timeframe' in q.columns and not q.swing_timeframe.eq('15m').all():
        raise ValueError('HTF event file contains non-15m first retests')
    for tf in ('30m','1h'):
        s=q[tf+'_context_status']
        if not set(s.dropna().unique()).issubset(ALLOWED_CONTEXT):
            raise ValueError('Unrecognized HTF status in '+tf)
        active=s.eq('ACTIVE')
        near=q[tf+'_near_0_20_pct'].map(lambda x: None if pd.isna(x) else (x if isinstance(x,bool) else str(x).strip().lower()=='true'))
        calculated=np.where(active & near.eq(True),'NEAR_ACTIVE_0.20',np.where(active & near.eq(False),'FAR_ACTIVE',s))
        q[tf+'_derived_group']=calculated
        if tf+'_group' in q:
            mismatch=~q[tf+'_group'].fillna('MISSING').eq(q[tf+'_derived_group'].astype(str))
            if mismatch.any():
                raise ValueError('Raw HTF status disagrees with derived grouping: '+tf)
    # A gap/unknown in either HTF context is not a negative observation.
    known=q['30m_derived_group'].notna() & q['1h_derived_group'].notna() & ~q['30m_derived_group'].isin(['UNKNOWN_HISTORY']) & ~q['1h_derived_group'].isin(['UNKNOWN_HISTORY'])
    eligible=q.loc[known & q.original_30m_coverage.eq('COMPLETE')].copy()
    eligible['H1_BACKED']=eligible['30m_derived_group'].eq('NEAR_ACTIVE_0.20') & eligible['1h_derived_group'].isin(['NEAR_ACTIVE_0.20','FAR_ACTIVE'])
    eligible['PRIMARY_SUCCESS']=eligible.original_30m_race_0_5.eq('FAVORABLE_FIRST')
    eligible['PRIMARY_AMBIGUOUS']=eligible.original_30m_race_0_5.isin(['AMBIGUOUS','AMBIGUOUS_1M'])
    if eligible.original_30m_race_0_5.isna().any():raise ValueError('Missing race for COMPLETE outcome')
    return eligible,{'short_reactions':int(len(q)), 'unknown_htf':int((~known).sum()),
                     'incomplete_primary_30m':int((known & ~q.original_30m_coverage.eq('COMPLETE')).sum()),
                     'primary_eligible':int(len(eligible))}


def count_table(events:pd.DataFrame)->pd.DataFrame:
    rows=[]
    for value in [True,False]:
        z=events[events.H1_BACKED.eq(value)]
        rows.append({'group':'H1_BACKED' if value else 'COMPARATOR',
              'n':len(z),'wins':int(z.PRIMARY_SUCCESS.sum()),
              'win_pct':(100*z.PRIMARY_SUCCESS.mean() if len(z) else np.nan),
              'ambiguous_1m':int(z.PRIMARY_AMBIGUOUS.sum()),
              'symbols':int(z.symbol.nunique()),
              'symbol_cutoffs':int(z[['symbol','pre_cutoff_utc']].drop_duplicates().shape[0])})
    return pd.DataFrame(rows)


def by_symbol(events:pd.DataFrame)->pd.DataFrame:
    d=events.groupby(['symbol','H1_BACKED']).PRIMARY_SUCCESS.agg(['count','sum']).reset_index()
    n=d.pivot(index='symbol',columns='H1_BACKED',values='count').fillna(0)
    w=d.pivot(index='symbol',columns='H1_BACKED',values='sum').fillna(0)
    out=pd.DataFrame(index=sorted(events.symbol.unique()))
    for tag,key in [('backed',True),('comparator',False)]:
        out[tag+'_n']=n[key] if key in n.columns else 0
        out[tag+'_wins']=w[key] if key in w.columns else 0
        out[tag+'_rate']=(out[tag+'_wins']/out[tag+'_n']).where(out[tag+'_n']>0)
    out['paired_delta_pp']=(out.backed_rate-out.comparator_rate)*100
    return out.reset_index().rename(columns={'index':'symbol'})


def symbol_block_bootstrap(events:pd.DataFrame,reps:int=10000,seed:int=20261104):
    """Resample entire symbol trajectories, preserving both cutoffs within each symbol."""
    tab=by_symbol(events)
    # Group-level counts and success, no drawing of individual correlated retests.
    arr=tab[['backed_n','backed_wins','comparator_n','comparator_wins']].fillna(0).to_numpy(float)
    if len(arr)<2 or not arr[:,0].sum() or not arr[:,2].sum():
        return {'low_pp':None,'high_pp':None,'replicates_valid':0}
    rng=np.random.default_rng(seed); n=len(arr); draws=[]
    for _ in range(reps):
        s=arr[rng.integers(0,n,size=n)].sum(axis=0)
        if s[0] and s[2]:draws.append(100*(s[1]/s[0]-s[3]/s[2]))
    if not draws:return {'low_pp':None,'high_pp':None,'replicates_valid':0}
    return {'low_pp':round(float(np.quantile(draws,0.025)),3),
            'high_pp':round(float(np.quantile(draws,0.975)),3),
            'replicates_valid':len(draws),'seed':seed,'symbol_clusters':n}


def secondary(events:pd.DataFrame)->dict:
    out={}
    for val, name in [(True,'H1_BACKED'),(False,'COMPARATOR')]:
        z=events.loc[events.H1_BACKED.eq(val)]
        closed=z.loc[z.original_30m_coverage.eq('COMPLETE')]
        opp=z.loc[z.opposite_breakout_scan_coverage.eq('COMPLETE')]
        def pct(w,n):return round(100*w/n,2) if n else None
        winclose=int(closed.original_30m_race_close_band_0_5.eq('FAVORABLE_FIRST').sum())
        confirmed=int(opp.opposite_breakout_confirmed.astype(str).str.lower().eq('true').sum())
        out[name]={'close_band_30m_wins':winclose,'close_band_30m_n':len(closed),'close_band_30m_pct':pct(winclose,len(closed)),
                   'opposite_60m_confirmed':confirmed,'opposite_60m_complete':len(opp),'opposite_60m_pct':pct(confirmed,len(opp))}
    return out


def assess(events:pd.DataFrame)->tuple[dict,pd.DataFrame,pd.DataFrame]:
    tbl=count_table(events); b=tbl.iloc[0]; o=tbl.iloc[1]
    delta=100*(b.wins/b.n-o.wins/o.n) if b.n and o.n else None
    rr=(b.wins/b.n)/(o.wins/o.n) if b.n and o.n and o.wins else None
    boots=symbol_block_bootstrap(events)
    sym=by_symbol(events)
    paired=sym.dropna(subset=['paired_delta_pp'])
    positive=int((paired.paired_delta_pp>0).sum()); negative=int((paired.paired_delta_pp<0).sum()); tied=int((paired.paired_delta_pp==0).sum())
    # No tuning after seeing holdout; 95% symbol-block CI must exclude zero for support.
    if not b.n or not o.n or boots['low_pp'] is None:
        decision='INCONCLUSIVE_INSUFFICIENT_COMPARABLE_DATA'
    elif delta>0 and boots['low_pp']>0:
        decision='H1_SUPPORTED_IN_HOLDOUT_DESCRIPTIVE'
    elif delta<=0:
        decision='H1_NOT_REPLICATED'
    else:
        decision='INCONCLUSIVE_INTERVAL_INCLUDES_ZERO'
    result={'hypothesis':'H1 SHORT 15m / 30m near active + 1h active',
      'primary':'30m FAVORABLE_FIRST +0.5% before wick invalidation',
      'backed':{'n':int(b.n),'wins':int(b.wins),'pct':round(float(b.win_pct),3),'ambiguous':int(b.ambiguous_1m)},
      'comparator':{'n':int(o.n),'wins':int(o.wins),'pct':round(float(o.win_pct),3),'ambiguous':int(o.ambiguous_1m)},
      'difference_pp':round(delta,3) if delta is not None else None,
      'risk_ratio':round(rr,3) if rr is not None else None,'symbol_bootstrap_95pct':boots,
      'paired_symbols':{'both_groups':len(paired),'positive':positive,'negative':negative,'ties':tied,
                        'mean_delta_pp':round(float(paired.paired_delta_pp.mean()),3) if len(paired) else None},
      'secondary':secondary(events),'decision':decision,
      'limitations':['Observational, not randomized; market-wide dependence not eliminated',
                     'Two time cuts; same market regime, possible correlated signals',
                     'Only symbols with HLL 1m history, not all 290 instruments',
                     'Ambiguous same-1m races counted as non-wins, also reported separately',
                     'No commissions/slippage/trading sizing; not an executable strategy']}
    return result,tbl,sym


def report_markdown(result:dict,meta:dict,manifest:dict,plan:dict,freeze_hash:str)->str:
    a=result['backed']; b=result['comparator']; boot=result['symbol_bootstrap_95pct']; pr=result['paired_symbols']
    def per(o):return f"{o['wins']}/{o['n']} ({o['pct']:.1f}%)" if o['n'] else 'sin eventos'
    return f'''# Validación fuera de muestra H1 — REACTION SHORT × respaldo HTF

**Estado:** {result['decision']}  
**Plan congelado:** `{manifest['cohort_plan_sha256']}`  
**Pre-registro SHA256:** `{freeze_hash}`  
**Ventanas UTC:** {', '.join(plan['holdout_cutoffs_utc'])}  
**Cobertura:** {manifest['completed_cases']}/{manifest['planned_cases']} casos completos.  

## Regla que no se ajustó durante holdout

REACTION original SHORT 15m, swing 30m HIGH `ACTIVE` y a <=0,20%, swing 1h HIGH `ACTIVE` (cercano o lejano). Grupo control: demás REACTIONs SHORT 15m con estados HTF conocidos. `UNKNOWN_HISTORY` no es ausencia de respaldo.

## Resultado primario

+0,5% favorable antes de invalidación por **mecha**, dentro de 30 minutos a partir de la REACTION original. Los retests clasificados ambiguos en la misma vela no cuentan como aciertos, pero se reportan.

| Grupo | Resultado |
|---|---:|
| Con H1 | {per(a)} |
| Comparador | {per(b)} |
| Diferencia absoluta | {result['difference_pp']} puntos porcentuales |
| Riesgo relativo de éxito | {result['risk_ratio']} |
| Bootstrap 95% (resampleo por símbolo) | [{boot['low_pp']}, {boot['high_pp']}] pp |

**Consistencia por símbolo:** {pr['both_groups']} símbolos tienen ambos grupos; diferencia positiva en {pr['positive']}, negativa en {pr['negative']}, empate en {pr['ties']}. Media de diferencias emparejadas: {pr['mean_delta_pp']} pp.

## Resultados secundarios (no cambian la decisión primaria)

```json
{json.dumps(result['secondary'],indent=2,ensure_ascii=False)}
```

## Cobertura y exclusiones

{json.dumps(meta,indent=2,ensure_ascii=False)}

## Interpretación

La decisión se basa en signo de diferencia primaria y límite inferior del intervalo bootstrap por símbolo. Un intervalo que cruza cero es inconcluso. No mover umbrales ni proponer producción según el holdout. Este intervalo no elimina dependencia común de mercado ni demuestra causalidad.
'''


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ['plan','prereg','coverage','events','manifest','out-prefix']:
        ap.add_argument('--'+key,required=True)
    a=ap.parse_args(argv)
    paths={k:Path(getattr(a,k.replace('-','_'))).resolve() for k in ['plan','prereg','coverage','events','manifest','out-prefix']}
    freeze,plan,manifest,frame=verify_inputs(paths['plan'],paths['prereg'],paths['coverage'],paths['events'],paths['manifest'])
    events,meta=transform(frame)
    result,counts,by_sym=assess(events)
    result['coverage_audit']=meta
    prefix=paths['out-prefix']
    for suffix in ('.json','_groups.csv','_by_symbol.csv','_eligible_events.csv','.md'):
        if Path(str(prefix)+suffix).exists():raise FileExistsError('Refusing to overwrite existing H1 result: '+str(prefix)+suffix)
    prefix.parent.mkdir(parents=True,exist_ok=True)
    Path(str(prefix)+'.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    counts.to_csv(str(prefix)+'_groups.csv',index=False)
    by_sym.to_csv(str(prefix)+'_by_symbol.csv',index=False)
    events.to_csv(str(prefix)+'_eligible_events.csv',index=False)
    md=report_markdown(result,meta,manifest,plan,sha256(paths['prereg']))
    Path(str(prefix)+'.md').write_text(md,encoding='utf-8')
    print('REACTION_H1_HOLDOUT',json.dumps({k:result[k] for k in ['backed','comparator','difference_pp','symbol_bootstrap_95pct','paired_symbols','decision']},ensure_ascii=False))
    print('OUTPUT',*[str(prefix)+s for s in ('.json','_groups.csv','_by_symbol.csv','_eligible_events.csv','.md')])

if __name__=='__main__':main()
