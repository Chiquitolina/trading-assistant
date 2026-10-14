"""Regression tests: Runner Analyzer Spearman without the optional SciPy package."""
import builtins
import unittest
from unittest.mock import patch
import pandas as pd
from reaction_lab_runner_analyzer import spearman_feature_table


class SpearmanWithoutScipyTests(unittest.TestCase):
    def test_tied_rank_correlation_without_scipy(self):
        data = pd.DataFrame({
            'side_adjusted_strength_vs_btc_4h': [1., 2., 2., 4., 5., None],
            'run_horizon_pct': [1., 3., 2., 4., 5., 10.],
        })
        real_import = builtins.__import__

        def reject_scipy(name, *args, **kwargs):
            if name == 'scipy' or name.startswith('scipy.'):
                raise ModuleNotFoundError("No module named 'scipy'")
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=reject_scipy):
            result = spearman_feature_table(data, min_n=4)
        row = result.set_index('Factor').loc['Strength BTC 4h (direccional)']
        self.assertEqual(int(row['N']), 5)  # pairwise non-null only
        expected = data.iloc[:5, 0].rank(method='average').corr(
            data.iloc[:5, 1].rank(method='average'))
        self.assertAlmostEqual(float(row['Spearman con Run']), round(expected, 4))

    def test_perfect_negative_with_ties(self):
        frame = pd.DataFrame({
            'side_adjusted_strength_vs_btc_4h': [1., 1., 2., 2., 3., 3.],
            'run_horizon_pct': [3., 3., 2., 2., 1., 1.],
        })
        result = spearman_feature_table(frame, min_n=5)
        self.assertEqual(float(result.iloc[0]['Spearman con Run']), -1.0)

    def test_insufficient_or_constant_values_omitted(self):
        frame = pd.DataFrame({
            'side_adjusted_strength_vs_btc_4h': [1., 1., 1., 1.],
            'run_horizon_pct': [1., 2., 3., 4.],
        })
        self.assertTrue(spearman_feature_table(frame, min_n=4).empty)
        frame.iloc[0, 0] = None
        self.assertTrue(spearman_feature_table(frame, min_n=4).empty)


if __name__ == '__main__':
    unittest.main()
