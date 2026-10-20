"""Descriptive DNA of REACTION favorable runners. No trading or optimization.

Features are sourced from the existing frozen/as-of Runner Discovery dataset.
Family taxonomy is pre-specified, not fitted to outcomes. Missing != false.
"""
from __future__ import annotations

import pandas as pd

TRAITS = (
    ("Departure ≥2%", "max_departure_pct", "ge", 2.0),
    ("Room 1h ≥2%", "opposing_room_1h_pct", "ge", 2.0),
    ("BTC 4h extremo (±2%)", "side_adjusted_strength_vs_btc_4h", "abs", 2.0),
    ("Volumen 15m ≥2×", "rel_volume_15m", "ge", 2.0),
    ("Volumen retest ≥2×", "relative_volume", "ge", 2.0),
    ("Room HTF ≥3%", "nearest_opposing_room_pct", "ge", 3.0),
    ("Breadth 1h extremo", "market_breadth_1h", "outer", (20.0, 80.0)),
    ("Cambio breadth 1h ±20", "breadth_delta_1h", "abs", 20.0),
    ("RSI 15m extremo", "rsi14_15m", "outer", (30.0, 70.0)),
)
FAMILY_ORDER = (
    "Impulso + Room + BTC",
    "Dos rasgos",
    "Solo impulso",
    "Solo Room",
    "Solo BTC",
    "Ninguno de los tres",
    "Contexto incompleto",
)
MAIN_FEATURES = (
    "max_departure_pct",
    "opposing_room_1h_pct",
    "side_adjusted_strength_vs_btc_4h",
)


def trait_flag(series, operation, limit):
    """Return (known mask, boolean trait) without coercing missing to negative."""
    values = pd.to_numeric(series, errors="coerce")
    valid = values.notna()
    if operation == "ge":
        yes = values >= limit
    elif operation == "abs":
        yes = values.abs() >= limit
    elif operation == "outer":
        yes = (values <= limit[0]) | (values >= limit[1])
    else:
        raise ValueError(f"Unknown trait operation: {operation}")
    return valid, yes.fillna(False)


def runner_labels(frame, *, mode, target, horizon):
    """Label all complete/casual observable eligible events; ignore stop entirely.

    Structural run uses retest_close, observed run uses first archived 1m OPEN
    at or after the signal was known and first seen. No retrospective backfill
    is treated as observed.
    """
    if mode not in ("observed", "structural"):
        raise ValueError("mode must be observed or structural")
    target = int(target)
    horizon = int(horizon)
    column = (f"obs_time_to_run_{target}pct" if mode == "observed"
              else f"time_to_run_{target}pct")
    if column not in frame.columns:
        raise KeyError(column)
    good = frame["quality"].eq("COMPLETE_VALID")
    if mode == "observed":
        good &= frame["observed_quality"].eq("OBSERVED_PROXY")
        good &= pd.to_numeric(frame["observed_bars"], errors="coerce") >= horizon
    selected = frame.loc[good].copy()
    selected["dna_minuto"] = pd.to_numeric(selected[column], errors="coerce")
    selected["es_runner"] = selected["dna_minuto"].le(horizon).fillna(False)
    return selected


def compare_traits(frame, *, min_runners=15, min_controls=30):
    """Prevalence among runners vs controls with available factor for both.

    Denominators are factor-specific. This is association, NOT an odds model.
    """
    winners = frame["es_runner"].eq(True)
    rows = []
    for label, key, operation, limit in TRAITS:
        if key not in frame.columns:
            continue
        known, has_trait = trait_flag(frame[key], operation, limit)
        n_runner = int((known & winners).sum())
        n_control = int((known & ~winners).sum())
        if n_runner < min_runners or n_control < min_controls:
            continue
        r_with = int((known & winners & has_trait).sum())
        c_with = int((known & ~winners & has_trait).sum())
        rate_run = 100 * r_with / n_runner
        rate_other = 100 * c_with / n_control
        rows.append({
            "Rasgo": label,
            "runner_con_rasgo": r_with,
            "runner_con_dato": n_runner,
            "resto_con_rasgo": c_with,
            "resto_con_dato": n_control,
            "runner_pct": rate_run,
            "resto_pct": rate_other,
            "diferencia_pp": rate_run - rate_other,
            "cobertura": int(known.sum()),
        })
    return (pd.DataFrame(rows).sort_values("diferencia_pp", ascending=False)
            if rows else pd.DataFrame(columns=["Rasgo", "runner_pct", "resto_pct", "diferencia_pp"]))


def assign_families(frame):
    """One mutually exclusive descriptive family, without outcome-based fitting.

    A row with ANY missing core factor goes to Contexto incompleto, never
    Ninguno de los tres; prevents treating missing strength as weak strength.
    """
    result = frame.copy()
    conditions = []
    knowns = []
    for key, op, limit in (
        ("max_departure_pct", "ge", 2.0),
        ("opposing_room_1h_pct", "ge", 2.0),
        ("side_adjusted_strength_vs_btc_4h", "abs", 2.0),
    ):
        if key in frame.columns:
            known, yes = trait_flag(frame[key], op, limit)
        else:
            known = pd.Series(False, index=frame.index)
            yes = pd.Series(False, index=frame.index)
        knowns.append(known)
        conditions.append(yes)
    full_context = knowns[0] & knowns[1] & knowns[2]
    count = conditions[0].astype(int) + conditions[1].astype(int) + conditions[2].astype(int)
    families = pd.Series("Contexto incompleto", index=frame.index, dtype="string")
    families.loc[full_context & count.eq(3)] = "Impulso + Room + BTC"
    families.loc[full_context & count.eq(2)] = "Dos rasgos"
    families.loc[full_context & count.eq(1) & conditions[0]] = "Solo impulso"
    families.loc[full_context & count.eq(1) & conditions[1]] = "Solo Room"
    families.loc[full_context & count.eq(1) & conditions[2]] = "Solo BTC"
    families.loc[full_context & count.eq(0)] = "Ninguno de los tres"
    result["familia_adn"] = families
    return result


def family_report(frame):
    report = frame.groupby("familia_adn", observed=True).agg(
        senales=("es_runner", "size"),
        runners=("es_runner", "sum"),
    ).reset_index()
    report["tasa_runner_pct"] = (100 * report["runners"] / report["senales"]).round(1)
    report["orden"] = report["familia_adn"].map({v: i for i, v in enumerate(FAMILY_ORDER)})
    return report.sort_values("orden").drop(columns="orden")
