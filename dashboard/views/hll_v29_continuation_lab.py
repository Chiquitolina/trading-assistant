#!/usr/bin/env python3
"""HLL V2.9 | causal, offline confirmation paths after frozen V2.8.2 PRE breakouts.

Adds TWO experimental tracks, independent of V2.6 retest/flip state:
- BREAKOUT_CONTINUATION: follow-through from CROSS_UP; records whether a retest contact occurred
- RECLAIM_CONTINUATION: a NEW above-zone crossing after the original attempt fails

Signals are known at a completed 30m close; next 30m open is informational,
NEVER used to qualify. Does not modify LIVE, V2.6, V2.8.2, execution, Redis.
Do not equate visual future profitability with a valid trade signal.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

TF=30*60*1000
COHORT=Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
CANDLES=Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT=Path('reports/horizontal_level_continuation_v29/runs/core15_continuation_v29')
FAILURES={'FAILED_BREAKOUT','RETEST_FAILED','ACCEPTED_THEN_FAILED_NO_CONTACT'}

@dataclass(frozen=True)
class Params:
    confirmation_window_bars:int=12
    reclaim_search_bars:int=18
    min_displacement_atr:float=0.50
    max_displacement_atr:float=3.50
    min_close_location:float=0.60
    min_consecutive_above:int=2


def _ts(value):
    if value is None or pd.isna(value) or str(value).strip() in ('','None','nan'):return None
    n=int(float(value));return n if n>0 else None


def _hash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def validate_candles(bars):
    req={'timestamp','open','high','low','close'}
    if req-set(bars):raise ValueError('OHLC 30m missing columns: '+str(req-set(bars)))
    if len(bars)==0:raise ValueError('Empty candles')
    for field in req:
        if pd.to_numeric(bars[field],errors='coerce').isna().any():raise ValueError('Malformed OHLC '+field)
    if bars.timestamp.duplicated().any():raise ValueError('Duplicate 30m timestamps')
    if not bars.timestamp.is_monotonic_increasing:raise ValueError('Nonmonotonic timestamps')
    if (bars.timestamp%TF!=0).any():raise ValueError('Nonaligned 30m timestamps')
    if ((bars.high<bars[['open','low','close']].max(axis=1)) |
            (bars.low>bars[['open','high','close']].min(axis=1)) |
            (bars.low<=0)).any():raise ValueError('Invalid candle wick or OHLC')


def _none(reason):
    return dict(confirmed_ts=None,signal_open_ts=None,signal_close=None,
                next_open_ts=None,next_open=None,displacement_atr=None,
                close_location=None,contact_before_confirmation=False,status=reason)


def first_continuation(bars,anchor_closed_ts,zone_low,zone_high,atr,params=Params()):
    """Confirm momentum using closed 30m candles while preserving V2.6 retest logic.

    A physical contact is recorded, not used to disqualify momentum. This covers
    continuations after a shallow retest, without relabeling V2.6 flips.
    A close below the bottom of the zone terminates this initial breakout episode.
    """
    if not (0<zone_low<=zone_high and atr>0):raise ValueError('Invalid band or ATR')
    anchor_open=anchor_closed_ts-TF
    matches=bars.index[bars.timestamp.eq(anchor_open)]
    if len(matches)!=1:return _none('ANCHOR_MISSING')
    a=int(matches[0])
    if float(bars.at[a,'close'])<=zone_high:return _none('NOT_CROSS_ABOVE_ZONE')
    consecutive=1
    contact=False
    for j in range(a+1,a+params.confirmation_window_bars+1):
        if j>=len(bars) or int(bars.at[j,'timestamp'])!=anchor_open+(j-a)*TF:
            return _none('CENSORED_CONTINUATION')
        row=bars.iloc[j]
        op,hi,lo,cl=[float(row[k]) for k in ('open','high','low','close')]
        if cl<zone_low:return _none('LOST_ZONE_BEFORE_CONTINUATION')
        # Unlike the original strict prototype, a shallow contact does not erase
        # later continuation. Original V2.6 retest/defense remains separate.
        if lo<=zone_high and hi>=zone_low:
            contact=True
        consecutive=consecutive+1 if cl>zone_high else 0
        displacement=(cl-zone_high)/atr
        close_location=(cl-lo)/(hi-lo) if hi>lo else (1.0 if cl>=op else 0.0)
        new_high=hi>float(bars.loc[a:j-1,'high'].max())
        if (consecutive>=params.min_consecutive_above and
            params.min_displacement_atr<=displacement<=params.max_displacement_atr and
            close_location>=params.min_close_location and new_high):
            known_ts=int(row.timestamp)+TF
            next_bar=bars.iloc[j+1] if j+1<len(bars) and int(bars.at[j+1,'timestamp'])==known_ts else None
            return dict(confirmed_ts=known_ts,signal_open_ts=int(row.timestamp),
                        signal_close=cl,next_open_ts=known_ts if next_bar is not None else None,
                        next_open=float(next_bar.open) if next_bar is not None else None,
                        displacement_atr=round(displacement,5),
                        close_location=round(close_location,5),
                        contact_before_confirmation=contact,status='CONFIRMED')
    return _none('NO_EARLY_CONTINUATION')


def reclaim_continuation(bars,original_breakout_ts,failed_ts,zone_low,zone_high,atr,params=Params()):
    """Re-cross AFTER known failure is a new episode, not a resurrected old signal."""
    if failed_ts is None:return _none('NO_ORIGINAL_FAILURE'),None
    failure_open=failed_ts-TF
    idx=bars.index[bars.timestamp.eq(failure_open)]
    if len(idx)!=1:return _none('FAILURE_CANDLE_MISSING'),None
    f=int(idx[0])
    if failed_ts<=original_breakout_ts or float(bars.at[f,'close'])>=zone_low:
        return _none('NOT_CONFIRMED_LOSS'),None
    censored=False
    for j in range(f+1,f+params.reclaim_search_bars+1):
        if j>=len(bars) or int(bars.at[j,'timestamp'])!=failure_open+(j-f)*TF:
            censored=True;break
        if float(bars.at[j-1,'close'])<=zone_high and float(bars.at[j,'close'])>zone_high:
            anchor=int(bars.at[j,'timestamp'])+TF
            event=first_continuation(bars,anchor,zone_low,zone_high,atr,params)
            if event['status']=='CONFIRMED':return event,anchor
    return _none('CENSORED_RECLAIM' if censored else 'NO_RECLAIM_CONTINUATION'),None


def events_for_attempt(row,bars,params=Params()):
    breakout=_ts(row['breakout_closed_ts'])
    if breakout is None:raise ValueError('Missing breakout closed timestamp')
    lo,hi,atr=[float(row[k]) for k in ('zone_low','zone_high','atr_pre')]
    ident=str(row['attempt_id'])
    direct=first_continuation(bars,breakout,lo,hi,atr,params)
    # IMPORTANT: never reinterpret a V2.6 defended flip's later loss as a failed
    # initial breakout. That is a distinct structural outcome.
    failure=_ts(row.get('failed_closed_ts')) if str(row.get('v26_status')) in FAILURES else None
    reclaim,reanchor=reclaim_continuation(bars,breakout,failure,lo,hi,atr,params)
    out=[]
    for branch,ev,anchor in [('BREAKOUT_CONTINUATION',direct,breakout),('RECLAIM_CONTINUATION',reclaim,reanchor)]:
        out.append(dict(attempt_id=ident,origin_symbol=str(row['symbol']),
            level_id=str(row.get('level_id','')),pre_pattern_48=str(row.get('pre_pattern_48','')),
            v26_status=str(row.get('v26_status','')),
            branch=branch,branch_anchor_closed_ts=anchor,
            signal_id=(f'{ident}|{branch}|{ev["confirmed_ts"]}' if ev['confirmed_ts'] else ''),
            **ev))
    return out


def calculate(cohort,candles_dir,params=Params(),max_symbols=0):
    req={'attempt_id','symbol','breakout_closed_ts','zone_low','zone_high','atr_pre','v26_status','failed_closed_ts'}
    if req-set(cohort):raise ValueError('Missing PRE cohort fields: '+str(req-set(cohort)))
    if cohort.attempt_id.isna().any() or cohort.attempt_id.duplicated().any():raise ValueError('Duplicate/blank attempt_id')
    syms=sorted(cohort.symbol.unique())
    if max_symbols>0:syms=syms[:max_symbols]
    records=[]
    for i,symbol in enumerate(syms,1):
        source=candles_dir/f'{symbol}_30m.csv'
        if not source.is_file():raise FileNotFoundError(source)
        b=pd.read_csv(source,usecols=['timestamp','open','high','low','close']).sort_values('timestamp').reset_index(drop=True)
        validate_candles(b)
        for row in cohort.loc[cohort.symbol.eq(symbol)].to_dict('records'):
            records.extend(events_for_attempt(row,b,params))
        print('HLL_V29_SYMBOL',json.dumps({'i':i,'symbol':symbol,'attempts':int(cohort.symbol.eq(symbol).sum())}),flush=True)
    ans=pd.DataFrame(records)
    if len(ans)!=2*sum(cohort.symbol.isin(syms)) or ans[['attempt_id','branch']].duplicated().any():
        raise ValueError('Unexpected signal cardinality')
    return ans


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--plan',action='store_true');m.add_argument('--run',action='store_true')
    p.add_argument('--cohort',type=Path,default=COHORT)
    p.add_argument('--candles-dir',type=Path,default=CANDLES)
    p.add_argument('--output-dir',type=Path,default=OUTPUT)
    p.add_argument('--max-symbols',type=int,default=0)
    args=p.parse_args(argv)
    if not args.cohort.is_file():raise SystemExit('Missing V282 cohort: '+str(args.cohort))
    cohort=pd.read_csv(args.cohort,low_memory=False)
    if cohort.attempt_id.isna().any() or cohort.attempt_id.duplicated().any():raise SystemExit('Bad cohort IDs')
    symbols=sorted(cohort.symbol.unique());symbols=symbols[:args.max_symbols] if args.max_symbols else symbols
    plan=dict(version='HLL_V29_CONTINUATION_EXPERIMENTAL_V1',mode='OFFLINE_READ_ONLY',
              attempts=int(cohort.symbol.isin(symbols).sum()),symbols=len(symbols),
              tracks=['BREAKOUT_CONTINUATION','RECLAIM_CONTINUATION'],
              thresholds=asdict(Params()),cohort=str(args.cohort),output=str(args.output_dir),
              no_live=True,no_trading=True,original_v26_unchanged=True)
    print('HLL_V29_PLAN',json.dumps(plan,ensure_ascii=False),flush=True)
    if args.plan:return 0
    out=args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):raise SystemExit('OUTPUT_EXISTS: cannot overwrite a research run')
    for inp in (args.cohort.resolve(),args.candles_dir.resolve()):
        if out==inp or inp in out.parents:raise SystemExit('Output inside source')
    if shutil.disk_usage(Path.cwd()).free<150*1024*1024:raise SystemExit('Not enough free disk')
    records=calculate(cohort,args.candles_dir,max_symbols=args.max_symbols)
    out.mkdir(parents=True,exist_ok=True)
    records.to_csv(out/'continuation_events.csv',index=False)
    summary={str(k):int(v) for k,v in records.loc[records.status.eq('CONFIRMED')].groupby('branch').size().items()}
    manifest=dict(**plan,source_sha256=_hash(args.cohort),total_rows=len(records),
                  confirmed_by_branch=summary,limitations='Exploratory signal events: breakout momentum may follow zone contact; original V2.6 flips unchanged. All thresholds fixed a priori. No TP/SL or validated edge. Close-known only, next-open informational.')
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_V29_DONE',json.dumps({'rows':len(records),'confirmed_by_branch':summary,'output':str(out)},ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
