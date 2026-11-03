"""Synthetic tests: zero dependence on LIVE or whole historical datasets."""
import unittest
from hll_resistance_process_cohort_v1 import (
    early4, consolidate_touch_processes, slice_candles, summarize_one, TF,
)

START=1788899400000


def b(i, relation, physical=False, above=False, loss=False, recovery=False,defense=False):
    return {'bar_offset':i,'known_closed_ts':START+(i+1)*TF,
     'relation':relation,'physical_touch':physical,'touch_from_above':above,
     'cross_down':loss,'cross_up':recovery,'candidate_defense':defense}

class TestHLLProcess(unittest.TestCase):
    def test_intermit_pattern_waxp_shape(self):
        x=early4([b(0,'ABOVE'),b(1,'IN_ZONE'),b(2,'ABOVE'),b(3,'IN_ZONE')])
        self.assertTrue(x['early4_intermit'])
        self.assertFalse(x['early4_two_above'])
        self.assertEqual(x['known_closed_ts'],START+4*TF)

    def test_two_above_not_intermit(self):
        x=early4([b(0,'ABOVE'),b(1,'ABOVE'),b(2,'IN_ZONE'),b(3,'IN_ZONE')])
        self.assertTrue(x['early4_two_above'])
        self.assertFalse(x['early4_intermit'])

    def test_below_prevents_intermit(self):
        x=early4([b(0,'ABOVE'),b(1,'IN_ZONE'),b(2,'ABOVE'),b(3,'BELOW')])
        self.assertFalse(x['early4_intermit'])

    def test_incomplete_is_not_negative(self):
        x=early4([b(0,'ABOVE'),b(1,'IN_ZONE')])
        self.assertEqual(x['early4_state'],'INCOMPLETE')

    def test_contact_bars_clustered_not_independent(self):
        x=[b(i,'IN_ZONE',physical=i in (0,1,3,7,8),above=i in (1,7)) for i in range(10)]
        groups=consolidate_touch_processes(x,max_gap_bars=1)
        self.assertEqual(len(groups),2)
        self.assertEqual(groups[0]['physical_bars'],3)
        self.assertEqual(groups[1]['physical_bars'],2)
        self.assertEqual(groups[0]['span_bars'],4)

    def test_gap_threshold_changes_groups(self):
        rows=[b(i,'IN_ZONE',physical=i in (0,3)) for i in range(4)]
        self.assertEqual(len(consolidate_touch_processes(rows,max_gap_bars=2)),1)
        self.assertEqual(len(consolidate_touch_processes(rows,max_gap_bars=1)),2)

    def test_no_touch_no_process(self):
        self.assertEqual(consolidate_touch_processes([b(i,'ABOVE') for i in range(5)]),[])

    def test_slice_candles_includes_prior_and_requested(self):
        o={START+i*TF:(1,1.01,.99,1) for i in range(-1,10)}
        result=slice_candles(o,START,horizon=4)
        self.assertEqual(len(result),5)
        self.assertEqual(result[START]['close_ts'],START+TF)

    def test_offline_end_to_end_pilot_preserves_sources(self):
        import csv, json, hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from hll_resistance_process_cohort_v1 import run, PRE, V29, EPISODES, CANDLES

        def csv_write(path, fields, rows):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w', encoding='utf-8', newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields)
                writer.writeheader();writer.writerows(rows)

        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            pre=[]; v29=[]; episodes=[]
            for n in range(290):
                sym=f'S{n:03d}USDT'; aid=f'A{n:03d}'
                pre.append(dict(attempt_id=aid,symbol=sym,level_id='LEV',
                    breakout_closed_ts=START+TF,pre_closed_ts=START,
                    v26_status='UNRESOLVED_FIRST4',zone_low=1,zone_high=1.02))
                for branch,status in [('BREAKOUT_CONTINUATION','NO_EARLY_CONTINUATION'),
                                      ('RECLAIM_CONTINUATION','NO_ORIGINAL_FAILURE')]:
                    v29.append(dict(attempt_id=aid,origin_symbol=sym,branch=branch,
                                    status=status,confirmed_ts=''))
                episodes.append(dict(attempt_id=aid,symbol=sym,episode_id=f'E{n:03d}'))
                if n < 3:
                    candle=[]
                    for i,value in enumerate([1.03,1.01,1.03,1.01]):
                        candle.append(dict(timestamp=START+i*TF,open=value,
                              high=value+.02,low=value-.02,close=value))
                    candle.insert(0,dict(timestamp=START-TF,open=1.,high=1.03,low=.99,close=1.))
                    csv_write(root/CANDLES/f'{sym}_30m.csv',list(candle[0]),candle)
            csv_write(root/PRE,list(pre[0]),pre)
            csv_write(root/V29,list(v29[0]),v29)
            csv_write(root/EPISODES,list(episodes[0]),episodes)
            prehash=hashlib.sha256((root/PRE).read_bytes()).hexdigest()
            (root/V29.parent/'manifest.json').write_text(json.dumps(dict(
                attempts=55717,symbols=290,source_sha256=prehash,version='TEST')))
            outcome=run(root,pilot_size=3,seed=11,
                visual_controls='S000USDT,S001USDT,S002USDT',horizon=4)
            self.assertEqual(outcome['attempts'],3)
            self.assertEqual(outcome['intermittent4'],3)
            summary=json.loads((Path(outcome['output'])/'summary.json').read_text())
            self.assertEqual(summary['early4_intermit_episodes'],3)
            self.assertEqual(hashlib.sha256((root/PRE).read_bytes()).hexdigest(),prehash)
            with self.assertRaises(FileExistsError):
                run(root,pilot_size=3,seed=11,
                    visual_controls='S000USDT,S001USDT,S002USDT',horizon=4)

    def test_invalid_group_gap(self):
        with self.assertRaises(ValueError):
            consolidate_touch_processes([],max_gap_bars=-1)

    def test_synthetic_observation_does_not_infer_entry(self):
        pre=START
        raw={pre+(i-1)*TF:(1.05,1.06,1.04,1.05) for i in range(9)}
        r={'attempt_id':'A','symbol':'WAXPUSDT','level_id':'L',
            'pre_closed_ts':str(pre),'breakout_closed_ts':str(pre+TF),
            'zone_low':'1','zone_high':'1.02','v26_status':'UNRESOLVED_FIRST4'}
        row,clusters=summarize_one(r,raw,{},
            {'BREAKOUT_CONTINUATION':'LOST_ZONE_BEFORE_CONTINUATION',
             'RECLAIM_CONTINUATION':'NO_ORIGINAL_FAILURE'},'EP',horizon=5)
        self.assertEqual(row['audit_state'],'OK')
        self.assertNotIn('entry_ts',row)
        self.assertEqual(row['formation_atr_status'],'MISSING_OR_DUPLICATE')
        self.assertEqual(row['defenses_descriptive'],'')

if __name__=='__main__':unittest.main()
