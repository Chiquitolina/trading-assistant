"""No-network regression checks for 240/300/360m First Touch + dashboard."""
import ast
import importlib.util
from pathlib import Path
import unittest

BASE=1_800_000_000_000
MIN=60_000
HERE=Path(__file__).parent


def module_at(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ui_func(name):
    p=HERE/'dashboard/views/reaction_microstructure_quality_view.py'
    tree=ast.parse(p.read_text(encoding='utf-8'))
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    mod=ast.Module(body=[fn],type_ignores=[])
    namespace={}
    exec(compile(ast.fix_missing_locations(mod),str(p),'exec'),namespace)
    return namespace[name]


class ExtendedHorizonsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab=module_at(HERE/'reaction_first_touch_lab.py','first_touch_horizon_under_test')

    def bars(self, hit_at=None, price=101.1):
        data=[]
        for i in range(360):
            hi=price if i==hit_at else 100.1
            data.append((BASE+i*MIN,100.,hi,99.9,100.))
        return data

    def test_horizons_exact_and_last_minute(self):
        self.assertEqual(self.lab.HORIZONS,(5,15,30,60,180,240,300,360))
        cases=((179,180),(239,240),(299,300),(359,360))
        for minute,hit_horizon in cases:
            with self.subTest(hit_horizon=hit_horizon):
                b=self.bars(hit_at=minute)
                self.assertEqual(self.lab.first_touch(b,0,hit_horizon,'LONG',100,1,.5),
                                 ('TP_FIRST',minute+1))
                self.assertEqual(self.lab.first_touch(b,0,hit_horizon-1,'LONG',100,1,.5),
                                 ('NO_HIT',None))

    def test_no_forward_results_after_earlier_stop(self):
        b=self.bars(hit_at=350)
        b[195]=(b[195][0],100.,100.1,99.,100.)
        for h in (240,300,360):
            self.assertEqual(self.lab.first_touch(b,0,h,'LONG',100,1,.5),('SL_FIRST',196))

    def test_delayed_observed_open_after_180_can_trade_later(self):
        b=self.bars(hit_at=219)
        e={'observation_type':'LIVE_OBSERVED','first_seen_ms':BASE+200*MIN,
           'known_ts':BASE+MIN}
        idx=self.lab.observed_index(b,e)
        self.assertEqual(idx,200)
        self.assertEqual(self.lab.first_touch(b,idx,180,'LONG',100,1,.5),('NOT_ENTERED',None))
        self.assertEqual(self.lab.first_touch(b,idx,240,'LONG',100,1,.5),('TP_FIRST',220))

    def test_180m_output_is_unchanged_for_normal_entries(self):
        b=self.bars(hit_at=200)
        e={'observation_type':'LIVE_OBSERVED','first_seen_ms':BASE+2*MIN,
           'known_ts':BASE+MIN}
        idx=self.lab.observed_index(b,e)
        self.assertEqual(idx,2)
        row=self.lab.row_for({'event_key':'x','symbol':'XUSDT','side':'LONG','retest_ts':BASE,
                              'observation_type':'LIVE_OBSERVED'},b,'OBSERVED_OPEN',idx,1,.5)
        self.assertEqual(row['result_180m'],'NO_HIT')
        self.assertEqual(row['result_240m'],'TP_FIRST')
        self.assertEqual(row['first_touch_result_180m'],'NO_HIT')
        self.assertEqual(row['minute_240m'],201)
        self.assertIn('result_360m',self.lab.ROW_FIELDS)
        self.assertIn('minute_300m',self.lab.ROW_FIELDS)

    def test_dashboard_does_not_offer_uncomputed_horizons(self):
        class MockCols:
            def __init__(self,columns): self.columns=columns
        available=ui_func('available_first_touch_horizons')
        old=MockCols(['result_%dm'%h for h in (5,15,30,60,180)]+
                     ['minute_%dm'%h for h in (5,15,30,60,180)])
        self.assertEqual(available(old),[5,15,30,60,180])
        new=MockCols(list(self.lab.ROW_FIELDS))
        self.assertEqual(available(new),[5,15,30,60,180,240,300,360])
        broken=MockCols(['result_300m','minute_240m'])
        self.assertEqual(available(broken),[])

    def test_existing_compatibility_adapter_is_preserved(self):
        s=(HERE/'dashboard/views/reaction_microstructure_quality_view.py').read_text()
        self.assertIn('def _render_compatible_research_tab(',s)
        self.assertIn('with tabs[10]:',s)
        self.assertIn('render_horizontal_level_rich_card(',s)


if __name__=='__main__': unittest.main()
