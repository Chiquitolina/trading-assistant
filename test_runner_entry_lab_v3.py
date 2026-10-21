import csv
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

import runner_entry_lab_v3 as e

MIN=e.v1.MIN


def path(side='LONG',price=100.,start=1800000000000,n=360):
    start=start//MIN*MIN
    return [(start+i*MIN,price,price,price,price) for i in range(n)]


class EntryLabTests(unittest.TestCase):
    def test_strict_observation_after_actual_first_seen(self):
        base=1800000000000//MIN*MIN
        self.assertEqual(e.observed_open_index(base+43000,base),1)
        self.assertEqual(e.observed_open_index(base+60000,base),2)
        self.assertEqual(e.observed_open_index(base+200000,base),4)
        p=path(start=base)
        r=(base-MIN,100.,101.,99.,100.)
        self.assertEqual(e.resolve_entry(p,r,'LONG',base+43000,'LIVE_OBSERVED','OBSERVED_NEXT_OPEN'),(1,'OBSERVED_OPEN'))
        self.assertEqual(e.resolve_entry(p,r,'LONG',base+43000,'RECOVERED_WINDOW','OBSERVED_NEXT_OPEN')[1],
                         'RECOVERED_NOT_LIVE_OBSERVABLE')
        self.assertEqual(e.resolve_entry(p,r,'LONG',base+190*MIN,'LIVE_OBSERVED','OBSERVED_NEXT_OPEN')[1],
                         'FIRST_SEEN_AFTER_HORIZON')
        self.assertEqual(e.resolve_entry(p,r,'LONG',base+43000,'LIVE_OBSERVED','THEORETICAL_NEXT_OPEN')[0],0)

    def test_confirmation_long_short_must_close_and_enter_next_open(self):
        p=path()
        base=p[0][0]
        ret=(base-MIN,100.,100.2,99.8,100.)
        p[0]=(base,100.,100.4,99.9,100.3)
        p[1]=(base+MIN,100.3,100.3,100.1,100.2)
        self.assertEqual(e.resolve_entry(p,ret,'LONG',base+30000,'LIVE_OBSERVED','CONFIRM_1M')[0],1)
        # If event first appears AFTER that candle closed, previous confirm cannot be traded retroactively.
        self.assertIsNone(e.resolve_entry(p,ret,'LONG',base+2*MIN+1,'LIVE_OBSERVED','CONFIRM_1M')[0])
        p=path();p[0]=(base,100.,100.1,99.5,99.6)
        self.assertEqual(e.resolve_entry(p,ret,'SHORT',base+20000,'LIVE_OBSERVED','CONFIRM_1M')[0],1)

    def test_revisit_needs_departure_then_different_candle_retouch(self):
        p=path();base=p[0][0];ret=(base-MIN,100.,100.2,99.8,100.)
        p[0]=(base,100,100.7,100.,100.6)  # confirmed departure
        p[1]=(base+MIN,99.96,100.65,99.95,100.1) # revisits ref and closes on favorable side
        p[2]=(base+2*MIN,100.1,100.1,100.1,100.1)
        self.assertEqual(e.resolve_entry(p,ret,'LONG',base+30000,'LIVE_OBSERVED','REVISIT_RETEST_REF_1M')[0],2)
        p[1]=(base+MIN,100.6,100.65,99.9,99.95) # must close >= ref to confirm long
        for j in range(2,len(p)):
            p[j]=(base+j*MIN,101.,101.,101.,101.)
        self.assertIsNone(e.resolve_entry(p,ret,'LONG',base+30000,'LIVE_OBSERVED','REVISIT_RETEST_REF_1M')[0])
        p=path();p[0]=(base,100.,100.,99.3,99.4)
        p[1]=(base+MIN,100.,100.05,99.3,99.95)
        self.assertEqual(e.resolve_entry(p,ret,'SHORT',base+30000,'LIVE_OBSERVED','REVISIT_RETEST_REF_1M')[0],2)

    def test_stops_fixed_vol_and_wick_asof(self):
        ret=(0,100.,100.5,99.,100.)
        px,dist,status=e.stop_for('RETEST_WICK_0P10',100.,ret,'LONG',1.5)
        self.assertEqual(status,'VALID');self.assertAlmostEqual(px,98.901)
        self.assertAlmostEqual(dist,1.099)
        px,dist,status=e.stop_for('RETEST_WICK_0P10',100.,ret,'SHORT',1.5)
        self.assertEqual(status,'VALID');self.assertAlmostEqual(px,100.6005)
        self.assertAlmostEqual(dist,.6005)
        self.assertEqual(e.stop_for('VOL_0P75_CLIP',100.,ret,'LONG',10.)[1],2.5)
        self.assertEqual(e.stop_for('VOL_0P75_CLIP',100.,ret,'SHORT',.5)[1],.75)
        self.assertEqual(e.stop_for('FIXED_1',100.,ret,'LONG',1.)[1],1.)
        bad=e.stop_for('RETEST_WICK_0P10',100.,(0,100.,100.05,99.95,100.),'LONG',1.)
        self.assertIsNone(bad[0]);self.assertIn('OUTSIDE',bad[2])

    def test_first_touch_same_candle_is_never_win_and_horizon_exit(self):
        p=path();base=p[0][0]
        p[1]=(base+MIN,100.,103.5,98.5,100.)
        x=e.evaluate_trade(p,'LONG',0,99.,1.,3.)
        self.assertEqual(x['state'],'AMBIGUOUS_SAME_1M')
        self.assertEqual(x['gross_pct'],-1.)
        p[1]=(base+MIN,100.,103.,99.5,102.)
        self.assertEqual(e.evaluate_trade(p,'LONG',0,99.,1.,3.)['state'],'TP_FIRST')
        self.assertEqual(e.evaluate_trade(p,'SHORT',0,101.,1.,3.)['state'],'SL_FIRST')
        p=path()
        p[179]=(base+179*MIN,100.,100.5,99.7,100.4)
        x=e.evaluate_trade(p,'LONG',1,99.,1.,3.)
        self.assertEqual(x['state'],'TIME_EXIT');self.assertAlmostEqual(x['gross_pct'],.4)
        self.assertEqual(x['exit_minute'],180)

    def test_gap_through_stop_is_disclosed(self):
        p=path();base=p[0][0]
        p[1]=(base+MIN,98.,98.5,97.9,98.1)
        result=e.evaluate_trade(p,'LONG',0,99.,1.,3.)
        self.assertEqual(result['state'],'SL_FIRST')
        self.assertEqual(result['gap_through_stop'],1)

    def test_reference_parity_catches_regression(self):
        k='XUSDT|LONG|123';rows=[]
        for tp in e.TPS:
            rows.append({'event_key':k,'entry_mode':'THEORETICAL_NEXT_OPEN','stop_mode':'FIXED_1',
                'tp_pct':tp,'state':'TP_FIRST' if tp==2 else 'TIME_EXIT',
                'exit_minute':5 if tp==2 else 180})
        baseline={k:{'tp2_sl1_state':'TP_FIRST','tp2_sl1_minute':'5',
                     'tp3_sl1_state':'NO_HIT','tp3_sl1_minute':''}}
        self.assertEqual(e.verify_reference_parity(rows,baseline),2)
        rows[0]['exit_minute']=6
        with self.assertRaisesRegex(ValueError,'PARITY FAILED'):
            e.verify_reference_parity(rows,baseline)

    def test_group_stats_per_opportunity_counts_skips_as_zero(self):
        rows=[{'state':'TP_FIRST','gross_pct':3.,'net_sensitivity_pct':2.88,'gap_through_stop':0},
              {'state':'NO_ENTRY','gross_pct':0.,'net_sensitivity_pct':0.,'gap_through_stop':0}]
        stats=e.group_stats(rows,2)
        self.assertEqual(stats['entries'],1)
        self.assertEqual(stats['tp_first_per_all'],.5)
        self.assertEqual(stats['gross_pct_per_opportunity'],1.5)

    def test_end_to_end_synthetic_600_and_no_original_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/e.BASE).mkdir(parents=True)
            (root/e.V2_OUT).mkdir(parents=True)
            start=1800000000000//MIN*MIN
            # samples use distinct event keys, same simple verified shape, ten fake days not necessary
            samples=[]
            with closing(e.v1.connect(root/e.V2_DB)) as db:
                with closing(sqlite3.connect(root/e.AUTO_DB)) as ap:
                    ap.execute('CREATE TABLE events (event_key TEXT PRIMARY KEY, entry_ts INTEGER, retest_ts INTEGER, state TEXT, count INTEGER, path_blob BLOB)')
                    for i in range(600):
                        ret=start+(i+5)*MIN; known=ret+MIN
                        sym=f'EV{i}USDT';key=f'{sym}|LONG|{ret}'
                        sample={'event_key':key,'symbol':sym,'side':'LONG','retest_ts':ret,
                           'known_ts':known,'first_seen_ms':known+40000,'observation_type':'LIVE_OBSERVED',
                           'episode_id':None,'wave_minute':known//MIN,'entry_open':100.,
                           'class_label':'CONTROL_LT3','mfe_180_pct':0.,'mae_180_pct':0.,
                           'first_2m':None,'first_3m':None,'first_5m':None}
                        samples.append(sample)
                        arr=[(known+j*MIN,100.,100.,100.,100.) for j in range(360)]
                        blob=zlib.compress(b''.join(e.v1.BAR.pack(*r) for r in arr))
                        ap.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',(key,known,ret,'COMPLETE',360,blob))
                        db.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',tuple(sample[k] for k in (
                          'event_key','symbol','side','retest_ts','known_ts','first_seen_ms','observation_type',
                          'episode_id','wave_minute','entry_open','class_label','mfe_180_pct','mae_180_pct',
                          'first_2m','first_3m','first_5m')))
                        db.execute('''INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (sym,'1m',ret,100.,100.5,99.,100.,10.,1000.,5,5.,500.,'test',100))
                    ap.commit()
                    digest=e.v1.sample_hash(samples)
                    for k,v in {'purpose':'UNBALANCED_HISTORICAL_LABEL_BLIND_REPLICATION',
                                'sample_sha256':digest,'volatility_pilot_p75':'1.35'}.items():
                        db.execute('INSERT INTO meta VALUES (?,?)',(k,v))
                    db.commit()
            (root/e.V2_OUT/'audit.json').write_text(json.dumps({
                'success':True,'frozen_sample_hash':digest,'sample_count':600}))
            with (root/e.V2_OUT/'features_before_retest.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['event_key',e.v2.VOL]);writer.writeheader()
                for s in samples:writer.writerow({'event_key':s['event_key'],e.v2.VOL:1.})
            with (root/e.V2_OUT/'historical_validation_events.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['event_key','high_vol_pilot_p75','tp2_sl1_state',
                                                    'tp2_sl1_minute','tp3_sl1_state','tp3_sl1_minute'])
                writer.writeheader()
                for s in samples:writer.writerow({'event_key':s['event_key'],'high_vol_pilot_p75':0,
                        'tp2_sl1_state':'NO_HIT','tp2_sl1_minute':'',
                        'tp3_sl1_state':'NO_HIT','tp3_sl1_minute':''})
            self.assertIsNone(e.run(root,do_write=False))
            self.assertFalse((root/e.OUT).exists())
            before=(root/e.V2_DB).stat().st_size
            summary=e.run(root,do_write=True)
            self.assertEqual(summary['sample_count'],600)
            self.assertEqual(summary['reference_parity_rows'],1200)
            self.assertEqual((root/e.V2_DB).stat().st_size,before)
            with (root/e.OUT/'entry_replay_events.csv').open() as f:
                self.assertEqual(sum(1 for _ in f)-1,600*4*3*2)
            groups=summary['group_comparisons']
            ref=next(r for r in groups if r['group']=='ALL' and r['entry_mode']=='THEORETICAL_NEXT_OPEN'
                      and r['stop_mode']=='FIXED_1' and r['tp_pct']==3.)
            self.assertEqual(ref['entries'],600)
            self.assertEqual(ref['states'],{'TIME_EXIT':600})


if __name__=='__main__': unittest.main()
