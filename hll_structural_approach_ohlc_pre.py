#!/usr/bin/env python3
"""HLL Structural Approach PRE V2: read-only OHLC 30m path descriptors.

No classifications, thresholds, entries, LIVE or dashboard writes. Python stdlib.
The timestamp contract is fail-closed. PRE is the last *fully closed* 30m
candle boundary; a candle with open==PRE is never visible to the calculation.
"""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BAR_MS = 30 * 60 * 1000
DEFAULT_SOURCE = Path('reports/horizontal_level_second_ascent_pre/core15_structural_approach_pre_v1')
DEFAULT_CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
DEFAULT_OUTPUT = Path('reports/horizontal_level_second_ascent_pre/core15_structural_approach_ohlc_pre_v2')
WINDOWS = (12, 24, 48, 96)
PRICE_FIELDS = {
    'open': ('open','o','open_price'),
    'high': ('high','h','high_price'),
    'low': ('low','l','low_price'),
    'close': ('close','c','close_price'),
}
OPEN_FIELDS = ('open_time','open_ts','open_timestamp','candle_open_time','kline_open_time','start_time','start_ts','timestamp_open')
CLOSE_FIELDS = ('close_time','close_ts','close_timestamp','candle_close_time','kline_close_time','end_time','end_ts','timestamp_close')
AMBIGUOUS_FIELDS = ('timestamp','ts','time','datetime','date','t')
OUT_FIELDS = (
    'attempt_id','symbol','pre_closed_ts','window_bars','window_start_ts',
    'status','bars_found','first_candle_open_ts','last_candle_close_ts',
    'net_close_return_pct','close_path_efficiency','signed_upward_efficiency',
    'positive_close_steps_share','new_close_highs_share','consecutive_range_overlap_mean',
    'close_max_drawdown_pct','close_max_drawup_pct','window_high_low_range_pct',
    'pre_close_position_in_range','pre_close_vs_window_low_pct',
    'recovery_from_window_close_low_pct','latest_h2_distance_zone_atr_1atr',
)
OVERVIEW_FIELDS = ('attempt_id','symbol','pre_closed_ts','status_12','status_24','status_48','status_96',
                   'efficiency_24','efficiency_48','overlap_24','overlap_48','net_return_24_pct','net_return_48_pct',
                   'latest_h2_distance_zone_atr_1atr')


