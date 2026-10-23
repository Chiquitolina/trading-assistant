import unittest
import tempfile
from unittest.mock import Mock
from pathlib import Path
import pandas as pd

import failed_touch_causal_2pct_v1 as m

STEP=60000
BASE=1790000000000


def fixture(side='LONG',scenario='reclaim',same_touch=False):
    rows=[]
    for i in range(310):
        if side=='LONG':o,h,l,c=99.,99.2,98.8,99.
        else:o,h,l,c=101.,101.2,100.8,101.
        if i==50:
            if side=='LONG':o,h,l,c=99.7,99.8,99.3,99.4
            else:o,h,l,c=100.3,100.7,100.2,100.6
        if i==60:
            if side=='LONG':o,h,l,c=98.3,98.5,97.9,98.1
            else:o,h,l,c=101.7,102.1,101.5,101.9
            if scenario=='ambiguous':
                o,h,l,c=(98.3,100.4,96.8,100.2) if side=='LONG' else (101.7,103.2,99.6,99.8)
        if i==62 and scenario=='reclaim':
            o,h,l,c=(98.5,100.7,98.2,100.3) if side=='LONG' else (101.5,101.7,99.3,99.7)
        if i==61 and scenario=='extend':
            o,h,l,c=(98.,98.2,96.8,97.5) if side=='LONG' else (102.,103.2,101.8,102.5)
        rows.append((BASE+i*STEP,o,h,l,c,1000.))
    df=pd.DataFrame(rows,columns=['timestamp','open','high','low','close','volume'])
    ev=dict(event_key='S|15m|3x3|LONG|...',symbol='S',side=side,
      pivot_price='100',first_touch_ts=str(BASE+50*STEP),status='TOUCH_FAILED',
      canonical_check='EXACT_TOUCH',touch_penetration_pct='0.7')
    return df,ev


