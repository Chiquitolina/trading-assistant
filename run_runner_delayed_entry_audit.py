#!/usr/bin/env python3
"""Read-only, exploratory delayed-entry experiment for frozen 2026-10-14 Continuous V1 cohort.

No production writes. Uses archived complete 360-bar 1m paths and existing decode/forward code.
Strategies: ORIGINAL_OPEN; WAIT_5_ALL; WAIT_5_FAVORABLE_CLOSE; WAIT_10_ALL; WAIT_10_FAVORABLE_CLOSE.
Signal qualification for favorable-close uses ONLY close of the bar preceding the candidate entry.
Entry occurs at NEXT 1m OPEN relative to that close. TP/SL measured from ACTUAL entry OPEN.
Exit after 180 1m bars FROM EACH entry; conservative ambiguous crossing as stop first.
All strategies evaluated on the same discovered cohort, NOT an out-of-sample validation.
"""
from __future__ import annotations
import argparse,csv,math,sqlite3,sys
from pathlib import Path
from collections import defaultdict,Counter

M=60_000
MODES=(('ORIGINAL_OPEN',0,False),('WAIT_5_ALL',5,False),('WAIT_5_FAVORABLE_CLOSE',5,True),
       ('WAIT_10_ALL',10,False),('WAIT_10_FAVORABLE_CLOSE',10,True))
SCENARIOS=((2.,.5),(2.,1.),(2.,1.5),(3.,1.),(3.,1.5),(3.,2.))

def number(x):
 try:
  n=float(x)
  return n if math.isfinite(n) else None
 except (TypeError,ValueError,OverflowError):return None

def selected(row):
 return all(number(row.get(k)) is not None and number(row[k])>=v for k,v in
    (('max_departure_pct',1.5),('opposing_room_1h_pct',2.),('volume_rank_pct_4h',60.),('return_rank_pct_4h',60.)))

def cohort(row):
 return '4_FACTORS' if selected(row) else ('REST_ROOM_AVAILABLE' if number(row.get('opposing_room_1h_pct')) is not None else 'ROOM_MISSING')

def scn(tp,sl):return f'TP{tp:g}_SL{sl:g}'

def eligible(path,side,offset,favorable_close):
 if not favorable_close:return True
 original=float(path[0][1])
 observed_close=float(path[offset-1][4])
 return ((observed_close>original) if side=='LONG' else (observed_close<original))

def simulate(path,side,offset,tp,sl,fee_pct=.10,horizon=180):
 """Mirrors legacy > TP, >= SL. Path must have been validated upstream."""
 if side not in ('LONG','SHORT'):raise ValueError('bad side')
 if len(path)<offset+horizon:raise ValueError('incomplete delayed path')
 sign=1 if side=='LONG' else -1
 bars=path[offset:offset+horizon]
 entry=float(bars[0][1]);result='NO_HIT';minute=horizon
 for i,(_ts,_op,hi,lo,_c) in enumerate(bars,1):
  gain=100*((float(hi)/entry-1) if sign==1 else (1-float(lo)/entry))
  adverse=100*((1-float(lo)/entry) if sign==1 else (float(hi)/entry-1))
  if gain>tp and adverse>=sl:result='AMBIGUOUS_SL_FIRST';minute=i;break
  if adverse>=sl:result='SL_FIRST';minute=i;break
  if gain>tp:result='TP_FIRST';minute=i;break
 if result=='TP_FIRST':gross=tp
 elif result in ('SL_FIRST','AMBIGUOUS_SL_FIRST'):gross=-sl
 else:gross=100*sign*(float(bars[-1][4])/entry-1)
 return dict(result=result,minute=minute,entry_price=entry,gross_pct=gross,net_pct=gross-fee_pct)

def aggregate(items):
 n=len(items);labels=Counter(x['result'] for x in items);vals=[float(x['net_pct']) for x in items]
 p=sum(x for x in vals if x>0);l=-sum(x for x in vals if x<0)
 return dict(n=n,tp=labels['TP_FIRST'],sl=labels['SL_FIRST'],amb=labels['AMBIGUOUS_SL_FIRST'],
             time=labels['NO_HIT'],mean_net_pct=round(sum(vals)/n,6) if n else '',
             profit_factor=round(p/l,5) if l>0 else ('INF' if p else ''),
             tp_rate=round(100*labels['TP_FIRST']/n,3) if n else '')

