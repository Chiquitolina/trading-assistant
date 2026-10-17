"""Navigation adapter for Phase-1 Candidate Competition Lab.

Reads existing immutable Competition Ledger exports only. No replay, portfolio,
Binance or Redis calls. Outputs can be generated separately with
candidate_competition_lab.py; they appear automatically after a dashboard rerun.
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from dashboard.views.reaction_lab_competition_view import render_competition_lab

REQUIRED_FILES = (
    "competition_manifest.json",
    "competition_decisions.csv",
    "slot_episodes.csv",
    "ready_groups.csv",
)


def discover_competition_ledgers(base_dir: Path) -> list[Path]:
    """Find completed exports, without generating or modifying any data."""
    base_dir = Path(base_dir)
    roots = [Path("/root"), base_dir / "reports" / "reaction_lab"]
    env_dir = os.getenv("COMPETITION_LAB_DATA_DIR", "").strip()
    discovered = []
    if env_dir:
        discovered.append(Path(env_dir).expanduser())
    for root in roots:
        try:
            discovered.extend(root.glob("runner_competition_*"))
            discovered.extend(root.glob("candidate_competition_*"))
            if root == base_dir / "reports" / "reaction_lab":
                discovered.extend(root.glob("competition_*"))
        except (OSError, PermissionError):
            continue
    valid: dict[str, Path] = {}
    for path in discovered:
        try:
            if path.is_dir() and all((path / f).is_file() for f in REQUIRED_FILES):
                resolved = path.resolve()
                valid[str(resolved)] = resolved
        except (OSError, PermissionError):
            continue
    def recent(path: Path) -> int:
        try:
            return (path / "competition_manifest.json").stat().st_mtime_ns
        except OSError:
            return 0
    return sorted(valid.values(), key=recent, reverse=True)


def render_competition_dashboard(base_dir: Path) -> None:
    """Entry point from Reaction & Swing Lab's view selector."""
    roots = discover_competition_ledgers(Path(base_dir))
    if not roots:
        st.info(
            "Todavía no hay un Competition Ledger completo en /root/runner_competition_* "
            "o reports/reaction_lab/competition_*. Ejecutá primero "
            "candidate_competition_lab.py sobre un portfolio congelado."
        )
        return

    choice = st.selectbox(
        "Competition Ledger (histórico, solo lectura)",
        options=range(len(roots)),
        format_func=lambda i: f"{roots[i].name} — {roots[i]}",
        key="reaction_competition_dataset_selection_v1",
    )
    st.caption(
        "Vista independiente del cohort selector de Reaction Lab. "
        "No recalcula señales, no ejecuta órdenes y no cambia ninguna estrategia. "
        "Phase 1: aún no existen Shadow Outcomes de candidatas descartadas."
    )
    render_competition_lab(roots[choice])
