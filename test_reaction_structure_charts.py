"""Exact REACTION candle markers on both independent charts (no 15m lookahead)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import reaction_compression_lineage as v3
from dashboard.views.reaction_compression_lineage_view import chart_event
from dashboard.views.reaction_structural_level_atlas_view import chart_level

S=v3.TF['15m']

class ChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        p=self.root/v3.ROOT_REL
        p.mkdir(parents=True)
        with sqlite3.connect(p/v3.CTX_NAME) as con:
            con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL)')
            for i in range(40):
                con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','15m',i*S,100.,101.,99.,100.,10.,1000.))
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',40*S,100.,101.2,99.7,100.4,10.,1000.))
        self.r=40*S

    def test_box_chart_reaction_uses_exact_1m_close(self):
        row=pd.Series({'symbol':'XUSDT','side':'LONG','timeframe':'15m','retest_ts':self.r,
              'box_start_ts':20*S,'box_last_close_ts':32*S-1,'box_low':99.,'box_high':101.,
              'wall':101.,'breakout_ts':35*S})
        fig=chart_event(self.root,row)
        self.assertIsNotNone(fig)
        hits=[t for t in fig.data if getattr(t,'name','')=='REACTION 1m cerrada']
        self.assertEqual(len(hits),1)
        self.assertAlmostEqual(float(hits[0].y[0]),100.4)
        self.assertEqual(str(hits[0].marker.symbol),'triangle-up')
        self.assertEqual(len(fig.data[0].x),25)  # from 15th through 39th closed HTF candle

    def test_level_chart_reaction_uses_exact_1m_close(self):
        event=pd.Series({'symbol':'XUSDT','side':'SHORT','retest_ts':self.r})
        tf=pd.Series({'timeframe':'15m','level_price':100.,'zone_low':99.8,'zone_high':100.2,
                      'formation_closed_ts':20*S})
        fig=chart_level(self.root,event,tf)
        self.assertIsNotNone(fig)
        hits=[t for t in fig.data if 'REACT' in str(getattr(t,'text',[]))]
        self.assertEqual(len(hits),1)
        self.assertAlmostEqual(float(hits[0].y[0]),100.4)
        self.assertEqual(str(hits[0].marker.symbol),'triangle-down')
        self.assertEqual(len(fig.data[0].x),40)

if __name__=='__main__':unittest.main()
