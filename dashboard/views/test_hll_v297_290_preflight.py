import csv
import json
import tempfile
import unittest
from pathlib import Path
from dashboard.views.hll_v297_290_preflight import _symbols_from_csv,_csv_edges,survey,main

class Preflight290(unittest.TestCase):
    def test_unique_symbols(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.csv';p.write_text('symbol,foo\nAAAUSDT,2\nAAAUSDT,3\nBBBUSDT,4\n')
            self.assertEqual(_symbols_from_csv(p)[0], ['AAAUSDT','BBBUSDT'])
    def test_edges_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'AAAUSDT_1m.csv';p.write_text('timestamp,open,high,low,close\n1788748800000,1,1,1,1\n1788748860000,2,2,2,2\n')
            m=_csv_edges(p);self.assertTrue(m['exists']);self.assertEqual(m['last_ms'],1788748860000)
            self.assertFalse(_csv_edges(Path(d)/'none.csv')['exists'])
    def test_no_hidden_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); cd=d/'30m'; md=d/'1m';cd.mkdir();md.mkdir();
            a=d/'atlas'; (a/'shards').mkdir(parents=True)
            s=d/'signals';s.mkdir(); (a/'run_config.json').write_text(json.dumps({'symbols':['AAAUSDT']}))
            (s/'freeze.json').write_text(json.dumps({'symbols':['AAAUSDT']}))
            for suf in ('formation','events','ends'):(a/'shards'/('AAAUSDT.'+suf+'.csv')).write_text('id\n')
            entries, meta=survey(['AAAUSDT'], d/'missing.csv',cd,md,a,s,1788748800000,1788748860000)
            self.assertEqual(meta['v21_v22_ready'],1)
            self.assertEqual(meta['1m_edge_coverage'],0)
            self.assertEqual(meta['30m_edge_coverage'],0)
    def test_count_guard(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.csv';p.write_text('symbol\nAAAUSDT\n')
            with self.assertRaises(SystemExit):main(['--plan','--universe-csv',str(p)])
if __name__=='__main__':unittest.main()
