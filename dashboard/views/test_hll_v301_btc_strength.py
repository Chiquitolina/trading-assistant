import unittest
import numpy as np
import pandas as pd

from dashboard.views.hll_v301_btc_strength import (
    INTERVAL, preentry_metrics, classify, features_for_entries, read_cases,
)


class BTCCausalResearchTests(unittest.TestCase):
    def build_prices(self, num=140):
        # Absolute aligned epoch for reliable timeframe tests.
        stamps=np.arange(num,dtype=np.int64)*INTERVAL+1_787_184_000_000
        br=np.array([.002*np.sin(i*.37)+.0004*np.cos(i*.13) for i in range(num-1)])
        ar=.8*br+np.array([.0008*np.sin(i*.19) for i in range(num-1)])
        btc=100*np.cumprod(np.r_[1,1+br])
        symbol=50*np.cumprod(np.r_[1,1+ar])
        return pd.DataFrame({'timestamp':stamps,'close_symbol':symbol,'close_btc':btc})

    def test_entry_before_current_bar_closed(self):
        p=self.build_prices()
        entry=int(p.timestamp.iloc[-1])+5*60_000
        m=preentry_metrics(p,entry)
        self.assertEqual(m['btc_context_status'],'AVAILABLE')
        self.assertEqual(m['context_last_close_ts'],int(p.timestamp.iloc[-2])+INTERVAL)
        self.assertLessEqual(m['context_last_close_ts'],entry)

    def test_at_boundary_previous_closed_bar_is_eligible(self):
        p=self.build_prices()
        entry=int(p.timestamp.iloc[-1])+INTERVAL
        m=preentry_metrics(p,entry)
        self.assertEqual(m['btc_context_status'],'AVAILABLE')
        self.assertEqual(m['context_last_close_ts'],entry)

    def test_future_prices_cannot_change_features(self):
        p=self.build_prices()
        entry=int(p.timestamp.iloc[-1])+5*60_000
        a=preentry_metrics(p,entry)
        future=p.copy()
        future.loc[future.index[-1],'close_symbol']*=5
        future.loc[future.index[-1],'close_btc']*=0.4
        b=preentry_metrics(future,entry)
        self.assertEqual(a,b)

    def test_missing_last_closed_bar_rejected(self):
        p=self.build_prices()
        entry=int(p.timestamp.iloc[-1])+INTERVAL
        p=p.iloc[:-1]
        self.assertEqual(preentry_metrics(p,entry)['btc_context_status'],'MISSING_LAST_CLOSED_30M')

    def test_gap_in_window_rejected(self):
        p=self.build_prices()
        entry=int(p.timestamp.iloc[-1])+INTERVAL
        p=p.drop(p.index[-20]).reset_index(drop=True)
        self.assertEqual(preentry_metrics(p,entry)['btc_context_status'],'GAP_IN_30M_LOOKBACK')

    def test_insufficient_history_is_missing_not_filled(self):
        p=self.build_prices(num=90)
        entry=int(p.timestamp.iloc[-1])+INTERVAL
        self.assertEqual(preentry_metrics(p,entry)['btc_context_status'],'NOT_ENOUGH_CLOSED_30M')

    def test_formula_corresponds_to_beta_residual(self):
        p=self.build_prices()
        m=preentry_metrics(p,int(p.timestamp.iloc[-1])+INTERVAL)
        self.assertAlmostEqual(m['btc_residual_4h_pct'],
              m['symbol_move_4h_pct']-m['btc_beta_30m_48h']*m['btc_move_4h_pct'],places=12)
        self.assertAlmostEqual(m['btc_r2_30m_48h'],m['btc_corr_30m_48h']**2,places=12)

    def test_bucket_classification_and_no_auto_filter(self):
        f=pd.DataFrame([
            {'entry_id':'one','btc_context_status':'AVAILABLE','btc_corr_30m_48h':-.2,
             'btc_residual_4h_pct':1.4,'symbol_move_4h_pct':1.0,'btc_move_4h_pct':-.5},
            {'entry_id':'two','btc_context_status':'NOT_ENOUGH_CLOSED_30M'}])
        out=classify(f)
        self.assertEqual(len(out),2)
        self.assertEqual(out.iloc[0]['btc_corr_bucket'],'NEGATIVE')
        self.assertEqual(out.iloc[0]['btc_strength_state_4h'],'ALT_UP_BTC_DOWN')
        self.assertEqual(out.iloc[1]['btc_strength_state_4h'],'MISSING')

    def test_no_automatic_filter_is_applied(self):
        f=pd.DataFrame([
            {'entry_id':'a','btc_context_status':'AVAILABLE','btc_corr_30m_48h':0.89,
             'btc_residual_4h_pct':-.9,'symbol_move_4h_pct':-1,'btc_move_4h_pct':0},
            {'entry_id':'b','btc_context_status':'AVAILABLE','btc_corr_30m_48h':-.3,
             'btc_residual_4h_pct':1.1,'symbol_move_4h_pct':2,'btc_move_4h_pct':-1}])
        out=classify(f)
        self.assertEqual(len(out),2)
        self.assertEqual(set(out.entry_id),{'a','b'})

if __name__=='__main__':unittest.main()
