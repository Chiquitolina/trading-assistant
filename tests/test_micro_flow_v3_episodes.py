"""Pure tests of episode grouping, directional outcome conventions & censoring."""
import math
import unittest

from dashboard.services.micro_flow_v3_research import (
    cluster_pressure_events, classify_observed_outcome,
    outcome_comparison, detect_pressure_events,
)

BASE = 1_800_000_000_000


def mk_event(i, side='BUY_PRESSURE', segment=1, *, ms=None):
    ready = BASE + i*1000 if ms is None else ms
    return {'event_id':f'{segment}:{ready}:{side}', 'segment_id':segment,
            'ready_ms':ready, 'side':side, 'entry_ms':ready+1000,
            'entry_proxy':2300, 'return_30s_pct':0.03,
            'return_60s_pct':None, 'return_180s_pct':None}


def synthetic_pressure(*, spike_direction=-1, after_rate=0.1, length=205):
    """Spike observed at second 7; future price path may favor or oppose."""
    result=[]
    for i in range(length):
        px=100 + (max(0,i-8)*after_rate)
        high = px + 0.01
        low = px - 0.01
        vol_buy, vol_sell=(30.0,.1) if spike_direction>0 else (.1,30.0)
        if i != 7:
            vol_buy,vol_sell=(1.0,1.0)
        start=BASE+i*1000
        result.append({'segment_id':1,'second_start_ms':start,
                       'available_ms':start+1100,'buy_qty':str(vol_buy),
                       'sell_qty':str(vol_sell),'trades':10 if i==7 else 2,
                       'open':str(px),'high':str(high), 'low':str(low), 'close':str(px)})
    return result


