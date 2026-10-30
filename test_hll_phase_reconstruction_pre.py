"""Regression, causal and strict-source tests for HLL Phase Reconstruction PRE."""
import contextlib
import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import hll_phase_reconstruction_pre as m

PRE = 1790000000000 - (1790000000000 % m.BAR_MS)


def make_meta(a='a',symbol='AAVEUSDT',pre=PRE):
    return dict(attempt_id=a,symbol=symbol,pre_closed_ts=pre,
                level_id='L1',zone_low=100.0,zone_high=101.0,atr_pre=2.0)


def make_bars(prices=None,pre=PRE):
    if prices is None:
        prices = [96.0+i*.08 for i in range(m.LOOKBACK)]
    n=len(prices)
    return [dict(open_ts=pre-(n-i)*m.BAR_MS,
                 close_ts=pre-(n-i-1)*m.BAR_MS,
                 open=float(p),high=float(p)+.25,low=float(p)-.25,close=float(p))
            for i,p in enumerate(prices)]


def reversal_bars():
    prices=[100.0]*m.LOOKBACK
    prices[:12]=[100,99,98,97,98,99,100,101,102,101,100,99]
    for i in range(12,m.LOOKBACK):
        prices[i] = 99+(i-12)*0.10
    return make_bars(prices)


def write_csv(path, fields, rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)


