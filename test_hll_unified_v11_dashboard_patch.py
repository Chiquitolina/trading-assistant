import unittest
import tempfile
import sys
from pathlib import Path

import hll_unified_v11_dashboard_patch as p
from dashboard.views.hll_unified_lifecycle_family_view_v11 import find_selected


class DashboardPatchTests(unittest.TestCase):
    def template(self):
        return ('def render():\n'
                '    if True:\n'
                '        st.plotly_chart(fig,key="hll_discovery_chart")\n'
                '        if universal_result is not None:\n'
                '            resistance_ui.render_audit(universal_result)\n'
                '        if mechanism_rows is not None:\n'
                '            pass\n')

    def test_insert_only_one_block(self):
        result=p.proposal(self.template())
        self.assertEqual(result.count(p.MARKER),1)
        self.assertTrue(result.startswith('def render():'))
        self.assertIn('resistance_ui.render_audit(universal_result)',result)
        self.assertIn('mechanism_rows is not None',result)

    def test_reject_double_apply(self):
        with self.assertRaisesRegex(ValueError,'ya figura'):
            p.proposal(p.proposal(self.template()))

    def test_reject_unknown_or_older_view(self):
        with self.assertRaisesRegex(ValueError,'ancla exacta'):
            p.proposal(self.template().replace('resistance_ui.render_audit(universal_result)',
                                              'st.warning("legacy view")'))

    def test_plan_is_read_only_and_apply_has_backup(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'discovery.py'
            original=self.template()
            target.write_text(original)
            info=p.run(target,apply=False)
            self.assertEqual(info['status'],'READY')
            self.assertEqual(target.read_text(),original)
            done=p.run(target,apply=True)
            self.assertEqual(done['status'],'APPLIED')
            self.assertEqual(Path(done['backup']).read_text(),original)
            self.assertIn(p.MARKER,target.read_text())
            with self.assertRaises(ValueError):p.run(target,apply=True)

    def test_exact_selected_identity_fails_closed(self):
        d={'by_attempt':{'x':{'attempt_id':'x','symbol':'WAXPUSDT','level_id':'L',
                            'episode_id':'E','pre_closed_ts':'1787292000000'}}}
        self.assertIsNotNone(find_selected(d,{'attempt_id':'x','symbol':'WAXPUSDT','level_id':'L','pre_closed_ts':'1787292000000'}))
        with self.assertRaises(ValueError):
            find_selected(d,{'attempt_id':'x','symbol':'WRONGUSDT'})
        with self.assertRaises(ValueError):
            find_selected(d,{'attempt_id':'x','pre_closed_ts':'1787293800000'})


if __name__=='__main__':unittest.main()
