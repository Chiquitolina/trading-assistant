"""Causal-stage replay tests: real rule branches, clocks, lineage and immutability."""
from __future__ import annotations
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import btc_multiswing_structural_behavior_v13 as v


def fixture(prices, tf=15, initial=95, touch=0):
    close = np.repeat(prices, tf).astype(float)
    op = np.r_[initial, close[:-1]]
    n = len(prices)
    minute = {'open_ts': np.arange(len(close), dtype='int64') * v.M,
              'closed_ts': (np.arange(len(close), dtype='int64') + 1) * v.M,
              'open': op, 'close': close, 'high': np.maximum(op, close), 'low': np.minimum(op, close)}
    major = {'open_ts': np.arange(n, dtype='int64') * tf * v.M,
             'closed_ts': (np.arange(n, dtype='int64') + 1) * tf * v.M,
             'open': op[::tf], 'close': close[tf-1::tf],
             'high': minute['high'].reshape(n, tf).max(axis=1),
             'low': minute['low'].reshape(n, tf).min(axis=1)}
    ep = dict(episode_id='ep', region_id='r', pre_cutoff_ts=touch,
              stage_low=100, stage_high=110, stage_known_from=0, stage_extrema=2,
              pre_close_1m=initial, approach_position='BELOW' if initial < 100 else 'ABOVE',
              band_origin_side='HIGH')
    return minute, major, ep


def stages(*extra):
    return v.StageIndex(pd.DataFrame([
        dict(region_id='r', known_from=0, known_until=1, band_low=100,
             band_high=110, independent_extrema=2), *extra]))


def replay(minute, major, ep, s, tf=15, **kwargs):
    return v.classify(major, episode=ep, source_end=int(minute['closed_ts'][-1]),
                      tf_minutes=tf, stages=s, path=v.MinutePath(minute, s), **kwargs)


def old_replay(major, ep, tf, end):
    kw = dict(episode_id=ep['episode_id'], region_id=ep['region_id'], origin_side=ep['band_origin_side'],
              approach=ep['approach_position'], band_low=ep['stage_low'], band_high=ep['stage_high'],
              touch_open=ep['pre_cutoff_ts'], source_end=end, tf_minutes=tf)
    if tf == 15: return v.p11.classify_transitions(major, **kw)
    q = int(np.searchsorted(major['closed_ts'], ep['pre_cutoff_ts'], side='right')) - 1
    prior = v.p11.price_position(float(major['close'][q]), 100, 110) if q >= 0 else None
    return v.p12.classify_transitions_v12(major, previous_closed_position=prior, **kw)


