#!/usr/bin/env python3
"""V1.7 read-only A/B: REACTION + PRE-persistent horizontal level versus no PRE level.

Requires the pre-existing all-live 6423-event event_labels.csv plus scanner_events_v1
and auto_paths_v1 SQLite. Never computes labels with future bars. Never treats
UNKNOWN_HISTORY as NO_LEVEL. The third arm (level contact WITHOUT REACTION)
requires an independent, unconditioned level/contact census and is NOT forged here.

Primary execution is observable LIVE_OBSERVED next 1m open at or after
max(known_ts,first_seen_ms). Horizon ends 360m after REACTION retest, so a delayed
entry gets fewer executable minutes. TP2/SL1.5 on executable 1m OHLC, round-trip
fee 0.1%, adverse fill slippage 5 bps each side. Single slot is an independent
secondary replay (80% equity x1, initial $200).
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
import zlib

VERSION = 'reaction-persistent-level-ab-v1.7-research-only'
M = 60_000
BAR = struct.Struct('<qdddd')
GROUPS = ('PERSISTENT_LEVEL_NEAR_PRE', 'NO_PERSISTENT_LEVEL_NEAR_PRE', 'UNKNOWN_HISTORY')
EVENT_DB = Path('reports/reaction_lab/scanner_events_v1.sqlite3')
PATH_DB = Path('reports/reaction_lab/auto_paths_v1.sqlite3')
PERSISTENT_RUN = Path('reports/reaction_lab/reaction_persistent_horizontal_v1/runs/all-live_0_bbab543f0ffe')
OUT = Path('reports/reaction_lab/reaction_persistent_level_ab_v17')
TP_PCT, SL_PCT, HORIZON, FEE_PCT, SLIP_BPS = 2.0, 1.5, 360, 0.1, 5.0


def hash_file(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()


def readonly(path):
    if not path.is_file(): raise RuntimeError('MISSING_INPUT: '+str(path))
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=20)
    c.execute('PRAGMA query_only=ON')
    return c


def _columns(con, table): return {r[1] for r in con.execute(f'PRAGMA table_info({table})')}


def _normalize_status(s):
    s=str(s or '').upper().strip()
    return s if s in GROUPS else None


def inspect_label_file(path):
    with path.open('r',encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        cols=reader.fieldnames or []
        keys=[c for c in cols if c.lower() in ('event_key','reaction_event_key')]
        groupcols=[c for c in cols if 'group' in c.lower() or 'label' in c.lower() or 'persistent' in c.lower() or 'status' in c.lower()]
        if not keys or not groupcols:
            return {'path':str(path),'valid':False,'columns':cols[:40],'reason':'MISSING_EXPLICIT_EVENT_KEY_OR_GROUP_COLUMN'}
        counts={c:Counter() for c in groupcols}
        total=0
        for row in reader:
            total+=1
            for col in groupcols:
                g=_normalize_status(row.get(col))
                if g:counts[col][g]+=1
        ranked=sorted(groupcols,key=lambda k:sum(counts[k].values()),reverse=True)
        col=ranked[0]
        complete=sum(counts[col].values())
        return {'path':str(path),'valid':complete>0,'rows':total,'key_column':keys[0],
                'group_column':col,'classified':complete,'groups':dict(counts[col]),'columns':cols[:50]}


def locate_labels(root, explicit=None):
    if explicit is not None:
        f=Path(explicit).resolve(); return inspect_label_file(f)
    base=root/PERSISTENT_RUN
    if not base.is_dir(): raise RuntimeError('MISSING_PERSISTENT_RUN: '+str(base))
    candidates=[]
    for name in ('event_labels.csv','labels_pre.csv','persistent_event_labels.csv'):
        direct=base/name
        if direct.exists():candidates.append(direct)
    # Strictly inside the already-audited frozen all-live run.
    candidates.extend(p for p in base.rglob('*.csv') if 'label' in p.name.lower() and p not in candidates)
    if len(candidates)>250:raise RuntimeError('TOO_MANY_LABEL_FILES; pass --labels explicitly')
    scored=[]
    for f in candidates:
        try:
            result=inspect_label_file(f)
            if result['valid']:
                scored.append((result['classified'],result['rows'],result))
        except (OSError,UnicodeDecodeError,csv.Error): continue
    if not scored:
        choices=[str(p.relative_to(base)) for p in candidates[:20]]
        raise RuntimeError('NO_RECOGNIZED_ALL_LIVE_PRE_LABELS. Candidates: '+json.dumps(choices)+'. Use --labels.')
    scored.sort(reverse=True,key=lambda x:(x[0],x[1]))
    return scored[0][2]


def load_labels(meta):
    if not meta['valid']: raise RuntimeError('INVALID_LABEL_SCHEMA: '+repr(meta))
    result={}
    with Path(meta['path']).open('r',encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            k=str(r.get(meta['key_column']) or '').strip()
            g=_normalize_status(r.get(meta['group_column']))
            if not k or not g: continue
            if k in result:
                if result[k]!=g:raise RuntimeError('CONFLICTING_LABEL:'+k)
                raise RuntimeError('DUPLICATE_LABEL:'+k)
            result[k]=g
    if len(result)<1:raise RuntimeError('ZERO_CAUSAL_LABELS')
    return result


def decode_path(blob):
    if not blob:return []
    raw=zlib.decompress(blob)
    if len(raw)%BAR.size:raise RuntimeError('INVALID_PATH_BLOB_SIZE')
    return [BAR.unpack_from(raw,i) for i in range(0,len(raw),BAR.size)]


def validate_path(rows, retest_ts, entry_ts, count):
    if count!=HORIZON or len(rows)!=HORIZON:return False
    if int(entry_ts)!=int(retest_ts)+M:return False
    for i,(ts,o,h,l,c) in enumerate(rows):
        if ts!=entry_ts+i*M:return False
        if not all(math.isfinite(v) and v>0 for v in (o,h,l,c)) or h<max(o,c,l) or l>min(o,c,h):return False
    return True


def first_observed_index(rows, known, first_seen):
    if known is None or first_seen is None:return None
    earliest=max(int(known),int(first_seen))
    for i,row in enumerate(rows):
        if row[0]>=earliest:return i
    return None


def simulate_path(bars, idx, side, tp=TP_PCT, sl=SL_PCT, fee=FEE_PCT, slip_bps=SLIP_BPS):
    if idx is None or not 0<=idx<len(bars):return None
    if side not in ('LONG','SHORT'):return None
    direct=1 if side=='LONG' else -1
    base=float(bars[idx][1]); slip=slip_bps/10000
    entry=base*(1+direct*slip)
    tp_level=entry*(1+direct*tp/100)
    sl_level=entry*(1-direct*sl/100)
    state='NO_HIT'; exit_idx=len(bars)-1; exit_px=float(bars[-1][4])
    for j in range(idx,len(bars)):
        _,o,h,l,c=bars[j]
        hit_tp=(h>=tp_level) if direct==1 else (l<=tp_level)
        hit_sl=(l<=sl_level) if direct==1 else (h>=sl_level)
        if hit_tp or hit_sl:
            if hit_sl:
                state='SL_FIRST' if not hit_tp else 'BOTH_SAME_1M_SL_CONSERVATIVE'
                target=sl_level
                # Market stop that gaps across threshold fills at the worse opening.
                exit_px=min(target,o) if direct==1 else max(target,o)
            else:
                state='TP_FIRST'
                target=tp_level
                exit_px=target
            exit_idx=j
            break
    exit_fill=exit_px*(1-direct*slip)
    net=(exit_fill/entry-1)*direct*100-fee
    return {'state':state,'net_pct':net,'entry_ts':int(bars[idx][0]),
            'exit_ts':int(bars[exit_idx][0])+M,'entry_price':entry,'exit_price':exit_fill,
            'entry_delay_min':idx}


def summarize(rows, group):
    r=[x for x in rows if x['group']==group]
    n=len(r)
    c=Counter(x['outcome'] for x in r)
    valid=[x for x in r if x['net_pct'] is not None]
    return {'group':group,'N_labels':n,'N_evaluable':len(valid),
            'TP':c['TP_FIRST'],'SL':c['SL_FIRST'],'SL_AMBIGUOUS':c['BOTH_SAME_1M_SL_CONSERVATIVE'],
            'TIMEOUT':c['NO_HIT'],'NOT_EVALUABLE':n-len(valid),
            'tp_rate_all_evaluable_pct':round(c['TP_FIRST']/len(valid)*100,2) if valid else None,
            'mean_net_pct':round(sum(x['net_pct'] for x in valid)/len(valid),5) if valid else None,
            'symbols':len({x['symbol'] for x in valid})}


def one_slot(rows, group, equity=200, allocation=.8):
    candidates=sorted((x for x in rows if x['group']==group and x['net_pct'] is not None),
                      key=lambda x:(x['entry_ts'],x['symbol'],x['event_key']))
    busy_until=-1; started=equity; peak=equity; dd=0; trades=[]; skipped=0
    for x in candidates:
        if x['entry_ts']<busy_until:
            skipped+=1;continue
        # independent trade return on 80% of available equity; x1, no margin rehypothecation
        pnl=equity*allocation*x['net_pct']/100
        equity+=pnl
        if equity<=0:raise RuntimeError('NEGATIVE_EQUITY')
        peak=max(peak,equity);dd=max(dd,(peak-equity)/peak*100)
        busy_until=x['exit_ts'];trades.append(x)
    gains=[x['net_pct'] for x in trades if x['net_pct']>0]
    losses=[-x['net_pct'] for x in trades if x['net_pct']<0]
    return {'group':group,'candidate_signals':len(candidates),'trades':len(trades),
            'skipped_slot_busy':skipped,'starting_usd':started,'ending_usd':round(equity,5),
            'return_pct':round((equity/started-1)*100,4),
            'max_drawdown_realised_pct':round(dd,4),'trade_TP':sum(x['outcome']=='TP_FIRST' for x in trades),
            'trade_SL':sum('SL_FIRST' in x['outcome'] or x['outcome'].startswith('BOTH') for x in trades),
            'trade_TIMEOUT':sum(x['outcome']=='NO_HIT' for x in trades),
            'profit_factor_pct_units':round(sum(gains)/sum(losses),4) if losses else None}


def load_reactions(root, labels):
    scanner=readonly(root/EVENT_DB); paths=readonly(root/PATH_DB)
    try:
        needed={'event_key','symbol','side','retest_ts','known_ts','first_seen_ms','observation_type'}
        if not needed<=_columns(scanner,'events'):raise RuntimeError('SCANNER_SCHEMA_CHANGED')
        if not {'event_key','entry_ts','state','count','path_blob'}<=_columns(paths,'events'):raise RuntimeError('AUTO_PATH_SCHEMA_CHANGED')
        rows=[];counts=Counter();missing_labels=set(labels)
        events=scanner.execute('SELECT event_key,symbol,side,retest_ts,known_ts,first_seen_ms,observation_type FROM events ORDER BY retest_ts,event_key')
        for key,symbol,side,retest,known,seen,origin in events:
            if key not in labels:continue
            missing_labels.discard(key)
            group=labels[key]
            counts['label_'+group]+=1
            if origin!='LIVE_OBSERVED':
                rows.append({'event_key':key,'symbol':symbol,'side':side,'group':group,'observation_type':origin,
                             'outcome':'NOT_LIVE_OBSERVED','net_pct':None,'entry_ts':None,'exit_ts':None})
                counts['not_live_observed']+=1;continue
            path=paths.execute('SELECT entry_ts,state,count,path_blob FROM events WHERE event_key=?',(key,)).fetchone()
            if not path or path[1]!='COMPLETE' or not path[3]:
                state='PATH_MISSING_OR_INCOMPLETE';outcome=None
            else:
                try:
                    bar=decode_path(path[3])
                    if not validate_path(bar,int(retest),int(path[0]),int(path[2])):
                        state='INVALID_PATH';outcome=None
                    else:
                        idx=first_observed_index(bar,known,seen)
                        if idx is None:state='OBSERVED_ENTRY_OUTSIDE_360M';outcome=None
                        else:state='COMPLETE_VALID';outcome=simulate_path(bar,idx,side)
                except (ValueError,OverflowError,TypeError,zlib.error,struct.error,RuntimeError):
                    state='PATH_DECODE_ERROR';outcome=None
            r={'event_key':key,'symbol':symbol,'side':side,'group':group,'observation_type':origin,
               'outcome':outcome['state'] if outcome else state,
               'net_pct':outcome['net_pct'] if outcome else None,
               'entry_ts':outcome['entry_ts'] if outcome else None,
               'exit_ts':outcome['exit_ts'] if outcome else None,
               'entry_delay_min':outcome['entry_delay_min'] if outcome else None,
               'retest_ts':retest}
            rows.append(r);counts[r['outcome']]+=1
        return rows,{'source_events_matched':len(rows),'labels_not_in_scanner':len(missing_labels),
                     'sample_missing_labels':sorted(missing_labels)[:5], 'qa_counts':dict(counts)}
    finally:scanner.close();paths.close()


def csv_atomic(path, rows, fields):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    temp.replace(path)


def run(root, label_meta, save=False):
    labels=load_labels(label_meta)
    # guard that the same all-live cohort is used, never use mismatched label files
    if len(labels)<5000:raise RuntimeError(f'NOT_ALL_LIVE_COHORT: {len(labels)} labels (need >=5000).')
    rows,qa=load_reactions(root,labels)
    if len(rows)/len(labels)<.98:raise RuntimeError('EVENT_ID_MISMATCH: '+repr(qa))
    allowed=[x for x in rows if x['group'] in GROUPS[:2]]
    report={'version':VERSION,'source_label_file':label_meta['path'],
            'source_hash_sha256':{label_meta['path']:hash_file(Path(label_meta['path']))},
            'label_groups':dict(Counter(labels.values())),'execution':{'primary':'LIVE_OBSERVED_ONLY',
            'entry':'first archived 1m OPEN at/after BOTH known_ts and first_seen_ms',
            'horizon':'360m absolute since REACTION retest (not 360m after delayed entry)',
            'tp_pct':TP_PCT,'sl_pct':SL_PCT,'fee_roundtrip_pct':FEE_PCT,
            'slippage_bps_per_side':SLIP_BPS,'same_minute':'SL_FIRST'},
            'qa':qa,'primary':[summarize(allowed,g) for g in GROUPS[:2]],
            'by_side':{side:[summarize([x for x in allowed if x['side']==side],g) for g in GROUPS[:2]]
                       for side in ('LONG','SHORT')},
            'one_slot_simulations':[one_slot(allowed,g) for g in GROUPS[:2]],
            'control_C_status':'NOT_EVALUATED: a contact-without-REACTION universe must be generated independently of REACTION-conditioned zones',
            'caveats':['Exploratory: this all-live cohort was already examined and must not be treated as OOS',
                       'Unknown PRE history is excluded, never reclassified as NO_LEVEL',
                       'Missing/incomplete 1m paths and unavailable LIVE_OBSERVED entries are not losses',
                       'TP/SL evaluated using 1m OHLC, conservative same-bar ties and fixed slippage',
                       'Simulated separate portfolios A/B do not constitute a combined strategy',
                       'No funding, latency variance, real order book fills, or exchange mechanics',
                       'Proximity to a persistent level does NOT establish side-compatible support or resistance',
                       'Input scanner/paths databases may continue changing for events outside this frozen labeled cohort; verify recomputes only labeled events']}
    if save:
        folder=root/OUT;folder.mkdir(parents=True,exist_ok=True)
        fields=['event_key','symbol','side','group','observation_type','outcome','net_pct','entry_ts','exit_ts','entry_delay_min','retest_ts']
        csv_atomic(folder/'event_comparison.csv',rows,fields)
        temp=folder/'report.json.tmp'
        temp.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
        temp.replace(folder/'report.json')
        print('REPORT_PATH',folder/'report.json')
    print('REACTION_LEVEL_AB_V17',json.dumps(report,ensure_ascii=False))
    return report


def cli():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='/home/multiservices-1m')
    p.add_argument('--labels',type=str,default=None,help='Optional explicit causal all-live event_labels.csv')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true')
    g.add_argument('--run',action='store_true')
    g.add_argument('--verify',action='store_true')
    a=p.parse_args();root=Path(a.root).resolve()
    meta=locate_labels(root,a.labels)
    if a.plan:
        r={'version':VERSION,'root':str(root),'selected_label_file':meta,
           'paths':{str(x):str(root/x) for x in (EVENT_DB,PATH_DB)},
           'configuration':{'tp':TP_PCT,'sl':SL_PCT,'horizon_min':HORIZON,'fee_roundtrip':FEE_PCT,
                            'slippage_bps_each_side':SLIP_BPS},
           'mode':'READ_ONLY_INPUTS; ONLY SAVES CSV/REPORT TO ISOLATED RESEARCH FOLDER'}
        print('REACTION_LEVEL_AB_PLAN',json.dumps(r,ensure_ascii=False));return
    if a.run:run(root,meta,save=True);return
    report=root/OUT/'report.json'
    if not report.is_file():raise RuntimeError('NO_REPORT: run --run first')
    existing=json.loads(report.read_text(encoding='utf-8'))
    for path,sha in existing['source_hash_sha256'].items():
        if hash_file(Path(path))!=sha:raise RuntimeError('SOURCE_HASH_CHANGED: '+path)
    regenerated=run(root,meta,save=False)
    if regenerated!=existing:raise RuntimeError('REPRODUCIBILITY_MISMATCH')
    print('REACTION_LEVEL_AB_VERIFY_SUCCESS')


if __name__=='__main__':
    try:cli()
    except (OSError,RuntimeError,ValueError,sqlite3.Error) as exc:
        print('REACTION_LEVEL_AB_ERROR:',exc)
        raise SystemExit(2)
