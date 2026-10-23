"""Fail Touch Analyzer V1 for Legacy confirmed-swing FIRST TOUCH research.

Research-only. Accepts the exact legacy TOUCH_FAILED classification, never
rewrites it. Trigger prices seen inside a closed 1m bar can enter no earlier
than the OPEN of the immediately following consecutive 1m bar. Never bridges
a timestamp gap. All post-touch geometry is descriptive, NOT a PRE feature.
"""
from __future__ import annotations

import math

import numpy as np

DEPTH_LEVELS = (0.5, 1.0, 1.5, 2.0, 3.0)
MODES = ("DEPTH_NEXT_OPEN", "RECOVER_50", "RECLAIM_PIVOT")
HORIZONS = (60, 180, 360)
TP_LEVELS = (1.0, 2.0)
SL_PCT = 1.0
SCAN_MINUTES = 360


def _finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _touch_geometry(row):
    pivot = _finite(row.get("swing_price"))
    op = _finite(row.get("retest_open"))
    hi = _finite(row.get("retest_high"))
    lo = _finite(row.get("retest_low"))
    cl = _finite(row.get("retest_close"))
    side = str(row.get("signal", "")).upper()
    if (side not in ("LONG", "SHORT") or not all(math.isfinite(x) for x in (pivot, op, hi, lo, cl))
            or pivot <= 0 or lo <= 0 or not (lo <= op <= hi and lo <= cl <= hi)):
        return {}
    range_price = hi - lo
    wick_price = (min(op, cl) - lo) if side == "LONG" else (hi - max(op, cl))
    beyond_pivot = max(0.0, (pivot - lo) / pivot * 100.0) if side == "LONG" else max(0.0, (hi - pivot) / pivot * 100.0)
    fail_close = ((pivot - cl) / pivot * 100.0) if side == "LONG" else ((cl - pivot) / pivot * 100.0)
    return {
        "ftr_touch_penetration_pct": beyond_pivot,
        "ftr_touch_wick_share": 100.0 * max(0.0, wick_price) / range_price if range_price > 0 else float("nan"),
        "ftr_touch_wick_pct_pivot": 100.0 * max(0.0, wick_price) / pivot,
        "ftr_touch_body_share": 100.0 * abs(cl - op) / range_price if range_price > 0 else float("nan"),
        "ftr_touch_range_pct_pivot": 100.0 * range_price / pivot,
        "ftr_fail_close_depth_pct": fail_close,
    }


def _execution_result(ts, highs, lows, closes, segment_end, start, side, entry, tp_pct, horizon):
    """Evaluate TP/SL from an executable open; censored != no-hit."""
    stop_price = entry * (1.0 - SL_PCT / 100) if side == "LONG" else entry * (1.0 + SL_PCT / 100)
    target_price = entry * (1.0 + tp_pct / 100) if side == "LONG" else entry * (1.0 - tp_pct / 100)
    end = min(int(segment_end[start]), len(ts) - 1, start + horizon - 1)
    for i in range(start, end + 1):
        hi, lo = float(highs[i]), float(lows[i])
        hit_tp = hi >= target_price if side == "LONG" else lo <= target_price
        hit_sl = lo <= stop_price if side == "LONG" else hi >= stop_price
        if hit_tp and hit_sl:
            return "SL_AMBIGUOUS", i - start + 1
        if hit_sl:
            return "SL_FIRST", i - start + 1
        if hit_tp:
            return "TP_FIRST", i - start + 1
    if end < start + horizon - 1:
        return "CENSORED", end - start + 1
    return "NO_HIT", horizon


def _is_depth(side, pivot, hi, lo, threshold):
    return (lo <= pivot * (1 - threshold / 100.0)) if side == "LONG" else (hi >= pivot * (1 + threshold / 100.0))


def _is_reclaim(side, pivot, close):
    return close >= pivot if side == "LONG" else close <= pivot


def _is_recovery_half(side, pivot, extreme, close):
    target = extreme + 0.50 * (pivot - extreme)
    return close >= target if side == "LONG" else close <= target