class ReplayRules(unittest.TestCase):
    def test_constant_geometry_matches_parent_including_failed_break_and_retest(self):
        for tf in (15, 60):
            with self.subTest(tf=tf):
                minute, major, ep = fixture([95,112,113,114,109,112,113,95,94], tf)
                a = old_replay(major, ep, tf, int(minute['closed_ts'][-1]))
                b, _, health = replay(minute, major, ep, stages(), tf)
                akeys = [(x['transition_id'], x['direction'], v.clean(x['parent_transition_id'])) for x in a]
                bkeys = [(x['comparison_key'], x['direction'], x['parent_comparison_key']) for x in b]
                self.assertEqual(akeys, bkeys)
                self.assertEqual(health['status'], 'COMPLETE_CLOSED_DATA')
                self.assertTrue(any(x['event_type'] == 'RETEST_TOUCH' for x in b))
                self.assertTrue(any(x['event_type'] == 'FAILED_BREAK_RETURN_ORIGIN_2C' for x in b))

    def test_expansion_changes_second_close_and_defers_breakout(self):
        minute, major, ep = fixture([112,113,116,117])
        s = stages(dict(region_id='r', known_from=30*v.M, known_until=31*v.M,
                        band_low=100, band_high=115, independent_extrema=3))
        b, d, _ = replay(minute, major, ep, s)
        breaks = [x['confirmation_ts'] for x in b if x['event_type'] == 'BREAK_ABOVE_2C']
        self.assertEqual(breaks, [60*v.M])
        self.assertEqual(d[0]['current_position'], 'ABOVE')
        self.assertEqual(d[1]['current_position'], 'INSIDE')
        self.assertEqual(d[1]['runs_above'], 0)

    def test_future_stage_and_future_candles_do_not_change_prefix(self):
        a_min, a_major, ep = fixture([112,113,116,117])
        b_min, b_major, _ = fixture([112,113,116,117,50,500,50])
        s = stages()
        future = stages(dict(region_id='r', known_from=75*v.M, known_until=100*v.M,
                             band_low=50, band_high=500, independent_extrema=3))
        a, d1, _ = replay(a_min, a_major, ep, s)
        b, d2, _ = replay(b_min, b_major, ep, future)
        self.assertEqual(a, [r for r in b if r['confirmation_ts'] <= 60*v.M])
        self.assertEqual(d1, [r for r in d2 if r['confirmation_ts'] <= 60*v.M])

    def test_minute_contact_uses_stage_at_minute_open(self):
        minute, _, _ = fixture([112])
        s = stages(dict(region_id='r', known_from=10*v.M, known_until=11*v.M,
                        band_low=100, band_high=115, independent_extrema=3))
        path = v.MinutePath(minute, s)
        # Suppress the initial price jump through the old band for this range test.
        minute['low'][:] = 112; minute['high'][:] = 112
        e = path.evidence('r', 0, 15*v.M)
        self.assertTrue(e['touch'])
        self.assertEqual(e['first_touch_known_ts'], 11*v.M)

    def test_stage_confirmed_at_tf_close_cannot_rewrite_prior_wicks(self):
        minute, _, _ = fixture([112])
        minute['low'][:] = 112; minute['high'][:] = 112
        s = stages(dict(region_id='r', known_from=15*v.M, known_until=16*v.M,
                        band_low=100, band_high=115, independent_extrema=3))
        e = v.MinutePath(minute, s).evidence('r', 0, 15*v.M)
        self.assertTrue(e['known']); self.assertFalse(e['touch'])
        self.assertEqual(v.p11.price_position(112, s.at('r',15*v.M)['band_low'], s.at('r',15*v.M)['band_high']), 'INSIDE')

    def test_acceptance_preserves_empty_direction(self):
        minute, major, ep = fixture([105,106,107])
        b, _, _ = replay(minute, major, ep, stages())
        accepted = [r for r in b if r['event_type'] == 'ACCEPT_INSIDE_3C']
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['direction'], '')
        observations, physical = v.canonicalize_asof(pd.DataFrame(b))
        self.assertEqual(physical.loc[physical.event_type.eq('ACCEPT_INSIDE_3C'),'direction'].iloc[0], '')

    def test_last_position_hourly_seed_uses_last_closed_hour(self):
        minute, major, ep = fixture([112,113,114], 60)
        ep['pre_cutoff_ts'] = 65*v.M; ep['pre_close_1m'] = 95
        b, _, _ = replay(minute, major, ep, stages(), 60)
        # At 65m the last CLOSED H1 price is ABOVE; no spurious first CLOSE_ABOVE.
        self.assertFalse(any(r['event_type'] == 'CLOSE_ABOVE' and r['confirmation_ts'] == 120*v.M for r in b))

    def test_display_expiry_is_not_loss_of_known_geometry(self):
        self.assertIsNotNone(stages().at('r', 100000*v.M))

    def test_nonfinite_stage_cannot_silently_become_inside(self):
        with self.assertRaisesRegex(ValueError,'INVALID_STAGE'):
            stages(dict(region_id='r',known_from=15*v.M,known_until=16*v.M,
                        band_low=np.nan,band_high=115,independent_extrema=3))

    def test_truncated_episode_is_censored_even_when_observed_data_are_complete(self):
        minute,major,ep=fixture([112,113,114])
        _,_,health=replay(minute,major,ep,stages())
        self.assertEqual(health['status'],'COMPLETE_CLOSED_DATA')
        self.assertTrue(health['right_censored_by_source'])
        self.assertEqual(health['observed_until_ts'],45*v.M)

    def test_gapped_minute_source_fails_closed(self):
        minute, _, _ = fixture([112])
        minute['open_ts'][5] += v.M
        with self.assertRaisesRegex(ValueError, 'GAPPED_MINUTE_SOURCE'):
            v.MinutePath(minute, stages())

    def test_unknown_stage_marks_decision_incomplete(self):
        minute, major, ep = fixture([112,113])
        s = stages()
        class MissingAfterOrigin:
            def at(self, region, timestamp): return s.at(region,timestamp) if timestamp == 0 else None
        b, d, health = replay(minute, major, ep, MissingAfterOrigin())
        self.assertEqual(b, [])
        self.assertTrue(all(not r['data_complete'] for r in d))
        self.assertEqual(health['status'], 'INDETERMINATE_PATH_OR_STAGE')

    def test_closed_pre_context_excludes_future_h1_and_h4(self):
        minute, major, ep = fixture([112,113,114,115])
        b, _, _ = replay(minute, major, ep, stages())
        events = pd.DataFrame(b)
        future = {k: arr.copy() for k,arr in major.items()}
        future['closed_ts'] = major['closed_ts'] + 240*v.M
        pre = v.build_pre(events, minute, {15:major,60:future,240:future}, stages(), pd.DataFrame([ep]))
        self.assertTrue(pre.pre_last_1h_close_ts.isna().all())
        self.assertTrue(pre.pre_last_4h_close_ts.isna().all())
        self.assertTrue((pre.pre_stage_known_from <= pre.pre_cutoff_ts).all())


