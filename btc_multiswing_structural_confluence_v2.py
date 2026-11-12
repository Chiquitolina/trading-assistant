#!/usr/bin/env python3
"""BTC 60D Multi-Swing Structural Confluence V2: candidate regions, PRE only.

This is a descriptive geometric inspector, NOT a support/resistance detector,
trade filter, HLL level, defense or recovery classifier. It never reads POST.
Input is the validated frozen 6TF catalog via `asof_rows` from the V1 explorer.

One physical extremum is counted once, even when confirmed by many TFs.
A region is born only at the CLOSED confirmation of the second independent
physical extremum meeting the selected price/time criteria. Each subsequent
stage is time stamped so earlier geometry is never rewritten by future pivots.
TIED_EXTREMA are excluded rather than being assigned a fake physical identity.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pandas as pd

from btc_multiswing_60d_explorer_v1 import DAY, M, group_asof

_ALLOWED = {'LOW', 'HIGH'}


@dataclass
class _Candidate:
    side: str
    first_id: str
    first_price: float
    first_pivot_ts: int
    members: list[dict] = field(default_factory=list)
    born_ts: int | None = None
    stages: list[dict] = field(default_factory=list)
    # V3.1.4: cached incremental extrema. These are derived solely from
    # confirmed members and replace repeated O(member_count) list creation.
    member_times: list[int] = field(default_factory=list)
    min_member_ts: int | None = None
    max_member_ts: int | None = None
    min_member_price: float | None = None
    max_member_price: float | None = None
    sum_member_price: float = 0.0


def _region_id(side: str, first_id: str, second_id: str) -> str:
    raw = f'BTCUSDT|MULTISWING_V2|{side}|{first_id}|{second_id}'
    return 'msv2_' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


def _price_spread_ok(prices: list[float], new_price: float, tolerance_pct: float) -> bool:
    lo, hi = min([*prices, new_price]), max([*prices, new_price])
    midpoint = (lo + hi) / 2
    return midpoint > 0 and (hi - lo) / midpoint * 100 <= tolerance_pct + 1e-12


def _unique_confirmed(known: pd.DataFrame, cutoff: int) -> list[dict]:
    """Only verified distinct extrema, using their *first as-of* confirmation."""
    if known.empty:
        return []
    needed = {'pivot_id','side','price','available_ts','physical_id',
              'physical_identity_state','physical_minute_ts','timeframe','pivot_ts'}
    if not needed.issubset(known.columns):
        raise ValueError('MISSING_CAUSAL_SWING_COLUMNS')
    if known['available_ts'].isna().any() or (known['available_ts'] > cutoff).any():
        raise ValueError('FUTURE_SWING_INPUT_NOT_ASOF')
    group = group_asof(known, collapse=True, multi_only=False)
    verified = group[(group.physical_identity_state == 'VERIFIED') & group.physical_id.ne('')].copy()
    if verified.empty:
        return []
    # V1 group_asof enforces same physical side, position and price for all TFs.
    result = []
    for p in verified.itertuples(index=False):
        if p.side not in _ALLOWED:
            raise ValueError('INVALID_SWING_SIDE')
        when = int(p.first_available_asof)
        where = int(p.physical_minute_ts)
        if when > cutoff or where >= when:
            raise ValueError('FUTURE_OR_INVALID_PHYSICAL_EXTREMUM')
        result.append({
            'physical_id': str(p.physical_id), 'side': str(p.side),
            'price': float(p.price), 'pivot_ts': where,
            'known_ts': when, 'tf_set_at_cutoff': str(p.tf_set_asof),
        })
    return sorted(result, key=lambda e:(e['known_ts'], e['pivot_ts'], e['physical_id']))


def construct_regions(known: pd.DataFrame, cutoff: int, *,
                      tolerance_pct: float = 0.20,
                      lookback_days: int = 7,
                      min_separation_minutes: int = 60,
                      min_extrema: int = 2) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Incremental PRE-only region formation from already-confirmed swings.

    Every physical extremum is assigned to at most one candidate cluster.
    Zones never mix HIGH and LOW. The band's recorded stages only reflect
    independently confirmed members available at each stage's known_ts.

    Price tolerance is MAX allowed spread between extrema, not +/- radius.
    Last-stage display ends after the lookback horizon without fresh confirmation;
    this is only a VISUAL horizon, never a lifecycle claim.
    Lookback bounds physical pivot-time span of a cluster. Separation rejects
    near-simultaneous extrema as independent structural confirmations.
    """
    if not (0 < tolerance_pct <= 2.0 and 1 <= lookback_days <= 60 and
            1 <= min_separation_minutes <= 24*60 and 2 <= min_extrema <= 10):
        raise ValueError('INVALID_REGION_PARAMETERS')
    events = _unique_confirmed(known, int(cutoff))
    if not events:
        empty = pd.DataFrame(columns=[
            'region_id','side','center','band_low','band_high','first_pivot_ts',
            'last_pivot_ts','born_ts','last_confirmation_ts','independent_extrema',
            'members','temporalities','status'])
        stages = pd.DataFrame(columns=['region_id','known_from','known_until',
                                       'band_low','band_high','independent_extrema','side'])
        return empty, stages, {'physical_extrema_evaluated':0,'ambiguous_excluded':int(len(known)),
                               'candidate_regions':0,'descriptive_only':True}
    clusters: list[_Candidate] = []
    interval = int(lookback_days)*DAY
    gap = int(min_separation_minutes)*M
    ignored_near_time = 0
    for event in events:
        price, ts = event['price'], event['pivot_ts']
        options = []
        close_without_gap = False
        for cluster in clusters:
            if cluster.side != event['side']:
                continue
            # The candidate's price/time extrema are updated at each closed
            # confirmation. Never inspect unconfirmed future members.
            if (max(int(cluster.max_member_ts), ts) -
                    min(int(cluster.min_member_ts), ts) > interval):
                continue
            lo = min(float(cluster.min_member_price), price)
            hi = max(float(cluster.max_member_price), price)
            if (hi - lo) / ((lo + hi) / 2) * 100 > tolerance_pct + 1e-12:
                continue
            if any(abs(ts - other) < gap for other in cluster.member_times):
                close_without_gap = True
                continue
            center = cluster.sum_member_price/len(cluster.members)
            options.append((abs(price-center)/center,cluster.first_pivot_ts,cluster.first_id,cluster))
        if options:
            cluster = min(options, key=lambda o:o[:3])[3]
        elif close_without_gap:
            ignored_near_time += 1
            continue
        else:
            cluster = _Candidate(event['side'],event['physical_id'],price,ts)
            clusters.append(cluster)
        cluster.members.append(event)
        cluster.member_times.append(ts)
        cluster.min_member_ts = ts if cluster.min_member_ts is None else min(cluster.min_member_ts, ts)
        cluster.max_member_ts = ts if cluster.max_member_ts is None else max(cluster.max_member_ts, ts)
        cluster.min_member_price = price if cluster.min_member_price is None else min(cluster.min_member_price, price)
        cluster.max_member_price = price if cluster.max_member_price is None else max(cluster.max_member_price, price)
        cluster.sum_member_price += price
        if len(cluster.members) >= min_extrema:
            if cluster.born_ts is None:
                cluster.born_ts = event['known_ts']
            prices = [m['price'] for m in cluster.members]
            cluster.stages.append({
                'known_from':int(event['known_ts']),
                'band_low':min(prices), 'band_high':max(prices),
                'independent_extrema':len(cluster.members), 'side':cluster.side,
            })
    regions = []
    stage_rows = []
    for cluster in clusters:
        if len(cluster.members) < min_extrema:
            continue
        members = cluster.members
        prices = [m['price'] for m in members]
        rid = _region_id(cluster.side,members[0]['physical_id'],members[1]['physical_id'])
        timestamps = [m['pivot_ts'] for m in members]
        tfs = sorted(set(','.join(m['tf_set_at_cutoff'] for m in members).split(',')))
        regions.append({
            'region_id':rid, 'side':cluster.side,
            'center':sum(prices)/len(prices),
            'band_low':min(prices),'band_high':max(prices),
            'first_pivot_ts':min(timestamps),'last_pivot_ts':max(timestamps),
            'born_ts':int(cluster.born_ts),'last_confirmation_ts':max(m['known_ts'] for m in members),
            'independent_extrema':len(members),
            'members':','.join(m['physical_id'] for m in members),
            'temporalities':','.join(tfs),
            'status':'GEOMETRIC_CANDIDATE_NOT_ACTIVE',
        })
        ordered = cluster.stages
        for i, stage in enumerate(ordered):
            known_until = (int(ordered[i+1]['known_from']) if i+1<len(ordered)
                           else min(int(cutoff), int(stage['known_from']) + interval))
            stage_rows.append({**stage, 'region_id':rid, 'known_until':known_until})
    region_frame = pd.DataFrame(regions, columns=[
        'region_id','side','center','band_low','band_high','first_pivot_ts','last_pivot_ts',
        'born_ts','last_confirmation_ts','independent_extrema','members','temporalities','status'])
    stage_frame = pd.DataFrame(stage_rows, columns=[
        'region_id','known_from','known_until','band_low','band_high','independent_extrema','side'])
    if not region_frame.empty:
        region_frame = region_frame.sort_values(['born_ts','side','region_id']).reset_index(drop=True)
    if not stage_frame.empty:
        stage_frame = stage_frame.sort_values(['known_from','side','region_id']).reset_index(drop=True)
    return region_frame,stage_frame,{
        'physical_extrema_evaluated':len(events),
        'ambiguous_excluded':int(known[known.physical_identity_state.ne('VERIFIED')]['pivot_id'].nunique()),
        'candidate_regions':len(region_frame),
        'near_time_extrema_skipped':ignored_near_time,
        'descriptive_only':True,
    }


