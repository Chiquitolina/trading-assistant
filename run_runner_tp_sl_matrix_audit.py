#!/usr/bin/env python3
"""Read-only 1m OHLC TP/SL audit for frozen Continuous V1 CSV cohort.

- Reads *only* project/reports/reaction_lab/auto_paths_v1.sqlite3 via mode=ro.
- Uses existing decode_path and forward_metrics, for strict legacy parity.
- Does NOT write any SQLite or modify services.
- CSV input was frozen from the 2026-10-14 Runner Analyzer export.
- TP legacy crossing > TP, SL >= SL; same-bar ambiguity counted conservatively as SL.
- Outputs single-signal analysis, NOT a one-slot portfolio or guaranteed fills.
"""
from __future__ import annotations
import argparse
import csv
import math
from pathlib import Path
import sqlite3
import sys
from collections import Counter, defaultdict

M = 60_000
SCENARIOS = [(2.0, 0.5), (2.0, 1.0), (2.0, 1.5),
             (3.0, 1.0), (3.0, 1.5), (3.0, 2.0)]
LEGACY = {(1.,.5):'tp_sl_1tp_0p5sl_180m',
          (2.,1.):'tp_sl_2tp_1p0sl_180m',
          (3.,1.):'tp_sl_3tp_1p0sl_180m',
          (5.,2.):'tp_sl_5tp_2p0sl_180m'}


def number(s):
    try:
        v=float(s)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError,OverflowError):
        return None


def selected(row):
    return (number(row['max_departure_pct']) is not None
        and number(row['max_departure_pct'])>=1.5
        and number(row['opposing_room_1h_pct']) is not None
        and number(row['opposing_room_1h_pct'])>=2.0
        and number(row['volume_rank_pct_4h']) is not None
        and number(row['volume_rank_pct_4h'])>=60.0
        and number(row['return_rank_pct_4h']) is not None
        and number(row['return_rank_pct_4h'])>=60.0)


def scenario_name(tp,sl):
    return f'TP{tp:g}_SL{sl:g}'


def simulate_legacy_semantics(path, side, tp, sl, horizon=180, fees=0.10):
    """Contract: next candle OPEN entry, full contiguous 1m candles validated upstream.

    Mirror deep_metrics.forward_metrics precisely: gains > TP; drawdown >= SL;
    simultaneous intrabar crossing = AMBIGUOUS_SL_FIRST. All returns in pct
    of unleveraged notional; stop/slippage exact barrier idealization.
    """
    entry=float(path[0][1])
    sign=1 if side=='LONG' else -1
    result='NO_HIT'
    minute=horizon
    for i, (_, op, hi, lo, close) in enumerate(path[:horizon],start=1):
        gain=100*(hi/entry-1) if sign==1 else 100*(1-lo/entry)
        adverse=100*(1-lo/entry) if sign==1 else 100*(hi/entry-1)
        if gain>tp and adverse>=sl:
            result='AMBIGUOUS_SL_FIRST'; minute=i;break
        if adverse>=sl:
            result='SL_FIRST';minute=i;break
        if gain>tp:
            result='TP_FIRST';minute=i;break
    if result=='TP_FIRST':gross=tp
    elif result in ('SL_FIRST','AMBIGUOUS_SL_FIRST'):gross=-sl
    else:
        gross=100*sign*(float(path[horizon-1][4])/entry-1)
    return {'result':result, 'minutes':minute, 'gross_pct':gross, 'net_pct':gross-fees}


def write_csv(path, rows, fields):
    with path.open('w',newline='',encoding='utf-8') as f:
        wr=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        wr.writeheader()
        wr.writerows(rows)


