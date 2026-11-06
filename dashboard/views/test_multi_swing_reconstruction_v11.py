"""Regresion V1.1: corte PRE-TOUCH, cobertura y extremos fisicos."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from multi_swing_reconstruction_v11 import compute_reconstruction
from reaction_multi_swing_quality_dataset_v11 import (
    run_cohort_v11,make_v11_row,write_run_v11,
)
from test_reaction_multi_swing_quality_dataset_v1 import setup_sqlite

M=60_000


def fixture():
    # 500 minutos de histórico continuo. Tres mínimos físicos diferentes
    # próximos al soporte: 180m, 195m, 240m. No son tres TF del mismo minuto.
    lows={180:100.04,195:100.03,240:100.0}
    minute=[{'timestamp':i*M,'open':103.,'high':104.,
             'low':lows.get(i,101.0),'close':103.} for i in range(300)]
    def pivot(tf, side, time, available, price):
        return {'timeframe':tf,'side':side,'pivot_ts':time*M,
                'available_ts':available*M,'price':price}
    event={'symbol':'XUSDT','side':'LONG','status':'REACTION',
           'pivot_ts':240*M,'actionable_ts':285*M,
           'retest_ts':300*M,'known_ts':301*M,'swing_price':100.0}
    points=[pivot('15m','LOW',240,300,100.0),
            pivot('1m','LOW',240,246,100.0),
            pivot('5m','LOW',240,270,100.0),
            pivot('5m','LOW',180,210,100.04),
            pivot('5m','LOW',195,225,100.03),
            pivot('5m','HIGH',190,220,104.0),
            pivot('5m','HIGH',220,250,103.0)]
    stamps={tf:list(range(0,300*M,dt*M))
            for tf,dt in [('1m',1),('5m',5),('15m',15)]}
    return event,points,minute,stamps


class ReconstructionV11(unittest.TestCase):
    def test_counts_independent_extrema_not_timeframes(self):
        event,points,minute,stamps=fixture()
        r=compute_reconstruction(event,points,minute,stamps)
        self.assertEqual(r['w120_near_zone_unique_extrema'],3)
        self.assertEqual(r['w120_near_zone_shared_tf_extrema'],1)
        self.assertEqual(r['w120_near_zone_independent_extrema'],2)
        self.assertEqual(r['w60_near_zone_unique_extrema'],1)
        self.assertEqual(r['w30_near_zone_unique_extrema'],0)
        self.assertEqual(r['w120_opposing_5m_count'],2)
        self.assertEqual(r['w120_opposing_pressure_step_fraction'],1.0)
        self.assertEqual(r['structural_cutoff_ts'],event['retest_ts'])

    def test_retest_bar_and_future_changes_cannot_change_pre(self):
        e,p,mins,ts=fixture()
        first=compute_reconstruction(e,p,mins,ts)
        mins.append({'timestamp':300*M,'open':103,'high':900,'low':0.01,'close':103})
        mins.append({'timestamp':301*M,'open':103,'high':900,'low':0.01,'close':103})
        p.append({'timeframe':'1m','side':'LOW','pivot_ts':299*M,
                  'available_ts':305*M,'price':1.0})
        self.assertEqual(first,compute_reconstruction(e,p,mins,ts))

    def test_gaps_fail_closed_not_zero(self):
        e,p,mins,ts=fixture()
        ts['1m'].remove(180*M)
        result=compute_reconstruction(e,p,mins,ts)
        # 1m OHLC existe en mins, pero faltó en lista de timeframes;
        # índice verificado de OHLC es la autoridad para 1m.
        self.assertEqual(result['w120_coverage_state'],'COVERED')
        mins[:]=[b for b in mins if b['timestamp']!=180*M]
        result=compute_reconstruction(e,p,mins,ts)
        self.assertEqual(result['w120_coverage_state'],'NOT_COVERED')
        self.assertIsNone(result['w120_near_zone_unique_extrema'])
        self.assertEqual(result['w30_coverage_state'],'COVERED')

    def test_missing_warmup_not_zero(self):
        e,p,mins,ts=fixture()
        ts['5m']=[x for x in ts['5m'] if x>=120*M]
        r=compute_reconstruction(e,p,mins,ts)
        self.assertEqual(r['w240_coverage_state'],'NOT_COVERED')
        self.assertIsNone(r['w240_near_zone_unique_extrema'])
        self.assertEqual(r['w30_coverage_state'],'COVERED')

    def test_ambiguous_physical_identity_not_partial_count(self):
        e,p,mins,ts=fixture()
        mins[241]['low']=100.0  # mismo LOW dentro vela 15m de la ancla
        r=compute_reconstruction(e,p,mins,ts)
        self.assertEqual(r['w120_coverage_state'],'COVERED')
        self.assertGreater(r['w120_near_zone_unknown_identity'],0)
        self.assertIsNone(r['w120_near_zone_unique_extrema'])

    def test_not_confirmed_or_after_cutoff_excluded(self):
        e,p,mins,ts=fixture()
        baseline=compute_reconstruction(e,p,mins,ts)
        p.append({'timeframe':'1m','side':'LOW','pivot_ts':265*M,
                  'available_ts':302*M,'price':100.02})
        self.assertEqual(baseline,compute_reconstruction(e,p,mins,ts))

    def test_short_high_symmetric_independent_extrema(self):
        e,p,mins,ts=fixture()
        e=dict(e,side='SHORT',swing_price=104.0)
        # HIGHs verificados en minutos diferentes con precio proximo.
        for row in mins:
            row['low']=100.0
            row['high']=103.0
        mins[180]['high']=104.04
        mins[195]['high']=104.03
        mins[240]['high']=104.0
        p=[{'timeframe':'15m','side':'HIGH','pivot_ts':240*M,
            'available_ts':300*M,'price':104.0},
           {'timeframe':'5m','side':'HIGH','pivot_ts':180*M,
            'available_ts':210*M,'price':104.04},
           {'timeframe':'5m','side':'HIGH','pivot_ts':195*M,
            'available_ts':225*M,'price':104.03}]
        result=compute_reconstruction(e,p,mins,ts)
        self.assertEqual(result['w120_near_zone_unique_extrema'],3)
        self.assertEqual(result['w120_near_zone_independent_extrema'],2)

    def test_unsafe_original_anchor_refused(self):
        e,p,mins,ts=fixture()
        e=dict(e,retest_ts=290*M,known_ts=291*M)
        with self.assertRaisesRegex(ValueError,'ANCLA_15M_NO_CONFIRMADA'):
            compute_reconstruction(e,p,mins,ts)

    def test_early_5m_confirmation_rejected(self):
        e,p,mins,ts=fixture()
        p.append({'timeframe':'5m','side':'LOW','pivot_ts':265*M,
                  'available_ts':275*M,'price':100.01})
        with self.assertRaisesRegex(ValueError,'barras derechas'):
            compute_reconstruction(e,p,mins,ts)

    def test_run_real_adapter_with_synthetic_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup_sqlite(tmp,include_missing=True)
            rows,meta=run_cohort_v11(Path(tmp),status='ALL',limit=2)
            self.assertEqual(len(rows),2)
            by={r['touch_key']:r for r in rows}
            self.assertEqual(by['touch-B']['v11_state'],'NOT_COVERED')
            self.assertIn(by['touch-A']['v11_state'],('V11_PARTIAL','V11_NOT_COVERED'))
            self.assertEqual(by['touch-A']['original_status'],'REACTION')
            self.assertEqual(by['touch-B']['original_status'],'TOUCH_INDECISIVE')
            file=write_run_v11(rows,meta,Path(tmp)/'v11_report',code_root=Path(__file__).parent)
            self.assertTrue(file.exists())
            self.assertTrue((file.parent/'manifest.json').exists())
            with self.assertRaises(FileExistsError):
                write_run_v11(rows,meta,file.parent,code_root=Path(__file__).parent)

if __name__ == '__main__': unittest.main()
