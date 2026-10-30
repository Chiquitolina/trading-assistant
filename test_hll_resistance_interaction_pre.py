"""Causal and topology regression tests for HLL Resistance Interaction PRE V3."""
import contextlib
import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import hll_resistance_interaction_pre as m

PRE = 1790000000000 - (1790000000000 % m.BAR_MS)


def level(a='a',symbol='AAVEUSDT',pre=PRE):
    return dict(attempt_id=a,symbol=symbol,pre_closed_ts=pre,level_id='L1',
                zone_low=100.0,zone_high=101.0,atr_pre=2.0)


def candles(n=96,pre=PRE,prices=None):
    prices=prices if prices is not None else [96.0+i*0.04 for i in range(n)]
    assert len(prices)==n
    return [dict(open_ts=pre-(n-i)*m.BAR_MS,close_ts=pre-(n-i-1)*m.BAR_MS,
                 open=p,high=p+0.3,low=p-0.3,close=p) for i,p in enumerate(prices)]


def temp_csv(path,cols,rows):
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=cols);writer.writeheader();writer.writerows(rows)


class ResistancePreTests(unittest.TestCase):
    def test_phase_extracts_prices_and_level(self):
        got=m.metadata_phase([level()])
        self.assertEqual(got['a']['zone_low'],100.0)
        self.assertEqual(got['a']['level_id'],'L1')

    def test_labels_do_not_change_phase_metadata(self):
        r=level();r['visual_label']='BUENO';a=m.metadata_phase([r]);r['visual_label']='MALO'
        self.assertEqual(a,m.metadata_phase([r]))

    def test_bad_atr_fails(self):
        r=level();r['atr_pre']='0'
        with self.assertRaises(ValueError):m.metadata_phase([r])

    def test_bad_zone_fails(self):
        r=level();r['zone_high']='99'
        with self.assertRaises(ValueError):m.metadata_phase([r])

    def test_missing_zone_column_fails(self):
        r=level();del r['zone_high']
        with self.assertRaises(ValueError):m.metadata_phase([r])

    def test_conflicting_duplicate_attempt_fails(self):
        r=level();r2=dict(r);r2['zone_high']=102
        with self.assertRaises(ValueError):m.metadata_phase([r,r2])

    def test_identical_duplicates_allowed(self):
        self.assertEqual(len(m.metadata_phase([level(),level()])),1)

    def test_match_detects_missing(self):
        self.assertEqual(m.match_attempts({}, {'a':('AAVEUSDT',PRE)})['missing_phase'],['a'])

    def test_match_detects_pre_mismatch(self):
        self.assertEqual(m.match_attempts({'a':level(pre=PRE-m.BAR_MS)}, {'a':('AAVEUSDT',PRE)})['conflicts'],['a'])

    def test_v2_rejects_incomplete(self):
        v={'attempt_id':'a','symbol':'AAVEUSDT','pre_closed_ts':PRE,
           **{f'status_{n}':'COMPLETE_PRE' for n in m.WINDOWS}}
        v['status_96']='INSUFFICIENT_OR_GAPPED_OHLC'
        with self.assertRaises(ValueError):m.metadata_v2([v])

    def test_v2_rejects_duplicate(self):
        v={'attempt_id':'a','symbol':'AAVEUSDT','pre_closed_ts':PRE,
           **{f'status_{n}':'COMPLETE_PRE' for n in m.WINDOWS}}
        with self.assertRaises(ValueError):m.metadata_v2([v,v])

    def test_no_contact_is_valid(self):
        data=candles(12,prices=[90]*12)
        row,ev=m.describe_window(data,level(),12)
        self.assertEqual(row['touch_episodes'],0)
        self.assertEqual(row['touch_bars'],0)
        self.assertEqual(ev,[])
        self.assertEqual(row['last_touch_age_bars'],'')

    def test_consecutive_touch_merged(self):
        data=candles(12,prices=[94,96,100.0,100.1,95,96,100,97,97,97,98,99])
        row,ev=m.describe_window(data,level(),12)
        self.assertEqual(row['touch_episodes'],2)
        self.assertEqual(row['touch_bars'],3)
        self.assertEqual(ev[0]['touch_bars'],2)
        self.assertEqual(ev[1]['touch_bars'],1)

    def test_rejection_censored_when_final_bar_touches(self):
        data=candles(12,prices=[95]*11+[100])
        row,ev=m.describe_window(data,level(),12)
        self.assertEqual(row['censored_last_contact'],1)
        self.assertEqual(row['observed_rejections'],0)
        self.assertEqual(ev[-1]['rejection_depth_atr'],'')

    def test_rejection_observed_before_pre_only(self):
        data=candles(12,prices=[95]*4+[100,95,94,95,95,95,95,95])
        row,ev=m.describe_window(data,level(),12)
        self.assertEqual(row['observed_rejections'],1)
        self.assertGreater(row['max_observed_rejection_depth_atr'],0)
        self.assertEqual(row['censored_last_contact'],0)

    def test_intercontact_troughs_require_observed_gap(self):
        prices=[100,95,95,100,97,97,100,96,96,100,95,95]
        row,ev=m.describe_window(candles(12,prices=prices),level(),12)
        self.assertEqual(row['touch_episodes'],4)
        self.assertEqual(row['intercontact_troughs'],3)
        self.assertNotEqual(row['intercontact_trough_last_minus_first_atr'],'')

    def test_no_fake_intravela_order(self):
        data=candles(12,prices=[100]*12)
        row,ev=m.describe_window(data,level(),12)
        self.assertNotIn('wick_order',row)
        self.assertEqual(row['touch_episodes'],1)

    def test_near_distance_is_normalized_by_atr(self):
        data=candles(12,prices=[98.9]*12)
        row,_=m.describe_window(data,level(),12)
        self.assertEqual(row['near_range_0p5_atr_share'],1.0)
        self.assertEqual(row['touch_episodes'],0)

    def test_post_bar_rejected_by_window_guard(self):
        data=candles(12)
        data[-1]['close_ts']+=m.BAR_MS
        with self.assertRaises(ValueError):m.describe_window(data,level(),12)

    def test_gap_rejected_by_window_guard(self):
        data=candles(12)
        data[4]['open_ts']+=m.BAR_MS
        with self.assertRaises(ValueError):m.describe_window(data,level(),12)

    def test_window_is_independent_of_future_candles(self):
        d=candles(96)
        by_time={b['open_ts']:b for b in d}
        phase={'a':level()}; overview={'a':('AAVEUSDT',PRE)}
        first=m.analyze(phase,overview,{'AAVEUSDT':by_time})
        by_time[PRE]=dict(open_ts=PRE,close_ts=PRE+m.BAR_MS,
                          open=100,high=1000,low=1,close=900)
        self.assertEqual(first,m.analyze(phase,overview,{'AAVEUSDT':by_time}))

    def test_input_order_invariance(self):
        d=candles(96);phase={'a':level()};overview={'a':('AAVEUSDT',PRE)}
        forward={b['open_ts']:b for b in d};backward={b['open_ts']:b for b in reversed(d)}
        self.assertEqual(m.analyze(phase,overview,{'AAVEUSDT':forward}),
                         m.analyze(phase,overview,{'AAVEUSDT':backward}))

    def test_output_all_attempts_all_windows(self):
        d=candles(96);phase={'a':level(),'b':level('b')}
        overview={'a':('AAVEUSDT',PRE),'b':('AAVEUSDT',PRE)}
        rows,events,summaries=m.analyze(phase,overview,{'AAVEUSDT':{b['open_ts']:b for b in d}})
        self.assertEqual(len(rows),8)
        self.assertEqual(len(summaries),2)
        self.assertTrue(all(r['status']=='COMPLETE_PRE' for r in rows))

    def test_missing_bar_recorded_not_interpolated(self):
        d=candles(96);d.pop(10)
        rows,_,_=m.analyze({'a':level()}, {'a':('AAVEUSDT',PRE)}, {'AAVEUSDT':{b['open_ts']:b for b in d}})
        self.assertEqual(rows[-1]['status'],'MISSING_OR_GAPPED_OHLC')

    def test_cli_plan_and_run_synthetic_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);phase=root/'phase.csv';v2=root/'v2.csv';cd=root/'candles';cd.mkdir();out=root/'output'
            temp_csv(phase,['attempt_id','symbol','pre_closed_ts','level_id','zone_low','zone_high','atr_pre','visual_label'],
                     [{**level(), 'visual_label':'BUENO'}])
            temp_csv(v2,['attempt_id','symbol','pre_closed_ts']+[f'status_{n}' for n in m.WINDOWS],
                     [{'attempt_id':'a','symbol':'AAVEUSDT','pre_closed_ts':PRE,
                       **{f'status_{n}':'COMPLETE_PRE' for n in m.WINDOWS}}])
            temp_csv(cd/'AAVEUSDT_30m.csv',['timestamp','open','high','low','close'],
                     [{'timestamp':b['open_ts'], **{k:b[k] for k in ('open','high','low','close')}} for b in candles(96)])
            args=['--phase-source',str(phase),'--v2-source',str(v2),'--candles-dir',str(cd),
                  '--output',str(out),'--timestamp-kind','open']
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(['--plan']+args),0)
                self.assertFalse(out.exists())
                self.assertEqual(m.main(['--run']+args),0)
            self.assertEqual(len(m.v2.read_csv(out/'resistance_windows_pre.csv')),4)
            man=json.loads((out/'manifest.json').read_text())
            self.assertEqual(man['statuses'],{'COMPLETE_PRE':4})
            self.assertFalse(man['uses_post'])
            self.assertFalse(man['uses_visual_labels'])


if __name__=='__main__':unittest.main()
