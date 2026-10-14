#!/usr/bin/env python3
"""Read-only post-entry path diagnostics for frozen 2026-10-14 Runner cohort.

Uses canonical Continuous V1 paths and verifies existing 2%/1% first-touch semantics.
This is descriptive analysis, not an entry policy, holdout, or 1-slot portfolio.
"""
from __future__ import annotations
import argparse
import csv
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

MINUTE_MS = 60_000
HEADER = ('event_key','symbol','signal','retest_timestamp','known_ts','retest_close','entry_price',
          'max_departure_pct','opposing_room_1h_pct','volume_rank_pct_4h','return_rank_pct_4h','mfe_180m_pct')


def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def selected(r):
    return all(num(r.get(k)) is not None and num(r[k])>=threshold for k,threshold in (
        ('max_departure_pct',1.5),('opposing_room_1h_pct',2.0),
        ('volume_rank_pct_4h',60.0),('return_rank_pct_4h',60.0)))


def fmt_number(n):
    return '' if n is None else round(n, 6)


def pct_median(values):
    v=sorted(float(x) for x in values if num(x) is not None)
    if not v: return ''
    return round(statistics.median(v),4)


def write_csv(path, items, columns):
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(items)


def path_features(path, side, horizon=180):
    """Order known only across full 1m bars; dual hits same minute remain ambiguous."""
    if side not in ('LONG','SHORT'): raise ValueError('bad side')
    if len(path)<horizon: raise ValueError('path too short')
    entry=float(path[0][1]);
    if entry<=0 or not math.isfinite(entry): raise ValueError('invalid entry')
    sign=1 if side=='LONG' else -1
    favorable=[]; adverse=[]; close_move=[]
    tp_cutoffs=(0.25,0.5,1.0,2.0,3.0)
    sl_cutoffs=(0.5,1.0,1.5,2.0)
    hit_tp={x:None for x in tp_cutoffs}
    hit_sl={x:None for x in sl_cutoffs}
    for i,(ts,op,hi,lo,close) in enumerate(path[:horizon],1):
        op,hi,lo,close=map(float,(op,hi,lo,close))
        if min(op,hi,lo,close)<=0 or lo>min(op,close,hi) or hi<max(op,close,lo):
            raise ValueError(f'bad OHLC at minute {i}')
        f=100*(hi/entry-1) if sign==1 else 100*(1-lo/entry)
        a=100*(1-lo/entry) if sign==1 else 100*(hi/entry-1)
        c=100*sign*(close/entry-1)
        favorable.append(f);adverse.append(a);close_move.append(c)
        for k in tp_cutoffs:
            if hit_tp[k] is None and f>k: hit_tp[k]=i  # strict legacy TP
        for k in sl_cutoffs:
            if hit_sl[k] is None and a>=k: hit_sl[k]=i  # inclusive legacy SL
    tp2=hit_tp[2.0];sl1=hit_sl[1.0]
    if sl1 is not None and tp2 is not None and sl1==tp2:
        outcome='AMBIGUOUS_SL_FIRST'
    elif sl1 is not None and (tp2 is None or sl1<tp2):
        outcome='SL_FIRST'
    elif tp2 is not None:
        outcome='TP_FIRST'
    else:
        outcome='NO_HIT'
    if outcome in ('SL_FIRST','AMBIGUOUS_SL_FIRST'):exit_min=sl1
    elif outcome=='TP_FIRST':exit_min=tp2
    else:exit_min=horizon
    peak_before_sl=max(favorable[:sl1-1]) if sl1 and sl1>1 else None
    risk_before_tp=max(adverse[:tp2-1]) if tp2 and tp2>1 else None
    return dict(
        result=outcome,exit_minute=exit_min,
        mfe_180m_pct=max(0.,max(favorable)),mae_180m_pct=max(0.,max(adverse)),
        runner_ge2_from_entry=max(0.,max(favorable))>=2,
        runner_gt2_from_entry=max(favorable)>2,
        first_tp_0p25_min=hit_tp[0.25],first_tp_0p5_min=hit_tp[0.5],
        first_tp_1_min=hit_tp[1.0],first_tp_2_min=tp2,first_tp_3_min=hit_tp[3.0],
        first_sl_0p5_min=hit_sl[0.5],first_sl_1_min=sl1,
        first_sl_1p5_min=hit_sl[1.5],first_sl_2_min=hit_sl[2.0],
        sl1_then_tp2=(sl1 is not None and tp2 is not None and sl1<tp2),
        tp2_and_sl1_same_min=(sl1 is not None and tp2 is not None and sl1==tp2),
        minutes_from_sl1_to_tp2=(tp2-sl1 if sl1 is not None and tp2 is not None and tp2>sl1 else None),
        peak_before_sl1_pct=peak_before_sl,
        adverse_before_tp2_pct=risk_before_tp,
        initial_3m_mae_pct=max(adverse[:3]),initial_5m_mae_pct=max(adverse[:5]),
        initial_10m_mae_pct=max(adverse[:10]),initial_15m_mae_pct=max(adverse[:15]),
        initial_5m_mfe_pct=max(favorable[:5]),initial_10m_mfe_pct=max(favorable[:10]),
        initial_5m_close_pct=close_move[4],initial_10m_close_pct=close_move[9],
        first_1m_close_pct=close_move[0],
        entry_open=entry,
    )


