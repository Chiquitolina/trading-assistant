#!/usr/bin/env python3
"""FAILED Reclaim Audit V1: strictly offline, causal and read-only source audit.

Cohort: ALL original TOUCH_FAILED first retests, not only signalled events.
Source: the same verified 1m path selection used by Runner Discovery ALL.
Reference: failure classification known at retest candle CLOSE (known_ts).
Signals: only CLOSED post-fail candles; entry at the NEXT 1m OPEN.
Outcome: independent TP +2/+3/+5 vs SL -1%, 180m AFTER each entry.
TP+SL same minute is AMBIGUOUS and conservatively charged as SL in net.
All modes use a common complete max_wait+hold coverage denominator.
No Binance/Redis/PM2/orders; source SQLite opened strictly read-only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from reaction_lab_runner_discovery_all_touches_v1 import (
    MINUTE, as_positive, iter_events, recovery_index, ro_connect,
    verify_historical_overlay,
)
from reaction_lab_failed_entry_timing_v1 import (
    find_entry, execute, post_entry_excursions,
)

SCHEMA = 'FAILED_RECLAIM_AUDIT_V1'
OUTPUT = Path('reports/reaction_lab/failed_reclaim_audit_v1')
MODES = (
    'IMMEDIATE_ORIGINAL',
    'IMMEDIATE_INVERSE',
    'BAND_REENTRY',
    'ORIGINAL_RECLAIM',
    'ORIGINAL_RECLAIM_2_CLOSES',
    'ORIGINAL_BREAK_FAIL_EXTREME',
    'INVERSE_2_CLOSES',
    'INVERSE_BREAK_FAIL_EXTREME',
    'INVERSE_RETEST_REJECTION',
)
TARGETS = (2, 3, 5)
EVENT_FIELDS = (
    'touch_key','date_utc','symbol','side','first_touch_status','observation_type',
    'retest_ts','known_ts','first_seen_ms','recording_lag_min',
    'entry_ts_at_retest','path_source','path_quality','path_count',
    'metadata_state','swing_price','retest_close','fail_depth_close_pct',
    'fail_low','fail_high','eligible_common_180m',
)
OUTCOME_FIELDS = (
    'touch_key','date_utc','symbol','side','observation_type','retest_ts',
    'path_source','path_count','metadata_state','mode','direction','target_pct',
    'hold_min','max_wait_min','cohort_eligible','signal_state',
    'signal_close_ts','signal_min_after_fail','entry_ts','entry_min_after_fail',
    'entry_open','outcome','exit_min_after_entry','exit_ts',
    'mfe_hold_pct','mae_hold_pct','gross_pct','net_pct',
)
SUMMARY_FIELDS = (
    'group_type','group','mode','target_pct','cohort_total','eligible_common',
    'coverage_pct','signal_n','signal_pct','no_signal_n','invalid_metadata_n',
    'censored_n','tp_first_n','sl_first_n','sl_gap_n','tp_gap_n',
    'ambiguous_n','time_exit_n','tp_rate_eligible_pct','sl_rate_eligible_pct',
    'tp_rate_signalled_pct','gross_sum_pct_points','net_sum_pct_points',
    'net_mean_per_eligible_pct','net_mean_per_signal_pct',
)


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def same_price(a,b):
    a,b=num(a),num(b)
    return a is not None and b is not None and math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-12)


def date_utc(ms):
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).date().isoformat()


def penetration(close, level, side):
    return 100*((level-close)/level if side=='LONG' else (close-level)/level)


def in_band(close, level, side, tol_pct=0.10):
    """Return to or beyond the FAIL-SIDE band edge, by close (not a wick)."""
    return close >= level*(1-tol_pct/100) if side=='LONG' else close <= level*(1+tol_pct/100)


def find_band_entry(bars, level, side, max_wait, tol_pct=0.10):
    # bars[0] is 1st post-fail candle, which must CLOSE before bars[1] OPEN.
    if not level or side not in ('LONG','SHORT'):
        return None
    for j in range(min(max_wait,len(bars)-1)):
        if in_band(bars[j][4],level,side,tol_pct):
            return j+1,side
    return None


def metadata_row(raw):
    """Canonical journal metadata, preserving observation-time vs known-time."""
    try:
        meta=json.loads(raw['causal_json'])
    except (ValueError,TypeError):
        meta=None
    return dict(raw,meta=meta)


def read_journal(path):
    """All failed journal rows indexed by unique touch key; no source changes."""
    with closing(ro_connect(path)) as db:
        cols={r[1] for r in db.execute('PRAGMA table_info(first_touches)')}
        required={'touch_key','symbol','side','status','retest_ts','known_ts',
                  'first_seen_ms','observation_type','causal_json'}
        if not required.issubset(cols):
            raise ValueError('First-touch journal columns missing: '+str(sorted(required-cols)))
        cur=db.execute('SELECT touch_key,symbol,side,status,retest_ts,known_ts,first_seen_ms,'
                       'observation_type,causal_json FROM first_touches '
                       "WHERE status='TOUCH_FAILED' ORDER BY retest_ts,touch_key")
        result={}
        for record in cur:
            raw=dict(zip(('touch_key','symbol','side','status','retest_ts','known_ts',
                          'first_seen_ms','observation_type','causal_json'),record))
            if raw['touch_key'] in result:
                raise ValueError('Duplicate journal key '+raw['touch_key'])
            result[raw['touch_key']]=metadata_row(raw)
        return result


def read_sample_journal(path):
    rows={}
    with Path(path).open(newline='',encoding='utf-8') as f:
        for record in csv.DictReader(f):
            if record.get('status')!='TOUCH_FAILED':
                continue
            key=record['touch_key']
            if key in rows:raise ValueError('Duplicate sample journal key '+key)
            rows[key]=metadata_row(record)
    return rows


def checked_metadata(event,journal):
    """Validate the identity and fail classification using only retest-close data."""
    if not journal:
        return 'MISSING_JOURNAL',None,None
    try:
        meta=journal['meta']
        if not isinstance(meta,dict):
            return 'BAD_CAUSAL_JSON',None,None
        key=event['touch_key']; side=event['side']; retest=int(event['retest_ts'])
        if (journal['touch_key']!=key or journal['status']!='TOUCH_FAILED' or
            journal['symbol']!=event['symbol'] or journal['side']!=side or
            int(journal['retest_ts'])!=retest or int(journal['known_ts'])!=retest+MINUTE or
            meta.get('touch_key')!=key or meta.get('status')!='TOUCH_FAILED' or
            meta.get('signal')!=side or int(meta.get('known_timestamp'))!=retest+MINUTE or
            int(meta.get('retest_timestamp',retest))!=retest or
            str(meta.get('symbol',event['symbol'])).upper()!=event['symbol']):
            return 'IDENTITY_MISMATCH',None,None
        level=as_positive(meta.get('swing_price'))
        close=as_positive(meta.get('retest_close'))
        high=as_positive(meta.get('retest_high'))
        low=as_positive(meta.get('retest_low'))
        op=as_positive(meta.get('retest_open'))
        if (any(x is None for x in (level,close,high,low)) or
            high<max(low,close) or low>min(high,close) or
            (op is not None and (high<op or low>op))):
            return 'BAD_RETEST_OHLC',None,None
        if not same_price(close,event['retest_close']):
            return 'RETEST_CLOSE_CONFLICT',None,None
        depth=penetration(close,level,side)
        if depth < .10-1e-7:
            return 'NOT_FAILED_BY_0_1_PCT',level,depth
        return 'VALID_FAILED',level,depth
    except (TypeError,ValueError,KeyError,OverflowError):
        return 'BAD_METADATA',None,None


def sample_paths(journal, recovery_root):
    """Pilot-only source: strictly SHA256-verified V11 records, not active SQLite.

    The sample does NOT validate parity with an online prefix (none is shipped);
    the full-VPS mode does parity through the Runner Discovery reader.
    """
    found,_=recovery_index(recovery_root)
    handles={}
    try:
        for key,meta in sorted(journal.items(),key=lambda x:(int(x[1]['retest_ts']),x[0])):
            row={'touch_key':key,'symbol':meta['symbol'],'side':meta['side'],
                 'first_touch_status':meta['status'], 'retest_ts':int(meta['retest_ts']),
                 'entry_ts':int(meta['retest_ts'])+MINUTE,
                 'retest_close':json.loads(meta['causal_json']).get('retest_close'),
                 'date_utc':date_utc(meta['retest_ts']),
                 'first_seen_ms':int(meta['first_seen_ms']),
                 'path_source':'NO_PATH','path_quality':'NO_CONTIGUOUS_PATH',
                 'path_count':0}
            bars=[]
            rec=found.get(key)
            if rec:
                db_path,symbol,side,status,retest,entry=rec
                if (symbol,side,status,retest,entry)!=(row['symbol'],row['side'],'TOUCH_FAILED',
                                                      row['retest_ts'],row['entry_ts']):
                    raise ValueError('Historical sample identity mismatch: '+key)
                db=handles.get(db_path)
                if db is None:
                    db=ro_connect(db_path);handles[db_path]=db
                data=db.execute('SELECT path_blob FROM recovered_paths WHERE touch_key=?',(key,)).fetchone()
                if data is None:raise ValueError('Missing historical sample record: '+key)
                bars=verify_historical_overlay([],data[0],entry)
                row.update(path_source='HISTORICAL_BINANCE_V11',path_quality='CONTIGUOUS_VALID',
                           path_count=len(bars))
            yield row,bars
    finally:
        for db in handles.values():db.close()


def event_base(row,bars,journal,max_wait,hold):
    status,level,depth=checked_metadata(row,journal)
    retest=int(row['retest_ts'])
    obs=journal['observation_type'] if journal else 'MISSING'
    seen=int(journal['first_seen_ms']) if journal and journal.get('first_seen_ms') else None
    eligible=(row.get('path_quality')=='CONTIGUOUS_VALID' and len(bars)>=max_wait+hold)
    record={'touch_key':row['touch_key'],'date_utc':row.get('date_utc') or date_utc(retest),
      'symbol':row['symbol'],'side':row['side'],'first_touch_status':'TOUCH_FAILED',
      'observation_type':obs,'retest_ts':retest,'known_ts':retest+MINUTE,
      'first_seen_ms':seen,
      'recording_lag_min':round((seen-retest-MINUTE)/MINUTE,2) if seen else None,
      'entry_ts_at_retest':retest+MINUTE,
      'path_source':row.get('path_source'),'path_quality':row.get('path_quality'),
      'path_count':len(bars),'metadata_state':status,
      'swing_price':level,'retest_close':row.get('retest_close'),
      'fail_depth_close_pct':round(depth,7) if depth is not None else None,
      'fail_low':num(journal['meta'].get('retest_low')) if journal and isinstance(journal['meta'],dict) else None,
      'fail_high':num(journal['meta'].get('retest_high')) if journal and isinstance(journal['meta'],dict) else None,
      'eligible_common_180m':eligible}
    return record


def find_mode(mode,bars,base,meta,max_wait):
    if mode=='BAND_REENTRY':
        return find_band_entry(bars,base['swing_price'],base['side'],max_wait)
    return find_entry(mode,bars,{'side':base['side'],'meta':meta},max_wait)


def calculate_mode(base,bars,meta,mode,hold,max_wait,target,cost):
    result={k:None for k in OUTCOME_FIELDS}
    result.update({k:base.get(k) for k in ('touch_key','date_utc','symbol','side','observation_type',
                'retest_ts','path_source','path_count','metadata_state')})
    result.update(mode=mode,target_pct=target,hold_min=hold,max_wait_min=max_wait,
                  cohort_eligible=bool(base['eligible_common_180m']))
    if not base['eligible_common_180m']:
        result.update(signal_state='CENSORED_COMMON_HORIZON',outcome='CENSORED')
        return result
    if base['metadata_state']!='VALID_FAILED':
        result.update(signal_state='INVALID_METADATA',outcome='INVALID_METADATA')
        return result
    signal=find_mode(mode,bars,base,meta,max_wait)
    if signal is None:
        result.update(signal_state='NO_SIGNAL',outcome='NO_TRADE')
        return result
    idx, direction=signal
    if idx<0 or idx>max_wait or idx+hold>len(bars):
        raise ValueError('Signal outside fixed common horizon: '+base['touch_key'])
    if mode not in ('IMMEDIATE_ORIGINAL','IMMEDIATE_INVERSE') and idx<1:
        raise ValueError('Non-causal signal without closed post-fail bar')
    entry_ts=int(bars[idx][0]); expected=int(base['known_ts'])+idx*MINUTE
    if entry_ts!=expected:
        raise ValueError('Signal timestamp not contiguous or causal: '+base['touch_key'])
    state,exit_min,gross=execute(bars,idx,direction,target,1.0,hold)
    mfe,mae=post_entry_excursions(bars,idx,direction,hold)
    result.update(signal_state='SIGNAL',direction=direction,
          signal_close_ts=int(bars[idx-1][0])+MINUTE if idx else int(base['known_ts']),
          signal_min_after_fail=idx,entry_ts=entry_ts,entry_min_after_fail=idx,
          entry_open=bars[idx][1],outcome=state,exit_min_after_entry=exit_min,
          exit_ts=entry_ts+(int(exit_min)-(1 if state.endswith('_GAP') else 0))*MINUTE,
          mfe_hold_pct=round(mfe,7),mae_hold_pct=round(mae,7),
          gross_pct=round(gross,7),net_pct=round(gross-cost,7))
    return result


def summarize(outcomes,events,group_type,group,mode,target):
    relevant=[e for e in events if group_type=='ALL' or (e['side'] if group_type=='SIDE' else
                         e['date_utc'] if group_type=='DATE' else e['observation_type'])==group]
    keys={e['touch_key'] for e in relevant}
    rows=[r for r in outcomes if r['mode']==mode and r['target_pct']==target and r['touch_key'] in keys]
    eligible=[r for r in rows if r['cohort_eligible']]
    signal=[r for r in eligible if r['signal_state']=='SIGNAL']
    counts=Counter(r['outcome'] for r in signal)
    tp=counts['TP_FIRST']+counts['TP_GAP']
    sl=counts['SL_FIRST']+counts['SL_GAP']
    n=len(eligible);m=len(signal)
    gross=sum(r['gross_pct'] for r in signal)
    net=sum(r['net_pct'] for r in signal)
    return dict(group_type=group_type,group=group,mode=mode,target_pct=target,
        cohort_total=len(relevant),eligible_common=n,
        coverage_pct=round(n/len(relevant)*100,3) if relevant else None,
        signal_n=m,signal_pct=round(m/n*100,3) if n else None,
        no_signal_n=sum(r['signal_state']=='NO_SIGNAL' for r in eligible),
        invalid_metadata_n=sum(r['signal_state']=='INVALID_METADATA' for r in eligible),
        censored_n=len(relevant)-n,tp_first_n=tp,sl_first_n=sl,
        sl_gap_n=counts['SL_GAP'],tp_gap_n=counts['TP_GAP'],
        ambiguous_n=counts['SL_AMBIGUOUS'],time_exit_n=counts['TIME_EXIT'],
        tp_rate_eligible_pct=round(tp/n*100,3) if n else None,
        sl_rate_eligible_pct=round(sl/n*100,3) if n else None,
        tp_rate_signalled_pct=round(tp/m*100,3) if m else None,
        gross_sum_pct_points=round(gross,6),net_sum_pct_points=round(net,6),
        net_mean_per_eligible_pct=round(net/n,6) if n else None,
        net_mean_per_signal_pct=round(net/m,6) if m else None)


def write_csv_atomic(path,rows,fields):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.failed_reclaim_',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
            w.writeheader();w.writerows(rows)
            f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def write_json_atomic(path,manifest):
    path=Path(path);fd,temp=tempfile.mkstemp(prefix='.failed_reclaim_',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(manifest,f,ensure_ascii=False,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def build(project,*,journal_db=None,first_touch_db=None,auto_db=None,recovery_root=None,
          sample_metadata=None,max_wait=60,hold=180,cost=0.20,out=None):
    """Create standalone DERIVED reports only; source SQLite never written."""
    if not (1<=max_wait<=120 and 15<=hold<=360 and max_wait+hold<=360 and
            0<=cost<=5):
        raise ValueError('Invalid wait/hold/cost configuration')
    project=Path(project).resolve()
    recovery_root=Path(recovery_root) if recovery_root else project/'reports/reaction_lab/first_touch_historical_recovery_v11'
    if sample_metadata:
        journal=read_sample_journal(sample_metadata)
        iterator=sample_paths(journal,recovery_root)
        scope='SAMPLE_V11_ONLY_NO_ONLINE_PREFIX'
    else:
        journal=read_journal(journal_db or project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3')
        index,batches=recovery_index(recovery_root)
        iterator=iter_events(project,first_touch_db=first_touch_db,auto_db=auto_db,
                             recovery=index,include_bars=True,only_status='TOUCH_FAILED')
        scope='FULL_VPS_VERIFIED_ONLINE_AND_HISTORY'
    events=[];outcomes=[];seen=set()
    def consume(row,bars):
        key=row['touch_key']
        if key in seen:raise ValueError('Duplicate first-touch key: '+key)
        seen.add(key)
        if row['first_touch_status']!='TOUCH_FAILED':raise ValueError('Wrong status from source')
        ev=event_base(row,bars,journal.get(key),max_wait,hold)
        events.append(ev)
        raw=journal.get(key)
        meta=raw.get('meta') if raw else None
        for mode in MODES:
            for tp in TARGETS:
                outcomes.append(calculate_mode(ev,bars,meta,mode,hold,max_wait,tp,cost))
    # Stream paths: never accumulate 360 bars x thousands of events in RAM.
    for row,bars in iterator:
        consume(row,bars)
    # The journal and path collector are separately running live processes.
    # Preserve just-recorded journal events even if the path collector has not
    # registered their key yet: NO_PATH/CENSORED, not an invented loser.
    for key in sorted(set(journal)-seen):
        raw=journal[key]
        meta=raw.get('meta') if isinstance(raw.get('meta'),dict) else {}
        t=int(raw['retest_ts'])
        row=dict(touch_key=key,symbol=raw['symbol'],side=raw['side'],
                 first_touch_status='TOUCH_FAILED',retest_ts=t,entry_ts=t+MINUTE,
                 date_utc=date_utc(t),retest_close=meta.get('retest_close'),
                 path_source='JOURNAL_ONLY_AWAITING_PATH_COLLECTOR',
                 path_quality='NO_CONTIGUOUS_PATH',path_count=0)
        consume(row,[])
    summaries=[]
    for group_type,groups in [('ALL',['ALL']),('SIDE',['LONG','SHORT']),
                              ('DATE',sorted({r['date_utc'] for r in events})),
                              ('OBSERVATION',sorted({r['observation_type'] for r in events}))]:
        for group in groups:
            for mode in MODES:
                for tp in TARGETS:
                    summaries.append(summarize(outcomes,events,group_type,group,mode,tp))
    base=Path(out).resolve() if out else project/OUTPUT
    manifest={'schema':SCHEMA,'created_utc':datetime.now(timezone.utc).isoformat(),
      'scope':scope,'source_journal':str(sample_metadata or journal_db or project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'),
      'historical_root':str(recovery_root),
      'cohort_total':len(events),
      'journal_total':len(journal),
      'journal_without_path':sum(e['path_source']=='JOURNAL_ONLY_AWAITING_PATH_COLLECTOR' for e in events),
      'path_without_journal':sum(e['metadata_state']=='MISSING_JOURNAL' for e in events),
      'coverage_common':sum(e['eligible_common_180m'] for e in events),
      'metadata_states':dict(Counter(e['metadata_state'] for e in events)),
      'sources':dict(Counter(e['path_source'] for e in events)),
      'statuses':dict(Counter(e['first_touch_status'] for e in events)),
      'observation_types':dict(Counter(e['observation_type'] for e in events)),
      'mode_count':len(MODES),'modes':list(MODES),'targets_pct':list(TARGETS),
      'settings':{'max_wait_min':max_wait,'hold_min':hold,'sl_pct':1.0,
                  'cost_roundtrip_pct_points':cost,'minimum_common_bars':max_wait+hold,
                  'band_reentry_tolerance_pct':.10,'reclaim_confirm_buffer_pct':.05},
      'notes':[
          'Same full failed cohort and >=max_wait+hold continuous bars denominator for ALL modes.',
          'Reclaim definition uses closed post-fail 1m bars; next OPEN only.',
          'IMMEDIATE means at first post-fail open, NOT actual LIVE first_seen fill.',
          'Historical reconstructed labels are retrospective observations; not LIVE execution evidence.',
          'NO_SIGNAL events remain in denominator (zero exposure).',
          'Journal-only fresh events remain CENSORED when the path collector has not yet registered them.',
          'MFE/MAE are full post-entry excursions, including after simulated exits.',
          'TP+SL one candle -> SL_AMBIGUOUS; -SL used for conservative net only.',
          'Cost constant roundtrip in percentage points; no funding/slippage/liquidity.',
          'One simulated trade per event/mode; overlapping events are NOT a portfolio.',
          'Exploratory cohort; no out-of-sample validation.',
          'Sample mode verifies V11 manifest SHA/identity/continuity but not online prefix parity.'
          if sample_metadata else
          'Full mode uses Runner Discovery historical SHA/identity/online prefix OHLC parity.'
      ]}
    base.mkdir(parents=True,exist_ok=True)
    write_csv_atomic(base/'events.csv',events,EVENT_FIELDS)
    write_csv_atomic(base/'event_outcomes.csv',outcomes,OUTCOME_FIELDS)
    write_csv_atomic(base/'summary.csv',summaries,SUMMARY_FIELDS)
    write_json_atomic(base/'manifest.json',manifest)
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.')
    p.add_argument('--journal-db')
    p.add_argument('--first-touch-db')
    p.add_argument('--auto-db')
    p.add_argument('--recovery-root')
    p.add_argument('--sample-metadata',help='Use packaged metadata CSV + validated sample V11 batch; pilot only')
    p.add_argument('--max-wait',type=int,default=60)
    p.add_argument('--hold',type=int,default=180)
    p.add_argument('--cost-roundtrip-pct',type=float,default=.20)
    p.add_argument('--out')
    x=p.add_mutually_exclusive_group(required=True)
    x.add_argument('--plan',action='store_true')
    x.add_argument('--run',action='store_true')
    a=p.parse_args(argv)
    if not (1<=a.max_wait<=120 and 15<=a.hold<=360 and a.max_wait+a.hold<=360 and
            0<=a.cost_roundtrip_pct<=5):p.error('bad wait/hold/cost')
    if a.plan:
        j=a.sample_metadata or a.journal_db or str(Path(a.project)/'reports/reaction_lab/scanner_first_touches_v1.sqlite3')
        metadata=read_sample_journal(j) if a.sample_metadata else read_journal(j)
        print('FAILED_RECLAIM_PLAN '+json.dumps({'mode':'PLAN','journal_failed':len(metadata),
            'modes':len(MODES),'targets_pct':list(TARGETS),'common_min_bars':a.max_wait+a.hold,
            'read_only_sources':True,'sample':bool(a.sample_metadata)},ensure_ascii=False))
        return 0
    manifest=build(a.project,journal_db=a.journal_db,first_touch_db=a.first_touch_db,
                   auto_db=a.auto_db,recovery_root=a.recovery_root,
                   sample_metadata=a.sample_metadata,max_wait=a.max_wait,hold=a.hold,
                   cost=a.cost_roundtrip_pct,out=a.out)
    print('FAILED_RECLAIM_RESULT '+json.dumps(manifest,ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
