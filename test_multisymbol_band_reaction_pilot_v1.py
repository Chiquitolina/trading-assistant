"""Causal clocks, symmetric responses, source integrity and symbol isolation."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

import multisymbol_band_reaction_pilot_v1 as p


def minute_fixture(hours=8):
    price = np.full(hours*60, 104.)
    price[60:120] = 103.
    price[60] = 101.
    price[180:240] = 101.
    price[240:300] = 99.
    frame = pd.DataFrame(dict(timestamp=np.arange(len(price))*p.M,
                             open=price, high=price+.05, low=price-.05, close=price))
    stage = pd.DataFrame([dict(region_id='b1', known_from=0, band_low=100., band_high=102.,
                                independent_extrema=2, side='HIGH', symbol='FOOUSDT')])
    return frame, stage


class ResponseClocks(unittest.TestCase):
    def setUp(self):
        self.frame, self.stage = minute_fixture()
        self.config = replace(p.Config(), symbols=('FOOUSDT',), followup_hours=5,
                              horizons_minutes=(30,60,180,720))

    def measure(self, frame=None, stage=None, end=None):
        f = self.frame if frame is None else frame
        end = len(f)*p.M if end is None else end
        return p.measure_tests(f, self.stage if stage is None else stage,
                               'FOOUSDT', p.H, end, self.config)

    def test_response_only_at_closed_hour_and_hold_next_entire_hour(self):
        tests, events, refs, outcomes, _ = self.measure()
        first = tests.iloc[0]
        selected = events[events.test_id == first.test_id]
        initial = selected[selected.event == 'DEFENSE_ABOVE_H1'].iloc[0]
        hold = selected[selected.event == 'H1_HOLD_CONFIRMED'].iloc[0]
        self.assertEqual(first.available_ts, p.H+p.M)
        self.assertEqual(initial.available_ts, 2*p.H)
        self.assertEqual(hold.available_ts, 3*p.H)
        self.assertEqual(refs[(refs.test_id==first.test_id)&(refs.signal=='RESPONSE_H1')].iloc[0].available_ts,
                         2*p.H+p.M)
        self.assertFalse((p.visible_rows(events,2*p.H-1).event=='DEFENSE_ABOVE_H1').any())
        self.assertTrue(p.visible_rows(refs,2*p.H).empty)
        self.assertTrue(p.visible_rows(outcomes,2*p.H+p.M).empty)

    def test_short_is_symmetric_and_band_origin_does_not_force_direction(self):
        frame = self.frame.copy()
        for col in ['open','close']:
            frame[col] = 202.-self.frame[col]
        frame['high'] = 202.-self.frame.low
        frame['low'] = 202.-self.frame.high
        _, ev, _, _, _ = self.measure(frame)
        row = ev[ev.event == 'REJECTION_BELOW_H1'].iloc[0]
        self.assertEqual(row.direction, 'SHORT')
        self.assertEqual(row.available_ts, 2*p.H)

    def test_inside_and_conflicting_approach_are_not_directional_rejections(self):
        self.assertEqual(p.initial_state('INSIDE','ABOVE'),('EXIT_ABOVE_H1','NA'))
        self.assertEqual(p.initial_state('ABOVE','INSIDE'),('ACCEPT_INSIDE_H1','NA'))
        self.assertEqual(p.initial_state('CONFLICT','BELOW'),('APPROACH_CONFLICT','NA'))
        self.assertEqual(p.initial_state('BELOW','ABOVE'),('BREAK_THROUGH_H1','NA'))

    def test_geometry_changed_during_hour_is_not_response(self):
        stage = pd.concat([self.stage, self.stage.assign(known_from=p.H+30*p.M,band_low=99.)])
        tests, ev, _, _, _ = self.measure(stage=stage)
        first = ev[ev.test_id == tests.iloc[0].test_id]
        self.assertIn('GEOMETRY_CHANGED', list(first.event))
        self.assertNotIn('DEFENSE_ABOVE_H1', list(first.event))

    def test_future_stage_does_not_change_past_events_or_ids(self):
        base_tests, base_events, _, _, _ = self.measure()
        stage = pd.concat([self.stage, self.stage.assign(known_from=6*p.H,band_low=98.)])
        tests, events, _, _, _ = self.measure(stage=stage)
        pd.testing.assert_frame_equal(p.visible_rows(base_tests,5*p.H).reset_index(drop=True),
                                       p.visible_rows(tests,5*p.H).reset_index(drop=True))
        pd.testing.assert_frame_equal(p.visible_rows(base_events,5*p.H).reset_index(drop=True),
                                       p.visible_rows(events,5*p.H).reset_index(drop=True))

    def test_truncated_data_never_publishes_unclosed_confirmation(self):
        _, events, refs, _, _ = self.measure(self.frame.iloc[:119], end=119*p.M)
        self.assertNotIn('DEFENSE_ABOVE_H1', list(events.event))
        self.assertTrue(refs.empty)

    def test_no_next_hour_is_censored_not_failed(self):
        _, events, _, outcomes, _ = self.measure(self.frame.iloc[:120], end=120*p.M)
        self.assertIn('H1_HOLD_CENSORED', list(events.event))
        self.assertNotIn('H1_HOLD_FAILED', list(events.event))
        self.assertTrue(outcomes.empty)  # no strictly later reference minute

    def test_reentry_and_later_failure_are_retained_after_initial_hold(self):
        tests, events, _, _, _ = self.measure()
        ev = events[events.test_id == tests.iloc[0].test_id]
        self.assertIn('FIRST_H1_REENTRY_INSIDE', list(ev.event))
        self.assertIn('FIRST_H1_OPPOSITE_CLOSE', list(ev.event))
        self.assertIn('H1_HOLD_CONFIRMED', list(ev.event))

    def test_censored_paths_do_not_receive_partial_mfe_or_markout(self):
        _, _, _, outcomes, _ = self.measure()
        censored = outcomes[outcomes.state=='CENSORED']
        self.assertGreater(len(censored),0)
        self.assertTrue(censored[['markout_pct','mfe_pct','mae_pct']].isna().all().all())
        complete = outcomes[outcomes.state=='COMPLETE']
        self.assertTrue((complete[['mfe_pct','mae_pct']]>=0).all().all())

    def test_four_hour_context_is_closed_not_current_candle(self):
        _, events, _, _, _ = self.measure()
        self.assertTrue(events[(events.event=='CONTEXT_LAST_CLOSED_4H') &
                               (events.available_ts < 4*p.H)].empty)
        context=events[events.event=='CONTEXT_LAST_CLOSED_4H']
        self.assertTrue((context.evidence_closed_ts<=context.available_ts).all())
        self.assertTrue((context.evidence_closed_ts%(4*p.H)==0).all())


class SourceAndGeometry(unittest.TestCase):
    def test_gaps_duplicates_unsorted_and_bad_ohlc_rejected(self):
        frame, _ = minute_fixture()
        cases = [frame.drop(index=10), pd.concat([frame,frame.iloc[-1:]]),frame.iloc[::-1]]
        bad = frame.copy();bad.loc[10,'high']=1.;cases.append(bad)
        bad = frame.astype({'timestamp':float});bad.loc[10,'timestamp']+=.5;cases.append(bad)
        for case in cases:
            with self.subTest(), self.assertRaises(ValueError):
                p.validate_minute(case)

    def test_closed_4h_bars_never_include_partial_tail(self):
        frame, _ = minute_fixture()
        bars = p.closed_bars(frame,240,end=7*p.H)
        self.assertEqual(list(bars.available_ts),[4*p.H])
        self.assertTrue(p.closed_bars(frame,240,end=4*p.H-1).empty)

    def test_preflight_uses_actual_common_interval_not_requested_60d(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for symbol,hours in [('AAUSDT',100),('BBUSDT',80)]:
                n=hours*60
                pd.DataFrame(dict(timestamp=np.arange(n)*p.M,open=1.,high=1.,low=1.,close=1.)).to_csv(
                    folder/f'{symbol}_1m.csv',index=False)
            cfg=replace(p.Config(),symbols=('AAUSDT','BBUSDT'),start=48*p.H,end=100*p.H)
            plan,_,_=p.preflight(folder,cfg,folder)
            self.assertTrue(plan['ready'])
            self.assertEqual(plan['start_ts'],48*p.H)
            self.assertEqual(plan['end_ts'],80*p.H)
            self.assertFalse(plan['full_requested_interval'])
            (folder/'BBUSDT_1m.csv').unlink()
            plan,_,_=p.preflight(folder,cfg,folder)
            self.assertFalse(plan['ready'])
            self.assertEqual(plan['sources'][1]['status'],'MISSING_SOURCE')

    def test_same_prices_in_different_symbols_have_disjoint_physical_and_band_ids(self):
        n=72*60
        t=np.arange(n)
        price=100+np.sin(t/23.)+np.sin(t/79.)*.5
        frame=pd.DataFrame(dict(timestamp=t*p.M,open=price,high=price+.02,
                                low=price-.02,close=price))
        cfg=replace(p.Config(),tolerance_pct=.20, band_timeframes=('1m','5m','15m','30m','1h','4h'))
        ca,ra,sa,_=p.build_geometry(frame,'AAUSDT',48*p.H,72*p.H,cfg)
        cb,rb,sb,_=p.build_geometry(frame,'BBUSDT',48*p.H,72*p.H,cfg)
        self.assertGreater(len(ra),0)
        self.assertEqual(len(ra),len(rb))
        self.assertTrue(set(ca.pivot_id).isdisjoint(cb.pivot_id))
        self.assertTrue(set(ra.region_id).isdisjoint(rb.region_id))
        self.assertTrue(set(sa.region_id)==set(ra.region_id))
        self.assertTrue(set(sb.region_id)==set(rb.region_id))

    def test_two_symbol_pipeline_writes_complete_run_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            code_root=Path(p.__file__).parent
            for rel in p.code_hashes(code_root):
                (root/rel).parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(code_root/rel,root/rel)
            folder=root/'minutes';folder.mkdir()
            n=72*60;t=np.arange(n)
            price=100+np.sin(t/23.)+np.sin(t/79.)*.5
            for symbol,scale in [('AAUSDT',1.),('BBUSDT',.5)]:
                pd.DataFrame(dict(timestamp=t*p.M,open=price*scale,high=(price+.02)*scale,
                    low=(price-.02)*scale,close=price*scale)).to_csv(folder/f'{symbol}_1m.csv',index=False)
            cfg=replace(p.Config(),symbols=('AAUSDT','BBUSDT'),start=48*p.H,end=72*p.H,
                        band_timeframes=('1m','5m','15m','30m','1h','4h'))
            dest,summary,manifest=p.run_pilot(root,cfg,folder,'fixture_multi')
            self.assertEqual(set(summary.symbol),{'AAUSDT','BBUSDT'})
            self.assertTrue((summary.bands>0).all())
            self.assertEqual(len(manifest['files_sha256']),17)
            for rel,sha in manifest['files_sha256'].items():
                self.assertEqual(p.digest(dest/rel),sha)
            with self.assertRaises(FileExistsError):
                p.run_pilot(root,cfg,folder,'fixture_multi')


if __name__ == '__main__':
    unittest.main()