class PhasePreTests(unittest.TestCase):
    def test_reversal_low_confirmed_after_close_move(self):
        bs=make_bars([100,99,98.2,99.2,100.2,101,100])
        p,mode,_=m.close_reversal_pivots(bs,2.0)
        self.assertEqual(p[0]['type'],'LOW')
        self.assertEqual(p[0]['index'],2)
        self.assertEqual(p[0]['known_closed_ts'],bs[4]['close_ts'])
        self.assertGreater(p[0]['known_closed_ts'],p[0]['pivot_close_ts'])
        self.assertEqual(mode,1)

    def test_reversal_high_confirmed_after_fall(self):
        bs=make_bars([100,101,101.8,100.8,99.8,99])
        p,mode,_=m.close_reversal_pivots(bs,2)
        self.assertEqual(p[0]['type'],'HIGH')
        self.assertEqual(p[0]['index'],2)
        self.assertEqual(p[0]['known_closed_ts'],bs[4]['close_ts'])
        self.assertEqual(mode,-1)

    def test_pivots_alternate(self):
        bs=make_bars([100,98.2,100.5,102.8,99,97,100,103,99])
        p,_,_=m.close_reversal_pivots(bs,2)
        self.assertEqual([x['type'] for x in p],['LOW','HIGH','LOW','HIGH'])
        self.assertEqual(sorted(x['index'] for x in p),[x['index'] for x in p])

    def test_monotone_has_one_confirmed_anchor_and_provisional_end(self):
        bs=make_bars([100+i for i in range(m.LOOKBACK)])
        sm, piv, legs, _=m.describe_attempt(bs,make_meta(),.5)
        self.assertEqual(len(piv),1)
        self.assertEqual(len(legs),1)
        self.assertEqual(legs[0]['status'],'PROVISIONAL')
        self.assertEqual(legs[0]['end_extreme_confirmed_known_closed_ts'],'')
        self.assertEqual(sm['confirmed_up_legs'],0)

    def test_too_small_moves_no_pivot_and_no_fake_phase(self):
        bs=make_bars([100.0+i*.001 for i in range(m.LOOKBACK)])
        s,p,l,c=m.describe_attempt(bs,make_meta(),1.5)
        self.assertEqual((p,l,c),([],[],[]))
        self.assertEqual(s['terminal_leg_role'],'UNRESOLVED')
        self.assertEqual(s['note'],'NO_CONFIRMED_CLOSE_REVERSAL')

    def test_no_intrabar_high_low_infer(self):
        bs=make_bars([100.0]*m.LOOKBACK)
        for b in bs:
            b['high']=b['close']+500;b['low']=max(0.1,b['close']-50)
        _,p,l,c=m.describe_attempt(bs,make_meta(),.5)
        self.assertEqual((p,l,c),([],[],[]))

    def test_96_cutoff_excludes_forming_bar(self):
        bs=reversal_bars()
        m.describe_attempt(bs,make_meta(),1)
        bs[-1]['close_ts'] +=m.BAR_MS
        with self.assertRaises(ValueError):m.describe_attempt(bs,make_meta(),1)

    def test_missing_interior_bar_gives_missing_status(self):
        bs=reversal_bars();d={b['open_ts']:b for b in bs};d.pop(bs[42]['open_ts'])
        s,p,l,c=m.analyze({'a':make_meta()},{'a':('AAVEUSDT',PRE)},{'AAVEUSDT':d})
        self.assertEqual(len(s),3)
        self.assertTrue(all(x['status']=='MISSING_OR_GAPPED_OHLC' for x in s))
        self.assertEqual((p,l,c),([],[],[]))

    def test_history_reordering_invariant(self):
        bs=reversal_bars()
        def run(rows):
            return m.analyze({'a':make_meta()},{'a':('AAVEUSDT',PRE)},
                             {'AAVEUSDT':{b['open_ts']:b for b in rows}})
        self.assertEqual(run(bs),run(reversed(bs)))

    def test_future_candle_ignored_even_large_move(self):
        bs=reversal_bars();d={b['open_ts']:b for b in bs}
        args=({'a':make_meta()},{'a':('AAVEUSDT',PRE)})
        earlier=m.analyze(*args,{'AAVEUSDT':d})
        d[PRE]=dict(open_ts=PRE,close_ts=PRE+m.BAR_MS,open=100,high=1e6,low=.01,close=800)
        self.assertEqual(earlier,m.analyze(*args,{'AAVEUSDT':d}))

    def test_pivot_known_before_cutoff_and_prefix_stability(self):
        bs=reversal_bars()
        prefix=bs[:24]
        before,_,_=m.close_reversal_pivots(prefix,1)
        after,_,_=m.close_reversal_pivots(bs,1)
        known=[p for p in after if p['known_closed_ts']<=prefix[-1]['close_ts']]
        self.assertEqual(before,known)

    def test_label_is_not_used_in_any_result(self):
        bs=reversal_bars();base=make_meta();m2=dict(base,visual_label='CONSTRUCTIVO')
        self.assertEqual(m.describe_attempt(bs,base,.5),m.describe_attempt(bs,m2,.5))
        m2['visual_label']='NO_CONVENCE'
        self.assertEqual(m.describe_attempt(bs,base,.5),m.describe_attempt(bs,m2,.5))

    def test_candidate_low_confirmation_and_all_retained(self):
        bs=reversal_bars();s,p,l,c=m.describe_attempt(bs,make_meta(),.5)
        self.assertEqual(len(c),sum(x['pivot_type']=='LOW' for x in p))
        self.assertEqual(sum(x['is_latest_confirmed_low'] for x in c),1)
        self.assertTrue(all(x['anchor_low_known_closed_ts']>=x['anchor_low_open_ts']+m.BAR_MS for x in c))
        self.assertTrue(all(x['candidate_is_retrospective_not_trade']==1 for x in c))

    def test_candidate_metrics_change_with_progress(self):
        bs=reversal_bars();_,_,_,cand=m.describe_attempt(bs,make_meta(),.5)
        self.assertTrue(cand)
        for c in cand:
            self.assertEqual(c['gap_reduction_atr'],c['gap_to_zone_at_low_atr']-c['gap_to_zone_at_pre_atr'])
            self.assertLessEqual(c['zone_touch_bars_since_low'],c['age_from_low_bars']+1)

    def test_left_boundary_pivot_explicitly_flagged(self):
        bs=make_bars([100+i for i in range(m.LOOKBACK)])
        _, piv, _, cand=m.describe_attempt(bs,make_meta(),.5)
        self.assertEqual(piv[0]['pivot_is_window_boundary'],1)
        self.assertEqual(cand[0]['anchor_is_window_boundary'],1)

    def test_no_candidate_if_only_high_anchor(self):
        bs=make_bars([105-i*.1 for i in range(m.LOOKBACK)])
        s,p,l,c=m.describe_attempt(bs,make_meta(),.5)
        self.assertEqual([x['pivot_type'] for x in p],['HIGH'])
        self.assertEqual(c,[])
        self.assertEqual(s['terminal_leg_direction'],'DOWN')

    def test_low_anchor_can_be_undercut_without_exclusion(self):
        bs=reversal_bars();s,p,l,c=m.describe_attempt(bs,make_meta(),.5)
        # All confirmed low candidates retained even if later lows supersede them.
        self.assertEqual(len(c),sum(x['pivot_type']=='LOW' for x in p))

    def test_zone_side(self):
        self.assertEqual(m.zone_side(99,100,101),'BELOW')
        self.assertEqual(m.zone_side(100.5,100,101),'INSIDE')
        self.assertEqual(m.zone_side(102,100,101),'ABOVE')

    def test_reject_wrong_PRE_timestamp(self):
        bs=reversal_bars()
        with self.assertRaises(ValueError):m.describe_attempt(bs,make_meta(pre=PRE-m.BAR_MS),1)

    def test_atr_sensitivity_all_scales_and_no_duplicates(self):
        bs=reversal_bars()
        phase={'a':make_meta(),'b':make_meta('b')}
        overview={'a':('AAVEUSDT',PRE),'b':('AAVEUSDT',PRE)}
        got=m.analyze(phase,overview,{'AAVEUSDT':{b['open_ts']:b for b in bs}})
        self.assertEqual(len(got[0]),6)
        self.assertEqual({x['scale_atr'] for x in got[0]},set(m.SCALES))
        self.assertTrue(all(x['level_group_attempts']==2 for x in got[0]))
        self.assertEqual({x['status'] for x in got[0]},{'COMPLETE_PRE'})

    def test_missing_source_attempt_is_error(self):
        with self.assertRaises(ValueError):m.analyze({}, {'a':('AAVEUSDT',PRE)}, {})

    def test_v3_duplicate_row_fails(self):
        rows=self.v3_rows()
        with self.assertRaises(ValueError):m.validate_v3_rows(rows+[rows[0]], {'a':make_meta()},{'a':('AAVEUSDT',PRE)})

    def test_v3_missing_window_fails(self):
        with self.assertRaises(ValueError):m.validate_v3_rows(self.v3_rows()[:-1],{'a':make_meta()},{'a':('AAVEUSDT',PRE)})

    def test_v3_wrong_zone_fails(self):
        rows=self.v3_rows();rows[0]['zone_low']='90'
        with self.assertRaises(ValueError):m.validate_v3_rows(rows,{'a':make_meta()},{'a':('AAVEUSDT',PRE)})

    def test_v3_wrong_PRE_fails(self):
        rows=self.v3_rows();rows[0]['pre_closed_ts']=str(PRE-m.BAR_MS)
        with self.assertRaises(ValueError):m.validate_v3_rows(rows,{'a':make_meta()},{'a':('AAVEUSDT',PRE)})

    def test_v3_rounding_tolerated(self):
        phase={'a':{**make_meta(), 'zone_low':100.000000004,'atr_pre':2.000000004}}
        m.validate_v3_rows(self.v3_rows(),phase, {'a':('AAVEUSDT',PRE)})

    def test_v3_incomplete_fails(self):
        rows=self.v3_rows();rows[1]['status']='MISSING_OR_GAPPED_OHLC'
        with self.assertRaises(ValueError):m.validate_v3_rows(rows,{'a':make_meta()},{'a':('AAVEUSDT',PRE)})

    def test_empty_output_no_write_plan_and_real_write_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'result';cd=root/'candles';cd.mkdir()
            phase=root/'phase.csv';v2=root/'v2.csv';v3=root/'v3.csv'
            m0=make_meta()
            write_csv(phase,list(m0)+['visual_label'],[{**m0,'visual_label':'GOOD'}])
            write_csv(v2,['attempt_id','symbol','pre_closed_ts']+[f'status_{n}' for n in (12,24,48,96)],
                [{'attempt_id':'a','symbol':'AAVEUSDT','pre_closed_ts':PRE,**{f'status_{n}':'COMPLETE_PRE' for n in (12,24,48,96)}}])
            write_csv(v3,list(self.v3_rows()[0]),self.v3_rows())
            write_csv(cd/'AAVEUSDT_30m.csv',['timestamp','open','high','low','close'],
                [{'timestamp':b['open_ts'],**{k:b[k] for k in ('open','high','low','close')}} for b in reversal_bars()])
            params=['--phase-source',str(phase),'--v2-source',str(v2),'--v3-source',str(v3),
                    '--candles-dir',str(cd),'--timestamp-kind','open','--output',str(out)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(['--plan']+params),0)
            self.assertFalse(out.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(['--run']+params),0)
            mf=json.loads((out/'manifest.json').read_text())
            self.assertEqual(mf['status_counts'],{'COMPLETE_PRE':3})
            self.assertEqual(len(m.v2.read_csv(out/'phase_summary_pre.csv')),3)
            self.assertEqual(len(m.v2.read_csv(out/'phase_confirmed_pivots_pre.csv')),mf['confirmed_pivot_rows'])
            self.assertFalse(mf['guardrails']['uses_post'])
            self.assertFalse(mf['guardrails']['modifies_dashboard'])

    def test_no_input_labels_in_CSV_exports(self):
        for names in (m.SUMMARY_FIELDS,m.PIVOT_FIELDS,m.LEG_FIELDS,m.CANDIDATE_FIELDS):
            self.assertFalse(any('label' in n for n in names))

    def test_nonfinite_atr_rejected(self):
        with self.assertRaises(ValueError):m.close_reversal_pivots(reversal_bars(),float('nan'))

    @staticmethod
    def v3_rows():
        return [{'attempt_id':'a','symbol':'AAVEUSDT','level_id':'L1','pre_closed_ts':str(PRE),
                 'window_bars':str(n),'status':'COMPLETE_PRE', 'zone_low':'100',
                 'zone_high':'101','atr_pre':'2'} for n in (12,24,48,96)]


if __name__=='__main__':unittest.main()