def write_csv(file,items,fields):
 with open(file,'w',newline='',encoding='utf-8') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(items)

def calc(records,paths,fees=.10):
 """Pure over complete path data; a strict validator checks real data in main."""
 outputs=[];issues=[]
 for r in records:
  key=r['event_key'];p=paths.get(key)
  if p is None:issues.append(dict(event_key=key,issue='PATH_MISSING'));continue
  group=cohort(r)
  for mode,offset,confirm in MODES:
   if not eligible(p,r['signal'],offset,confirm):continue
   for tp,sl in SCENARIOS:
    v=simulate(p,r['signal'],offset,tp,sl,fee_pct=fees)
    outputs.append(dict(event_key=key,symbol=r['symbol'],side=r['signal'],group=group,
      known_ts=r['known_ts'],mode=mode,entry_offset_min=offset,entry_ts=int(r['known_ts'])+offset*M,
      scenario=scn(tp,sl),tp_pct=tp,sl_pct=sl,**v))
 return outputs,issues

def summaries(details,records):
 groups=defaultdict(list);paired=defaultdict(list)
 counts=Counter(cohort(r) for r in records)
 bykey={(d['event_key'],d['mode'],d['scenario']):d for d in details}
 for d in details:
  for g in (d['group'],'ALL_COMPLETE',d['group']+'_'+d['side']):groups[(g,d['mode'],d['scenario'])].append(d)
 summary=[];comparisons=[]
 for (grp,mode,scenario),entries in sorted(groups.items()):
  denominator=(len(records) if grp=='ALL_COMPLETE' else
      sum(1 for r in records if (cohort(r)==grp if not grp.endswith(('_LONG','_SHORT')) else
      (cohort(r)==grp.rsplit('_',1)[0] and r['signal']==grp.rsplit('_',1)[1]))))
  summary.append(dict(group=grp,mode=mode,scenario=scenario,eligible_total=denominator,
    entry_fraction_pct=round(100*len(entries)/denominator,3) if denominator else '',**aggregate(entries)))
  if mode=='ORIGINAL_OPEN':continue
  matched=[(e,bykey[(e['event_key'],'ORIGINAL_OPEN',scenario)]) for e in entries]
  if len(matched)!=len(entries):raise RuntimeError('Missing baseline for matched comparison')
  diff=[float(a['net_pct'])-float(b['net_pct']) for a,b in matched]
  comparisons.append(dict(group=grp,mode=mode,scenario=scenario,matched_n=len(matched),
    matched_baseline_mean_net_pct=round(sum(float(b['net_pct']) for a,b in matched)/len(matched),6),
    matched_delayed_mean_net_pct=round(sum(float(a['net_pct']) for a,b in matched)/len(matched),6),
    matched_delta_net_pct=round(sum(diff)/len(diff),6),
    better=sum(v>0 for v in diff),worse=sum(v<0 for v in diff),equal=sum(v==0 for v in diff),
    baseline_tp=sum(b['result']=='TP_FIRST' for a,b in matched),
    delayed_tp=sum(a['result']=='TP_FIRST' for a,b in matched)))
 return summary,comparisons

