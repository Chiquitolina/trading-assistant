import unittest, importlib.util, pathlib, csv, tempfile
p=pathlib.Path(__file__).with_name('hll_structural_approach_pre.py')
spec=importlib.util.spec_from_file_location('approach',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
PRE=10* m.BAR_MS

def chain(**kw):
    r=dict(attempt_id='a',symbol='X',pre_closed_ts=str(PRE),threshold_atr='1.0',chain_id='1',l0_open_ts='0',l0_known_ts='100',l0_price='90',h1_open_ts=str(m.BAR_MS),h1_known_ts=str(m.BAR_MS+100),h1_price='100',l1_open_ts=str(2*m.BAR_MS),l1_known_ts=str(2*m.BAR_MS+100),l1_price='95',h2_open_ts=str(3*m.BAR_MS),h2_pivot_known_ts=str(3*m.BAR_MS+100),h2_swing_known_ts=str(5*m.BAR_MS),h2_price='103',h2_status='SWING_CONFIRMED',first_rise_atr='2',correction_atr='1',second_rise_atr='1.5',correction_low_vs_first_low_atr='1',h2_vs_h1_atr='0.5',h2_distance_zone_atr='-0.2',first_rise_bars='1',correction_bars='1',second_rise_bars='1')
    r.update(kw);return r

def swing(**kw):
    r=dict(attempt_id='a',symbol='X',pre_closed_ts=str(PRE),threshold_atr='1.0',kind='L',open_ts=str(6*m.BAR_MS),pivot_known_ts=str(7*m.BAR_MS),swing_known_ts=str(8*m.BAR_MS),price='94',status='SWING_CONFIRMED')
    r.update(kw);return r

def summary(**kw):
    r=dict(attempt_id='a',symbol='X',pre_closed_ts=str(PRE),threshold_atr='1.0',excluded_dual_extreme_bars='0')
    r.update(kw);return r

class ApproachTests(unittest.TestCase):
    def calc(self,cs=None,ss=None,sm=None):return m.analyze(cs if cs is not None else [chain()],ss if ss is not None else [swing()],sm if sm is not None else [summary()])
    def test_causal_pivot(self):
        with self.assertRaises(ValueError):self.calc([chain(h2_pivot_known_ts=str(PRE+1))])
    def test_causal_swing(self):
        with self.assertRaises(ValueError):self.calc([chain(h2_swing_known_ts=str(PRE+1))])
    def test_causal_subsequent_swing(self):
        with self.assertRaises(ValueError):self.calc(ss=[swing(swing_known_ts=str(PRE+1))])
    def test_unconfirmed_future_not_counted(self):
        row,_=self.calc(ss=[swing(status='PIVOT_ONLY_UNCONFIRMED_SWING',swing_known_ts='')]);self.assertEqual(row[0]['later_confirmed_l_below_l1'],0)
    def test_confirmed_lower_low(self):
        row,_=self.calc();self.assertEqual(row[0]['later_confirmed_l_below_l1'],1)
    def test_later_high(self):
        row,_=self.calc(ss=[swing(kind='H',price='104')]);self.assertEqual(row[0]['later_confirmed_h_above_h2'],1)
    def test_no_older_swing_count(self):
        row,_=self.calc(ss=[swing(open_ts=str(2*m.BAR_MS))]);self.assertEqual(row[0]['later_swings_count'],0)
    def test_efficiency_proxy(self):
        row,_=self.calc();self.assertAlmostEqual(row[0]['pivot_path_efficiency_proxy'],2.5/4.5,places=5)
    def test_duration(self):
        row,_=self.calc();self.assertEqual(row[0]['chain_duration_bars'],3)
    def test_pivot_only_flag(self):
        row,_=self.calc([chain(h2_status='PIVOT_ONLY_UNCONFIRMED_SWING',h2_swing_known_ts='')]);self.assertIn('H2_PIVOT_ONLY',row[0]['diagnostic_flags'])
    def test_visual_label_invariant(self):
        a,b=self.calc();c,d=self.calc([dict(chain(),visual_label='MALO')],[dict(swing(),visual_label='BUENO')],[dict(summary(),visual_label='DUDOSO')]);self.assertEqual((a,b),(c,d))
    def test_input_order_invariant(self):
        r1=chain();r2=chain(chain_id='2',h2_open_ts=str(4*m.BAR_MS),h2_pivot_known_ts=str(4*m.BAR_MS+100),h2_swing_known_ts=str(5*m.BAR_MS));a,b=self.calc([r1,r2]);c,d=self.calc([r2,r1]);self.assertEqual((a,b),(c,d))
    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):self.calc([chain(),chain()])
    def test_no_chain(self):
        a,b=self.calc(cs=[]);self.assertEqual(a,[]);self.assertEqual(b[0]['chains_count'],0)
    def test_scale_consensus(self):
        c1=chain();c2=chain(threshold_atr='1.5');s1=summary();s2=summary(threshold_atr='1.5');_,b=self.calc([c1,c2],[],[s1,s2]);self.assertEqual([x['scale_consensus_latest_h2_open'] for x in b],[1,1])
    def test_plan_missing(self):
        with tempfile.TemporaryDirectory() as d:self.assertEqual(len(m.run(pathlib.Path(d),pathlib.Path(d))['missing']),4)

if __name__=='__main__':unittest.main()