def summary(rows):
    n=len(rows)
    outcomes=Counter(r['result'] for r in rows)
    nets=[float(r['net_pct']) for r in rows]
    profit=sum(x for x in nets if x>0)
    loss=-sum(x for x in nets if x<0)
    result={'n':n,'tp_first':outcomes['TP_FIRST'],
            'sl_first':outcomes['SL_FIRST'],
            'ambiguous_sl_first':outcomes['AMBIGUOUS_SL_FIRST'],
            'no_hit':outcomes['NO_HIT'],
            'tp_first_pct':round(100*outcomes['TP_FIRST']/n,3) if n else None,
            'avg_net_pct':round(sum(nets)/n,5) if n else None,
            'sum_net_pct_no_compounding':round(sum(nets),4),
            'profit_factor':round(profit/loss,5) if loss else ('INF' if profit else None)}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',default='.',help='VPS project root')
    p.add_argument('--csv',default=str(Path(__file__).with_name('runner_matrix_cohort_2026_10_14.csv')))
    p.add_argument('--out',default='runner_matrix_results_2026_10_14')
    p.add_argument('--fees',type=float,default=0.10,help='round-trip fee %% of notional, default 0.10')
    args=p.parse_args()
    project=Path(args.project).resolve()
    target_db=project/'reports/reaction_lab/auto_paths_v1.sqlite3'
    if not target_db.is_file():p.error(f'No archived paths found at: {target_db}')
    if args.fees<0:p.error('fees must be nonnegative')
    sys.path.insert(0,str(project))
    from reaction_lab_auto_paths import decode_path
    from reaction_lab_deep_metrics import forward_metrics
    
    with open(args.csv,newline='',encoding='utf-8-sig') as f:
        records=list(csv.DictReader(f))
    if not records:p.error('Empty input CSV')
    seen=set()
    for row in records:
        key=row['event_key']
        if key in seen:raise RuntimeError(f'Duplicate Event ID {key}')
        seen.add(key)
        if row['path_state']!='COMPLETE':raise RuntimeError(f'Input is not COMPLETE: {key}')
        if key!=f"{row['symbol']}|{row['signal']}|{int(row['retest_timestamp'])}":
            raise RuntimeError(f'Noncanonical Event ID: {key}')

    index={r['event_key']:r for r in records}
    source_rows={}
    uri='file:'+target_db.as_posix()+'?mode=ro'
    with sqlite3.connect(uri,uri=True,timeout=30) as conn:
        for start in range(0,len(records),300):
            chunk=[r['event_key'] for r in records[start:start+300]]
            placeholders=','.join('?'*len(chunk))
            for r in conn.execute(f'''SELECT event_key,symbol,side,retest_ts,entry_ts,state,count,path_blob
                        FROM events WHERE event_key IN ({placeholders})''',chunk):
                source_rows[r[0]]=r

    details=[]; problems=[]; comparisons=[]; full_count=0
    bygroup=defaultdict(list)
    for row in records:
        key=row['event_key']
        src=source_rows.get(key)
        if src is None:
            problems.append({'event_key':key,'issue':'PATH_ROW_MISSING'})
            continue
        _,symbol,side,retest,entry_ts,state,count,blob=src
        if (symbol!=row['symbol'] or side!=row['signal'] or
            int(retest)!=int(row['retest_timestamp']) or int(entry_ts)!=int(retest)+M):
            problems.append({'event_key':key,'issue':'PATH_METADATA_MISMATCH','actual_state':state,'count':count})
            continue
        if state!='COMPLETE' or int(count)!=360 or not blob:
            problems.append({'event_key':key,'issue':'PATH_NOT_COMPLETE_360','actual_state':state,'count':count})
            continue
        try:
            path=decode_path(blob)
            if len(path)!=int(count):raise ValueError('Decoded bars/count mismatch')
            legacy=forward_metrics(path,side,number(row['retest_close']),int(retest))
            if legacy.get('valid_path_minutes')!=360:
                raise ValueError(f"Only {legacy.get('valid_path_minutes')} valid contiguous minutes")
            entry=float(path[0][1])
        except Exception as ex:
            problems.append({'event_key':key,'issue':'INVALID_PATH','detail':repr(ex)})
            continue
        full_count+=1
        supplied_entry=number(row['entry_price'])
        if supplied_entry is not None and not math.isclose(entry,supplied_entry,rel_tol=1e-9,abs_tol=1e-12):
            problems.append({'event_key':key,'issue':'ENTRY_PRICE_DIFFERENCE',
                             'expected':supplied_entry,'actual':entry})
        mfecsv=number(row['mfe_180m_pct'])
        mfecalc=number(legacy.get('mfe_entry_180m_pct'))
        if mfecsv is not None and mfecalc is not None and abs(mfecsv-mfecalc)>1e-6:
            problems.append({'event_key':key,'issue':'MFE_ENTRY_DIFFERENCE',
                             'expected':mfecsv,'actual':mfecalc})
        for scn, col in LEGACY.items():
            stored=row.get(col,'')
            if stored and stored!='nan':
                actual=legacy.get(col)
                comparisons.append({'event_key':key,'scenario':col,'stored':stored,'recomputed':actual,'match':stored==actual})
                if stored!=actual:
                    problems.append({'event_key':key,'issue':'LEGACY_TP_SL_MISMATCH',
                                     'scenario':col,'expected':stored,'actual':actual})
        group='4_FACTORS' if selected(row) else ('REST_ROOM_AVAILABLE' if number(row['opposing_room_1h_pct']) is not None else 'ROOM_MISSING')
        for tp,sl in SCENARIOS:
            v=simulate_legacy_semantics(path,side,tp,sl,fees=args.fees)
            field=LEGACY.get((tp,sl))
            if field and v['result']!=legacy.get(field):
                problems.append({'event_key':key,'issue':'MATRIX_VS_EXISTING_ENGINE_MISMATCH',
                                 'scenario':field,'expected':legacy.get(field),'actual':v['result']})
            result={'event_key':key,'group':group,'side':side,
                    'retest_ts':retest,'entry_ts':int(entry_ts),
                    'entry_open':entry,'scenario':scenario_name(tp,sl),
                    'tp_pct':tp,'sl_pct':sl,**v}
            details.append(result)
            for label in (group,'ALL_COMPLETE',group+'_'+side):
                bygroup[(label,result['scenario'])].append(result)
    summary_rows=[]
    for group in ('ALL_COMPLETE','4_FACTORS','REST_ROOM_AVAILABLE','ROOM_MISSING','4_FACTORS_LONG','4_FACTORS_SHORT','REST_ROOM_AVAILABLE_LONG','REST_ROOM_AVAILABLE_SHORT'):
        for tp,sl in SCENARIOS:
            scn=scenario_name(tp,sl)
            if (group,scn) not in bygroup:continue
            summary_rows.append({'group':group,'scenario':scn,'tp_pct':tp,'sl_pct':sl,
                                 **summary(bygroup[(group,scn)])})
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=True)
    write_csv(out/'matrix_summary.csv',summary_rows,
              ['group','scenario','tp_pct','sl_pct','n','tp_first','sl_first','ambiguous_sl_first','no_hit','tp_first_pct','avg_net_pct','sum_net_pct_no_compounding','profit_factor'])
    write_csv(out/'per_event_results.csv',details,
              ['event_key','group','side','retest_ts','entry_ts','entry_open','scenario','tp_pct','sl_pct','result','minutes','gross_pct','net_pct'])
    write_csv(out/'parity_comparisons.csv',comparisons,['event_key','scenario','stored','recomputed','match'])
    write_csv(out/'issues.csv',problems,['event_key','issue','detail','actual_state','count','expected','actual','scenario'])
    bad=sum(not r['match'] for r in comparisons)
    print(f'INPUT: {len(records)} COMPLETE records, 4-factor CSV selection: {sum(selected(x) for x in records)}')
    print(f'ARCHIVE: {full_count} validated 360-minute paths, {len(records)-full_count} missing/invalid')
    print(f'LEGACY PARITY: {len(comparisons)} stored TP/SL outcomes checked, {bad} mismatches')
    print(f'ISSUES: {len(problems)} diagnostic records')
    for r in summary_rows:
        if r['group'] in ('4_FACTORS','REST_ROOM_AVAILABLE'):
            print(f"{r['group']:21s} {r['scenario']:12s} n={r['n']:4d} TP={r['tp_first']:3d} SL={r['sl_first']:3d} "+
                  f"amb={r['ambiguous_sl_first']:3d} NO_HIT={r['no_hit']:3d} "+
                  f"net_avg%={r['avg_net_pct']} PF={r['profit_factor']}")
    print('OUTPUT:',out)
    if bad:raise SystemExit('WARNING: legacy TP/SL parity mismatches. Review issues.csv')

if __name__=='__main__':main()
