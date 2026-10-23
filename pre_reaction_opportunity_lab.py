#!/usr/bin/env python3
"""PRE-Reaction Opportunity Lab / Phase 1: coverage, causally armed pilot, audit.

READS existing historical monthly Binance Futures 1m pickle caches and scanner SQLite.
WRITES only reports/reaction_lab/pre_reaction_opportunity_lab_v0/pilot_*.
No network, Redis, exchange, dashboard modification, or order execution.

IMPORTANT: 15m bars are derived from complete native 1m cache. This is a
coverage/methodology PILOT, NOT canonical 15m parity or a strategy backtest.
Canonical scanner app.py must be checked in subsequent parity stage.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys

ONE=60_000; FIFTEEN=15*ONE
VERSION='pre-reaction-opportunity-v0.1-pilot'
DEFAULT_CACHE=Path('reports/candidate_long_horizon/candle_cache/1m')
DEFAULT_OUT=Path('reports/reaction_lab/pre_reaction_opportunity_lab_v0')
REQUIRED={'timestamp','open','high','low','close','volume'}


def iso_ms(x):
    dt=datetime.fromisoformat(str(x).replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('Dates must include timezone or Z')
    return int(dt.timestamp()*1000)


def utc(ts): return datetime.fromtimestamp(ts/1000,timezone.utc).isoformat()


def ro_conn(path):
    return sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=20)


def month_list(a,b):
    a=datetime.fromtimestamp(a/1000,timezone.utc);b=datetime.fromtimestamp(b/1000,timezone.utc)
    year,month=a.year,a.month
    while (year,month)<=(b.year,b.month):
        yield f'{year:04d}-{month:02d}'
        month+=1
        if month>12:month=1;year+=1


def source_inventory(root,cache,symbols,start,end):
    c=root/cache
    db=root/'reports/reaction_lab/scanner_events_v1.sqlite3'
    event_counts={}; total=None
    if db.exists():
        with ro_conn(db) as con:
            total=con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            event_counts=dict(con.execute('SELECT symbol,COUNT(*) FROM events GROUP BY symbol'))
    months=list(month_list(start-3*FIFTEEN,end+360*ONE))
    rows=[]
    for s in symbols:
        present=[m for m in months if (c/s/(m+'.pkl.gz')).is_file()]
        rows.append({'symbol':s,'available_months':present,
            'missing_months':[m for m in months if m not in present],
            'scanner_reactions':event_counts.get(s,0)})
    return {'version':VERSION,'cache':str(c),'cache_exists':c.is_dir(),
            'scanner_db_exists':db.exists(),'scanner_events_total':total,
            'time_window':[utc(start),utc(end)],'required_months':months,
            'symbols':rows,'source_type':'monthly_native_1m_pkl_gz'}


def load_native_1m(root,cache,sym,start,end):
    import pandas as pd
    frames=[];files=[]
    for month in month_list(start,end):
        p=root/cache/sym/(month+'.pkl.gz')
        if not p.is_file():continue
        frame=pd.read_pickle(p,compression='gzip')
        if not REQUIRED.issubset(frame.columns):
            raise ValueError(f'Invalid 1m monthly cache schema: {p}: missing {sorted(REQUIRED-set(frame.columns))}')
        frames.append(frame[list(REQUIRED)].copy());files.append(p)
    if not frames:return pd.DataFrame(columns=sorted(REQUIRED)),files
    d=pd.concat(frames,ignore_index=True)
    for col in REQUIRED:
        d[col]=pd.to_numeric(d[col],errors='coerce')
    d=d.dropna(subset=list(REQUIRED))
    d=d.loc[d.timestamp.between(start,end-ONE)].sort_values('timestamp')
    dups=d[d.duplicated('timestamp',keep=False)]
    if not dups.empty:
        for ts,g in dups.groupby('timestamp'):
            if len(g.drop_duplicates())>1:raise RuntimeError(f'Conflicting 1m source bars: {sym} {ts}')
        d=d.drop_duplicates('timestamp')
    if not d.empty:
        if (d.timestamp.astype('int64')%ONE).any():raise RuntimeError('Non-aligned 1m candle')
        a=d[['open','high','low','close','volume']]
        if (a[['open','high','low','close']]<=0).any().any():raise RuntimeError('Nonpositive OHLC')
        if ((d.high<d[['open','close','low']].max(axis=1)) |
            (d.low>d[['open','close','high']].min(axis=1)) |
            (d.volume<0)).any():raise RuntimeError('Invalid 1m OHLCV')
    return d.reset_index(drop=True),files


def contiguous_15m_from_1m(frame):
    """Only aggregate 15m groups containing exactly 15 consecutive valid 1m bars."""
    import pandas as pd
    if frame.empty:return pd.DataFrame(columns=['timestamp','open','high','low','close','volume'])
    d=frame.copy();d['group']=(d.timestamp.astype('int64')//FIFTEEN)*FIFTEEN
    agg=d.groupby('group',sort=True).agg(
        timestamp=('timestamp','min'),open=('open','first'),high=('high','max'),
        low=('low','min'),close=('close','last'),volume=('volume','sum'),
        n=('timestamp','size'),last=('timestamp','max'))
    agg=agg[(agg.n==15)&(agg.timestamp==agg.index)&(agg['last']==agg.index+14*ONE)]
    return agg.reset_index(drop=True)[['timestamp','open','high','low','close','volume']]


def find_swings(frame):
    """Use installed 3x3 detector, which exposes confirming candle OPEN timestamp."""
    from engine.live.research.swing_detector import SwingDetector
    return SwingDetector(3,3,0.0).detect_all(frame.to_dict('records'))


def scan_opportunity(one_rows,point,max_age=360,departure_pct=0.20,tolerance_pct=0.10):
    """One candidate, pure; no future feature can affect ARMED at departure close.

    No gap crossing, and no departure/retest in the same 1m candle. First touch
    is not skipped after indecision/failure. Later events are never used for PRE.
    """
    side='SHORT' if point.side=='HIGH' else 'LONG'
    pivot=point.price
    action=int(point.confirmed_timestamp)+FIFTEEN
    horizon=action+max_age*ONE
    seq=[r for r in one_rows if action<=int(r['timestamp'])<=horizon]
    res={'side':side,'pivot_ts':int(point.pivot_timestamp),
         'confirm_open_ts':int(point.confirmed_timestamp), 'actionable_ts':action,
         'pivot_price':pivot,'departure_ts':None,'departure_known_ts':None,
         'departure_price':None,'departure_close':None,'departure_range_pct':None,
         'relative_volume_30m':None,'first_touch_ts':None,'first_touch_status':None,
         'status':'UNKNOWN_GAP', 'observed_minutes':0,'retest_minutes':None}
    # Empty period is UNKNOWN, not an opportunity.
    if not seq or int(seq[0]['timestamp'])!=action:return res
    # Evaluate causal known PRE fields only from closed bars; no future outcome.
    prior=[];departed=False
    prev_close=None
    for j,r in enumerate(seq):
        ts=int(r['timestamp'])
        if ts!=action+j*ONE:
            res['status']='UNKNOWN_GAP';res['observed_minutes']=j;return res
        h,l,c,o,v=(float(r[k]) for k in ('high','low','close','open','volume'))
        res['observed_minutes']=j+1
        if not departed:
            away=max(0.,(pivot-l)/pivot*100) if side=='SHORT' else max(0.,(h-pivot)/pivot*100)
            if away>=departure_pct:
                departed=True
                res.update(departure_ts=ts,departure_known_ts=ts+ONE,
                    departure_price=l if side=='SHORT' else h,
                    departure_close=c,departure_range_pct=(h-l)/pivot*100,
                    relative_volume_30m=(v/(sum(prior[-30:])/len(prior[-30:]))) if len(prior)>=30 and sum(prior[-30:])>0 else None,
                    status='ARMED_NO_RETEST')
            prior.append(v);prev_close=c;continue
        zone_lo=pivot*(1-tolerance_pct/100)
        zone_hi=pivot*(1+tolerance_pct/100)
        hit=(h>=zone_lo and l<=zone_hi)
        approach=(prev_close is None or (prev_close<=zone_hi if side=='SHORT' else prev_close>=zone_lo))
        if hit and approach:
            candle_loc=(c-l)/(h-l) if h>l else 0.5
            reaction=(c<=pivot and candle_loc<=0.5) if side=='SHORT' else (c>=pivot and candle_loc>=0.5)
            failed=(c>zone_hi) if side=='SHORT' else (c<zone_lo)
            status='REACTION' if reaction else 'TOUCH_FAILED' if failed else 'TOUCH_INDECISIVE'
            res.update(first_touch_ts=ts,first_touch_status=status,status=status,
                       retest_minutes=(ts-action)//ONE)
            return res
        prior.append(v);prev_close=c
    # Can't infer no-retest if the full horizon is missing.
    if int(seq[-1]['timestamp'])!=horizon:
        res['status']='UNKNOWN_GAP'
    elif not departed:
        res['status']='NO_DEPARTURE'
    else:
        res['status']='ARMED_NO_RETEST'
    return res


def evaluate_symbol(root,cache,sym,start,end):
    import pandas as pd
    # Strictly require three hours before beginning for 3x3 swing warmup.
    warm=start-3*60*ONE; end_data=end+360*ONE+ONE
    mins,files=load_native_1m(root,cache,sym,warm,end_data)
    fif=contiguous_15m_from_1m(mins)
    # IMPORTANT: a 3x3 pivot's 7-bar segment must be contiguous at 15m.
    swings=[]
    if len(fif)>=7:
        for p in find_swings(fif):
            if not start<=int(p.confirmed_timestamp)+FIFTEEN<end:continue
            pos=fif.index[fif.timestamp.eq(int(p.pivot_timestamp))]
            confirm=fif.index[fif.timestamp.eq(int(p.confirmed_timestamp))]
            if len(pos)!=1 or len(confirm)!=1:continue
            idx=int(pos[0]); ci=int(confirm[0]);li=idx-3
            if li<0 or ci+1-idx!=4:continue
            segment=fif.iloc[li:ci+1].timestamp.astype('int64').to_numpy()
            if any(segment[j+1]-segment[j]!=FIFTEEN for j in range(len(segment)-1)):continue
            swings.append(p)
    rows=mins.to_dict('records')
    # Index slicing makes each swing <=361 rows; no quadratic full-month rescans.
    lookup={int(r['timestamp']):i for i,r in enumerate(rows)}
    found=[]
    for point in swings:
        action=int(point.confirmed_timestamp)+FIFTEEN
        ix=lookup.get(action)
        if ix is None:
            opp=scan_opportunity([],point)
        else:
            opp=scan_opportunity(rows[ix:ix+361],point)
        opp.update(symbol=sym,opportunity_key=f'{sym}|{opp["side"]}|{int(point.pivot_timestamp)}|{action}',
                   history_origin='AGGREGATED_VERIFIED_CONTIGUOUS_1M',
                   phase='PRE_ONLY_MEMBERSHIP_NO_FUTURE_OUTCOME_USED')
        found.append(opp)
    n_1m=len(mins);n_15m=len(fif)
    return found,{'symbol':sym,'1m_rows':n_1m,'15m_complete_aggregates':n_15m,
                  'confirmed_swings_in_period':len(swings),
                  'statuses':dict(Counter(r['status'] for r in found)),
                  'source_files':[str(x) for x in files]}


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda:fh.read(2**20),b''):h.update(chunk)
    return h.hexdigest()


def write_pilot(root,cache,symbols,start,end,out):
    import pandas as pd
    if out.exists():raise RuntimeError(f'Output directory already exists (immutable run): {out}')
    all_rows=[];sources=[]
    for sym in symbols:
        rows,meta=evaluate_symbol(root,cache,sym,start,end)
        all_rows.extend(rows);sources.append(meta)
        print('OPPORTUNITY_SYMBOL',json.dumps({k:meta[k] for k in ('symbol','1m_rows','15m_complete_aggregates','confirmed_swings_in_period','statuses')},sort_keys=True),flush=True)
    if not any(m['1m_rows'] for m in sources):
        raise RuntimeError('No historical 1m cache for requested pilot. No output written.')
    all_rows.sort(key=lambda v:(v['actionable_ts'],v['opportunity_key']))
    if len({x['opportunity_key'] for x in all_rows})!=len(all_rows):raise RuntimeError('Duplicate opportunity identity')
    out.mkdir(parents=True)
    frame=pd.DataFrame(all_rows)
    if frame.empty:
        frame=pd.DataFrame(columns=['opportunity_key','symbol','side','pivot_ts','confirm_open_ts',
            'actionable_ts','pivot_price','departure_ts','departure_known_ts','status',
            'first_touch_ts','first_touch_status','history_origin'])
    frame.to_csv(out/'opportunities_PRE.csv',index=False)
    source_hashes={str(f):sha(root/f) for m in sources for f in m['source_files'] if (root/f).exists()}  # paths from root relative
    manifest={'version':VERSION,'start_utc':utc(start),'end_exclusive_utc':utc(end),
         'symbols':symbols,'total':len(frame),'statuses':dict(Counter(x['status'] for x in all_rows)),
         'sources':sources,'source_hashes':source_hashes,
         'opportunities_sha256':sha(out/'opportunities_PRE.csv'),
         'limitations':['15m aggregates from 1m; native 15m parity NOT validated',
           'pilot solely tests opportunity census and data continuity',
           'no financial outcomes, models or forecasts',
           'canonical dashboard scanner event parity NOT yet independently validated']}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False))
    return manifest


def audit_pilot(out):
    import pandas as pd
    m=json.loads((out/'manifest.json').read_text())
    f=out/'opportunities_PRE.csv'
    if sha(f)!=m['opportunities_sha256']:raise RuntimeError('Pilot CSV integrity hash mismatch')
    for raw, expected_hash in m.get('source_hashes',{}).items():
        p=Path(raw)
        if not p.is_file() or sha(p)!=expected_hash:
            raise RuntimeError(f'Source changed since pilot: {p}')
    d=pd.read_csv(f)
    if len(d)!=m['total']:raise RuntimeError('Pilot row count mismatch')
    if not d.empty:
        if d.opportunity_key.duplicated().any():raise RuntimeError('Duplicate opportunity keys')
        if not (d.actionable_ts==d.confirm_open_ts+FIFTEEN).all():raise RuntimeError('Action timing invalid')
        if not (d.actionable_ts>d.pivot_ts).all():raise RuntimeError('Premature swing')
        if not (d.departure_ts.dropna().astype('int64')+ONE==d.departure_known_ts.dropna().astype('int64')).all():raise RuntimeError('Departure known timing invalid')
        if not (d.loc[d.departure_ts.notna(),'departure_ts']>=d.loc[d.departure_ts.notna(),'actionable_ts']).all():raise RuntimeError('Pre-departure invalid')
        touching=d[d.first_touch_ts.notna()]
        if not touching.empty and not (touching.first_touch_ts>touching.departure_ts).all():raise RuntimeError('Touch not later than departure')
        if 'UNKNOWN_GAP' in d.status.values and any(d.loc[d.status.eq('UNKNOWN_GAP'),'first_touch_ts'].notna()):raise RuntimeError('Unknown gap has fake touch')
    print('PRE_REACTION_PILOT_AUDIT',json.dumps({'success':True,'total':len(d),'statuses':m['statuses'],'report_dir':str(out)}))


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    grp=ap.add_mutually_exclusive_group(required=True)
    grp.add_argument('--plan',action='store_true');grp.add_argument('--pilot',action='store_true');grp.add_argument('--audit',action='store_true')
    ap.add_argument('--project',type=Path,default=Path.cwd());ap.add_argument('--cache',type=Path,default=DEFAULT_CACHE)
    ap.add_argument('--start',default='2026-10-12T00:00:00Z');ap.add_argument('--end',default='2026-10-13T00:00:00Z')
    ap.add_argument('--symbols',default='HANAUSDT,TAKEUSDT,ETHUSDT')
    ap.add_argument('--output',type=Path,default=None)
    args=ap.parse_args(argv)
    root=args.project.resolve();start=iso_ms(args.start);end=iso_ms(args.end)
    if start>=end:ap.error('start must precede end')
    if (end-start)>31*24*60*ONE:ap.error('Pilot limited to 31 days')
    symbols=tuple(dict.fromkeys(x.strip().upper() for x in args.symbols.split(',') if x.strip()))
    if not symbols or len(symbols)>10:ap.error('--symbols must include 1..10 symbols')
    out=(args.output or root/DEFAULT_OUT/('pilot_'+datetime.fromtimestamp(start/1000,timezone.utc).strftime('%Y%m%d')+'_'+datetime.fromtimestamp(end/1000,timezone.utc).strftime('%Y%m%d')+'_'+'_'.join(symbols))).resolve()
    if args.plan:
        print('PRE_REACTION_PLAN',json.dumps(source_inventory(root,args.cache,symbols,start,end),indent=2))
    elif args.pilot:
        report=write_pilot(root,args.cache,symbols,start,end,out)
        print('PRE_REACTION_PILOT_DONE',json.dumps({'success':True,'total':report['total'],'statuses':report['statuses'],'folder':str(out)},ensure_ascii=False))
    else:audit_pilot(out)

if __name__=='__main__':main()
