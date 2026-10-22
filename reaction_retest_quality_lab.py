#!/usr/bin/env python3
"""REACTION Retest Quality Lab: read-only causal features + first-touch outcomes.

Reads frozen Full Context V1 and audited First-Touch V1; outputs only isolated
CSV/JSON for Streamlit. No Redis, Binance, exchange orders or live mutations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

BASE = Path('reports/reaction_lab')
OUTPUT = 'reaction_retest_quality_lab_v1'
VERSION = 'retest-quality-lab-v1.0'
MIN = 60_000
FEATURES = (
 'pre_range5_pct','pre_range15_pct','pre_range30_pct','pre_realized_vol60_pct',
 'pre_signed_return5_pct','pre_signed_return15_pct','pre_signed_return60_pct',
 'pre_quote_relative5_to_previous30','pre_trades_relative5_to_previous30',
 'pre_taker_side_imbalance5','pre_close_strength_side_last1m',
 'pre_wick_rejection_share_last1m','pre_efficiency15',
 'pre_quote_relative1_to_previous30','pre_trades_relative1_to_previous30',
 'pre_taker_side_imbalance1','pre_taker_side_imbalance15',
 'pre_rejection_mean5','pre_close_strength_mean5',
 'pre_signed_body_mean5_pct','pre_range_contraction5_over30',
 'pre_price_location_15m_side',
 'pre15m_range1h_pct','pre15m_range4h_pct','pre15m_atr14_pct',
 'pre15m_side_return1h_pct','pre15m_side_return4h_pct',
 'pre15m_vs_btc4h_pct',
 'btc_pre_signed_return5_pct','btc_pre_signed_return15_pct','btc_pre_signed_return60_pct',
 'pre_strength_vs_btc15_pct','pre_strength_vs_btc60_pct',
 'known_retest_range_pct','known_retest_close_strength_side',
 'known_retest_rejection_wick_share','known_retest_signed_body_pct',
 'known_retest_relative_quote30','known_retest_taker_side_imbalance',
 'known_swing_level','known_retest_distance_to_swing_pct',
 'known_swing_age_min','known_departure_pct','known_retest_distance_pct',
 'known_relative_volume_scanner','known_swing_prominence_pct',
 'known_pivot_to_confirmation_pct','known_penetration_pct',
 'known_pivot_to_retest_min','known_triggering_swing_count',
)
FIELDS = ('event_key','symbol','side','date_utc','retest_ts','known_ts','first_seen_ms',
 'observation_type','first_seen_delay_sec','path_quality','path_status',
 'ret_test_ohlc_source','quality_feature_status',*FEATURES)
BAR_FIELDS = 'open_ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base,taker_buy_quote'


def ro(path):
    path=Path(path)
    if not path.exists():raise FileNotFoundError(path)
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=25)
    con.execute('PRAGMA query_only=ON')
    return con


def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def validate_sources(root):
    p=root/BASE/'reaction_full_context_v1'/'audit.json'
    f=root/BASE/'reaction_first_touch_lab_v1'/'summary.json'
    if not p.exists() or not f.exists():
        raise RuntimeError('Faltan Full Context audit.json o First-Touch summary.json; ejecutar auditorías y --run')
    a=json.loads(p.read_text(encoding='utf-8'))
    b=json.loads(f.read_text(encoding='utf-8'))
    if (not a.get('success') or a.get('conflicts')!=0 or
        a.get('incomplete_windows_total')!=0 or
        a.get('aggregate',{}).get('unverified_mismatches_total')!=0 or
        a.get('events')!=b.get('source_events') or
        a.get('frozen_cohort_sha256')!=b.get('frozen_cohort_sha256')):
        raise RuntimeError('Auditoría upstream inválida o cohortes Full Context/First-Touch diferentes')
    if b.get('scenario_rows')!=64630 and b.get('scenario_rows') != b.get('path_quality',{}).get('COMPLETE_VALID',0)*10:
        raise RuntimeError('Conteo First Touch inconsistente')
    for name,sha in b.get('file_sha256',{}).items():
        path=root/BASE/'reaction_first_touch_lab_v1'/name
        if not path.exists() or digest(path)!=sha:
            raise RuntimeError('Checksum First Touch inválido: '+name)
    return a,b


def plan(root):
    a,b=validate_sources(root)
    return {'version':VERSION,'events':a['events'],'cohort_sha256':a['frozen_cohort_sha256'],
     'context_windows':a['events']*12,'first_touch_scenarios':b['scenario_rows'],
     'complete_path_events':b['path_quality'].get('COMPLETE_VALID'),
     'sources_read_only':True,'writes_or_network_calls':False,
     'output':str(root/BASE/OUTPUT)}


def rows_for(con,symbol,tf,start,end):
    return con.execute('SELECT '+BAR_FIELDS+' FROM candles WHERE symbol=? AND timeframe=? AND open_ts>=? AND open_ts<? ORDER BY open_ts',
                       (symbol,tf,int(start),int(end))).fetchall()


def aligned(rows,end,n,step=MIN):
    return len(rows)==n and all(int(row[0])==int(end)-(n-i)*step for i,row in enumerate(rows))


def div(a,b):
    return a/b if b else None


def percent_return(x,y):
    return (y/x-1)*100 if x>0 else None


def calc_features(side,asset,btc,retest,causal,asset15=None,btc15=None):
    """PRE uses only bars open_ts < retest_ts; KNOWN uses retest candle after closure."""
    if side not in ('LONG','SHORT'):raise ValueError('Unknown side')
    sign=1 if side=='LONG' else -1
    out={field:None for field in FEATURES}
    if len(asset)<180 or len(btc)<180:return out,'MISSING_PRE_CANDLES'
    def rnge(s):
        data=asset[-s:]
        return 100*(max(x[2] for x in data)-min(x[3] for x in data))/data[-1][4]
    def signed_ret(candles,n):
        return sign*percent_return(candles[-n-1][4],candles[-1][4])
    def src_pct(candles,n):
        return percent_return(candles[-n-1][4],candles[-1][4])
    def strength(bar):
        _,op,hi,lo,cl,*_=bar
        return (cl-lo)/(hi-lo) if side=='LONG' and hi>lo else (
            (hi-cl)/(hi-lo) if hi>lo else .5)
    def rejection(bar):
        _,op,hi,lo,cl,*_=bar
        ran=hi-lo
        if ran<=0:return 0.0
        return (min(op,cl)-lo)/ran if side=='LONG' else (hi-max(op,cl))/ran
    for n in (5,15,30):out[f'pre_range{n}_pct']=rnge(n)
    returns=[math.log(b[4]/a[4]) for a,b in zip(asset[-61:-1],asset[-60:])]
    out['pre_realized_vol60_pct']=100*math.sqrt(sum(x*x for x in returns))
    for n in (5,15,60):out[f'pre_signed_return{n}_pct']=signed_ret(asset,n)
    for n in (5,15,60):out[f'btc_pre_signed_return{n}_pct']=sign*src_pct(btc,n)
    for n in (15,60):out[f'pre_strength_vs_btc{n}_pct']=sign*(src_pct(asset,n)-src_pct(btc,n))
    last5=asset[-5:];prev30=asset[-35:-5]
    q5=sum(x[6] for x in last5);q30=sum(x[6] for x in prev30)
    t5=sum(x[7] for x in last5);t30=sum(x[7] for x in prev30)
    out['pre_quote_relative5_to_previous30']=div(q5/5,q30/30)
    out['pre_trades_relative5_to_previous30']=div(t5/5,t30/30)
    out['pre_taker_side_imbalance5']=sign*(2*div(sum(x[9] for x in last5),q5)-1) if q5 else None
    out['pre_close_strength_side_last1m']=strength(asset[-1])
    out['pre_wick_rejection_share_last1m']=rejection(asset[-1])
    closes=[x[4] for x in asset[-16:]]
    path=sum(abs(math.log(b/a)) for a,b in zip(closes[:-1],closes[1:]))
    out['pre_efficiency15']=div(abs(math.log(closes[-1]/closes[0])),path)
    # All following features are computed from fully CLOSED candles preceding
    # the retest, never the retest candle or a subsequent path candle.
    last30=asset[-35:-5]
    q30_per_bar=div(sum(b[6] for b in last30),30)
    tr30_per_bar=div(sum(b[7] for b in last30),30)
    out['pre_quote_relative1_to_previous30']=div(asset[-1][6],q30_per_bar)
    out['pre_trades_relative1_to_previous30']=div(asset[-1][7],tr30_per_bar)
    out['pre_taker_side_imbalance1']=sign*(2*div(asset[-1][9],asset[-1][6])-1) if asset[-1][6] else None
    last15=asset[-15:]
    q15=sum(b[6] for b in last15)
    out['pre_taker_side_imbalance15']=sign*(2*div(sum(b[9] for b in last15),q15)-1) if q15 else None
    out['pre_rejection_mean5']=sum(rejection(b) for b in last5)/5
    out['pre_close_strength_mean5']=sum(strength(b) for b in last5)/5
    out['pre_signed_body_mean5_pct']=sum(sign*100*(b[4]/b[1]-1) for b in last5)/5
    out['pre_range_contraction5_over30']=div(rnge(5),rnge(30))
    top=max(b[2] for b in last15);bottom=min(b[3] for b in last15)
    if top>bottom:
        out['pre_price_location_15m_side']=(asset[-1][4]-bottom)/(top-bottom) if sign==1 else (top-asset[-1][4])/(top-bottom)
    if asset15 and btc15 and len(asset15)==96 and len(btc15)==96:
        def r15(n):
            return 100*(max(x[2] for x in asset15[-n:])-min(x[3] for x in asset15[-n:]))/asset15[-1][4]
        out['pre15m_range1h_pct']=r15(4)
        out['pre15m_range4h_pct']=r15(16)
        tr=[]
        for prev,cur in zip(asset15[-15:-1],asset15[-14:]):
            tr.append(max(cur[2]-cur[3],abs(cur[2]-prev[4]),abs(cur[3]-prev[4])))
        out['pre15m_atr14_pct']=100*sum(tr)/14/asset15[-1][4]
        for n,label in [(4,'1h'),(16,'4h')]:
            out[f'pre15m_side_return{label}_pct']=sign*percent_return(asset15[-n-1][4],asset15[-1][4])
        asset4=percent_return(asset15[-17][4],asset15[-1][4])
        btc4=percent_return(btc15[-17][4],btc15[-1][4])
        out['pre15m_vs_btc4h_pct']=sign*(asset4-btc4)
    if retest is not None:
        _,op,hi,lo,cl,vol,q,trades,takerbase,takerq=retest
        out['known_retest_range_pct']=100*(hi-lo)/op
        out['known_retest_close_strength_side']=strength(retest)
        out['known_retest_rejection_wick_share']=rejection(retest)
        out['known_retest_signed_body_pct']=100*sign*(cl/op-1)
        out['known_retest_relative_quote30']=div(q,q30/30)
        out['known_retest_taker_side_imbalance']=sign*(2*takerq/q-1) if q else None
    if not isinstance(causal,dict):causal={}
    for k,src in (('known_swing_age_min','retest_age_min'),
                  ('known_departure_pct','max_departure_pct'),
                  ('known_retest_distance_pct','retest_distance_pct'),
                  ('known_relative_volume_scanner','relative_volume'),
                  ('known_swing_prominence_pct','prominence_pct'),
                  ('known_pivot_to_confirmation_pct','pivot_to_confirmation_pct'),
                  ('known_penetration_pct','penetration_pct'),
                  ('known_pivot_to_retest_min','pivot_to_retest_min'),
                  ('known_triggering_swing_count','triggering_swing_count')):
        v=causal.get(src)
        if v is not None:
            try:
                n=float(v)
                out[k]=n if math.isfinite(n) else None
            except (TypeError,ValueError):pass
    # Price of the confirmed swing when present in persisted canonical metadata.
    for key in ('swing_price','pivot_price','level_price'):
        if key in causal and causal[key] is not None:
            try:
                out['known_swing_level']=float(causal[key]);break
            except (TypeError,ValueError):pass
    if out['known_swing_level'] and retest:
        out['known_retest_distance_to_swing_pct']=100*abs(retest[4]/out['known_swing_level']-1)
    return out,'COMPLETE'


def build_row(con,e,path_quality=None,path_status=None):
    key,sym,side,r,k,seen,obs,causal=e
    pre=rows_for(con,sym,'1m',r-180*MIN,r)
    btc=rows_for(con,'BTCUSDT','1m',r-180*MIN,r)
    retest=rows_for(con,sym,'1m',r,r+MIN)
    cutoff15=(r//900_000)*900_000
    pre15=rows_for(con,sym,'15m',cutoff15-96*900_000,cutoff15)
    btc15=rows_for(con,'BTCUSDT','15m',cutoff15-96*900_000,cutoff15)
    if (not aligned(pre,r,180) or not aligned(btc,r,180) or
        not aligned(pre15,cutoff15,96,900_000) or
        not aligned(btc15,cutoff15,96,900_000)):
        f={field:None for field in FEATURES};quality='MISSING_PRE_CANDLES'
    else:
        rt=retest[0] if aligned(retest,r+MIN,1) else None
        try:c=json.loads(causal)
        except (TypeError,ValueError,json.JSONDecodeError):c={}
        f,quality=calc_features(side,pre,btc,rt,c,pre15,btc15)
    row={'event_key':key,'symbol':sym,'side':side,'date_utc':datetime.fromtimestamp(r/1000,timezone.utc).date().isoformat(),
      'retest_ts':r,'known_ts':k,'first_seen_ms':seen,'observation_type':obs,
      'first_seen_delay_sec':round((seen-k)/1000,3),
      'path_quality':path_quality,'path_status':path_status,
      'ret_test_ohlc_source':'CLOSED_1M_AT_KNOWN' if len(retest)==1 else 'MISSING',
      'quality_feature_status':quality}
    row.update(f)
    return row


def atomic_csv(path,fields,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.rql_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as h:
            w=csv.DictWriter(h,fieldnames=fields,extrasaction='ignore')
            w.writeheader();w.writerows(data);h.flush();os.fsync(h.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def build(root):
    a,b=validate_sources(root)
    csvpath=root/BASE/'reaction_first_touch_lab_v1'/'events_coverage_features.csv'
    with csvpath.open(encoding='utf-8',newline='') as h:
        existing={x['event_key']:x for x in csv.DictReader(h)}
    result=[];counter=Counter()
    with closing(ro(root/BASE/'reaction_full_context_v1.sqlite3')) as con:
        meta=dict(con.execute('SELECT key,value FROM meta'))
        if meta.get('cohort_sha256')!=a['frozen_cohort_sha256'] or int(meta.get('event_count',0))!=a['events']:
            raise RuntimeError('SHA/count from frozen full context SQLite inconsistent')
        # The index covers all 7631 events; no outcome-based cohort selection.
        for e in con.execute('''SELECT event_key,symbol,side,retest_ts,known_ts,first_seen_ms,
                observation_type,causal_json FROM events ORDER BY retest_ts,event_key'''):
            stat=existing.get(e[0],{})
            row=build_row(con,e,stat.get('path_quality'),stat.get('path_status'))
            result.append(row);counter[row['quality_feature_status']]+=1
    if len(result)!=a['events'] or len(existing)!=a['events'] or len({r['event_key'] for r in result})!=len(result):
        raise RuntimeError('All frozen events must be represented exactly once')
    out=root/BASE/OUTPUT;out.mkdir(parents=True,exist_ok=True)
    features_file=out/'retest_quality_features.csv'
    atomic_csv(features_file,FIELDS,result)
    meta={'version':VERSION,'events':len(result),'frozen_cohort_sha256':a['frozen_cohort_sha256'],
      'feature_quality':dict(counter),'fields':list(FEATURES),
      'retest_features_available_at':'KNOWN_TS_NOT_PRE_RETEST',
      'outcome_source':'reaction_first_touch_lab_v1/first_touch_scenarios.csv',
      'all_7631_events_preserved':True,'input_first_touch_sha256':b['file_sha256'],
      'features_sha256':digest(features_file),'source_dbs_read_only':True,
      'notes':['PRE fields use closed candles strictly before retest',
      'KNOWN fields can use the 1m retest candle only after its close',
      'POST chart uses only archived 360x1m Auto Paths; there is no archived future 15m candle guarantee',
      'LIVE_OBSERVED and RECOVERED_WINDOW never conflated for observed execution',
      'No causal or trading performance claim: descriptive historical analysis']}
    (out/'summary.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return meta


def audit(root):
    a,_=validate_sources(root)
    folder=root/BASE/OUTPUT
    meta=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    path=folder/'retest_quality_features.csv'
    with path.open(newline='',encoding='utf-8') as f:rows=list(csv.DictReader(f))
    ok=(meta.get('features_sha256')==digest(path) and meta.get('frozen_cohort_sha256')==a['frozen_cohort_sha256']
       and meta.get('events')==a['events']==len(rows) and len({r['event_key'] for r in rows})==len(rows)
       and meta.get('feature_quality',{}).get('COMPLETE')==a['events'])
    return {'success':ok,'events':len(rows),'expected':a['events'],
            'feature_quality':meta.get('feature_quality'),'frozen_sha256_matches':meta.get('frozen_cohort_sha256')==a['frozen_cohort_sha256'],
            'features_sha256_matches':meta.get('features_sha256')==digest(path)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    for flag in ('plan','build','audit'):group.add_argument('--'+flag,action='store_true')
    parser.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    args=parser.parse_args(argv);root=args.project.resolve()
    if args.plan:print('QUALITY_PLAN',json.dumps(plan(root),ensure_ascii=False))
    elif args.build:print('QUALITY_BUILD_DONE',json.dumps(build(root),ensure_ascii=False))
    else:print('QUALITY_AUDIT',json.dumps(audit(root),ensure_ascii=False))

if __name__=='__main__':main()