def old_row(tid='ep|M15|CLOSE_ABOVE|900000', kind='CLOSE_ABOVE', direction='ABOVE', ep='ep', parent=''):
    return dict(transition_id=tid, physical_transition_id='physical1', episode_id=ep, region_id='r',
                source_tf='M15_CLOSED', event_type=kind, direction=direction, confirmation_ts=900000,
                confirmation_utc='test', parent_transition_id=parent, band_low_pre=100, band_high_pre=110)


def new_row(key, kind, direction, ep='ep', parent=''):
    return dict(asof_transition_id='new_'+ep, comparison_key=key, asof_physical_transition_id='physicalB',
                episode_id=ep, region_id='r', source_tf='M15_CLOSED', event_type=kind, direction=direction,
                confirmation_ts=900000, parent_comparison_key=parent)


def decision(ep='ep', complete=True):
    return dict(episode_id=ep, source_tf='M15_CLOSED', confirmation_ts=900000, current_position='INSIDE',
                data_complete=complete, reason='TEST_COMPLETE' if complete else 'NO_STAGE_ASOF',
                band_low_asof=100, band_high_asof=115, stage_known_from=0)


class ComparisonRules(unittest.TestCase):
    def test_replacement_needs_unique_same_family_same_close(self):
        a = old_row()
        b = new_row('ep|M15|CLOSE_INSIDE|900000', 'CLOSE_INSIDE', 'INSIDE')
        c, added = v.compare_observations(pd.DataFrame([a]),pd.DataFrame([b]),pd.DataFrame([decision()]))
        self.assertEqual(c.comparison_status.iloc[0], 'CHANGED_LABEL')
        self.assertEqual(len(added),0)

    def test_unrelated_family_is_removal_and_new_event(self):
        a = old_row()
        b = new_row('ep|M15|ACCEPT_INSIDE_3C|900000', 'ACCEPT_INSIDE_3C', '')
        c, added = v.compare_observations(pd.DataFrame([a]),pd.DataFrame([b]),pd.DataFrame([decision()]))
        self.assertEqual(c.comparison_status.iloc[0], 'REMOVED_UNDER_ASOF')
        self.assertEqual(len(added),1)

    def test_same_label_with_different_parent_is_not_unchanged_sequence(self):
        a = old_row(kind='RETEST_TOUCH', parent='old_break')
        b = new_row(a['transition_id'], 'RETEST_TOUCH','ABOVE',parent='later_break')
        c, _ = v.compare_observations(pd.DataFrame([a]),pd.DataFrame([b]),pd.DataFrame([decision()]))
        self.assertEqual(c.comparison_status.iloc[0], 'SAME_LABEL_DIFFERENT_LINEAGE')

    def test_unknown_evidence_is_not_counted_as_removed(self):
        a = old_row()
        b = new_row('other', 'ACCEPT_INSIDE_3C', '')
        c, _ = v.compare_observations(pd.DataFrame([a]),pd.DataFrame([b]),pd.DataFrame([decision(complete=False)]))
        self.assertEqual(c.comparison_status.iloc[0], 'INDETERMINATE')

    def test_context_disagreement_keeps_one_parent_physical_id(self):
        old = pd.DataFrame([dict(physical_transition_id='p')])
        observations = pd.DataFrame([
            dict(physical_transition_id='p', comparison_status='SAME_LABEL_AND_LINEAGE',asof_physical_transition_id='b',asof_event_type='CLOSE_ABOVE',asof_direction='ABOVE'),
            dict(physical_transition_id='p', comparison_status='REMOVED_UNDER_ASOF',asof_physical_transition_id='',asof_event_type='',asof_direction='')])
        result = v.physical_comparison(old, observations)
        self.assertEqual(len(result),1)
        self.assertEqual(result.physical_comparison_status.iloc[0], 'CONTEXT_DEPENDENT_DIVERGENCE')

    def linked_row(self, tf, timestamp, ident, high=110):
        return dict(asof_transition_id=ident, asof_physical_transition_id='physical_'+ident,
                    episode_id='ep', region_id='r', source_tf=tf, event_type='BREAK_BELOW_2C',
                    direction='BELOW', origin_position='ABOVE', confirmation_ts=timestamp,
                    parent_asof_transition_id='', stage_known_from=0,
                    band_low_asof=100, band_high_asof=high)

    def test_cross_timeframe_link_keeps_distinct_geometries_and_lag(self):
        m = self.linked_row('M15_CLOSED',45*v.M,'m')
        h = self.linked_row('H1_CLOSED',120*v.M,'h',high=111)
        link = v.link_timeframes(pd.DataFrame([m,h])).iloc[0]
        self.assertEqual(link.link_status,'LAGGED_CONFIRMATION')
        self.assertEqual(link.lag_minutes,75)
        self.assertTrue(link.geometry_changed_between_confirmations)

    def test_cross_timeframe_link_does_not_use_a_future_m15(self):
        m = self.linked_row('M15_CLOSED',75*v.M,'m')
        h = self.linked_row('H1_CLOSED',60*v.M,'h')
        link = v.link_timeframes(pd.DataFrame([m,h])).iloc[0]
        self.assertEqual(link.link_status,'NO_MATCH')

    def test_same_close_is_not_later_confirmation(self):
        m = self.linked_row('M15_CLOSED',60*v.M,'m')
        h = self.linked_row('H1_CLOSED',60*v.M,'h')
        link = v.link_timeframes(pd.DataFrame([m,h])).iloc[0]
        self.assertEqual(link.link_status,'SAME_CLOSE')
        self.assertEqual(link.lag_minutes,0)


