import unittest
import tempfile
import json
from pathlib import Path
import pandas as pd
import numpy as np
from hll_v24_tp_sl_sweep import grid, fmt_num, summarized, check_dedup_consistency, require_inputs, sha

class V24RiskTests(unittest.TestCase):
    def test_grid_unique_sorted(self):
        self.assertEqual(grid('2,0.75,1,2'),[.75,1.,2.])
        self.assertEqual(fmt_num(1.5),'1p5')
        with self.assertRaises(ValueError):grid('0,1')
        with self.assertRaises(ValueError):grid('360,720',0,360)
    def example(self):
        r=[]
        for id_,o,net in [('a','TP_FIRST',1.9),('b','SL_FIRST',-1.6),('c','BOTH_SAME_1M',np.nan),('d','TIME_EXIT',.3)]:
            r.append(dict(event_id=id_,symbol='ABCUSDT',stage='SUPPORT_CROSS_DOWN',direction='SHORT',
                known_closed_ts=1790000000000+(int(ord(id_)-97)*1800000),
                horizon_min=180,tp_pct=2.,sl_pct=1.5,fee_roundtrip_pct=.1,
                entry_ts=1790000060000,entry_price=100,path_state='COMPLETE_1M',
                outcome=o,return_after_fee_pct=net))
        return pd.DataFrame(r)
    def test_worst_ambiguous_is_loss(self):
        x=summarized(self.example(),'DEDUPE').iloc[0]
        self.assertEqual(x.TP,1);self.assertEqual(x.SL,1);self.assertEqual(x.TIME,1);self.assertEqual(x.AMB,1)
        self.assertAlmostEqual(x.mean_net_worst_pct,(1.9-1.6-1.6+.3)/4)
    def test_duplicate_same_bar_conflicting_outcome_fails(self):
        d=self.example()
        d.loc[1,'known_closed_ts']=d.loc[0,'known_closed_ts']
        with self.assertRaisesRegex(ValueError,'Conflicting outcomes'):
            check_dedup_consistency(d)
    def test_duplicate_same_bar_identical_pass(self):
        d=self.example(); r=d.iloc[0].copy();r['event_id']='z';d=pd.concat([d,pd.DataFrame([r])],ignore_index=True)
        check_dedup_consistency(d)
    def test_checksum_freeze(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x);r=p/'run';r.mkdir();minute=p/'min';minute.mkdir()
            s=pd.DataFrame([dict(event_id='e',symbol='A',stage='SUPPORT_CROSS_DOWN',known_closed_ts=1,level_id='x')])
            s.to_csv(r/'signals.csv',index=False)
            (r/'freeze.json').write_text(json.dumps({'signals_sha256':sha(r/'signals.csv')}))
            got,h=require_inputs(r,minute)
            self.assertEqual(len(got),1)
            self.assertEqual(h,sha(r/'signals.csv'))
            (r/'freeze.json').write_text(json.dumps({'signals_sha256':'BAD'}))
            with self.assertRaises(ValueError):require_inputs(r,minute)

if __name__=='__main__':unittest.main()
