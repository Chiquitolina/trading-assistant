import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import hll_detector_miss_audit_v1 as m


class MissAuditTests(unittest.TestCase):
    def bars(self, *values):
        return {i*m.TF:(op,hi,lo,cl) for i,(op,hi,lo,cl) in enumerate(values)}

    def test_tp_first_is_not_auto_trade(self):
        b=self.bars((100,101,99.7,100),(100,103.2,100,102))
        x=m.first_barrier(b,0,2,3,1)
        self.assertEqual(x['state'],'TP_FIRST')
        self.assertEqual(x['first_barrier_bars'],2)
        self.assertEqual(x['first_barrier_ts'],m.TF)

    def test_sl_first(self):
        b=self.bars((100,100.5,98.8,99),(99,105,98.6,102))
        self.assertEqual(m.first_barrier(b,0,2,3,1)['state'],'SL_FIRST')

    def test_simultaneous_bar_is_ambiguous_not_tp(self):
        b=self.bars((100,103.5,98,101))
        self.assertEqual(m.first_barrier(b,0,1,3,1)['state'],'SAME_BAR_AMBIGUOUS')

    def test_missing_candle_is_censored(self):
        b={0:(100,101,99.5,100),2*m.TF:(101,104,101,102)}
        x=m.first_barrier(b,0,3,3,1)
        self.assertEqual(x['state'],'INSUFFICIENT_POST')
        self.assertEqual(x['observed_bars'],1)

    def test_missing_anchor_is_not_zero_or_miss(self):
        x=m.first_barrier({},0,1,3,1)
        self.assertEqual(x['state'],'NO_REFERENCE_OPEN')
        self.assertEqual(m.candidate_bucket(x['state'],m.classified_routes('FAILED_BREAKOUT','NO_EARLY_CONTINUATION','NO_ORIGINAL_FAILURE')),'INCOMPLETE_PATH')

    def test_no_hit_with_complete_horizon(self):
        b=self.bars((100,101,99.5,100), (100,100.5,99.2,100))
        self.assertEqual(m.first_barrier(b,0,2,3,1)['state'],'NO_HIT')

    def test_confirmed_flip_even_if_later_lost(self):
        f=m.classified_routes('FLIP_CONFIRMED_LATER_LOST','NO_EARLY_CONTINUATION','NO_ORIGINAL_FAILURE')
        self.assertTrue(f['flip_confirmed'])
        self.assertTrue(f['any_confirmed'])
        self.assertFalse(f['reclaim_eligible'])
        self.assertEqual(m.candidate_bucket('TP_FIRST',f),'TP_FIRST_WITH_MECHANISM')

    def test_censored_detector_not_called_false_negative(self):
        f=m.classified_routes('CENSORED_RETEST','NO_EARLY_CONTINUATION','NO_ORIGINAL_FAILURE')
        self.assertEqual(m.candidate_bucket('TP_FIRST',f),'TP_FIRST_DETECTOR_INDETERMINATE')

    def test_reclaim_not_eligible_distinct_from_reclaim_negative(self):
        a=m.classified_routes('FAILED_BREAKOUT','NO_EARLY_CONTINUATION','NO_ORIGINAL_FAILURE')
        b=m.classified_routes('FAILED_BREAKOUT','NO_EARLY_CONTINUATION','NO_RECLAIM_CONTINUATION')
        self.assertFalse(a['reclaim_eligible'])
        self.assertTrue(b['reclaim_eligible'])

    def test_timestamps_exact_equivalence_and_reject_fractional(self):
        self.assertEqual(m.millis('1787292000000.0'),1787292000000)
        self.assertEqual(m.millis('1787292000000'),1787292000000)
        with self.assertRaises(ValueError):m.millis('1787292000000.5')

    def test_reproducible_selection_keeps_visual_control(self):
        syms=[f'A{i}USDT' for i in range(20)]
        a=m.select_symbols(syms,5,123,'A0USDT,A1USDT')
        b=m.select_symbols(syms,5,123,'A0USDT,A1USDT')
        self.assertEqual(a,b)
        self.assertIn('A0USDT',a[0]);self.assertIn('A1USDT',a[0])

    def test_synthetic_end_to_end_no_source_mutation_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            pre=root/m.PRE;v29=root/m.V29;epi=root/m.EPISODES;cd=root/m.CANDLES
            for x in (pre,v29,epi):x.parent.mkdir(parents=True,exist_ok=True)
            cd.mkdir(parents=True,exist_ok=True)
            def csv_write(p,cols,rows):
                with p.open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(rows)
            pre_rows=[];v29_rows=[];episodes=[]
            for n in range(290):
                sym=f'S{n:03d}USDT';aid=f'attempt-{n}'
                pre_rows.append(dict(attempt_id=aid,symbol=sym,level_id=f'level-{n}',
                                     breakout_closed_ts='0',pre_closed_ts='0',v26_status='FAILED_BREAKOUT',
                                     zone_low='99',zone_high='100'))
                episodes.append(dict(attempt_id=aid,episode_id=f'episode-{n}'))
                for br,status in [('BREAKOUT_CONTINUATION','NO_EARLY_CONTINUATION'),
                                  ('RECLAIM_CONTINUATION','NO_ORIGINAL_FAILURE')]:
                    v29_rows.append(dict(attempt_id=aid,origin_symbol=sym,branch=br,
                                         status=status,confirmed_ts=''))
            csv_write(pre,list(pre_rows[0]),pre_rows)
            csv_write(v29,list(v29_rows[0]),v29_rows)
            csv_write(epi,list(episodes[0]),episodes)
            (v29.parent/'manifest.json').write_text(json.dumps({'attempts':55717,'symbols':290,
                       'source_sha256':m.sha256(pre),'version':'TEST'}))
            # Force visual controls into pilot; no need to fabricate 290 candle archives.
            for sym in ['S000USDT','S001USDT']:
                candle_rows=[dict(timestamp=i*m.TF,open='100',high='100.5',low='99.5',close='100') for i in range(96)]
                candle_rows[1]['high']='103.5'
                csv_write(cd/f'{sym}_30m.csv',list(candle_rows[0]),candle_rows)
            original={p:m.sha256(p) for p in (pre,v29,epi,cd/'S000USDT_30m.csv')}
            dest=root/'miss_out'
            result=m.run(root,2,42,'S000USDT,S001USDT',False,dest)
            self.assertEqual(result['attempts'],2)
            self.assertEqual(result['scenario_rows'],8)
            summary=json.loads((dest/'summary.json').read_text())
            self.assertEqual(summary['by_scenario_category']['TP3_SL1_48']['TP_FIRST_WITHOUT_ANY_MECHANISM'],2)
            self.assertTrue(all(m.sha256(p)==digest for p,digest in original.items()))
            with self.assertRaises(FileExistsError):m.run(root,2,42,'S000USDT,S001USDT',False,dest)


if __name__ == '__main__':
    unittest.main()
