"""Focused Explore must never select winners or reveal unpublished data."""
import copy
import pytest
from dashboard.services.micro_flow_v3_explore_service import (
    FOCUS_OPTIONS, build_explore_catalog, filter_explore_events, observed_view,
)

BASE = 1792393200000


def _rows():
    data = []
    cvd = 0.0
    for i in range(720):
        t = BASE + i*1000
        if i in range(185, 191):
            buy, sell = 150.0, 1.0
        elif i in range(420, 426):
            buy, sell = 1.0, 160.0
        else:
            buy, sell = 1.1, 1.0
        cvd += buy-sell
        px = 2400 + i*0.001
        data.append({
            'event_type': 'BUCKET_1S', 'symbol':'ETHUSDT', 'segment_id': 1,
            'second_start_ms':t,'second_end_ms_exclusive':t+1000,
            'available_wall_ns':(t+1150)*1_000_000,
            'available_ms':t+1150,
            'first_seq':i*2+1, 'last_seq':i*2+2,
            'first_agg_id':i*2+1,'last_agg_id':i*2+2,
            'open':str(px),'high':str(px+.01), 'low':str(px-.01),
            'close':str(px), 'buy_qty':str(buy),'sell_qty':str(sell),
            'delta_qty':str(buy-sell),'cvd_qty':str(cvd),'trades':4,
        })
    return data


def _toy_event(kind='AE', label='Divergencia flujo/precio', rejection=False, pressure='BUY'):
    return {'kind':kind,'label':label,'rejection':rejection,
            'pressure_side':pressure,'event_id':f'{kind}:{label}:{pressure}'}


def test_focus_filters_do_not_use_outcomes_or_future():
    events = [
        dict(_toy_event(), future_result='LOSS', failure='NEVER'),
        dict(_toy_event(), future_result='WIN', failure='CONFIRMED'),
        _toy_event('AE','Poco desplazamiento',True),
        _toy_event('AE','Flujo y precio alineados'),
        _toy_event('VE','Volume Exhaustion (RSI + volumen, experimental)'),
    ]
    assert len(filter_explore_events(events)) == 2
    assert len(filter_explore_events(events,focus='Rechazo observable')) == 1
    assert len(filter_explore_events(events,focus='Volume Exhaustion 1m/5m')) == 1
    assert len(filter_explore_events(events,focus='Todas las agresiones')) == 4
    assert len(filter_explore_events(events,focus='Divergencia flujo/precio', pressure='SELL')) == 0
    for value in (None,'FAILURE_CONFIRMED'):
        for event in events:
            event['future_result'] = value
        assert len(filter_explore_events(events)) == 2


def test_strict_asof_view_excludes_unpublished_candles_and_buckets():
    t = BASE + 60_000
    # Earlier timestamp is not proof of causal availability.
    candles=[{'timestamp':t-30_000, 'available_ms':t-100,'segment_id':1},
             {'timestamp':t-20_000, 'available_ms':t+4000,'segment_id':1}]
    buckets=[{'second_start_ms':t-1000,'available_wall_ns':(t+500)*1_000_000,'segment_id':1},
             {'second_start_ms':t-2000,'available_wall_ns':(t-100)*1_000_000,'segment_id':1},
             {'second_start_ms':t-2000,'available_wall_ns':(t-100)*1_000_000,'segment_id':2}]
    result=observed_view(buckets,candles,segment_id=1,anchor_ms=t,before_minutes=2,reveal=False)
    assert len(result['candles'])==1
    assert len(result['buckets'])==1
    future=observed_view(buckets,candles,segment_id=1,anchor_ms=t,before_minutes=2,
                         after_minutes=1,reveal=True)
    assert len(future['candles'])==2
    assert len(future['buckets'])==2


def test_catalog_does_not_modify_original_buckets_and_preserves_all_impulses():
    raw=_rows()
    original=copy.deepcopy(raw)
    study=build_explore_catalog(raw,timeframe_minutes=1,feature_window_seconds=15)
    assert raw==original
    ae=[e for e in study['events'] if e['kind']=='AE']
    assert len(ae)==len(study['aggression']['events'])
    assert len(ae)==len(study['features']['features'])
    assert all(e['feature'] is not None for e in ae)
    assert len({e['event_id'] for e in study['events']})==len(study['events'])
    for e in ae:
        assert e['feature']['available_ms'] <= e['available_ms']
    assert study['parameters']['feature_window_seconds']==15


def test_catalog_rejects_invalid_ui_settings():
    with pytest.raises(ValueError):
        build_explore_catalog([],timeframe_minutes=30)
    with pytest.raises(ValueError):
        build_explore_catalog([],feature_window_seconds=7)
    with pytest.raises(ValueError):
        filter_explore_events([],focus='Ganadores del futuro')
