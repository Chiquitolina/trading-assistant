import unittest
import tempfile
from pathlib import Path
import pandas as pd
from hll_level_succession_v1c import successor_candidates, max_age_ids_from_ends, audit

class SuccessionV1CTests(unittest.TestCase):
    def setUp(self):
        self.rows=pd.DataFrame([
            dict(level_id='old',center=100,zone_low=99,zone_high=101,formation_closed_ts=10,last_observed_closed_ts=100,state_at_cutoff='EXPIRED'),
            dict(level_id='new',center=100.3,zone_low=99.3,zone_high=101.3,formation_closed_ts=120,last_observed_closed_ts=400,state_at_cutoff='ACTIVE'),
            dict(level_id='future',center=100,zone_low=99,zone_high=101,formation_closed_ts=500,last_observed_closed_ts=600,state_at_cutoff='ACTIVE'),
            dict(level_id='far',center=110,zone_low=109,zone_high=111,formation_closed_ts=130,last_observed_closed_ts=400,state_at_cutoff='ACTIVE')])
    def test_pairs_and_no_future(self):
        pairs=successor_candidates(self.rows,cutoff_ms=450,max_age_ids={'old'})
        self.assertEqual(pairs.successor_level_id.tolist(),['new'])
        self.assertGreater(pairs.overlap_iou.iloc[0],.7)
    def test_cutoff_blocks_birth(self):
        self.assertTrue(successor_candidates(self.rows,cutoff_ms=120,max_age_ids={'old'}).empty)
    def test_birth_after_end_strict(self):
        s=self.rows.copy();s.loc[s.level_id.eq('new'),'formation_closed_ts']=100
        self.assertTrue(successor_candidates(s,cutoff_ms=450,max_age_ids={'old'}).empty)
    def test_no_non_max_age(self):
        self.assertTrue(successor_candidates(self.rows,cutoff_ms=450,max_age_ids=set()).empty)
    def test_max_age_reason(self):
        ends=pd.DataFrame([dict(level_id='old',end_state='MAX_AGE',end_closed_ts=100),
                           dict(level_id='other',end_state='LOSS',end_closed_ts=200),
                           dict(level_id='future',end_state='MAX_AGE',end_closed_ts=500)])
        self.assertEqual(max_age_ids_from_ends(ends,cutoff_ms=450),{'old'})
    def test_threshold(self):
        self.assertTrue(successor_candidates(self.rows,cutoff_ms=450,min_overlap_iou=.9,max_age_ids={'old'}).empty)
    def test_invalid_missing(self):
        with self.assertRaises(ValueError):
            successor_candidates(self.rows.drop(columns='zone_low'),cutoff_ms=450)
    def test_offline_audit_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)/'frozen';base.mkdir()
            self.rows.to_csv(base/'snapshot_asof.csv',index=False)
            pd.DataFrame([dict(level_id='old',end_state='MAX_AGE',end_closed_ts=100)]).to_csv(base/'ends.csv',index=False)
            pd.DataFrame([dict(timestamp=300),dict(timestamp=420)]).to_csv(base/'candles_30m.csv',index=False)
            # This fixture uses tiny millisecond timestamps; 'future' at 500 ms
            # is still earlier than the synthetic cutoff (420 + 30m).
            # Exclude it explicitly so the fixture tests the intended one pair.
            self.rows.loc[self.rows.level_id.eq('future'), 'formation_closed_ts'] = 3_000_000
            self.rows.loc[self.rows.level_id.eq('future'), 'last_observed_closed_ts'] = 3_600_000
            self.rows.to_csv(base/'snapshot_asof.csv',index=False)
            pairs,summary,manifest=audit(base,Path(tmp)/'out')
            self.assertEqual(len(pairs),1)
            self.assertEqual(manifest['expired_with_candidates'],1)
            self.assertTrue((Path(tmp)/'out'/'succession_candidates_all.csv').exists())
    def test_duplicate(self):
        with self.assertRaises(ValueError):
            successor_candidates(pd.concat([self.rows,self.rows.iloc[:1]]),cutoff_ms=450)

if __name__=='__main__':unittest.main()
