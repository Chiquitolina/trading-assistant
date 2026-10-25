#!/usr/bin/env python3
"""Read-only, causal FAILED entry-timing research against Continuous V1 archives.

No reads from Binance, Redis or trading accounts. Source SQLite files are opened
read-only. --run writes ONLY new CSVs/JSON under its own isolated report folder.

All strategies compete on the SAME eligible first-touch cohort per holding horizon.
Signals are determined from closed bars and orders enter at the NEXT 1m OPEN.
Any strategy that does not trigger within max_wait is NO_TRADE (zero exposure).
No intrabar ordering assumptions: TP+SL hit in one candle => SL_AMBIGUOUS.
Results are exploratory, not an out-of-sample strategy certification.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import statistics
import struct
import zlib

MINUTE = 60_000
BAR = struct.Struct('<qdddd')
MODES = (
    'IMMEDIATE_ORIGINAL',
    'IMMEDIATE_INVERSE',
    'INVERSE_2_CLOSES',
    'INVERSE_BREAK_FAIL_EXTREME',
    'INVERSE_RETEST_REJECTION',
    'ORIGINAL_RECLAIM',
    'ORIGINAL_RECLAIM_2_CLOSES',
    'ORIGINAL_BREAK_FAIL_EXTREME',
)
CONFIGS = ((60, 1.0, 1.0), (60, 2.0, 1.5), (120, 1.0, 1.0), (120, 2.0, 1.5))
FIELDNAMES = ('touch_key','symbol','original_side','observation_type','retest_ts','status',
              'path_state','bars_saved','mode','hold_minutes','tp_pct','sl_pct',
              'entry_min_after_fail','entry_ts','entry_open','direction','post_entry_mfe_pct','post_entry_mae_pct','outcome',
              'exit_min','exit_ts','gross_pct','net_pct')


def read_only(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=20)
    conn.execute('PRAGMA query_only=ON')
    return conn


def decode(blob):
    if not blob:
        return []
    raw = zlib.decompress(blob)
    if len(raw) % BAR.size:
        raise ValueError('Corrupt path blob byte length')
    return [BAR.unpack_from(raw, i) for i in range(0, len(raw), BAR.size)]


def number(value):
    try:
        n = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def inverse(side):
    return 'SHORT' if side == 'LONG' else 'LONG'


def outside(close, level, side, buffer_pct=0.05):
    """Whether a close is outside swing in the *opposite* side direction."""
    b = buffer_pct / 100
    return close < level*(1-b) if side == 'LONG' else close > level*(1+b)


def reclaimed(close, level, side, buffer_pct=0.05):
    b = buffer_pct / 100
    return close > level*(1+b) if side == 'LONG' else close < level*(1-b)


def find_entry(mode, bars, row, max_wait):
    """Returns (index of next bar's OPEN, trading direction), or no trigger.

    bars[0] is the first minute AFTER failed retest. The failed retest candle's
    OHLC and swing level are the only data known at the FAIL close.
    """
    original = row['side']
    if original not in ('LONG','SHORT'):
        raise ValueError('Unknown side')
    if mode == 'IMMEDIATE_ORIGINAL':
        return (0, original)
    if mode == 'IMMEDIATE_INVERSE':
        return (0, inverse(original))
    meta = row['meta']
    level = number(meta.get('swing_price'))
    if not level:
        return None
    fail_high = number(meta.get('retest_high'))
    fail_low = number(meta.get('retest_low'))
    # Need a fully CLOSED post-FAIL candle to decide; next candle OPEN fills.
    limit = min(max_wait, len(bars)-1)
    extended = False
    for j in range(limit):
        close = bars[j][4]
        prev_close = bars[j-1][4] if j else None
        if mode == 'INVERSE_2_CLOSES':
            if j >= 1 and outside(prev_close,level,original) and outside(close,level,original):
                if (close <= prev_close*(1-0.0005) if original=='LONG' else
                    close >= prev_close*(1+0.0005)):
                    return (j+1,inverse(original))
        elif mode == 'INVERSE_BREAK_FAIL_EXTREME':
            if fail_high and fail_low and (
                close < fail_low*(1-0.0005) if original=='LONG'
                else close > fail_high*(1+0.0005)
            ):
                return (j+1,inverse(original))
        elif mode == 'INVERSE_RETEST_REJECTION':
            # Must first see a CLOSED 1m candle extended 0.20% past swing,
            # then on a LATER candle test the level from the broken side
            # and reject it (close outside by 0.05%).
            if extended:
                if original == 'LONG':
                    if bars[j][2] >= level*(1-0.001) and outside(close,level,original):
                        return (j+1,'SHORT')
                else:
                    if bars[j][3] <= level*(1+0.001) and outside(close,level,original):
                        return (j+1,'LONG')
            if (close <= level*(1-0.002) if original=='LONG' else
                close >= level*(1+0.002)):
                extended = True
        elif mode == 'ORIGINAL_RECLAIM':
            if reclaimed(close,level,original):
                return (j+1,original)
        elif mode == 'ORIGINAL_RECLAIM_2_CLOSES':
            if j>=1 and reclaimed(prev_close,level,original) and reclaimed(close,level,original):
                return (j+1,original)
        elif mode == 'ORIGINAL_BREAK_FAIL_EXTREME':
            if fail_high and fail_low and (
                close > fail_high*(1+0.0005) if original=='LONG'
                else close < fail_low*(1-0.0005)
            ):
                return (j+1,original)
        else:
            raise ValueError('Unsupported entry mode '+mode)
    return None


def post_entry_excursions(bars,entry_ix,side,hold):
    ref=bars[entry_ix][1]
    window=bars[entry_ix:entry_ix+hold]
    high=max(bar[2] for bar in window)
    low=min(bar[3] for bar in window)
    if side=='LONG':
        return max(0,(high/ref-1)*100),max(0,(1-low/ref)*100)
    return max(0,(1-low/ref)*100),max(0,(high/ref-1)*100)


def execute(bars,entry_ix,side,tp,sl,hold):
    """One future open entry, chronological 1m TP/SL, conservative ties."""
    if entry_ix+hold>len(bars):
        raise ValueError('Missing complete holding horizon')
    ref = bars[entry_ix][1]
    if side == 'LONG':
        target=ref*(1+tp/100); stop=ref*(1-sl/100)
    else:
        target=ref*(1-tp/100); stop=ref*(1+sl/100)
    for i in range(entry_ix,entry_ix+hold):
        _, op, hi, lo, cl = bars[i]
        # Conservative stop fill for open-through-stop gaps in later candles.
        if i>entry_ix and (op<=stop if side=='LONG' else op>=stop):
            netgross=(op/ref-1)*100 if side=='LONG' else (1-op/ref)*100
            return 'SL_GAP',i-entry_ix+1,min(-sl,netgross)
        if i>entry_ix and (op>=target if side=='LONG' else op<=target):
            return 'TP_GAP',i-entry_ix+1,tp
        tp_hit=hi>=target if side=='LONG' else lo<=target
        sl_hit=lo<=stop if side=='LONG' else hi>=stop
        if sl_hit and tp_hit:return 'SL_AMBIGUOUS',i-entry_ix+1,-sl
        if sl_hit:return 'SL_FIRST',i-entry_ix+1,-sl
        if tp_hit:return 'TP_FIRST',i-entry_ix+1,tp
    end = bars[entry_ix+hold-1][4]
    gross=(end/ref-1)*100 if side=='LONG' else (1-end/ref)*100
    return 'TIME_EXIT',hold,gross


def collect(journal_db,paths_db):
    with closing(read_only(journal_db)) as journal:
        journal_rows=journal.execute('''SELECT touch_key,symbol,side,observation_type,retest_ts,causal_json
             FROM first_touches WHERE status='TOUCH_FAILED' ORDER BY retest_ts,touch_key''').fetchall()
    with closing(read_only(paths_db)) as paths:
        saved=paths.execute('''SELECT touch_key,status,state,count,path_blob,entry_ts
             FROM paths WHERE status='TOUCH_FAILED' ''').fetchall()
    saved_by_key={x[0]:x for x in saved}
    output=[]
    for key,symbol,side,origin,ts,json_str in journal_rows:
        meta=json.loads(json_str)
        if not isinstance(meta,dict):raise ValueError('Invalid causal payload '+key)
        stored=saved_by_key.get(key)
        if stored:
            _,status,state,count,blob,entry_ts=stored
            bars=decode(blob)
            if len(bars)!=count or any(b[0]!=int(entry_ts)+i*MINUTE for i,b in enumerate(bars)):
                raise ValueError('Non-contiguous path: '+key)
            if int(entry_ts)!=int(ts)+MINUTE:
                raise ValueError('Non-causal entry minute: '+key)
        else:
            state='NOT_CAPTURED'; bars=[]
        output.append(dict(key=key,symbol=symbol,side=side,origin=origin,ts=int(ts),meta=meta,state=state,bars=bars))
    return output


def simulate_cohort(events, hold=60, tp=1.0, sl=1.0, max_wait=15, cost=0.2):
    if hold<=0 or max_wait<=0 or min(tp,sl)<=0 or cost<0:
        raise ValueError('Invalid config')
    eligible=[e for e in events if len(e['bars']) >= max_wait+hold]
    rows=[]
    for ev in eligible:
        b=ev['bars']
        for mode in MODES:
            trigger=find_entry(mode,b,ev,max_wait)
            line={
                'touch_key':ev['key'],'symbol':ev['symbol'],'original_side':ev['side'],
                'observation_type':ev['origin'],'retest_ts':ev['ts'],
                'status':'TOUCH_FAILED','path_state':ev['state'],'bars_saved':len(b),
                'mode':mode,'hold_minutes':hold,'tp_pct':tp,'sl_pct':sl,
                'entry_min_after_fail':None,'entry_ts':None,'entry_open':None,
                'direction':None,'post_entry_mfe_pct':None,'post_entry_mae_pct':None,
                'outcome':'NO_TRIGGER','exit_min':None,'exit_ts':None,
                'gross_pct':None,'net_pct':None}
            if trigger is not None:
                i,side=trigger
                outcome,exit_min,gross=execute(b,i,side,tp,sl,hold)
                mfe,mae=post_entry_excursions(b,i,side,hold)
                line.update(post_entry_mfe_pct=round(mfe,5),post_entry_mae_pct=round(mae,5),
                            entry_min_after_fail=i,entry_ts=b[i][0],entry_open=b[i][1],
                            direction=side,outcome=outcome,exit_min=exit_min,
                            exit_ts=b[i+exit_min-1][0],gross_pct=gross,net_pct=gross-cost)
            rows.append(line)
    return eligible,rows


def summary_for(rows,eligible_n):
    trades=[r for r in rows if r['outcome']!='NO_TRIGGER']
    nets=[r['net_pct'] for r in trades]
    gain=sum(max(0,x) for x in nets);loss=-sum(min(0,x) for x in nets)
    nonzero=len(trades)
    def rnd(x):return round(x,5) if x is not None and math.isfinite(x) else None
    return dict(eligible=eligible_n,triggered=nonzero,no_trigger=eligible_n-nonzero,
        trigger_pct=rnd(100*nonzero/eligible_n) if eligible_n else None,
        net_mean_traded_pct=rnd(statistics.mean(nets)) if nets else None,
        net_median_traded_pct=rnd(statistics.median(nets)) if nets else None,
        net_mean_per_eligible_pct=rnd(sum(nets)/eligible_n) if eligible_n else None,
        gross_mean_traded_pct=rnd(statistics.mean([r['gross_pct'] for r in trades])) if trades else None,
        profit_factor=rnd(gain/loss) if loss else (None if not gain else 'INF'),
        positive_net_pct=rnd(100*sum(n>0 for n in nets)/nonzero) if nonzero else None,
        median_entry_delay_min=rnd(statistics.median([r['entry_min_after_fail'] for r in trades])) if trades else None,
        median_post_entry_mfe_pct=rnd(statistics.median([r['post_entry_mfe_pct'] for r in trades])) if trades else None,
        median_post_entry_mae_pct=rnd(statistics.median([r['post_entry_mae_pct'] for r in trades])) if trades else None,
        outcome_counts=json.dumps(dict(Counter(r['outcome'] for r in rows)),ensure_ascii=False,sort_keys=True))


def portfolio(rows, capital=200.,fraction=0.8):
    """One shared slot; deterministic sorting. Event overlap causes SKIP, not duplicate exposure."""
    trades=sorted((r for r in rows if r['outcome']!='NO_TRIGGER'),key=lambda r:(r['entry_ts'],r['touch_key']))
    equity=peak=capital;busy_until=-1;chosen=0;skipped=0;max_dd=0
    for r in trades:
        if r['entry_ts']<=busy_until:
            skipped+=1
            continue
        chosen+=1
        equity=equity*(1+fraction*r['net_pct']/100)
        peak=max(peak,equity)
        max_dd=max(max_dd,(1-equity/peak)*100)
        busy_until=r['exit_ts']+MINUTE-1
    return {'slot_trades':chosen,'slot_skipped':skipped,'initial_usd':capital,
            'final_usd':round(equity,5),'return_pct':round((equity/capital-1)*100,5),
            'max_dd_realized_pct':round(max_dd,5)}


def report(events,max_wait=15,cost=0.2):
    all_rows=[];summary=[];breakdowns=[]
    for hold,tp,sl in CONFIGS:
        eligible,rows=simulate_cohort(events,hold,tp,sl,max_wait,cost)
        middle=(sorted(e['ts'] for e in eligible)[len(eligible)//2] if eligible else None)
        all_rows.extend(rows)
        for mode in MODES:
            subset=[r for r in rows if r['mode']==mode]
            stats=summary_for(subset,len(eligible))
            port=portfolio(subset)
            summary.append(dict(mode=mode,hold_minutes=hold,tp_pct=tp,sl_pct=sl,**stats,**port))
            # Each slice is assessed on the same cohort, including nontriggering events.
            for dim in ('original_side','observation_type','chronology'):
                groups=defaultdict(list)
                for r in subset:
                    val=('EARLY' if r['retest_ts']<=middle else 'LATE') if dim=='chronology' else r[dim]
                    groups[val].append(r)
                for val,subset2 in sorted(groups.items()):
                    breakdowns.append(dict(mode=mode,hold_minutes=hold,tp_pct=tp,sl_pct=sl,
                        dimension=dim,group=val,**summary_for(subset2,len(subset2))))
    coverage={str(h):sum(len(e['bars'])>=max_wait+h for e in events) for h in sorted({c[0] for c in CONFIGS})}
    notes=[
      'Exploratory within previously inspected historical data; not untouched OOS.',
      'Same eligible cohort for each mode per hold: at least hold+max_wait contiguous CLOSED 1m bars.',
      'All signals use post-fail CLOSED 1m candles. Orders fill at NEXT 1m OPEN, except immediate entry at first post-fail OPEN.',
      'No trigger -> NO_TRADE, counted in eligible but not charged fees or traded expectancy.',
      'Costs fixed RT in percentage points; no volume-dependent slippage, funding or exchange filters.',
      'Portfolio one slot uses entry timestamp priority then touch key. 80pct of realized equity at 1x, no borrowing.',
      'TP+SL in the same 1m -> conservative SL_AMBIGUOUS. Open-through-stop -> gap fill at worse open.',
      'Earliest visible trade cannot prove execution speed of real WS and order routing.',
    ]
    return {'source_events':len(events),'event_coverage_by_hold':coverage,
            'max_wait_min':max_wait,'cost_roundtrip_pct':cost,'configurations':CONFIGS,
            'notes':notes},all_rows,summary,breakdowns


def write_csv(path,rows,columns):
    with Path(path).open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--journal-db',type=Path,default=None)
    ap.add_argument('--paths-db',type=Path,default=None)
    ap.add_argument('--out',type=Path,default=None)
    ap.add_argument('--max-wait',type=int,default=15)
    ap.add_argument('--cost-roundtrip-pct',type=float,default=0.20)
    mode=ap.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--run',action='store_true')
    args=ap.parse_args(argv)
    project=args.project.resolve()
    journal=args.journal_db or project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
    paths=args.paths_db or project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
    out=args.out or project/'reports/reaction_lab/failed_entry_timing_v1'
    if args.max_wait<2 or args.max_wait>60:ap.error('--max-wait must be 2..60')
    if not (0 <= args.cost_roundtrip_pct<=5):ap.error('Invalid cost')
    for name,path in [('journal',journal),('paths',paths)]:
        if not Path(path).is_file():ap.error(f'Missing {name} SQLite DB: {path}')
    events=collect(journal,paths)
    coverage={h:sum(len(x['bars'])>=args.max_wait+h for x in events) for h in (60,120)}
    print('FAILED_TIMING_PREFLIGHT',json.dumps({'events':len(events),'evaluable':coverage,
                'read_only_input':True,'journal':str(journal),'paths':str(paths)},ensure_ascii=False))
    if not args.run:
        print('CHECK OK: no files modified; run with --run to write isolated report')
        return 0
    if Path(out).resolve() in (Path(journal).resolve(),Path(paths).resolve()):
        ap.error('Output collides with source database')
    info,rows,summaries,breakdowns=report(events,args.max_wait,args.cost_roundtrip_pct)
    Path(out).mkdir(parents=True,exist_ok=True)
    write_csv(Path(out)/'event_outcomes.csv',rows,FIELDNAMES)
    write_csv(Path(out)/'summary.csv',summaries,list(summaries[0]) if summaries else ['mode'])
    write_csv(Path(out)/'breakdowns.csv',breakdowns,list(breakdowns[0]) if breakdowns else ['mode'])
    info['created_at_utc']=datetime.now(timezone.utc).isoformat()
    (Path(out)/'report.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    print('FAILED_TIMING_REPORT',json.dumps(info,ensure_ascii=False))
    print('FAILED_TIMING_SUMMARY_HEAD')
    for r in summaries:
        if r['hold_minutes']==60:
            print(r['mode'],f"TP{r['tp_pct']}/SL{r['sl_pct']}",f"eligible={r['eligible']}",
                  f"traded={r['triggered']}",f"mean_net={r['net_mean_traded_pct']}",
                  f"PF={r['profit_factor']}",f"slot_return={r['return_pct']}")
    print('FAILED_TIMING_REPORT_PATH',Path(out).resolve())
    return 0

if __name__=='__main__':
    raise SystemExit(main())
