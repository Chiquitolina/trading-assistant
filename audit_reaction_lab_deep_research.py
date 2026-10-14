#!/usr/bin/env python3
"""Independent read-only checks of Deep Research V1 stored causal/outcome data."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
import math
from pathlib import Path

from reaction_lab_deep_collector import db_ro, output_db
from reaction_lab_deep_metrics import causal_features, forward_metrics
from reaction_lab_auto_paths import decode_path

CAUSAL_EXACT=('departure_duration_min','departure_speed_pct_per_min','departure_efficiency',
    'departure_directional_bar_share','departure_relative_volume','departure_atr_multiple',
    'retest_rejection_wick_share','retest_close_strength','retest_volume_relative_30m',
    'retest_approach_5m_speed_pct_min','retest_approach_15m_efficiency',
    'retest_level_distance_atr','atr_1m_pct','atr_1m_percentile_200',
    'realized_vol_15m_pct','realized_vol_60m_pct','retest_volatility_shock',
    'deep_market_breadth_1h','deep_market_breadth_4h',
    'deep_breadth_delta_1h','deep_breadth_delta_4h',
    'deep_market_flow_divergence','deep_1m_latest_ts')


def equal(a,b):
    if a is None or b is None:return a is None and b is None
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-9)
    return a==b


def audit(project,limit=20,db=None):
    project=Path(project);out=Path(db) if db else output_db(project)
    if not out.exists():return {'ERROR':'deep DB not created'}
    base=project/'reports/reaction_lab'
    examined=passes=differences=0;late_paths=0
    results=[]
    with closing(db_ro(out)) as side,closing(db_ro(base/'scanner_events_v1.sqlite3')) as scanner,closing(db_ro(base/'causal_context_v1.sqlite3')) as ctx,closing(db_ro(base/'auto_paths_v1.sqlite3')) as paths:
        keys=[x[0] for x in side.execute('SELECT event_key FROM event_features ORDER BY known_ts DESC LIMIT ?', (limit,))]
        for key in keys:
            examined+=1;diff=[]
            raw=side.execute('SELECT known_ts,features_json,evidence_sha256 FROM event_features WHERE event_key=?',(key,)).fetchone()
            known,features,proof=raw;fields=json.loads(features)
            scanner_row=scanner.execute('SELECT known_ts,causal_json FROM events WHERE event_key=?',(key,)).fetchone()
            ctx_row=ctx.execute('SELECT known_ts,context_json,candles_json FROM contexts WHERE event_key=?',(key,)).fetchone()
            if not scanner_row or not ctx_row or scanner_row[0]!=known or ctx_row[0]!=known:
                diff.append('event source/known_ts mismatched')
            else:
                recalculated=causal_features(json.loads(scanner_row[1]),json.loads(ctx_row[1]),json.loads(ctx_row[2]),known)
                for col in CAUSAL_EXACT:
                    if not equal(fields.get(col),recalculated.get(col)):
                        diff.append('causal '+col)
                last=fields.get('deep_1m_latest_ts')
                if last is not None and last+60_000>known:
                    diff.append('future/open 1m bar')
            outcome=side.execute('SELECT source_updated_ms,metrics_json FROM event_outcomes WHERE event_key=?',(key,)).fetchone()
            if outcome:
                path=paths.execute('SELECT side,retest_ts,path_blob,updated_utc_ms FROM events WHERE event_key=?',(key,)).fetchone()
                if path and int(path[3])==int(outcome[0]):
                    entry=json.loads(scanner_row[1]).get('retest_close') if scanner_row else None
                    expected=forward_metrics(decode_path(path[2]),path[0],entry,path[1]);observed=json.loads(outcome[1])
                    if set(expected)!=set(observed):diff.append('outcome field mismatch')
                    else:
                        for col in expected:
                            if not equal(expected[col],observed[col]):diff.append('outcome '+col)
                else:late_paths+=1
            if not diff:passes+=1
            else:differences+=1
            results.append({'event_key':key,'result':'PASS' if not diff else 'DIFF', 'differences':diff[:10]})
    return {'events':examined,'pass':passes,'different':differences,
            'paths_changed_since_snapshot':late_paths,'details':results[:limit]}


def main():
    p=argparse.ArgumentParser();p.add_argument('--project',default='.')
    p.add_argument('--db');p.add_argument('--limit',type=int,default=20)
    a=p.parse_args()
    result=audit(a.project,a.limit,a.db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get('different',0):raise SystemExit(1)

if __name__=='__main__':main()
