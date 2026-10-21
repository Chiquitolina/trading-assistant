#!/usr/bin/env python3
"""Unbalanced, label-blind, retrospective replication of Runner Precursors V1.

Reads frozen V1 sample and completed Scanner/Auto Paths in read-only mode;
never accesses Redis or execution. Reuses audited V1 REST collection/audit/export
with its own SQLite file and output directory. No claim of temporal OOS.

CLI: --plan | --run | --audit | --export | --analyze
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

import runner_precursor_pilot as v1

BASE = Path('reports/reaction_lab')
NAME = 'runner_precursor_validation_v2.sqlite3'
OUT = 'runner_precursor_validation_v2'
VOL = 'asset_prer_1m_realized_vol_60_pct'
RET = 'asset_prer_1m_ret_15_pct'
TRADES = 'asset_prer_1m_trades_5'


def percentile(items, q):
    vals = sorted(float(x) for x in items if x is not None and math.isfinite(float(x)))
    if not vals:
        raise ValueError('empty percentile')
    p = (len(vals)-1)*q
    low = math.floor(p)
    high = math.ceil(p)
    return vals[low]+(p-low)*(vals[high]-vals[low])


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def frozen_pilot(root):
    path = root / BASE / v1.DB_NAME
    with closing(v1.ro(path)) as con:
        return v1.load_samples(con)


def historical_candidates(root, prior, episodes_csv=None):
    candidates, rejected = v1.source_candidates(root, episodes_csv)
    prior_keys = {e['event_key'] for e in prior}
    prior_eps = {e['episode_id'] for e in prior if e['episode_id']}
    by_pair = defaultdict(list)
    for e in prior:
        by_pair[(e['symbol'],e['side'])].append(e['retest_ts'])
    pool=[]
    reasons=Counter(rejected)
    for e in candidates:
        if e['event_key'] in prior_keys:
            reasons['frozen_pilot_keys']+=1
        elif e['episode_id'] and e['episode_id'] in prior_eps:
            reasons['frozen_pilot_episode']+=1
        elif any(abs(e['retest_ts']-ts)<180*v1.MIN
                 for ts in by_pair[(e['symbol'],e['side'])]):
            reasons['near_frozen_pilot_same_symbol_side_180m']+=1
        else:
            pool.append(e)
    return pool,reasons


def choose_blind(pool, count, seed=20261021, max_symbol=5, max_wave=2, sep_minutes=180):
    """Never branch/order by class_label, MFE, MAE, or future hit times."""
    def deterministic_rank(e):
        key=f'{seed}|{e["event_key"]}'.encode()
        return hashlib.sha256(key).digest()
    selected=[]
    sym=Counter(); waves=Counter(); episodes=set(); per_pair=defaultdict(list)
    for e in sorted(pool,key=deterministic_rank):
        if sym[e['symbol']]>=max_symbol or waves[e['wave_minute']]>=max_wave:
            continue
        if e['episode_id'] and e['episode_id'] in episodes:
            continue
        pair=(e['symbol'],e['side'])
        if any(abs(e['retest_ts']-ts)<sep_minutes*v1.MIN for ts in per_pair[pair]):
            continue
        selected.append(e)
        sym[e['symbol']]+=1
        waves[e['wave_minute']]+=1
        per_pair[pair].append(e['retest_ts'])
        if e['episode_id']:
            episodes.add(e['episode_id'])
        if len(selected)==count:break
    if len(selected)<count:
        raise ValueError(f'Could select only {len(selected)}/{count} independent episodes; lower --count')
    return sorted(selected,key=lambda e:(e['retest_ts'],e['event_key']))


def freeze_thresholds(root):
    path=root/BASE/'runner_precursor_pilot'/'features_before_retest.csv'
    rows=read_csv(path)
    if len(rows)!=200 or len(set(x['event_key'] for x in rows))!=200:
        raise ValueError('V1 pre-retest CSV is not the frozen 200-row pilot')
    return {'volatility_pilot_p75':percentile([float(x[VOL]) for x in rows],0.75),
            'pilot_features_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def auc(rows,positive_field='runner',score_field='score'):
    # Pairwise ranking with ties, never treats case-control balance as precision.
    a=sorted(float(x[score_field]) for x in rows if x[positive_field] and x[score_field] is not None)
    b=sorted(float(x[score_field]) for x in rows if not x[positive_field] and x[score_field] is not None)
    if not a or not b:return None
    import bisect
    wins=sum(bisect.bisect_left(b,x)+.5*(bisect.bisect_right(b,x)-bisect.bisect_left(b,x)) for x in a)
    return wins/(len(a)*len(b))


def first_touch(path_rows, side, tp, sl=1.0, bars=180):
    """OHLC barrier first-touch; same-minute TP+SL stays AMBIGUOUS (no assumptions)."""
    if not path_rows or len(path_rows)<bars or side not in ('LONG','SHORT'):
        raise ValueError('invalid future path')
    entry=path_rows[0][1]
    for minute,(_ts,_o,high,low,_c) in enumerate(path_rows[:bars],1):
        if side=='LONG':
            take=high>=entry*(1+tp/100)
            stop=low<=entry*(1-sl/100)
        else:
            take=low<=entry*(1-tp/100)
            stop=high>=entry*(1+sl/100)
        if take and stop:return 'AMBIGUOUS_SAME_1M',minute
        if stop:return 'SL_FIRST',minute
        if take:return 'TP_FIRST',minute
    return 'NO_HIT',None


def decode_outcomes(root, event_keys):
    db=root/BASE/'auto_paths_v1.sqlite3'
    out={}
    with closing(v1.ro(db)) as con:
        for key,entry,retest,count,blob in con.execute('SELECT event_key,entry_ts,retest_ts,count,path_blob FROM events WHERE state=? AND count=360',('COMPLETE',)):
            if key not in event_keys:continue
            rows=v1.decode(blob,entry,retest,count)
            if rows is None:
                raise ValueError(f'future path invalid: {key}')
            out[key]=rows
    if set(out)!=set(event_keys):
        raise ValueError(f'Missing complete future paths: {len(set(event_keys)-set(out))}')
    return out


def analyze(root, db_path, output_dir):
    with closing(v1.ro(db_path)) as con:
        samples=v1.load_samples(con)
        meta=dict(con.execute('SELECT key,value FROM meta'))
    if meta.get('purpose')!='UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION':
        raise ValueError('Not a V2 replication database')
    features={x['event_key']:x for x in read_csv(output_dir/'features_before_retest.csv')}
    outcomes={x['event_key']:x for x in read_csv(output_dir/'outcomes_future.csv')}
    keys={x['event_key'] for x in samples}
    if len(keys)!=len(samples) or keys!=set(features) or keys!=set(outcomes):
        raise ValueError('Cohort/features/outcomes mismatch; cannot analyze')
    paths=decode_outcomes(root,keys)
    vol_p75=float(meta['volatility_pilot_p75'])
    records=[]
    for e in samples:
        key=e['event_key']; f=features[key]; outcome=outcomes[key]
        vol=float(f[VOL]); pre_dir_ret=float(f[RET])  # V1's ret_15_pct is ALREADY side-adjusted
        row={'event_key':key,'symbol':e['symbol'],'side':e['side'],
             'day_utc':v1.day(e['retest_ts']),'class_label':outcome['class_label'],
             'volatility_60_pct':vol,'high_vol_pilot_p75':int(vol>=vol_p75),
             'range_30_pct':float(f['asset_prer_1m_range_30_pct']),
             'trades_5':float(f[TRADES]),'directional_retrace_15_pct':-pre_dir_ret,
             'mfe_180_pct':float(outcome['mfe_180_pct']),
             'mae_180_pct':float(outcome['mae_180_pct'])}
        for tp in (2,3):
            status,minute=first_touch(paths[key],e['side'],tp,sl=1.0)
            row[f'tp{tp}_sl1_state']=status
            row[f'tp{tp}_sl1_minute']=minute
        records.append(row)
    def cohort(items):
        n=len(items);r=sum(x['class_label']=='RUNNER_3' for x in items)
        return {'n':n,'runners':r,'runner_rate':round(r/n,6) if n else None,
                'tp2_sl1_first':dict(Counter(x['tp2_sl1_state'] for x in items)),
                'tp3_sl1_first':dict(Counter(x['tp3_sl1_state'] for x in items))}
    top=[r for r in records if r['high_vol_pilot_p75']];other=[r for r in records if not r['high_vol_pilot_p75']]
    def a_for(name, direction=1):
        return auc([{'runner':r['class_label']=='RUNNER_3','score':direction*r[name]} for r in records])
    metrics={'method':'HISTORICAL_UNBALANCED_REPLICATION_NOT_FUTURE_TEMPORAL_OOS',
             'sample_count':len(records),'frozen_sample_hash':meta.get('sample_sha256'),
             'pilot_volatility_p75_fixed':vol_p75,
             'pilot_features_sha256':meta.get('pilot_features_sha256'),
             'overall':cohort(records),'high_vol':cohort(top),'below_high_vol':cohort(other),
             'auc':{'volatility_60_pct':a_for('volatility_60_pct'),
                    'range_30_pct':a_for('range_30_pct'),
                    'trades_5':a_for('trades_5'),
                    'directional_retrace_15_pct':a_for('directional_retrace_15_pct')},
             'unique_symbols':len({x['symbol'] for x in records}),
             'unique_days':len({x['day_utc'] for x in records}),
             'notes':['Fixed score cutoff derived ONLY from previous frozen 200 pilot',
                      'Population estimates apply to this restricted sampled cohort only',
                      'Same-date historical replication is NOT prospective/out-of-time',
                      '1m both-target hits are ambiguous, not wins',
                      'No fees/spread/slippage/funding or live fill assumption',
                      'First 1m after retest OPEN is a theoretical entry']}
    if metrics['below_high_vol']['runner_rate'] not in (None,0) and metrics['high_vol']['runner_rate'] is not None:
        metrics['runner_rate_lift_high_vs_other']=round(metrics['high_vol']['runner_rate']/metrics['below_high_vol']['runner_rate'],4)
    else:metrics['runner_rate_lift_high_vs_other']=None
    output_dir.mkdir(parents=True,exist_ok=True)
    with (output_dir/'historical_validation_events.csv').open('w',newline='',encoding='utf-8') as fh:
        writer=csv.DictWriter(fh,fieldnames=list(records[0]))
        writer.writeheader();writer.writerows(records)
    (output_dir/'historical_validation_report.json').write_text(json.dumps(metrics,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return metrics


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    modes=p.add_mutually_exclusive_group(required=True)
    for command in ('plan','run','audit','export','analyze'):
        modes.add_argument('--'+command,action='store_true')
    p.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--count',type=int,default=600)
    p.add_argument('--seed',type=int,default=20261022)
    p.add_argument('--episodes-csv',type=Path,default=None)
    p.add_argument('--pause',type=float,default=.45)
    p.add_argument('--max-requests',type=int,default=2000)
    p.add_argument('--max-failures',type=int,default=10)
    p.add_argument('--min-free-gb',type=float,default=1.0)
    args=p.parse_args(argv)
    if args.count<20 or args.max_requests<1:p.error('invalid count / request limit')
    root=args.project.resolve();db=root/BASE/NAME;dest=root/BASE/OUT
    if args.plan or args.run:
        prior=frozen_pilot(root)
        if db.is_file():
            with closing(v1.ro(db)) as con:selected=v1.load_samples(con)
            print('EXISTING_FROZEN_REPLICATION',len(selected))
        else:
            pool,reasons=historical_candidates(root,prior,args.episodes_csv)
            selected=choose_blind(pool,args.count,args.seed)
            print('ELIGIBLE_AFTER_PILOT_EXCLUSIONS',len(pool),'EXCLUSION_REASONS',dict(reasons))
        if args.plan:
            spans=v1.windows(selected,120,96,15)
            count_bars=sum((b-a)//v1.TFS[tf]+1 for (_,tf),pieces in spans.items() for a,b in pieces)
            requests=sum((b-a)//(v1.TFS[tf]*1000)+1 for (_,tf),pieces in spans.items() for a,b in pieces)
            print('BLIND_SAMPLE',len(selected),'DAYS',len({v1.day(x['retest_ts']) for x in selected}),
                  'SYMBOLS',len({x['symbol'] for x in selected}),
                  'LABELS_WITHHELD_UNTIL_FROZEN','YES')
            print('UNIQUE_REQUIRED_BARS',count_bars,'APPROX_REQUESTS',requests,'DB',db)
            print('SAMPLE_HASH',v1.sample_hash(selected),'NO_FILES_OR_NETWORK_MODIFIED')
            return
        thresholds=freeze_thresholds(root)
        if db.is_file():
            with closing(v1.ro(db)) as existing_db:
                existing_meta=dict(existing_db.execute('SELECT key,value FROM meta'))
            previous_hash=existing_meta.get('pilot_features_sha256')
            if previous_hash and previous_hash!=thresholds['pilot_features_sha256']:
                raise ValueError('V1 feature CSV changed since this V2 cohort was frozen')
        runner_args=argparse.Namespace(cases=len(selected),controls=0,
              pre1=120,pre15=96,post_audit=15,seed=args.seed,
              min_free_gb=args.min_free_gb,max_requests=args.max_requests,
              max_failures=args.max_failures,pause=args.pause)
        try:
            result=v1.run(root,db,runner_args,selected=selected)
        finally:
            # Persist methodology even when an API limit pauses a resumable batch.
            if db.exists():
                with closing(v1.connect(db)) as con:
                    with con:
                        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('purpose','UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION'))
                        for k,v in thresholds.items():con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(k,str(v)))
                        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('pilot_sample_hash',v1.sample_hash(prior)))
        print('RESULT',json.dumps(dict(result)))
        return
    if args.audit:
        with closing(v1.ro(db)) as con:
            purpose=dict(con.execute('SELECT key,value FROM meta')).get('purpose')
        if purpose!='UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION':raise ValueError('wrong cohort purpose')
        print(json.dumps(v1.audit(root,db),indent=2,ensure_ascii=False))
    elif args.export:
        with closing(v1.ro(db)) as con:
            purpose=dict(con.execute('SELECT key,value FROM meta')).get('purpose')
        if purpose!='UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION':raise ValueError('wrong cohort purpose')
        summary=v1.export(root,db,dest,True)
        summary['sampled_balanced']=False
        summary['design']='HISTORICAL_UNBALANCED_LABEL_BLIND_SELECTION'
        summary['warning']='Historical same-period replication, NOT prospective/out-of-time. Execution not validated.'
        (dest/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps(summary,indent=2,ensure_ascii=False))
    else:
        result=analyze(root,db,dest)
        print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