class Tests(unittest.TestCase):
    def test_exact_first_threshold_and_reclaim(self):
        f,e=fixture()
        rows,snaps=m.analyze_events(f,[e])
        self.assertEqual(rows[0]['depth2_delay_min'],10)
        d={int(r['checkpoint_min']):r for r in snaps}
        self.assertEqual(d[0]['checkpoint_status'],'ACTIVE')
        self.assertEqual(d[0]['structural_outcome'],'RECOVERY_FIRST')
        self.assertEqual(d[5]['checkpoint_status'],'ALREADY_RECLAIMED')
        self.assertEqual(d[0]['entry_ts'],BASE+61*STEP)

    def test_extension_first_short(self):
        f,e=fixture('SHORT','extend')
        _,snaps=m.analyze_events(f,[e])
        self.assertEqual(snaps[0]['structural_outcome'],'CONTINUATION_FIRST')
        self.assertEqual(snaps[1]['checkpoint_status'],'ALREADY_EXTENDED_3')

    def test_same_bar_collision_never_assumes_recovery(self):
        f,e=fixture('LONG','ambiguous')
        # For exact 2%-hit bar already crossed both barriers; no signal is eligible.
        _,snaps=m.analyze_events(f,[e])
        self.assertEqual(snaps[0]['checkpoint_status'],'BOTH_ALREADY_OCCURRED')
        self.assertEqual(snaps[0]['structural_outcome'],'NOT_ELIGIBLE')
        self.assertEqual(snaps[0]['entry_ts'],'')

    def test_future_mutations_cannot_change_signal_features(self):
        f,e=fixture('LONG','reclaim')
        _,one=m.analyze_events(f,[e])
        second=f.copy()
        second.loc[61:150,['open','high','low','close']]=[97,105,95,99]
        _,two=m.analyze_events(second,[e])
        protected=['signal_ts','checkpoint_status','close_depth_pct','max_depth_so_far_pct',
            'favorable_wick_share','close_location_favorable','favorable_return_5m_pct',
            'favorable_return_15m_pct','favorable_candles_5m',
            'volume_last5_over_prior30','range_5m_pct','depth2_delay_min']
        for key in protected:self.assertEqual(one[0][key],two[0][key],key)
        self.assertNotEqual(one[0]['structural_outcome'],two[0]['structural_outcome'])

    def test_no_lookahead_depth_after_180_not_eligible(self):
        f,e=fixture()
        f.loc[60,['open','high','low','close']]=[99,99.2,98.9,99]
        f.loc[231,['open','high','low','close']]=[98.3,98.5,97.9,98.1]
        census,snaps=m.analyze_events(f,[e])
        self.assertEqual(census[0]['depth2_status'],'NO_DEPTH_2_WITHIN_180')
        self.assertEqual(snaps,[])

    def test_trade_conservative_collision(self):
        f,e=fixture()
        f.loc[61,['open','high','low','close']]=[98,100.1,96.0,98]
        self.assertEqual(m.trade_path(f,61,'LONG',98)[0],'SL_AMBIGUOUS')

    def test_snapshot_plus5_is_delayed_and_causal(self):
        f,e=fixture('LONG','nothing')
        _,s=m.analyze_events(f,[e])
        self.assertTrue(all(x['checkpoint_status']=='ACTIVE' for x in s))
        self.assertEqual([x['entry_ts'] for x in s],
                         [BASE+61*STEP,BASE+66*STEP,BASE+76*STEP])
        self.assertEqual([x['signal_ts'] for x in s],
                         [BASE+60*STEP,BASE+65*STEP,BASE+75*STEP])

    def test_contiguous_only(self):
        f,e=fixture()
        f.loc[55,'timestamp']+=STEP
        with self.assertRaises(ValueError):m.analyze_events(f,[e])

    def test_duplicated_contact_counts_are_disclosed(self):
        f,e=fixture('LONG','nothing')
        second=dict(e,event_key='another-level')
        census,s=m.analyze_events(f,[e,second])
        self.assertEqual(len(census),2)
        self.assertEqual(len({r['cluster_key'] for r in census}),1)
        grouped=m.summary_rows(s)
        base=next(r for r in grouped if r['scope']=='ALL33' and r['side']=='ALL'
                  and r['checkpoint_min']==0 and r['feature']=='BASELINE')
        self.assertEqual(base['n'],2)
        self.assertEqual(base['unique_touch_clusters'],1)

    def test_wilson_and_timeout_mark_to_market(self):
        lo,hi=m.wilson(2,4)
        self.assertTrue(lo<50<hi)
        f,e=fixture('LONG','nothing')
        status,bars,gross=m.trade_path(f,61,'LONG',99)
        self.assertIn(status,('TIMEOUT','SL_FIRST','TP_FIRST'))

    def test_freeze_cohort_cannot_silently_expand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old=root/'old';study=root/'study'
            for sym in ('AAAUSDT','BBBUSDT'):
                (old/sym).mkdir(parents=True)
                (old/sym/'manifest.json').write_text('{}',encoding='utf-8')
            subs,universe,all_syms=m.frozen_universe(study,old,'AAAUSDT',create=True)
            self.assertEqual(subs,['AAAUSDT'])
            self.assertEqual(len(universe),2)
            (old/'CCCUSDT').mkdir()
            (old/'CCCUSDT'/'manifest.json').write_text('{}',encoding='utf-8')
            subs,universe,all_syms=m.frozen_universe(study,old,create=False)
            self.assertEqual(subs,['AAAUSDT','BBBUSDT'])
            self.assertEqual(len(all_syms),3)
            with self.assertRaises(ValueError):m.frozen_universe(study,old,'CCCUSDT',create=False)

    def test_process_one_end_to_end_mocked_source_reads_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cache=root/'cache';study=root/'study';old=root/'old'
            sym='AAUSDT';d=old/sym;d.mkdir(parents=True)
            df,ev=fixture('LONG','reclaim');ev['symbol']=sym
            ev['event_key']='AAUSDT|15m|3x3|LONG|foo'
            m.atomic_csv(d/'first_touches.csv',[ev],list(ev))
            m.atomic_csv(d/'failed_entries.csv',[dict(event_key=ev['event_key'],
                depth_pct='2.0',mode='DEPTH_NEXT_OPEN',
                signal_ts=str(BASE+60*STEP))],('event_key','depth_pct','mode','signal_ts'))
            (d/'manifest.json').write_text('test manifest',encoding='utf-8')
            fake=Mock()
            fake.DEFAULT_ORIGINAL=Path('unused')
            fake.verify_symbol.return_value=None
            fake.source_info.return_value=('ISOLATED_VERIFIED',df,[])
            res=m.process_one(root,cache,old,study,sym,fake,0,100)
            self.assertEqual(res['failed'],1)
            self.assertEqual(res['reached_depth2'],1)
            self.assertEqual(res['active_snapshots'],1)
            self.assertEqual(m.verify_output(study,sym)['counts']['snapshots'],3)
            summary=m.report(study,[sym])
            self.assertEqual(summary['depth2_within_180'],1)

    def test_immutable_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'AAA';p.mkdir()
            (p/'event_census.csv').write_text('header\n',encoding='utf-8')
            (p/'snapshot_features_outcomes.csv').write_text('header\n',encoding='utf-8')
            meta=dict(version=m.VERSION,symbol='AAA',input_manifest_sha256='old',
                hashes={name:m.sha(p/name) for name in ('event_census.csv','snapshot_features_outcomes.csv')})
            m.atomic_json(p/'manifest.json',meta)
            self.assertEqual(m.verify_output(root,'AAA','old')['symbol'],'AAA')
            (p/'event_census.csv').write_text('tampered\n',encoding='utf-8')
            with self.assertRaises(RuntimeError):m.verify_output(root,'AAA','old')

if __name__=='__main__':unittest.main()
