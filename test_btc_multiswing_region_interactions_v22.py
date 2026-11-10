#!/usr/bin/env python3
"""Regression tests for frozen single-region physical-history audit V2.2."""
import unittest

import pandas as pd

from btc_multiswing_60d_explorer_v1 import M, asof_rows
from btc_multiswing_region_interactions_v22 import (
    audit_fixed_region, later_compatible_extrema,
)

BASE=1_800_000_000_000
STAGE={'region_id':'msv2_example','side':'HIGH','band_low':100.0,
       'band_high':102.0,'known_from':BASE}


def bar(n, op, hi, lo, cl):
    return {'timestamp':BASE+n*M,'open':op,'high':hi,'low':lo,'close':cl}


def candles():
    return pd.DataFrame([
        bar(0,99,99.5,98.5,99.3),
        bar(1,99,100.5,98,99),
        bar(2,99,101,98.5,100.5),
        bar(3,100.5,103,100,102.5),
        bar(4,102.5,104,102.2,103),
        bar(5,103,105,102.5,104),
        bar(6,104,104,99.5,99.7),
        bar(7,99.7,99.8,98,98.5),
        bar(8,98.5,99,97,98),
        bar(9,98,102,97,101),
        bar(10,101,102.2,100,101),
        bar(11,101,103,100,102.5),
        bar(12,102.5,103,102.5,102.8),
        bar(13,102.8,104,102.5,103),
    ])


def pivot(pid, time, price, *, known=None, side='HIGH',tf='15m'):
    return {'pivot_id':pid+'_'+tf,'side':side,'price':price,
            'physical_id':pid,'physical_identity_state':'VERIFIED',
            'physical_minute_ts':BASE+time*M,'pivot_ts':BASE+time*M,
            'available_ts':BASE+(known if known is not None else time+2)*M,
            'timeframe':tf}


