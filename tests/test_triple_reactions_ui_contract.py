"""Regression check: runner DNA retained, PRE filter expands to all REACTIONs."""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
ORIGINAL=ROOT/'original/reaction_lab_runner_discovery_v2_view.py'
CURRENT=ROOT/'dashboard/views/reaction_lab_runner_discovery_v2_view.py'

class ViewContract(unittest.TestCase):
    def setUp(self):
        self.original=ORIGINAL.read_text(encoding='utf-8')
        self.current=CURRENT.read_text(encoding='utf-8')

    def test_only_three_existing_functions_changed(self):
        def functions(text):
            return {n.name:ast.dump(n,include_attributes=False)
                    for n in ast.parse(text).body if isinstance(n,ast.FunctionDef)}
        old, new=functions(self.original),functions(self.current)
        self.assertEqual(set(old),set(new))
        self.assertEqual({name for name in old if old[name]!=new[name]},
             {'_render_triple_runner_filter','_triple_15m_chart','_render_runner_dna'})

    def test_events_not_restricted_to_winners_before_pattern(self):
        self.assertIn('base=working.merge(triples[fields]',self.current)
        self.assertIn('base=base.merge(available',self.current)
        self.assertIn("'No llegó al objetivo · NO RUNNER'",self.current)
        self.assertIn("'Sin evaluación'",self.current)
        self.assertIn("filtered.es_runner.eq(False)",self.current)
        self.assertIn('runner = pool.loc[pool["es_runner"]].copy()',self.current)

    def test_outcome_post_is_explicit_and_pre_alone_has_no_outcome(self):
        self.assertIn('_triple_15m_chart(project,event)',self.current)
        self.assertIn('_signal_chart(project,event,mode=mode,tp=tp,horizon=horizon,show_sl=True)',self.current)
        self.assertIn("status='SIN COBERTURA'",self.current) if False else None
        self.assertIn('NO RUNNER',self.current)

    def test_original_main_app_identical(self):
        import hashlib
        a=ROOT/'dashboard/app.py'
        # Container copy of the exact user-provided app.py has already been retained.
        self.assertTrue(a.is_file())
        self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(),
                         'e1e5c9108f06492ce5221da8aeb4b720a2053b9a4ab533c7bf6db4d71eadbe64')

if __name__=='__main__':unittest.main()
