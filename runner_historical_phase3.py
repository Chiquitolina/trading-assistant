#!/usr/bin/env python3
"""Historical Runner V1/#220 portfolio backtest; fail-closed provenance.

Structural 15m 3x3 REACTIONs must originate in the standalone historical replay.
Causal 1h Room + 4h ranks must be independently reconstructed and supplied with
as-of timestamps. This script NEVER fabricates them from present-day snapshots.
No order placement, source DB updates, or live connector calls.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dashboard.services import reaction_lab_historical as hist
from dashboard.services.reaction_lab_portfolio import (
    PortfolioSettings, run_comparison, export_comparison, source_digest)
from dashboard.services.reaction_lab_runner_optimizer import DEFAULT_COSTS


def parity_evidence(summary, parity=None):
    mode=summary.get('mode')
    if mode=='parity':
        return (summary.get('metrics',{}).get('FULL_NUMERIC_PARITY') is True
                and summary.get('journal_hash_equal') is True
                and summary.get('journal_events',0)>0
                and summary.get('symbols_failed')==0 and summary.get('data_gaps')==0)
    if mode=='gap' and parity is not None:
        return (parity.get('mode')=='parity' and parity.get('canonical_hash')==summary.get('canonical_hash')
                and parity.get('step_minutes')==summary.get('step_minutes')
                and parity.get('symbols_total')==summary.get('symbols_total')
                and parity.get('metrics',{}).get('FULL_NUMERIC_PARITY') is True
                and parity.get('journal_hash_equal') is True
                and parity.get('symbols_failed')==0 and parity.get('data_gaps')==0)
    return False


def preflight(args):
    events, summary=hist.load_replay(args.events, args.summary)
    if not events:
        raise ValueError('Replay vacío: no hay REACTIONs estructurales reconstruidas')
    factors=hist.load_factors(args.factors) if args.factors else {}
    candidates, factor_issues=hist.prepare_candidates(
        events,factors,assumed_execution_delay_ms=args.assumed_execution_delay_ms)
    parity_summary=json.loads(args.parity_summary.read_text()) if args.parity_summary else None
    structure_verified=parity_evidence(summary,parity_summary)
    result={'replay_events':len(events),'valid_factor_rows':len(candidates),
            'missing_or_invalid_factor_rows':len(factor_issues),
            'factor_issue_counts':dict(Counter(i['reason'] for i in factor_issues)),
            'replay_symbols_total':summary.get('symbols_total'),
            'replay_symbols_failed':summary.get('symbols_failed'),
            'replay_data_gaps':summary.get('data_gaps'),
            'structural_parity_verified':structure_verified,
            'independent_factor_parity_verified':False,
            'run_ready':bool(candidates) and not factor_issues and structure_verified
                         and summary.get('symbols_failed')==0 and summary.get('data_gaps')==0,
            'historical_validation_ready':False,  # independent numerical factor parity pending
            'timing_policy':'HISTORICAL_POST_CANDLE_BOUNDARY_1MS_PLUS_SCENARIO',
            'post_boundary_guard_ms':hist.POST_BOUNDARY_GUARD_MS,
            'assumed_execution_delay_ms':args.assumed_execution_delay_ms,
            'note':'4h/1h historical numeric parity requires separate verification. ' +
                   'Replay first_replay_observed_ms is a virtual observation, not LIVE first_seen_ms.'}
    return events,summary,candidates,factor_issues,result


def execute(args):
    events,summary,candidates,factor_issues,quality=preflight(args)
    print(json.dumps(quality,ensure_ascii=False,indent=2),flush=True)
    if args.mode=='preflight':return quality
    if args.mode=='template':
        if args.template_output is None:
            raise ValueError('Indicar --template-output para escribir CSV de factores VACÍO')
        if args.template_output.exists():
            raise FileExistsError(f'Never overwrite existing factor template: {args.template_output}')
        hist.write_csv(args.template_output,hist.factor_template(events))
        print('Factor contract (empty values, NOT calculations):',args.template_output)
        return quality
    if args.mode!='run':raise ValueError('Unsupported mode')
    if args.factors is None:
        raise ValueError('Missing --factors: Room 1h and 4h ranks cannot be invented')
    if (factor_issues or summary.get('symbols_failed') or summary.get('data_gaps')) and not args.allow_partial:
        raise ValueError('Historical universe incomplete. See preflight and supply full coverage, '
                         'or use --allow-partial ONLY for explicitly labeled exploratory analysis')
    if not quality['structural_parity_verified'] and not args.research_unverified:
        raise ValueError('Canonical structural replay parity not established. '
                         'Use --research-unverified ONLY for diagnostic research with UNVERIFIED cohort label')
    if not candidates: raise ValueError('No causally populated candidate events')
    if args.max_events:
        candidates=candidates[:args.max_events]
    needed=defaultdict(set)
    for candidate in candidates:
        first=candidate['retest_ts']+hist.MINUTE
        needed[candidate['symbol']].update(first+i*hist.MINUTE for i in range(240))
    cache, cache_info=hist.load_candles_from_cache(args.cache_dir,
                set(needed), needed_timestamps=needed)
    records,path_issues=hist.attach_paths(candidates,cache,length=240)
    quality.update({'selected_candidates':len(candidates),'complete_1m_paths':len(records),
                    'missing_or_invalid_paths':len(path_issues),
                    'path_issue_counts':dict(Counter(i['reason'] for i in path_issues)),
                    'path_cache_files_by_symbol':cache_info})
    if path_issues and not args.allow_partial:
        raise ValueError(f'{len(path_issues)} paths incomplete. Fetch native 1m candle continuation '
                         'through 240m after each retest and rerun. No partial equity written.')
    if not records:raise ValueError('No complete 1m causal event paths')
    if not args.output_root:
        out_root=args.project/'reports/reaction_lab/runner_portfolio_v1'
    else:out_root=args.output_root
    cohort=('HISTORICAL_RESEARCH_UNVERIFIED_OR_PARTIAL' if
            not quality['run_ready'] or path_issues or args.max_events
            else 'HISTORICAL_REPLAY_STRUCTURAL_PARITY_ONLY')
    settings=PortfolioSettings(args.initial_equity,args.margin_fraction,args.leverage).validate()
    costs={'maker_fee_pct':args.maker_fee_pct, 'taker_fee_pct':args.taker_fee_pct,
           'taker_slippage_bps':args.taker_slippage_bps}
    comparison=run_comparison(
        records, settings=settings, costs=costs,
        include_extreme_strength_guard=args.extreme_strength_guard_experiment)
    quality['experimental_extreme_strength_guard_enabled']=bool(args.extreme_strength_guard_experiment)
    quality['source_digest_sha256']=source_digest(records)
    quality['cohort']=cohort
    quality['strategies']={k:v['summary'] for k,v in comparison.items()}
    print(json.dumps({'cohort':cohort,'source_events':len(records),
                      'strategies':quality['strategies']},ensure_ascii=False,indent=2),flush=True)
    if args.dry_run:
        print('DRY RUN: no files written')
        return quality
    label=datetime.now(timezone.utc).strftime('HIST_%Y%m%dT%H%M%S%fZ')+'_'+quality['source_digest_sha256'][:10]
    metadata={'historical_replay_events_sha256':hist.source_sha256(args.events),
              'historical_factors_sha256':hist.source_sha256(args.factors),
              'historical_replay_summary_sha256':hist.source_sha256(args.summary) if args.summary else None,
              'replay_step_minutes':summary.get('step_minutes'),
              'causal_first_observation':'VIRTUAL_HISTORICAL_REPLAY_CLOCK',
              'timing_policy':'HISTORICAL_POST_CANDLE_BOUNDARY_1MS_PLUS_SCENARIO',
              'post_boundary_guard_ms':hist.POST_BOUNDARY_GUARD_MS,
              'assumed_execution_delay_ms':args.assumed_execution_delay_ms,
              'context':'HISTORICAL_ASOF_DERIVED_NOT_NUMERICALLY_PROVEN_LIVE',
              'experimental_extreme_strength_guard_enabled':bool(args.extreme_strength_guard_experiment),
              'quality':{k:v for k,v in quality.items() if k!='strategies'},
              'warnings':['Research only; virtual first observed time, no real latency evidence',
                          'Candle-boundary guard and hypothetical execution delay are scenario assumptions; no live latency replication',
                          'Factor numeric parity not established',
                          'Portfolio engine estimates fills with native 1m OHLC; no funding/liquidation']}
    dest=export_comparison(comparison,records,out_root/label,provenance=metadata,cohort=cohort)
    excursion=hist.extrema_report(comparison)
    hist.write_csv(dest/'historical_excursions.csv',excursion)
    hist.write_csv(dest/'historical_factor_issues.csv',factor_issues)
    hist.write_csv(dest/'historical_path_issues.csv',path_issues)
    (dest/'historical_quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n')
    print('Saved NEW HISTORICAL research run:',dest,flush=True)
    return quality


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('preflight','template','run'),default='preflight')
    p.add_argument('--project',type=Path,default=Path('/home/multiservices-1m'))
    p.add_argument('--events',type=Path,required=True,
                   help='replay_events.csv from canonical replay research/parity/gap')
    p.add_argument('--summary',type=Path,help='summary.json belonging to exact replay')
    p.add_argument('--factors',type=Path,help='Independently reconstructed causal 1h/4h factor CSV')
    p.add_argument('--parity-summary',type=Path,help='Verified parity summary for gap replay')
    p.add_argument('--template-output',type=Path,help='Write empty factor contract; never fill from current snapshots')
    p.add_argument('--cache-dir',type=Path,help='Read-only native Binance 1m JSON candle cache')
    p.add_argument('--output-root',type=Path)
    p.add_argument('--research-unverified',action='store_true',help='Clearly label unverified structural replay')
    p.add_argument('--allow-partial',action='store_true',help='Clearly label incomplete-universe research')
    p.add_argument('--max-events',type=int,default=0,help='SMOKE TEST ONLY; not full sample')
    p.add_argument('--assumed-execution-delay-ms',type=int,default=0,
                   help='Historical-only scenario delay after known causal inputs (ms). Always adds 1ms boundary guard; NOT learned from LIVE.')
    p.add_argument('--initial-equity',type=float,default=200)
    p.add_argument('--margin-fraction',type=float,default=.8)
    p.add_argument('--leverage',type=float,default=3.)
    p.add_argument('--maker-fee-pct',type=float,default=DEFAULT_COSTS['maker_fee_pct'])
    p.add_argument('--taker-fee-pct',type=float,default=DEFAULT_COSTS['taker_fee_pct'])
    p.add_argument('--taker-slippage-bps',type=float,default=DEFAULT_COSTS['taker_slippage_bps'])
    p.add_argument('--extreme-strength-guard-experiment',action='store_true',
                   help='OPT-IN third isolated #220 portfolio. Block SHORT only when return_rank_4h>=95 AND volume_rank_4h>=90. In-sample exploratory, never LIVE.')
    p.add_argument('--dry-run',action='store_true')
    return p


if __name__=='__main__':
    args=parser().parse_args()
    if args.max_events<0:raise SystemExit('--max-events must be >=0')
    if args.assumed_execution_delay_ms<0:raise SystemExit('--assumed-execution-delay-ms must be >=0')
    if args.mode=='run' and args.cache_dir is None:raise SystemExit('--cache-dir required for --mode run')
    try:execute(args)
    except (FileNotFoundError,ValueError,FileExistsError) as exc:
        print('ERROR:',exc,file=sys.stderr)
        sys.exit(2)