def read_csv(path):
    with path.open('r',newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def filehash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def parse_time(value):
    """UTC epoch seconds/ms or ISO-8601. Naive ISO interpreted as UTC."""
    v=str(value).strip()
    if not v: raise ValueError('Empty timestamp')
    try:
        f=float(v)
        if not math.isfinite(f):raise ValueError('Nonfinite timestamp')
        if 1e9 <= f < 1e11: f*=1000
        elif not 1e12 <= f < 2e12: raise ValueError('Unexpected epoch unit or range: '+v)
        ms=int(round(f))
    except ValueError:
        try:
            dt=datetime.fromisoformat(v.replace('Z','+00:00'))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            ms=int(round(dt.timestamp()*1000))
        except ValueError as e:
            raise ValueError('Invalid UTC timestamp '+repr(v)) from e
    if not (1577836800000 <= ms <= 2082758400000):
        raise ValueError('Timestamp outside supported 2020-2035 UTC range: '+v)
    return ms


def pick_col(cols, candidates):
    by_lower={name.strip().lower():name for name in cols}
    return next((by_lower[c] for c in candidates if c in by_lower),None)


def schema_for(cols, timestamp_kind=None):
    prices={k:pick_col(cols,v) for k,v in PRICE_FIELDS.items()}
    missing=[k for k,v in prices.items() if v is None]
    if missing:raise ValueError('OHLC columns absent: '+','.join(missing)+'; present='+str(cols))
    ts_open=pick_col(cols,OPEN_FIELDS)
    ts_close=pick_col(cols,CLOSE_FIELDS)
    generic=pick_col(cols,AMBIGUOUS_FIELDS)
    if timestamp_kind not in (None,'open','close'):
        raise ValueError('timestamp_kind must be open or close')
    if ts_open and ts_close:kind='open';ts_col=ts_open
    elif ts_open:kind='open';ts_col=ts_open
    elif ts_close:kind='close';ts_col=ts_close
    elif generic and timestamp_kind:kind=timestamp_kind;ts_col=generic
    elif generic:raise ValueError('Ambiguous timestamp '+repr(generic)+'. Rerun with --timestamp-kind open|close based on the CSV semantics')
    else:raise ValueError('No recognized candle timestamp column: '+str(cols))
    return {'time_col':ts_col,'time_kind':kind,'other_close_col':ts_close if ts_open and ts_close else None,
            'other_open_col':ts_open if ts_open and ts_close else None,'prices':prices}


def decode_candle(row,schema):
    t=parse_time(row[schema['time_col']]); kind=schema['time_kind']
    if kind=='open':
        open_ts=t; close_ts=t+BAR_MS
        # Secondary time field validates unit/synchronization when present.
        if schema['other_close_col'] is not None:
            raw_close=parse_time(row[schema['other_close_col']])
            if raw_close not in (close_ts, close_ts-1):
                raise ValueError('Open/close timestamp disagreement: '+str((open_ts,raw_close)))
    else:
        close_ts=t
        # Binance close-time is often end-1ms; normalize to next boundary.
        if t%BAR_MS==BAR_MS-1:close_ts=t+1
        open_ts=close_ts-BAR_MS
    if open_ts%BAR_MS != 0 or close_ts-open_ts!=BAR_MS:
        raise ValueError('Non-aligned 30m candle: '+str((open_ts,close_ts)))
    vals={key:float(row[col]) for key,col in schema['prices'].items()}
    if not all(math.isfinite(v) and v>0 for v in vals.values()):
        raise ValueError('Non-positive/nonfinite OHLC')
    if vals['low'] > min(vals['open'],vals['close']) or vals['high'] < max(vals['open'],vals['close']) or vals['low']>vals['high']:
        raise ValueError('Inconsistent OHLC envelope')
    return {'open_ts':open_ts,'close_ts':close_ts,**vals}


def load_candles(path,timestamp_kind=None):
    with path.open('r',newline='',encoding='utf-8-sig') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames:raise ValueError('CSV has no header')
        schema=schema_for(reader.fieldnames,timestamp_kind)
        candles={}
        for line,row in enumerate(reader,2):
            try:c=decode_candle(row,schema)
            except (ValueError,TypeError,KeyError) as e:
                raise ValueError(f'{path}:{line}: {e}') from e
            key=c['open_ts']
            if key in candles:raise ValueError(f'Duplicate open_ts {key} in {path}')
            candles[key]=c
    return candles,schema


def get_attempts(rows):
    attempts={}
    context={}
    for r in rows:
        a=r['attempt_id'];s=r['symbol'];pre=parse_time(r['pre_closed_ts'])
        if pre%BAR_MS:raise ValueError('Non-30m PRE boundary: '+a)
        if a in attempts and attempts[a]!=(s,pre):raise ValueError('Inconsistent attempt metadata '+a)
        attempts[a]=(s,pre)
        if float(r['threshold_atr'])==1.0:
            context[a]=r.get('latest_chain_h2_distance_zone_atr','')
    if not attempts:raise ValueError('No attempts in summary')
    return attempts,context


def compute_metrics(bars):
    if len(bars)<2:raise ValueError('Need >=2 candles')
    close=[b['close'] for b in bars];high=[b['high'] for b in bars];low=[b['low'] for b in bars]
    delta=[close[i]-close[i-1] for i in range(1,len(close))]
    gross=sum(abs(d) for d in delta)
    net=close[-1]-close[0]
    efficiency=abs(net)/gross if gross>0 else 0.0
    signed=net/gross if gross>0 else 0.0
    overlaps=[]
    for prev,curr in zip(bars,bars[1:]):
        intersection=max(0.0,min(prev['high'],curr['high'])-max(prev['low'],curr['low']))
        union=max(prev['high'],curr['high'])-min(prev['low'],curr['low'])
        overlaps.append(intersection/union if union>0 else 0.0)
    newhigh=0;running=close[0];peak=close[0];trough=close[0]
    max_dd=0.0;max_du=0.0
    for c in close[1:]:
        if c>running:newhigh+=1
        running=max(running,c)
        peak=max(peak,c)
        trough=min(trough,c)
        max_dd=max(max_dd,(peak-c)/peak)
        max_du=max(max_du,(c-trough)/trough)
    highest=max(high);lowest=min(low);width=highest-lowest
    minclose=min(close)
    return {
        'net_close_return_pct':100*net/close[0],
        'close_path_efficiency':efficiency,
        'signed_upward_efficiency':signed,
        'positive_close_steps_share':sum(d>0 for d in delta)/len(delta),
        'new_close_highs_share':newhigh/len(delta),
        'consecutive_range_overlap_mean':sum(overlaps)/len(overlaps),
        'close_max_drawdown_pct':100*max_dd,
        'close_max_drawup_pct':100*max_du,
        'window_high_low_range_pct':100*width/close[0],
        'pre_close_position_in_range':(close[-1]-lowest)/width if width>0 else 0.0,
        'pre_close_vs_window_low_pct':100*(close[-1]-lowest)/lowest,
        'recovery_from_window_close_low_pct':100*(close[-1]-minclose)/minclose,
    }


def analyze(rows,candles_by_symbol,windows=WINDOWS):
    attempts,context=get_attempts(rows)
    output=[];overview=[]
    for a,(symbol,pre) in sorted(attempts.items()):
        candles=candles_by_symbol.get(symbol)
        per_window={}
        for window in windows:
            expected=[pre-window*BAR_MS+i*BAR_MS for i in range(window)]
            available=[candles[t] for t in expected if candles is not None and t in candles]
            if candles is None:status='MISSING_SYMBOL'
            elif len(available)!=window:status='INSUFFICIENT_OR_GAPPED_OHLC'
            else:status='COMPLETE_PRE'
            item={'attempt_id':a,'symbol':symbol,'pre_closed_ts':pre,'window_bars':window,
                  'window_start_ts':expected[0],'status':status,'bars_found':len(available),
                  'first_candle_open_ts':expected[0] if status=='COMPLETE_PRE' else '',
                  'last_candle_close_ts':pre if status=='COMPLETE_PRE' else '',
                  'latest_h2_distance_zone_atr_1atr':context.get(a,'')}
            if status=='COMPLETE_PRE':item.update(compute_metrics(available))
            output.append(item);per_window[window]=item
        def metric(w,key):return round(per_window[w][key],8) if w in per_window and key in per_window[w] else ''
        overview.append({'attempt_id':a,'symbol':symbol,'pre_closed_ts':pre,
                         **{f'status_{w}':per_window[w]['status'] if w in per_window else '' for w in (12,24,48,96)},
                         'efficiency_24':metric(24,'close_path_efficiency'),'efficiency_48':metric(48,'close_path_efficiency'),
                         'overlap_24':metric(24,'consecutive_range_overlap_mean'),
                         'overlap_48':metric(48,'consecutive_range_overlap_mean'),
                         'net_return_24_pct':metric(24,'net_close_return_pct'),
                         'net_return_48_pct':metric(48,'net_close_return_pct'),
                         'latest_h2_distance_zone_atr_1atr':context.get(a,'')})
    return output,overview


def write_csv(path,rows,cols):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader()
        for row in rows:
            cleaned={k:(round(v,8) if isinstance(v,float) else v) for k,v in row.items()}
            w.writerow(cleaned)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    op=p.add_mutually_exclusive_group(required=True);op.add_argument('--plan',action='store_true');op.add_argument('--run',action='store_true')
    p.add_argument('--source',type=Path,default=DEFAULT_SOURCE)
    p.add_argument('--candles-dir',type=Path,default=DEFAULT_CANDLES)
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--timestamp-kind',choices=('open','close'),default=None,
                   help='ONLY for ambiguous timestamp/time columns; requires verifying CSV meaning')
    args=p.parse_args(argv)
    summary=args.source/'approach_summary_pre.csv'
    if not summary.is_file():p.error('Missing source '+str(summary))
    rows=read_csv(summary);attempts,_=get_attempts(rows)
    symbols=sorted({s for s,_ in attempts.values()})
    schemas={}; candles={};errors={}
    for symbol in symbols:
        path=args.candles_dir/(symbol+'_30m.csv')
        if not path.is_file():errors[symbol]='MISSING_FILE';continue
        try:
            data,schema=load_candles(path,args.timestamp_kind)
            candles[symbol]=data
            schemas[symbol]={'file':str(path),'rows':len(data),'time_column':schema['time_col'],
                             'time_kind':schema['time_kind'],
                             'min_open_ts':min(data) if data else None,'max_open_ts':max(data) if data else None}
        except (ValueError,KeyError) as e:errors[symbol]=str(e)
    plan={'source':str(summary),'candles_dir':str(args.candles_dir),'output':str(args.output),
          'attempts':len(attempts),'symbols':len(symbols),'loaded_symbols':len(candles),
          'timestamp_contract':'open+30m <= pre_closed_ts; PRE does not include the forming candle',
          'read_only_inputs':True,'no_live':True,'no_dashboard_changes':True,
          'no_resistance_price_in_v1_summary':True,
          'errors':errors,'schemas':schemas}
    print('HLL_OHLC_PRE_PLAN '+json.dumps(plan,ensure_ascii=False,sort_keys=True))
    if args.plan:return 0
    if errors:
        print('HLL_OHLC_PRE_ABORT '+json.dumps({'reason':'Invalid/missing candle schema or files', 'errors':errors},ensure_ascii=False))
        return 2
    output,overview=analyze(rows,candles)
    counts=Counter(r['status'] for r in output)
    args.output.mkdir(parents=True,exist_ok=True)
    write_csv(args.output/'candle_windows_pre.csv',output,OUT_FIELDS)
    write_csv(args.output/'attempts_overview_pre.csv',overview,OVERVIEW_FIELDS)
    manifest={'version':'HLL_STRUCTURAL_APPROACH_OHLC_PRE_V2','input_summary_sha256':filehash(summary),
              'candle_source_sha256':{s:filehash(args.candles_dir/(s+'_30m.csv')) for s in symbols},
              'attempts':len(attempts),'windows':WINDOWS,'rows':len(output),
              'statuses':dict(counts),'sources':plan['schemas'],
              'uses_post':False,'creates_signals':False,'modifies_live':False,'modifies_dashboard':False,
              'visual_labels_used_for_features':False,
              'no_resistance_price_in_v1_summary':True,
              'notes':[
                  'Metrics use exactly N consecutive CLOSED 30m candles ending at PRE, no forward fill.',
                  'All examples are included: no feature thresholding or label-dependent selection.',
                  'OHLC does not identify high-before-low inside same candle.',
                  '1ATR latest H2 distance from previous V1, not the distance of each candle from resistance.',
                  'The 50 attempts were selected around breakouts, not a population of all resistance approaches.',
                  'Overlapping attempts from a symbol are not statistically independent.',
                  'Close-to-close efficiency describes only close path, not tick or intrabar efficiency.',
              ]}
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('HLL_OHLC_PRE_DONE '+json.dumps({'output':str(args.output),'attempts':len(attempts),
                                         'window_rows':len(output),'statuses':dict(counts)},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
