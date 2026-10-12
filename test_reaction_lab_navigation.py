"""Regression guards: journal never blocks five original Reaction & Swing Lab views."""
import ast
import unittest
from pathlib import Path

APP = Path(__file__).parent / 'dashboard/app.py'
MODES = (
    '📡 REACTION journal',
    '🧬 Candidate Research',
    '🔁 REACTION / Retests',
    '🎯 Pivots / Confirmations',
    '📍 Event swing structure',
    '🧬 Volume → future swings',
)

class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text(encoding='utf-8')
        cls.tree = ast.parse(cls.src)
        cls.lab = next(n for n in ast.walk(cls.tree)
                       if isinstance(n, ast.If)
                       and isinstance(n.test, ast.Compare)
                       and isinstance(n.test.left, ast.Name)
                       and n.test.left.id == 'selected_section'
                       and any(isinstance(v, ast.Constant) and v.value == 'reaction_swing_lab' for v in n.test.comparators))

    def test_every_original_lab_mode_remains_in_radio(self):
        radio = next(n for n in ast.walk(self.lab)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == 'radio'
                     and n.args and isinstance(n.args[0], ast.Constant)
                     and n.args[0].value == 'Research view')
        options = next(kw.value for kw in radio.keywords if kw.arg == 'options')
        self.assertEqual(tuple(v.value for v in options.elts), MODES)

    def test_no_stop_in_cohort_branch(self):
        journal_branch = next(n for n in ast.walk(self.lab)
                              if isinstance(n, ast.If)
                              and isinstance(n.test, ast.Compare)
                              and isinstance(n.test.left, ast.Name)
                              and n.test.left.id == 'lab_mode'
                              and any(isinstance(v, ast.Constant) and v.value == MODES[0] for v in n.test.comparators))
        self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                             and isinstance(n.func.value, ast.Name) and n.func.value.id=='st'
                             and n.func.attr=='stop'
                             for statement in journal_branch.body for n in ast.walk(statement)))

    def test_existing_views_have_own_conditions(self):
        for label in MODES[1:-1]:
            self.assertTrue(any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                         and isinstance(n.test.left, ast.Name) and n.test.left.id=='lab_mode'
                         and any(isinstance(v, ast.Constant) and v.value == label for v in n.test.comparators)
                         for n in ast.walk(self.lab)), label)
        # Volume → future swings uses the pre-existing terminal else branch.
        self.assertIn("        else:\n            events = load_volume_exhaustion_events()", self.src)

if __name__ == '__main__':
    unittest.main()
