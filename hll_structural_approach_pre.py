#!/usr/bin/env python3
"""Read-only descriptive PRE structural approach audit. Python stdlib only."""
import argparse, csv, hashlib, json, math
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_SOURCE = Path('reports/horizontal_level_second_ascent_pre/core15_swing_topology_pre_v1')
DEFAULT_OUTPUT = Path('reports/horizontal_level_second_ascent_pre/core15_structural_approach_pre_v1')
REQUIRED = ('all_two_leg_chains_pre.csv','structural_swings_pre.csv','swing_summary_pre.csv','manifest.json')
BAR_MS = 1800000
FIELDS = ['attempt_id','symbol','pre_closed_ts','threshold_atr','chain_id','h2_open_ts','h2_pivot_known_ts','h2_swing_known_ts','h2_status','h2_age_bars','chain_duration_bars','first_rise_bars','correction_bars','second_rise_bars','first_rise_atr','correction_atr','second_rise_atr','correction_retention_ratio','second_vs_first_rise_ratio','pivot_path_efficiency_proxy','l1_vs_l0_atr','h2_vs_h1_atr','h2_distance_zone_atr','chain_status','later_confirmed_l_below_l1','later_confirmed_h_above_h2','later_swings_count','last_confirmed_swing_kind','last_confirmed_swing_age_bars','diagnostic_flags']
SUMMARY_FIELDS = ['attempt_id','symbol','pre_closed_ts','threshold_atr','chains_count','latest_chain_id','latest_h2_age_bars','latest_chain_status','latest_chain_h2_distance_zone_atr','latest_chain_efficiency_proxy','latest_chain_later_l_below_l1','latest_chain_later_h_above_h2','recent_chains_12bars','fully_confirmed_chains','near_zone_chains_1atr','ambiguous_dual_extreme_bars','scale_consensus_latest_h2_open','scale_consensus_latest_chain_status','diagnostic_flags']

def read_csv(path):
    with path.open(newline='',encoding='utf-8-sig') as f: return list(csv.DictReader(f))
def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):return None

def integer(v):
    x=num(v)
    return int(x) if x is not None and x.is_integer() else None

def rounded(x):return '' if x is None else round(x,6)
def div(a,b):return a/b if a is not None and b is not None and b>0 else None

def seen_pre(row, pre):
    # Confirmed at PRE, cannot use the bar's pivot time as if confirmation time.
    t=integer(row.get('swing_known_ts'))
    return t is not None and t<=pre and row.get('status')=='SWING_CONFIRMED'

def validate(rows, swings, summaries):
    if not summaries:raise ValueError('Missing chain/summary data')
    keys=set()
    for r in rows:
        key=(r['attempt_id'],r['threshold_atr'],r['chain_id'])
        if key in keys:raise ValueError('Duplicate chain: '+str(key))
        keys.add(key)
        pre=integer(r['pre_closed_ts'])
        if pre is None:raise ValueError('Invalid PRE timestamp')
        for name in ('l0_known_ts','h1_known_ts','l1_known_ts','h2_pivot_known_ts'):
            t=integer(r[name]);
            if t is None or t>pre:raise ValueError('Non causal pivot: '+str(key)+' '+name)
        st=integer(r.get('h2_swing_known_ts'))
        if r['h2_status']=='SWING_CONFIRMED' and (st is None or st>pre):raise ValueError('Non causal H2 swing: '+str(key))
        for name in ('l0_open_ts','h1_open_ts','l1_open_ts','h2_open_ts'):
            t=integer(r[name]);
            if t is None or t>pre:raise ValueError('Non causal pivot open: '+str(key)+' '+name)
    for r in swings:
        pre=integer(r['pre_closed_ts']); t=integer(r['pivot_known_ts'])
        if pre is None or t is None or t>pre:raise ValueError('Non causal swing pivot')
        if r['status']=='SWING_CONFIRMED' and not seen_pre(r,pre):raise ValueError('Non causal swing confirmation')
    for r in summaries:
        if integer(r['pre_closed_ts']) is None:raise ValueError('Invalid summary PRE')

