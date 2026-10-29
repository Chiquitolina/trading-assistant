import unittest
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
from dashboard.views.hll_v301_btc_dependency_mtf import (
    MINUTE, FRAMES, aggregate_closed, at_entry, contextualize, LABELS
)

class BTCDependencyMTFTests(unittest.TestCase):
    @staticmethod
    def source(n=18000):
        # Nearly 12.5 days, enough for 4h correlation of seven days.
        t=1_787_184_000_000 + np.arange(n,dtype=np.int64)*MINUTE
        k=np.arange(n)
        btc=100*np.exp(np.cumsum(.0002*np.sin(k*.09)+.0001*np.cos(k*.021)))
        sym=35*np.exp(np.cumsum(.00025*np.sin(k*.09)+.00013*np.sin(k*.041)))
        return (pd.DataFrame({'timestamp':t,'close':sym}),
                pd.DataFrame({'timestamp':t,'close':btc}))

    def make_aligned(self,tf):
        alt,btc=self.source()
        a,b=FRAMES[tf][0:2]
        if a==30*MINUTE:
            alt=aggregate_closed(alt,MINUTE,a)
            btc=aggregate_closed(btc,MINUTE,a)
        alt=aggregate_closed(alt,a,b)
        btc=aggregate_closed(btc,a,b)
        return alt.merge(btc,on='timestamp',suffixes=('_symbol','_btc'))

    def test_all_five_timeframes_available_and_causal(self):
        for tf in FRAMES:
            with self.subTest(tf=tf):
                aligned=self.make_aligned(tf)
                entry=int(aligned.timestamp.iloc[-1])+FRAMES[tf][1]
                x=at_entry(aligned,entry,tf)
                self.assertEqual(x['status'],'AVAILABLE')
                self.assertEqual(x['last_close_ts'],entry)
                self.assertIn(x['dependency'],LABELS)
                self.assertAlmostEqual(x['residual_pct'],
                    x['symbol_move_pct']-x['beta']*x['btc_move_pct'],places=10)

    def test_no_lookahead_on_all_frames(self):
        for tf in FRAMES:
            with self.subTest(tf=tf):
                aligned=self.make_aligned(tf)
                entry=int(aligned.timestamp.iloc[-1])+MINUTE
                a=at_entry(aligned,entry,tf)
                b=aligned.copy()
                b.loc[b.index[-1],'close_symbol']*=25
                b.loc[b.index[-1],'close_btc']*=.1
                self.assertEqual(a,at_entry(b,entry,tf))

    def test_incomplete_5m_candle_rejected(self):
        a,_=self.source(100)
        a=a.iloc[:-1].copy()
        out=aggregate_closed(a,MINUTE,5*MINUTE)
        self.assertNotIn(int(a.timestamp.iloc[-1]//(5*MINUTE)*(5*MINUTE)),set(out.timestamp))

    def test_gaps_must_not_be_imputed(self):
        d=self.make_aligned('5m')
        entry=int(d.timestamp.iloc[-1])+5*MINUTE
        d=d.drop(d.index[-20]).reset_index(drop=True)
        self.assertEqual(at_entry(d,entry,'5m')['status'],'GAP_IN_LOOKBACK')

    def test_original_copied_and_independent_thresholds(self):
        def cls(corr,res):
            r2=corr**2
            copied=corr>=.80 and r2>=.60
            return ('btc_copied_weak' if res<=-.20 else 'btc_copied_neutral' if res<.20
                    else 'btc_correlated_with_strength') if copied else (
                    'independent_strength' if res>=.20 else
                    'independent_weakness' if res<=-.20 else 'mixed')
        self.assertEqual(cls(.81,-.20),'btc_copied_weak')
        self.assertEqual(cls(.81,.00),'btc_copied_neutral')
        self.assertEqual(cls(.81,.20),'btc_correlated_with_strength')
        self.assertEqual(cls(.72,.20),'independent_strength')
        self.assertEqual(cls(.72,-.20),'independent_weakness')
        self.assertEqual(cls(.72,0),'mixed')

    def test_matches_existing_30m_calculation_and_cardinality(self):
        from dashboard.views.hll_v301_btc_strength import preentry_metrics
        from dashboard.views.hll_v301_btc_dependency_mtf import read_minute
        alt,btc=self.source()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for folder in ('candles','minutes'):
                (root/folder).mkdir()
            alt.to_csv(root/'minutes'/'ETHUSDT_1m.csv',index=False)
            alt30=aggregate_closed(alt,MINUTE,30*MINUTE)
            btc30=aggregate_closed(btc,MINUTE,30*MINUTE)
            alt30.to_csv(root/'candles'/'ETHUSDT_30m.csv',index=False)
            ts=int(alt.timestamp.iloc[-1])+MINUTE
            entries=pd.DataFrame([{'entry_id':'e','symbol':'ETHUSDT','route':'FLIP_1M',
                 'entry_ts':ts,'entry_utc':'2026-09-02T00:00:00Z','entry_month':'2026-09',
                 'universe_group':'ADDITIONAL_275','pre_pattern_48':'ASCENDING_CLEAN'}])
            features=contextualize(entries,btc30,btc,
                minute_dir=root/'minutes',candle_dir=root/'candles')
            self.assertEqual(len(features),1)
            for tf in FRAMES:
                self.assertEqual(features.loc[0,f'btc_status_{tf}'],'AVAILABLE')
            old=alt30.merge(btc30,on='timestamp',suffixes=('_symbol','_btc'))
            reference=preentry_metrics(old,ts)
            self.assertAlmostEqual(features.loc[0,'btc_corr_30m'],reference['btc_corr_30m_48h'],places=11)
            self.assertAlmostEqual(features.loc[0,'btc_residual_pct_30m'],reference['btc_residual_2h_pct'],places=11)

    def test_1h_never_uses_half_open_higher_bar(self):
        a=self.make_aligned('1h')
        t=int(a.timestamp.iloc[-1])
        row=at_entry(a,t+30*MINUTE,'1h')
        self.assertEqual(row['last_close_ts'],t)

if __name__=='__main__': unittest.main()