class PersistenceAndParity(unittest.TestCase):
    def test_parity_rejects_direction_or_parent_drift(self):
        minute, major, ep = fixture([112,113,114])
        rows = old_replay(major,ep,15,int(minute['closed_ts'][-1]))
        original = pd.DataFrame(rows)
        drift = original.copy();drift.loc[0,'direction'] = 'BELOW'
        with self.assertRaisesRegex(ValueError,'BASELINE_DATA_NOT_REPRODUCED'):
            v.assert_baseline_parity(original,drift)

    def test_plan_does_not_require_inputs_or_write_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp)/'missing'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(v.main(['--plan','--project-root',str(missing)]),0)
            self.assertFalse(missing.exists())

    def test_hash_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);f=folder/'x.csv';f.write_text('original')
            (folder/'manifest.json').write_text(json.dumps(dict(version='test',immutable_run=True,
                no_live_imports_or_network=True,files_sha256={'x.csv':v.digest(f)})))
            f.write_text('modified')
            with self.assertRaisesRegex(ValueError,'PARENT_HASH_MISMATCH'):
                v.checked_parent(folder,'test')

    def test_existing_or_input_nested_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=root/'parent';parent.mkdir()
            with self.assertRaisesRegex(ValueError,'OUTPUT_INSIDE_IMMUTABLE_INPUT'):
                v.write_immutable(parent/'new',{}, {},root,[parent])
            target=root/'output';target.mkdir();(target/'keep').write_text('original')
            with self.assertRaises(FileExistsError):v.write_immutable(target,{}, {},root,[parent])
            self.assertEqual((target/'keep').read_text(),'original')


if __name__ == '__main__': unittest.main()
