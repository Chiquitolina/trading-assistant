import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
from hll_reaction_overlay_v1 import load_frozen,select_events,add_reaction_traces

class ReactionOverlayV1(unittest.TestCase):
    def make(self,path):
        df=pd.DataFrame([dict(touch_key='btc-1',symbol='BTCUSDT',side='LONG',status='REACTION',
                              retest_ts=100000,known_ts=160000,swing_price=80000.,hll_coverage='COMPLETE',
                              hll_near_05atr_count=1,hll_persistent_group='HLL_ONLY'),
                         dict(touch_key='btc-2',symbol='BTCUSDT',side='SHORT',status='TOUCH_FAILED',
                              retest_ts=160000,known_ts=220000,swing_price=80100.,hll_coverage='COMPLETE',
                              hll_near_05atr_count=0,hll_persistent_group='NEITHER_NEAR')])
        df.to_csv(path/'event_hll_labels.csv',index=False)
        digest=hashlib.sha256((path/'event_hll_labels.csv').read_bytes()).hexdigest()
        (path/'audit.json').write_text(json.dumps(dict(version='universal-runner-hll-lifecycle-join-v1.0',
            events=2,no_post_outcomes_read=True,read_only_to_project=True,
            source_hashes={'event_hll_labels.csv':digest})))
    def test_load_verified_and_filter(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            data,_=load_frozen(p)
            self.assertEqual(len(data),2)
            selected,n=select_events(data,first=0,cutoff=180000)
            self.assertEqual(n,1);self.assertEqual(selected.touch_key.tolist(),['btc-1'])
            fig=go.Figure();add_reaction_traces(fig,selected)
            self.assertEqual(len(fig.data),1)
    def test_hash_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            with (p/'event_hll_labels.csv').open('a') as f:f.write('garbage\n')
            with self.assertRaises(ValueError):load_frozen(p)
    def test_incomplete_coverage_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.make(p)
            data=pd.read_csv(p/'event_hll_labels.csv');data.loc[0,'hll_coverage']='UNKNOWN'
            data.to_csv(p/'event_hll_labels.csv',index=False)
            a=json.loads((p/'audit.json').read_text());a['source_hashes']['event_hll_labels.csv']=hashlib.sha256((p/'event_hll_labels.csv').read_bytes()).hexdigest()
            (p/'audit.json').write_text(json.dumps(a))
            with self.assertRaises(ValueError):load_frozen(p)
    def test_limit_preserves_count_and_cutoff(self):
        rows=[]
        for i in range(200):rows.append(dict(touch_key=str(i),known_ts=i*60000,status='REACTION',side='LONG'))
        df=pd.DataFrame(rows)
        subset,total=select_events(df,first=0,cutoff=150*60000,limit=25)
        self.assertEqual(total,151);self.assertLessEqual(len(subset),25)
        self.assertTrue((subset.known_ts<=150*60000).all())

if __name__=='__main__':unittest.main()
