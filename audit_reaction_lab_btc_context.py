#!/usr/bin/env python3
"""Read-only replay of frozen BTC correlation/velocity inputs and output values."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import zlib

from reaction_lab_btc_context import TF_MS, readonly, source_fingerprint
from reaction_lab_btc_correlation_reference import BTCCorrelationAnalyzer
from reaction_lab_btc_velocity_reference import BTCVelocityContextService


class ArchivedBuffer:
    def __init__(self, snapshot):
        self.known=int(snapshot['known_ts'])
        self.snapshots=snapshot['price_closes_asof']
    def get_candles(self, symbol, timeframe, as_of_ts=None):
        cutoff=self.known if as_of_ts is None else min(self.known,int(as_of_ts))
        bars=self.snapshots.get(str(symbol).upper(),{}).get(str(timeframe),[])
        return [{'timestamp':int(ts),'close':float(price)} for ts,price in bars
                if int(ts)+TF_MS[timeframe]<=cutoff]


def replay_one(event_key,symbol,side,known_ts,features_json,inputs_zlib,source_hash,
               expected_hash):
    if source_hash!=expected_hash:
        return {'event_key':event_key,'result':'VERSION_MISMATCH','diffs':[],
                'why':'Frozen reference classes differ from installed references'}
    inputs=json.loads(zlib.decompress(inputs_zlib).decode('utf-8'))
    if inputs['known_ts']!=known_ts:
        return {'event_key':event_key,'result':'INVALID_INPUT_TIMESTAMPS','diffs':['known_ts']}
    for sym,tf_dict in inputs['price_closes_asof'].items():
        for tf,bars in tf_dict.items():
            if any(int(ts)+TF_MS[tf]>known_ts for ts,price in bars):
                return {'event_key':event_key,'result':'LOOKAHEAD_IN_SNAPSHOT',
                        'diffs':[sym+':'+tf]}
    data=json.loads(features_json)
    buffer=ArchivedBuffer(inputs)
    calc=BTCCorrelationAnalyzer(buffer).analyze(symbol)
    svc=BTCVelocityContextService()
    vel=svc.evaluate(buffer,as_of_ts=known_ts)
    relation=svc.evaluate_trade_relationship(vel,side,
        correlation=calc.get('btc_corr_5m_4h'),beta=calc.get('btc_beta_5m_4h'))
    calc.update({
        'btc_velocity_15m':vel.velocity_15m,'btc_velocity_1h':vel.velocity_1h,
        'btc_signed_move_15m_pct':vel.signed_move_15m_pct,
        'btc_signed_move_1h_pct':vel.signed_move_1h_pct,
        'btc_direction_15m':vel.direction_15m,'btc_direction_1h':vel.direction_1h,
        'btc_direction_alignment':vel.direction_alignment,
        'btc_velocity_state':vel.state,'btc_velocity_reason':vel.reason,
        **relation,
    })
    diffs=[]
    for field,value in calc.items():
        frozen=data.get(field)
        if isinstance(value,(int,float)) and not isinstance(value,bool) and value is not None:
            same=frozen is not None and abs(float(frozen)-value) < 1e-8
        else:
            same=frozen==value
        if not same:diffs.append({'field':field,'frozen':frozen,'original_replay':value})
    return {'event_key':event_key,'result':'PASS' if not diffs else 'DIFF',
            'diffs':diffs,'compared':len(calc)}


def audit(db,project,limit):
    if not db.is_file():raise FileNotFoundError(db)
    fingerprint=source_fingerprint(project)
    with closing(readonly(db)) as con:
        rows=con.execute('SELECT event_key,symbol,side,known_ts,features_json,inputs_zlib,source_hash '
            'FROM btc_events ORDER BY known_ts DESC LIMIT ?',(int(limit),)).fetchall()
    results=[]
    for row in rows:
        try:results.append(replay_one(*row,fingerprint))
        except (ValueError,TypeError,KeyError,zlib.error) as exc:
            results.append({'event_key':row[0],'result':'ERROR','why':f'{type(exc).__name__}: {exc}'})
    from collections import Counter
    states=Counter(r['result'] for r in results)
    summary={'events':len(results),'states':dict(states),
             'comparisons':sum(r.get('compared',0) for r in results),
             'differences':sum(len(r.get('diffs',[])) for r in results)}
    return summary,results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--db',type=Path)
    p.add_argument('--limit',type=int,default=10)
    a=p.parse_args();root=a.project.resolve()
    db=(a.db or root/'reports/reaction_lab/btc_context_v1.sqlite3').resolve()
    summary,rows=audit(db,root,a.limit)
    for r in rows:print(json.dumps(r,ensure_ascii=False))
    print('RESUMEN',json.dumps(summary,ensure_ascii=False))
    if not rows:print('NO_EVENTS: wait for prospective REACTIONs');return 2
    return 0 if summary['states'].get('PASS',0)==len(rows) else 2

if __name__=='__main__':raise SystemExit(main())