def path_cohort_label(rec):
    if not rec['selected_4f']: return 'COMPARABLE_CONTROL' if rec['room_available'] else 'ROOM_MISSING'
    if rec['runner_ge2_from_entry']:
        if rec['result']=='TP_FIRST':return 'RUNNER_TP_FIRST'
        if rec['result']=='SL_FIRST':return 'RUNNER_SL_FIRST'
        if rec['result']=='AMBIGUOUS_SL_FIRST':return 'RUNNER_AMBIGUOUS'
        return 'RUNNER_NO_HIT'
    return 'SELECTED_NO_RUNNER'


def assemble_summary(records):
    groups=defaultdict(list)
    for r in records:
        if r['selected_4f']:
            groups['ALL_SELECTED'].append(r)
            groups[r['classification']].append(r)
            groups['SELECTED_'+r['signal']].append(r)
        elif r['room_available']:
            groups['COMPARABLE_CONTROL'].append(r)
    cols=['group','n','pct_of_4f','mfe_180m_med_pct','mae_180m_med_pct','first_sl1_med_min',
          'first_tp2_med_min','sl1_to_tp2_med_min','initial_5m_mae_med_pct',
          'initial_5m_close_med_pct','initial_10m_close_med_pct','departure_med_pct',
          'room_1h_med_pct','rank_volume_4h_med','rank_return_4h_med']
    result=[]
    for name in ('ALL_SELECTED','RUNNER_TP_FIRST','RUNNER_SL_FIRST','RUNNER_AMBIGUOUS',
                 'RUNNER_NO_HIT','SELECTED_NO_RUNNER','SELECTED_LONG','SELECTED_SHORT','COMPARABLE_CONTROL'):
        a=groups[name]
        if not a:continue
        med=lambda fld:pct_median(x.get(fld) for x in a)
        result.append(dict(group=name,n=len(a),pct_of_4f=round(100*len(a)/len(groups['ALL_SELECTED']),2) if name!='COMPARABLE_CONTROL' else '',
            mfe_180m_med_pct=med('mfe_180m_pct'),mae_180m_med_pct=med('mae_180m_pct'),
            first_sl1_med_min=med('first_sl_1_min'),first_tp2_med_min=med('first_tp_2_min'),
            sl1_to_tp2_med_min=med('minutes_from_sl1_to_tp2'),
            initial_5m_mae_med_pct=med('initial_5m_mae_pct'),
            initial_5m_close_med_pct=med('initial_5m_close_pct'),
            initial_10m_close_med_pct=med('initial_10m_close_pct'),
            departure_med_pct=med('max_departure_pct'),room_1h_med_pct=med('opposing_room_1h_pct'),
            rank_volume_4h_med=med('volume_rank_pct_4h'),rank_return_4h_med=med('return_rank_pct_4h')))
    return result,cols


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',default='/home/multiservices-1m')
    ap.add_argument('--csv',default=str(Path(__file__).with_name('runner_matrix_cohort_2026_10_14.csv')))
    ap.add_argument('--out',default='/root/runner_path_diagnostics_20261014')
    ap.add_argument('--prior',default='/root/runner_matrix_results_20261014/per_event_results.csv',
        help='Previously generated matrix details, if available, for TP2/SL1 parity check')
    a=ap.parse_args()
    project=Path(a.project).resolve();db=project/'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not db.is_file():ap.error(f'Cannot find auto_paths: {db}')
    sys.path.insert(0,str(project))
    from reaction_lab_auto_paths import decode_path
    from reaction_lab_deep_metrics import forward_metrics
    with open(a.csv,newline='',encoding='utf-8-sig') as f:records=list(csv.DictReader(f))
    if len(records)!=1311:ap.error('Frozen input expected 1311 COMPLETE rows')
    ids=set()
    for r in records:
        eid=r.get('event_key','')
        if eid in ids:ap.error(f'Duplicate {eid}')
        ids.add(eid)
        if r['path_state']!='COMPLETE':ap.error(f'Input is not complete: {eid}')
        if eid != f"{r['symbol']}|{r['signal']}|{int(r['retest_timestamp'])}":ap.error(f'Noncanonical ID {eid}')
        if int(r['known_ts'])!=int(r['retest_timestamp'])+MINUTE_MS:
            ap.error(f'Known timestamp does not align with entry: {eid}')
    with sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True) as c:
        source={}
        for off in range(0,len(records),300):
            keys=[r['event_key'] for r in records[off:off+300]]
            source.update({p[0]:p for p in c.execute('''SELECT event_key,symbol,side,retest_ts,entry_ts,state,count,path_blob
                FROM events WHERE event_key IN ('''+','.join('?'*len(keys))+')',keys)})
    prior={}
    if Path(a.prior).exists():
        with open(a.prior,newline='',encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r['scenario']=='TP2_SL1':prior[r['event_key']]=r
    diagnostics=[];issues=[];mismatch=[]
    for r in records:
        eid=r['event_key'];p=source.get(eid)
        if p is None:issues.append((eid,'NO_PATH'));continue
        key,sym,side,retest,entry_ts,state,count,blob=p
        if (sym!=r['symbol'] or side!=r['signal'] or int(retest)!=int(r['retest_timestamp'])
            or int(entry_ts)!=int(retest)+MINUTE_MS or state!='COMPLETE' or int(count)!=360 or not blob):
            issues.append((eid,'INVALID_PATH_METADATA'));continue
        try:
            path=decode_path(blob)
            if len(path)!=360:raise ValueError('not 360 candles')
            legacy=forward_metrics(path,side,num(r['retest_close']),int(retest))
            if legacy['valid_path_minutes']!=360:raise ValueError('non-contiguous or invalid OHLC')
            z=path_features(path,side)
        except Exception as exc:
            issues.append((eid,str(exc)));continue
        if legacy.get('tp_sl_2tp_1p0sl_180m')!=z['result']:
            mismatch.append((eid,'LEGACY_MISMATCH',legacy.get('tp_sl_2tp_1p0sl_180m'),z['result']))
        if num(r['mfe_180m_pct']) is not None and abs(num(r['mfe_180m_pct'])-z['mfe_180m_pct'])>0.000001:
            mismatch.append((eid,'MFE_MISMATCH',r['mfe_180m_pct'],z['mfe_180m_pct']))
        if eid in prior:
            if prior[eid]['result']!=z['result'] or int(prior[eid]['minutes'])!=z['exit_minute']:
                mismatch.append((eid,'PRIOR_AUDIT_MISMATCH',prior[eid]['result']+':'+prior[eid]['minutes'],z['result']+':'+str(z['exit_minute'])))
        item={**{k:r.get(k) for k in HEADER},**z}
        item['selected_4f']=selected(r)
        item['room_available']=num(r['opposing_room_1h_pct']) is not None
        item['classification']=path_cohort_label(item)
        diagnostics.append(item)
    out=Path(a.out);out.mkdir(exist_ok=True,parents=True)
    details_fields=[*HEADER,'selected_4f','room_available','classification','result','exit_minute',
        'entry_open','mfe_180m_pct','mae_180m_pct','runner_ge2_from_entry','runner_gt2_from_entry',
        'first_tp_0p25_min','first_tp_0p5_min','first_tp_1_min','first_tp_2_min','first_tp_3_min',
        'first_sl_0p5_min','first_sl_1_min','first_sl_1p5_min','first_sl_2_min',
        'sl1_then_tp2','tp2_and_sl1_same_min','minutes_from_sl1_to_tp2',
        'peak_before_sl1_pct','adverse_before_tp2_pct','initial_3m_mae_pct',
        'initial_5m_mae_pct','initial_10m_mae_pct','initial_15m_mae_pct',
        'initial_5m_mfe_pct','initial_10m_mfe_pct','initial_5m_close_pct','initial_10m_close_pct','first_1m_close_pct']
    # Remove duplicate mfe from merged source header while keeping calculated value.
    details_fields=list(dict.fromkeys(details_fields))
    write_csv(out/'all_events_diagnostics.csv',diagnostics,details_fields)
    write_csv(out/'selected_72_diagnostics.csv',[r for r in diagnostics if r['selected_4f']],details_fields)
    summary,fields=assemble_summary(diagnostics)
    write_csv(out/'diagnostic_summary.csv',summary,fields)
    write_csv(out/'issues.csv',[{'event_key':x,'issue':y} for x,y in issues],['event_key','issue'])
    write_csv(out/'mismatches.csv',
              [{'event_key':x,'issue':y,'stored':z,'computed':w} for x,y,z,w in mismatch],
              ['event_key','issue','stored','computed'])
    chosen=[r for r in diagnostics if r['selected_4f']]
    counts=Counter(r['classification'] for r in chosen)
    runner=[r for r in chosen if r['runner_ge2_from_entry']]
    later=sum(r['sl1_then_tp2'] for r in chosen)
    same=sum(r['tp2_and_sl1_same_min'] for r in chosen)
    md='''# TP/SL 2%/1% — Diagnóstico descriptivo de trayectorias

'''
    md+=f"* Paths auditados: {len(diagnostics)}/{len(records)}; errores de path: {len(issues)}; diferencias de fidelidad: {len(mismatch)}.\n"
    md+=f"* Cuatro factores: {len(chosen)} eventos; MFE desde entrada ≥2%: {len(runner)}.\n"
    md+=f"* Resultados: TP_FIRST={sum(r['result']=='TP_FIRST' for r in chosen)}, SL_FIRST={sum(r['result']=='SL_FIRST' for r in chosen)}, AMBIGUOUS={sum(r['result']=='AMBIGUOUS_SL_FIRST' for r in chosen)}, NO_HIT={sum(r['result']=='NO_HIT' for r in chosen)}.\n"
    md+=f"* Alcanza >2% **después** de haber tocado SL1% en vela anterior: {later}; doble cruce en la misma vela 1m: {same}.\n\n"
    md+='## Grupos de los 72\n\n'
    for group,n in sorted(counts.items()):md+=f'* {group}: {n}\n'
    md+='''\n## Interpretación\n
- `RUNNER_TP_FIRST`: +2% fue tocado antes del SL 1%; `RUNNER_SL_FIRST`: movimiento favorable ≥2% en 180m pero stop 1% primero.
- `first_tp_2_min` es el primer cruce estrictamente **>2%**; `runner_ge2_from_entry` es **≥2%**. Se informan ambos para no mezclar conceptos.
- Cambios en primeros 5m/10m son diagnósticos observados **después de la entrada**, NO factores disponibles al decidir en el OPEN inicial.
- Misma vela que cruza dos barreras: orden intrabar desconocido, se conserva la regla prudente `AMBIGUOUS_SL_FIRST`.
- El MFE posterior al stop NO equivale a beneficio realizable: la operación habría terminado; no se supone un reingreso retrospectivo.
- La cohorte usada para encontrar estos cuatro factores NO es holdout. No usar estas medianas para optimizar nuevos umbrales sin validación prospectiva.
- La simulación usa extremos OHLC 1m y barreras ideales. No incorpora spread/slippage, latencia ni posición simultánea (slots).
'''
    (out/'report.md').write_text(md,encoding='utf-8')
    print('PATHS:',len(diagnostics),'of',len(records),'issues=',len(issues))
    print('LEGACY + PRIOR PARITY MISMATCHES:',len(mismatch),'checked prior=',len(prior))
    print('4F:',len(chosen),'runner_ge2=',len(runner),'stopped first then TP2=',later,'same minute=',same)
    for group,n in sorted(counts.items()):print(' ',group,n)
    print('OUTPUT:',out)
    if len(diagnostics)!=1311 or len(chosen)!=72 or issues or mismatch:
        raise SystemExit('Audit incomplete or mismatch, inspect issues.csv and mismatches.csv')

if __name__=='__main__':main()