def attach_fail_touch_metrics(row, forward_cache):
    """Returns row with per-fail touch geometry, depth/recovery and entry grids.

    All trigger eligibility is evaluated at CLOSED bars. The close of the
    first-contact candle is known before the NEXT 1m bar opens. No synthetic
    fills at the intrabar threshold, wick extreme or reclaim close.
    """
    row = dict(row)
    if str(row.get("status", "")) != "TOUCH_FAILED":
        return row
    row.update(_touch_geometry(row))
    row["ftr_grid"] = {}
    row["ftr_observed_minutes"] = 0
    row["ftr_reclaim_min"] = float("nan")
    row["ftr_max_depth_180m_pct"] = float("nan")
    row["ftr_complete_180m"] = False
    row["ftr_minutes_to_max_depth_180m"] = float("nan")
    row["ftr_extreme_wick_share"] = float("nan")
    if not forward_cache or not math.isfinite(_finite(row.get("ftr_touch_penetration_pct"))):
        return row
    try:
        side = str(row["signal"]).upper()
        pivot = float(row["swing_price"])
        touch_ts = int(row["retest_timestamp"])
        ts = forward_cache["timestamps"]
        opens = forward_cache["opens"]
        highs = forward_cache["highs"]
        lows = forward_cache["lows"]
        closes = forward_cache["closes"]
        seg_end = forward_cache["segment_end"]
        touch_i = int(np.searchsorted(ts, touch_ts, side="left"))
        if touch_i >= len(ts) or int(ts[touch_i]) != touch_ts:
            return row
        end = min(int(seg_end[touch_i]), len(ts) - 1, touch_i + SCAN_MINUTES)
    except (KeyError, TypeError, ValueError, IndexError):
        return row
    if side not in ("LONG", "SHORT") or pivot <= 0:
        return row

    # Post-touch sample includes the first failed 1m candle. All computed
    # maxima are outcome descriptors; only finalized when full coverage exists.
    row["ftr_observed_minutes"] = end - touch_i
    depth_at = lambda hi, lo: max(0.0, (pivot - lo) / pivot * 100) if side == "LONG" else max(0.0, (hi - pivot) / pivot * 100)
    observed_end_180 = min(end, touch_i + 180)
    row["ftr_max_depth_180m_pct"] = max(depth_at(float(highs[i]), float(lows[i])) for i in range(touch_i, observed_end_180 + 1))
    row["ftr_complete_180m"] = end >= touch_i + 180
    depth_candidates = [(depth_at(float(highs[i]), float(lows[i])), i) for i in range(touch_i, observed_end_180 + 1)]
    largest_depth, max_i = max(depth_candidates, key=lambda pair: pair[0])
    extreme_high, extreme_low = float(highs[max_i]), float(lows[max_i])
    extreme_open, extreme_close = float(opens[max_i]), float(closes[max_i])
    wick = (min(extreme_open, extreme_close) - extreme_low) if side == "LONG" else (extreme_high - max(extreme_open, extreme_close))
    candle_range = extreme_high - extreme_low
    row["ftr_max_depth_180m_pct"] = largest_depth
    row["ftr_minutes_to_max_depth_180m"] = (int(ts[max_i]) - touch_ts) / 60000.0
    row["ftr_extreme_wick_share"] = (100.0 * max(0.0, wick) / candle_range) if candle_range > 0 else float("nan")
    first_reclaim_ts = None
    for i in range(touch_i + 1, end + 1):
        if _is_reclaim(side, pivot, float(closes[i])):
            first_reclaim_ts = int(ts[i])
            break
    if first_reclaim_ts is not None:
        row["ftr_reclaim_min"] = (first_reclaim_ts - touch_ts) / 60000.0

    # Independent depth thresholds, searched chronologically. For a threshold
    # already hit in the FAILED bar, trigger is known at that bar close.
    for depth in DEPTH_LEVELS:
        first_hit = None
        for i in range(touch_i, end + 1):
            if _is_depth(side, pivot, float(highs[i]), float(lows[i]), depth):
                first_hit = i
                break
        for mode in MODES:
            entry_i = None
            signal_i = None
            if first_hit is not None:
                if mode == "DEPTH_NEXT_OPEN":
                    signal_i = first_hit
                else:
                    extreme = float(lows[touch_i]) if side == "LONG" else float(highs[touch_i])
                    for i in range(touch_i, end + 1):
                        extreme = min(extreme, float(lows[i])) if side == "LONG" else max(extreme, float(highs[i]))
                        if i < first_hit:
                            continue
                        cl = float(closes[i])
                        if mode == "RECOVER_50" and _is_recovery_half(side, pivot, extreme, cl):
                            signal_i = i
                            break
                        if mode == "RECLAIM_PIVOT" and i > touch_i and _is_reclaim(side, pivot, cl):
                            signal_i = i
                            break
            if signal_i is not None:
                after = signal_i + 1
                if after <= end and after < len(ts) and int(ts[after]) == int(ts[signal_i]) + 60000:
                    entry_i = after
            key = f"{depth:.1f}|{mode}"
            record = {
                "depth_pct": depth,
                "mode": mode,
                "triggered": first_hit is not None,
                "signal_ts": int(ts[signal_i]) if signal_i is not None else None,
                "entry_ts": int(ts[entry_i]) if entry_i is not None else None,
                "entry_price": float(opens[entry_i]) if entry_i is not None else None,
                "entry_delay_min": ((int(ts[entry_i]) - touch_ts) / 60000) if entry_i is not None else None,
                "outcomes": {},
            }
            for horizon in HORIZONS:
                for tp in TP_LEVELS:
                    if entry_i is None:
                        if signal_i is not None:
                            label = "ENTRY_CENSORED"
                        elif end < touch_i + SCAN_MINUTES:
                            label = "TRIGGER_CENSORED"
                        else:
                            label = "NO_TRIGGER"
                        bars = 0
                    else:
                        label, bars = _execution_result(ts, highs, lows, closes, seg_end, entry_i, side, float(opens[entry_i]), tp, horizon)
                    record["outcomes"][f"tp{tp:.0f}_sl1_{horizon}m"] = {"status": label, "observed_bars": bars}
            row["ftr_grid"][key] = record
    return row


