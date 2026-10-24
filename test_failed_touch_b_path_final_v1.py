import unittest
import math
from collections import Counter
import pandas as pd

import failed_touch_b_path_final_v1 as app
import failed_touch_causal_2pct_v1 as core
import failed_touch_causal_validation_50_v1 as v

START=1789630000000


def frame0(n=235):
    return pd.DataFrame(dict(timestamp=[START+i*60000 for i in range(n)],
        open=[100.]*n,high=[100.1]*n,low=[99.9]*n,close=[100.]*n,volume=[20.]*n))


def src(frame,idx=10,side='LONG',pivot=102.):
    label,bars=core.structural_path(frame,idx,side,pivot,180)
    trade,tbars,gross=core.trade_path(frame,idx,side,float(frame.iloc[idx].open),180,2.,1.5)
    return dict(event_key='FOOUSDT|fake',symbol='FOOUSDT',cluster_key='FOOUSDT|LONG|1',
        side=side,first_touch_ts=int(frame.iloc[0].timestamp),depth2_delay_min='7',
        signal_ts=int(frame.iloc[idx-1].timestamp),entry_ts=int(frame.iloc[idx].timestamp),
        entry_price='100',pivot_price=str(pivot),trade_outcome=trade,
        trade_bars=str(tbars),trade_gross_pct=str(gross) if gross is not None else '',
        structural_outcome=label,structural_bars=str(bars))

class Tests(unittest.TestCase):
    def test_expected_fieldnames_for_thresholds(self):
        self.assertEqual(app.label_0_1(0.5),'0_5')
        self.assertEqual(app.label_0_1(1.0),'1')
        self.assertEqual(app.label_0_1(1.5),'1_5')
        self.assertEqual(app.label_0_1(2.0),'2')

    def test_competing_risk_without_lookahead(self):
        f=[0.7,1.1,0.0,2.1]
        a=[0.2,0.3,1.6,0.4]
        self.assertEqual(app.first_hit_level(f,a,0.5)[0],'TP_FIRST')
        self.assertEqual(app.first_hit_level(f,a,1.0)[0],'TP_FIRST')
        self.assertEqual(app.first_hit_level(f,a,1.5)[0],'SL_FIRST')
        self.assertEqual(app.first_hit_level(f,a,2.0)[0],'SL_FIRST')
        self.assertEqual(app.first_hit_level([2.1],[1.6],2)[0],'SL_AMBIGUOUS')

    def test_long_mfe_after_sl_is_not_tradable(self):
        f=frame0()
        f.loc[10,['high','low','close']]=[100.8,99.7,100.1]
        f.loc[11,['high','low','close']]=[101.1,99.8,100.2]
        f.loc[12,['high','low','close']]=[100.3,98.4,99.0]
        f.loc[14,['high','low','close']]=[103.0,98.9,100.5]
        f.loc[15,['high','low','close']]=[102.4,99.9,102.1]
        r=app.evaluate_path(f,src(f))
        self.assertEqual(r['prior_trade_outcome'],'SL_FIRST')
        self.assertEqual(r['tp1_vs_sl1_5'],'TP_FIRST')
        self.assertEqual(r['tp2_vs_sl1_5'],'SL_FIRST')
        self.assertAlmostEqual(r['mfe_180_pct'],3.0)
        self.assertAlmostEqual(r['mfe_until_original_exit_pct'],1.1)
        self.assertEqual(r['pivot_reclaim_bar'],6)
        self.assertEqual(r['pivot_relost_within_entry_180'],True)
        self.assertEqual(r['sl_first_after_reclaim'],False)
        self.assertEqual(r['post_reclaim_entry_ts'],int(f.iloc[16].timestamp))
        self.assertTrue(r['post_reclaim_60m_complete'])

    def test_short_symmetric(self):
        f=frame0()
        f.loc[10,['high','low','close']]=[100.4,99.3,99.7]
        f.loc[11,['high','low','close']]=[100.2,98.9,99.1]
        f.loc[12,['high','low','close']]=[101.6,99.7,101.0]
        f.loc[14,['high','low','close']]=[101.1,97.0,100.0]
        r=app.evaluate_path(f,src(f,side='SHORT',pivot=98.))
        self.assertEqual(r['tp1_vs_sl1_5'],'TP_FIRST')
        self.assertEqual(r['tp2_vs_sl1_5'],'SL_FIRST')
        self.assertEqual(r['prior_trade_outcome'],'SL_FIRST')
        self.assertAlmostEqual(r['mfe_180_pct'],3.)

    def test_same_candle_censor_intrabar(self):
        f=frame0()
        f.loc[10,['high','low','close']]=[102.4,98.2,100.]
        r=app.evaluate_path(f,src(f))
        self.assertEqual(r['tp2_vs_sl1_5'],'SL_AMBIGUOUS')
        self.assertEqual(r['tp2_sl1_5_gross_pct'],'')
        self.assertEqual(r['prior_trade_outcome'],'SL_AMBIGUOUS')

    def test_trade_timeout_and_post_reclaim_close_causality(self):
        f=frame0()
        f.loc[11,['high','low','close']]=[101.,99.3,100.5]
        f.loc[30,['high','low','close']]=[101.9,99.5,101.8]
        r=app.evaluate_path(f,src(f,side='LONG',pivot=101.7))
        self.assertEqual(r['prior_trade_outcome'],'TIMEOUT')
        self.assertEqual(r['pivot_reclaim_bar'],21)
        self.assertEqual(r['post_reclaim_entry_ts'],int(f.iloc[31].timestamp))
        self.assertEqual(r['post_reclaim_60m_reloss'],True)

    def test_detect_source_mismatch(self):
        f=frame0()
        s=src(f)
        s['entry_ts']=str(int(s['entry_ts'])+60000)
        with self.assertRaises(RuntimeError):app.evaluate_path(f,s)
        s=src(f);s['trade_outcome']='TP_FIRST'
        with self.assertRaises(RuntimeError):app.evaluate_path(f,s)

    def test_missing_bar_fails(self):
        f=frame0(); f.loc[45,'timestamp']+=60000
        with self.assertRaises(RuntimeError):app.evaluate_path(f,src(frame0()))

    def test_frozen_B_selection_dedup_before_filter(self):
        rule=v.RULES['B_RETURN5_POS_T15']
        src=[dict(cluster_key='K',event_key='A',checkpoint_status='ACTIVE',checkpoint_min='15',favorable_return_5m_pct='-0.1'),
             dict(cluster_key='K',event_key='B',checkpoint_status='ACTIVE',checkpoint_min='15',favorable_return_5m_pct='0.5'),
             dict(cluster_key='L',event_key='C',checkpoint_status='ACTIVE',checkpoint_min='15',favorable_return_5m_pct='0.1')]
        picked=v.deduplicate(src)
        self.assertEqual([r['event_key'] for r in picked if v.eligible(r,rule)],['C'])

    def test_summary_includes_trade_and_postreclaim(self):
        f=frame0()
        r=app.evaluate_path(f,src(f))
        stats=app.summarize([dict(x) for x in (r,)],'ALL','ALL','ALL')
        self.assertEqual(stats['n'],1)
        self.assertEqual(stats['competing_tp_sl15']['0.5']['n'],1)
        self.assertEqual(stats['original']['TIMEOUT'],1)

if __name__=='__main__':unittest.main()
