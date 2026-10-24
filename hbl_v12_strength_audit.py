#!/usr/bin/env python3
"""Read-only V1.2 diagnostics; no replays, API access, or LIVE operations.

Usage: python3 hbl_v12_strength_audit.py [run_directory]
"""
from pathlib import Path
import sys
import pandas as pd

DEFAULT = Path('/home/multiservices-1m/reports/horizontal_breakout_lab_v1/lifecycle_v12/runs/5cbde3dd3256d2')
run = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
keys = ['attempt_id', 'level_id', 'symbol', 'signal_closed_ts']
att = pd.read_csv(run / 'attempts.csv')
out = pd.read_csv(run / 'outcomes.csv')
if att.duplicated(keys).any() or out.duplicated(keys).any():
    raise ValueError('Duplicated event key: cannot trust results')
d = att.merge(out, on=keys, how='outer', validate='one_to_one', indicator=True)
if len(d) != len(att) or len(d) != len(out) or (d['_merge'] != 'both').any():
    raise ValueError('Missing or unmatched outcome records')
d = d.drop(columns='_merge').sort_values(['level_id','signal_closed_ts','attempt_id']).copy()
d['first_on_level'] = ~d.duplicated('level_id')
d['signal_day_utc'] = pd.to_datetime(d['signal_closed_ts'],unit='ms',utc=True).dt.strftime('%Y-%m-%d')
d['entry_over_zone_pct'] = (pd.to_numeric(d['entry_price'],errors='coerce') / pd.to_numeric(d['zone_high'],errors='coerce') - 1) * 100


def panel(frame, title):
    print('\n'+ '='*80 + '\n'+title+'  [N='+str(len(frame))+']')
    if frame.empty:
        return
    rows=[]
    for (strength,kind),grp in frame.groupby(['strength_class','trigger_kind'],dropna=False):
        row={'strength':strength,'trigger':kind,'N':len(grp),'unique_levels':grp.level_id.nunique(),
             'entry_vs_top_median_%':round(grp.entry_over_zone_pct.median(),3)}
        for h in (180,360,2880):
            counts=grp[f'tp2_h{h}'].value_counts()
            row.update({f'TP2_{h}':int(counts.get('TP_FIRST',0)),
                        f'SL_{h}':int(counts.get('SL_FIRST',0)),
                        f'NH_{h}':int(counts.get('NO_HIT',0)),
                        f'cens_{h}':int(counts.get('CENSORED',0))})
            raw=pd.to_numeric(grp[f'net_tp2_h{h}_pct'],errors='coerce')
            row[f'net_mean_{h}_%']=round(float(raw.mean()),3) if raw.notna().any() else float('nan')
            row[f'net_n_{h}']=int(raw.notna().sum())
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))

print('RUN',run)
print('Attempts',len(d),'Unique levels',d.level_id.nunique(),'Symbols',d.symbol.nunique())
print('NOTE: net mean = theoretical independent trade returns with TP2/SL1.5, 0.1% fixed fee and NO_HIT mark-to-market at horizon; no slippage/funding, no portfolio slots or overlap control.')
print('NOTE: continuations are a later signal after an initial cross; not directly equivalent to first crossings.')
panel(d,'ALL EVENTS — STRONG vs WEAK, SPLIT BY DIRECT CROSS vs LATER CONTINUATION')
panel(d[d.first_on_level],'ONLY FIRST ATTEMPT PER LEVEL — REDUCES REPEATED SIGNALS')
print('\nBY SYMBOL — signal counts, 360m TP2, theoretical mean net return')
for (symbol,strength),grp in d.groupby(['symbol','strength_class']):
    vc=grp['tp2_h360'].value_counts()
    ret=pd.to_numeric(grp['net_tp2_h360_pct'],errors='coerce')
    print(f'{symbol:>16} {strength:>18} N={len(grp):>3} TP={vc.get("TP_FIRST",0):>2} SL={vc.get("SL_FIRST",0):>2} NH={vc.get("NO_HIT",0):>2} net_mean={ret.mean():+.3f}%' if ret.notna().any() else f'{symbol} {strength} no returns')
print('\nBY UTC DAY — signal counts, regardless of outcome')
print(pd.crosstab(d.signal_day_utc,d.strength_class).to_string())
cont=d[d.trigger_kind=='EXPANSION_CONTINUATION']
print('\nCONTINUATION AGE RELATIVE TO PRIOR CROSS (same level)')
minutes=[]
for x in cont.itertuples():
    prev=d[(d.level_id==x.level_id)&(d.signal_closed_ts<x.signal_closed_ts)]
    if not prev.empty:
        minutes.append((x.signal_closed_ts-int(prev.signal_closed_ts.min()))/60000)
print(f'Continuations N={len(cont)}, with prior attempt={len(minutes)}, delay in minutes min/median/max={round(min(minutes),1)}/{round(pd.Series(minutes).median(),1)}/{round(max(minutes),1)}' if minutes else f'Continuations N={len(cont)}, no earlier attempt found')
print('\nCAVEAT: only five symbols in a previously examined period; none of these comparisons is out-of-sample validation.')
