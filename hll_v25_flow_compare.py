#!/usr/bin/env python3
"""Read-only exploratory Market Flow vs executed 1m outcomes by fixed TP/SL."""
import argparse
from pathlib import Path
import pandas as pd

def summarize(features, outcomes, *, tp=3.0, sl=2.0, horizon=360):
    a=outcomes[(outcomes.tp_pct==tp)&(outcomes.sl_pct==sl)&
        (outcomes.horizon_min==horizon)&(outcomes.path_state=='COMPLETE_1M')].copy()
    if a.empty:raise ValueError('No COMPLETE_1M outcomes for selected fixed TP SL horizon')
    if features.event_id.duplicated().any():raise ValueError('Repeated event_id in market flow features')
    merged=a.merge(features[['event_id','market_regime','side_flow']],on='event_id',how='left',validate='many_to_one')
    if merged.market_regime.isna().any():raise ValueError('Missing causal market-flow data for some events')
    # One per symbol+stage+known 30m candle. No outcomes used to choose survivor.
    merged=merged.sort_values(['symbol','stage','known_closed_ts','event_id'])
    merged=merged.drop_duplicates(['symbol','stage','known_closed_ts'],keep='first')
    grouped=[]
    for (stage,regime,flow),group in merged.groupby(['stage','market_regime','side_flow']):
        n=len(group); counts=group.outcome.value_counts()
        grouped.append(dict(stage=stage,market_regime=regime,side_flow=flow,
            n=n,symbols=group.symbol.nunique(),days=pd.to_datetime(group.known_closed_ts,unit='ms',utc=True).dt.date.nunique(),
            tp=int(counts.get('TP_FIRST',0)),sl=int(counts.get('SL_FIRST',0)),
            time=int(counts.get('TIME_EXIT',0)),ambiguous=int(counts.get('AMBIGUOUS_BOTH',0)),
            tp_pct_all=round(100*int(counts.get('TP_FIRST',0))/n,4),
            mean_net_pct=round(float(group.return_after_fee_pct.mean()),5)))
    return pd.DataFrame(grouped).sort_values(['stage','market_regime']).reset_index(drop=True)

def main(argv=None):
    p=argparse.ArgumentParser(description='Compare frozen V2.2 1m results against causal BTC and cohort flow')
    p.add_argument('--features',required=True,type=Path)
    p.add_argument('--outcomes',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--tp',type=float,default=3.0)
    p.add_argument('--sl',type=float,default=2.0)
    p.add_argument('--horizon',type=int,default=360)
    a=p.parse_args(argv)
    if a.output.exists():p.error('Output exists; no overwrites')
    features=pd.read_csv(a.features);outcomes=pd.read_csv(a.outcomes)
    result=summarize(features,outcomes,tp=a.tp,sl=a.sl,horizon=a.horizon)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    result.to_csv(a.output,index=False)
    print('HLL_V25_FLOW_COMPARISON',dict(tp=a.tp,sl=a.sl,horizon=a.horizon,groups=len(result),output=str(a.output)))
    print(result.to_string(index=False))

if __name__=='__main__':main()
