#!/usr/bin/env python3
"""Runner Entry Lab V3: causal OFFLINE comparison over the V2 frozen cohort.

Reads V2 SQLite/features/audit, original Auto Paths SQLite strictly read-only.
No Binance network, market data producer, trade execution or writes to V1/V2.
Reference retest price is the confirmed 1m retest candle CLOSE (not swing level).
Theoretical first-next-open is an unreachable-for-some benchmark, NOT LIVE.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

import runner_precursor_pilot as v1
import runner_precursor_validation_v2 as v2

BASE = Path('reports/reaction_lab')
V2_DB = BASE / 'runner_precursor_validation_v2.sqlite3'
V2_OUT = BASE / 'runner_precursor_validation_v2'
AUTO_DB = BASE / 'auto_paths_v1.sqlite3'
OUT = BASE / 'runner_entry_lab_v3'
HORIZON = 180
ENTRY_MODES = ('THEORETICAL_NEXT_OPEN','OBSERVED_NEXT_OPEN','CONFIRM_1M','REVISIT_RETEST_REF_1M')
STOP_MODES = ('FIXED_1','RETEST_WICK_0P10','VOL_0P75_CLIP')
TPS = (2.0,3.0)
ROUND_TRIP_COST_SENSITIVITY_PCT = 0.12  # hypothetical scenario; NOT measured fees.


def signed_pct(px, base, side):
    if base <= 0: raise ValueError('invalid base')
    return (px / base - 1) * 100 * (1 if side == 'LONG' else -1)


def observed_open_index(first_seen_ms, first_ts):
    """First 1m OPEN strictly AFTER first_seen_ms, not same bar open."""
    return max(0, (int(first_seen_ms)-int(first_ts))//v1.MIN+1)


def get_retest_candle(con, sample):
    row=con.execute('''SELECT open_ts,open,high,low,close FROM candles
           WHERE symbol=? AND timeframe='1m' AND open_ts=?''',
           (sample['symbol'],int(sample['retest_ts']))).fetchone()
    if row is None or int(row[0]) != int(sample['retest_ts']):
        raise ValueError('Missing retest 1m candle for '+sample['event_key'])
    if not all(math.isfinite(float(x)) and float(x)>0 for x in row[1:]):
        raise ValueError('Invalid retest candle for '+sample['event_key'])
    return tuple(row)


def resolve_entry(rows, retest, side, first_seen_ms, observation_type, mode):
    """Return (entry_bar_index, reason). All decisions after closed candle, fill next open."""
    if mode=='THEORETICAL_NEXT_OPEN': return 0, 'THEORETICAL_ORACLE_TIMING'
    if observation_type!='LIVE_OBSERVED': return None,'RECOVERED_NOT_LIVE_OBSERVABLE'
    available=observed_open_index(first_seen_ms,rows[0][0])
    if available>=HORIZON: return None,'FIRST_SEEN_AFTER_HORIZON'
    if mode=='OBSERVED_NEXT_OPEN': return available,'OBSERVED_OPEN'
    rts,ro,rh,rl,rc=retest
    if mode=='CONFIRM_1M':
        for i in range(min(15,HORIZON-1)):
            ts,o,h,l,c=rows[i]
            # Can only act on a bar that closes AFTER the signal is actually visible.
            if ts+v1.MIN < first_seen_ms: continue
            valid=(c>rh and c>o) if side=='LONG' else (c<rl and c<o)
            if not valid: continue
            entry_i=i+1
            if entry_i<available: continue
            drift=signed_pct(rows[entry_i][1],rows[0][1],side)
            if -0.75<=drift<=0.75: return entry_i,'RETEST_WICK_CLOSE_BREAK'
        return None,'NO_CONFIRMATION_OR_CHASE_CAP'
    if mode=='REVISIT_RETEST_REF_1M':
        # Proxy: return to original RETEST candle close, not an inferred 15m swing level.
        departed=False
        for i in range(min(60,HORIZON-1)):
            ts,o,h,l,c=rows[i]
            if ts+v1.MIN < first_seen_ms: continue
            if not departed:
                if signed_pct(c,rc,side)>=0.50: departed=True
                continue  # must be a LATER separate bar
            if side=='LONG':
                retouch=rc*0.999<=l<=rc*1.001 and c>=rc and c>=o
            else:
                retouch=rc*0.999<=h<=rc*1.001 and c<=rc and c<=o
            if not retouch: continue
            entry_i=i+1
            if entry_i<available: continue
            drift=signed_pct(rows[entry_i][1],rc,side)
            if abs(drift)<=0.50: return entry_i,'REVISIT_REF_AFTER_CONFIRMED_DEPARTURE'
        return None,'NO_DEPARTURE_OR_REVISIT'
    raise ValueError('Unknown entry mode '+mode)


def stop_for(mode, entry, retest, side, vol):
    if mode=='FIXED_1':
        distance=1.0
    elif mode=='VOL_0P75_CLIP':
        if not math.isfinite(vol) or vol<=0: return None,None,'INVALID_PREVOL'
        distance=max(0.75,min(2.50,0.75*vol))
    elif mode=='RETEST_WICK_0P10':
        rts,ro,rh,rl,rc=retest
        stop=rl*0.999 if side=='LONG' else rh*1.001
        distance=-signed_pct(stop,entry,side)
        if distance<0.25 or distance>3.0:
            return None,None,'WICK_STOP_RISK_OUTSIDE_0P25_TO_3P00'
    else:raise ValueError('Unknown stop mode')
    price=entry*(1-distance/100) if side=='LONG' else entry*(1+distance/100)
    if price<=0:return None,None,'INVALID_STOP_PRICE'
    return price,distance,'VALID'


def evaluate_trade(rows,side,entry_i,stop,stop_pct,tp,horizon=HORIZON):
    """Same first-touch convention as V2; conservative SL for same-bar ambiguity.

    Gross NO_HIT is mark-to-market at COMMON original retest+180m close.
    Stops fill at threshold (optimistic if price gaps); flag gaps explicitly.
    """
    entry=rows[entry_i][1]
    take=entry*(1+tp/100) if side=='LONG' else entry*(1-tp/100)
    if take<=0:raise ValueError('invalid TP price')
    for i in range(entry_i,min(horizon,len(rows))):
        ts,o,h,l,c=rows[i]
        if side=='LONG':
            hit_tp=h>=take;hit_sl=l<=stop
            gap_through_stop=o<stop
        else:
            hit_tp=l<=take;hit_sl=h>=stop
            gap_through_stop=o>stop
        if hit_tp and hit_sl:
            return {'state':'AMBIGUOUS_SAME_1M','exit_minute':i+1,'gross_pct':-stop_pct,
                    'gap_through_stop':int(gap_through_stop),'ambiguous_lower_bound':True}
        if hit_sl:
            return {'state':'SL_FIRST','exit_minute':i+1,'gross_pct':-stop_pct,
                    'gap_through_stop':int(gap_through_stop),'ambiguous_lower_bound':False}
        if hit_tp:
            return {'state':'TP_FIRST','exit_minute':i+1,'gross_pct':tp,
                    'gap_through_stop':0,'ambiguous_lower_bound':False}
    close=rows[min(horizon,len(rows))-1][4]
    return {'state':'TIME_EXIT','exit_minute':min(horizon,len(rows)),
            'gross_pct':signed_pct(close,entry,side),'gap_through_stop':0,
            'ambiguous_lower_bound':False}


def original_v2_selection_hash(samples):
    """Recreate the SHA-256 input used *before* V2 inserted the cohort into SQLite.

    v1.outcomes() produces integer zero through ``max(0, ...)`` when an MFE or
    MAE is exactly zero. SQLite's REAL columns deserialize those integer zeros
    to floats (0.0), changing json.dumps() and thus v1.sample_hash(). Only
    these two known outcome fields need their original JSON scalar type restored.
    Every other field, sample, order, and numeric value is hashed unchanged.
    """
    original=[]
    for s in samples:
        row=dict(s)
        for field in ('mfe_180_pct','mae_180_pct'):
            if type(row[field]) is float and row[field]==0.0:
                row[field]=0
        original.append(row)
    return v1.sample_hash(original)


def load_inputs(root):
    output=root/V2_OUT
    with closing(v1.ro(root/V2_DB)) as con:
        samples=v1.load_samples(con)
        meta=dict(con.execute('SELECT key,value FROM meta'))
    if meta.get('purpose')!='UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION':
        raise ValueError('Expected the isolated V2 cohort, not V1')
    if len(samples)!=600 or len({s['event_key'] for s in samples})!=600:
        raise ValueError('V2 sample must be the 600 frozen unique cases')
    digest=original_v2_selection_hash(samples)
    if digest!=meta['sample_sha256']:
        raise ValueError('Frozen V2 sample SHA does not match SQLite metadata')
    audit=json.loads((output/'audit.json').read_text(encoding='utf-8'))
    if audit.get('success') is not True or audit.get('frozen_sample_hash')!=digest or audit.get('sample_count')!=600:
        raise ValueError('V2 audit must be success=true with matching frozen hash')
    feature_rows=v2.read_csv(output/'features_before_retest.csv')
    baseline_rows=v2.read_csv(output/'historical_validation_events.csv')
    features={x['event_key']:x for x in feature_rows}
    baseline={x['event_key']:x for x in baseline_rows}
    if len(features)!=len(feature_rows) or len(baseline)!=len(baseline_rows):
        raise ValueError('Duplicate V2 event keys in exported files')
    keys={s['event_key'] for s in samples}
    if set(features)!=keys or set(baseline)!=keys:
        raise ValueError('V2 features or outcomes are not the frozen 600 cases')
    if any(int(x['high_vol_pilot_p75'])!=int(float(features[k][v2.VOL])>=float(meta['volatility_pilot_p75']))
           for k,x in baseline.items()):
        raise ValueError('V2 high-vol labels changed versus frozen V1 threshold')
    return samples,meta,features,baseline


def verify_reference_parity(records, baseline):
    """Must exactly reproduce existing 600 V2 first-touch states & minute indices."""
    cases=[x for x in records if x['entry_mode']=='THEORETICAL_NEXT_OPEN'
           and x['stop_mode']=='FIXED_1']
    if len(cases)!=len(baseline)*len(TPS):
        raise ValueError('Missing reference parity rows')
    for r in cases:
        key=r['event_key'];tp=int(r['tp_pct']); b=baseline[key]
        state=b[f'tp{tp}_sl1_state']
        if state=='NO_HIT': state='TIME_EXIT'
        minute=b[f'tp{tp}_sl1_minute']
        minute=None if minute in ('','None',None) else int(float(minute))
        actual_minute=None if r['state']=='TIME_EXIT' else int(r['exit_minute'])
        if r['state']!=state or actual_minute!=minute:
            raise ValueError(f'V2 FIRST-TOUCH PARITY FAILED {key} TP{tp}: '+
                             f'expected={state}/{minute}, got={r["state"]}/{actual_minute}')
    return len(cases)


def simulate(root,samples,meta,features,baseline):
    keys={s['event_key'] for s in samples}
    paths=v2.decode_outcomes(root,keys)
    records=[]
    deltas=[]
    with closing(v1.ro(root/V2_DB)) as db:
        for s in samples:
            key=s['event_key']; side=s['side']; f=features[key]
            p=paths[key]
            if len(p)!=360 or p[0][0]!=s['known_ts']:
                raise ValueError('Auto Paths continuity/anchor mismatch '+key)
            retest=get_retest_candle(db,s)
            vol=float(f[v2.VOL]); is_high=vol>=float(meta['volatility_pilot_p75'])
            obs_delay=(s['first_seen_ms']-s['known_ts'])/v1.MIN
            deltas.append(obs_delay)
            for entry_mode in ENTRY_MODES:
                entry_i,entry_reason=resolve_entry(p,retest,side,s['first_seen_ms'],s['observation_type'],entry_mode)
                for stop_mode in STOP_MODES:
                    stop=None;stop_pct=None;stop_reason='NOT_ENTERED'
                    if entry_i is not None:
                        stop,stop_pct,stop_reason=stop_for(stop_mode,p[entry_i][1],retest,side,vol)
                    for tp in TPS:
                        r={'event_key':key,'symbol':s['symbol'],'side':side,
                           'day_utc':v1.day(s['retest_ts']),'episode_id':s.get('episode_id') or '',
                           'observation_type':s['observation_type'],
                           'first_seen_delay_minutes':round(obs_delay,6),
                           'runner_label_for_stratification_only':s['class_label'],
                           'volatility_60_pct':round(vol,7),'high_vol_pilot_p75':int(is_high),
                           'entry_mode':entry_mode,'stop_mode':stop_mode,'tp_pct':tp,
                           'entry_minute':entry_i+1 if entry_i is not None else None,
                           'entry_open':p[entry_i][1] if entry_i is not None else None,
                           'entry_reason':entry_reason,'stop_pct':stop_pct,
                           'stop_reason':stop_reason,'state':'NO_ENTRY',
                           'exit_minute':None,'gross_pct':0.0,'net_sensitivity_pct':0.0,
                           'gap_through_stop':0,'ambiguous_lower_bound':False}
                        if entry_i is not None and stop is None:
                            r['state']='INVALID_STOP'
                        elif entry_i is not None:
                            result=evaluate_trade(p,side,entry_i,stop,stop_pct,tp)
                            r.update(result)
                            r['net_sensitivity_pct']=r['gross_pct']-ROUND_TRIP_COST_SENSITIVITY_PCT
                        records.append(r)
    assert len(records)==len(samples)*len(ENTRY_MODES)*len(STOP_MODES)*len(TPS)
    parity=verify_reference_parity(records,baseline)
    return records,parity,deltas


def group_stats(rows, denominator):
    n=len(rows)
    states=Counter(r['state'] for r in rows)
    filled=n-states.get('NO_ENTRY',0)-states.get('INVALID_STOP',0)
    wins=states.get('TP_FIRST',0)
    net=sum(r['net_sensitivity_pct'] for r in rows)
    return {'opportunities':denominator,'entries':filled,'entry_rate':round(filled/denominator,6) if denominator else None,
            'tp_first':wins,'tp_first_per_all':round(wins/denominator,6) if denominator else None,
            'tp_first_per_entry':round(wins/filled,6) if filled else None,
            'states':dict(states),
            'gross_pct_per_opportunity':round(sum(r['gross_pct'] for r in rows)/denominator,6) if denominator else None,
            'net_sensitivity_pct_per_opportunity':round(net/denominator,6) if denominator else None,
            'net_sensitivity_pct_per_entry':round(net/filled,6) if filled else None,
            'gap_stop_count':sum(int(r['gap_through_stop']) for r in rows),
            'ambiguous_count':states.get('AMBIGUOUS_SAME_1M',0)}


def summarize(records,meta,parity,deltas):
    n=len({r['event_key'] for r in records})
    groups=[]
    for high in ('ALL','HIGH_VOL','OTHER'):
        sel=[r for r in records if high=='ALL' or bool(r['high_vol_pilot_p75'])==(high=='HIGH_VOL')]
        denominator=len({r['event_key'] for r in sel})
        for em in ENTRY_MODES:
            for sm in STOP_MODES:
                for tp in TPS:
                    sub=[r for r in sel if r['entry_mode']==em and r['stop_mode']==sm and r['tp_pct']==tp]
                    x={'group':high,'entry_mode':em,'stop_mode':sm,'tp_pct':tp}
                    x.update(group_stats(sub,denominator))
                    groups.append(x)
    # Scope check: high-risk of confusing LIVE-observed with oracle entry.
    obs=Counter(r['observation_type'] for r in records if r['entry_mode']=='THEORETICAL_NEXT_OPEN'
                and r['stop_mode']=='FIXED_1' and r['tp_pct']==3)
    return {'version':'runner_entry_lab_v3.0','research_only':True,
            'sample_count':n,'source_frozen_sha256':meta['sample_sha256'],
            'reference_parity_rows':parity,
            'v1_vol_p75_frozen':float(meta['volatility_pilot_p75']),
            'source_observation_types':dict(obs),
            'first_seen_delay_min_median':statistics.median(deltas),
            'first_seen_delay_min_p95':v2.percentile(deltas,.95),
            'entry_strategies':list(ENTRY_MODES),'stop_strategies':list(STOP_MODES),
            'tp_levels_pct':list(TPS),'common_horizon_from_original_retest_min':HORIZON,
            'cost_sensitivity_round_trip_pct':ROUND_TRIP_COST_SENSITIVITY_PCT,
            'group_comparisons':groups,
            'caveats':[
                'THEORETICAL_NEXT_OPEN is oracle timing: ignores first_seen and includes recovered windows',
                'All other entries exclude RECOVERED_WINDOW (not contemporaneously observable)',
                'Entry at first 1m open STRICTLY AFTER observation; signals use completed 1m bars',
                'Revisit uses retest 1m CLOSE as reference, NOT confirmed 15m swing level',
                'Horizon is fixed at original retest+180m; delayed strategies have less holding time',
                'TP/SL same candle marked ambiguous and conservatively scored as SL',
                'SL filled at barrier without gap/slippage guarantee; gap-through events counted',
                '0.12 percent round-trip cost is an arbitrary SENSITIVITY, not actual measured costs',
                'Sample is 10-day historical cohort (not true out-of-time validation)',
                'All missed/no-entry opportunities included as zero in per-opportunity metrics',
                'Retest/stop rules were inspired by V2 outcomes; V3 is exploratory only'
            ]}


def run(root,do_write=True):
    samples,meta,features,baseline=load_inputs(root)
    if not do_write:
        labels=Counter(s['class_label'] for s in samples)
        delays=[(s['first_seen_ms']-s['known_ts'])/v1.MIN for s in samples]
        print('PLAN',json.dumps({'cases':len(samples),'labels_for_reporting_only':dict(labels),
             'observations':dict(Counter(s['observation_type'] for s in samples)),
             'median_first_seen_delay_min':round(statistics.median(delays),4),
             'frozen_sample_hash':meta['sample_sha256'],'will_write':False},ensure_ascii=False))
        return None
    rows,parity,deltas=simulate(root,samples,meta,features,baseline)
    summary=summarize(rows,meta,parity,deltas)
    out=root/OUT
    out.mkdir(parents=True,exist_ok=True)
    with (out/'entry_replay_events.csv').open('w',newline='',encoding='utf-8') as fh:
        writer=csv.DictWriter(fh,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    flat=[]
    for r in summary['group_comparisons']:
        flat.append({k:(json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,dict) else v) for k,v in r.items()})
    with (out/'entry_comparison.csv').open('w',newline='',encoding='utf-8') as fh:
        writer=csv.DictWriter(fh,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('ENTRY_LAB_DONE',json.dumps({'events':len(samples),'scenario_rows':len(rows),
          'parity':parity,'output':str(out)},ensure_ascii=False))
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    modes=p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true',help='Read-only checks; no files written')
    modes.add_argument('--run',action='store_true',help='Run replay, compare and export isolated V3 only')
    p.add_argument('--root',type=Path,default=Path('.'))
    args=p.parse_args(argv)
    run(args.root.resolve(),do_write=args.run)


if __name__=='__main__':main()
