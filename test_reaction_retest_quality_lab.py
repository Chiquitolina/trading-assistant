import csv
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from reaction_retest_quality_lab import (
  FEATURES, BASE, calc_features, aligned, build_row, plan, build, audit, digest,
  ro, validate_sources,
)
from dashboard.views.reaction_microstructure_quality_view import score_binary, matrix, aggregate_display_5m, candles_plot

MIN=60000

def fake_bars(start,n,price=100,quote=1000,trades=10):
    return [(start+i*MIN,price,price+0.5,price-0.5,price+0.1,20,quote,trades,10,quote*.6)
            for i in range(n)]

class TestRetestQuality(unittest.TestCase):
    def test_pre_only_and_known_both_distinct(self):
        a=fake_bars(0,180);btc=fake_bars(0,180,price=200)
        ret=(180*MIN,100,104,95,103,50,5000,35,25,3200)
        x,status=calc_features('LONG',a,btc,ret,{"swing_price":99,"max_departure_pct":1.2})
        self.assertEqual(status,'COMPLETE')
        self.assertEqual(x['known_retest_range_pct'],9.0)
        self.assertGreater(x['known_retest_rejection_wick_share'],0)
        self.assertAlmostEqual(x['known_retest_distance_to_swing_pct'],(103/99-1)*100)
        self.assertEqual(x['known_departure_pct'],1.2)
        self.assertAlmostEqual(x['pre_taker_side_imbalance5'],.2)
        y,_=calc_features('LONG',a,btc,(180*MIN,100,200,1,100,50,5000,35,25,3200),{})
        for key in FEATURES:
            if key.startswith('pre_') or key.startswith('btc_pre_'):
                self.assertEqual(x[key],y[key],key)

    def test_short_direction_and_wick_rejection(self):
        a=fake_bars(0,180)
        sh,_=calc_features('SHORT',a,a,(180*MIN,100,109,98,99,50,5000,35,25,3200),{})
        lg,_=calc_features('LONG',a,a,(180*MIN,100,109,98,99,50,5000,35,25,3200),{})
        self.assertGreater(sh['known_retest_rejection_wick_share'],lg['known_retest_rejection_wick_share'])
        self.assertAlmostEqual(sh['known_retest_signed_body_pct'],1.)
        self.assertAlmostEqual(lg['known_retest_signed_body_pct'],-1.)
        self.assertAlmostEqual(sh['pre_taker_side_imbalance5'],-lg['pre_taker_side_imbalance5'])

    def test_bar_continuity(self):
        bars=fake_bars(0,180)
        self.assertTrue(aligned(bars,180*MIN,180))
        bars[55]=(bars[55][0]+MIN,)+bars[55][1:]
        self.assertFalse(aligned(bars,180*MIN,180))

    def test_read_only_retest_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'m.sqlite'
            con=sqlite3.connect(db)
            con.execute('CREATE TABLE candles (symbol TEXT,timeframe TEXT,open_ts INT,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL,trades INT,taker_buy_base REAL,taker_buy_quote REAL)')
            e=('TESTUSDT|LONG|10800000','TESTUSDT','LONG',180*MIN,181*MIN,181*MIN+25000,'LIVE_OBSERVED',json.dumps({'swing_price':99}))
            rows=[]
            for sym,px in [('TESTUSDT',100),('BTCUSDT',200)]:
                rows.extend([(sym,'1m',*bar) for bar in fake_bars(0,181,price=px)])
            cutoff=(180*MIN//900_000)*900_000
            for sym,px in [('TESTUSDT',100),('BTCUSDT',200)]:
                for i in range(96):
                    ts=cutoff-(96-i)*900_000
                    rows.append((sym,'15m',ts,px,px+.5,px-.5,px+.1,20,1000,10,10,600))
            con.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows);con.commit()
            row=build_row(con,e,'COMPLETE_VALID','COMPLETE')
            self.assertEqual(row['quality_feature_status'],'COMPLETE')
            self.assertEqual(row['known_swing_level'],99)
            self.assertEqual(row['first_seen_delay_sec'],25)
            before=con.execute('SELECT count(*) FROM candles').fetchone()[0]
            con.close()
            from contextlib import closing
            with closing(ro(db)) as reader:self.assertEqual(reader.execute('SELECT count(*) FROM candles').fetchone()[0],before)

    def test_all_catalog_export_and_hash(self):
        import reaction_retest_quality_lab as lab
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            folder=root/BASE/'reaction_full_context_v1';folder.mkdir(parents=True)
            out=root/BASE/'reaction_first_touch_lab_v1';out.mkdir(parents=True)
            catalog=root/BASE/'reaction_full_context_v1.sqlite3'
            con=sqlite3.connect(catalog)
            con.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INT,known_ts INT,first_seen_ms INT,observation_type TEXT,causal_json TEXT);CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INT,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL,trades INT,taker_buy_base REAL,taker_buy_quote REAL);')
            events=[];rows=[]
            for j in (180,220):
                ts=j*MIN
                event=(f'TESTUSDT|LONG|{ts}','TESTUSDT','LONG',ts,ts+MIN,ts+MIN+20_000,'LIVE_OBSERVED',json.dumps({'swing_price':101}) if j==180 else '{}')
                events.append(event)
            for sym,px in [('TESTUSDT',100),('BTCUSDT',200)]:
                rows.extend([(sym,'1m',*bar) for bar in fake_bars(0,221,price=px)])
            con.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',events)
            cutoff=(180*MIN//900_000)*900_000
            # Need 96 15m candles for both event anchors; the last event is at 220m.
            for sym,px in [('TESTUSDT',100),('BTCUSDT',200)]:
                for i in range(102):
                    ts=cutoff-(96-i)*900_000
                    rows.append((sym,'15m',ts,px,px+.5,px-.5,px+.1,20,1000,10,10,600))
            con.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
            con.executemany('INSERT INTO meta VALUES (?,?)',[('cohort_sha256','ABC'),('event_count','2')]);con.commit();con.close()
            pathdb=root/BASE/'auto_paths_v1.sqlite3'
            ap=sqlite3.connect(pathdb)
            ap.execute('CREATE TABLE events (event_key TEXT,entry_ts INT,state TEXT,count INT,path_blob BLOB)')
            from reaction_lab_auto_paths import encode_path
            sample=events[0]
            future=[((181+i)*MIN,100,100.2,99.8,100.0 if i<4 else 101.1) for i in range(360)]
            ap.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                       (sample[0],sample[3]+MIN,'COMPLETE',360,encode_path(future)))
            ap.commit();ap.close()
            audit_obj={'success':True,'conflicts':0,'incomplete_windows_total':0,'events':2,'ready':2,'frozen_cohort_sha256':'ABC','aggregate':{'unverified_mismatches_total':0}}
            (folder/'audit.json').write_text(json.dumps(audit_obj))
            with (out/'events_coverage_features.csv').open('w',newline='') as h:
                w=csv.DictWriter(h,fieldnames=['event_key','path_quality','path_status']);w.writeheader()
                for e in events:w.writerow({'event_key':e[0],'path_quality':'COMPLETE_VALID','path_status':'COMPLETE'})
            with (out/'first_touch_scenarios.csv').open('w',newline='') as h:h.write('event_key\n')
            checksum=digest(out/'first_touch_scenarios.csv')
            (out/'summary.json').write_text(json.dumps({'source_events':2,'frozen_cohort_sha256':'ABC','scenario_rows':20,'path_quality':{'COMPLETE_VALID':2},'file_sha256':{'first_touch_scenarios.csv':checksum}}))
            r=build(root)
            self.assertEqual(r['events'],2)
            self.assertEqual(r['feature_quality'],{'COMPLETE':2})
            with (root/BASE/'reaction_retest_quality_lab_v1'/'retest_quality_features.csv').open() as stream:
                indexed={row['event_key']:row for row in csv.DictReader(stream)}
            self.assertEqual(indexed[events[0][0]]['known_morphology'],'DEEP_BREAK_OUTSIDE')
            self.assertEqual(indexed[events[0][0]]['post_morphology'],'POST_LATE_RECLAIM_3_TO_15M')
            self.assertEqual(int(float(indexed[events[0][0]]['post_reclaim_minute_15m'])),5)
            self.assertEqual(indexed[events[1][0]]['post_morphology'],'NOT_DEEP_BREAK')
            self.assertTrue(audit(root)['success'])
            # Confirm the ONLY sqlite is the original, unchanged dataset.
            from contextlib import closing
            with closing(ro(catalog)) as reader:self.assertEqual(reader.execute('SELECT count(*) FROM candles').fetchone()[0],646)

    def test_updated_dashboard_has_one_isolated_retest_view(self):
        app = (Path(__file__).resolve().parent / 'dashboard' / 'app.py').read_text(encoding='utf-8')
        self.assertEqual(app.count('render_microstructure_quality_lab(BASE_DIR)'), 1)
        self.assertEqual(app.count('                "🔬 Retest Quality Lab",'), 1)
        self.assertIn('if lab_mode not in ("🔬 Retest Quality Lab",', app)
        compile(app, 'dashboard/app.py', 'exec')

    def test_htf_features_exported_from_complete_15m(self):
        a = fake_bars(0,180); btc=fake_bars(0,180,price=200)
        def bars15(price):
            return [(i*900_000, price, price+.3, price-.3, price+.1,50,1500,40,20,750)
                    for i in range(96)]
        x,status=calc_features('LONG',a,btc,None,{},bars15(100),bars15(200))
        self.assertEqual(status,'COMPLETE')
        for f in ('pre15m_range1h_pct','pre15m_range4h_pct',
                  'pre15m_atr14_pct','pre15m_side_return1h_pct',
                  'pre15m_side_return4h_pct','pre15m_vs_btc4h_pct',
                  'pre_quote_relative1_to_previous30',
                  'pre_taker_side_imbalance15','pre_rejection_mean5'):
            self.assertIn(f,FEATURES)
            self.assertIsNotNone(x[f], f)

    def test_display_5m_uses_only_closed_contiguous_minutes(self):
        bars=fake_bars(300_000,10)
        agg=aggregate_display_5m(bars)
        self.assertEqual(len(agg),2)
        self.assertEqual(agg[0][0],300_000)
        self.assertEqual(agg[0][1],bars[0][1])
        self.assertEqual(agg[0][4],bars[4][4])
        missing=bars[:3]+bars[4:]
        self.assertEqual(len(aggregate_display_5m(missing)),1)

    def test_plot_detail_annotations(self):
        bars=fake_bars(1792000000000,10)
        fig=candles_plot(bars,'test',verticals=[(bars[0][0],'RETEST','#ffffff')],
                         horizontal=[(100,'Entry','#cccccc')])
        self.assertEqual(len(fig.layout.shapes),2)

    def test_matrix_and_auc_descriptive(self):
        import pandas as pd
        z=pd.DataFrame({'pre_range15_pct':list(range(100)), 'pre_realized_vol60_pct':list(range(100)),
              'outcome':['SL_FIRST']*50+['TP_FIRST']*50})
        ranked=score_binary(z,'pre_range15_pct')
        self.assertEqual(ranked['auc_higher_is_tp'],1.)
        cells,_=matrix(z,'pre_range15_pct','pre_realized_vol60_pct',min_bin=10)
        self.assertGreater(len(cells),0)
        self.assertEqual(int(cells.n.sum()),100)

if __name__=='__main__':unittest.main()
