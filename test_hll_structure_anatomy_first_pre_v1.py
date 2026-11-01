#!/usr/bin/env python3
"""Safety, source and baseline lineage tests. No LIVE writes."""
import csv
import tempfile
import unittest
from pathlib import Path
import hll_structure_anatomy_first_pre_v1 as f
import hll_structure_anatomy_pre_v1 as a

ROOT=Path(__file__).resolve().parent
ZIP=ROOT/'HLL_FUENTES_ANATOMIA_PRE_V2.zip'
MAP=ROOT/'attempt_episode_map.csv'
if not MAP.exists():
    MAP=f.DEFAULT_MAP

class FirstBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ZIP.exists() or not MAP.exists():
            raise unittest.SkipTest('Ejecutar en raiz del VPS con ZIP y mapa completo disponibles')
        cls.first,cls.rows,cls.candles,_,_=f.load_first_baseline(ZIP,MAP)

    def test_all_15_are_authoritative_first(self):
        self.assertEqual(len(self.first),15)
        self.assertEqual(len(self.rows),40)
        self.assertEqual(len(set(r['episode_id'] for r in self.first)),15)
        for r in self.first:
            self.assertEqual(r['episode_ordinal'],1)
            self.assertEqual(r['episode_first_attempt_id'],r['attempt_id'])
            self.assertEqual(r['is_first_in_episode'],'1')

    def test_four_first_attempts_recovered_not_relabelled(self):
        expected={
          'BANDUSDT':('28aafba92b2ef71ccdfed0eb',2),
          'BICOUSDT':('56d8d48b44c41f40eedc0bd6',2),
          'AXLUSDT':('7d9a6de2a69176843bc83b53',5),
          'AWEUSDT':('2016c9f711d4d8ffef685d45',3),
        }
        for s,(first,ordinal) in expected.items():
            r=next(x for x in self.first if x['symbol']==s)
            self.assertEqual(r['attempt_id'],first)
            reviewed=next(x for x in self.rows if x['attempt_id']==a.REFERENCES[s][0])
            self.assertEqual(reviewed['episode_ordinal'],ordinal)
            self.assertNotEqual(r['attempt_id'],reviewed['attempt_id'])

    def test_all_first_pre_windows_complete(self):
        for r in self.first:
            for n in a.WINDOWS:
                bars=a.take_pre(self.candles[r['symbol']],r['pre_closed_ts'],n)
                self.assertEqual(len(bars),n)
                self.assertEqual(bars[-1]['close_ts'],r['pre_closed_ts'])
                self.assertTrue(all(x['close_ts']<=r['pre_closed_ts'] for x in bars))

    def test_first_baseline_metrics_are_independent_from_visual_notes(self):
        ss,_,_,_,missing,_=a.collect(self.first,self.candles)
        self.assertFalse(missing)
        baseline=f.baseline_table(self.first,ss)
        self.assertEqual(len(baseline),15)
        for row in baseline:
            self.assertNotIn('visual_reference_grade_report_only',row)
            self.assertNotIn('visual_reference_comment_report_only',row)
            self.assertNotIn('quality_score',row)

    def test_first_four_numerical_metrics_differ_from_reviewed_when_cuts_differ(self):
        rows=[r for r in self.rows if r['symbol'] in ('BANDUSDT','BICOUSDT','AXLUSDT','AWEUSDT')]
        ss,_,_,_,missing,_=a.collect(rows,self.candles)
        self.assertFalse(missing)
        first_in_group=[r for r in self.first if r['symbol'] in ('BANDUSDT','BICOUSDT','AXLUSDT','AWEUSDT')]
        co=f.comparisons(first_in_group,rows,ss)
        self.assertEqual(len(co),8)
        self.assertTrue(all(c['different_cuts']==1 for c in co))

    def test_waxp_bsv_preserved_full_followup(self):
        for symbol,expected in [('WAXPUSDT',9),('BSVUSDT',14)]:
            seq=sorted([r for r in self.rows if r['symbol']==symbol],key=lambda r:r['episode_ordinal'])
            self.assertEqual([r['episode_ordinal'] for r in seq],list(range(1,expected+1)))
            self.assertEqual(seq[0]['attempt_id'],a.REFERENCES[symbol][0])

    def test_output_existing_directory_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'existing';out.mkdir()
            with self.assertRaises(FileExistsError):
                f.main(['--run','--source-zip',str(ZIP),'--episode-map',str(MAP),
                    '--output-dir',str(out)])

    def test_invalid_first_is_rejected(self):
        sample=next(r for r in self.first if r['symbol']=='BANDUSDT').copy()
        sample['episode_ordinal']='abc'
        with self.assertRaises(ValueError):f.parse_mapped_row(sample)

if __name__=='__main__':unittest.main()
