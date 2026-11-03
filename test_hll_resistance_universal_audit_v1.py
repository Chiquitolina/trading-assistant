import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hll_resistance_universal_audit_v1 import (
    TF, ms, price_behavior, continuation_gates, replay_frozen, bar_map,
)
from dashboard.views.hll_resistance_universal_audit_view import (
    level_formation_atr, add_behavior_markers,
)


PRE=1_800_000_000_000  # exact 30m-aligned epoch ms
assert PRE % TF == 0


def candles(seq):
    """seq = (open,high,low,close) at PRE + offset*TF, prepend PRE-1."""
    src={}
    for i,(o,h,l,c) in enumerate(seq):
        ts=PRE+i*TF
        src[ts]=dict(open_ts=ts,close_ts=ts+TF,open=o,high=h,low=l,close=c)
    ts=PRE-TF
    src[ts]=dict(open_ts=ts,close_ts=PRE,open=10.5,high=10.9,low=10.4,close=10.5)
    return src


class UniversalAuditTests(unittest.TestCase):
    def test_timestamp_decimal_exact_and_fractional_rejected(self):
        self.assertEqual(ms(f'{PRE}.0'),PRE)
        with self.assertRaises(ValueError):ms(f'{PRE}.5')

    def test_independent_physical_touch_from_above_and_later_defense(self):
        path=candles([
            (10.9,11.8,10.8,11.4),
            (11.4,11.7,10.9,11.1),
            (11.1,12.0,11.05,11.8),
            (11.8,12.0,9.3,9.5),
            (9.5,11.9,9.4,11.3),
        ])
        result=price_behavior(path,pre_closed_ts=PRE,breakout_closed_ts=PRE+TF,
            zone_low=10,zone_high=11,formation_atr=1,horizon=5)
        ev=result['events']
        self.assertTrue(any(x['event']=='CONTACTO_DESDE_ARRIBA' and x['bar_offset']==1 for x in ev))
        self.assertTrue(any(x['event']=='DEFENSA_DESCRIPTIVA' and x['bar_offset']==2 for x in ev))
        self.assertTrue(any(x['event']=='PERDIDA_ZONA' and x['bar_offset']==3 for x in ev))
        self.assertTrue(any(x['event']=='RECUPERACION_CIERRE' and x['bar_offset']==4 for x in ev))
        self.assertEqual(result['summary']['descriptive_defenses'],1)

    def test_no_defense_without_formation_atr(self):
        path=candles([(10.9,11.8,10.8,11.4),(11.4,11.7,10.9,11.1),
                      (11.1,12,11.05,11.8),(11.8,12.1,10.9,11.9)])
        result=price_behavior(path,pre_closed_ts=PRE,breakout_closed_ts=PRE+TF,
            zone_low=10,zone_high=11,horizon=4)
        self.assertEqual(result['summary']['descriptive_defenses'],0)

    def test_gap_censored_does_not_bridge_missing_bars(self):
        path=candles([(10.9,11.8,10.8,11.4),(11.4,11.7,10.9,11.1),
                      (11.1,12,11.05,11.8),(11.8,12.1,10.9,11.9)])
        del path[PRE+2*TF]
        result=price_behavior(path,pre_closed_ts=PRE,breakout_closed_ts=PRE+TF,
            zone_low=10,zone_high=11,formation_atr=1,horizon=4)
        self.assertFalse(result['summary']['complete_window'])
        self.assertEqual(result['summary']['first_gap_offset'],2)
        self.assertEqual(result['summary']['descriptive_defenses'],0)

    def test_zone_ambiguity_and_duplicate_ts_fail_closed(self):
        path=candles([(10.9,11.8,10.8,11.4)]*4)
        observed=price_behavior(path,pre_closed_ts=PRE,
           breakout_closed_ts=PRE+TF,zone_low=11.5,zone_high=11.6,horizon=4)
        self.assertFalse(observed['summary']['breakout_close_above_zone_high'])
        path[PRE]['close_ts']+=1
        with self.assertRaises(ValueError):bar_map(path)

    def test_continuation_trace_stops_on_zone_loss(self):
        path=candles([(10.9,11.8,10.8,11.4), (11.4,11.7,10.9,11.1),
                      (11.1,11.4,9.4,9.6)] + [(9.6,9.8,9.2,9.6)]*12)
        gates=continuation_gates(path,breakout_closed_ts=PRE+TF,
            zone_low=10,zone_high=11,atr_pre=1)
        self.assertEqual(gates[-1]['state'],'LOST_ZONE_BEFORE_CONTINUATION')
        self.assertEqual(len(gates),2)

    def test_gate_trace_reports_exact_conditions_without_post_hoc_relax(self):
        path=candles([(10.9,11.8,10.8,11.4)] + [(11.4,11.7,11.1,11.3)]*13)
        gates=continuation_gates(path,breakout_closed_ts=PRE+TF,
            zone_low=10,zone_high=11,atr_pre=1)
        self.assertEqual(len(gates),12)
        self.assertFalse(any(g['state']=='CONFIRMED' for g in gates))
        self.assertEqual(gates[0]['state'],'NOT_CONFIRMED_THIS_BAR')

    def test_replay_original_v26_v29_three_routes_match(self):
        path=candles([(11.05,11.2,11.01,11.10)]*96)
        frozen=[dict(route=route,mechanism_status=status,
                     entry_status='NOT_COVERED',confirmed_closed_ts='',entry_ts='')
                for route,status in [
                    ('FLIP_1M','DIRECT_NO_RETEST_24B'),
                    ('V29_CONTINUATION_30M','NO_EARLY_CONTINUATION'),
                    ('V29_RECLAIM_30M','NO_ORIGINAL_FAILURE')]]
        replay=replay_frozen(path,breakout_closed_ts=PRE+TF,zone_low=10,zone_high=11,
            formation_atr=1,atr_pre=1,frozen=frozen)
        self.assertTrue(replay['all_match'],replay['rows'])
        self.assertEqual(len(replay['rows']),3)

    def test_replay_mismatch_explicit(self):
        path=candles([(11.05,11.2,11.01,11.10)]*96)
        frozen=[dict(route=route,mechanism_status=status,
                     entry_status='NO_SIGNAL',confirmed_closed_ts='',entry_ts='')
                for route,status in [
                    ('FLIP_1M','FLIP_SUSTAINED_4B'),
                    ('V29_CONTINUATION_30M','NO_EARLY_CONTINUATION'),
                    ('V29_RECLAIM_30M','NO_ORIGINAL_FAILURE')]]
        replay=replay_frozen(path,breakout_closed_ts=PRE+TF,zone_low=10,zone_high=11,
            formation_atr=1,atr_pre=1,frozen=frozen)
        self.assertFalse(replay['all_match'])
        self.assertFalse(replay['rows'][0]['replay_equal'])

    def test_formation_atr_reads_exact_level_and_rejects_wrong_zone(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'levels_features.csv'
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['symbol','level_id','zone_low','zone_high','atr_at_formation'])
                w.writeheader();w.writerow(dict(symbol='WAXPUSDT',level_id='LEVEL',zone_low=10,zone_high=11,atr_at_formation=1.5))
            self.assertEqual(level_formation_atr(p,'WAXPUSDT','LEVEL',10,11),1.5)
            with self.assertRaises(ValueError):level_formation_atr(p,'WAXPUSDT','LEVEL',10,12)

    def test_markers_optional_respect_visible_time(self):
        import plotly.graph_objects as go
        rows=[dict(event='CONTACTO_DESDE_ARRIBA',known_closed_ts=PRE+TF,price=11.1),
              dict(event='PERDIDA_ZONA',known_closed_ts=PRE+10*TF,price=9.5)]
        fig=add_behavior_markers(go.Figure(),rows,PRE,4,visible_types=['CONTACTO_DESDE_ARRIBA'])
        self.assertEqual(len(fig.data),1)
        self.assertEqual(fig.data[0].name,'Contacto desde arriba (candidato)')
        fig2=add_behavior_markers(go.Figure(),rows,PRE,4,visible_types=['PERDIDA_ZONA'])
        self.assertEqual(len(fig2.data),0)
        fig3=add_behavior_markers(go.Figure(),rows,PRE,4,visible_types=[])
        self.assertEqual(len(fig3.data),0)

    def test_on_demand_prepare_for_exact_selected_attempt_without_writes(self):
        from dashboard.views.hll_resistance_universal_audit_view import prepare, LEVELS
        path=candles([(11.05,11.2,11.01,11.10)]*96)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            levels=root/LEVELS
            levels.parent.mkdir(parents=True)
            with levels.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['symbol','level_id','zone_low','zone_high','atr_at_formation'])
                w.writeheader();w.writerow(dict(symbol='WAXPUSDT',level_id='level1',zone_low=10,zone_high=11,atr_at_formation=1))
            selected=dict(attempt_id='some-episode-attempt-nine',symbol='WAXPUSDT',level_id='level1',
                          pre_closed_ts=PRE,breakout_closed_ts=PRE+TF,zone_low=10,zone_high=11,atr_pre=1)
            frozen=[dict(route=route,mechanism_status=status,
                     entry_status='NOT_COVERED',confirmed_closed_ts='',entry_ts='')
                for route,status in [
                    ('FLIP_1M','DIRECT_NO_RETEST_24B'),
                    ('V29_CONTINUATION_30M','NO_EARLY_CONTINUATION'),
                    ('V29_RECLAIM_30M','NO_ORIGINAL_FAILURE')]]
            out=prepare(root,selected,path,frozen,visible_post_bars=96)
            self.assertEqual(out['attempt_id'],'some-episode-attempt-nine')
            self.assertTrue(out['detector']['all_match'])
            self.assertEqual([p.name for p in root.rglob('*') if p.is_file()],['levels_features.csv'])

    def test_source_view_does_not_change_ranker_or_frozen_detectors(self):
        p=Path('dashboard/views/hll_structural_discovery_view.py').read_text()
        self.assertIn('resistance_ui.prepare',p)
        self.assertIn('if universal_on and show_post',p)
        self.assertIn("st.plotly_chart(fig,use_container_width=True",p)
        self.assertNotIn('similarity.rank(',p)


if __name__=='__main__':unittest.main()