def summarize_failure_entries(rows, depth, mode, tp, horizon):
    """Summary includes NO_ENTRY and CENSORED denominators."""
    key = f"{float(depth):.1f}|{mode}"
    label = f"tp{int(tp)}_sl1_{int(horizon)}m"
    counters = {s: 0 for s in ("TP_FIRST", "SL_FIRST", "SL_AMBIGUOUS", "NO_HIT", "CENSORED", "NO_TRIGGER", "TRIGGER_CENSORED", "ENTRY_CENSORED")}
    for row in rows:
        grid = row.get("ftr_grid") or {}
        detail = grid.get(key) or {}
        outcome = (detail.get("outcomes") or {}).get(label) or {}
        counters[outcome.get("status", "ENTRY_CENSORED")] += 1
    total = sum(counters.values())
    resolved = counters["TP_FIRST"] + counters["SL_FIRST"] + counters["SL_AMBIGUOUS"]
    return {
        "N failed": total,
        "Depth %": depth,
        "Entry": mode,
        "TP %": tp,
        "Horizon min": horizon,
        "TP first": counters["TP_FIRST"],
        "SL first": counters["SL_FIRST"],
        "Same bar TP+SL → SL": counters["SL_AMBIGUOUS"],
        "No hit (complete)": counters["NO_HIT"],
        "Censored": counters["CENSORED"],
        "No trigger (complete 360m)": counters["NO_TRIGGER"],
        "Trigger censored": counters["TRIGGER_CENSORED"],
        "Entry censored": counters["ENTRY_CENSORED"],
        "No entry total": counters["NO_TRIGGER"] + counters["TRIGGER_CENSORED"] + counters["ENTRY_CENSORED"],
        "TP / resolved %": round(100.0 * counters["TP_FIRST"] / resolved, 2) if resolved else float("nan"),
        "TP / all fails %": round(100.0 * counters["TP_FIRST"] / total, 2) if total else float("nan"),
    }



