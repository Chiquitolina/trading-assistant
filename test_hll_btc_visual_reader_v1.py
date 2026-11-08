import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import pandas as pd
from hll_btc_visual_reader_v1 import load_verified, plot_window, MINUTE

class BTCReaderVisual(unittest.TestCase):
    def make_archive(self, root, n=1440):
        p=Path(root)
        # Two complete 1-day blocks, UTC aligned; small dataset for tests.
        b=pd.DataFrame([{'timestamp': i*MINUTE, 'open':100+i/10000,
            'high':101+i/10000,'low':99+i/10000,'close':100.5+i/10000,
            'volume':10,'quoteVolume':1000} for i in range(n)])
        path=p/'BTCUSDT_1m.csv';b.to_csv(path,index=False)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        manifest={'version':'hll-btc-visual-candles-v1.0','symbol':'BTCUSDT',
            'integrity':'CONTIGUOUS_1M_VERIFIED','mode':'RUN_OFFLINE',
            'start_ms':0,'end_exclusive_ms':n*MINUTE,'bars':n,
            'expected_bars':n,'files_sha256':{'BTCUSDT_1m.csv':digest}}
        (p/'manifest.json').write_text(json.dumps(manifest))
        return b,manifest,path
    def test_read_and_aggregate_5m(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_archive(td)
            bars,m=load_verified(Path(td))
            agg=plot_window(bars,m,1440*MINUTE,1,'5m')
            self.assertEqual(len(agg),288)
            self.assertEqual(float(agg.high.iloc[0]),float(bars.high.iloc[:5].max()))
            self.assertEqual(float(agg.low.iloc[0]),float(bars.low.iloc[:5].min()))
            self.assertEqual(float(agg.close.iloc[0]),float(bars.close.iloc[4]))
    def test_1m_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_archive(td)
            bars,m=load_verified(Path(td))
            self.assertEqual(len(plot_window(bars,m,1440*MINUTE,1,'1m')),1440)
    def test_not_enough_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            self.make_archive(td)
            bars,m=load_verified(Path(td))
            with self.assertRaises(ValueError):
                plot_window(bars,m,1440*MINUTE,2,'5m')
    def test_hash_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _,_,p=self.make_archive(td)
            with p.open('a') as f:f.write('garbage\n')
            with self.assertRaises(ValueError):load_verified(Path(td))
    def test_missing_minute_rejected_even_with_rehashed_file(self):
        with tempfile.TemporaryDirectory() as td:
            _,m,p=self.make_archive(td)
            b=pd.read_csv(p).drop(index=30)
            b.to_csv(p,index=False)
            m['files_sha256']['BTCUSDT_1m.csv']=hashlib.sha256(p.read_bytes()).hexdigest()
            (Path(td)/'manifest.json').write_text(json.dumps(m))
            with self.assertRaises(ValueError):load_verified(Path(td))

if __name__=='__main__': unittest.main()
