#!/usr/bin/env python3
"""Offline causal join tests. Does not access DB, network, Redis or future outcomes."""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import universal_runner_hll_lifecycle_join_v1 as m

BASE=1_800_000
RET=500*BASE+60_000


def touch(price=100., grp='NO_PERSISTENT_LEVEL_NEAR_PRE', ret=RET):
    label=dict(touch_key='K1',symbol='BTCUSDT',side='LONG',status='REACTION',
      retest_ts=str(ret),known_ts=str(ret+60_000),group_pre=grp,
      chosen_zone_low='99.5' if grp=='PERSISTENT_LEVEL_NEAR_PRE' else '',
      chosen_zone_high='100.5' if grp=='PERSISTENT_LEVEL_NEAR_PRE' else '',
      structural_level_id='persist-01' if grp=='PERSISTENT_LEVEL_NEAR_PRE' else '')
    attrs={'K1':dict(side='LONG',status='REACTION',retest_ts=ret,
                     known_ts=ret+60_000,swing_price=price)}
    return label,attrs


def level(formed=RET-30*BASE, center=100., lo=99.8,hi=100.2, atr=1.0):
    return dict(level_id='HLL01',symbol='BTCUSDT',origin='LOW',
        formation_closed_ts=formed,center=center,zone_low=lo,zone_high=hi,
        atr_at_formation=atr,quality_flag='STANDARD_WIDTH')


def evt(ts,kind,id='e1'):
    return dict(level_id='HLL01',event_id=id,observed_closed_ts=ts,event_type=kind)


def ended(state='ACTIVE_AT_END',ts=None):
    return dict(level_id='HLL01',end_state=state,end_closed_ts=ts)


def joined(label=None,attrs=None,lvs=None,evs=None,ends=None,**kw):
    if label is None:label,attrs=touch()
    else:
        if attrs is None:attrs=touch()[1]
    return m.join_symbol('BTCUSDT',[label],attrs,
       [level()] if lvs is None else lvs,
       [] if evs is None else evs,
       [ended()] if ends is None else ends,**kw)


class Join(unittest.TestCase):
    def test_same_swing_price_matches_hll_level(self):
        rows,candidates=joined()
        self.assertEqual(rows[0]['hll_near_05atr_count'],1)
        self.assertEqual(rows[0]['hll_persistent_group'],'HLL_ONLY')
        self.assertEqual(candidates[0]['level_id'],'HLL01')
        self.assertEqual(rows[0]['hll_nearest_in_zone'],1)

    def test_both_detectors_near_remain_separate_ids(self):
        label,attrs=touch(grp='PERSISTENT_LEVEL_NEAR_PRE')
        row,candidate=joined(label=label,attrs=attrs)
        self.assertEqual(row[0]['hll_persistent_group'],'BOTH_NEAR_OVERLAPPING_BANDS')
        self.assertEqual(row[0]['persistent_level_id'],'persist-01')
        self.assertEqual(row[0]['hll_nearest_level_id'],'HLL01')
        self.assertEqual(candidate[0]['persistent_band_overlap'],1)

    def test_both_detectors_near_but_different_bands_not_same_line(self):
        label,attrs=touch(grp='PERSISTENT_LEVEL_NEAR_PRE')
        # HLL band touches swing price at 100.0, but does not overlap
        # frozen persistent band [99.5,100.5].
        alternate=level(center=100.8,lo=100.6,hi=100.9,atr=2.0)
        rows,_=joined(label=label,attrs=attrs,lvs=[alternate])
        self.assertEqual(rows[0]['hll_persistent_group'],
                         'BOTH_NEAR_DIFFERENT_BANDS')

    def test_no_hll_with_full_causal_history_is_not_near(self):
        rows,_=joined(lvs=[],ends=[])
        self.assertEqual(rows[0]['hll_persistent_group'],'NEITHER_NEAR')
        self.assertEqual(rows[0]['hll_level_count_asof'],0)

    def test_unconfirmed_future_level_cannot_leak(self):
        rows,_=joined(lvs=[level(formed=RET)],ends=[ended()])
        self.assertEqual(rows[0]['hll_level_count_asof'],0)

    def test_future_defense_not_known_and_prior_defense_known(self):
        rows,can=joined(evs=[evt(RET-2*BASE,'TEST_FROM_ABOVE','a'),
                             evt(RET-BASE,'DEFENSE_CONFIRMED','b'),
                             evt(RET,'LOSS_CONFIRMED','c')])
        self.assertEqual(rows[0]['hll_nearest_prior_tests'],1)
        self.assertEqual(rows[0]['hll_nearest_prior_defenses'],1)
        self.assertEqual(rows[0]['hll_nearest_prior_losses'],0)
        self.assertEqual(can[0]['prior_losses'],0)

    def test_expired_level_does_not_match(self):
        rows,_=joined(ends=[ended('MAX_AGE',RET-1)])
        self.assertEqual(rows[0]['hll_level_count_asof'],0)

    def test_left_censor_fails_closed_even_if_line_detected(self):
        rows,cands=joined(warmup_until_ms=RET+1)
        self.assertEqual(rows[0]['hll_coverage'],'UNKNOWN_HLL_WARMUP')
        self.assertEqual(rows[0]['hll_persistent_group'],'UNKNOWN_HLL_WARMUP')
        self.assertEqual(len(cands),0)

    def test_missing_swing_price_is_unknown_not_no_level(self):
        label,attrs=touch(price=None)
        rows,_=joined(label=label,attrs=attrs)
        self.assertEqual(rows[0]['hll_coverage'],'UNKNOWN_SWING_PRICE')

    def test_old_level_outside_max_age(self):
        rows,_=joined(lvs=[level(formed=RET-163*BASE)],ends=[ended()])
        self.assertEqual(rows[0]['hll_level_count_asof'],0)

    def test_frozen_identity_mismatch_fails_closed(self):
        label,attrs=touch()
        attrs['K1']['status']='TOUCH_FAILED'
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            joined(label=label,attrs=attrs)

    def test_price_reads_only_causal_swing_or_pivot(self):
        self.assertEqual(m.parse_causal_price('{"swing_price":101,"pivot_price":99}'),101.)
        self.assertEqual(m.parse_causal_price('{"pivot_price":99}'),99.)
        self.assertIsNone(m.parse_causal_price('{"swing_price":null}'))

    def test_freeze_integrity_detects_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            d=Path(folder)
            (d/'event_labels.csv').write_text('touch_key,coverage_all,group_pre\nA,1,NO_PERSISTENT_LEVEL_NEAR_PRE\n')
            (d/'candidate_levels.csv').write_text('touch_key\n')
            audit=dict(source_mode='OFFLINE_FROZEN',no_outcomes_used_for_labels=True,
                source_error_symbols=0,sha256={
                    p:m.sha(d/p) for p in ('event_labels.csv','candidate_levels.csv')})
            (d/'audit.json').write_text(json.dumps(audit))
            rows,_=m.read_frozen_labels(d)
            self.assertEqual(len(rows),1)
            (d/'event_labels.csv').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'hash changed'):
                m.read_frozen_labels(d)

if __name__=='__main__':unittest.main()
