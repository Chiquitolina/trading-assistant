#!/usr/bin/env python3
"""V2.6 cross-semantics audit: read-only, no detector/rule/outcome modification.

Compare frozen V2.1 HIGH/CROSS_UP events with the 30m OHLC and the original
zone boundaries, using round-trip parsed V2.1 floats. Checks ALL 290 symbols
before the expensive V2.6 full run.
"""
import argparse
import json
from collections import Counter
from pathlib import Path
import pandas as pd
from hll_v26_structure_lab import TF, _validate_v21_cross_up

ATLAS=Path('reports/horizontal_level_lifecycle_v21/sep290_aug20_oct04_frozen_v21')
CANDLES=Path('reports/horizontal_level_290_history_v298/candles_30m_aug20_oct04_v298')


def audit_symbol(symbol, atlas, candles):
    f=pd.read_csv(candles/f'{symbol}_30m.csv')
    if f.timestamp.duplicated().any():
        raise ValueError(f'{symbol}: duplicate 30m timestamps')
    f=f.sort_values('timestamp').reset_index(drop=True)
    index={int(t)+TF:i for i,t in enumerate(f.timestamp)}
    forms=pd.read_csv(atlas/'shards'/f'{symbol}.formation.csv',
                      keep_default_na=False,float_precision='round_trip')
    events=pd.read_csv(atlas/'shards'/f'{symbol}.events.csv',
                       keep_default_na=False,float_precision='round_trip')
    high_ids=set(forms.loc[forms.origin.eq('HIGH'),'level_id'].astype(str))
    crosses=events.loc[events.event_type.eq('CROSS_UP') & events.level_id.astype(str).isin(high_ids)]
    errors=[]; checked=0
    for e in crosses.to_dict('records'):
        ts=int(e['observed_closed_ts']);i=index.get(ts)
        if i is None or i==0:
            errors.append(f'{symbol} level_id={e["level_id"]} known_ts={ts} MISSING_30M_BAR')
            continue
        try:
            # Formation values used in the original V2.6 branch are the
            # immutable V2.1 zone boundaries, not a recomputed band.
            lvl=forms.loc[forms.level_id.astype(str).eq(str(e['level_id']))]
            if len(lvl)!=1:
                raise ValueError(f'Expected one formation, got {len(lvl)}')
            zone=lvl.iloc[0]
            _validate_v21_cross_up(symbol=symbol,level_id=str(e['level_id']),
                known_ts=ts,event=e,
                previous_close=float(f.at[i-1,'close']),close=float(f.at[i,'close']),
                zone_low=float(zone['zone_low']),zone_high=float(zone['zone_high']))
            checked+=1
        except (RuntimeError,ValueError) as exc:
            errors.append(str(exc))
    return len(crosses), checked, errors


def main(argv=None):
    p=argparse.ArgumentParser()
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    p.add_argument('--atlas-run',type=Path,default=ATLAS)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--max-symbols',type=int,default=0)
    args=p.parse_args(argv)
    conf=json.loads((args.atlas_run/'run_config.json').read_text())
    symbols=conf['symbols'][:args.max_symbols or None]
    print('HLL_V26_CROSS_PLAN',json.dumps(dict(symbols=len(symbols),atlas=str(args.atlas_run),
        candles=str(args.candles_dir),read_only=True,meaning='strict V21 CROSS_UP parity')),
        flush=True)
    if args.plan:return 0
    totals=Counter(); bad=[]
    for i,s in enumerate(symbols,1):
        n,ok,errors=audit_symbol(s,args.atlas_run,args.candles_dir)
        totals['crosses']+=n;totals['matches']+=ok;totals['mismatches']+=len(errors)
        if errors:
            bad.append(s)
            for msg in errors[:3]:
                print('HLL_V26_CROSS_MISMATCH',msg,flush=True)
        if i%25==0 or i==len(symbols):
            print('HLL_V26_CROSS_PROGRESS',json.dumps(dict(symbols_checked=i,total=len(symbols),
                crosses=int(totals['crosses']),mismatches=int(totals['mismatches']))),flush=True)
    passed=totals['mismatches']==0
    print('HLL_V26_CROSS_RESULT',json.dumps(dict(symbols=len(symbols),
        crosses=int(totals['crosses']),matches=int(totals['matches']),
        mismatches=int(totals['mismatches']),bad_symbols=bad[:25],
        pass_check=passed,read_only=True)),flush=True)
    return 0 if passed else 2


if __name__=='__main__':raise SystemExit(main())