def visible_region_stages(stages: pd.DataFrame, start: int, end: int, limit: int = 100) -> pd.DataFrame:
    """Clip stages in [start,end) without leaking their pre-birth or POST ranges."""
    if not (start < end and 1 <= limit <= 1000):
        raise ValueError('INVALID_REGION_VIEW_WINDOW')
    if stages.empty:
        return stages.copy()
    rows = stages[(stages.known_until > int(start)) & (stages.known_from < int(end))].copy()
    if rows['region_id'].nunique() > limit:
        raise ValueError(f'TOO_MANY_REGIONS_FOR_VISUALIZATION: {rows.region_id.nunique()} > {limit}')
    rows['visible_from'] = rows['known_from'].clip(lower=int(start))
    rows['visible_until'] = rows['known_until'].clip(upper=int(end))
    return rows[rows['visible_from'] < rows['visible_until']]


# V2.1: selection/inspection layer. Never mutates V2 geometric regions or stages.
_REGION_CANDIDATE_COLUMNS = [
    'region_id', 'side', 'center', 'band_low', 'band_high',
    'independent_extrema', 'known_from', 'born_ts', 'last_confirmation_ts',
    'temporalities', 'members', 'status',
]


def visible_region_candidates(regions: pd.DataFrame, stages: pd.DataFrame,
                              start: int, cutoff: int, *,
                              min_extrema: int = 2, side: str = 'BOTH',
                              order: str = 'RECENT') -> pd.DataFrame:
    """List only regions with an eligible causal stage inside the visible window.

    Importantly, display count/price is the *last qualifying stage known by the
    requested cutoff*, NOT the region's retrospectively final count or band.
    Does not rank predictive quality: RECENT and EXTREMA are descriptive orders.
    """
    if (int(start) >= int(cutoff) or min_extrema not in (2, 3, 4, 5)
            or side not in ('BOTH', 'HIGH', 'LOW')
            or order not in ('RECENT', 'EXTREMA')):
        raise ValueError('INVALID_REGION_INSPECTION_FILTER')
    empty = pd.DataFrame(columns=_REGION_CANDIDATE_COLUMNS)
    if regions.empty or stages.empty:
        return empty
    required_regions = {'region_id', 'born_ts', 'last_confirmation_ts',
                        'members', 'temporalities', 'status'}
    required_stages = {'region_id', 'side', 'known_from', 'known_until',
                       'band_low', 'band_high', 'independent_extrema'}
    if not required_regions.issubset(regions.columns) or not required_stages.issubset(stages.columns):
        raise ValueError('INVALID_REGION_INSPECTOR_SCHEMA')
    if (regions.region_id.duplicated().any()
            or (stages.known_until < stages.known_from).any()):
        raise ValueError('INVALID_REGION_INSPECTOR_IDENTITY')
    frame = stages.loc[
        (stages.known_from < cutoff) & (stages.known_until > start)
        & (stages.independent_extrema >= min_extrema)
    ].copy()
    if side != 'BOTH':
        frame = frame[frame.side == side]
    if frame.empty:
        return empty
    # No future stage or final-band leakage: the last *eligible* causal stage
    # supplies the displayed geometry, even when the final region has grown.
    frame = frame.sort_values(['known_from', 'region_id'], kind='mergesort')
    frame = frame.drop_duplicates('region_id', keep='last')
    metadata = regions[list(required_regions)].copy()
    frame = frame.merge(metadata, on='region_id', how='left', validate='one_to_one')
    if frame[list(required_regions - {'region_id'})].isna().any().any():
        raise ValueError('UNKNOWN_REGION_STAGE_ID')
    if (frame.born_ts > frame.known_from).any():
        raise ValueError('REGION_STAGE_BEFORE_BIRTH')
    frame['center'] = (frame['band_low'].astype(float) + frame['band_high'].astype(float)) / 2
    if order == 'RECENT':
        by, asc = ['known_from', 'independent_extrema', 'region_id'], [False, False, True]
    else:
        by, asc = ['independent_extrema', 'known_from', 'region_id'], [False, False, True]
    return frame.sort_values(by, ascending=asc, kind='mergesort').loc[:,
        _REGION_CANDIDATE_COLUMNS].reset_index(drop=True)


