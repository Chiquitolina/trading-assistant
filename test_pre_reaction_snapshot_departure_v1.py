import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent))
import pre_reaction_snapshot_departure_v1 as m
from pre_reaction_opportunity_lab import sha, iso_ms, ONE


def bars(start=1_800_000_000_000, n=100, base=100.):
    return [dict(timestamp=start+i*ONE,open=base,high=base+.1,
                 low=base-.1,close=base,volume=10.) for i in range(n)]


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as h:
        w=csv.DictWriter(h,fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class PureTests(unittest.TestCase):
    def test_strict_cutoff_ignores_future_data(self):
        rows=bars()
        dep=rows[60]['timestamp']
        row=m.asof_features(rows,'SHORT',100.,rows[30]['timestamp'],dep,99.7,dep+ONE)
        changed=copy.deepcopy(rows)
        for x in changed[61:]:
            x.update(open=10000.,high=10001.,low=9999.,close=10000.,volume=1e8)
        row2=m.asof_features(changed,'SHORT',100.,rows[30]['timestamp'],dep,99.7,dep+ONE)
        self.assertEqual(row,row2)
        self.assertEqual(row['last_closed_1m_open_ts'],dep)

    def test_short_and_long_orientation(self):
        rows=bars()
        dep=rows[60]['timestamp']
        a=m.asof_features(rows,'SHORT',100.,rows[30]['timestamp'],dep,99.75,dep+ONE)
        b=m.asof_features(rows,'LONG',100.,rows[30]['timestamp'],dep,100.25,dep+ONE)
        self.assertAlmostEqual(a['departure_excursion_pct'],.25)
        self.assertAlmostEqual(b['departure_excursion_pct'],.25)
        self.assertAlmostEqual(a['close_strength_side'],.5)
        self.assertAlmostEqual(b['close_strength_side'],.5)

    def test_reject_departure_bar_not_closed(self):
        rows=bars()
        dep=rows[60]['timestamp']
        with self.assertRaises(ValueError):
            m.asof_features(rows,'LONG',100.,rows[30]['timestamp'],dep,100.3,dep)

    def test_reject_gaps_before_departure(self):
        rows=bars()
        dep=rows[60]['timestamp']
        rows.pop(51)
        with self.assertRaisesRegex(ValueError,'Gap'):
            m.asof_features(rows,'LONG',100.,rows[30]['timestamp'],dep,100.3,dep+ONE)

    def test_reject_insufficient_warmup(self):
        rows=bars(n=28)
        dep=rows[25]['timestamp']
        with self.assertRaisesRegex(ValueError,'Insufficient'):
            m.asof_features(rows,'LONG',100.,rows[10]['timestamp'],dep,100.3,dep+ONE)

    def test_separate_labels_no_outcome_in_feature_schema(self):
        self.assertNotIn('status_first_touch',m.FEATURES)
        self.assertNotIn('first_touch_ts',m.FEATURES)
        self.assertNotIn('label_validation',m.FEATURES)
        self.assertEqual(m.label_row({
            'opportunity_key':'e','symbol':'A','side':'LONG','status':'TOUCH_INDECISIVE',
            'actionable_ts':str(3*ONE),'departure_known_ts':str(4*ONE),
            'first_touch_ts':str(8*ONE),
        },'EXACT_FIRST_TOUCH_PARITY')['label_validation'],'CANONICAL_FIRST_TOUCH_VALIDATED')

    def test_no_touch_label_explicit_uncertainty(self):
        label=m.label_row({'opportunity_key':'e','symbol':'A','side':'SHORT',
            'status':'ARMED_NO_RETEST','actionable_ts':str(3*ONE),
            'departure_known_ts':str(4*ONE),'first_touch_ts':''},
            'NO_TOUCH_BOTH_UNRESOLVED_CAUSAL_IDENTITY')
        self.assertIn('UNRESOLVED',label['label_validation'])
        self.assertEqual(label['first_touch_ts'],'')


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.pilot=self.root/'pilot'
        self.parity=self.root/'parity'
        self.cache=self.root/'cache'
        self.output=self.root/'output'
        self.pilot.mkdir()
        self.parity.mkdir()
        self.start=iso_ms('2026-10-12T00:00:00Z')
        self.end=self.start+ONE*60
        self.symbol='TESTUSDT'
        warm=self.start-180*ONE
        finish=self.end+361*ONE
        candle_path=self.cache/self.symbol/'2026-10.pkl.gz'
        candle_path.parent.mkdir(parents=True)
        data=pd.DataFrame(bars(warm,(finish-warm)//ONE))
        data.to_pickle(candle_path,compression='gzip')
        action=self.start+15*ONE
        depart=action+2*ONE
        touch=depart+3*ONE
        self.record={'opportunity_key':f'{self.symbol}|SHORT|{action-4*15*ONE}|{action}',
            'symbol':self.symbol,'side':'SHORT',
            'pivot_ts':action-4*15*ONE,'confirm_open_ts':action-15*ONE,
            'actionable_ts':action,'pivot_price':100.,
            'departure_ts':depart,'departure_known_ts':depart+ONE,
            'departure_price':99.7,'status':'REACTION','first_touch_ts':touch,
            'first_touch_status':'REACTION'}
        write_csv(self.pilot/'opportunities_PRE.csv',[self.record],list(self.record))
        manifest={'version':'pilot','start_utc':'2026-10-12T00:00:00+00:00',
            'end_exclusive_utc':'2026-10-12T01:00:00+00:00',
            'symbols':[self.symbol],'total':1,
            'opportunities_sha256':sha(self.pilot/'opportunities_PRE.csv'),
            'source_hashes':{str(candle_path):sha(candle_path)}}
        (self.pilot/'manifest.json').write_text(json.dumps(manifest))
        compare={'opportunity_key':self.record['opportunity_key'],
            'parity_status':'EXACT_FIRST_TOUCH_PARITY'}
        write_csv(self.parity/'first_touch_canonical_comparison.csv',[compare],list(compare))
        write_csv(self.parity/'canonical_only.csv',[],['symbol'])
        parity_report={
            'inputs_sha256':{'frozen_pilot':sha(self.pilot/'opportunities_PRE.csv')},
            'outputs_sha256':{'comparison':sha(self.parity/'first_touch_canonical_comparison.csv'),
                              'canonical_only':sha(self.parity/'canonical_only.csv')},
            'summary':{'hard_disagreements':0,'canonical_only_touches':0,
                       'exact_first_touches':1,'pilot_touches':1},
        }
        (self.parity/'canonical_replay_parity_report.json').write_text(json.dumps(parity_report))

    def test_end_to_end_and_audit(self):
        m.execute(self.root,self.pilot,self.parity,self.cache,self.output)
        m.audit(self.root,self.pilot,self.parity,self.output)
        data=m.csv_rows(self.output/'features_at_departure_PRE.csv')
        self.assertEqual(len(data),1)
        self.assertEqual(set(data[0]),set(m.FEATURES))
        self.assertEqual(data[0]['relative_volume_30m'],'1.0')
        labels=m.csv_rows(self.output/'first_touch_labels_SEPARATE.csv')
        self.assertEqual(labels[0]['status_first_touch'],'REACTION')
        self.assertEqual(labels[0]['label_validation'],'CANONICAL_FIRST_TOUCH_VALIDATED')
        with self.assertRaisesRegex(RuntimeError,'already exists'):
            m.execute(self.root,self.pilot,self.parity,self.cache,self.output)

    def test_reject_tampered_parity(self):
        path=self.parity/'first_touch_canonical_comparison.csv'
        path.write_text(path.read_text()+'\n')
        with self.assertRaisesRegex(RuntimeError,'Canonical parity CSV changed'):
            m.execute(self.root,self.pilot,self.parity,self.cache,self.output)

    def test_reject_hard_disagreement_even_if_frozen(self):
        path=self.parity/'canonical_replay_parity_report.json'
        data=json.loads(path.read_text());data['summary']['hard_disagreements']=1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError,'hard disagreements'):
            m.execute(self.root,self.pilot,self.parity,self.cache,self.output)

    def test_detect_tampered_output(self):
        m.execute(self.root,self.pilot,self.parity,self.cache,self.output)
        path=self.output/'features_at_departure_PRE.csv'
        path.write_text(path.read_text()+'\n')
        with self.assertRaisesRegex(RuntimeError,'Output modified'):
            m.audit(self.root,self.pilot,self.parity,self.output)

if __name__=='__main__': unittest.main()
