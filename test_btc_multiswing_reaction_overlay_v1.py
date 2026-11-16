"""Causal clocks, as-of band matching, source identity and read-only journal."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import btc_multiswing_reaction_overlay_v1 as rx

BASE=1_790_000_000_000
BASE-=BASE%rx.M


def event(key='BTCUSDT|LONG|1',minute=10,price=100.,close=100.2,side='LONG'):
    return dict(event_id=key,touch_key='',symbol='BTCUSDT',side=side,status='REACTION',
        retest_ts=BASE+minute*rx.M,known_ts=BASE+(minute+1)*rx.M,
        swing_price=price,retest_close=close,source='FIXTURE')


def frame(*rows):return rx.validate_events(pd.DataFrame(rows,columns=rx.COLUMNS))


def stages(*rows):
    return pd.DataFrame(rows or [dict(region_id='band',known_from=BASE,band_low=99.9,band_high=100.1)],
        columns=['region_id','known_from','band_low','band_high'])


class Clocks(unittest.TestCase):
    def test_touch_is_not_known_until_minute_close(self):
        f=frame(event());at=BASE+10*rx.M
        self.assertTrue(rx.visible_reactions(f,start=BASE,end=at,cutoff=at).empty)
        self.assertEqual(len(rx.visible_reactions(f,start=BASE,end=at+rx.M,cutoff=at+rx.M)),1)

    def test_revealed_post_is_separate_and_future_stays_hidden(self):
        f=frame(event('pre',5),event('post',11),event('future',20));cut=BASE+10*rx.M
        q=rx.visible_reactions(f,start=BASE,end=BASE+15*rx.M,cutoff=cut)
        self.assertEqual(q.event_id.tolist(),['pre','post']);self.assertEqual(q.phase.tolist(),['PRE','POST'])

    def test_invalid_causal_clock_rejected(self):
        e=event();e['known_ts']=e['retest_ts']
        with self.assertRaises(ValueError):frame(e)

    def test_duplicate_original_keys_are_not_silently_counted(self):
        with self.assertRaises(ValueError):frame(event(),event())

    def test_wrong_symbol_and_nonreaction_not_accepted(self):
        for field,value in [('symbol','ETHUSDT'),('status','TOUCH_FAILED'),('side','LOW')]:
            with self.subTest(field=field):
                e=event();e[field]=value
                with self.assertRaises(ValueError):frame(e)


class Geometry(unittest.TestCase):
    def test_uses_retest_open_not_later_confirmation_geometry(self):
        e=event(price=105.)
        s=stages(dict(region_id='b',known_from=BASE,band_low=90.,band_high=91.),
                 dict(region_id='b',known_from=e['known_ts'],band_low=104.,band_high=106.))
        q=rx.compare_bands(frame(e),s)
        self.assertFalse(q.near_band.iloc[0]);self.assertEqual(q.band_low_at_retest.iloc[0],90.)

    def test_band_born_after_touch_is_not_backdated(self):
        e=event();s=stages(dict(region_id='new',known_from=e['known_ts'],band_low=99.,band_high=101.))
        q=rx.compare_bands(frame(e),s)
        self.assertEqual(q.match_status.iloc[0],'NO_BAND_KNOWN_AT_RETEST')

    def test_duplicate_band_matches_keep_one_reaction_identity(self):
        s=stages(dict(region_id='a',known_from=BASE,band_low=99.,band_high=101.),
                 dict(region_id='b',known_from=BASE,band_low=99.5,band_high=100.5))
        q=rx.compare_bands(frame(event()),s)
        self.assertEqual(len(q),1);self.assertEqual(q.matched_band_count.iloc[0],2)
        self.assertEqual(q.matched_region_ids.iloc[0],'a,b')

    def test_selected_band_scope_and_zero_tolerance(self):
        s=stages(dict(region_id='a',known_from=BASE,band_low=99.,band_high=101.),
                 dict(region_id='b',known_from=BASE,band_low=200.,band_high=201.))
        q=rx.compare_bands(frame(event()),s,region_ids=['b'],tolerance_pct=0)
        self.assertFalse(q.near_band.iloc[0]);self.assertEqual(q.nearest_region_id.iloc[0],'b')

    def test_tolerance_is_outside_distance_not_zone_inflation(self):
        e=event(price=100.15)
        self.assertFalse(rx.compare_bands(frame(e),stages(),tolerance_pct=0).near_band.iloc[0])
        self.assertTrue(rx.compare_bands(frame(e),stages(),tolerance_pct=.10).near_band.iloc[0])

    def test_missing_swing_price_is_unknown_not_negative_confluence(self):
        q=rx.compare_bands(frame(event(price=None)),stages())
        self.assertEqual(q.match_status.iloc[0],'SWING_PRICE_UNAVAILABLE')
        self.assertFalse(q.near_band.iloc[0])

    def test_inputs_are_not_mutated(self):
        f=frame(event());s=stages();f0=f.copy(deep=True);s0=s.copy(deep=True)
        rx.compare_bands(f,s);pd.testing.assert_frame_equal(f,f0);pd.testing.assert_frame_equal(s,s0)


class Journal(unittest.TestCase):
    def test_retest_in_warmup_can_become_known_exactly_at_visible_start(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'journal.sqlite3';con=sqlite3.connect(p)
            con.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,known_ts INTEGER,causal_json TEXT)')
            con.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?)',
                ('boundary','BTCUSDT','LONG','REACTION',BASE-rx.M,BASE,'{"swing_price":100}'))
            con.commit();con.close()
            minute=pd.DataFrame([dict(timestamp=BASE-rx.M,close=100.2)])
            q=rx.read_journal(p,minute,start=BASE,end=BASE+rx.M)
            self.assertEqual(q.event_id.tolist(),['boundary'])

    def test_readonly_identity_causal_price_and_ignore_outcome_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'journal.sqlite3';con=sqlite3.connect(p)
            con.execute('CREATE TABLE first_touches(touch_key TEXT,symbol TEXT,side TEXT,status TEXT,retest_ts INTEGER,known_ts INTEGER,causal_json TEXT,outcome_json TEXT)')
            for symbol,status,key in [('BTCUSDT','REACTION','original'),('ETHUSDT','REACTION','other'),('BTCUSDT','TOUCH_FAILED','failed')]:
                con.execute('INSERT INTO first_touches VALUES(?,?,?,?,?,?,?,?)',(key,symbol,'LONG',status,BASE,BASE+rx.M,
                    json.dumps(dict(swing_price=100.,future_outcome='DO_NOT_EXPORT')),'DO_NOT_READ'))
            con.commit();con.close();before=hashlib.sha256(p.read_bytes()).hexdigest()
            minute=pd.DataFrame([dict(timestamp=BASE,close=100.2)])
            q=rx.read_journal(p,minute,start=BASE,end=BASE+rx.M)
            self.assertEqual(q.event_id.tolist(),['original']);self.assertEqual(q.touch_key.tolist(),['original'])
            self.assertEqual(q.swing_price.iloc[0],100.);self.assertEqual(q.retest_close.iloc[0],100.2)
            self.assertNotIn('DO_NOT',q.to_csv(index=False))
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),before)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'absent.sqlite3'
            with self.assertRaises(FileNotFoundError):rx.read_journal(p,pd.DataFrame(),start=BASE,end=BASE+rx.M)
            self.assertFalse(p.exists())


class Drawing(unittest.TestCase):
    def test_only_appends_markers_and_positions_at_known_time(self):
        q=rx.compare_bands(rx.visible_reactions(frame(event()),start=BASE,end=BASE+20*rx.M,cutoff=BASE+20*rx.M),stages())
        fig=dict(data=[dict(type='candlestick',x=['original'],close=[100.])],layout=dict(title='unchanged'))
        original=copy.deepcopy(fig);rx.add_to_figure(fig,dict(events=q),start=BASE,end=BASE+20*rx.M)
        self.assertEqual(fig['data'][0],original['data'][0]);self.assertEqual(fig['layout'],original['layout'])
        self.assertEqual(len(fig['data']),2)
        self.assertEqual(pd.Timestamp(fig['data'][1]['x'][0]).value//1_000_000,BASE+11*rx.M)

    def test_marker_end_excludes_future_even_if_input_contains_it(self):
        q=rx.compare_bands(rx.visible_reactions(frame(event('past',1),event('future',20)),start=BASE,end=BASE+30*rx.M,cutoff=BASE+30*rx.M),stages())
        traces=rx.marker_traces(q,start=BASE,end=BASE+10*rx.M)
        self.assertEqual(sum(len(t['x']) for t in traces),1);self.assertNotIn('future',str(traces))

    def test_disabled_overlay_never_loads_sources(self):
        class St:
            def checkbox(self,*a,**k):return False
        with patch.object(rx,'load_reactions',side_effect=AssertionError('must not load')):
            self.assertIsNone(rx.render_overlay(St(),'.',stages=stages(),region_ids=['band'],
                cutoff=BASE,start=BASE,end=BASE,prefix='test_'))

    def test_table_and_download_never_receive_future_rows(self):
        class St:
            def __init__(self):self.tables=[];self.downloads=[]
            def checkbox(self,label,**kw):return label.startswith('Mostrar REACTIONs')
            def selectbox(self,label,options,**kw):return list(options)[kw.get('index',0)]
            def multiselect(self,*a,**kw):return ['LONG','SHORT']
            def button(self,*a,**kw):return False
            def caption(self,*a,**kw):pass
            def info(self,*a,**kw):raise AssertionError(a)
            def expander(self,*a,**kw):return nullcontext()
            def dataframe(self,x,**kw):self.tables.append(x.copy())
            def download_button(self,label,data,**kw):self.downloads.append(data)
        st=St();f=frame(event('visible',1),event('FUTURE_DO_NOT_LEAK',20))
        with patch.object(rx,'load_reactions',return_value=(f,dict(label='fixture',coverage_start=BASE,coverage_end=BASE+30*rx.M))):
            overlay=rx.render_overlay(st,'.',stages=stages(),region_ids=['band'],cutoff=BASE+10*rx.M,
                start=BASE,end=BASE+10*rx.M,prefix='test_')
        rx.render_table(st,overlay)
        self.assertEqual(len(st.tables[0]),1);self.assertNotIn('FUTURE_DO_NOT_LEAK',st.downloads[0].decode('utf-8-sig'))


if __name__=='__main__':unittest.main()
