import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import pandas as pd
from test_reaction_structural_level_atlas import candles, e, M, S
import reaction_structural_level_atlas_v2 as m


class LevelAtlasV2Tests(unittest.TestCase):
    def test_pre_levels_independent_of_retest_wick_and_future_outcome(self):
        d=candles(n=110)
        event=e(110*S)
        a,c=m.levels_for_tf(event,'15m',d,'NATIVE_15M',[110*S,102,103,100.1,102])
        changed=dict(event,outcome='SL_FIRST')
        b,c2=m.levels_for_tf(changed,'15m',d,'NATIVE_15M',[110*S,102,115,70,80])
        self.assertEqual(c,'COMPLETE');self.assertEqual(c2,'COMPLETE')
        self.assertTrue(a)
        fields=['zone_id','zone_low','zone_high','formation_closed_ts','gap_to_zone_atr',
                'level_state_pre','orientation','contact_band_pre']
        self.assertEqual([[r[k] for k in fields] for r in a],[[r[k] for k in fields] for r in b])

    def test_all_zones_kept_while_nearest_selected(self):
        ev=e(110*S)
        def z(price,tag,orientation='SUPPORT_BELOW',gap=.3,state='ACCEPTED_DEFENDED'):
            return {'event_key':ev['event_key'],'zone_id':tag,'level_price':price,'zone_low':price-.1,'zone_high':price+.1,
              'zone_atr_at_formation':.2,'atr_pre':.2,'pre_last_close':102.,'gap_to_zone_pct':abs(price-102)/102*100,
              'gap_to_zone_atr':gap,'level_state_pre':state,'orientation':orientation,
              'formation_closed_ts':10,'timeframe':'15m','defenses_pre':2,'acceptance_last4':4,
              'known_touch':False,'known_hold':False,'level_origin':'RESISTANCE_FLIP'}
        z1=z(100,'near',gap=.2)
        z2=z(101,'far',gap=3.,orientation='RESISTANCE_ABOVE')
        r=m.summarize_event(ev,{'15m':[z1,z2],'30m':[]},{'15m':'COMPLETE','30m':'COMPLETE'})
        self.assertEqual(r['n_zones_total'],2)
        self.assertEqual(r['primary_zone_id'],'near')
        self.assertEqual(r['pre_family'],'NEAR_ACCEPTED_DEFENDED')
        self.assertEqual(r['support_zone_id'],'near')
        self.assertEqual(r['opposing_zone_id'],'far')
        self.assertEqual(r['space_to_opposition_atr'],3.)

    def test_distant_level_not_defense(self):
        ev=e(110*S)
        z={'zone_id':'distant','zone_low':97.9,'zone_high':98.1,'level_price':98.,
          'gap_to_zone_atr':4.2,'gap_to_zone_pct':4.,'level_state_pre':'ACCEPTED_DEFENDED',
          'orientation':'SUPPORT_BELOW','formation_closed_ts':10,'timeframe':'15m',
          'defenses_pre':5,'acceptance_last4':4,'known_touch':False,'known_hold':False,
          'level_origin':'RESISTANCE_FLIP','zone_atr_at_formation':.2,'atr_pre':.2,'pre_last_close':102.}
        r=m.summarize_event(ev,{'15m':[z],'30m':[]},{'15m':'COMPLETE','30m':'COMPLETE'})
        self.assertEqual(r['pre_family'],'DISTANT_LEVEL_ONLY')
        self.assertEqual(r['known_family'],'NO_TOUCH_KNOWN')

    def test_incomplete_history_cannot_prove_absence(self):
        ev=e(110*S)
        a,c=m.levels_for_tf(ev,'15m',candles(n=20),'NATIVE_15M',None)
        self.assertFalse(a);self.assertEqual(c,'INSUFFICIENT')
        summary=m.summarize_event(ev,{'15m':[],'30m':[]},{'15m':c,'30m':'COMPLETE'})
        self.assertEqual(summary['pre_family'],'UNKNOWN_HISTORY')
        self.assertEqual(summary['all_covered'],0)

    def test_event_outcome_is_never_read_during_pre_selection(self):
        d=candles(110)
        events=[dict(e(110*S),outcome=x) for x in ('TP_FIRST','SL_FIRST')]
        d1,c=m.levels_for_tf(events[0],'15m',d,'NATIVE_15M',None)
        d2,c=m.levels_for_tf(events[1],'15m',d,'NATIVE_15M',None)
        self.assertEqual(d1,d2)

    def test_adjacent_candles_count_as_one_defense_episode(self):
        df=pd.DataFrame({'timestamp':[i*S for i in range(30)],'open':[105.]*30,
             'high':[105.5]*30,'low':[104.5]*30,'close':[105.]*30,'volume':[1000.]*30})
        df.loc[10:12,'low']=99.9
        df.loc[10:12,'close']=100.2
        df.loc[20:20,'low']=99.9
        df.loc[20:20,'close']=100.2
        zone={'high':100.1,'low':99.9,'center':100.}
        episodes=m._defense_episodes(df,zone,'LONG',5*S,S)
        self.assertEqual(episodes,2)

    def test_long_short_support_and_opposing_zones(self):
        low={'zone_low':99.,'zone_high':100.}
        high={'zone_low':105.,'zone_high':106.}
        self.assertEqual(m._orientation(102.,low,'LONG'),'SUPPORT_BELOW')
        self.assertEqual(m._orientation(102.,high,'LONG'),'RESISTANCE_ABOVE')
        self.assertEqual(m._orientation(102.,high,'SHORT'),'RESISTANCE_ABOVE')
        self.assertEqual(m._orientation(102.,low,'SHORT'),'SUPPORT_BELOW')

    def test_post_candle_changes_only_known_label_not_pre(self):
        df=candles(110)
        ev=e(110*S)
        aa,_=m.levels_for_tf(ev,'15m',df,'15m',[110*S,100,101,99.5,100])
        bb,_=m.levels_for_tf(ev,'15m',df,'15m',[110*S,100,120,50,120])
        ra=m.summarize_event(ev,{'15m':aa,'30m':[]},{'15m':'COMPLETE','30m':'COMPLETE'})
        rb=m.summarize_event(ev,{'15m':bb,'30m':[]},{'15m':'COMPLETE','30m':'COMPLETE'})
        self.assertEqual(ra['primary_zone_id'],rb['primary_zone_id'])
        self.assertEqual(ra['pre_family'],rb['pre_family'])

    def test_end_to_end_read_only_and_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/m.v1.lineage.ROOT_REL
            folder.mkdir(parents=True)
            db=folder/m.v1.lineage.CTX_NAME
            con=sqlite3.connect(db)
            con.execute('CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,quote_volume REAL)')
            con.execute('CREATE TABLE events(event_key TEXT,symbol TEXT,side TEXT,retest_ts INTEGER,observation_type TEXT)')
            rt=305*30*M
            con.execute('INSERT INTO events VALUES(?,?,?,?,?)',(f'XUSDT|LONG|{rt}','XUSDT','LONG',rt,'LIVE_OBSERVED'))
            for tf,step,n in [('15m',15*M,630),('30m',30*M,305)]:
                df=candles(n,step)
                con.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',
                    [('XUSDT',tf,int(x.timestamp),float(x.open),float(x.high),float(x.low),float(x.close),1000.,100_000.)
                     for x in df.itertuples(index=False)])
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',rt-M,102.,103.,101.,102.,1000.,100_000.))
            con.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('XUSDT','1m',rt,102.,103.,101.,102.,1000.,100_000.))
            con.commit();con.close()
            before=m.v1.lineage.hash_file(db)
            with patch.object(m.v1.lineage,'integrity',return_value={'events':7631,'frozen_cohort_sha256':'synthetic'}):
                result=m.run(root,1,'live-stratified')
                audit=m.audit(root)
            self.assertTrue(audit['success'])
            self.assertEqual(result['events'],1)
            self.assertEqual(result['candidate_levels'],audit['candidate_levels'])
            self.assertEqual(before,m.v1.lineage.hash_file(db))


if __name__=='__main__':unittest.main()
