"""Contrato BTC Multi-Swing 60D: as-of, identidad fisica, OHLC y aislamiento."""
import csv
import hashlib
import json
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from btc_multiswing_60d_explorer_v1 import (
    DAY, M, TF_MINUTES, aggregate_closed, asof_rows, chart_window,
    group_asof, iso, load_snapshot, ms, read_snapshot, selected_inspection_window,
    window_evidence_stats,
    FREEZE_REL, CATALOG_REL,
)
from dashboard.views.reaction_multiswing_btc60d_view_v1 import build_figure
from dashboard.views.reaction_continuous_multiswing_view import render_continuous_multiswing_view

START = ms('2026-09-09T01:00:00Z')
END = ms('2026-11-08T01:00:00Z')


def catalog_frame():
    data = [
        # same physical 1m extreme, three representations, later confirmation
        dict(pivot_id='A',timeframe='1m',side='LOW',price=99.0,pivot_ts=START+M,
             available_ts=START+7*M,physical_id='P1',physical_minute_ts=START+M,
             physical_identity_state='VERIFIED'),
        dict(pivot_id='B',timeframe='5m',side='LOW',price=99.0,pivot_ts=START,
             available_ts=START+36*M,physical_id='P1',physical_minute_ts=START+M,
             physical_identity_state='VERIFIED'),
        dict(pivot_id='C',timeframe='15m',side='LOW',price=99.0,pivot_ts=START-M*60,
             available_ts=START+70*M,physical_id='P1',physical_minute_ts=START+M,
             physical_identity_state='VERIFIED'),
        # physically independent low, same-ish price
        dict(pivot_id='D',timeframe='1m',side='LOW',price=99.1,pivot_ts=START+10*M,
             available_ts=START+16*M,physical_id='P2',physical_minute_ts=START+10*M,
             physical_identity_state='VERIFIED'),
        # tied extremum cannot be assigned to known physical group
        dict(pivot_id='E',timeframe='30m',side='LOW',price=99.0,pivot_ts=START+M*30,
             available_ts=START+150*M,physical_id='',physical_minute_ts=float('nan'),
             physical_identity_state='TIED_EXTREMA'),
    ]
    return pd.DataFrame(data)


