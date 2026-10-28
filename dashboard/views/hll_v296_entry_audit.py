"""HLL V2.9.6: audit trail, ONE row per observed route entry and 25 scenarios per entry.

Offline, read-only research. Never changes detectors, prices, LIVE or trading settings.
The reference is the source V2.9.4 OPEN; execution at TP/SL thresholds is theoretical.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from dashboard.views.hll_v294_entry_matrix import (
    ROUTES, build_entry_catalogue, _window_from_loaded, _positive,
)
from dashboard.views.hll_v295_net_matrix import (
    first_attempt_ids, first_touch_net, _validate_ohlc, KNOWN,
)

PRE = Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
V29 = Path('reports/horizontal_level_continuation_v29/runs/core15_continuation_v29/continuation_events.csv')
MECH = Path('reports/horizontal_level_mechanisms_v292/runs/core15_mechanisms_v292/mechanism_attempts.csv')
MECH_MANIFEST = MECH.parent / 'manifest.json'
CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
MINUTES = Path('reports/horizontal_level_cross_research_v22/extended_1m_core15_v25')
OUTPUT = Path('reports/horizontal_level_entry_audit_v296/runs/core15_first_per_level_v296')
SCOPES = ('FIRST_PER_LEVEL', 'ALL_ATTEMPTS')
PRESPEC = ((2, 2), (3, 2))  # (TP, SL). Audited hypotheses, NOT winners.


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def utc(ts):
    if pd.isna(ts):
        return None
    return datetime.fromtimestamp(int(ts)/1000, tz=timezone.utc).isoformat()


def validate_sources(root: Path):
    required = [PRE, V29, MECH, MECH_MANIFEST]
    absent = [str(p) for p in required if not (root/p).is_file()]
    if absent:
        raise FileNotFoundError('Faltan archivos fuente: ' + ', '.join(absent))
    source_hash = sha(root/PRE)
    manifest = json.loads((root/MECH_MANIFEST).read_text(encoding='utf-8'))
    if manifest.get('source_prehash') != source_hash:
        raise ValueError('V2.9.2 no corresponde al PRE V2.8.2 actual: SHA distinto')
    pre = pd.read_csv(root/PRE, low_memory=False)
    mech = pd.read_csv(root/MECH, low_memory=False)
    v29 = pd.read_csv(root/V29, low_memory=False)
    if pre.attempt_id.isna().any() or pre.attempt_id.duplicated().any():
        raise ValueError('PRE sin attempt_id único')
    if mech.attempt_id.isna().any() or mech.attempt_id.duplicated().any():
        raise ValueError('V292 sin attempt_id único')
    if len(pre) != len(mech) or set(pre.attempt_id) != set(mech.attempt_id):
        raise ValueError('V292 y PRE tienen universos distintos')
    for col in ['symbol','level_id','breakout_closed_ts','pre_closed_ts','zone_low','zone_high']:
        if col not in mech or col not in pre:
            raise ValueError('Falta campo fundamental '+col)
        joined = pre[['attempt_id',col]].merge(mech[['attempt_id',col]], on='attempt_id', validate='one_to_one')
        if not joined[f'{col}_x'].fillna('__NA__').eq(joined[f'{col}_y'].fillna('__NA__')).all():
            raise ValueError(f'V292 modificó campo original PRE: {col}')
    required_meta = {'attempt_id','mechanism_48','pre_pattern_48', 'touch_v26_independent_48',
                     'physical_overlap_clusters_48','close_efficiency_48'}
    if missing := required_meta-set(mech):
        raise ValueError('V292 sin métricas PRE: '+str(sorted(missing)))
    required_v29 = {'attempt_id','branch','status','confirmed_ts','signal_close','signal_open_ts'}
    if missing := required_v29-set(v29):
        raise ValueError('V29 sin columnas '+str(sorted(missing)))
    if v29.duplicated(['attempt_id','branch']).any() or not set(v29.attempt_id).issubset(set(pre.attempt_id)):
        raise ValueError('V29 con entradas duplicadas o IDs ajenos')
    return pre, mech, v29, {'pre_sha256':source_hash,
                           'v292_sha256':sha(root/MECH), 'v29_sha256':sha(root/V29)}


def status_table(pre:pd.DataFrame, catalogue:pd.DataFrame) -> pd.DataFrame:
    """All attempts x routes, including NO_SIGNAL, to audit missing entries."""
    keys = pre[['attempt_id','symbol','level_id','breakout_closed_ts']].copy()
    keys['key'] = 1
    routes = pd.DataFrame({'route':list(ROUTES),'key':1})
    all_routes = keys.merge(routes,on='key').drop(columns='key')
    if not catalogue.empty:
        all_routes = all_routes.merge(catalogue[['attempt_id','route','status','entry_ts','entry_price','signal_ts']],
            how='left', on=['attempt_id','route'],validate='one_to_one')
    else:
        all_routes['status'] = pd.NA
        for k in ['entry_ts','entry_price','signal_ts']: all_routes[k] = np.nan
    all_routes['status'] = all_routes.status.fillna('NO_SIGNAL')
    all_routes['breakout_utc'] = all_routes.breakout_closed_ts.apply(utc)
    all_routes['entry_utc'] = all_routes.entry_ts.apply(utc)
    if all_routes.duplicated(['attempt_id','route']).any():
        raise ValueError('Duplicación de intentos/rutas en coverage')
    return all_routes


def build_entries(pre:pd.DataFrame, mech:pd.DataFrame, v29:pd.DataFrame,
                  candle_dir:Path, minute_dir:Path, scope:str='FIRST_PER_LEVEL'):
    if scope not in SCOPES:
        raise ValueError('scope inválido')
    # Deduplicate from original whole universe BEFORE route/post labels.
    selected = pre.loc[pre.attempt_id.astype(str).isin(first_attempt_ids(pre))].copy() \
        if scope == 'FIRST_PER_LEVEL' else pre.copy()
    catalogue = build_entry_catalogue(selected, v29, candle_dir, minute_dir)
    coverage = status_table(selected, catalogue)
    entry = catalogue.loc[catalogue.status.eq('ENTRY_VERIFIED')].copy()
    if entry.empty:
        return selected, coverage, pd.DataFrame()
    cols = ['attempt_id','level_id','breakout_closed_ts','pre_closed_ts','post_outcome',
            'v26_status','pre_pattern_48','zone_low','zone_high']
    entry = entry.merge(selected[cols],on='attempt_id',how='left',validate='many_to_one')
    metrics=['attempt_id','mechanism_48','touch_v26_independent_48',
             'physical_overlap_clusters_48','confirmed_pivots_48',
             'close_efficiency_48','pre_30m_coverage_48']
    metrics = [k for k in metrics if k in mech]
    entry = entry.merge(mech[metrics],on='attempt_id',how='left',validate='many_to_one')
    if entry.duplicated(['attempt_id','route']).any():
        raise ValueError('Entradas repetidas por intento/ruta')
    if entry.entry_ts.isna().any() or not entry.entry_price.map(_positive).all():
        raise ValueError('Entrada verificada sin precio/reloj')
    if not entry.entry_ts.map(lambda v: math.isfinite(float(v))).all():
        raise ValueError('Reloj de entrada inválido')
    entry['entry_ts'] = entry.entry_ts.astype('int64')
    entry['entry_id'] = entry.apply(lambda r: hashlib.sha256(
        f'{r.attempt_id}|{r.route}|{int(r.entry_ts)}'.encode()).hexdigest()[:24],axis=1)
    entry['entry_utc']=entry.entry_ts.apply(utc)
    entry['signal_utc']=entry.signal_ts.apply(utc)
    entry['breakout_utc']=entry.breakout_closed_ts.apply(utc)
    entry['entry_month']=entry.entry_utc.str[:7]
    entry['breakout_month']=entry.breakout_utc.str[:7]
    entry['symbol_day']=entry.entry_utc.str[:10]+'|'+entry.symbol.astype(str)
    if entry.entry_id.duplicated().any():
        raise ValueError('Colisión de identificador de entrada')
    return selected, coverage, entry


def detail_for_entries(entries:pd.DataFrame, minute_dir:Path, horizons:list[int],
                       commission:float, slippage:float):
    if entries.empty:
        return pd.DataFrame()
    if sorted(set(horizons)) != sorted(horizons) or not set(horizons).issubset({120,720,2880}):
        raise ValueError('Horizontes inválidos o duplicados')
    outcomes=[]
    # Strictly 1m OHLC from the existing archived source. Same symbol loaded once.
    for symbol, group in entries.groupby('symbol',sort=True):
        p=Path(minute_dir)/f'{symbol}_1m.csv'
        if p.is_file():
            bars=pd.read_csv(p,usecols=['timestamp','open','high','low','close'])
            _validate_ohlc(bars,str(symbol))
            bars.timestamp=pd.to_numeric(bars.timestamp, errors='raise').astype('int64')
        else:
            bars=None
        for e in group.itertuples(index=False):
            for horizon in horizons:
                if bars is None:
                    window=pd.DataFrame()
                    state='NO_1M_FILE'
                else:
                    window,state=_window_from_loaded(bars,int(e.entry_ts),horizon)
                    if not window.empty and abs(float(window.open.iloc[0])-float(e.entry_price)) > \
                            max(1e-10,abs(float(e.entry_price))*1e-7):
                        raise ValueError(f'OPEN 1m distinto a entrada verificada: {e.entry_id}')
                result=first_touch_net(window,float(e.entry_price),state,int(e.entry_ts),
                                       commission,slippage)
                result['entry_id']=e.entry_id
                result['attempt_id']=e.attempt_id
                result['symbol']=symbol
                result['level_id']=e.level_id
                result['route']=e.route
                result['entry_ts']=int(e.entry_ts)
                result['entry_utc']=e.entry_utc
                result['entry_price']=float(e.entry_price)
                result['breakout_utc']=e.breakout_utc
                result['entry_month']=e.entry_month
                result['symbol_day']=e.symbol_day
                result['mechanism_pre_48']=e.mechanism_48
                result['pre_pattern_48']=e.pre_pattern_48
                result['post_outcome_v26']=e.post_outcome
                result['horizon_min']=horizon
                result['path_state']=state
                result['tp_price']=float(e.entry_price)*(1+result.tp_pct/100)
                result['sl_price']=float(e.entry_price)*(1-result.sl_pct/100)
                result['exit_reason']=result.result
                # Within-minute path unknown: timestamp means minute bucket, NOT exact fill time.
                result['exit_minute_utc']=result.first_hit_minute_utc
                is_timeout=result.result.eq('TIMEOUT')
                result.loc[is_timeout,'exit_minute_utc']=utc(int(e.entry_ts)+horizon*60_000)
                result['minutes_to_exit']=result.first_hit_min
                result.loc[is_timeout,'minutes_to_exit']=horizon
                result['outcome_known']=result.result.isin(KNOWN)
                result['is_censored']=result.result.isin(['CENSORED_GAP','CENSORED_TAIL','NO_1M_FILE','NO_ENTRY_MINUTE'])
                outcomes.append(result)
    detail=pd.concat(outcomes,ignore_index=True)
    if detail.duplicated(['entry_id','horizon_min','tp_pct','sl_pct']).any():
        raise ValueError('Duplicado en matriz por entrada')
    n_expected=len(entries)*len(horizons)*25
    if len(detail)!=n_expected:
        raise ValueError(f'Esperadas {n_expected} celdas, producidas {len(detail)}')
    if detail.loc[~detail.outcome_known,'net_pct'].notna().any():
        raise ValueError('Se atribuyó PnL a un episodio censurado/ambiguo')
    return detail.sort_values(['entry_utc','entry_id','horizon_min','sl_pct','tp_pct']).reset_index(drop=True)


def summarize(detail:pd.DataFrame, groupby:list[str]):
    """Predeclared (TP2/SL2, TP3/SL2); never rank 25 cells by training score."""
    if detail.empty: return pd.DataFrame()
    d=detail.loc[detail.apply(lambda x:(int(x.tp_pct),int(x.sl_pct)) in PRESPEC,axis=1)].copy()
    out=[]
    for keys,g in d.groupby(groupby+['horizon_min','tp_pct','sl_pct'],dropna=False,sort=True):
        key=keys if isinstance(keys,tuple) else (keys,)
        meta=dict(zip(groupby+['horizon_min','tp_pct','sl_pct'],key))
        known=g[g.outcome_known]
        net=pd.to_numeric(known.net_pct,errors='coerce')
        if net.isna().any():raise ValueError('Resultado conocido sin neto')
        counts=g.result.value_counts()
        out.append({**meta,'entries':len(g),'known':len(known),
          'coverage_pct':round(100*len(known)/len(g),2),
          'tp_first':int(counts.get('TP_FIRST',0)),
          'sl_first':int(counts.get('SL_FIRST',0)),
          'timeout':int(counts.get('TIMEOUT',0)),
          'ambiguous':int(counts.get('AMBIGUOUS_1M',0)),
          'censored':int(g.is_censored.sum()),
          'mean_net_known_pct':round(float(net.mean()),6) if len(known) else np.nan,
          'median_net_known_pct':round(float(net.median()),6) if len(known) else np.nan,
          'median_minutes_to_exit':float(known.minutes_to_exit.median()) if known.minutes_to_exit.notna().any() else np.nan,
          'symbols':int(g.symbol.nunique()),
          'symbol_days':int(g.symbol_day.nunique())})
    return pd.DataFrame(out)


def paired_route_comparison(detail:pd.DataFrame):
    """Only SAME attempt, SAME horizon and TP/SL, BOTH known. Never pool unlike routes."""
    if detail.empty:return pd.DataFrame()
    d=detail.loc[detail.apply(lambda x:(int(x.tp_pct),int(x.sl_pct)) in PRESPEC,axis=1) & detail.outcome_known].copy()
    l=d[d.route.eq('FLIP_1M')]
    r=d[d.route.eq('V29_CONTINUATION_30M')]
    a=l.merge(r,on=['attempt_id','horizon_min','tp_pct','sl_pct'],suffixes=('_flip','_continuation'),validate='one_to_one')
    if a.empty:return pd.DataFrame()
    return pd.DataFrame({'attempt_id':a.attempt_id,'symbol':a.symbol_flip,
        'entry_utc_flip':a.entry_utc_flip,'entry_utc_continuation':a.entry_utc_continuation,
        'horizon_min':a.horizon_min,'tp_pct':a.tp_pct,'sl_pct':a.sl_pct,
        'flip_result':a.result_flip,'continuation_result':a.result_continuation,
        'net_flip_pct':a.net_pct_flip,'net_continuation_pct':a.net_pct_continuation,
        'net_difference_flip_minus_continuation_pct':a.net_pct_flip-a.net_pct_continuation})


def run(root:Path, output:Path, scope:str, horizons:list[int], commission:float,slippage:float):
    if output.exists():
        raise FileExistsError(f'La salida ya existe y no se sobrescribe: {output}')
    pre,mech,v29,hashes=validate_sources(root)
    selected,coverage,entries=build_entries(pre,mech,v29,root/CANDLES,root/MINUTES,scope)
    print('HLL_V296_COHORT',json.dumps({'attempts':len(selected),'verified_entries':len(entries),
        'unverified_signals':int(coverage.status.ne('NO_SIGNAL').sum()-len(entries)),
        'route_counts':entries.route.value_counts().to_dict() if not entries.empty else {}},ensure_ascii=False),flush=True)
    detail=detail_for_entries(entries,root/MINUTES,horizons,commission,slippage)
    route_month=summarize(detail,['route','entry_month'])
    mechanism=summarize(detail,['route','mechanism_pre_48'])
    by_route=summarize(detail,['route'])
    paired=paired_route_comparison(detail)
    source_files={str(x):{'size_bytes':(root/x).stat().st_size,'mtime_ns':(root/x).stat().st_mtime_ns}
                  for sym in selected.symbol.unique() for x in [(MINUTES/f'{sym}_1m.csv'), (CANDLES/f'{sym}_30m.csv')]
                  if (root/x).is_file()}
    manifest={'version':'HLL_V296_ENTRY_AUDIT','mode':'OFFLINE_READ_ONLY',
              'scope':scope,'horizons_min':horizons,'commission_roundtrip_pct':commission,
              'slippage_roundtrip_pct':slippage,'predeclared_scenarios':list(PRESPEC),
              'source_sha256':hashes,'ohlc_file_metadata':source_files,
              'selected_attempts':len(selected),'verified_entries':len(entries),
              'scenario_rows':len(detail),'common_route_pairs':len(paired),
              'limitations':['Threshold fill prices are theoretical; gaps and adverse execution not modeled',
                  'Ambiguous minute and censored data have unknown PnL',
                  'Routes can share one original attempt; not a simultaneous portfolio',
                  'Monthly historical splits are retrospective, NOT out-of-sample validation']}
    if output.exists():
        raise FileExistsError(f'La salida ya existe y no se sobrescribe: {output}')
    output.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix='.hll-v296-',dir=str(output.parent)))
    try:
        artifacts={'original_attempts.csv':selected,
                   'attempt_route_coverage.csv':coverage,
                   'entry_events.csv':entries,
                   'entry_scenarios_25.csv':detail,
                   'route_month_prespecified.csv':route_month,
                   'route_mechanism_prespecified.csv':mechanism,
                   'route_prespecified.csv':by_route,
                   'same_attempt_route_pairs.csv':paired}
        for name,df in artifacts.items(): df.to_csv(tmp/name,index=False)
        (tmp/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        tmp.rename(output)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    print('HLL_V296_DONE',json.dumps({'output':str(output),'attempts':len(selected),
            'entries':len(entries),'scenario_rows':len(detail),'paired':len(paired)},ensure_ascii=False),flush=True)
    return manifest


def report(output:Path):
    if not (output/'manifest.json').is_file():raise FileNotFoundError('Corrida inexistente: '+str(output))
    manifest=json.loads((output/'manifest.json').read_text())
    print('HLL_V296_REPORT',json.dumps({k:manifest[k] for k in
          ['scope','horizons_min','selected_attempts','verified_entries','scenario_rows','common_route_pairs']},ensure_ascii=False))
    p=output/'route_prespecified.csv'
    if p.is_file():
        table=pd.read_csv(p)
        if not table.empty:print(table.to_string(index=False))
    r=output/'route_month_prespecified.csv'
    if r.is_file():
        df=pd.read_csv(r)
        if not df.empty:print('HLL_V296_BY_MONTH\n'+df.to_string(index=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--plan',action='store_true')
    action.add_argument('--run',action='store_true')
    action.add_argument('--report',action='store_true')
    parser.add_argument('--root',type=Path,default=Path('.'))
    parser.add_argument('--output',type=Path,default=OUTPUT)
    parser.add_argument('--scope',choices=SCOPES,default='FIRST_PER_LEVEL')
    parser.add_argument('--horizons',type=int,nargs='+',default=[120,720,2880],choices=[120,720,2880])
    parser.add_argument('--commission-pct',type=float,default=0.10)
    parser.add_argument('--slippage-pct',type=float,default=0.00)
    args=parser.parse_args()
    args.root=args.root.resolve()
    output=args.output if args.output.is_absolute() else args.root/args.output
    if args.report:
        report(output);return
    pre,mech,v29,hashes=validate_sources(args.root)
    first=len(first_attempt_ids(pre))
    plan={'version':'HLL_V296_ENTRY_AUDIT','source_attempts':len(pre),'first_per_level':first,
        'scope':args.scope,'estimated_cohort_attempts':first if args.scope=='FIRST_PER_LEVEL' else len(pre),
        'horizons':args.horizons,'commission_pct':args.commission_pct,
        'slippage_pct':args.slippage_pct,'output':str(output),'read_only':True,'hashes':hashes}
    print('HLL_V296_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if args.run:
        run(args.root,output,args.scope,args.horizons,args.commission_pct,args.slippage_pct)

if __name__=='__main__':main()