def summarize_failed_touch_geometry(failures):
    """Aggregate 1m geometry and *complete* 180m paths, by direction.

    Uses only the provided TOUCH_FAILED population; no implicit extrapolation
    from incomplete post-touch paths. Missing/non-finite metrics are omitted
    from their own denominators, not from the event count.
    """
    import pandas as pd

    if failures is None:
        failures = pd.DataFrame()
    failed = failures.copy()
    if "status" in failed.columns:
        failed = failed.loc[failed["status"].astype(str).eq("TOUCH_FAILED")].copy()
    else:
        failed = failed.iloc[0:0].copy()

    def _numeric(frame, column):
        if column not in frame.columns:
            return pd.Series(float("nan"), index=frame.index, dtype="float64")
        values = pd.to_numeric(frame[column], errors="coerce")
        return values.replace([float("inf"), float("-inf")], float("nan"))

    def _completed(frame):
        if "ftr_complete_180m" not in frame.columns:
            return pd.Series(False, index=frame.index, dtype="bool")
        # The producer writes actual booleans, but tolerate CSV string forms.
        return frame["ftr_complete_180m"].map(lambda value: (
            value is True or str(value).strip().lower() in ("true", "1")
        )).fillna(False).astype(bool)

    groups = (("ALL", failed),)
    if "signal" in failed.columns:
        groups += tuple((side, failed.loc[failed["signal"].astype(str).str.upper().eq(side)]) for side in ("LONG", "SHORT"))
    else:
        groups += (("LONG", failed.iloc[0:0]), ("SHORT", failed.iloc[0:0]))

    out = []
    for side, group in groups:
        initial = _numeric(group, "ftr_touch_penetration_pct").dropna()
        wick = _numeric(group, "ftr_touch_wick_share").dropna()
        completed = _completed(group)
        max_depth = _numeric(group, "ftr_max_depth_180m_pct").loc[completed].dropna()
        out.append({
            "Dirección": side,
            "N failed": int(len(group)),
            "N perforación 1m": int(len(initial)),
            "Avg Perforation 1m (%)": float(initial.mean()) if len(initial) else float("nan"),
            "Median Perforation 1m (%)": float(initial.median()) if len(initial) else float("nan"),
            "N 180m completos": int(completed.sum()),
            "N profundidad válida 180m": int(len(max_depth)),
            "Avg Max Perforation 180m (%)": float(max_depth.mean()) if len(max_depth) else float("nan"),
            "N wick 1m": int(len(wick)),
            "Avg Wick 1m (% rango)": float(wick.mean()) if len(wick) else float("nan"),
        })
    return out


