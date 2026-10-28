#!/usr/bin/env python3
"""HLL V3.0.0: frozen 290-symbol ASCENDING_CLEAN first-flip 1m 12h replication.

Public historical USD-M futures data only. No trading, LIVE, DB, or detector writes.
The September sample and TP2/SL2 hypothesis are fixed before observing new outcomes.

Run from repo root: python3 -m dashboard.views.hll_v300_clean_flip_290 --plan
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

PRE=Path('reports/horizontal_level_structure_quality_v282/runs/sep290_pre_all_frozen_v282/pre_cohort.csv')
OLD_PRE=Path('reports/horizontal_level_structure_quality_v282/runs/core15_pre_all_v282/pre_cohort.csv')
UNIVERSE=Path('reports/horizontal_level_290_preflight_v297/runs/sep290_preflight_v297/universe_290.txt')
CORE_1M=Path('reports/horizontal_level_cross_research_v22/extended_1m_core15_v25')
OUT=Path('reports/horizontal_level_clean_flip_290_v300/runs/sep_first_per_level_clean_flip_tp2_sl2_12h')
SEP_START=1788739200000  # September 7, 2026 UTC
SEP_END=1790812800000  # October 1, 2026 UTC (exclusive)
MIN=60_000
HORIZON=720
FIELDS=('timestamp','open','high','low','close','volume','quoteVolume')
BASE='https://fapi.binance.com/fapi/v1/klines'


def file_sha(p:Path):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def utc(ts):return datetime.fromtimestamp(int(ts)/1000,tz=timezone.utc).isoformat()


def validate_symbol(s):
    return bool(s) and len(s)<=40 and s.upper()==s and not any(c in s for c in '/\\.\0\r\n\t?#&=')


def cohort(pre:pd.DataFrame, old_symbols:set[str], universe:list[str]):
    req={'attempt_id','symbol','level_id','breakout_closed_ts','pre_pattern_48','post_outcome','flip_confirmed_closed_ts'}
    if missing:=req-set(pre):raise ValueError(f'Missing PRE columns {sorted(missing)}')
    if pre.attempt_id.isna().any() or pre.attempt_id.duplicated().any() or pre.level_id.isna().any():
        raise ValueError('PRE missing/duplicate attempt_id or missing level_id')
    if set(pre.symbol)!=set(universe):raise ValueError('PRE does not contain exact frozen 290-symbol universe')
    data=pre.copy()
    data['breakout_closed_ts']=pd.to_numeric(data.breakout_closed_ts,errors='raise').astype('int64')
    # Freeze FIRST attempt over entire PRE history BEFORE month, pattern, or flip filtering.
    first=(data.sort_values(['breakout_closed_ts','attempt_id'],kind='stable')
           .drop_duplicates(['symbol','level_id'],keep='first'))
    eligible=first.loc[first.breakout_closed_ts.ge(SEP_START)&first.breakout_closed_ts.lt(SEP_END)&
        first.pre_pattern_48.eq('ASCENDING_CLEAN')&first.post_outcome.eq('FLIP_CONFIRMADO')].copy()
    if eligible.flip_confirmed_closed_ts.isna().any():raise ValueError('Confirmed flip missing timestamp')
    eligible['entry_ts']=pd.to_numeric(eligible.flip_confirmed_closed_ts,errors='raise').astype('int64')+MIN
    if (eligible.entry_ts<=eligible.breakout_closed_ts).any() or (eligible.entry_ts%MIN).any():
        raise ValueError('Invalid flip entry timestamp')
    eligible['end_ts']=eligible.entry_ts+HORIZON*MIN
    eligible['group']=eligible.symbol.map(lambda x:'ORIGINAL_15' if x in old_symbols else 'ADDITIONAL_275')
    eligible['breakout_utc']=eligible.breakout_closed_ts.map(utc)
    eligible['entry_utc']=eligible.entry_ts.map(utc)
    eligible['entry_id']=eligible.apply(lambda r: hashlib.sha256(
        f'{r.attempt_id}|FLIP_1M|{r.entry_ts}'.encode()).hexdigest()[:24],axis=1)
    if eligible.entry_id.duplicated().any() or eligible.duplicated(['symbol','level_id']).any():
        raise ValueError('Duplicate selected entry')
    return eligible.sort_values(['symbol','entry_ts','attempt_id']).reset_index(drop=True)


def load_cohort(pre_path:Path,old_pre_path:Path,universe_path:Path):
    u=universe_path.read_text(encoding='utf-8').splitlines()
    if len(u)!=290 or len(set(u))!=290 or u!=sorted(u) or not all(validate_symbol(x) for x in u):
        raise ValueError('Frozen 290 universe invalid')
    old=pd.read_csv(old_pre_path,usecols=['symbol'])
    olds=set(old.symbol.astype(str))
    if len(olds)!=15 or not olds.issubset(set(u)):raise ValueError('Original 15 cohort mismatch')
    pre=pd.read_csv(pre_path,low_memory=False)
    c=cohort(pre,olds,u)
    return c,{'pre_sha256':file_sha(pre_path),'old_pre_sha256':file_sha(old_pre_path),
              'universe_sha256':file_sha(universe_path),'all_attempts':len(pre),
              'cohort_count':len(c),'original_15_count':int(c.group.eq('ORIGINAL_15').sum()),
              'additional_275_count':int(c.group.eq('ADDITIONAL_275').sum())}


def windows_for(g:pd.DataFrame):
    """Union 12h windows for a single symbol; never fetch unrelated minutes."""
    spans=sorted((int(r.entry_ts),int(r.end_ts)) for r in g.itertuples(index=False))
    merged=[]
    for a,b in spans:
        if not merged or a>merged[-1][1]:merged.append([a,b])
        else:merged[-1][1]=max(merged[-1][1],b)
    return [(a,b) for a,b in merged]


def validate_row(row,expected_ts=None):
    if not all(k in row for k in FIELDS):raise ValueError('Missing OHLCV fields')
    ts=int(row['timestamp'])
    if expected_ts is not None and ts!=expected_ts:raise ValueError(f'1m gap expected={expected_ts} actual={ts}')
    vals=[float(row[k]) for k in FIELDS[1:]]
    if not all(math.isfinite(v) for v in vals):raise ValueError('Nonfinite OHLCV')
    op,hi,lo,cl,vol,q=vals
    if min(op,hi,lo,cl)<=0 or min(vol,q)<0 or hi<max(op,lo,cl) or lo>min(op,hi,cl):
        raise ValueError('Invalid OHLCV')
    return [str(ts)]+[str(row[k]) for k in FIELDS[1:]]


def validate_archive(path:Path, spans:list[tuple[int,int]],require_exact=True):
    """Validate ALL required windows; gaps outside requested spans are permitted."""
    if not path.is_file():raise FileNotFoundError(path)
    expect=[(s,e) for s,e in spans]
    sidx=0; expected=expect[0][0] if expect else None
    selected=[]; last=None
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if set(FIELDS)-set(reader.fieldnames or []):raise ValueError('Missing CSV columns '+str(path))
        for row in reader:
            ts=int(row['timestamp'])
            if last is not None and ts<=last:raise ValueError('Non-increasing timestamps in '+str(path))
            last=ts
            # Skip bars not requested (baseline 1m may contain more history).
            while sidx<len(expect) and expected==expect[sidx][1]:
                sidx+=1
                if sidx<len(expect):expected=expect[sidx][0]
            if sidx==len(expect):
                if require_exact:raise ValueError('Unexpected tail outside requested windows')
                continue
            start,end=expect[sidx]
            if ts<expected:
                if require_exact:raise ValueError('Unexpected row outside requested windows')
                continue
            if ts>=end:
                raise ValueError(f'Missing 1m bars before {utc(end)}: {path}')
            validate_row(row,expected)
            selected.append([str(ts)]+[str(row[k]) for k in FIELDS[1:]])
            expected+=MIN
    if sidx<len(expect) and expected==expect[sidx][1]:sidx+=1
    if sidx!=len(expect):raise ValueError(f'Missing tail 1m in {path}, requested={len(expect)}, complete={sidx}')
    return selected


def fetch_page(sym,start,end,sleep_s=0.3):
    url=BASE+'?'+urlencode({'symbol':sym,'interval':'1m','startTime':start,'endTime':end-1,'limit':1000})
    last=None
    for i in range(5):
        try:
            with urlopen(Request(url,headers={'User-Agent':'HLL-V300-FrozenCleanFlip1m/1.0'}),timeout=30) as res:
                bars=json.load(res)
            if not isinstance(bars,list):raise ValueError(f'Unexpected Binance payload: {str(bars)[:150]}')
            if sleep_s:time.sleep(sleep_s)
            return bars
        except HTTPError as e:
            last=f'HTTP {e.code}: {e.read(200).decode(errors="replace")}'
            if e.code==418:raise RuntimeError('FATAL Binance 418 IP ban: stop') from e
            if e.code not in [429,500,502,503,504]:raise RuntimeError(last) from e
            delay=max(60,int(e.headers.get('Retry-After','60'))) if e.code==429 else min(30,2**(i+1))
        except (URLError,TimeoutError,OSError) as e:
            last=repr(e);delay=min(30,2**(i+1))
        if i<4:time.sleep(delay)
    raise RuntimeError(f'Binance fetch failed for {sym}: {last}')


def fetch_spans(sym,spans,fetcher=fetch_page,sleep_s=.3):
    result=[];pages=0
    for start,end in spans:
        cursor=start
        while cursor<end:
            packet=fetcher(sym,cursor,end,sleep_s=sleep_s);pages+=1
            if not packet:raise ValueError(f'Empty 1m page {sym} {utc(cursor)}')
            previous=cursor
            for k in packet:
                if len(k)<8:raise ValueError('Malformed Binance kline')
                t=int(k[0]); row=dict(zip(FIELDS,(t,k[1],k[2],k[3],k[4],k[5],k[7])))
                if t>=end:raise ValueError('Out of range Binance 1m')
                result.append(validate_row(row,previous))
                previous+=MIN
            cursor=previous
    # Revalidate gap logic for disjoint windows from assembled rows (without disk).
    expected_total=sum((b-a)//MIN for a,b in spans)
    if len(result)!=expected_total:raise ValueError('Downloaded count differs from required minutes')
    return result,pages


def write_exclusive(path:Path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.stem+'.',suffix='.partial')
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            w=csv.writer(f);w.writerow(FIELDS);w.writerows(rows)
            f.flush();os.fsync(f.fileno())
        os.link(temp,path)
    finally:Path(temp).unlink(missing_ok=True)


def provenance_one(sym,g,args,fetcher=fetch_page):
    spans=windows_for(g)
    dst=args.output/'minutes'/(sym+'_1m.csv')
    met=args.output/'provenance'/(sym+'.json')
    if dst.is_file():
        validate_archive(dst,spans)
        if not met.is_file():raise ValueError('Existing 1m without provenance: '+str(dst))
        saved=json.loads(met.read_text(encoding='utf-8'))
        if saved.get('sha256')!=file_sha(dst) or saved.get('spans')!=spans_as_json(spans):
            raise ValueError('Existing 1m provenance mismatch: '+sym)
        return dict(symbol=sym,status='SKIP_VERIFIED',bars1m=sum((b-a)//MIN for a,b in spans))
    source=args.core_1m/(sym+'_1m.csv')
    reuse=None
    if source.is_file():
        try:reuse=validate_archive(source,spans,require_exact=False)
        except (ValueError,OSError):pass
    if reuse is not None:
        rows=reuse; origin='CORE15_ARCHIVE_REUSE'; pages=0
    else:
        rows,pages=fetch_spans(sym,spans,fetcher,sleep_s=args.sleep_s)
        origin='BINANCE_FAPI_PUBLIC'
    write_exclusive(dst,rows)
    # Validate generated path before writing provenance. On failure file remains
    # visibly invalid; never silently considered successful on resume.
    validate_archive(dst,spans)
    metadata=dict(symbol=sym,spans=spans_as_json(spans),sha256=file_sha(dst),
                  source=origin,source_sha256=file_sha(source) if origin.startswith('CORE15') else None,
                  first_ts=int(rows[0][0]),last_ts=int(rows[-1][0]),bars1m=len(rows),pages=pages)
    met.parent.mkdir(parents=True,exist_ok=True)
    with met.open('x',encoding='utf-8') as f:json.dump(metadata,f,ensure_ascii=False,indent=2)
    return {'symbol':sym,'status':'FULL','source':origin,'bars1m':len(rows),'pages':pages}


def spans_as_json(spans):return [[int(a),int(b)] for a,b in spans]


def init_manifest(args,c,src):
    args.output.mkdir(parents=True,exist_ok=True)
    mp=args.output/'cohort_manifest.json'
    selected=[dict(attempt_id=str(r.attempt_id),symbol=r.symbol,entry_ts=int(r.entry_ts),
                   group=r.group) for r in c.itertuples(index=False)]
    manifest=dict(version='HLL_V300_CLEAN_FLIP_290',scope='FIRST_PER_LEVEL',
        timeframe='1m',horizon_min=HORIZON,breakout_start_utc=utc(SEP_START),
        breakout_end_exclusive_utc=utc(SEP_END),pattern='ASCENDING_CLEAN',window_pre_30m_bars=48,
        route='FLIP_1M',entry_rule='OPEN 1m at flip_confirmed_closed_ts + 60000ms',
        prespecified_tp_pct=2,prespecified_sl_pct=2,fee_roundtrip_pct=0.1,
        source=src,selected=selected)
    if mp.is_file():
        saved=json.loads(mp.read_text(encoding='utf-8'))
        if saved!=manifest:raise ValueError('Frozen cohort changed or source CSV hash differs')
    else:
        with mp.open('x',encoding='utf-8') as f:json.dump(manifest,f,ensure_ascii=False,indent=2)
    return manifest


def check_metadata(g,args):
    p=args.output/'minutes'/(str(g.symbol.iloc[0])+'_1m.csv')
    m=args.output/'provenance'/(str(g.symbol.iloc[0])+'.json')
    if not p.is_file() or not m.is_file():return 'MISSING'
    try:
        spans=windows_for(g)
        validate_archive(p,spans)
        meta=json.loads(m.read_text(encoding='utf-8'))
        if meta.get('sha256')!=file_sha(p) or meta.get('spans')!=spans_as_json(spans):return 'BAD_PROVENANCE'
    except (ValueError,OSError,KeyError):return 'INVALID'
    return 'FULL'


def evaluate(args,c,src):
    init_manifest(args,c,src)
    symbols=sorted(c.symbol.unique().tolist())
    status={sym:check_metadata(c[c.symbol.eq(sym)],args) for sym in symbols}
    if any(v!='FULL' for v in status.values()):
        print('HLL_V300_NOT_READY '+json.dumps(dict(Counter(status.values())),ensure_ascii=False),flush=True)
        raise RuntimeError('Missing/invalid 1m windows: finish --run before --evaluate')
    from dashboard.views.hll_v294_entry_matrix import _window_from_loaded
    from dashboard.views.hll_v295_net_matrix import first_touch_net, _validate_ohlc
    entries=[];detail=[]
    for sym,group in c.groupby('symbol',sort=True):
        bars=pd.read_csv(args.output/'minutes'/(sym+'_1m.csv'),usecols=['timestamp','open','high','low','close'],float_precision='round_trip')
        _validate_ohlc(bars,sym)
        bars.timestamp=pd.to_numeric(bars.timestamp,errors='raise').astype('int64')
        for r in group.itertuples(index=False):
            window,state=_window_from_loaded(bars,int(r.entry_ts),HORIZON)
            if state!='COMPLETE_1M' or len(window)!=HORIZON:
                raise ValueError(f'Source validated but window incomplete: {sym} {r.attempt_id} {state}')
            op=float(window.open.iloc[0]);
            if not math.isfinite(op) or op<=0:raise ValueError('No observed OPEN')
            meta=dict(entry_id=r.entry_id,attempt_id=r.attempt_id,symbol=sym,group=r.group,
                      level_id=r.level_id,breakout_utc=r.breakout_utc,entry_ts=int(r.entry_ts),
                      entry_utc=r.entry_utc,pre_pattern_48='ASCENDING_CLEAN',route='FLIP_1M',
                      signal_closed_utc=utc(int(r.flip_confirmed_closed_ts)),entry_price=op,
                      path_state=state)
            entries.append(meta)
            result=first_touch_net(window,op,state,int(r.entry_ts),.1,0)
            for d in result.to_dict('records'):
                d.update(meta)
                d['exit_minute_utc']=(d['first_hit_minute_utc'] if d['result']!='TIMEOUT'
                      else utc(int(r.entry_ts)+HORIZON*MIN))
                d['minutes_to_exit']=d['first_hit_min'] if d['result']!='TIMEOUT' else HORIZON
                d['horizon_min']=HORIZON
                detail.append(d)
    edf=pd.DataFrame(entries)
    ddf=pd.DataFrame(detail)
    if len(edf)!=len(c) or len(ddf)!=len(c)*25 or ddf.duplicated(['entry_id','tp_pct','sl_pct']).any():
        raise ValueError('Entry/scenario count integrity failure')
    primary=ddf.loc[ddf.tp_pct.eq(2)&ddf.sl_pct.eq(2)].copy()
    summary=[]
    for name,grp in [('ORIGINAL_15',primary[primary.group.eq('ORIGINAL_15')]),
                     ('ADDITIONAL_275',primary[primary.group.eq('ADDITIONAL_275')]),
                     ('ALL_290',primary)]:
        known=grp[grp.result.isin(['TP_FIRST','SL_FIRST','TIMEOUT'])]
        summary.append({'group':name,'entries':len(grp),'known':len(known),
            'tp_first':int(grp.result.eq('TP_FIRST').sum()),
            'sl_first':int(grp.result.eq('SL_FIRST').sum()),
            'timeout':int(grp.result.eq('TIMEOUT').sum()),
            'ambiguous':int(grp.result.eq('AMBIGUOUS_1M').sum()),
            'censored':int(len(grp)-len(known)-int(grp.result.eq('AMBIGUOUS_1M').sum())),
            'mean_net_known_pct':float(known.net_pct.mean()) if len(known) else None,
            'median_net_known_pct':float(known.net_pct.median()) if len(known) else None,
            'symbols_with_entries':int(grp.symbol.nunique())})
    dest=args.output/'evaluation'
    if dest.exists():raise FileExistsError('Evaluation already exists: '+str(dest))
    dest.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(dir=dest.parent,prefix='.hll-v300-eval-'))
    try:
        edf.to_csv(tmp/'entry_events.csv',index=False)
        ddf.to_csv(tmp/'entry_scenarios_25.csv',index=False)
        pd.DataFrame(summary).to_csv(tmp/'primary_tp2_sl2_12h_summary.csv',index=False)
        pd.DataFrame([{'symbol':s,'group':g.group.iloc[0],
            'entries':len(g),'mean_net_known_pct':float(g.net_pct.mean()),
            'tp_first':int(g.result.eq('TP_FIRST').sum()),
            'sl_first':int(g.result.eq('SL_FIRST').sum())}
            for s,g in primary.groupby('symbol')]).to_csv(tmp/'by_symbol_tp2_sl2.csv',index=False)
        with (tmp/'manifest.json').open('w',encoding='utf-8') as f:
            json.dump(dict(version='HLL_V300_EVALUATION',source=src,cohort=len(c),
              cost_roundtrip_pct=.1,slippage_pct=0,limitations=[
              'Barrier fills theoretical; no funding, spread, adverse gaps, or capacity',
              'All selected events historically inspected; 275 symbols are not a genuine forward holdout',
              'One-minute OHLC cannot sequence simultaneous high/low barrier touches',
              'Correlated levels/symbols/days; descriptive outcomes not strategy live profitability']),f,indent=2)
        tmp.rename(dest)
    finally:
        if tmp.exists():shutil.rmtree(tmp)
    print('HLL_V300_EVALUATED '+json.dumps({'entries':len(c),'scenario_rows':len(ddf),'summary':summary,
         'output':str(dest)},ensure_ascii=False),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    for action in ('plan','run','report','evaluate'):mode.add_argument('--'+action,action='store_true')
    p.add_argument('--pre',type=Path,default=PRE)
    p.add_argument('--old-pre',type=Path,default=OLD_PRE)
    p.add_argument('--universe',type=Path,default=UNIVERSE)
    p.add_argument('--core-1m',type=Path,default=CORE_1M)
    p.add_argument('--output',type=Path,default=OUT)
    p.add_argument('--batch-start',type=int,default=0)
    p.add_argument('--batch-size',type=int,default=20)
    p.add_argument('--sleep-s',type=float,default=.3)
    p.add_argument('--min-free-gb',type=float,default=4)
    a=p.parse_args(argv)
    if a.batch_start<0 or a.batch_size<1 or a.batch_size>290 or a.sleep_s<.1 or a.min_free_gb<1:p.error('Invalid batch/rate/disk guard')
    c,src=load_cohort(a.pre,a.old_pre,a.universe)
    syms=sorted(c.symbol.unique())
    spans=sum(len(windows_for(g)) for _,g in c.groupby('symbol'))
    plan=dict(version='HLL_V300_CLEAN_FLIP_290',signals=len(c),groups=c.group.value_counts().to_dict(),
        symbols_with_signals=len(syms),merged_1m_windows=spans,
        total_requested_1m_bars=sum((e-s)//MIN for _,g in c.groupby('symbol') for s,e in windows_for(g)),
        mode='READ_ONLY_PLAN' if a.plan else 'BATCH_1M_FETCH' if a.run else 'CHECK' if a.report else 'EVALUATE',
        source=src,output=str(a.output),read_only_live=True)
    print('HLL_V300_PLAN '+json.dumps(plan,ensure_ascii=False),flush=True)
    if a.plan:return 0
    if a.report:
        status=Counter(check_metadata(c[c.symbol.eq(sym)],a) for sym in syms)
        print('HLL_V300_REPORT '+json.dumps({'symbols_with_signals':len(syms),**status},ensure_ascii=False),flush=True)
        return 0
    init_manifest(a,c,src)
    if a.evaluate:
        evaluate(a,c,src);return 0
    picked=syms[a.batch_start:a.batch_start+a.batch_size]
    if not picked:p.error('Batch out of range')
    counters=Counter()
    for i,sym in enumerate(picked,a.batch_start+1):
        if shutil.disk_usage(a.output).free < a.min_free_gb*1024**3:
            raise RuntimeError('DISK_GUARD low space, no action taken for remaining symbols')
        try:
            info=provenance_one(sym,c.loc[c.symbol.eq(sym)],a)
            counters[info['status']]+=1
            print('HLL_V300_SYMBOL '+json.dumps({'index':i,'of':len(syms),**info},ensure_ascii=False),flush=True)
        except (OSError,RuntimeError,ValueError,KeyError) as e:
            counters['ERROR']+=1
            print('HLL_V300_ERROR '+json.dumps({'symbol':sym,'error':f'{type(e).__name__}: {e}'[:240]},ensure_ascii=False),flush=True)
            if 'FATAL Binance 418' in str(e):break
    print('HLL_V300_BATCH_DONE '+json.dumps({'batch_start':a.batch_start,'attempted':len(picked),**counters},ensure_ascii=False),flush=True)
    return 2 if counters['ERROR'] else 0


if __name__=='__main__':raise SystemExit(main())