class EpisodeV3Tests(unittest.TestCase):
    def test_fixed_180_second_windows(self):
        rows=[mk_event(i) for i in (0,25,67,119,179,180,181,359,360)]
        annotated, eps=cluster_pressure_events(rows,window_seconds=180)
        self.assertEqual([e['trigger_count'] for e in eps],[5,3,1])
        self.assertEqual(len({e['event_id'] for e in annotated}),9)
        self.assertEqual(sum(e['is_episode_anchor'] for e in annotated),3)
        self.assertEqual(sum(e['trigger_count'] for e in eps),len(rows))
        self.assertEqual([eps[i]['anchor_ready_ms'] for i in range(3)],
                         [BASE,BASE+180000,BASE+360000])

    def test_mixed_sides_and_side_anchor_not_rewritten(self):
        events=[mk_event(0,'BUY_PRESSURE'),mk_event(30,'SELL_PRESSURE'),
                mk_event(195,'SELL_PRESSURE')]
        annotated,eps=cluster_pressure_events(events)
        self.assertTrue(eps[0]['mixed_sides'])
        self.assertEqual(eps[0]['episode_side'],'MIXED')
        self.assertEqual(eps[0]['anchor_side'],'BUY_PRESSURE')
        self.assertEqual(eps[0]['buy_triggers'],1)
        self.assertEqual(eps[0]['sell_triggers'],1)
        self.assertEqual(eps[1]['episode_side'],'SELL_PRESSURE')
        self.assertFalse(annotated[1]['is_episode_anchor'])

    def test_segment_boundaries_and_scrambled_input(self):
        rows=[mk_event(10,'BUY_PRESSURE',segment=2),
              mk_event(20,'BUY_PRESSURE',segment=1),
              mk_event(0,'SELL_PRESSURE',segment=1)]
        annotated,eps=cluster_pressure_events(rows)
        self.assertEqual(len(eps),2)
        self.assertEqual(eps[0]['segment_id'],1)
        self.assertEqual(eps[1]['segment_id'],2)
        self.assertEqual(annotated[0]['side'],'SELL_PRESSURE')

    def test_anchor_comparison_avoids_secondary_duplicate(self):
        rows=[mk_event(0,'BUY_PRESSURE'),mk_event(20,'BUY_PRESSURE'),
              mk_event(50,'BUY_PRESSURE'),mk_event(190,'BUY_PRESSURE')]
        rows[1]['return_30s_pct']=-.05
        rows[2]['return_30s_pct']=-.05
        annotated, eps=cluster_pressure_events(rows)
        anchors=[r for r in annotated if r['is_episode_anchor']]
        full=[r for r in outcome_comparison(annotated) if r['side']=='BUY_PRESSURE'
              and r['horizon_s']==30][0]
        independent=[r for r in outcome_comparison(anchors) if r['side']=='BUY_PRESSURE'
                     and r['horizon_s']==30][0]
        self.assertEqual(full['observed'],4)
        self.assertEqual(full['reversal'],2)
        self.assertEqual(independent['observed'],2)
        self.assertEqual(independent['reversal'],0)

    def test_sell_price_rise_is_negative_directional_and_adverse_mae(self):
        event=detect_pressure_events(
            synthetic_pressure(spike_direction=-1,after_rate=.10),
            lookback=5,relative_volume=2,min_imbalance=.7,
            min_trades=3,cooldown_seconds=0)[0]
        self.assertEqual(event['direction'],-1)
        self.assertLess(event['return_30s_pct'],0)
        self.assertLess(event['mae_30s_pct'],0)
        self.assertLess(event['directional_mae_30s_pct'],0)
        self.assertGreaterEqual(event['directional_mfe_30s_pct'],0)
        self.assertAlmostEqual(event['mae_30s_pct'],event['directional_mae_30s_pct'])
        self.assertGreater(event['price_high_excursion_30s_pct'],0)
        self.assertEqual(event['outcome_state_30s'],'OBSERVED')

    def test_sell_price_fall_is_positive_directional_and_favorable_mfe(self):
        event=detect_pressure_events(
            synthetic_pressure(spike_direction=-1,after_rate=-.10),
            lookback=5,relative_volume=2,min_imbalance=.7,
            min_trades=3,cooldown_seconds=0)[0]
        self.assertGreater(event['return_30s_pct'],0)
        self.assertGreater(event['directional_mfe_30s_pct'],0)
        self.assertLessEqual(event['directional_mae_30s_pct'],0)
        self.assertLess(event['price_low_excursion_30s_pct'],0)

    def test_buy_price_rise_and_censored_horizon(self):
        event=detect_pressure_events(
            synthetic_pressure(spike_direction=1,after_rate=.1,length=80),
            lookback=5,relative_volume=2,min_imbalance=.7,min_trades=3)[0]
        self.assertGreater(event['return_30s_pct'],0)
        self.assertGreater(event['directional_mfe_30s_pct'],0)
        self.assertIsNone(event['return_180s_pct'])
        self.assertEqual(event['outcome_state_180s'],'HORIZON_UNAVAILABLE')
        self.assertIsNone(event['exit_observed_ms_180s'])

    def test_classifier_including_censored_and_neutral(self):
        self.assertEqual(classify_observed_outcome(None),'CENSORED')
        self.assertEqual(classify_observed_outcome(-.12),'REVERSAL')
        self.assertEqual(classify_observed_outcome(.12),'CONTINUATION')
        self.assertEqual(classify_observed_outcome(.005),'NEUTRAL')
        self.assertEqual(classify_observed_outcome(.005,neutral_band_pct=0),'CONTINUATION')

    def test_comparison_explicitly_counts_censoring(self):
        rows=[mk_event(1,'SELL_PRESSURE'),mk_event(181,'SELL_PRESSURE')]
        rows[0]['return_30s_pct']=-.5
        rows[1]['return_30s_pct']=None
        r=[r for r in outcome_comparison(rows) if r['side']=='SELL_PRESSURE'
           and r['horizon_s']==30][0]
        self.assertEqual(r['signals'],2)
        self.assertEqual(r['censored'],1)
        self.assertEqual(r['observed'],1)
        self.assertEqual(r['reversal'],1)
        self.assertEqual(r['continuation_pct'],0)

    def test_no_segment_crossing_in_outcome(self):
        a=synthetic_pressure(length=12)
        b=synthetic_pressure(length=70)
        for row in b:
            row['segment_id']=2
        events=detect_pressure_events(a+b,lookback=5,relative_volume=2,
                                      min_imbalance=.7,min_trades=3)
        self.assertEqual(len(events),2)
        self.assertIsNone(events[0]['return_30s_pct'])
        self.assertIsNotNone(events[1]['return_30s_pct'])

    def test_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            cluster_pressure_events([mk_event(0)],window_seconds=0)


if __name__=='__main__':
    unittest.main()