def main():
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--project',default='/home/multiservices-1m')
 ap.add_argument('--csv',default=str(Path(__file__).with_name('runner_matrix_cohort_2026_10_14.csv')))
 ap.add_argument('--out',default='/root/runner_delayed_entry_results_20261014')
 ap.add_argument('--fees',type=float,default=.10)
 a=ap.parse_args()
 if not (0<=a.fees<=5):ap.error('--fees invalid')
 db=Path(a.project)/'reports/reaction_lab/auto_paths_v1.sqlite3'
 if not db.is_file():ap.error(f'path database not found: {db}')
 sys.path.insert(0,str(Path(a.project).resolve()))
 from reaction_lab_auto_paths import decode_path
 from reaction_lab_deep_metrics import forward_metrics
 with open(a.csv,newline='',encoding='utf-8-sig') as f:records=list(csv.DictReader(f))
 if len(records)!=1311:ap.error(f'Expected 1311 records, got {len(records)}')
 if sum(selected(r) for r in records)!=72:ap.error('Frozen 4 factor selection changed')
 seen=set()
 for r in records:
  key=r['event_key']
  if key in seen:ap.error('Duplicate Event ID: '+key)
  seen.add(key)
  if r['path_state']!='COMPLETE' or key!=f"{r['symbol']}|{r['signal']}|{int(r['retest_timestamp'])}":
   ap.error('Input cohort mismatch: '+key)
  if int(r['known_ts'])!=int(r['retest_timestamp'])+M:ap.error('Known timestamp mismatch: '+key)
 rows={}
 with sqlite3.connect('file:'+db.resolve().as_posix()+'?mode=ro',uri=True) as con:
  for i in range(0,len(records),250):
   keys=[r['event_key'] for r in records[i:i+250]]
   for rec in con.execute('SELECT event_key,symbol,side,retest_ts,entry_ts,state,count,path_blob FROM events WHERE event_key IN ('+','.join('?'*len(keys))+')',keys):
    rows[rec[0]]=rec
 paths={};issues=[]
 for r in records:
  key=r['event_key'];z=rows.get(key)
  if z is None:issues.append(dict(event_key=key,issue='NOT_FOUND'));continue
  eid,sym,side,retest,entry_ts,state,count,blob=z
  if sym!=r['symbol'] or side!=r['signal'] or int(retest)!=int(r['retest_timestamp']) or int(entry_ts)!=int(r['known_ts']) or state!='COMPLETE' or count!=360:
   issues.append(dict(event_key=key,issue='INVALID_METADATA'));continue
  try:
   p=decode_path(blob)
   if len(p)!=360:raise ValueError('wrong path length')
   orig=forward_metrics(p,side,number(r['retest_close']),int(retest))
   if orig.get('valid_path_minutes')!=360:raise ValueError('not contiguous valid 1m')
   first=simulate(p,side,0,2.,1.,fee_pct=a.fees)
   if first['result']!=orig.get('tp_sl_2tp_1p0sl_180m'):
    raise ValueError('baseline diverges from legacy')
   if not math.isclose(float(p[0][1]),float(r['entry_price']),rel_tol=1e-9,abs_tol=1e-12):
    raise ValueError('entry price different from frozen cohort')
   paths[key]=p
  except Exception as ex:issues.append(dict(event_key=key,issue=str(ex)))
 if issues:ap.error(f'{len(issues)} path problems; first: {issues[0]}')
 details,calc_issues=calc(records,paths,fees=a.fees)
 if calc_issues:ap.error(f'Calculation issues: {calc_issues[:3]}')
 baseline=[r for r in details if r['group']=='4_FACTORS' and r['mode']=='ORIGINAL_OPEN' and r['scenario']=='TP2_SL1']
 cn=Counter(r['result'] for r in baseline)
 if (len(baseline),cn['TP_FIRST'],cn['SL_FIRST'],cn['NO_HIT'])!=(72,18,49,5):
  ap.error(f'Baseline 4F parity mismatch {cn}')
 summary,comp=summaries(details,records)
 dest=Path(a.out);dest.mkdir(parents=True,exist_ok=True)
 write_csv(dest/'entry_mode_matrix_summary.csv',summary,['group','mode','scenario','eligible_total','entry_fraction_pct','n','tp','sl','amb','time','mean_net_pct','profit_factor','tp_rate'])
 write_csv(dest/'matched_vs_immediate.csv',comp,['group','mode','scenario','matched_n','matched_baseline_mean_net_pct','matched_delayed_mean_net_pct','matched_delta_net_pct','better','worse','equal','baseline_tp','delayed_tp'])
 write_csv(dest/'per_event_entry_modes.csv',details,['event_key','symbol','side','group','known_ts','mode','entry_offset_min','entry_ts','scenario','tp_pct','sl_pct','entry_price','result','minute','gross_pct','net_pct'])
 write_csv(dest/'issues.csv',issues,['event_key','issue'])
 print('PATHS:',len(paths),'of',len(records),'| selected:',sum(selected(r) for r in records),'| LEGACY PARITY: OK | issues:',len(issues))
 for r in summary:
  if r['group']=='4_FACTORS' and r['scenario'] in ('TP2_SL1','TP3_SL1'):
   print(f"{r['mode']:24s} {r['scenario']:8s} n={r['n']:3d}/{r['eligible_total']} TP={r['tp']:2d} SL={r['sl']:2d} NO_HIT={r['time']:2d} netavg={r['mean_net_pct']} PF={r['profit_factor']}")
 print('OUTPUT:',dest)

if __name__=='__main__':main()
