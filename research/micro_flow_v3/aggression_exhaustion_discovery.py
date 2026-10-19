"""Standalone BUY/SELL aggressive-impulse and failure *discovery*.

This is NOT an auto-trading strategy and does NOT claim to observe absorption.
IMPULSE, WEAKENING, and FAILURE are distinct time-ordered *observable* stages;
a failure is not required to have registered weakening. Unconfirmed and censored
cases are retained. Entry references/outcomes are descriptive and future-only.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import uuid

from research.micro_flow_v3.swing_sweep_discovery import DiscoveryError, validate_buckets, file_sha256
from research.micro_flow_v3.aggression_features import effort_snapshot
from research.micro_flow_v3.sweep_entry_research import _next_open_reference, _entry_outcomes

SCHEMA='micro-flow-v3-aggression-exhaustion-discovery-v1'


def _stage(row, name):
    return {
        'stage':name,
        'observed_second_start_ms':row['second_start_ms'],
        'available_wall_ns':row['available_wall_ns'],
        'available_ms':(row['available_wall_ns']+999_999)//1_000_000,
        'observed_close':row['close'],
        'first_seq':row['first_seq'], 'last_seq':row['last_seq'],
    }


def discover_exhaustion(buckets, *, window_seconds=5, baseline_buckets=30,
                        relative_volume=2.0, min_imbalance=0.70, min_trades=3,
                        cooldown_seconds=60, followup_seconds=300,
                        weakening_relative_multiplier=.70,
                        weakening_imbalance_multiplier=.80,
                        weakening_max_signed_move_pct=.01,
                        failure_beyond_start_pct=.01,
                        max_observation_gap_seconds=3,
                        entry_freshness_seconds=3, outcome_freshness_seconds=3):
    """Causal independent impulse cohort. Features never require a swing level.

    IMPULSE: elevated executed volume + directional imbalance (time T).
    WEAKENING: a *later* aligned-pressure window with little signed progress.
    FAILURE: a *later* complete 1s close beyond the pre-impulse OPEN in the
             opposite direction, by a fixed exploratory tolerance.
    Both later labels can independently be absent, so avoid success selection.
    """
    if not (1 <= window_seconds <= 60 and baseline_buckets >= 10 and
            relative_volume > 0 and 0 < min_imbalance <= 1 and min_trades >= 1 and
            cooldown_seconds >= 0 and followup_seconds >= 1 and
            0 < weakening_relative_multiplier <= 1 and
            0 < weakening_imbalance_multiplier <= 1 and
            0 <= weakening_max_signed_move_pct <= 1 and
            0 <= failure_beyond_start_pct <= 1 and
            max_observation_gap_seconds >= 1):
        raise DiscoveryError('Invalid exhaustion research parameters')
    rows=validate_buckets(buckets)
    source_buckets_sha256=sha256(json.dumps(rows,sort_keys=True,separators=(',', ':'),
                         allow_nan=False).encode('utf8')).hexdigest()
    segments=defaultdict(list)
    for row in rows:
        segments[row['segment_id']].append(row)
    events=[]
    for seg,data in segments.items():
        starts=[r['second_start_ms'] for r in data]
        # Calculate every snapshot independent of the future. Last bucket at i
        # is published before we can register an impulse at i.
        features=[effort_snapshot(data,starts,i,window_seconds=window_seconds,
                    baseline_buckets=baseline_buckets,
                    max_observation_gap_seconds=max_observation_gap_seconds)
                  for i in range(len(data))]
        last_accepted={'BUY':-10**30,'SELL':-10**30}
        for i,snapshot in enumerate(features):
            if snapshot is None:
                continue
            side=snapshot['pressure_side']
            if (side not in ('BUY','SELL') or
                snapshot['relative_volume'] < relative_volume or
                abs(snapshot['imbalance']) < min_imbalance or
                snapshot['trades'] < min_trades or
                starts[i]-last_accepted[side]<cooldown_seconds*1000):
                continue
            last_accepted[side]=starts[i]
            direction=1 if side=='BUY' else -1
            initial=snapshot['window_open_price']
            cutoff=starts[i]+followup_seconds*1000
            weakening, failure = None,None
            stop_reason='FOLLOWUP_WINDOW_COMPLETED'
            saw_window_end=False
            last_i=i
            for j in range(i+1,len(data)):
                if starts[j] > cutoff:
                    saw_window_end=True
                    break
                if starts[j]-starts[last_i] > max_observation_gap_seconds*1000:
                    stop_reason='CENSORED_OBSERVATION_GAP'
                    break
                last_i=j
                curr=data[j]
                # These are separate labels: a failure can precede weakening.
                if failure is None and direction*(float(curr['close'])/initial-1)*100 <= -failure_beyond_start_pct:
                    failure=_stage(curr,'AGGRESSION_FAILURE_OBSERVED')
                later=features[j]
                if (weakening is None and later is not None and
                        later['window_start_ms']>starts[i] and
                        later['pressure_side']==side and
                        later['relative_volume'] >= relative_volume*weakening_relative_multiplier and
                        abs(later['imbalance']) >= min_imbalance*weakening_imbalance_multiplier and
                        later['pressure_signed_displacement_pct'] <= weakening_max_signed_move_pct):
                    weakening=_stage(curr,'EFFORT_WITHOUT_PRICE_PROGRESS_OBSERVED')
                # Do not use any future observation to backdate milestone T.
            else:
                stop_reason='CENSORED_SESSION_END'
            if saw_window_end:
                stop_reason='FOLLOWUP_WINDOW_COMPLETED'
            if stop_reason=='CENSORED_SESSION_END' and starts[last_i] >= cutoff:
                stop_reason='FOLLOWUP_WINDOW_COMPLETED'
            entry_ref,outcomes=None,{}
            entry_state='NO_FAILURE_SIGNAL'
            if failure is not None:
                # Locate using publication time, not the highest tick of a bucket.
                k,entry_ref=_next_open_reference(data,starts,failure,
                                          max_lag_seconds=entry_freshness_seconds)
                if entry_ref is None:
                    entry_state='CENSORED_NO_FRESH_NEXT_SECOND'
                else:
                    entry_state='OBSERVED_REFERENCE_NOT_FILL'
                    outcomes=_entry_outcomes(data,starts,k,entry_ref['entry_reference_price'],
                                'HIGH' if side=='BUY' else 'LOW',
                                (30,60,180,300),outcome_freshness_seconds)
            events.append({
                'event_id':f'{seg}:EXHAUST:{side}:{starts[i]}',
                'segment_id':seg,'pressure_side':side,
                'hypothesis_direction':'SHORT' if side=='BUY' else 'LONG',
                'impulse':_stage(data[i],'AGGRESSION_IMPULSE_OBSERVED'),
                'impulse_features':snapshot,
                'initial_displacement_state':('DIRECTIONAL_EXPANSION' if
                    snapshot['pressure_signed_displacement_pct']>0.01 else 'LITTLE_OR_NO_PROGRESS'),
                'weakening':weakening,'failure':failure,
                'followup_state':stop_reason,
                'entry_state':entry_state,'entry_reference':entry_ref,
                'outcomes':outcomes,
                'interpretation':'EXPLORATORY_AGGRESSION_NOT_PROVEN_ABSORPTION',
            })
    events.sort(key=lambda e:(e['impulse']['available_wall_ns'],e['event_id']))
    return {
        'schema':SCHEMA,
        'parameters':{
            'window_seconds':window_seconds,'baseline_buckets':baseline_buckets,
            'relative_volume':relative_volume,'min_imbalance':min_imbalance,
            'min_trades':min_trades,'cooldown_seconds':cooldown_seconds,
            'followup_seconds':followup_seconds,
            'weakening_relative_multiplier':weakening_relative_multiplier,
            'weakening_imbalance_multiplier':weakening_imbalance_multiplier,
            'weakening_max_signed_move_pct':weakening_max_signed_move_pct,
            'failure_beyond_start_pct':failure_beyond_start_pct,
            'max_observation_gap_seconds':max_observation_gap_seconds,
            'entry_freshness_seconds':entry_freshness_seconds,
            'outcome_freshness_seconds':outcome_freshness_seconds,
            'horizons_seconds':[30,60,180,300],
        },
        'events':events,
        'source_buckets_sha256':source_buckets_sha256,
        'summary':{'impulses':len(events),
                   'buy_impulses':sum(e['pressure_side']=='BUY' for e in events),
                   'sell_impulses':sum(e['pressure_side']=='SELL' for e in events),
                   'weakening_observed':sum(e['weakening'] is not None for e in events),
                   'failure_observed':sum(e['failure'] is not None for e in events),
                   'failure_with_entry_reference':sum(e['entry_reference'] is not None for e in events),
                   'censored_followup':sum(e['followup_state']!='FOLLOWUP_WINDOW_COMPLETED' for e in events),
        },
    }
