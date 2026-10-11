#!/usr/bin/env python3
"""Prepare causal Sep-2026 4h BTC/breadth deltas for Candidate V2 replay.

Keeps the original events.csv intact. Reads or incrementally repairs the same
4h pickle cache layout used by app.py. It does NOT backtest portfolio outcomes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

FOUR_H = 4 * 3600_000
SEP_START = int(pd.Timestamp('2026-09-01T00:00:00Z').timestamp() * 1000)
OCT_START = int(pd.Timestamp('2026-10-01T00:00:00Z').timestamp() * 1000)
STAMPS = np.arange(SEP_START, OCT_START, FOUR_H, dtype='int64')
FIELDS = ['timestamp','open','high','low','close','volume','close_time']
URL = 'https://fapi.binance.com/fapi/v1/klines'


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or not set(FIELDS).issubset(frame.columns):
        return pd.DataFrame(columns=FIELDS)
    out = frame[FIELDS].copy()
    for col in FIELDS:
        out[col] = pd.to_numeric(out[col], errors='coerce')
    out = out.dropna(subset=FIELDS)
    out = out.loc[out['timestamp'].between(SEP_START, OCT_START - FOUR_H)]
    out['timestamp'] = out['timestamp'].astype('int64')
    return out.sort_values('timestamp').drop_duplicates('timestamp', keep='last').reset_index(drop=True)


def _fetch(symbol: str, session: requests.Session) -> pd.DataFrame:
    params = {'symbol':symbol,'interval':'4h','startTime':SEP_START,'endTime':OCT_START-1,'limit':1000}
    for attempt in range(4):
        try:
            response = session.get(URL, params=params, timeout=22)
            if response.status_code in (418, 429):
                time.sleep(max(3, int(response.headers.get('Retry-After', '3'))))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f'unexpected API result: {str(payload)[:180]}')
            rows = [x[:7] for x in payload if isinstance(x, list) and len(x) >= 7]
            if not rows:
                return pd.DataFrame(columns=FIELDS)
            return _clean(pd.DataFrame(rows, columns=FIELDS))
        except requests.exceptions.HTTPError as exc:
            if response.status_code in (400, 404):
                raise RuntimeError(f'Binance {response.status_code}: {str(response.text)[:150]}') from exc
            if attempt == 3:
                raise
        except requests.RequestException:
            if attempt == 3:
                raise
        time.sleep(1.4 * (2 ** attempt))
    raise RuntimeError('Binance rate limited too many times')


def _load_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=FIELDS)
    try:
        return _clean(pd.read_pickle(path, compression='gzip'))
    except Exception as exc:
        raise RuntimeError(f'cannot read {path}: {exc}') from exc


def _merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    return _clean(pd.concat([old, new], ignore_index=True, sort=False))


def _repair(path: Path, symbol: str, session: requests.Session, download: bool):
    cached = _load_cache(path)
    present = set(cached['timestamp'].astype('int64').tolist())
    missing = len(set(STAMPS.tolist()) - present)
    status = 'complete' if missing == 0 else 'incomplete'
    if missing and download:
        try:
            downloaded = _fetch(symbol, session)
            merged = _merge(cached, downloaded)
            if not merged.equals(cached):
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + '.new')
                try:
                    merged.to_pickle(temp, compression='gzip')
                    temp.replace(path)
                finally:
                    if temp.exists():
                        temp.unlink()
            cached = merged
            missing = len(set(STAMPS.tolist()) - set(cached['timestamp'].astype('int64').tolist()))
            status = 'complete' if missing == 0 else ('partial_or_not_listed' if not cached.empty else 'no_binance_data')
        except Exception as exc:
            status = 'fetch_error: ' + str(exc)[:180]
    return cached, missing, status


def _build_market(universe: list[str], cache: dict[str, pd.DataFrame]) -> pd.DataFrame:
    # Retorno 4h = (cierre de vela / cierre de vela anterior - 1)*100.
    # El denominador del Breadth son sólo los símbolos con retornos válidos.
    closes = pd.DataFrame(index=pd.Index(STAMPS, name='timestamp'))
    for symbol in universe:
        df = cache.get(symbol)
        if df is not None and not df.empty:
            closes[symbol] = df.set_index('timestamp')['close'].reindex(STAMPS).to_numpy(dtype=float)
    returns = (closes / closes.shift(1) - 1.0) * 100.0
    returns = returns.replace([np.inf, -np.inf], np.nan)
    valid_n = returns.notna().sum(axis=1)
    pos_n = returns.gt(0).sum(axis=1)
    breadth = (100.0 * pos_n / valid_n.replace(0,np.nan)).round(4)
    btc_ret = returns['BTCUSDT'].round(4) if 'BTCUSDT' in returns else pd.Series(np.nan,index=returns.index)
    return pd.DataFrame({
        'boundary_timestamp': STAMPS,
        'valid_symbols': valid_n.to_numpy(),
        'positive_symbols': pos_n.to_numpy(),
        'coverage_pct': (valid_n / len(universe) * 100).round(4).to_numpy(),
        'market_breadth_4h': breadth.to_numpy(),
        'breadth_delta_4h': (breadth - breadth.shift(1)).to_numpy(),
        'btc_return_pct_4h_rebuilt': btc_ret.to_numpy(),
        'btc_return_delta_4h': (btc_ret - btc_ret.shift(1)).to_numpy(),
    })


def _enrich_events(events: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    # Match _candidate_v2_closed_boundary_series: previous closed 4h open.
    if 'candidate_v1_reaction_known_ts' in events.columns:
        known = pd.to_numeric(events['candidate_v1_reaction_known_ts'],errors='coerce')
    else:
        known = pd.Series(np.nan,index=events.index)
    retest = pd.to_numeric(events.get('retest_timestamp'), errors='coerce')
    known = known.fillna(retest + 60_000)
    # Fixed replay rows have same causal entry timestamp as original known_ts.
    boundary = (np.floor(known / FOUR_H) * FOUR_H) - FOUR_H
    result = events.copy()
    result['historical_4h_closed_boundary'] = pd.to_numeric(boundary, errors='coerce')
    return result.merge(market, left_on='historical_4h_closed_boundary', right_on='boundary_timestamp', how='left', validate='many_to_one')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true', help='Repair missing/incomplete 2026-09 4h cache partitions via Binance Futures')
    parser.add_argument('--project', default='.', help='Repository project root')
    args = parser.parse_args()
    root = Path(args.project).resolve()
    sys.path.insert(0, str(root))
    from config.strategies.v1 import SYMBOLS  # noqa E402
    universe = sorted({str(symbol).upper() for symbol in SYMBOLS} | {'BTCUSDT'})
    cache_root = root / 'reports/candidate_long_horizon/candle_cache/4h'
    report_dir = root / 'reports/candidate_long_horizon/diagnostics/v2_sep_2026'
    report_dir.mkdir(parents=True, exist_ok=True)
    cached = {}
    summary = []
    with requests.Session() as session:
        for i, symbol in enumerate(universe, 1):
            path = cache_root / symbol / '2026-09.pkl.gz'
            data, missing, status = _repair(path,symbol,session,args.download)
            cached[symbol] = data
            summary.append({'symbol':symbol, 'cached_4h_rows':len(data), 'missing_4h_bars':missing, 'status':status})
            if args.download and (i%25==0 or i==len(universe)):
                print(f'Processed {i}/{len(universe)} 4h symbols; last={symbol}: {status}', flush=True)
            time.sleep(0.04 if args.download else 0)
    summary_df = pd.DataFrame(summary)
    market = _build_market(universe, cached)
    events_path = root / 'reports/candidate_long_horizon/runs/20260907_20261006_28cb79f0702a/events.csv'
    events = pd.read_csv(events_path, low_memory=False)
    enriched = _enrich_events(events, market)
    cond_a = pd.to_numeric(enriched['btc_return_delta_4h'], errors='coerce').le(-0.4494)
    cond_b = pd.to_numeric(enriched['breadth_delta_4h'], errors='coerce').ge(47.2414)
    both_available = enriched['btc_return_delta_4h'].notna() & enriched['breadth_delta_4h'].notna()
    strength = pd.to_numeric(enriched['side_adjusted_strength_vs_btc_4h'], errors='coerce')
    selected = strength.gt(0)
    enriched['ab_context_available'] = both_available
    enriched['ab_no_trade_blocked'] = (cond_a | cond_b).where(both_available, pd.NA).astype('boolean')
    summary_df.to_csv(report_dir / 'cache_4h_coverage.csv', index=False)
    market.to_csv(report_dir / 'market_4h_context.csv', index=False)
    enriched.to_csv(report_dir / 'events_strength_ab_context.csv', index=False)
    strengths = enriched.loc[selected]
    breakdown = {
        'universe_count': len(universe),
        'symbols_full_month': int(summary_df['missing_4h_bars'].eq(0).sum()),
        'symbols_partial_or_absent': int(summary_df['missing_4h_bars'].gt(0).sum()),
        'min_market_coverage_pct_sep7_sep30': float(market.loc[market['boundary_timestamp'].ge(int(pd.Timestamp('2026-09-07T00:00:00Z').timestamp()*1000)), 'coverage_pct'].min()),
        'events': len(enriched),
        'strength_positive': int(selected.sum()),
        'strength_positive_with_both_features': int(strengths['ab_context_available'].sum()),
        'strength_positive_blocked_by_A_or_B': int(strengths['ab_no_trade_blocked'].eq(True).sum()),
        'strength_positive_eligible_by_A_or_B': int(strengths['ab_no_trade_blocked'].eq(False).sum()),
        'strength_positive_unknown_A_or_B': int(strengths['ab_no_trade_blocked'].isna().sum()),
        'note': 'Pre-portfolio feature coverage and block masks only. Exact market context parity and portfolio backtest NOT certified.',
    }
    (report_dir/'summary.json').write_text(json.dumps(breakdown,indent=2),encoding='utf-8')
    print(json.dumps(breakdown,indent=2))
    print('Output:', report_dir)
    if not args.download:
        print('READ-ONLY CACHE MODE: use --download to fetch missing data (reports were written).')


if __name__=='__main__':
    main()