def analyze(chains,swings,summaries):
    validate(chains,swings,summaries)
    swing_idx=defaultdict(list)
    for s in swings:
        k=(s['attempt_id'],s['threshold_atr']);swing_idx[k].append(s)
    summary_idx={(s['attempt_id'],s['threshold_atr']):s for s in summaries}
    if len(summary_idx)!=len(summaries):raise ValueError('Duplicate summary key')
    chain_idx=defaultdict(list)
    for c in chains:chain_idx[(c['attempt_id'],c['threshold_atr'])].append(c)
    out=[]; summary=[]
    for key in sorted(summary_idx, key=lambda k:(k[0],float(k[1]))):
        sm=summary_idx[key]; pre=integer(sm['pre_closed_ts']); ss=swing_idx.get(key,[])
        confirmed=sorted((s for s in ss if seen_pre(s,pre)),key=lambda s:(integer(s['open_ts']),integer(s['swing_known_ts']),s['kind']))
        chains_for_key=sorted(chain_idx.get(key,[]),key=lambda c:(integer(c['h2_open_ts']),integer(c['h2_pivot_known_ts']),integer(c['chain_id'])))
        for c in chains_for_key:
            h2=integer(c['h2_open_ts']);l0=integer(c['l0_open_ts']); l1price=num(c['l1_price']);h2price=num(c['h2_price'])
            future=[s for s in confirmed if integer(s['open_ts'])>h2]
            later_l=any(s['kind']=='L' and num(s['price']) is not None and num(s['price'])<l1price for s in future) if l1price is not None else False
            later_h=any(s['kind']=='H' and num(s['price']) is not None and num(s['price'])>h2price for s in future) if h2price is not None else False
            a=num(c['first_rise_atr']);b=num(c['correction_atr']);d=num(c['second_rise_atr'])
            # A *pivot path* proxy, never OHLC path efficiency. Counts two legs up and correction down.
            net=(a or 0)-(b or 0)+(d or 0)
            proxy=div(abs(net),sum(abs(v) for v in (a,b,d) if v is not None)) if None not in (a,b,d) else None
            flags=[]
            if c['h2_status']!='SWING_CONFIRMED':flags.append('H2_PIVOT_ONLY')
            if integer(sm.get('excluded_dual_extreme_bars')) not in (None,0):flags.append('DUAL_EXTREME_EXCLUDED')
            if later_l:flags.append('LATER_CONFIRMED_LOWER_LOW')
            if num(c['h2_vs_h1_atr']) is not None and num(c['h2_vs_h1_atr'])<=0:flags.append('H2_NOT_HIGHER_HIGH')
            row={'attempt_id':c['attempt_id'],'symbol':c['symbol'],'pre_closed_ts':c['pre_closed_ts'],'threshold_atr':c['threshold_atr'],'chain_id':c['chain_id'],'h2_open_ts':c['h2_open_ts'],'h2_pivot_known_ts':c['h2_pivot_known_ts'],'h2_swing_known_ts':c['h2_swing_known_ts'],'h2_status':c['h2_status'],'h2_age_bars':rounded((pre-h2)/BAR_MS),'chain_duration_bars':rounded((h2-l0)/BAR_MS),'first_rise_bars':c['first_rise_bars'],'correction_bars':c['correction_bars'],'second_rise_bars':c['second_rise_bars'],'first_rise_atr':c['first_rise_atr'],'correction_atr':c['correction_atr'],'second_rise_atr':c['second_rise_atr'],'correction_retention_ratio':rounded(div(b,a)),'second_vs_first_rise_ratio':rounded(div(d,a)),'pivot_path_efficiency_proxy':rounded(proxy),'l1_vs_l0_atr':c['correction_low_vs_first_low_atr'],'h2_vs_h1_atr':c['h2_vs_h1_atr'],'h2_distance_zone_atr':c['h2_distance_zone_atr'],'chain_status':'LATER_CONFIRMED_L_BELOW_L1' if later_l else ('LATER_CONFIRMED_H_ABOVE_H2' if later_h else 'NO_CONFIRMED_BREACH_SEEN'),'later_confirmed_l_below_l1':int(later_l),'later_confirmed_h_above_h2':int(later_h),'later_swings_count':len(future),'last_confirmed_swing_kind':confirmed[-1]['kind'] if confirmed else '', 'last_confirmed_swing_age_bars':rounded((pre-integer(confirmed[-1]['open_ts']))/BAR_MS) if confirmed else '','diagnostic_flags':'|'.join(flags)}
            out.append(row)
        latest=next((r for r in reversed(out) if (r['attempt_id'],r['threshold_atr'])==key),None)
        flags=[]
        if integer(sm.get('excluded_dual_extreme_bars')) not in (None,0):flags.append('DUAL_EXTREME_EXCLUDED')
        if latest is None:flags.append('NO_CHAIN')
        summary.append({'attempt_id':sm['attempt_id'],'symbol':sm['symbol'],'pre_closed_ts':sm['pre_closed_ts'],'threshold_atr':sm['threshold_atr'],'chains_count':len(chains_for_key),'latest_chain_id':latest['chain_id'] if latest else '', 'latest_h2_age_bars':latest['h2_age_bars'] if latest else '', 'latest_chain_status':latest['chain_status'] if latest else '', 'latest_chain_h2_distance_zone_atr':latest['h2_distance_zone_atr'] if latest else '', 'latest_chain_efficiency_proxy':latest['pivot_path_efficiency_proxy'] if latest else '', 'latest_chain_later_l_below_l1':latest['later_confirmed_l_below_l1'] if latest else '', 'latest_chain_later_h_above_h2':latest['later_confirmed_h_above_h2'] if latest else '', 'recent_chains_12bars':sum(0 <=(pre-integer(c['h2_open_ts']))/BAR_MS<=12 for c in chains_for_key), 'fully_confirmed_chains':sum(c['h2_status']=='SWING_CONFIRMED' for c in chains_for_key), 'near_zone_chains_1atr':sum(num(c['h2_distance_zone_atr']) is not None and abs(num(c['h2_distance_zone_atr']))<=1 for c in chains_for_key),'ambiguous_dual_extreme_bars':sm.get('excluded_dual_extreme_bars',''),'scale_consensus_latest_h2_open':'','scale_consensus_latest_chain_status':'','diagnostic_flags':'|'.join(flags)})
    by_attempt=defaultdict(list)
    for s in summary:by_attempt[s['attempt_id']].append(s)
    for group in by_attempt.values():
        h2s={next((r['h2_open_ts'] for r in out if r['attempt_id']==s['attempt_id'] and r['threshold_atr']==s['threshold_atr'] and r['chain_id']==s['latest_chain_id']),None) for s in group}
        states={s['latest_chain_status'] for s in group}
        for s in group:s['scale_consensus_latest_h2_open']=int(len(h2s)==1 and None not in h2s);s['scale_consensus_latest_chain_status']=int(len(states)==1)
    return out,summary

