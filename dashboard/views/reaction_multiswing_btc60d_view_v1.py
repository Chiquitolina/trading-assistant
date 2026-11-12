"""Visor read-only BTC 60D Multi-Swing dentro de Reaction & Swing Lab.

No reconstruye pivotes al abrir el dashboard. Solo lee freeze y catálogo 6TF
verificados, agrupando identidades físicas *as-of* antes de cada corte.
POST es exclusivamente visual: nunca incorpora swings confirmados después del
corte seleccionado. Los paneles multi-TF comparten el mismo instante causal,
pero NO conectan todavía HLL, REACTION, órdenes ni LIVE.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from collections import OrderedDict
from threading import RLock
import math
import time as _perf_time
import re
import numpy as np
from pathlib import Path

from btc_multiswing_structural_confluence_v2 import (
    construct_regions, visible_region_stages, visible_region_candidates,
    display_region_ids, region_member_evidence,
)

from btc_multiswing_region_interactions_v22 import (
    audit_fixed_region, later_compatible_extrema,
)

from btc_multiswing_structural_visits_v23 import (
    audit_structural_visits, visit_gap_sensitivity,
)

from btc_multiswing_60d_explorer_v1 import (
    DAY, M, TF_COLORS, TF_MINUTES, aggregate_closed, asof_rows,
    chart_window, group_asof, iso, load_snapshot, selected_inspection_window,
    window_evidence_stats,
)


# V3.1.3 — Bounded read-only caches for verified frozen datasets only.
# Streamlit reruns the whole script after a selector changes. The source loader
# already caches disk reads; these caches avoid repeating geometric detection,
# repeated physical grouping, the retrospective navigation scan, and full-history
# 1m->HTF resampling. Each PRE cutoff is a distinct key — no lookahead reuse.
_V313_CACHE_LOCK = RLock()
_V313_CACHES: dict[str, OrderedDict] = {
    'regions': OrderedDict(),
    'group': OrderedDict(),
    'stats': OrderedDict(),
    'events': OrderedDict(),
    'bars': OrderedDict(),
}
_V313_CAPACITY = {'regions': 12, 'group': 8, 'stats': 12,
                  'events': 12, 'bars': 6}


def v313_verified_cache_source(source: dict) -> bool:
    """Cache only immutable, hash-verified freezes; never fake/test/live data."""
    if not isinstance(source, dict):
        return False
    return all(bool(re.fullmatch(r'[0-9a-fA-F]{64}', str(source.get(k, ''))))
               for k in ('source_sha256', 'catalog_sha256'))


def v313_memo(kind: str, key: tuple, refs: tuple, compute, *, enabled: bool):
    """Process-local bounded cache, keyed by exact inputs and object identity.

    Strong references prevent id reuse. Results must be treated as read-only;
    all render-specific sorting/filtering must return new frames. No network,
    filesystem mutation, or Streamlit state is owned by this cache.
    """
    if kind not in _V313_CACHES:
        raise ValueError('INVALID_V313_CACHE_KIND')
    if not enabled:
        return compute()
    cache=_V313_CACHES[kind]
    with _V313_CACHE_LOCK:
        item=cache.get(key)
        if item is not None and len(item[0]) == len(refs) and all(
                obj is expected for obj,expected in zip(item[0],refs)):
            cache.move_to_end(key)
            return item[1]
    value=compute()
    with _V313_CACHE_LOCK:
        cache[key]=(tuple(refs),value)
        cache.move_to_end(key)
        while len(cache)>_V313_CAPACITY[kind]:
            cache.popitem(last=False)
    return value


def v313_region_build(catalog, known, *, cutoff: int, selected_tfs: tuple,
                       side: str, tolerance_pct: float, lookback_days: int,
                       separation: int, enabled: bool):
    key=(id(catalog),int(cutoff),tuple(selected_tfs),str(side),
         float(tolerance_pct),int(lookback_days),int(separation))
    return v313_memo('regions',key,(catalog,),lambda:construct_regions(
        known,cutoff,tolerance_pct=tolerance_pct,
        lookback_days=lookback_days,min_separation_minutes=separation),
        enabled=enabled)


def v313_group_build(catalog, known, *, cutoff: int, selected_tfs: tuple,
                     side: str, collapse: bool, multi_only: bool,
                     enabled: bool):
    key=(id(catalog),int(cutoff),tuple(selected_tfs),str(side),
         bool(collapse),bool(multi_only))
    return v313_memo('group',key,(catalog,),lambda:group_asof(
        known,collapse=collapse,multi_only=multi_only),enabled=enabled)


def v313_stats_build(catalog, known, *, cutoff: int, start: int,
                      selected_tfs: tuple, side: str, multi_only: bool,
                      enabled: bool):
    key=(id(catalog),int(cutoff),int(start),tuple(selected_tfs),
         str(side),bool(multi_only))
    return v313_memo('stats',key,(catalog,),lambda:window_evidence_stats(
        known,start,cutoff,multi_only=multi_only),enabled=enabled)


def v313_events_build(full, stages, *, region_id: str, source_end: int,
                       gap_minutes: int, enabled: bool):
    key=(id(full),id(stages),str(region_id),int(source_end),int(gap_minutes))
    return v313_memo('events',key,(full,stages),
        lambda:v33_structural_event_index(full,stages,region_id=region_id,
            source_end=source_end,gap_minutes=gap_minutes),enabled=enabled)


def v313_preaggregated(full, tf: str, *, enabled: bool):
    """Precompute complete, UTC-aligned OHLC once per TF per frozen snapshot.

    It is safe to precompute from the offline full history ONLY because
    v313_slice_bars strictly removes any candle whose close exceeds the chosen
    PRE/POST boundary. No future-derived feature or swing is added.
    """
    if tf not in TF_MINUTES:
        raise ValueError('INVALID_V313_TIMEFRAME')
    key=(id(full),str(tf))
    return v313_memo('bars',key,(full,),lambda:aggregate_closed(
        full,tf,int(full.timestamp.iloc[0]),int(full.timestamp.iloc[-1])+M),
        enabled=enabled)


def v313_slice_bars(preaggregated, tf: str, start: int, end: int):
    """Equivalent to aggregate_closed(...,tf,start,end), no partial candle."""
    if tf not in TF_MINUTES or int(end)<=int(start):
        raise ValueError('INVALID_CHART_INTERVAL')
    step=TF_MINUTES[tf]*M
    return preaggregated.loc[(preaggregated.timestamp+step>int(start)) &
                             (preaggregated.timestamp+step<=int(end))].copy().reset_index(drop=True)


def v313_render_bars(full, tf: str, start: int, end: int, *, enabled: bool):
    if not enabled:
        return aggregate_closed(full,tf,start,end)
    return v313_slice_bars(v313_preaggregated(full,tf,enabled=True),tf,start,end)


def build_figure(candles, swings, *, display_tf: str, selected=None, cutoff=None,
                 chart_end=None, candle_x_at_close: bool = False):
    import pandas as pd
    import plotly.graph_objects as go
    if candles.empty:
        raise ValueError('NO_CANDLES_TO_DRAW')
    fig = go.Figure()
    # For execution replay, place HTF candles at their CLOSE timestamps.
    # A 4h candle 12:00–16:00 may only appear at 16:00, never left of a
    # 13:30 PRE boundary when its POST content is revealed.
    xstamps=(candles.timestamp + TF_MINUTES[display_tf]*M
             if candle_x_at_close else candles.timestamp)
    fig.add_trace(go.Candlestick(
        x=pd.to_datetime(xstamps,unit='ms',utc=True),
        open=candles.open, high=candles.high, low=candles.low,close=candles.close,
        name=f'BTCUSDT {display_tf}', increasing_line_color='#43ae90',
        decreasing_line_color='#df7070', showlegend=False,
    ))
    for tf in TF_MINUTES:
        for side in ('HIGH','LOW'):
            subset = swings[(swings.timeframe == tf) & (swings.side == side)]
            if subset.empty:
                continue
            xpos = pd.to_datetime(subset['plot_ts'],unit='ms',utc=True)
            labels = [
                f"{tf} {side} · {float(row.price):,.2f} USD"
                f"<br>Extremo: {iso(int(row.plot_ts))}"
                f"<br>Primera confirmación conocida: {iso(int(row.first_available_asof))}"
                f"<br>Última confirmación conocida: {iso(int(row.last_available_asof))}"
                f"<br>Temporalidades PRE: {row.tf_set_asof}"
                f"<br>Identidad: {row.physical_identity_state}"
                for row in subset.itertuples(index=False)
            ]
            fig.add_trace(go.Scattergl(
                x=xpos,y=subset.price, mode='markers',
                marker={'symbol':'triangle-down' if side=='HIGH' else 'triangle-up',
                        'size':8 if tf in ('1m','5m') else 10,
                        'color':TF_COLORS[tf], 'opacity':0.78},
                hovertext=labels, hoverinfo='text',
                name=f'{tf} {side}',
            ))
    if selected is not None:
        stamp=int(selected['plot_ts'])
        fig.add_trace(go.Scatter(
            x=[pd.to_datetime(stamp,unit='ms',utc=True)],
            y=[float(selected['price'])],mode='markers',
            marker={'symbol':'star','size':18,'color':'#f3ce50',
                    'line':{'width':1,'color':'#f8f8f8'}},
            name='Pivote seleccionado',
            hovertext=f"Seleccionado · {iso(stamp)} · {selected['tf_set_asof']}",
            hoverinfo='text',
        ))
    if cutoff is not None and chart_end is not None and int(chart_end)>int(cutoff):
        dt=pd.to_datetime(int(cutoff),unit='ms',utc=True)
        fig.add_shape(type='line',xref='x',yref='paper',x0=dt,x1=dt,
                      y0=0,y1=1,line={'color':'#f2c66d','width':2,'dash':'dash'})
        fig.add_annotation(x=dt,y=1,xref='x',yref='paper',text='Corte PRE',showarrow=False,
                           yanchor='bottom',font={'color':'#f2c66d','size':11})
    fig.update_layout(
        title=f'BTCUSDT · Multi-Swing histórico · velas {display_tf}',
        height=690, hovermode='closest', xaxis_rangeslider_visible=False,
        margin={'l':25,'r':25,'t':48,'b':25},legend={'orientation':'h','y':-0.12},
        xaxis_title='UTC',yaxis_title='Precio USD',
    )
    return fig


def _selected_cutoff_date(last_exclusive: int):
    return datetime.fromtimestamp((last_exclusive-M)/1000,timezone.utc).date()


def v22_visual_context_window(*, source_start: int, cutoff: int,
                              stage_known_from: int, context: str) -> tuple[int, int]:
    """Solo limites de DIBUJO, nunca los limites de la auditoria V2.2.

    El contexto completo empieza en el inicio del freeze principal BTC 60D
    (no incluye el warmup). La banda de la etapa seleccionada solo es
    representable desde `stage_known_from`; sus velas anteriores son contexto.
    """
    if context not in ('FULL_60D', 'FROM_STAGE', 'LAST_7D'):
        raise ValueError('INVALID_V22_VISUAL_CONTEXT')
    source_start, cutoff, stage_known_from = (
        int(source_start), int(cutoff), int(stage_known_from))
    if not (source_start < cutoff and source_start <= stage_known_from <= cutoff):
        raise ValueError('INVALID_V22_VISUAL_TIMELINE')
    if context == 'FULL_60D':
        left = source_start
    elif context == 'FROM_STAGE':
        left = stage_known_from
    else:
        left = max(source_start, cutoff - 7 * DAY)
    return left, cutoff


V3_MODES = ('MAPA', 'INSPECTOR', 'EJECUCION')


def v3_view_policy(mode: str) -> dict:
    """UI-only policy. No impact on causal evidence or research datasets."""
    if mode not in V3_MODES:
        raise ValueError('INVALID_MULTISWING_UI_MODE')
    return {
        'draw_all_pivots_by_default': False,
        'show_region_audit': mode == 'INSPECTOR',
        'allow_post_inspection': mode == 'INSPECTOR',
        'read_only_execution_preview': mode == 'EJECUCION',
    }


def v3_rank_regions_near_price(candidates, price: float):
    """Display ranking only: proximity to last known PRE close, not quality."""
    if not math.isfinite(float(price)) or float(price) <= 0:
        raise ValueError('INVALID_PRE_REFERENCE_PRICE')
    if candidates.empty:
        return candidates.copy()
    if not {'region_id','center','known_from'}.issubset(candidates.columns):
        raise ValueError('INVALID_REGION_RANKING_INPUT')
    frame = candidates.copy()
    if frame.center.isna().any() or (frame.center <= 0).any():
        raise ValueError('INVALID_REGION_RANKING_PRICE')
    frame['_distance_pct'] = (frame.center.astype(float) / float(price) - 1.0).abs()
    return (frame.sort_values(['_distance_pct','known_from','region_id'],
                              ascending=[True,False,True],kind='mergesort')
            .drop(columns=['_distance_pct']).reset_index(drop=True))


def v3_execution_visual_window(*, source_start: int, cutoff: int,
                               minutes: int = 24 * 60) -> tuple[int, int]:
    """Closed PRE history for the visual-only multi-TF replay; never POST."""
    if int(source_start) >= int(cutoff) or not (1 <= int(minutes) <= 24 * 60):
        raise ValueError('INVALID_EXECUTION_VISUAL_WINDOW')
    return max(int(source_start), int(cutoff) - int(minutes) * M), int(cutoff)


def v31_replay_cutoff_utc(day: date, clock: time, *,
                          source_start: int, source_end: int) -> int:
    """A closed-minute decision time on the frozen UTC timeline.

    The replay does not use the host's local timezone or current clock. A PRE
    timestamp is the instant at which earlier candles have finished closing.
    """
    if (not isinstance(day, date) or isinstance(day, datetime)
            or not isinstance(clock, time) or clock.tzinfo is not None
            or clock.second or clock.microsecond):
        raise ValueError('INVALID_REPLAY_UTC_TIME')
    stamp = int(datetime.combine(day, clock, tzinfo=timezone.utc).timestamp()*1000)
    if (stamp <= int(source_start) or stamp > int(source_end)
            or stamp % M != 0):
        raise ValueError('REPLAY_CUTOFF_OUTSIDE_FROZEN_WINDOW')
    return stamp


def v31_replay_visual_window(*, source_start: int, cutoff: int,
                             hours: int = 24) -> tuple[int, int]:
    """Only PRE candles, with up to 48h context and no silent lookahead."""
    if int(hours) not in (2, 4, 8, 12, 24, 48):
        raise ValueError('INVALID_REPLAY_CONTEXT_HOURS')
    if int(source_start) >= int(cutoff):
        raise ValueError('INVALID_REPLAY_PRE_WINDOW')
    return max(int(source_start), int(cutoff)-int(hours)*60*M), int(cutoff)


def v31_reveal_post_window(*, pre_cutoff: int, source_end: int,
                            reveal: bool, horizon_minutes: int) -> tuple[int, str]:
    """Display-only POST window. Truncation is explicitly CENSORED."""
    if int(horizon_minutes) not in (15, 30, 60, 240, 720):
        raise ValueError('INVALID_REPLAY_POST_HORIZON')
    if int(pre_cutoff) > int(source_end):
        raise ValueError('REPLAY_POST_BEYOND_FREEZE')
    if not reveal:
        return int(pre_cutoff), 'HIDDEN'
    requested = int(pre_cutoff) + int(horizon_minutes)*M
    return min(requested, int(source_end)), ('FULL_HORIZON' if requested <= source_end
                                             else 'CUTOFF_CENSORED')


def v31_pre_region_segments(stages, *, region_id: str, pre_start: int,
                            pre_cutoff: int, min_extrema: int = 2):
    """Render a region's immutable stages strictly within known PRE time.

    The source may contain future stages (e.g. a retrospective full-history
    catalog); these may NEVER enter the replay. No final-stage band is allowed
    to rewrite historical width/center. Returns segments clipped to PRE only.
    """
    if int(pre_start) >= int(pre_cutoff) or not region_id or int(min_extrema) < 2:
        raise ValueError('INVALID_REPLAY_REGION_SEGMENTS')
    if stages.empty:
        return stages.copy()
    needed = {'region_id', 'known_from', 'known_until', 'band_low', 'band_high',
              'independent_extrema'}
    if not needed.issubset(stages.columns):
        raise ValueError('INVALID_REPLAY_STAGE_SCHEMA')
    rows = stages.loc[
        stages.region_id.eq(region_id) &
        (stages.known_from <= int(pre_cutoff)) &
        (stages.independent_extrema >= int(min_extrema))
    ].copy()
    if rows.empty:
        return rows
    if ((rows.known_until < rows.known_from) |
        (rows.band_low > rows.band_high)).any():
        raise ValueError('INVALID_REPLAY_CAUSAL_STAGE')
    return visible_region_stages(rows, int(pre_start), int(pre_cutoff), limit=1)


def v31_replay_candidates(regions, stages, start: int, cutoff: int, *,
                          min_extrema: int = 2, side: str = 'BOTH',
                          order: str = 'RECENT'):
    """Only as-of candidates, including a band confirmed AT the PRE minute.

    V2 expresses its last known stage with a zero-length [cutoff,cutoff)
    interval. For selector visibility only, extend exactly that boundary one
    millisecond; never extend geometry in time, never include future stages.
    """
    if stages.empty:
        return visible_region_candidates(regions, stages, start, cutoff,
                 min_extrema=min_extrema,side=side,order=order)
    if not {'known_from','known_until'}.issubset(stages.columns):
        raise ValueError('INVALID_REPLAY_CANDIDATE_STAGES')
    safe=stages.loc[stages.known_from <= int(cutoff)].copy()
    at_cutoff=(safe.known_from == int(cutoff)) & (safe.known_until == int(cutoff))
    safe.loc[at_cutoff,'known_until']=int(cutoff)+1
    return visible_region_candidates(regions,safe,start,int(cutoff)+1,
                        min_extrema=min_extrema,side=side,order=order)


V32_PANEL_TFS = tuple(TF_MINUTES)
V32_CONTEXT_DAYS = (1, 3, 7, 14, 30, 60)


def v32_context_window(*, source_start: int, cutoff: int, days: int) -> tuple[int, int]:
    """Optional HTF context: independent lookback, shared PRE decision minute."""
    if int(days) not in V32_CONTEXT_DAYS or int(source_start) >= int(cutoff):
        raise ValueError('INVALID_V32_CONTEXT_WINDOW')
    return max(int(source_start), int(cutoff)-int(days)*DAY), int(cutoff)


def v32_build_context_chart(full, *, stages, selected_region, region_id: str,
                            timeframe: str, source_start: int, pre_cutoff: int,
                            chart_end: int, days: int, min_extrema: int = 2,
                            preaggregated=None):
    """Second synchronized, read-only panel. Does not load HLL or REACTION.

    The same cutoff drives both charts and all stage eligibility. Historical
    bands retain their own timestamped geometries; the reference over a
    revealed POST is ONLY the selected PRE geometry. Candles plot at close.

    Returns (figure or None, bar_count, context_start). Empty/sparse history
    is explicitly reported by the caller; no silent truncation is performed.
    """
    import pandas as pd
    if timeframe not in V32_PANEL_TFS:
        raise ValueError('INVALID_V32_TIMEFRAME')
    if not (int(source_start) < int(pre_cutoff) <= int(chart_end)):
        raise ValueError('INVALID_V32_REPLAY_TIMELINE')
    context_start,_=v32_context_window(
        source_start=source_start,cutoff=pre_cutoff,days=days)
    candles=(aggregate_closed(full,timeframe,context_start,chart_end)
             if preaggregated is None else
             v313_slice_bars(preaggregated,timeframe,context_start,chart_end))
    if candles.empty or len(candles)>4000:
        return None, len(candles), context_start
    empty_markers=pd.DataFrame(columns=['timeframe','side','plot_ts'])
    fig=build_figure(candles,empty_markers,display_tf=timeframe,
            cutoff=pre_cutoff,chart_end=chart_end,candle_x_at_close=True)
    fig.update_layout(
        title=f'BTCUSDT · contexto estructural {timeframe} · mismo corte PRE',
        height=510,showlegend=False,
    )
    if chart_end>pre_cutoff:
        fig.add_shape(type='rect',xref='x',yref='paper',
            x0=pd.to_datetime(pre_cutoff,unit='ms',utc=True),
            x1=pd.to_datetime(chart_end,unit='ms',utc=True),
            y0=0,y1=1,fillcolor='#9e7ed3',opacity=0.055,
            line={'width':0},layer='below')
    if selected_region is not None and region_id:
        color='#4fd3ae' if selected_region.side=='LOW' else '#ffb36a'
        segments=v31_pre_region_segments(stages,region_id=str(region_id),
               pre_start=context_start,pre_cutoff=pre_cutoff,
               min_extrema=min_extrema)
        if len(segments)>400:
            raise ValueError('V32_TOO_MANY_REGION_STAGES')
        for stage in segments.itertuples(index=False):
            fig.add_shape(type='rect',xref='x',yref='y',
                x0=pd.to_datetime(int(stage.visible_from),unit='ms',utc=True),
                x1=pd.to_datetime(int(stage.visible_until),unit='ms',utc=True),
                y0=float(stage.band_low),y1=float(stage.band_high),
                line={'color':color,'dash':'dot','width':1.2},
                fillcolor=color,opacity=0.16)
        if chart_end>pre_cutoff:
            fig.add_shape(type='rect',xref='x',yref='y',
                x0=pd.to_datetime(pre_cutoff,unit='ms',utc=True),
                x1=pd.to_datetime(chart_end,unit='ms',utc=True),
                y0=float(selected_region.band_low),
                y1=float(selected_region.band_high),
                line={'color':color,'dash':'dash','width':1},
                fillcolor=color,opacity=0.06)
            fig.add_annotation(
                x=pd.to_datetime(pre_cutoff,unit='ms',utc=True),
                y=float(selected_region.band_high),xref='x',yref='y',
                text='POST · banda PRE fija',showarrow=False,
                yanchor='bottom',font={'size':10,'color':color})
    fig.update_xaxes(
        range=[pd.to_datetime(context_start,unit='ms',utc=True),
               pd.to_datetime(chart_end,unit='ms',utc=True)],
        autorange=False,
    )
    return fig,len(candles),context_start



# V3.1.2 | Retrospective navigation only. Events locate a historical decision
# instant, but are NEVER used as PRE features, signals, trading outcomes, or
# evidence for the selected band. The charts still reconstruct at the cutoff.
V33_REPLAY_ORIGINS = ('ARRIVAL', 'VISIT', 'BIRTH', 'CONFIRMATION', 'MANUAL')
V33_NAVIGATION_GAP_MINUTES = 30


def v33_structural_event_index(full, stages, *, region_id: str,
                               source_end: int, gap_minutes: int = 30):
    """Find causal decision anchors for one region across its known stages.

    Stage events: cutoff at the closed confirmation minute.
    CONTACT events: cutoff at the OPEN of the first touching 1m bar, before
      its range or closing price is known.
    VISIT events: contacts grouped when the gap is <= `gap_minutes`; cutoff
      remains the *first* touching bar's open, NOT the visit closure.

    Scanning historical high/low is permitted ONLY for navigation. Its results
    must never be read by the PRE renderer or used to claim a predicted touch.
    The fixed-band V2.3 retrospective visit audit remains unchanged.
    """
    import pandas as pd
    if not region_id or int(source_end) % M or gap_minutes not in (5, 15, 30, 60):
        raise ValueError('INVALID_EVENT_NAVIGATION_SETTINGS')
    needed={'region_id','known_from','known_until','band_low','band_high',
            'independent_extrema','side'}
    if stages is None or not needed.issubset(stages.columns):
        raise ValueError('INVALID_EVENT_NAVIGATION_STAGES')
    if not {'timestamp','high','low'}.issubset(full.columns):
        raise ValueError('INVALID_EVENT_NAVIGATION_CANDLES')
    region=stages.loc[stages.region_id.eq(region_id)].sort_values(
        ['known_from','known_until'],kind='mergesort').copy()
    out_cols=['event_id','event_type','decision_ts','stage_known_from',
              'band_low','band_high','extrema_at_event','visit_number']
    if region.empty:
        return pd.DataFrame(columns=out_cols)
    if ((region.known_until < region.known_from).any() or
        (region.band_low <= 0).any() or
        (region.band_high < region.band_low).any()):
        raise ValueError('INVALID_EVENT_NAVIGATION_STAGE_TIMELINE')
    if any(region.known_from.iloc[i] < region.known_until.iloc[i-1]
           for i in range(1,len(region))):
        raise ValueError('OVERLAPPING_EVENT_NAVIGATION_STAGES')
    # Several independent extrema may be confirmed in the same minute.
    # Only the last stage of that minute is operative, without leaking later
    # minutes. Zero-duration earlier stages are still legitimate history.
    region=region.drop_duplicates('known_from',keep='last').reset_index(drop=True)
    if (full.timestamp.duplicated().any() or
        (full.timestamp.diff().dropna() != M).any()):
        raise ValueError('NONCONTIGUOUS_EVENT_NAVIGATION_1M')
    events=[]
    for n,row in enumerate(region.itertuples(index=False)):
        stamp=int(row.known_from)
        if stamp <= int(source_end):
            events.append({'event_id':f'{region_id}:C:{stamp}',
                'event_type':'CONFIRMATION','decision_ts':stamp,
                'stage_known_from':stamp,'band_low':float(row.band_low),
                'band_high':float(row.band_high),
                'extrema_at_event':int(row.independent_extrema),
                'visit_number':None})
    # Every minute is evaluated against the region stage that existed at the
    # OPEN of that bar. A future geometry cannot change an earlier touch.
    start=int(region.known_from.min())
    bars=full.loc[(full.timestamp>=start)&(full.timestamp<int(source_end)),
                  ['timestamp','high','low']]
    if not bars.empty:
        stamp=bars.timestamp.to_numpy(dtype=np.int64)
        hi=bars.high.to_numpy(dtype=float)
        lo=bars.low.to_numpy(dtype=float)
        if (not np.isfinite(hi).all() or not np.isfinite(lo).all() or
            (hi<lo).any()):
            raise ValueError('INVALID_EVENT_NAVIGATION_OHLC')
        known=region.known_from.to_numpy(dtype=np.int64)
        until=region.known_until.to_numpy(dtype=np.int64)
        idx=np.searchsorted(known,stamp,side='right')-1
        safe=np.maximum(idx,0)
        valid=(idx>=0)&(stamp<until[safe])
        bandlow=region.band_low.to_numpy(dtype=float)[safe]
        bandhigh=region.band_high.to_numpy(dtype=float)[safe]
        touched=valid&(hi>=bandlow)&(lo<=bandhigh)
        starts=np.flatnonzero(touched & ~np.r_[False,touched[:-1]])
        # A new contact begins only after a non-contact 1m bar (V2.2 rule).
        # To group visits we use the last touching close, like V2.3.
        contact_records=[]
        for i in starts:
            last=int(i)
            while last+1<len(touched) and touched[last+1]:
                last+=1
            si=int(safe[i]); ts=int(stamp[i])
            contact_records.append((ts,int(stamp[last])+M,si))
            events.append({'event_id':f'{region_id}:T:{ts}',
                'event_type':'ARRIVAL','decision_ts':ts,
                'stage_known_from':int(known[si]),
                'band_low':float(bandlow[i]),'band_high':float(bandhigh[i]),
                'extrema_at_event':int(region.iloc[si].independent_extrema),
                'visit_number':None})
        previous_end=None
        visit_n=0
        for ts,last_close,si in contact_records:
            if previous_end is None or ts-previous_end>gap_minutes*M:
                visit_n+=1
                events.append({'event_id':f'{region_id}:V:{ts}',
                    'event_type':'VISIT','decision_ts':ts,
                    'stage_known_from':int(known[si]),
                    'band_low':float(region.iloc[si].band_low),
                    'band_high':float(region.iloc[si].band_high),
                    'extrema_at_event':int(region.iloc[si].independent_extrema),
                    'visit_number':visit_n})
            previous_end=last_close
    return pd.DataFrame(events,columns=out_cols).sort_values(
        ['decision_ts','event_type','event_id'],
        ascending=[False,True,True],kind='mergesort').reset_index(drop=True)


def v33_auto_decision_cutoff(event_row, *, source_start: int,
                             source_end: int, event_type: str) -> int:
    """Validate a retrospective anchor; the returned minute is PRE decision time."""
    if event_type not in ('ARRIVAL','VISIT','CONFIRMATION'):
        raise ValueError('INVALID_AUTO_REPLAY_EVENT_TYPE')
    if str(event_row['event_type']) != event_type:
        raise ValueError('AUTO_REPLAY_EVENT_MISMATCH')
    stamp=int(event_row['decision_ts'])
    known=int(event_row['stage_known_from'])
    if not (int(source_start)<stamp<=int(source_end) and
            known<=stamp and stamp % M==0):
        raise ValueError('INVALID_AUTO_REPLAY_CUTOFF')
    return stamp

def render_btc_multiswing_60d_view(project):
    import pandas as pd
    import streamlit as st

    st.markdown('### 🧭 Multi-Swing Structural Lab · BTC 60D')
    mode = st.radio(
        'Modo de investigación', V3_MODES, horizontal=True, index=0,
        format_func=lambda m: {
            'MAPA': '① Mapa estructural',
            'INSPECTOR': '② Inspector de región',
            'EJECUCION': '③ Preparación multi-TF · experimental',
        }[m], key='ms60_v3_mode')
    policy = v3_view_policy(mode)
    replay_origin='MANUAL'
    if mode == 'EJECUCION':
        replay_origin=st.selectbox('Ubicar PRE automáticamente según',
            list(V33_REPLAY_ORIGINS),index=0,
            format_func=lambda x:{
                'ARRIVAL':'Llegada del precio · PRE antes del primer contacto',
                'VISIT':'Visita histórica · PRE al comenzar la visita',
                'BIRTH':'Nacimiento original · PRE al confirmar el segundo extremo',
                'CONFIRMATION':'Nueva confirmación · evolución posterior de la banda',
                'MANUAL':'Exploración libre · fecha y hora manuales'}[x],
            key='ms60_v33_event_origin')
    ui_state = getattr(st, 'session_state', None)
    if ui_state is not None:
        previous_mode = ui_state.get('ms60_v31_last_mode')
        if mode == 'EJECUCION' and previous_mode in ('MAPA', 'INSPECTOR'):
            # Re-entering the replay always starts blind after another mode.
            ui_state['ms60_v31_reveal_post'] = False
        ui_state['ms60_v31_last_mode'] = mode
    if mode == 'MAPA':
        st.caption('Mapa limpio: velas + bandas geométricas. Los pivotes se ocultan '
                   'por defecto y los criterios descriptivos NO representan un score.')
    elif mode == 'INSPECTOR':
        st.caption('Elegí una región para revisar miembros físicos, evolución PRE, '
                   'contactos V2.2 y visitas V2.3 en paneles separados.')
    else:
        st.caption('Replay UTC de velas 1m a 4h: PRE cerrado por minuto y POST oculto '
                   'hasta revelar explícitamente. SIN triggers validados, órdenes, backtest ni LIVE.')
    st.caption('Catálogo congelado 1m / 5m / 15m / 30m / 1h / 4h. '
               'Los pivotes solo son elegibles cuando su confirmación está cerrada. '
               'Sin nuevos detectores de pivotes, sin consultas de red y sin modificar LIVE.')
    v314_load_started = _perf_time.perf_counter()
    try:
        full, catalog, source = load_snapshot(Path(project))
    except (FileNotFoundError, KeyError, ValueError, OSError) as exc:
        st.error(f'No se pudo abrir el BTC 60D congelado: {exc}')
        st.info('Comprobá el freeze y el catálogo 6TF; no se sustituyen por datos LIVE.')
        return

    v314_load_ms = (_perf_time.perf_counter()-v314_load_started)*1000
    start, end = source['start'], source['end']
    cache_verified=v313_verified_cache_source(source)
    d1,d2,d3,d4=st.columns([2,1,2,1])
    with d1:
        if mode == 'EJECUCION' and replay_origin != 'MANUAL':
            selected_date=_selected_cutoff_date(end)
            st.caption('Fecha PRE: automática al seleccionar un evento de la banda')
        else:
            selected_date=st.date_input(
                'Fecha del replay PRE (UTC)' if mode == 'EJECUCION'
                else 'Corte de exploración (UTC)',
                value=_selected_cutoff_date(end),
                min_value=datetime.fromtimestamp(start/1000,timezone.utc).date(),
                max_value=_selected_cutoff_date(end),
                key='ms60_cutoff_date')
    with d2:
        span_days=st.selectbox('Ventana (días)',[1,3,7,14,30,60],index=2,key='ms60_days')
    with d3:
        selected_tfs=tuple(st.multiselect('Temporalidades',list(TF_MINUTES),
              default=['15m','30m','1h','4h'],key='ms60_tfs'))
    with d4:
        side=st.selectbox('Tipo', ['BOTH','LOW','HIGH'],
                          format_func=lambda s: {'BOTH':'Ambos','LOW':'LOW','HIGH':'HIGH'}[s],
                          key='ms60_side')
    group_mode = 'Una marca por extremo'
    multi_only = False
    show_context_panel = False
    context_tf = '1h'
    context_days = 7
    if mode == 'EJECUCION':
        display_tf=st.selectbox('Gráfico principal · temporalidad',
                                list(V32_PANEL_TFS),index=1,
                                key='ms60_v3_execution_tf')
        show_context_panel=st.checkbox('Mostrar segundo gráfico de contexto multi-TF',
                               value=False,key='ms60_v32_show_context')
        if show_context_panel:
            ct1,ct2=st.columns(2)
            with ct1:
                context_tf=st.selectbox('Contexto · temporalidad',
                   list(V32_PANEL_TFS),index=4,key='ms60_v32_context_tf')
            with ct2:
                context_days=int(st.selectbox('Contexto · días anteriores',
                   list(V32_CONTEXT_DAYS),index=2,key='ms60_v32_context_days'))
        st.caption('Ambos gráficos comparten el mismo corte UTC PRE/POST. '
            'En el replay las velas se posicionan en su hora de CIERRE. '
            'El segundo gráfico es opcional; HLL y REACTION aún no se superponen.')
    else:
        display_tf=st.selectbox('Velas del gráfico',list(TF_MINUTES),
                                index=3,key='ms60_display_tf')
    show_all_pivots=False
    if mode == 'MAPA':
        show_all_pivots=st.checkbox('Mostrar pivotes individuales (puede saturar)',
                                    value=False,key='ms60_v3_map_pivots')
    elif mode == 'INSPECTOR':
        with st.expander('Pivotes generales · opciones avanzadas',expanded=False):
            show_all_pivots=st.checkbox('Mostrar todos los pivotes de la ventana',
                                         value=False,key='ms60_v3_inspect_pivots')
            group_mode=st.selectbox('Identidad física',
                    ['Una marca por extremo','Todas las representaciones TF'],
                    key='ms60_group_mode')
            multi_only=st.checkbox('Solo extremos con ≥2 TF confirmados',
                                   value=False,key='ms60_multi')
    show_regions=True  # Modes are structurally oriented; no detector mutation.
    with st.expander('⚙️ Parámetros geométricos V2 · avanzados',expanded=False):
        st.caption('Estos controles reconstruyen candidatos geométricos OFFLINE. '
                   'Los parámetros no validan vigencia ni probabilidad de trading.')
        v1,v2,v3=st.columns(3)
        with v1:
            tolerance_pct=float(st.selectbox('Dispersión máxima entre extremos (%)',
                            [0.10,0.20,0.30,0.50],index=1,key='ms60_v2_price_spread'))
        with v2:
            lookback_days=int(st.selectbox('Ventana entre extremos (días)',
                            [1,3,7,14],index=2,key='ms60_v2_lookback'))
        with v3:
            separation=int(st.selectbox('Separación física mínima (min)',
                            [15,30,60,120],index=2,key='ms60_v2_separation'))
        st.caption('V2: un mismo extremo reconocido en 6 TF cuenta UNA vez. '
                   'Una región requiere dos extremos físicos distintos con suficiente separación. '
                   'Las bandas comienzan al confirmar el segundo; sus cambios posteriores '
                   'no se aplican retroactivamente. La proyección visual caduca sin nueva confirmación '
                   'después de la ventana elegida. Son candidatas geométricas, no defensas, '
                   'ACTIVE ni señales de trading. TIED_EXTREMA queda excluido.')
    if not selected_tfs:
        st.info('Seleccioná una o más temporalidades para continuar.')
        return
    # Historical exploration uses a day-inclusive cutoff. Execution replay
    # instead uses an explicit UTC minute; changing that minute hides POST again.
    midnight_after=datetime.combine(selected_date+timedelta(days=1),time.min,tzinfo=timezone.utc)
    exploration_cutoff=min(end,int(midnight_after.timestamp()*1000))
    replay_hours = 24
    reveal_post = False
    replay_post_horizon = 60
    replay_post_status = 'HIDDEN'
    event_anchor_region=''
    event_id=''
    v314_navigation_started = _perf_time.perf_counter()
    if mode == 'EJECUCION' and replay_origin != 'MANUAL':
        # This full-history index is RETROSPECTIVE NAVIGATION ONLY. Every PRE
        # feature/region/chart below is rebuilt at the chosen event timestamp.
        nav_known=asof_rows(catalog,end,selected_tfs,side)
        nav_regions,nav_stages,_=v313_region_build(catalog,nav_known,
            cutoff=end,selected_tfs=selected_tfs,side=side,
            tolerance_pct=tolerance_pct,lookback_days=lookback_days,
            separation=separation,enabled=cache_verified)
        nav_regions=nav_regions.sort_values(
            ['born_ts','region_id'],ascending=[False,True],
            kind='mergesort')
        region_lookup={str(r.region_id):r for r in nav_regions.itertuples(index=False)}
        birth_rows=(nav_stages.sort_values(['known_from','region_id'],kind='mergesort')
                    .drop_duplicates('region_id',keep='first'))
        birth_lookup={str(r.region_id):r for r in birth_rows.itertuples(index=False)}
        region_options=['']+nav_regions.region_id.astype(str).tolist()
        # On entry from map/inspector, reuse the selected structural identity.
        # In the replay itself, keep the user's explicit navigation selection.
        if ui_state is not None:
            previous=str(ui_state.get('ms60_v21_region_selection') or '')
            if (previous in region_lookup and
                (previous_mode in ('MAPA','INSPECTOR') or
                 'ms60_v33_region_anchor' not in ui_state)):
                ui_state['ms60_v33_region_anchor']=previous
        if (ui_state is not None and ui_state.get('ms60_v33_region_anchor','')
                not in region_options):
            ui_state['ms60_v33_region_anchor']=''
        event_anchor_region=st.selectbox('Banda para replay automático',
            region_options,
            format_func=lambda rid:'Seleccioná una banda para explorar sus eventos'
            if not rid else (
                f'{region_lookup[rid].side} · '
                f'{(float(birth_lookup[rid].band_low)+float(birth_lookup[rid].band_high))/2:,.2f} USD'
                f' (geometría de nacimiento) · {iso(int(region_lookup[rid].born_ts))}'
                f' · {rid[:12]}'),
            key='ms60_v33_region_anchor')
        if not event_anchor_region:
            st.info('Elegí una banda. El momento PRE se configurará automáticamente; '
                    'la hora manual queda disponible en Exploración libre.')
            return
        nav_events=v313_events_build(full,nav_stages,
                    region_id=event_anchor_region,source_end=end,
                    gap_minutes=V33_NAVIGATION_GAP_MINUTES,enabled=cache_verified)
        if replay_origin in ('BIRTH','CONFIRMATION'):
            confirmations=nav_events.loc[nav_events.event_type.eq('CONFIRMATION')].sort_values(
                ['decision_ts','event_id'],kind='mergesort').reset_index(drop=True)
            relevant=(confirmations.head(1) if replay_origin=='BIRTH'
                      else confirmations.iloc[1:]).copy()
        else:
            relevant=nav_events.loc[nav_events.event_type.eq(replay_origin)].copy()
        if relevant.empty:
            st.info('No hay eventos de este tipo para la banda seleccionada. '
                    'Podés consultar su confirmación u optar por exploración libre.')
            return
        event_lookup={str(r.event_id):r for r in relevant.itertuples(index=False)}
        if ui_state is not None:
            event_signature=(str(event_anchor_region),str(replay_origin),
                float(tolerance_pct),int(lookback_days),int(separation),
                tuple(selected_tfs),str(side))
            if ui_state.get('ms60_v33_last_event_catalog')!=event_signature:
                ui_state['ms60_v33_event_choice']=str(relevant.event_id.iloc[0])
                ui_state['ms60_v33_last_event_catalog']=event_signature
        event_id=st.selectbox('Evento histórico · ubicar corte PRE',
            relevant.event_id.astype(str).tolist(),
            format_func=lambda eid: (
                f'{iso(int(event_lookup[eid].decision_ts))} UTC'
                f' · {"etapa confirmada" if replay_origin=="CONFIRMATION" else ("primer contacto" if replay_origin=="ARRIVAL" else "inicio de visita")}'
                f' · {int(event_lookup[eid].extrema_at_event)} extremos PRE'),
            key='ms60_v33_event_choice')
        selected_event=relevant.loc[relevant.event_id.eq(event_id)].iloc[0]
        try:
            exploration_cutoff=v33_auto_decision_cutoff(selected_event,
                source_start=start,source_end=end,
                event_type='CONFIRMATION' if replay_origin=='BIRTH' else replay_origin)
        except ValueError as exc:
            st.error(f'Evento histórico inválido: {exc}')
            return
        st.caption('Navegación retrospectiva: los contactos se localizan con el '
            'historial para elegir un instante, pero su vela y desenlace NO forman '
            'parte del PRE. Una visita se agrupa con 30m entre contactos y se '
            'abre en su primer toque; no equivale a una señal de REACTION.')
    if mode == 'EJECUCION':
        if replay_origin == 'MANUAL':
            initial_clock = (datetime.fromtimestamp(end/1000,timezone.utc).time()
                             if selected_date == datetime.fromtimestamp(end/1000,timezone.utc).date()
                             else time(12,0))
            rc1,rc2=st.columns([2,1])
            with rc1:
                replay_clock=st.time_input('Instante PRE exacto (UTC) · HH:MM',
                             value=initial_clock,step=timedelta(minutes=1),
                             key='ms60_v31_replay_clock')
            with rc2:
                replay_hours=int(st.selectbox('Contexto anterior (horas)',
                                  [2,4,8,12,24,48],index=4,
                                  key='ms60_v31_pre_hours'))
            try:
                exploration_cutoff=v31_replay_cutoff_utc(
                    selected_date,replay_clock,source_start=start,source_end=end)
            except ValueError as exc:
                st.warning(f'Replay PRE fuera del historial congelado: {exc}. '
                           'Elegí un instante UTC dentro del período disponible.')
                return
        else:
            replay_hours=int(st.selectbox('Contexto anterior (horas)',
                              [2,4,8,12,24,48],index=4,
                              key='ms60_v31_pre_hours'))
        replay_signature=(int(exploration_cutoff),str(replay_origin),
                          str(event_anchor_region if replay_origin!='MANUAL'
                              else (ui_state.get('ms60_v21_region_selection','')
                                    if ui_state is not None else '')),
                          str(event_id))
        if ui_state is not None:
            last_signature=ui_state.get('ms60_v33_last_replay_signature')
            # Keep compatibility with pre-V3.1.2 MANUAL replay sessions. Only
            # the initial migration may reuse a revealed POST at the same UTC.
            legacy_unchanged=(last_signature is None and replay_origin=='MANUAL'
                and ui_state.get('ms60_v31_last_cutoff') == exploration_cutoff)
            if last_signature != replay_signature and not legacy_unchanged:
                # Before the widget: changing band/event or time hides POST.
                st.session_state['ms60_v31_reveal_post'] = False
            ui_state['ms60_v33_last_replay_signature']=replay_signature
            st.session_state['ms60_v31_last_cutoff'] = exploration_cutoff
        rp1,rp2=st.columns([2,1])
        with rp1:
            reveal_post=st.checkbox('Revelar POST · solo velas cerradas',
                                    key='ms60_v31_reveal_post')
        with rp2:
            replay_post_horizon=int(st.selectbox('POST a revelar (minutos)',
                    [15,30,60,240,720],index=2,disabled=not reveal_post,
                    key='ms60_v31_post_minutes'))
        available_post_minutes=max(0,(int(end)-int(exploration_cutoff))//M)
        st.caption(f'POST disponible en freeze desde este evento: '
                   f'{available_post_minutes:,} minutos · '
                   'al pedir un horizonte mayor, se marca CENSORED.')
        _,replay_post_status=v31_reveal_post_window(
            pre_cutoff=exploration_cutoff,source_end=end,
            reveal=reveal_post,horizon_minutes=replay_post_horizon)
        st.caption(f'Instante de decisión PRE: {iso(exploration_cutoff)} · '
                   f'POST: {replay_post_status}. Todos los swings y las bandas '
                   'se reconstruyen exclusivamente al corte PRE; revelar POST '
                   'NO actualiza evidencias ni geometría.')
    v314_navigation_ms = (_perf_time.perf_counter()-v314_navigation_started)*1000
    v314_evidence_started = _perf_time.perf_counter()
    explore_start,_=chart_window(exploration_cutoff,start,int(span_days))
    known=asof_rows(catalog,exploration_cutoff,selected_tfs,side)
    # V3.1.4: the MAP and execution preview do not use an individual pivot
    # selector. Do not materialize thousands of physical-group rows just to
    # throw them away. The inspector still provides its full original selector.
    if mode == 'INSPECTOR':
        display=v313_group_build(catalog,known,cutoff=exploration_cutoff,
            selected_tfs=selected_tfs,side=side,collapse=group_mode.startswith('Una'),
            multi_only=multi_only,enabled=cache_verified)
        pool=display[(display['plot_ts']>=explore_start) &
                     (display['plot_ts']<exploration_cutoff)].copy()
        pool=pool.sort_values(['plot_ts','timeframe'],ascending=[False,True]).reset_index(drop=True)
    else:
        display=None
        pool=pd.DataFrame(columns=['plot_ts','timeframe','group_key','pivot_id'])
    # Las cifras deben corresponder a la misma ventana espacial del grafico;
    # las agrupaciones siguen siendo causales al corte, sin confirmar pivotes futuros.
    stats=v313_stats_build(catalog,known,cutoff=exploration_cutoff,
        start=explore_start,selected_tfs=selected_tfs,side=side,
        multi_only=multi_only,enabled=cache_verified)
    v314_evidence_ms = (_perf_time.perf_counter()-v314_evidence_started)*1000
    x1,x2,x3,x4=st.columns(4)
    x1.metric('Representaciones PRE · ventana',stats['representations'])
    x2.metric('Extremos físicos verificados · ventana',stats['verified_extrema'])
    x3.metric('Extremos ≥2 TF · ventana',stats['multi_tf_extrema'])
    x4.metric('Identidad ambigua · ventana',stats['ambiguous_representations'])
    st.caption(f"Corte: {iso(exploration_cutoff)} · Ventana desde {iso(explore_start)}. "
               'Los contadores corresponden únicamente a pivotes ubicados en la ventana, '
               'confirmados al corte y con los TF/lado/filtro múltiple seleccionados. '
               'No representan toda la historia PRE de 60 días.')
    chosen=''
    if mode == 'INSPECTOR':
        with st.expander('Inspeccionar un pivote individual · opcional',expanded=False):
            selector_count=500
            subset=pool.head(selector_count)
            keys=['']+subset['group_key'].astype(str).tolist() if group_mode.startswith('Una') else ['']+subset['pivot_id'].astype(str).tolist()
            keys=list(dict.fromkeys(keys))
            by_key={
                (str(row.group_key) if group_mode.startswith('Una') else str(row.pivot_id)):row
                for row in subset.itertuples(index=False)
            }
            chosen=st.selectbox('Inspeccionar un pivote (los 500 más recientes de la ventana)',keys,
                format_func=lambda k: 'Exploración general · sin seleccionar' if not k else (
                    f"{iso(int(by_key[k].plot_ts))} · {by_key[k].side} · "
                    f"{float(by_key[k].price):,.2f} · TF PRE {by_key[k].tf_set_asof} · "
                    f"{by_key[k].physical_identity_state}"), key='ms60_v3_selected_pivot')
            if len(pool)>selector_count:
                st.caption(f'Selector: últimos {selector_count} de {len(pool)} pivotes visibles. '
                           'Para inspeccionar anteriores, cambiá la fecha del corte y/o temporalidades.')
    elif mode == 'INSPECTOR' and pool.empty:
        st.info('No hay pivotes nuevos en esta ventana; las bandas históricas conocidas '
                'al corte siguen siendo inspeccionables cuando sus etapas se intersectan.')
    chosen_row=None
    cutoff=exploration_cutoff
    chart_start=explore_start
    chart_end=exploration_cutoff
    if chosen:
        row=pool.loc[pool['group_key'].eq(chosen) if group_mode.startswith('Una')
                     else pool['pivot_id'].eq(chosen)].iloc[0]
        chosen_row=row
        r1,r2,r3=st.columns([2,2,2])
        with r1:
            before=int(st.selectbox('Horas PRE alrededor del pivote',[2,4,8,12,24,48],index=4,key='ms60_pre_hours'))
        with r2:
            reveal=st.checkbox('Revelar POST · solo velas',value=False,key='ms60_reveal_post')
        with r3:
            after=int(st.selectbox('Horas POST',[2,4,8,12,24],index=2,key='ms60_post_hours',
                                   disabled=not reveal))
        chart_start,chart_end,cutoff=selected_inspection_window(
            row,before,after,start,end,reveal)
        st.caption(f'Pivote: {iso(int(row.plot_ts))} · Corte PRE: {iso(cutoff)} · '
                   'Toda confirmación POST permanece oculta incluso al revelar velas futuras.')

    if mode == 'EJECUCION':
        chart_start,_=v31_replay_visual_window(
            source_start=start,cutoff=cutoff,hours=replay_hours)
        chart_end,replay_post_status=v31_reveal_post_window(
            pre_cutoff=cutoff,source_end=end,
            reveal=reveal_post,horizon_minutes=replay_post_horizon)
        st.caption(f'PRE cerrado: {iso(chart_start)} → {iso(cutoff)} · '
                   f'POST: {replay_post_status}' +
                   (f' · hasta {iso(chart_end)}; solo velas, no evidencia nueva.'
                    if reveal_post else ' · completamente oculto.') +
                   ' El contexto de la banda sigue siendo una hipótesis geométrica.')
    v314_chart_started = _perf_time.perf_counter()
    candles=v313_render_bars(full,display_tf,chart_start,chart_end,enabled=cache_verified)
    if candles.empty:
        st.warning('No hay velas completas en la temporalidad elegida hasta ese corte. '
                   'Elegí un timeframe menor o una ventana mayor.')
        return
    max_candles=4000
    if len(candles)>max_candles:
        st.warning(f'El gráfico contiene {len(candles):,} velas {display_tf} (límite visual {max_candles:,}). '
                   'Elegí un timeframe mayor o menos días; no reducimos datos silenciosamente.')
        return
    # Recompute as-of evidence at selected pivot's first/last confirmation.
    # Even with POST candles visible, never add future swing markers.
    v314_candles_ms = (_perf_time.perf_counter()-v314_chart_started)*1000
    v314_structural_started = _perf_time.perf_counter()
    asof = known if int(cutoff) == int(exploration_cutoff) else asof_rows(
        catalog,cutoff,selected_tfs,side)
    # In normal operation triangles are disabled. Passing a genuinely empty
    # marker frame avoids an O(catalog) regrouping on EVERY Streamlit widget
    # rerun; only inspector/map explicit opt-in rebuilds the pivot overlays.
    if show_all_pivots:
        if mode == 'INSPECTOR' and display is not None and int(cutoff)==int(exploration_cutoff):
            markers=display
        else:
            markers=v313_group_build(catalog,asof,cutoff=cutoff,
                selected_tfs=selected_tfs,side=side,
                collapse=group_mode.startswith('Una'),multi_only=multi_only,
                enabled=cache_verified)
        markers=markers[(markers['plot_ts']>=chart_start) &
                        (markers['plot_ts']<cutoff)].copy()
    else:
        markers=pd.DataFrame(columns=['timeframe','side','plot_ts'])
    max_markers=3500
    if show_all_pivots and len(markers)>max_markers:
        st.warning(f'{len(markers):,} marcadores, sobre el límite visual {max_markers:,}. '
                   'Reducí ventana o temporalidades; no ocultamos pivotes sin avisar.')
        return
    regions = None
    region_stages = None
    region_stats = None
    region_inspection = None
    region_member_details = None
    if show_regions:
        # Always reconstruct from representations known at selected PRE cutoff.
        # Do not apply the >=2 TF VISUAL filter: this is a test of *independent*
        # physical extrema, including ones detected by only one timeframe.
        regions, region_stages, region_stats = v313_region_build(catalog,asof,
            cutoff=cutoff,selected_tfs=selected_tfs,side=side,
            tolerance_pct=tolerance_pct,lookback_days=lookback_days,
            separation=separation,enabled=cache_verified)
        st.caption(f"V2 · {region_stats['candidate_regions']} regiones geométricas conocidas "
                   f"al corte (historia completa) · {region_stats['physical_extrema_evaluated']} "
                   f"extremos físicos evaluados · {region_stats['ambiguous_excluded']} "
                   'representaciones ambiguas excluidas. Universo: temporalidades y lado seleccionados.')
        st.markdown('#### Bandas geométricas · selección y filtros')
        f1,f2,f3,f4=st.columns(4)
        with f1:
            region_min=int(st.selectbox('Mínimo de extremos independientes',[2,3,4,5],
                                        index=0,key='ms60_v21_min_extrema'))
        with f2:
            region_side=st.selectbox('Lado de las regiones',['BOTH','LOW','HIGH'],
                                      format_func=lambda x:{'BOTH':'Ambos','LOW':'LOW','HIGH':'HIGH'}[x],
                                      key='ms60_v21_region_side')
        with f3:
            max_regions=int(st.selectbox('Máximo de regiones dibujadas',[5,10,20,40],
                                         index=1,key='ms60_v21_max_regions'))
        with f4:
            region_order=st.selectbox('Orden descriptivo',
                                      ['DISTANCE','RECENT','EXTREMA'],
                                      index=0 if mode == 'MAPA' else 1,
                                      format_func=lambda x:{
                                          'DISTANCE':'Más cerca del último cierre PRE',
                                          'RECENT':'Confirmación más reciente',
                                          'EXTREMA':'Más extremos independientes'}[x],
                                      key='ms60_v21_order')
        candidate_selector=(v31_replay_candidates if mode == 'EJECUCION'
                            else visible_region_candidates)
        candidates = candidate_selector(
            regions,region_stages,chart_start,cutoff,
            min_extrema=region_min,side=region_side,
            order='RECENT' if region_order == 'DISTANCE' else region_order)
        if region_order == 'DISTANCE':
            # Fully closed 1m candle before cutoff, never the future candle.
            recent_closes = full.loc[full.timestamp < cutoff, 'close']
            if recent_closes.empty:
                st.error('No hay cierre PRE disponible para ordenar bandas por distancia.')
                return
            candidates = v3_rank_regions_near_price(candidates,float(recent_closes.iloc[-1]))
        candidates_by_id = {str(r.region_id):r for r in candidates.itertuples(index=False)}
        if mode == 'EJECUCION' and replay_origin != 'MANUAL':
            region_selection=event_anchor_region
            if region_selection not in candidates_by_id:
                st.warning('La banda elegida para navegar no tiene una etapa '
                           'elegible en este instante PRE con los filtros actuales. '
                           'No se dibuja una estructura futura; elegí otro evento '
                           'o disminuí el mínimo de extremos.')
                return
            stage=candidates_by_id[region_selection]
            st.caption(f'Banda PRE seleccionada automáticamente: {stage.side} '
                f'{float(stage.center):,.2f} USD · '
                f'{int(stage.independent_extrema)} extremos conocidos · '
                f'{region_selection[:12]}')
        else:
            region_selection = st.selectbox('Inspeccionar una región candidata',
                ['']+candidates.region_id.astype(str).tolist(),
                format_func=lambda rid:'Vista general · regiones seleccionadas por orden descriptivo'
                if not rid else (
                    f"{candidates_by_id[rid].side} · {candidates_by_id[rid].center:,.2f} USD"
                    f" · {int(candidates_by_id[rid].independent_extrema)} extremos PRE"
                    f" · {iso(int(candidates_by_id[rid].known_from))} · {rid[:12]}"),
                key='ms60_v21_region_selection')
        draw_region_ids, draw_stats = display_region_ids(
            candidates,max_regions,region_selection)
        if mode in ('INSPECTOR','EJECUCION') and not region_selection:
            draw_region_ids = []  # Single-region modes require explicit selection.
        st.caption(f"V2.1 · {draw_stats['eligible_regions']} regiones elegibles en la ventana. "
                   f"Máximo descriptivo: {max_regions}; "
                   f"bandas realmente dibujadas en este modo: {len(draw_region_ids)}. "
                   'Las demás siguen disponibles en el catálogo; ninguna se elimina. '
                   'El mínimo de extremos se aplica a cada etapa PRE, no al total final.')
        if region_selection:
            region_inspection = candidates.loc[
                candidates.region_id.eq(region_selection)].iloc[0]
            region_member_details = region_member_evidence(asof, regions.loc[
                regions.region_id.eq(region_selection)].iloc[0],cutoff)
    # Never clutter the initial map. In the inspector only selected physical
    # members are highlighted unless the user explicitly enables all pivots.
    plot_markers = markers if show_all_pivots else markers.iloc[0:0].copy()
    fig=build_figure(candles,plot_markers,display_tf=display_tf,
                     selected=chosen_row,
                     cutoff=cutoff if (chosen_row is not None or mode == 'EJECUCION') else None,
                     chart_end=chart_end,
                     candle_x_at_close=(mode == 'EJECUCION'))
    if mode == 'EJECUCION' and reveal_post and chart_end > cutoff:
        import pandas as pd
        fig.add_shape(type='rect',xref='x',yref='paper',
                      x0=pd.to_datetime(cutoff,unit='ms',utc=True),
                      x1=pd.to_datetime(chart_end,unit='ms',utc=True),
                      y0=0,y1=1,line={'width':0},fillcolor='#9e7ed3',
                      opacity=0.055,layer='below')
    if show_regions and region_stages is not None and not region_stages.empty:
        try:
            filtered_stages=region_stages[
                region_stages.region_id.isin(draw_region_ids) &
                (region_stages.independent_extrema >= region_min)].copy()
            if mode == 'EJECUCION' and region_inspection is not None:
                visible=v31_pre_region_segments(
                    filtered_stages,region_id=str(region_selection),
                    pre_start=chart_start,pre_cutoff=cutoff,
                    min_extrema=region_min)
            else:
                visible=visible_region_stages(filtered_stages,chart_start,cutoff,limit=100)
            if len(visible)>400:
                st.warning(f'V2: {len(visible)} etapas de regiones exceden el límite visual de 400. '
                           'Reducí temporalidades o ventana; no se ocultan registros sin avisar.')
            else:
                for stage in visible.itertuples(index=False):
                    color='#4fd3ae' if stage.side=='LOW' else '#ffb36a'
                    highlighted=region_inspection is not None
                    left=pd.to_datetime(int(stage.visible_from),unit='ms',utc=True)
                    right=pd.to_datetime(int(stage.visible_until),unit='ms',utc=True)
                    if float(stage.band_low)==float(stage.band_high):
                        fig.add_shape(type='line',xref='x',yref='y',x0=left,x1=right,
                                      y0=float(stage.band_low),y1=float(stage.band_high),
                                      line={'color':color,'width':3 if highlighted else 2,'dash':'dot'},opacity=0.85 if highlighted else 0.65)
                    else:
                        fig.add_shape(type='rect',xref='x',yref='y',x0=left,x1=right,
                                      y0=float(stage.band_low),y1=float(stage.band_high),
                                      fillcolor=color,opacity=0.20 if highlighted else 0.10,
                                      line={'color':color,'width':2 if highlighted else 1,'dash':'dot'})
                if mode == 'EJECUCION' and region_inspection is not None:
                    # Stage transition markers are at CONFIRMATION time, not at
                    # the earlier historical pivot. They never reveal future
                    # width or replace the old band with the final geometry.
                    import plotly.graph_objects as go
                    transitions=filtered_stages.loc[
                        (filtered_stages.region_id == region_selection) &
                        (filtered_stages.known_from >= chart_start) &
                        (filtered_stages.known_from <= cutoff) &
                        (filtered_stages.independent_extrema >= region_min)
                    ].copy()
                    if not transitions.empty:
                        fig.add_trace(go.Scatter(
                            x=pd.to_datetime(transitions.known_from,unit='ms',utc=True),
                            y=(transitions.band_low+transitions.band_high)/2,
                            mode='markers',marker={'size':11,'symbol':'diamond',
                              'color':'#efc569','line':{'width':1,'color':'#302612'}},
                            hovertext=[
                                f'Etapa PRE confirmada: {iso(int(r.known_from))}'
                                f'<br>Banda conocida: {float(r.band_low):,.2f}–'
                                f'{float(r.band_high):,.2f} USD'
                                f'<br>Extremos confirmados: {int(r.independent_extrema)}'
                                for r in transitions.itertuples(index=False)],
                            hoverinfo='text',name='Confirmaciones de banda PRE'))
                    if reveal_post and chart_end > cutoff:
                        # The exact PRE-selected stage is a STATIC reference
                        # over POST, even if the stage was just born at cutoff.
                        # It is never updated using a future pivot.
                        stage=region_inspection
                        # POST can begin immediately after a stage is confirmed:
                        # in that case no PRE segment is visible and the loop
                        # above never assigns its local `color`. Resolve the
                        # reference color from the selected PRE band itself.
                        color='#4fd3ae' if stage.side=='LOW' else '#ffb36a'
                        x0=pd.to_datetime(cutoff,unit='ms',utc=True)
                        x1=pd.to_datetime(chart_end,unit='ms',utc=True)
                        fig.add_shape(type='rect',xref='x',yref='y',
                              x0=x0,x1=x1,y0=float(stage.band_low),
                              y1=float(stage.band_high),
                              line={'color':color,'width':1,'dash':'dash'},
                              fillcolor=color,opacity=0.06)
                        fig.add_annotation(x=x0,y=float(stage.band_high),
                             xref='x',yref='y',yanchor='bottom',showarrow=False,
                             text='POST · referencia PRE fija (no ACTIVE)',
                             font={'color':color,'size':10})
        except ValueError as exc:
            st.warning(f'V2: {exc}. Reducí el período o las temporalidades.')
    if mode == 'INSPECTOR' and region_member_details is not None and not region_member_details.empty:
        # These marks are all already-confirmed physical members at PRE cutoff.
        # A physical pivot is displayed at its own timestamp, NOT at confirmation.
        # Its hover says when that physical extremum first became available.
        import plotly.graph_objects as go
        visible_members=region_member_details[
            (region_member_details.plot_ts>=chart_start) &
            (region_member_details.plot_ts<cutoff)].copy()
        if not visible_members.empty:
            fig.add_trace(go.Scatter(
                x=pd.to_datetime(visible_members.plot_ts,unit='ms',utc=True),
                y=visible_members.price,mode='markers',
                marker={'size':15,'symbol':'circle-open','color':'#f9d36a',
                        'line':{'width':3,'color':'#f9d36a'}},
                name='Extremos físicos de región seleccionada',
                hovertext=[
                    f"{r.side} · {float(r.price):,.2f} USD"
                    f"<br>Extremo físico: {iso(int(r.plot_ts))}"
                    f"<br>Primera confirmación: {iso(int(r.first_available_asof))}"
                    f"<br>TF conocidos: {r.tf_set_asof}"
                    for r in visible_members.itertuples(index=False)],
                hoverinfo='text'))
        if len(visible_members)<len(region_member_details):
            st.caption(f'La región tiene {len(region_member_details)} extremos físicos confirmados '
                       f'al corte; {len(visible_members)} están dentro de esta ventana visible.')
    # Explicit viewport: choosing 60 days really shows the full frozen period
    # in general exploration. A selected pivot still gets its PRE/POST window.
    fig.update_xaxes(range=[pd.to_datetime(chart_start,unit='ms',utc=True),
                            pd.to_datetime(chart_end,unit='ms',utc=True)],
                     autorange=False)
    st.caption(f'{"Mapa estructural" if mode == "MAPA" else ("Inspector" if mode == "INSPECTOR" else "Contexto PRE multi-TF")} '
               f'· velas cerradas {display_tf}: {len(candles):,} '
               f'· contexto {iso(chart_start)} → {iso(chart_end)}. '
               'La ventana se dibuja completa; ningún dato se recorta silenciosamente.')
    st.plotly_chart(fig,use_container_width=True,key='ms60_chart',
                    config={'displaylogo':False,'scrollZoom':True})
    v314_structural_plot_ms = (_perf_time.perf_counter()-v314_structural_started)*1000
    with st.expander('⏱️ Diagnóstico de rendimiento · BTC 60D', expanded=False):
        st.json({'freeze_load_ms':round(v314_load_ms,1),
                 'navigation_and_controls_ms':round(v314_navigation_ms,1),
                 'physical_evidence_ms':round(v314_evidence_ms,1),
                 'candles_ms':round(v314_candles_ms,1),
                 'regions_plus_main_chart_ms':round(v314_structural_plot_ms,1),
                 'total_main_view_ms':round(v314_load_ms+v314_navigation_ms+
                       v314_evidence_ms+v314_candles_ms+v314_structural_plot_ms,1),
                 'validated_freeze_cache':bool(cache_verified)})
        st.caption('Tiempos internos del visor hasta el primer gráfico; no incluye '
                   'todo dashboard/app.py, la red ni el dibujo en el navegador. '
                   'El segundo gráfico y los detalles desplegados añaden trabajo.')
    if mode == 'MAPA':
        if region_inspection is not None:
            st.info(f'Región {region_inspection.side} · {float(region_inspection.center):,.2f} USD. '
                    'Abrí el inspector para consultar su historia física.')
            def _open_inspector():
                st.session_state['ms60_v3_mode'] = 'INSPECTOR'
            st.button('Inspeccionar esta banda',on_click=_open_inspector,
                      key='ms60_v3_open_inspector')
        st.caption('Mapa estructural: la opacidad no es confianza estadística; '
                   'las bandas no son niveles ACTIVE ni señales de trading.')
        return
    if mode == 'EJECUCION':
        if region_inspection is None:
            st.info('Seleccioná una banda en el desplegable para verla en el contexto PRE multi-TF. '
                    'Aún no hay señales ejecutables ni órdenes.')
        else:
            st.info(f'Zona {region_inspection.side} · '
                    f'{float(region_inspection.band_low):,.2f}–'
                    f'{float(region_inspection.band_high):,.2f} USD · conocida desde '
                    f'{iso(int(region_inspection.known_from))}. '
                    'Este modo NO determina rechazo, breakout, TP/SL, tamaño ni ejecución. '
                    'Solo se muestran etapas causales. Si revelás POST, la banda '
                    'se proyecta fija exclusivamente como referencia visual, '
                    'sin modificar su geometría PRE ni determinar resultados.')
        if replay_post_status == 'CUTOFF_CENSORED' and reveal_post:
            st.warning('POST limitado por el final del freeze BTC 60D: CENSORED. '
                       'No equivale a ausencia de reacción.')
        st.caption('Próximas capas: HLL as-of, eventos REACTION confirmados y triggers 1m/5m. '
                   'La investigación de triggers se desarrollará y validará por separado.')
        if show_context_panel:
            try:
                second_fig,second_count,second_start=v32_build_context_chart(
                    full,stages=region_stages,selected_region=region_inspection,
                    region_id=str(region_selection or ''),
                    timeframe=context_tf,source_start=start,pre_cutoff=cutoff,
                    chart_end=chart_end,days=context_days,min_extrema=region_min,
                    preaggregated=(v313_preaggregated(full,context_tf,enabled=True)
                                   if cache_verified else None))
                if second_fig is None:
                    st.warning(f'Segundo gráfico {context_tf}: {second_count:,} velas '
                        'completas. Sin velas suficientes o supera el máximo visual '
                        'de 4.000; elegí más días o una temporalidad mayor.')
                else:
                    st.caption(f'Contexto multi-TF {context_tf}: {second_count:,} velas '
                        f'cerradas · {iso(second_start)} → {iso(chart_end)}. '
                        f'Corte PRE común: {iso(cutoff)}. '
                        'Banda seleccionada y POST congelados al mismo PRE.')
                    st.plotly_chart(second_fig,use_container_width=True,
                        key='ms60_v32_context_chart',
                        config={'displaylogo':False,'scrollZoom':True})
            except ValueError as exc:
                st.warning(f'Contexto secundario no disponible: {exc}. '
                           'El gráfico principal permanece intacto.')
        return
    if region_inspection is None:
        st.info('Elegí una región para ver su evidencia física y su historial V2.2/V2.3.')
    with st.expander('🔎 Pivotes confirmados en la ventana · evidencia PRE',expanded=chosen_row is not None):
        view=markers.copy().sort_values('plot_ts',ascending=False)
        if not view.empty:
            view['pivot_utc']=pd.to_datetime(view['plot_ts'],unit='ms',utc=True)
            view['available_first_utc']=pd.to_datetime(view['first_available_asof'],unit='ms',utc=True)
            view['available_last_utc']=pd.to_datetime(view['last_available_asof'],unit='ms',utc=True)
            st.dataframe(view[['pivot_utc','side','price','timeframe','tf_set_asof',
                  'representations_asof','available_first_utc','available_last_utc',
                  'physical_identity_state','pivot_id']].head(1000),
                  hide_index=True,use_container_width=True)
            if len(view)>1000:
                st.caption(f'Tabla: últimas 1000 de {len(view)} filas; el gráfico no se recortó.')
    if region_inspection is not None and region_member_details is not None:
        st.markdown('#### 🔬 Región seleccionada · evidencia física causal')
        s1,s2,s3=st.columns(3)
        with s1:
            st.metric('Extremos conocidos en etapa PRE',int(region_inspection.independent_extrema))
        with s2:
            st.metric('Centro de etapa PRE (USD)',f'{float(region_inspection.center):,.2f}')
        with s3:
            st.metric('Miembros físicos al corte',len(region_member_details))
        st.caption(f"Etapa geométrica desde {iso(int(region_inspection.known_from))} "
                   f"· banda PRE {float(region_inspection.band_low):,.2f}–"
                   f"{float(region_inspection.band_high):,.2f} USD. "
                   'El detalle enumera todos los miembros confirmados hasta el corte; '
                   'algunos pueden haberse incorporado después de la etapa mostrada. '
                   'No certifica defensa, ruptura, departure ni calidad predictiva.')
        details=region_member_details.copy()
        details['extremo_UTC']=pd.to_datetime(details.plot_ts,unit='ms',utc=True)
        details['primera_confirmacion_UTC']=pd.to_datetime(
            details.first_available_asof,unit='ms',utc=True)
        details['ultima_confirmacion_UTC']=pd.to_datetime(
            details.last_available_asof,unit='ms',utc=True)
        st.dataframe(details[['physical_id','side','price','extremo_UTC',
                    'primera_confirmacion_UTC','ultima_confirmacion_UTC',
                    'tf_set_asof','representations_asof']],
                    hide_index=True,use_container_width=True)
    inspect_history = st.checkbox('Abrir historial de contactos y visitas · V2.2 / V2.3',
                                  value=False,key='ms60_v3_inspector_history') if region_inspection is not None else False
    if mode == 'INSPECTOR' and inspect_history and region_inspection is not None and show_regions:
        st.markdown('#### 🧭 V2.2 · Historial físico de la banda fija · retrospectivo')
        st.caption('La banda se CONGELA con límites conocidos en la etapa PRE seleccionada. '
                   'Los eventos posteriores se reconstruyen solo con velas BTC 1m ya cerradas '
                   'al corte. CONTACTO = intersección del rango 1m con la banda; '
                   'CRUCE = cambio de lado por CIERRE; DOS CIERRES = racha descriptiva. '
                   'No significa defensa, resistencia vigente, ruptura validada ni señal LIVE.')
        horizon=int(st.selectbox(
            'Horizonte de excursión después del contacto (minutos)',
            [60,240,720],index=1,key='ms60_v22_excursion_horizon'))
        try:
            events_v22, contacts_v22, audit_v22 = audit_fixed_region(
                full,region_inspection,cutoff,horizon_minutes=horizon)
            compatible_v22 = later_compatible_extrema(asof,region_inspection,cutoff)
        except (ValueError,KeyError,TypeError) as exc:
            st.warning(f'V2.2 no pudo reconstruir la trayectoria: {exc}. '
                       'No se fabrican eventos a partir de datos incompletos.')
        else:
            q1,q2,q3,q4=st.columns(4)
            q1.metric('Episodios de contacto 1m',audit_v22['contact_episodes'])
            q2.metric('Cruces por cierre arriba',audit_v22['close_cross_above'])
            q3.metric('Cruces por cierre abajo',audit_v22['close_cross_below'])
            q4.metric('Nuevos extremos HIGH/LOW compatibles',len(compatible_v22))
            st.caption(
                f"Etapa fijada: {iso(audit_v22['stage_known_from'])} · "
                f"banda {audit_v22['fixed_band_low']:,.2f}–"
                f"{audit_v22['fixed_band_high']:,.2f} USD · "
                f"{audit_v22['closed_minutes']:,} velas 1m cerradas hasta "
                f"{iso(audit_v22['cutoff'])}. "
                f"Dos cierres consecutivos arriba: {audit_v22['two_closes_above']}; "
                f"abajo: {audit_v22['two_closes_below']}. "
                'Una excursión POST-contacto solo se conoce posteriormente; '
                'el estado CENSORED no se interpreta como cero movimiento.')

            # This is a visual-only window; V2.2 audit_fixed_region() above
            # always starts at the selected causal stage and does not depend on
            # context controls. No future pivots, no stage modification, no LIVE.
            import plotly.graph_objects as go
            context_labels={
                'FULL_60D':'BTC 60D completo hasta el corte',
                'FROM_STAGE':'Desde confirmación de la etapa PRE',
                'LAST_7D':'Últimos 7 días hasta el corte',
            }
            v22c1,v22c2=st.columns([3,1])
            with v22c1:
                v22_context=st.selectbox('Contexto del historial V2.2',
                    list(context_labels),format_func=context_labels.get,
                    index=0,key='ms60_v22_visual_context')
            with v22c2:
                v22_chart_tf=st.selectbox('Velas del historial V2.2',
                    ['30m','1h','4h'],index=0,key='ms60_v22_chart_tf')
            chart_v22_start,_=v22_visual_context_window(
                source_start=start,cutoff=cutoff,
                stage_known_from=int(region_inspection.known_from),
                context=v22_context)
            chart_v22=(aggregate_closed(full,v22_chart_tf,chart_v22_start,cutoff)
                       if chart_v22_start<cutoff else pd.DataFrame())
            st.caption(f'Vista V2.2: {iso(chart_v22_start)} → {iso(cutoff)} '
                       f'· {len(chart_v22):,} velas {v22_chart_tf} completas. '
                       f'Banda fija conocida desde {iso(int(region_inspection.known_from))}; '
                       'nunca se dibuja antes de esa etapa. '
                       'Los contactos y recorridos se calculan siempre sobre '
                       'todo el historial causal de la etapa, no sobre este zoom.')
            max_v22_candles=4000
            if not chart_v22.empty and len(chart_v22)<=max_v22_candles:
                fig_v22=go.Figure(go.Candlestick(
                    x=pd.to_datetime(chart_v22.timestamp,unit='ms',utc=True),
                    open=chart_v22.open,high=chart_v22.high,
                    low=chart_v22.low,close=chart_v22.close,
                    increasing_line_color='#43ae90',
                    decreasing_line_color='#df7070',showlegend=False))
                left=max(chart_v22_start,int(region_inspection.known_from))
                t0=pd.to_datetime(left,unit='ms',utc=True)
                t1=pd.to_datetime(cutoff,unit='ms',utc=True)
                bottom=float(region_inspection.band_low)
                top=float(region_inspection.band_high)
                fig_v22.add_shape(type='rect',xref='x',yref='y',
                    x0=t0,x1=t1,y0=bottom,y1=top,
                    line={'color':'#ffc477','width':1.5,'dash':'dot'},
                    fillcolor='#ffc477',opacity=0.16)
                fig_v22.add_annotation(x=t0,y=top,xref='x',yref='y',
                    text='Banda PRE fija · referencia retrospectiva',
                    showarrow=False,yanchor='bottom',font={'color':'#ffc477','size':10})
                highlight=events_v22[events_v22.event_type.isin([
                    'CONTACT_START','TWO_CLOSES_ABOVE','TWO_CLOSES_BELOW'])].copy()
                highlight=highlight.loc[
                    (highlight.event_ts>=chart_v22_start) &
                    (highlight.event_ts<=cutoff)]
                if len(highlight)<=300:
                    for kind,marker,color,ypos in (
                        ('CONTACT_START','diamond','#f4d27a',(bottom+top)/2),
                        ('TWO_CLOSES_ABOVE','triangle-up','#bd99f2',top),
                        ('TWO_CLOSES_BELOW','triangle-down','#7ebfe8',bottom)):
                        part=highlight[highlight.event_type.eq(kind)]
                        if not part.empty:
                            fig_v22.add_trace(go.Scatter(
                                x=pd.to_datetime(part.event_ts,unit='ms',utc=True),
                                y=[ypos]*len(part),mode='markers',
                                marker={'symbol':marker,'size':9,'color':color},
                                name=kind.replace('_',' ').title(),
                                hovertext=[f"{r.description} · {iso(int(r.event_ts))}"
                                           for r in part.itertuples(index=False)],
                                hoverinfo='text'))
                else:
                    st.caption(f'Hay {len(highlight)} marcadores de eventos; '
                               'se mantienen en la tabla y se omiten del gráfico '
                               'para evitar saturación.')
                fig_v22.update_layout(title=f'BTC · {v22_chart_tf} · banda PRE fija · contexto {context_labels[v22_context]}',
                    height=510,xaxis_rangeslider_visible=False,
                    xaxis_title='UTC',yaxis_title='USD',
                    margin={'l':25,'r':20,'t':45,'b':25},
                    legend={'orientation':'h','y':-0.16})
                fig_v22.update_xaxes(
                    range=[pd.to_datetime(chart_v22_start,unit='ms',utc=True),
                           pd.to_datetime(cutoff,unit='ms',utc=True)],
                    autorange=False)
                st.plotly_chart(fig_v22,use_container_width=True,
                    key='ms60_v22_fixed_region_chart',
                    config={'displaylogo':False,'scrollZoom':True})
            else:
                if len(chart_v22)>max_v22_candles:
                    st.warning(f'Historial V2.2: {len(chart_v22):,} velas exceden '
                               f'el máximo visual de {max_v22_candles:,}. '
                               'Seleccioná velas 1h o 4h; la auditoría 1m '
                               'completa no se recorta.')
                else:
                    st.info('No hay velas cerradas entre el inicio del contexto '
                            'y el corte elegido. Podés seleccionar 60D completo '
                            'para ver el nacimiento de la banda.')

            st.markdown('**Contactos físicos y excursiones posteriores**')
            if contacts_v22.empty:
                st.info('No se encontraron intersecciones 1m después de fijar esta etapa.')
            else:
                table22=contacts_v22.copy()
                for col in ('start_ts','last_touch_close_ts','end_known_ts','post_known_until'):
                    table22[col.replace('_ts','_UTC').replace('_until','_until_UTC')]=pd.to_datetime(
                        table22[col],unit='ms',utc=True)
                keep=['contact_number','start_UTC','last_touch_close_UTC',
                      'touch_minutes','body_overlap_minutes','status',
                      'post_minutes_observed','max_above_band_pct',
                      'max_below_band_pct','post_status','post_known_until_UTC']
                st.dataframe(table22[keep].head(250),hide_index=True,use_container_width=True)
                if len(table22)>250:
                    st.caption(f'Se ven 250 de {len(table22)} contactos; descarga completa disponible.')
                st.download_button('Exportar contactos V2.2 · CSV',
                    contacts_v22.to_csv(index=False).encode('utf-8'),
                    file_name='btc_multiswing_region_v22_contacts.csv',
                    mime='text/csv',key='ms60_v22_contacts_export')
            with st.expander('Todos los eventos por cierre 1m · V2.2',expanded=False):
                e22=events_v22.copy()
                if not e22.empty:
                    e22['evento_UTC']=pd.to_datetime(e22.event_ts,unit='ms',utc=True)
                    st.dataframe(e22[['evento_UTC','event_type','close_price',
                        'contact_number','description']].head(500),
                        hide_index=True,use_container_width=True)
                    if len(e22)>500:
                        st.caption(f'Primeros 500 de {len(e22)} eventos; descarga completa disponible.')
                st.download_button('Exportar eventos V2.2 · CSV',
                    events_v22.to_csv(index=False).encode('utf-8'),
                    file_name='btc_multiswing_region_v22_events.csv',
                    mime='text/csv',key='ms60_v22_events_export')
            with st.expander('Nuevos extremos físicos compatibles con la banda fija',expanded=False):
                st.caption('Extremos VERIFIED confirmados DESPUÉS de la etapa fijada, '
                           'dentro de la banda original y con los TF seleccionados. '
                           'No equivalen automáticamente a nuevas defensas ni a una región sucesora.')
                if compatible_v22.empty:
                    st.info('No se encontraron nuevos extremos compatibles hasta el corte.')
                else:
                    new22=compatible_v22.copy()
                    new22['extremo_UTC']=pd.to_datetime(new22.plot_ts,unit='ms',utc=True)
                    new22['confirmado_UTC']=pd.to_datetime(
                        new22.first_available_asof,unit='ms',utc=True)
                    st.dataframe(new22[['physical_id','side','price','extremo_UTC',
                        'confirmado_UTC','tf_set_asof']],
                        hide_index=True,use_container_width=True)
            st.caption('V2.2 es exclusivamente retrospectivo. '
                       'Las bandas visibles después de su horizonte geométrico '
                       'NO están ACTIVE; ningún resultado se publica a estrategias LIVE.')

            st.markdown('#### 🧪 V2.3 · Visitas estructurales y recorridos posteriores')
            st.caption('Agrupa los contactos V2.2 si entre ellos transcurrieron '
                       'como máximo 5, 15, 30 o 60 minutos sin tocar la banda. '
                       'No clasifica defensas ni rupturas. El FIN de una visita '
                       'se conoce únicamente después de gap+1 velas sin contacto '
                       'cerradas; los recorridos POST comienzan recién ahí.')
            vg1,vg2=st.columns(2)
            with vg1:
                gap_v23=int(st.selectbox('Separación máxima para unir contactos (min)',
                    [5,15,30,60],index=2,key='ms60_v23_gap'))
            with vg2:
                horizon_v23=int(st.selectbox('Horizonte POST para comparar (min)',
                    [60,240,720,1440,2880],index=1,key='ms60_v23_horizon'))
            try:
                visits_v23, outcomes_v23, vs23 = audit_structural_visits(
                    full,region_inspection,cutoff,gap_minutes=gap_v23)
                sensitivity_v23=visit_gap_sensitivity(contacts_v22)
            except (ValueError,KeyError,TypeError) as exc:
                st.warning(f'V2.3 no pudo analizar visitas: {exc}. '
                           'No se extrapolan velas ni se completan horizontes censurados.')
            else:
                v1,v2,v3,v4=st.columns(4)
                v1.metric('Contactos V2.2',vs23['contact_episodes_v22'])
                v2.metric('Visitas agrupadas',vs23['visits'])
                v3.metric('Visitas finalizadas y conocidas',vs23['visits_closed'])
                v4.metric('Visitas abiertas / no cerradas',vs23['visits_open'])
                st.caption('Sensibilidad del mismo historial a distintos intervalos de unión: '
                           + ' · '.join(f"{int(r.gap_minutes)}m → {int(r.visits)} visitas"
                           for r in sensitivity_v23.itertuples(index=False)) + '. '
                           'El umbral es exploratorio; no se eligió para maximizar un resultado.')
                if visits_v23.empty:
                    st.info('No hay contactos para construir visitas con la etapa PRE seleccionada.')
                else:
                    vt23=visits_v23.copy()
                    for col in ('first_touch_ts','first_known_ts',
                                'last_touch_close_ts','end_known_ts'):
                        vt23[col.replace('_ts','_UTC')]=pd.to_datetime(
                            vt23[col],unit='ms',utc=True)
                    st.dataframe(vt23[[
                        'visit_number','first_touch_UTC','last_touch_close_UTC',
                        'end_known_UTC','status','arrival_side','exit_side',
                        'contact_episodes','touch_minutes','bridged_noncontact_minutes',
                        'two_closes_above','two_closes_below',
                        'in_visit_max_above_pct','in_visit_max_below_pct',
                    ]].head(250),hide_index=True,use_container_width=True)
                    if len(vt23)>250:
                        st.caption(f'Mostradas 250 de {len(vt23)} visitas; CSV contiene todas.')
                    st.download_button('Exportar visitas V2.3 · CSV',
                        visits_v23.to_csv(index=False).encode('utf-8'),
                        file_name='btc_multiswing_region_v23_visits.csv',
                        mime='text/csv',key='ms60_v23_visits_export')
                    st.download_button('Exportar resultados POST V2.3 · CSV',
                        outcomes_v23.to_csv(index=False).encode('utf-8'),
                        file_name='btc_multiswing_region_v23_outcomes.csv',
                        mime='text/csv',key='ms60_v23_outcomes_export')

                    choices=visits_v23.visit_number.astype(int).tolist()
                    selected_visit=st.selectbox('Inspeccionar una visita estructural',
                        choices,index=len(choices)-1,
                        format_func=lambda num: (
                            f"Visita #{num} · "
                            f"{iso(int(visits_v23.iloc[num-1].first_touch_ts))} · "
                            f"{int(visits_v23.iloc[num-1].contact_episodes)} contactos V2.2"),
                        key='ms60_v23_selected_visit')
                    visit=visits_v23.loc[
                        visits_v23.visit_number.eq(selected_visit)].iloc[0]
                    st.caption(
                        f"Visita #{selected_visit} · llegada {visit.arrival_side} "
                        f"(cierre 1m anterior) · primera evidencia conocida "
                        f"{iso(int(visit.first_known_ts))} · cierre de visita: "
                        f"{iso(int(visit.end_known_ts)) if pd.notna(visit.end_known_ts) else 'PENDIENTE'} "
                        f"· estado {visit.status}. El cierre no se conoce en el primer contacto.")
                    selected_outcomes=outcomes_v23[
                        outcomes_v23.visit_number.eq(selected_visit)].copy()
                    selected_outcomes['desde_UTC']=pd.to_datetime(
                        selected_outcomes.evaluation_from_ts,unit='ms',utc=True)
                    selected_outcomes['conocido_hasta_UTC']=pd.to_datetime(
                        selected_outcomes.last_known_ts,unit='ms',utc=True)
                    st.dataframe(selected_outcomes[[
                        'horizon_minutes','desde_UTC','observed_minutes','status',
                        'max_above_band_pct','max_below_band_pct','conocido_hasta_UTC',
                    ]],hide_index=True,use_container_width=True)
                    chosen_post=selected_outcomes.loc[
                        selected_outcomes.horizon_minutes.eq(horizon_v23)].iloc[0]
                    st.caption(
                        f"POST {horizon_v23}m para visita #{selected_visit}: "
                        f"{chosen_post.status} · {int(chosen_post.observed_minutes)} "
                        'minutos observados. FULL_HORIZON exige todas las velas; '
                        'CENSORED nunca significa movimiento cero.')

                    # Zoom on one visit. No future bars beyond cutoff and no
                    # recomputation of the selected V2 stage/band.
                    zoom_left=max(int(region_inspection.known_from),
                                  int(visit.first_touch_ts)-3*60*M)
                    zoom_right=min(cutoff,max(int(visit.last_touch_close_ts)+6*60*M,
                                             int(visit.first_touch_ts)+4*60*M))
                    zoom_tf='5m' if zoom_right-zoom_left<=2*DAY else '30m'
                    local_candles=aggregate_closed(full,zoom_tf,zoom_left,zoom_right)
                    if not local_candles.empty and len(local_candles)<=900:
                        fig23=go.Figure(go.Candlestick(
                            x=pd.to_datetime(local_candles.timestamp,unit='ms',utc=True),
                            open=local_candles.open,high=local_candles.high,
                            low=local_candles.low,close=local_candles.close,
                            increasing_line_color='#43ae90',
                            decreasing_line_color='#df7070',showlegend=False))
                        ltime=pd.to_datetime(zoom_left,unit='ms',utc=True)
                        rtime=pd.to_datetime(zoom_right,unit='ms',utc=True)
                        fig23.add_shape(type='rect',xref='x',yref='y',
                            x0=ltime,x1=rtime,
                            y0=float(region_inspection.band_low),
                            y1=float(region_inspection.band_high),
                            fillcolor='#ffc477',opacity=0.14,
                            line={'color':'#ffc477','width':1,'dash':'dot'})
                        for when,kind,color in (
                            (int(visit.first_known_ts),'Primer contacto confirmado','#f9d36a'),
                            (int(visit.last_touch_close_ts),'Último contacto','#70d5c0'),
                        ):
                            dt=pd.to_datetime(when,unit='ms',utc=True)
                            fig23.add_shape(type='line',xref='x',yref='paper',
                                x0=dt,x1=dt,y0=0,y1=1,
                                line={'color':color,'width':1.5,'dash':'dot'})
                            fig23.add_annotation(x=dt,y=0.98,xref='x',yref='paper',
                                text=kind,showarrow=False,
                                font={'color':color,'size':10})
                        if pd.notna(visit.end_known_ts):
                            done_at=pd.to_datetime(int(visit.end_known_ts),
                                                   unit='ms',utc=True)
                            fig23.add_shape(type='line',xref='x',yref='paper',
                                x0=done_at,x1=done_at,y0=0,y1=1,
                                line={'color':'#d7a5ff','width':1.5,'dash':'dash'})
                            fig23.add_annotation(x=done_at,y=0.06,xref='x',yref='paper',
                                text='Fin conocido',showarrow=False,
                                font={'color':'#d7a5ff','size':10})
                        fig23.update_layout(
                            title=f'Visita #{selected_visit} · BTC {zoom_tf} · banda PRE fija',
                            height=490,xaxis_rangeslider_visible=False,
                            margin={'l':25,'r':20,'t':50,'b':20},
                            xaxis_title='UTC',yaxis_title='USD')
                        st.plotly_chart(fig23,use_container_width=True,
                            key='ms60_v23_visit_chart',
                            config={'displaylogo':False,'scrollZoom':True})
                    else:
                        st.caption('Zoom V2.3 no representable; permanecen las tablas y CSV.')
                st.caption('V2.3 es investigación OFFLINE/retrospectiva. '
                           'Cada visita se conoce progresivamente; los resultados POST '
                           'no eran conocidos en el primer contacto. '
                           'No hay scoring, HLL, órdenes ni señales LIVE.')

    if show_regions and regions is not None:
        with st.expander('🧩 V2 · Regiones por extremos físicos independientes · PRE',expanded=False):
            if regions.empty:
                st.info('No se formaron regiones con los criterios seleccionados. '
                        'No equivale a ausencia de estructura real.')
            else:
                table=regions[(regions.born_ts<=cutoff)].copy()
                if table.empty:
                    st.info('No hay regiones formadas hasta el corte elegido.')
                else:
                    table['formada_UTC']=pd.to_datetime(table.born_ts,unit='ms',utc=True)
                    table['ultimo_pivote_UTC']=pd.to_datetime(table.last_pivot_ts,unit='ms',utc=True)
                    st.dataframe(table[['region_id','side','center','band_low','band_high',
                        'independent_extrema','formada_UTC','ultimo_pivote_UTC','temporalities',
                        'status']].sort_values('formada_UTC',ascending=False).head(250),
                        hide_index=True,use_container_width=True)
                    if len(table)>250:
                        st.caption(f'Tabla: primeras 250 de {len(table)} regiones; '
                                   'la reconstrucción interna no se recortó.')
            st.caption('Construcción por confirmaciones conocidas al corte. '
                       'No identifica aún departure, defensas, rupturas, HLL ni outcomes. '
                       'La extensión dibujada se limita por la ventana de exploración y es descriptiva, '
                       'NO vigencia operativa.')
    with st.expander('📦 Procedencia y límites de interpretación',expanded=False):
        st.json({'symbol':'BTCUSDT','catalog':'6TF v1.1','source_sha256':source['source_sha256'],
                 'catalog_sha256':source['catalog_sha256'],
                 'HLL_30m_parity':source['source_hll_parity'],
                 'start':iso(start),'end_exclusive':iso(end),
                 'plot_end':iso(chart_end),'evidence_cutoff':iso(cutoff),
                 'confirmed_records_asof':len(asof),'rendered_markers':len(markers),
                 'rendered_closed_candles':len(candles)})
        st.caption('Las marcas se posicionan en el extremo/pivote histórico; '
                   'su disponibilidad causal se determina por available_ts. '
                   'TIED_EXTREMA no se agrupa en una falsa identidad física. '
                   'Departure, cruces, HLL y outcomes aún NO se calculan en este visor V1.')
