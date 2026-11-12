"""V3.1.5.1 regression: POST reference has a color even with no visible PRE stage.

Pure Python / offline test. No Streamlit, network, HLL, LIVE or data snapshot.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

import pandas as pd

VIEW = (Path(__file__).resolve().parent / "dashboard" / "views"
        / "reaction_multiswing_btc60d_view_v1.py")


class RecordingFigure:
    def __init__(self):
        self.shapes = []
        self.annotations = []

    def add_shape(self, **kwargs):
        self.shapes.append(kwargs)

    def add_annotation(self, **kwargs):
        self.annotations.append(kwargs)


def post_reference_code():
    """Compile the exact rendering branch without running the whole dashboard.

    The real failure occurs when the surrounding PRE-stage loop never runs;
    running this branch in a fresh namespace simulates precisely that case.
    """
    tree = ast.parse(VIEW.read_text(encoding="utf-8"), filename=str(VIEW))
    funcs = [node for node in tree.body
             if isinstance(node, ast.FunctionDef)
             and node.name == "render_btc_multiswing_60d_view"]
    assert len(funcs) == 1, "Main multi-swing renderer not found"
    branches = [node for node in ast.walk(funcs[0])
                if isinstance(node, ast.If)
                and ast.unparse(node.test) == "reveal_post and chart_end > cutoff"]
    assert len(branches) == 1, "POST reference branch changed"
    snippet = ast.Module(body=[branches[0]], type_ignores=[])
    return compile(ast.fix_missing_locations(snippet), str(VIEW), "exec")


class PostBandColorV3151(unittest.TestCase):
    def draw(self, side: str, reveal: bool = True):
        fig = RecordingFigure()
        region = SimpleNamespace(side=side, band_low=75644.0, band_high=75786.8)
        cutoff = 1794058200000
        context = {
            "fig": fig,
            "region_inspection": region,
            "cutoff": cutoff,
            "chart_end": cutoff + 60 * 60_000,
            "reveal_post": reveal,
            "pd": pd,
            # Intentionally NO `color` and NO visible PRE stages.
        }
        exec(post_reference_code(), context, context)
        return fig, region

    def test_low_post_without_pre_segments_has_selected_band_color(self):
        fig, region = self.draw("LOW")
        self.assertEqual(len(fig.shapes), 1)
        self.assertEqual(fig.shapes[0]["fillcolor"], "#4fd3ae")
        self.assertEqual(fig.shapes[0]["line"]["color"], "#4fd3ae")
        self.assertEqual(fig.annotations[0]["font"]["color"], "#4fd3ae")
        self.assertEqual((fig.shapes[0]["y0"], fig.shapes[0]["y1"]),
                         (region.band_low, region.band_high))

    def test_high_post_without_pre_segments_has_selected_band_color(self):
        fig, _ = self.draw("HIGH")
        self.assertEqual(len(fig.shapes), 1)
        self.assertEqual(fig.shapes[0]["fillcolor"], "#ffb36a")
        self.assertEqual(fig.annotations[0]["font"]["color"], "#ffb36a")

    def test_post_hidden_draws_no_reference(self):
        fig, _ = self.draw("LOW", reveal=False)
        self.assertEqual(fig.shapes, [])
        self.assertEqual(fig.annotations, [])


if __name__ == "__main__":
    unittest.main()