def write_csv(path,rows,columns):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,columns);w.writeheader();w.writerows(rows)

def run(source,output):
    missing=[n for n in REQUIRED if not (source/n).is_file()]
    plan={'source':str(source),'output':str(output),'missing':missing,'read_only_inputs':True,'no_live':True,'no_dashboard_changes':True,'needs_ohlc_for_true_path_metrics':True}
    return plan

def main():
    p=argparse.ArgumentParser();p.add_argument('--plan',action='store_true');p.add_argument('--run',action='store_true');p.add_argument('--source',type=Path,default=DEFAULT_SOURCE);p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    args=p.parse_args()
    plan=run(args.source,args.output);print('HLL_APPROACH_PLAN '+json.dumps(plan,ensure_ascii=False))
    if not args.run:return
    if plan['missing']:raise SystemExit('Missing required files: '+','.join(plan['missing']))
    source_manifest=json.loads((args.source/'manifest.json').read_text(encoding='utf-8'))
    chains,swings,summaries=[read_csv(args.source/n) for n in REQUIRED[:3]]
    details,overview=analyze(chains,swings,summaries)
    args.output.mkdir(parents=True,exist_ok=True)
    write_csv(args.output/'approach_chains_pre.csv',details,FIELDS)
    write_csv(args.output/'approach_summary_pre.csv',overview,SUMMARY_FIELDS)
    # Visual tags are *never* read to choose a chain, calculate features or construct output.
    manifest={'method':'descriptive PRE only; not classifier or outcomes','input_source':str(args.source),'upstream_manifest':source_manifest,'rows_chains':len(details),'rows_level_scales':len(overview),'unique_levels':len({r['attempt_id'] for r in overview}),'source_hashes':{n:hashlib.sha256((args.source/n).read_bytes()).hexdigest() for n in REQUIRED},'no_post':True,'no_live':True,'no_dashboard_changes':True,'selection':'latest H2 by pivot open time, deterministic and label-blind','limitations':['Breakout-conditioned CORE15 sample, not population of all levels','Pivot path proxy is NOT OHLC candle efficiency','Confirmation-only subsequent breaks, cannot infer intrabar order','No actual candle overlap/ATR dynamic persistence without source OHLC','No trading signals or performance claims']}
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('HLL_APPROACH_DONE '+json.dumps({'output':str(args.output),'levels':manifest['unique_levels'],'chains':len(details),'level_scale_rows':len(overview)},ensure_ascii=False))
if __name__=='__main__':main()