class FixedRegionHistoryV22(unittest.TestCase):
    def audit(self, n=14, stage=STAGE, frame=None, horizon=60):
        return audit_fixed_region(candles() if frame is None else frame,
                                  stage,BASE+n*M,horizon_minutes=horizon)

    def test_contact_episodes_count_once_not_one_per_bar(self):
        events, contacts, summary=self.audit()
        self.assertEqual(len(contacts),3)
        self.assertEqual(contacts.touch_minutes.tolist(),[3,1,3])
        self.assertEqual(summary['contact_episodes'],3)
        self.assertEqual(summary['contact_minutes'],7)
        self.assertEqual(int((events.event_type=='CONTACT_START').sum()),3)
        self.assertEqual(int((events.event_type=='CONTACT_END').sum()),3)

    def test_wick_contact_separate_from_body(self):
        _, contacts,_=self.audit()
        self.assertEqual(contacts.body_overlap_minutes.tolist(),[2,1,3])
        self.assertEqual(int(contacts.iloc[0].start_ts),BASE+1*M)
        self.assertEqual(int(contacts.iloc[0].last_touch_close_ts),BASE+4*M)
        self.assertEqual(int(contacts.iloc[0].end_known_ts),BASE+5*M)

    def test_close_crosses_and_second_consecutive_close(self):
        events,_,stats=self.audit()
        self.assertEqual(stats['close_cross_above'],2)
        self.assertEqual(stats['close_cross_below'],1)
        self.assertEqual(stats['two_closes_above'],2)
        self.assertEqual(stats['two_closes_below'],2)
        self.assertEqual(events[events.event_type.eq('TWO_CLOSES_ABOVE')]
                         .event_ts.tolist(),[BASE+5*M,BASE+13*M])
        self.assertEqual(events[events.event_type.eq('TWO_CLOSES_BELOW')]
                         .event_ts.tolist(),[BASE+2*M,BASE+8*M])
        self.assertEqual(stats['last_close_position'],'ABOVE')

    def test_no_fictitious_cross_on_first_observed_bar(self):
        one=pd.DataFrame([bar(0,103,104,103,103)])
        e,c,s=self.audit(1,frame=one)
        self.assertEqual(s['close_cross_above'],0)
        self.assertEqual(s['two_closes_above'],0)
        self.assertEqual(len(c),0)
        self.assertTrue(e.empty)

    def test_censored_post_contact_never_implied_zero(self):
        _,contacts,_=self.audit()
        self.assertEqual(contacts.post_status.tolist(),[
            'NEXT_CONTACT_CENSORED','NEXT_CONTACT_CENSORED','CUTOFF_CENSORED'])
        self.assertEqual(contacts.post_minutes_observed.tolist(),[2,2,2])
        self.assertAlmostEqual(contacts.iloc[0].max_above_band_pct,(105-102)/102*100)
        self.assertEqual(contacts.iloc[0].max_below_band_pct,0.0)

    def test_open_episode_censored_at_cutoff(self):
        _, contacts, stats=self.audit(3)
        self.assertEqual(stats['contact_episodes'],1)
        self.assertEqual(contacts.iloc[0]['status'],'OPEN_AT_CUTOFF')
        self.assertTrue(pd.isna(contacts.iloc[0]['end_known_ts']))
        self.assertEqual(contacts.iloc[0]['post_status'],'CUTOFF_CENSORED')
        self.assertTrue(pd.isna(contacts.iloc[0]['max_above_band_pct']))

    def test_no_future_change_rewrites_history(self):
        early=self.audit(9)
        extra=pd.concat([candles(),pd.DataFrame([
            bar(14,103,150,90,150),bar(15,150,151,80,81)])],ignore_index=True)
        early_future=self.audit(9,frame=extra)
        pd.testing.assert_frame_equal(early[0],early_future[0])
        pd.testing.assert_frame_equal(early[1],early_future[1])
        self.assertEqual(early[2],early_future[2])

    def test_audit_starts_at_frozen_stage_not_first_member(self):
        stage={**STAGE,'known_from':BASE+4*M}
        e,c,s=self.audit(stage=stage)
        self.assertEqual(s['closed_minutes'],10)
        self.assertEqual(c.start_ts.min(),BASE+6*M)
        self.assertTrue((e.event_ts>BASE+4*M).all())

    def test_reject_gaps_and_missing_last_minute(self):
        with self.assertRaisesRegex(ValueError,'NONCONTIGUOUS'):
            self.audit(frame=candles().drop(index=4))
        with self.assertRaisesRegex(ValueError,'NONCONTIGUOUS'):
            self.audit(frame=candles().iloc[:-1])

    def test_reject_invalid_ohlc_or_stage(self):
        bad=candles().copy()
        bad.loc[4,'high']=101
        with self.assertRaisesRegex(ValueError,'INVALID_FROZEN_1M_OHLC'):
            self.audit(frame=bad)
        with self.assertRaisesRegex(ValueError,'INVALID_FIXED_REGION_STAGE'):
            self.audit(stage={**STAGE,'band_low':103})
        with self.assertRaisesRegex(ValueError,'INVALID_FIXED_REGION_STAGE'):
            self.audit(horizon=300)

    def test_time_is_closed_bar_end_not_open(self):
        events,_,_=self.audit()
        first=events.iloc[0]
        self.assertEqual(int(first.bar_open_ts),BASE+1*M)
        self.assertEqual(int(first.event_ts),BASE+2*M)

    def test_full_horizon_only_when_complete(self):
        _,contacts,_=self.audit()
        self.assertFalse((contacts.post_status=='FULL_HORIZON').any())
        rows=[bar(n,104,105,103,104) for n in range(14,75)]
        extended=pd.concat([candles(),pd.DataFrame(rows)],ignore_index=True)
        _,contacts,_=self.audit(75,frame=extended)
        self.assertEqual(contacts.iloc[-1].post_status,'FULL_HORIZON')
        self.assertEqual(int(contacts.iloc[-1].post_minutes_observed),60)

    def test_compatible_new_physical_extrema_asof(self):
        raw=pd.DataFrame([
            pivot('old',-10,101,known=-5),
            pivot('new',3,101.5,known=5),
            pivot('new',3,101.5,known=8,tf='30m'),
            pivot('future',11,101.1,known=15),
            pivot('outside',7,103.5,known=8),
            pivot('low',9,101.1,known=10,side='LOW'),
        ])
        known=asof_rows(raw,BASE+12*M,('15m','30m'))
        vals=later_compatible_extrema(known,STAGE,BASE+12*M)
        self.assertEqual(vals.physical_id.tolist(),['new'])
        self.assertEqual(int(vals.iloc[0].representations_asof),2)
        with self.assertRaisesRegex(ValueError,'FUTURE_COMPATIBLE_EXTREMUM_INPUT'):
            later_compatible_extrema(raw,STAGE,BASE+12*M)

    def test_no_runtime_dependency_on_live_or_hll(self):
        from pathlib import Path
        source=Path(__file__).parent/'dashboard/views/reaction_multiswing_btc60d_view_v1.py'
        txt=source.read_text(encoding='utf-8')
        self.assertIn('audit_fixed_region(',txt)
        self.assertIn('later_compatible_extrema(',txt)
        self.assertIn('Banda PRE fija',txt)
        self.assertNotIn('from hll_',txt)
        self.assertNotIn('from engine.live.',txt)

if __name__=='__main__':
    unittest.main()