def render_legacy_fail_touch_analyzer(view, chart_provider=None):
    """Legacy V1 dashboard section; no network, no journal or LIVE changes."""
    import pandas as pd
    import streamlit as st

    st.markdown("### 🧪 Fail Touch Analyzer · Legacy V1")
    st.caption(
        "Solo primeros contactos TOUCH_FAILED del universo Legacy actualmente seleccionado. "
        "El estado original NO cambia. Profundidades y reclaim posteriores son desenlaces, "
        "no señales PRE. Cada entrada es al OPEN de la siguiente vela 1m consecutiva, "
        "nunca al extremo de una mecha. Sin fees/slippage."
    )
    if view is None or view.empty or "status" not in view.columns:
        st.info("No hay first touches cargados.")
        return
    failures = view.loc[view["status"].astype(str).eq("TOUCH_FAILED")].copy()
    if failures.empty:
        st.info("No hay TOUCH_FAILED con los filtros actuales. Elegí All touches o Failed only.")
        return
    cols = st.columns(4)
    cols[0].metric("TOUCH_FAILED", len(failures))
    cols[1].metric("LONG", int(failures["signal"].eq("LONG").sum()))
    cols[2].metric("SHORT", int(failures["signal"].eq("SHORT").sum()))
    complete = failures.get("ftr_complete_180m", pd.Series(False, index=failures.index)).fillna(False).astype(bool)
    cols[3].metric("Cobertura 180m", f"{int(complete.sum())}/{len(failures)}")

    if "ftr_touch_penetration_pct" not in failures.columns:
        st.warning("Faltan campos Fail Touch V1. Recargá Legacy después de subir el módulo y app.py.")
        return

    all_failures = failures.copy()
    side = st.selectbox("Dirección · Fail Touch", ["ALL", "LONG", "SHORT"], key="legacy_ftr_side")
    if side != "ALL":
        failures = failures.loc[failures["signal"].eq(side)].copy()
    col1, col2, col3, col4 = st.columns(4)
    depth = col1.selectbox("Perforación mínima (%)", DEPTH_LEVELS, index=2, key="legacy_ftr_depth")
    mode = col2.selectbox("Entrada hipotética", MODES, index=1, key="legacy_ftr_mode")
    tp = col3.selectbox("TP (%)", TP_LEVELS, index=1, key="legacy_ftr_tp")
    horizon = col4.selectbox("Horizonte (min)", HORIZONS, index=1, key="legacy_ftr_horizon")

    geom = failures[[c for c in (
        "symbol", "signal", "detector", "retest_time", "swing_price", "ftr_touch_penetration_pct",
        "ftr_touch_wick_share", "ftr_touch_wick_pct_pivot", "ftr_touch_body_share",
        "ftr_fail_close_depth_pct", "ftr_max_depth_180m_pct", "ftr_minutes_to_max_depth_180m", "ftr_extreme_wick_share", "ftr_complete_180m",
        "failure_recovery_status", "failure_recovery_minutes", "ftr_reclaim_min") if c in failures.columns]].copy()
    for c in geom.columns:
        if c not in ("symbol", "signal", "detector", "retest_time", "failure_recovery_status", "ftr_complete_180m"):
            geom[c] = pd.to_numeric(geom[c], errors="coerce").round(3)
    st.markdown("#### Mechas, distancia y recuperación del pivot")
    st.dataframe(geom, use_container_width=True, hide_index=True)
    st.download_button("Exportar geometría FAILED (CSV)", geom.to_csv(index=False).encode(), "legacy_failed_touch_geometry_v1.csv", "text/csv", key="legacy_ftr_export")

    valid = pd.to_numeric(failures["ftr_touch_penetration_pct"], errors="coerce")
    wick = pd.to_numeric(failures["ftr_touch_wick_share"], errors="coerce")
    metric_cols = st.columns(2)
    metric_cols[0].metric("Mecha mediana (% rango)", f"{wick.median():.1f}%" if wick.notna().any() else "—")
    recovered = failures.get("failure_recovery_status", pd.Series("", index=failures.index)).fillna("").eq("RECOVERED")
    metric_cols[1].metric("Reclaim observado 180m", f"{int(recovered.sum())}/{len(failures)}")

    st.markdown("#### Avg Perforation y mechas · 1m / 180m")
    # Current direction cards match the selected Legacy Fail Touch filter;
    # the table retains ALL / LONG / SHORT for side-by-side comparison.
    geometry_summary = summarize_failed_touch_geometry(all_failures)
    selected_geometry = next(item for item in geometry_summary if item["Dirección"] == side)
    def _format_pct(value, digits=2):
        return f"{value:.{digits}f}%" if math.isfinite(value) else "—"

    avg_cols = st.columns(4)
    avg_cols[0].metric("Avg Perforation 1m", _format_pct(selected_geometry["Avg Perforation 1m (%)"]))
    avg_cols[1].metric("Median Perforation 1m", _format_pct(selected_geometry["Median Perforation 1m (%)"]))
    avg_cols[2].metric("Avg Max Perforation 180m", _format_pct(selected_geometry["Avg Max Perforation 180m (%)"]))
    avg_cols[3].metric("Avg Wick 1m (% rango)", _format_pct(selected_geometry["Avg Wick 1m (% rango)"], 1))
    st.caption(
        f"Dirección: {side} · Perforación inicial: "
        f"{selected_geometry['N perforación 1m']}/{selected_geometry['N failed']} con dato válido · "
        f"Profundidad máxima a 180m: {selected_geometry['N profundidad válida 180m']}/"
        f"{selected_geometry['N failed']} con cobertura completa y valor válido · "
        f"Mecha 1m: {selected_geometry['N wick 1m']}/{selected_geometry['N failed']} con dato válido. "
        "La profundidad máxima es un dato POST descriptivo, no una señal disponible al entrar."
    )
    summary_df = pd.DataFrame(geometry_summary)
    for col in ("Avg Perforation 1m (%)", "Median Perforation 1m (%)", "Avg Max Perforation 180m (%)", "Avg Wick 1m (% rango)"):
        summary_df[col] = summary_df[col].round(3)
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    st.markdown("#### Barrida inicial y mechas — grupos predefinidos")
    bins_p = [0.0, 0.25, 0.50, 1.0, 2.0, 3.0, 5.0, float("inf")]
    bins_w = [0.0, 25.0, 50.0, 75.0, 100.00001]
    for label, src, edges in (
        ("Penetración del pivot en primer 1m (%)", "ftr_touch_penetration_pct", bins_p),
        ("Mecha de fallo (% del rango 1m)", "ftr_touch_wick_share", bins_w),
    ):
        segment = failures.copy()
        segment["_bucket"] = pd.cut(pd.to_numeric(segment[src], errors="coerce"), bins=edges, include_lowest=True)
        report = []
        for bucket, subgroup in segment.groupby("_bucket", observed=True, dropna=True):
            states = subgroup.get("failure_recovery_status", pd.Series("", index=subgroup.index)).fillna("")
            resolved = states.isin(("RECOVERED", "STRUCTURAL_FAIL_180M"))
            n_resolved = int(resolved.sum())
            n_reclaimed = int(states.eq("RECOVERED").sum())
            report.append({
                "Grupo": str(bucket), "N": len(subgroup), "Reclaimed 180m": n_reclaimed,
                "Structural fail 180m": int(states.eq("STRUCTURAL_FAIL_180M").sum()),
                "Pending/other": len(subgroup) - n_resolved,
                "Reclaim / evaluables %": round(n_reclaimed * 100 / n_resolved, 2) if n_resolved else float("nan"),
                "Mecha mediana %": round(pd.to_numeric(subgroup["ftr_touch_wick_share"], errors="coerce").median(), 2),
                "Penetración mediana %": round(pd.to_numeric(subgroup["ftr_touch_penetration_pct"], errors="coerce").median(), 3),
            })
        st.caption(label + " · exploratory; los casos sin cobertura no cuentan como fallidos")
        if report:
            st.dataframe(pd.DataFrame(report), hide_index=True, use_container_width=True)

    st.markdown("#### Profundidad → entrada → TP/SL")
    summary = summarize_failure_entries(failures.to_dict("records"), depth, mode, tp, horizon)
    st.dataframe(pd.DataFrame([summary]), hide_index=True, use_container_width=True)
    st.caption("NO_TRIGGER solo si se observó la ventana completa de búsqueda (360m). "
               "TRIGGER_CENSORED y ENTRY_CENSORED señalan datos insuficientes, no señales negativas. "
               "CENSORED tras entrar no es NO_HIT. TP+SL en la misma 1m cuenta como pérdida ambigua. "
               "Compará las tasas sobre resueltos y sobre TODOS los fallos.")
    if st.checkbox("Ver matriz de todas las profundidades y entradas", value=False, key="legacy_ftr_all_grid"):
        matrix = [summarize_failure_entries(failures.to_dict("records"), d, m, tp, horizon) for d in DEPTH_LEVELS for m in MODES]
        st.dataframe(pd.DataFrame(matrix), hide_index=True, use_container_width=True)

    # Individual inspections: explicit first-touch data and entry vs status;
    # original Legacy 15m inspector remains immediately below this section.
    table = []
    key = f"{float(depth):.1f}|{mode}"
    outcome_key = f"tp{int(tp)}_sl1_{int(horizon)}m"
    for _, event in failures.iterrows():
        record = (event.get("ftr_grid") or {}).get(key, {})
        status = (record.get("outcomes") or {}).get(outcome_key, {}).get("status", "ENTRY_CENSORED")
        table.append({
            "Symbol": event.get("symbol"), "Side": event.get("signal"), "Detector": event.get("detector"),
            "First touch": event.get("retest_time"), "Touch penetration %": event.get("ftr_touch_penetration_pct"),
            "Touch wick % range": event.get("ftr_touch_wick_share"), "Max depth 180m %": event.get("ftr_max_depth_180m_pct"),
            "Entry time (UTC ms)": record.get("entry_ts"), "Entry price": record.get("entry_price"),
            "Outcome": status,
        })
    st.markdown("#### Casos individuales · incluye fallos reales")
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
    st.caption("Investigación exploratoria: la muestra corresponde a la ventana/filtros de Legacy, "
               "no a un registro continuo exhaustivo. No interpretar un MFE alto como ejecución posible.")

    # Optional 1m inspector: load one selected event only. No network in the
    # metric engine; provider is the existing Legacy candle loader.
    if chart_provider is not None and st.checkbox("Inspeccionar velas 1m del FAILED seleccionado", value=False, key="legacy_ftr_inspect_1m"):
        import plotly.graph_objects as go
        inspect = failures.reset_index(drop=True)
        selection = st.selectbox(
            "Evento failed", list(range(len(inspect))),
            format_func=lambda i: (
                f"{inspect.iloc[i].get('symbol')} · {inspect.iloc[i].get('signal')} · "
                f"{inspect.iloc[i].get('retest_time')}"
            ), key="legacy_ftr_inspect_event",
        )
        chosen = inspect.iloc[int(selection)]
        touch_ts = int(chosen["retest_timestamp"])
        age_min = pd.to_numeric(chosen.get("retest_age_min"), errors="coerce")
        fetch_limit = min(5000, max(400, int(age_min + 200) if pd.notna(age_min) else 400))
        candles = chart_provider(symbol=str(chosen["symbol"]), timeframe="1m", limit=fetch_limit)
        if candles is None or candles.empty or not {"timestamp", "open", "high", "low", "close"}.issubset(candles.columns):
            st.warning("No hay velas 1m para este evento; la tabla de métricas permanece disponible.")
        else:
            candles = candles.copy()
            for c in ("timestamp", "open", "high", "low", "close"):
                candles[c] = pd.to_numeric(candles[c], errors="coerce")
            candles = candles.dropna(subset=["timestamp", "open", "high", "low", "close"])
            candles = candles.drop_duplicates("timestamp").sort_values("timestamp")
            window = candles.loc[candles["timestamp"].between(touch_ts - 20 * 60000, touch_ts + 180 * 60000)].copy()
            if touch_ts not in set(window["timestamp"].astype("int64")):
                st.warning("El proveedor no incluye el primer contacto histórico en la ventana 1m solicitada. No se reconstruye un gráfico incompleto.")
            else:
                x = pd.to_datetime(window["timestamp"], unit="ms", utc=True)
                fig = go.Figure(data=[go.Candlestick(x=x, open=window["open"], high=window["high"], low=window["low"], close=window["close"], name="1m OHLC")])
                pivot = float(chosen["swing_price"])
                fig.add_hline(y=pivot, line_dash="dash", line_color="#d2a154", annotation_text="Pivot confirmado")
                trigger_price = pivot * (1 - float(depth)/100) if chosen["signal"] == "LONG" else pivot * (1 + float(depth)/100)
                fig.add_hline(y=trigger_price, line_dash="dot", line_color="#c85b5b", annotation_text=f"Profundidad {depth}%")
                fig.add_vline(x=pd.to_datetime(touch_ts, unit="ms", utc=True).to_pydatetime().timestamp() * 1000, line_dash="dash", line_color="#d47750")
                selected_entry = (chosen.get("ftr_grid") or {}).get(key) or {}
                if selected_entry.get("entry_ts") is not None:
                    entry_dt = pd.to_datetime(int(selected_entry["entry_ts"]), unit="ms", utc=True)
                    fig.add_trace(go.Scatter(x=[entry_dt], y=[selected_entry["entry_price"]], mode="markers", marker={"size":11,"symbol":"diamond","color":"#249875"}, name="Entry next 1m OPEN"))
                fig.update_layout(title="Legacy FAILED · 1m · pivot / barrida / entrada", xaxis_rangeslider_visible=False, height=460, margin={"l":10,"r":10,"t":52,"b":10})
                st.plotly_chart(fig, use_container_width=True, key="legacy_ftr_1m_chart")
                st.caption("El gráfico 1m solo representa barras efectivamente devueltas por el proveedor; la estrategia usa la trayectoria contigua verificada durante el escaneo original.")