def display_region_ids(candidates: pd.DataFrame, maximum: int,
                       selected_region_id: str = '') -> tuple[list[str], dict]:
    """Select geometric overlays only; never truncate data or alter V2 state."""
    if maximum not in (5, 10, 20, 40):
        raise ValueError('INVALID_REGION_DRAW_LIMIT')
    if candidates.empty:
        if selected_region_id:
            raise ValueError('SELECTED_REGION_NOT_VISIBLE_ASOF')
        return [], {'eligible_regions':0,'drawn_regions':0,'hidden_by_display_limit':0}
    all_ids = candidates.region_id.astype(str).tolist()
    if selected_region_id and selected_region_id not in all_ids:
        raise ValueError('SELECTED_REGION_NOT_VISIBLE_ASOF')
    shown = [selected_region_id] if selected_region_id else all_ids[:maximum]
    return shown, {
        'eligible_regions':len(all_ids),
        'drawn_regions':len(shown),
        'hidden_by_display_limit':0 if selected_region_id else max(0,len(all_ids)-len(shown)),
    }


def region_member_evidence(known: pd.DataFrame, region_row: pd.Series,
                           cutoff: int) -> pd.DataFrame:
    """Constituent verified physical extrema known as-of cutoff, never POST."""
    cols = ['physical_id', 'side', 'price', 'plot_ts', 'first_available_asof',
            'last_available_asof', 'tf_set_asof', 'representations_asof']
    if known.empty:
        raise ValueError('REGION_MEMBER_EVIDENCE_MISSING')
    if (known.available_ts.isna().any() or (known.available_ts > int(cutoff)).any()):
        raise ValueError('FUTURE_REGION_MEMBER_EVIDENCE')
    ids = str(region_row['members']).split(',')
    if not ids or any(not s for s in ids) or len(ids)!=len(set(ids)):
        raise ValueError('INVALID_REGION_MEMBERS')
    grouped = group_asof(known, collapse=True)
    eligible = grouped[
        grouped.physical_identity_state.eq('VERIFIED') & grouped.physical_id.isin(ids)
    ].copy()
    if len(eligible)!=len(ids) or set(eligible.physical_id)!=set(ids):
        raise ValueError('REGION_MEMBER_EVIDENCE_MISSING')
    if ((eligible.first_available_asof > int(cutoff))
            | (eligible.last_available_asof > int(cutoff))).any():
        raise ValueError('FUTURE_REGION_MEMBER_EVIDENCE')
    if eligible.side.nunique()!=1 or eligible.side.iloc[0]!=region_row['side']:
        raise ValueError('REGION_MEMBER_SIDE_CONFLICT')
    return eligible[cols].sort_values(
        ['first_available_asof','plot_ts','physical_id'],kind='mergesort'
    ).reset_index(drop=True)
