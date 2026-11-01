#!/usr/bin/env python3
"""Unit and frozen-reference tests for HLL Anatomy PRE V1. No LIVE writes."""
import copy
import tempfile
import unittest
from pathlib import Path

import hll_structure_anatomy_pre_v1 as a
import hll_phase_reconstruction_pre as phase

ROOT=Path(__file__).resolve().parent
SAMPLE_ZIP=ROOT/'HLL_FUENTES_ANATOMIA_PRE_V2.zip'

class AnatomyPrePureTests(unittest.TestCase):
    def setUp(self):
        self.atr=.01
        self.pre=2000*a.BAR_MS
        self.meta={'attempt_id':'T0','symbol':'XUSDT','episode_id':'E0','level_id':'L0',
                   'pre_closed_ts':self.pre,'episode_ordinal':1,'atr_pre':self.atr,
                   'zone_low':1.01,'zone_high':1.02}
        self.bars=[]
        for j in range(288):
            op=self.pre-(288-j)*a.BAR_MS
            close=1.0+.0002*j+(.009 if j%7<3 else 0)
            self.bars.append({'open_ts':op,'close_ts':op+a.BAR_MS,'open':close,
                              'high':close+.001,'low':close-.001,'close':close})

    def test_future_candle_does_not_affect_snapshot(self):
        before=a.audit_snapshot(self.bars[-96:],self.meta,1,96)
        candles={r['open_ts']:copy.deepcopy(r) for r in self.bars}
        candles[self.pre]={'open_ts':self.pre,'close_ts':self.pre+a.BAR_MS,
                            'open':100,'high':100,'low':100,'close':100}
        with_future=a.take_pre(candles,self.pre,96)
        self.assertEqual(with_future,self.bars[-96:])
        self.assertEqual(before,a.audit_snapshot(with_future,self.meta,1,96))

    def test_pivot_confirmation_never_exceeds_pre(self):
        s,ps,ls,cs=a.audit_snapshot(self.bars[-96:],self.meta,.5,96)
        self.assertGreater(len(ps),2)
        for p in ps:
            self.assertLessEqual(p['confirmed_known_closed_ts'],self.pre)
            self.assertLess(p['pivot_open_ts'],p['confirmed_known_closed_ts'])
        for leg in ls:
            self.assertLessEqual(leg['end_confirmed_known_ts'],self.pre)
        for chain in cs:
            self.assertLessEqual(chain['fully_known_by_ts'],self.pre)

    def test_reuses_original_pivot_detector_exactly(self):
        bars=self.bars[-96:]
        orig,_,_=phase.close_reversal_pivots(bars,.5*self.atr)
        _,ps,_,_=a.audit_snapshot(bars,self.meta,.5,96)
        self.assertEqual([(p['type'],p['open_ts'],p['known_closed_ts']) for p in orig],
                         [(p['pivot_type'],p['pivot_open_ts'],p['confirmed_known_closed_ts']) for p in ps])

    def test_gap_is_incomplete_not_padded(self):
        candles={r['open_ts']:r for r in self.bars}
        del candles[self.pre-5*a.BAR_MS]
        self.assertIsNone(a.take_pre(candles,self.pre,96))
        self.assertIsNone(a.take_pre(candles,self.pre,288))

    def test_pre_cutoff_mismatch_rejected(self):
        wrong={**self.meta,'pre_closed_ts':self.pre+a.BAR_MS}
        with self.assertRaises(ValueError):
            a.audit_snapshot(self.bars[-96:],wrong,1,96)

    def test_96_and_288_independent(self):
        s96=a.audit_snapshot(self.bars[-96:],self.meta,1,96)[0]
        s288=a.audit_snapshot(self.bars,self.meta,1,288)[0]
        self.assertEqual(s96['window_bars'],96)
        self.assertEqual(s288['window_bars'],288)
        self.assertEqual(s96['signed_efficiency_48'],s288['signed_efficiency_48'])
        # Identical counts are possible; independent windows must not be conflated.
        self.assertNotEqual(self.bars[0]['open_ts'], self.bars[-96]['open_ts'])
        self.assertEqual(s288['window_bars'], 288)

    def test_residual_osc_is_drift_invariant(self):
        b=self.bars[-24:]
        linear=[{**x,'close':x['close']+j*.0006} for j,x in enumerate(b)]
        self.assertAlmostEqual(a.residual_oscillation(b,self.atr),a.residual_oscillation(linear,self.atr))

    def test_path_eff_signed_and_trend_not_amplitude(self):
        b=[{**x,'close':1+i*.01} for i,x in enumerate(self.bars[-24:])]
        self.assertAlmostEqual(a.path_efficiency(b),1.)
        for i,x in enumerate(b):x['close']=1-i*.01
        self.assertAlmostEqual(a.path_efficiency(b),-1.)

    def test_missing_three_lows_is_missing_not_zero(self):
        self.assertEqual(a.signed_pivot_stats([], 'LOW',1),(None,None,None))

    def test_no_overwrite_output(self):
        if not SAMPLE_ZIP.is_file():self.skipTest('Solo si ZIP de referencia está al lado')
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'already_exists';output.mkdir()
            with self.assertRaises(FileExistsError):
                a.main(['--run','--source-zip',str(SAMPLE_ZIP),'--output-dir',str(output)])

class FullReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SAMPLE_ZIP.is_file():raise unittest.SkipTest('ZIP fuente no está al lado del test')
        cls.rows,cls.candles,cls.episodes,_=a.load_source(SAMPLE_ZIP)

    def test_exact_case_membership_and_coverage(self):
        self.assertEqual(len(self.rows),36)
        self.assertEqual(len(self.episodes),15)
        for r in self.rows:
            for n in (96,288):
                self.assertIsNotNone(a.take_pre(self.candles[r['symbol']],r['pre_closed_ts'],n))

    def test_reference_cut_may_not_be_episode_first(self):
        refs={r['symbol']:r for r in self.rows if r['attempt_id']==a.REFERENCES[r['symbol']][0]}
        self.assertEqual(refs['BANDUSDT']['episode_ordinal'],2)
        self.assertEqual(refs['BICOUSDT']['episode_ordinal'],2)
        self.assertEqual(refs['AXLUSDT']['episode_ordinal'],5)
        self.assertEqual(refs['AWEUSDT']['episode_ordinal'],3)

    def test_96_pivots_match_existing_phase_function(self):
        for r in self.rows:
            bars=a.take_pre(self.candles[r['symbol']],r['pre_closed_ts'],96)
            for scale in a.SCALES:
                p,_,_=phase.close_reversal_pivots(bars,scale*r['atr_pre'])
                _,ps,_,_=a.audit_snapshot(bars,r,scale,96)
                self.assertEqual(len(p),len(ps))
                self.assertEqual([(x['open_ts'],x['known_closed_ts']) for x in p],
                                 [(x['pivot_open_ts'],x['confirmed_known_closed_ts']) for x in ps])

    def test_longitudinal_cases_separate_from_reference(self):
        for symbol,n in (('WAXPUSDT',9),('BSVUSDT',14)):
            entries=sorted((r for r in self.rows if r['symbol']==symbol),key=lambda r:r['episode_ordinal'])
            self.assertEqual(len(entries),n)
            self.assertEqual(entries[0]['attempt_id'],a.REFERENCES[symbol][0])
            self.assertEqual(entries[-1]['episode_ordinal'],n)

if __name__ == '__main__':unittest.main()