def bars(start, n):
    return pd.DataFrame({
        'timestamp':[start+i*M for i in range(n)],
        'open':[100.]*n,'high':[102.]*n,'low':[98.]*n,'close':[100.]*n,
    })


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MultiSwingExplorerV1(unittest.TestCase):
    def test_stats_count_only_pivots_inside_window(self):
        # P1 tiene dos TF confirmados en el corte, P2 uno, E es ambiguo.
        known=asof_rows(catalog_frame(),START+200*M,tuple(TF_MINUTES))
        # Recorte incluye P2 y E, pero excluye P1 (fisico +1m).
        stats=window_evidence_stats(known,START+5*M,START+40*M)
        self.assertEqual(stats,{
            'representations':2, 'verified_extrema':1,
            'multi_tf_extrema':0, 'ambiguous_representations':1,
        })
        # Acercar la ventana a P1 muestra 3 representaciones, un solo fisico.
        stats=window_evidence_stats(known,START,START+5*M)
        self.assertEqual(stats['representations'],3)
        self.assertEqual(stats['verified_extrema'],1)
        self.assertEqual(stats['multi_tf_extrema'],1)

    def test_stats_respect_confirmation_and_multi_only(self):
        # La tercera TF de P1 se confirma recién en +70m.
        early=asof_rows(catalog_frame(),START+40*M,tuple(TF_MINUTES))
        stats=window_evidence_stats(early,START,START+40*M,multi_only=True)
        self.assertEqual(stats,{
            'representations':2,'verified_extrema':1,
            'multi_tf_extrema':1,'ambiguous_representations':0,
        })
        late=asof_rows(catalog_frame(),START+200*M,tuple(TF_MINUTES))
        stats=window_evidence_stats(late,START,START+200*M,multi_only=True)
        self.assertEqual(stats['representations'],3)
        self.assertEqual(stats['verified_extrema'],1)
        self.assertEqual(stats['ambiguous_representations'],0)

    def test_window_stats_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'INVALID_EVIDENCE_WINDOW'):
            window_evidence_stats(pd.DataFrame(),START,START)

    def test_multiple_tfs_same_physical_only_one_identity(self):
        source=catalog_frame()
        pre=asof_rows(source,START+40*M,('1m','5m','15m','30m'))
        grouped=group_asof(pre,collapse=True)
        self.assertEqual(len(grouped),2)
        p=grouped[grouped.group_key=='P1'].iloc[0]
        self.assertEqual(p.representations_asof,2)
        self.assertEqual(p.tf_set_asof,'1m,5m')
        self.assertEqual(int(p.first_available_asof),START+7*M)
        self.assertEqual(int(p.last_available_asof),START+36*M)

    def test_new_confirmation_only_known_after_its_close(self):
        source=catalog_frame()
        early=group_asof(asof_rows(source,START+69*M,tuple(TF_MINUTES)),collapse=True)
        later=group_asof(asof_rows(source,START+70*M,tuple(TF_MINUTES)),collapse=True)
        self.assertEqual(early[early.group_key=='P1'].iloc[0].representations_asof,2)
        self.assertEqual(later[later.group_key=='P1'].iloc[0].representations_asof,3)

    def test_tied_extremum_is_not_silently_merged(self):
        data=group_asof(asof_rows(catalog_frame(),START+200*M,tuple(TF_MINUTES)),collapse=True)
        self.assertIn('E',set(data.group_key))
        self.assertEqual(len(data),3)
        self.assertEqual(len(data[data.physical_identity_state=='TIED_EXTREMA']),1)

    def test_distinct_independent_extrema_not_merged_by_price(self):
        data=group_asof(asof_rows(catalog_frame(),START+200*M,tuple(TF_MINUTES)),collapse=True)
        self.assertIn('P1',set(data.group_key))
        self.assertIn('P2',set(data.group_key))

    def test_only_multitf_filters_real_verified_groups(self):
        data=group_asof(asof_rows(catalog_frame(),START+200*M,tuple(TF_MINUTES)),
                        collapse=True,multi_only=True)
        self.assertEqual(data.group_key.tolist(),['P1'])

    def test_tf_selection_and_wrong_side(self):
        source=catalog_frame()
        self.assertEqual(len(asof_rows(source,START+200*M,('1m',),'HIGH')),0)
        self.assertEqual(set(asof_rows(source,START+200*M,('1m',),'LOW').pivot_id),{'A','D'})
        with self.assertRaisesRegex(ValueError,'INVALID_SIDE'):
            asof_rows(source,START+200*M,('1m',),'SHORT')

    def test_group_conflict_fails_closed(self):
        source=catalog_frame()
        source.loc[source.pivot_id=='B','price']=110.
        with self.assertRaisesRegex(ValueError,'PHYSICAL_GROUP_CONFLICT'):
            group_asof(asof_rows(source,START+200*M,tuple(TF_MINUTES)),collapse=True)

    def test_render_4h_drops_final_partial_bar(self):
        # Covers 3 full 4h candles plus 1h opened remainder.
        data=bars(0,13*60)
        full=aggregate_closed(data,'4h',0,13*60*M)
        self.assertEqual(len(full),3)
        self.assertEqual(full.timestamp.tolist(),[0,4*60*M,8*60*M])

    def test_4h_no_late_candle_at_pre_cutoff(self):
        data=bars(0,16*60)
        p=aggregate_closed(data,'4h',0,9*60*M)
        self.assertEqual(p.timestamp.tolist(),[0,4*60*M])
        # No open 08:00–12:00 bar allowed at 09:00.

    def test_partial_left_boundary_allowed_only_with_warmup(self):
        data=bars(0,240*3)
        frame=aggregate_closed(data,'4h',M*30,240*3*M)
        self.assertEqual(len(frame),3)
        self.assertEqual(frame.timestamp.iloc[0],0)

    def test_invalid_chart_interval(self):
        with self.assertRaisesRegex(ValueError,'INVALID_CHART_INTERVAL'):
            aggregate_closed(bars(0,10),'1m',10*M,10*M)

    def test_inspection_before_after_and_selected_pivot(self):
        record=pd.Series({'plot_ts':START+M,'last_available_asof':START+14*60*M})
        a,b,cut=selected_inspection_window(record,2,4,START,END,False)
        self.assertEqual(b,cut)
        self.assertLessEqual(a,START+M)
        a,b,cut=selected_inspection_window(record,2,4,START,END,True)
        self.assertEqual(b,cut+4*60*M)

    def test_chart_window_no_future(self):
        a,b=chart_window(START+DAY,START,7)
        self.assertEqual((a,b),(START,START+DAY))

    def test_plot_candles_and_asof_markers(self):
        frame=bars(START,10)
        pre=group_asof(asof_rows(catalog_frame(),START+8*M,('1m',)),collapse=True)
        fig=build_figure(frame,pre,display_tf='1m')
        self.assertEqual(fig.data[0].type,'candlestick')
        self.assertEqual(len(fig.data),2)  # OHLC and one already-known physical LOW
        self.assertEqual(len(fig.data[1].x),1)

    def test_plot_post_vline_does_not_change_markers(self):
        frame=bars(START,10)
        pre=group_asof(asof_rows(catalog_frame(),START+8*M,('1m',)),collapse=True)
        fig=build_figure(frame,pre,display_tf='1m',cutoff=START+8*M,chart_end=START+10*M)
        self.assertEqual(len(fig.layout.shapes),1)
        self.assertEqual(len(fig.data[1].x),1)

    def test_frozen_missing_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                read_snapshot(Path(tmp))

    def test_missing_source_sha_fails_closed(self):
        # Avoid writing 115260 rows: test manifest validation before file reads.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for folder in (root/FREEZE_REL,root/CATALOG_REL): folder.mkdir(parents=True)
            (root/FREEZE_REL/'manifest.json').write_text(json.dumps({
                'integrity':'CONTIGUOUS_1M_VERIFIED',
                'hll_30m_parity':{'status':'PARITY_OK'},
                'files_sha256':{'BTCUSDT_1m.csv':'A','BTCUSDT_1m_warmup.csv':'B'}
            }))
            (root/CATALOG_REL/'manifest.json').write_text(json.dumps({
                'version':'btc-multiswing-60d-catalog-v1.1-6tf',
                'source_csv_sha256':'MISMATCH','source_warmup_sha256':'B'}
            ))
            with self.assertRaisesRegex(ValueError,'CATALOG_FREEZE_SOURCE_MISMATCH'):
                read_snapshot(root)

    def test_integration_keeps_original_continuous_unmodified(self):
        path=Path(__file__).parent/'dashboard/views/reaction_continuous_multiswing_view.py'
        s=path.read_text(encoding='utf8')
        self.assertIn("'Evento REACTION · Continuous'",s)
        self.assertIn("'Mapa BTC 60D · 6 TF'",s)
        self.assertIn('render_btc_multiswing_60d_view(project)',s)
        self.assertIn('read_frozen_event(project,event_key,known)',s)
        self.assertNotIn('import hll',Path(__file__).parent.joinpath('btc_multiswing_60d_explorer_v1.py').read_text())

    def test_dashboard_render_smoke_without_streamlit_or_disk(self):
        # Contract of the new Streamlit view with synthetic already-verified frames.
        import datetime as _dt
        from dashboard.views.reaction_multiswing_btc60d_view_v1 import render_btc_multiswing_60d_view
        class FakeUI(types.ModuleType):
            def __init__(self):
                super().__init__('streamlit')
                self.figures=[];self.errors=[];self.metrics=[]
            def __enter__(self): return self
            def __exit__(self,*a): return False
            def markdown(self,*a,**k): pass
            def caption(self,*a,**k): pass
            def columns(self,n): return [self]*(n if isinstance(n,int) else len(n))
            def date_input(self,*a,**k): return k['value']
            def selectbox(self,label,options,**k):
                return options[k.get('index',0)]
            def multiselect(self,*a,**k): return ['1m','5m','15m','30m','1h','4h']
            def checkbox(self,*a,**k): return k.get('value',False)
            def metric(self,*a,**k): self.metrics.append(a)
            def info(self,*a,**k): pass
            def warning(self,*a,**k): self.errors.append(a)
            def error(self,*a,**k): self.errors.append(a)
            def dataframe(self,*a,**k): pass
            def expander(self,*a,**k): return self
            def json(self,*a,**k): pass
            def plotly_chart(self,figure,**k): self.figures.append(figure)
        fake=FakeUI()
        final=END
        data=bars(final-2*DAY,2880)
        source=catalog_frame().copy()
        # The cutoff is Nov 8 01 UTC; produce actual recent pivots.
        source['pivot_ts'] += (final-60*M-START)
        source['available_ts'] += (final-60*M-START)
        source['physical_minute_ts'] += (final-60*M-START)
        with patch.dict(sys.modules,{'streamlit':fake}), patch(
            'dashboard.views.reaction_multiswing_btc60d_view_v1.load_snapshot',
            return_value=(data,source,{'start':START,'end':END,'source_sha256':'x',
                     'catalog_sha256':'y','source_hll_parity':'PARITY_OK'})
        ):
            render_btc_multiswing_60d_view(Path('/tmp/offline'))
        self.assertFalse(fake.errors)
        self.assertEqual(len(fake.figures),1)
        self.assertEqual(fake.figures[0].data[0].type,'candlestick')
        self.assertEqual(len(fake.metrics),4)
        self.assertTrue(all('ventana' in args[0] for args in fake.metrics))

    def test_route_new_view_without_loading_event_journal(self):
        mockui=types.ModuleType('streamlit')
        mockui.selectbox=lambda *args,**kw:'Mapa BTC 60D · 6 TF'
        with patch.dict(sys.modules,{'streamlit':mockui}), patch(
            'dashboard.views.reaction_multiswing_btc60d_view_v1.render_btc_multiswing_60d_view'
        ) as target:
            render_continuous_multiswing_view(Path('/tmp/nojournal'))
            target.assert_called_once_with(Path('/tmp/nojournal'))


if __name__=='__main__': unittest.main()
