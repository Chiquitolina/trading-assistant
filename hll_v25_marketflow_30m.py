#!/usr/bin/env python3
"""Attach causal BTC/symbol-breadth market context to frozen HLC V2.2 signals.

Pure research, offline, no exchange/network/order/DB access. Every feature uses
30m bars whose open + 30min <= signal. The breadth universe is the explicit
non-BTC symbols supplied; this is a COHORT breadth, not true Binance breadth.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TF = 1_800_000
HORIZONS = {'btc_return_1h_pct': 2, 'btc_return_4h_pct': 8, 'btc_return_24h_pct': 48}
FIELDS = ['event_id','symbol','stage','direction','known_closed_ts','btc_return_1h_pct',
          'btc_return_4h_pct','btc_return_24h_pct','symbol_return_4h_pct',
          'strength_vs_btc_4h_pct','cohort_breadth_4h','cohort_n_available',
          'cohort_n_requested','market_regime','side_flow','source_state']


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''): h.update(part)
    return h.hexdigest()


def parse_data(path):
    if not path.is_file():
        raise FileNotFoundError(path)
    out = {}
    with path.open(newline='',encoding='utf-8') as f:
        reader=csv.DictReader(f)
        if not {'timestamp','close'}.issubset(reader.fieldnames or []):
            raise ValueError(f'Unrecognized 30m CSV: {path}')
        for row in reader:
            ts=int(row['timestamp']); cl=float(row['close'])
            if ts % TF or not math.isfinite(cl) or cl <=0 or (ts+TF) in out:
                raise ValueError(f'Invalid 30m data {path} at {ts}')
            out[ts+TF]=cl # keyed by actual candle CLOSE timestamp
    return out


def ret(prices, t, n):
    """Require EVERY intermediate closed 30m bar; never bridge gaps."""
    if any((t - k*TF) not in prices for k in range(n+1)):
        return None
    old=prices[t-n*TF]
    return (prices[t]/old-1)*100


def contextualize(event, btc, universe, *, min_breadth=8, up_threshold=0.5,
                  breadth_up=0.6, breadth_down=0.4):
    t=int(event['known_closed_ts']); sym=event['symbol']; direction=event['direction']
    bc1=ret(btc,t,2); bc4=ret(btc,t,8); bc24=ret(btc,t,48)
    sr4=ret(universe[sym],t,8) if sym in universe else None
    present=[]
    for prices in universe.values():
        r=ret(prices,t,8)
        if r is not None:present.append(r)
    breadth=sum(v>0 for v in present)/len(present) if present else None
    regime='UNAVAILABLE'
    if bc4 is not None and breadth is not None and len(present)>=min_breadth:
        if bc4>=up_threshold and breadth>=breadth_up:regime='RISK_ON'
        elif bc4<=-up_threshold and breadth<=breadth_down:regime='RISK_OFF'
        else:regime='MIXED'
    side_flow=('TAILWIND' if (direction=='LONG' and regime=='RISK_ON') or
               (direction=='SHORT' and regime=='RISK_OFF') else
               'HEADWIND' if (direction=='LONG' and regime=='RISK_OFF') or
               (direction=='SHORT' and regime=='RISK_ON') else
               'MIXED' if regime=='MIXED' else 'UNAVAILABLE')
    fmt=lambda x: '' if x is None else round(x,8)
    return dict(event_id=event['event_id'],symbol=sym,stage=event['stage'],
        direction=direction,known_closed_ts=t,btc_return_1h_pct=fmt(bc1),
        btc_return_4h_pct=fmt(bc4),btc_return_24h_pct=fmt(bc24),
        symbol_return_4h_pct=fmt(sr4),
        strength_vs_btc_4h_pct=fmt(sr4-bc4 if sr4 is not None and bc4 is not None else None),
        cohort_breadth_4h=fmt(breadth),cohort_n_available=len(present),
        cohort_n_requested=len(universe),market_regime=regime,
        side_flow=side_flow,source_state='COMPLETE' if regime!='UNAVAILABLE' else 'INSUFFICIENT_CLOSED_CONTEXT')


def main(argv=None):
    p=argparse.ArgumentParser(description='Causal 30m BTC and cohort market flow for frozen V2.2 signals')
    p.add_argument('--run-dir',required=True,type=Path)
    p.add_argument('--candles-dir',required=True,type=Path)
    p.add_argument('--output-dir',required=True,type=Path)
    p.add_argument('--min-breadth-symbols',type=int,default=8)
    a=p.parse_args(argv)
    sig=a.run_dir/'signals.csv'
    if not sig.is_file():p.error(f'No frozen signals.csv at {sig}')
    if a.min_breadth_symbols<1:p.error('Min breadth must >=1')
    with sig.open(newline='',encoding='utf-8') as f:
        signals=list(csv.DictReader(f))
    expected={'event_id','symbol','stage','direction','known_closed_ts'}
    if not signals or not expected.issubset(signals[0]):p.error('Not a V2.2 signals.csv')
    syms=sorted({x['symbol'] for x in signals if x['symbol']!='BTCUSDT'})
    sources={s:parse_data(a.candles_dir/f'{s}_30m.csv') for s in syms}
    btc=parse_data(a.candles_dir/'BTCUSDT_30m.csv')
    if a.output_dir.exists():p.error('Output exists; choose a new folder, never overwrite a frozen run')
    a.output_dir.mkdir(parents=True,exist_ok=False)
    counter=Counter()
    with (a.output_dir/'features.csv').open('x',newline='',encoding='utf-8') as f:
        wr=csv.DictWriter(f,fieldnames=FIELDS);wr.writeheader()
        for event in signals:
            if event['symbol']=='BTCUSDT':continue
            row=contextualize(event,btc,sources,min_breadth=a.min_breadth_symbols)
            wr.writerow(row);counter[(row['stage'],row['side_flow'])]+=1
    info={'version':'hll-v2.5-causal-cohort-flow','signals_sha256':sha(sig),
          'feature_sha256':sha(a.output_dir/'features.csv'),
          'btc30_sha256':sha(a.candles_dir/'BTCUSDT_30m.csv'),
          'n_events':sum(counter.values()),'n_cohort_symbols':len(syms),
          'min_breadth_symbols':a.min_breadth_symbols,
          'regimes':{stage+'__'+regime:n for (stage,regime),n in sorted(counter.items())},
          'interpretation':'COHORT breadth, NOT entire Binance market. CONTEXT ONLY, no TP/SL selection.',
          'causality':'Only 30m candles closed at/before event. Continuous history required.'}
    (a.output_dir/'audit.json').write_text(json.dumps(info,indent=2,ensure_ascii=False),encoding='utf-8')
    print('HLL_V25_FLOW_RESULT',json.dumps(info,ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
