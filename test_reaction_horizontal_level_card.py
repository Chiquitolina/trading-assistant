import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from reaction_horizontal_level_card import (
    MIN, PRE_BARS, classify_one, enrich_events, groups_summary,
    reaction_candle_label, setup_from_prices,
)


def bars(closes, start=0, touch=True):
    return [(start+i*MIN, v, v+(.3 if touch else 0),v-(.3 if touch else 0),v)
            for i,v in enumerate(closes)]


def ev(side='LONG',lo=99,hi=101):
    return {'event_key':'e1','symbol':'TESTUSDT','side':side,'retest_ts':15*MIN,
            'all_covered':1,'primary_zone_low':lo,'primary_zone_high':hi}


class HorizontalRichCardTests(unittest.TestCase):
    def test_long_at_support_is_defense_setup(self):
        r=setup_from_prices(side='LONG',zone_low=99,zone_high=101,
                            pre_bars=bars([102]*15))
        self.assertEqual(r['setup'],'DEFENSA_POTENCIAL')
    def test_short_at_resistance_is_defense_setup(self):
        r=setup_from_prices(side='SHORT',zone_low=99,zone_high=101,
                            pre_bars=bars([98]*15))
        self.assertEqual(r['setup'],'DEFENSA_POTENCIAL')
    def test_short_at_support_is_breakdown_not_defense(self):
        r=setup_from_prices(side='SHORT',zone_low=99,zone_high=101,
                            pre_bars=bars([102]*15))
        self.assertEqual(r['setup'],'RUPTURA_POTENCIAL')
    def test_long_at_resistance_is_breakout_not_defense(self):
        r=setup_from_prices(side='LONG',zone_low=99,zone_high=101,
                            pre_bars=bars([98]*15))
        self.assertEqual(r['setup'],'RUPTURA_POTENCIAL')
    def test_entered_zone_from_above_does_not_change_origin(self):
        r=setup_from_prices(side='SHORT',zone_low=99,zone_high=101,
                            pre_bars=bars([102]*12+[100]*3))
        self.assertEqual(r['origin'],'ABOVE')
        self.assertEqual(r['pre_location'],'IN_ZONE')
        self.assertEqual(r['setup'],'RUPTURA_POTENCIAL')
    def test_inside_zone_whole_window_is_unknown(self):
        r=setup_from_prices(side='SHORT',zone_low=99,zone_high=101,
                            pre_bars=bars([100]*15))
        self.assertEqual(r['setup'],'EN_ZONA_SIN_ORIGEN')
    def test_short_breakdown_candle_only_one_close_not_acceptance(self):
        result=reaction_candle_label(side='SHORT',setup='RUPTURA_POTENCIAL',
                     zone_low=99,zone_high=101,
                     retest_bar=(15*MIN,101,101.2,98,98.5))
        self.assertEqual(result,'CRUCE_INICIAL_1M')
    def test_short_resistance_rejection(self):
        result=reaction_candle_label(side='SHORT',setup='DEFENSA_POTENCIAL',
                     zone_low=99,zone_high=101,
                     retest_bar=(15*MIN,98.8,100.2,98.2,98.4))
        self.assertEqual(result,'RECHAZO_FAVORABLE_1M')
    def test_short_support_not_broken(self):
        result=reaction_candle_label(side='SHORT',setup='RUPTURA_POTENCIAL',
                     zone_low=99,zone_high=101,
                     retest_bar=(15*MIN,102,102.1,100,101.6))
        self.assertEqual(result,'PRUEBA_SIN_QUIEBRE')
    def test_long_support_lost(self):
        result=reaction_candle_label(side='LONG',setup='DEFENSA_POTENCIAL',
                     zone_low=99,zone_high=101,
                     retest_bar=(15*MIN,102,102.4,98.5,98.6))
        self.assertEqual(result,'NIVEL_PERDIDO_EN_CIERRE')
    def test_retest_candle_and_future_outcome_never_affect_pre(self):
        pre=bars([102]*15)
        a=classify_one(ev('SHORT'),pre,(15*MIN,102,103,98,98))
        b=classify_one(ev('SHORT'),pre,(15*MIN,102,104,100,103))
        self.assertEqual(a['setup'],b['setup'])
        self.assertEqual(a['pre_contacts_5'],b['pre_contacts_5'])
        self.assertNotEqual(a['known'],b['known'])
    def test_pressure_is_pre_only_and_not_equated_with_win(self):
        pre=bars([102]*10+[100.9]*5)
        r=classify_one(ev('SHORT'),pre,(15*MIN,102,103,98,98))
        self.assertTrue(r['pressure_pre'])
        self.assertEqual(r['pre_contacts_5'],5)
        self.assertEqual(r['setup'],'RUPTURA_POTENCIAL')
    def test_missing_pre_continuity_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'reports/reaction_lab/reaction_full_context_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL)')
                for i in range(15):
                    if i==9:continue
                    con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?)',
                                ('TESTUSDT','1m',i*MIN,102,103,101.5,102))
                con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?)',
                            ('TESTUSDT','1m',15*MIN,102,103,98,99))
            data=pd.DataFrame([ev('SHORT')]);enriched=enrich_events(root,data)
            self.assertEqual(enriched.iloc[0].setup_pre,'DESCONOCIDO')
            self.assertEqual(enriched.iloc[0].known_candle_label,'SIN_VELA_CERRADA')
    def test_read_only_enrich_and_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'reports/reaction_lab/reaction_full_context_v1.sqlite3'
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL)')
                for i in range(15):
                    con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?)',
                                ('TESTUSDT','1m',i*MIN,102,103,101.8,102))
                con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?)',
                            ('TESTUSDT','1m',15*MIN,102,102.2,98,98.5))
            before=db.stat().st_size
            data=pd.DataFrame([{**ev('SHORT'),'outcome':'TP_FIRST'}])
            enriched=enrich_events(root,data)
            self.assertEqual(enriched.iloc[0].setup_pre,'RUPTURA_POTENCIAL')
            self.assertEqual(enriched.iloc[0].known_candle_label,'CRUCE_INICIAL_1M')
            self.assertEqual(db.stat().st_size,before)
            tab=groups_summary(enriched)
            self.assertEqual(int(tab.iloc[0].TP),1)
            self.assertEqual(float(tab.iloc[0]['TP resueltos (%)']),100.0)
    def test_no_double_event(self):
        with tempfile.TemporaryDirectory() as td:
            dup=pd.DataFrame([ev('SHORT'),ev('SHORT')])
            with self.assertRaisesRegex(ValueError,'repetido'):
                enrich_events(Path(td),dup)

if __name__=='__main__':unittest.main()
