"""Read-only batch inspection of HLL Phase Reconstruction PRE V1.

Additive section of the existing HLL inspector. No recomputation or trading logic.
All overlay features were created from the 96 fully closed 30m candles before PRE.
"""
from __future__ import annotations

import math
import json
import hashlib
from pathlib import Path

import pandas as pd

TF_MS = 1_800_000
# Display context only; the frozen PRE detections/family analysis remain 96 candles.
PRE_VISUAL_OPTIONS = (48, 96, 144, 192, 288, 384)
POST_VISUAL_OPTIONS = (12, 24, 48, 72, 96)
# UI-only defaults. The frozen detector still uses 96 fully closed PRE bars.
DEFAULT_PRE_VISUAL_BARS = 288
DEFAULT_POST_VISUAL_BARS = 96
REL = Path("reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1")
PHASE_REL = Path("reports/horizontal_level_second_ascent_pre/core15_two_leg_v12/segments_pre.csv")
CANDLES_REL = Path("reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc")
FAMILY_REL = Path("reports/horizontal_level_second_ascent_pre/core15_pre_family_interpreter_v1")
FAMILY_FILE = "families_pre.csv"
FAMILY_MANIFEST = "manifest.json"
AUDIT_REL = Path("reports/horizontal_level_second_ascent_pre/core15_pre_family_audit_v1")
CONTINUITY_REL = Path("reports/horizontal_level_second_ascent_pre/core15_pre_phase_continuity_audit_v1")
CONTINUITY_SOURCES = {
    "audit_matrix": AUDIT_REL / "audit_matrix_pre.csv",
    "audit_manifest": AUDIT_REL / "manifest.json",
    "phase_manifest": REL / "manifest.json",
    "summary": REL / "phase_summary_pre.csv",
    "legs": REL / "phase_legs_pre.csv",
    "candidates": REL / "phase_recovery_candidates_pre.csv",
}

FAMILY_SOURCES = {
    "summary": REL / "phase_summary_pre.csv",
    "pivots": REL / "phase_confirmed_pivots_pre.csv",
    "legs": REL / "phase_legs_pre.csv",
    "recovery": REL / "phase_recovery_candidates_pre.csv",
    "resistance": Path("reports/horizontal_level_second_ascent_pre/core15_resistance_interaction_pre_v3/resistance_windows_pre.csv"),
    "ohlc": Path("reports/horizontal_level_second_ascent_pre/core15_structural_approach_ohlc_pre_v2/candle_windows_pre.csv"),
}

FILES = {
    "summary": "phase_summary_pre.csv",
    "pivots": "phase_confirmed_pivots_pre.csv",
    "legs": "phase_legs_pre.csv",
    "candidates": "phase_recovery_candidates_pre.csv",
}

# Visual judgments are contextual notes, never features or filter rules.
# The six third-batch judgments were made by the assistant, not the user.
TANDAS = {
    "Tanda 1 · referencias (7)": [
        ("AAVEUSDT", "2221ee173c945cf17b026f2d", "Constructiva", "Revisión visual preliminar"),
        ("AIAUSDT", "81bcd23506a40114b2eba209", "Constructiva", "Revisión visual preliminar"),
        ("1000PEPEUSDT", "5c16416a996220d47fe32c96", "Constructiva", "Revisión visual preliminar"),
        ("2ZUSDT", "ffcb487cc3523ae7d1c48dbe", "Constructiva", "Revisión visual preliminar"),
        ("ACUUSDT", "b7b84e538c0fbe8799b2762a", "No convence", "Revisión visual preliminar"),
        ("1000FLOKIUSDT", "a8cd48b0f38f97d50a91be6d", "No convence", "Revisión visual preliminar"),
        ("1INCHUSDT", "917bcd9be0ad18833727dc3d", "No convence", "Revisión visual preliminar"),
    ],
    "Tanda 2 · tres fantásticos y BONK (4)": [
        ("ACUUSDT", "bb5f7458c98c04efff3edaa0", "Fantástica", "Usuario"),
        ("AAVEUSDT", "abe8975e8b1ff733e8513f76", "Fantástica", "Usuario"),
        ("2ZUSDT", "412e50a892f6e90f492ceaaa", "Fantástica", "Usuario"),
        ("1000BONKUSDT", "a419c2ae10918faceff87cc4", "No convence", "Usuario"),
    ],
    "Tanda 3 · contraste adicional (6)": [
        ("4USDT", "edc22fc645c77831fe3f6a48", "Constructiva", "Asistente · provisional"),
        ("1000FLOKIUSDT", "405fb903640b8544e19ab2e0", "Dudosa", "Asistente · provisional"),
        ("ADAUSDT", "4958928649fa8e6fe52be59e", "Constructiva/dudosa", "Asistente · provisional"),
        ("1000PEPEUSDT", "b00b2dac6b48b6fa67caa257", "Constructiva", "Asistente · provisional"),
        ("1000BONKUSDT", "a3015ca2f41fa79ef27de2c1", "No convence", "Asistente · provisional"),
        ("AIAUSDT", "2731c934c96999d326127e57", "Constructiva", "Asistente · provisional"),
    ],
}
ALL = "Todos los 50 intentos CORE15"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False)


def _required_sources(root: Path) -> dict[str, Path]:
    sources = {k: root / REL / v for k, v in FILES.items()}
    sources["levels"] = root / PHASE_REL
    return sources


def validate_sources(summaries: pd.DataFrame, levels: pd.DataFrame) -> None:
    """Fail closed for mismatched attempt/symbol/cutoff or incomplete phases."""
    must = {"attempt_id", "symbol", "pre_closed_ts", "scale_atr", "status"}
    needed = {"attempt_id", "symbol", "pre_closed_ts", "zone_low", "zone_high"}
    if not must.issubset(summaries.columns):
        raise ValueError("Faltan columnas de Phase Summary PRE")
    if not needed.issubset(levels.columns):
        raise ValueError("Faltan columnas de zonas PRE originales")
    if summaries.duplicated(["attempt_id", "scale_atr"]).any():
        raise ValueError("Phase Summary tiene attempt/scale duplicados")
    if levels.duplicated(["attempt_id"]).any():
        raise ValueError("La fuente de zonas tiene attempt_id duplicados")
    if not summaries.status.eq("COMPLETE_PRE").all():
        raise ValueError("Hay reconstrucciones PRE incompletas")
    meta = summaries[["attempt_id", "symbol", "pre_closed_ts"]].drop_duplicates()
    if meta.duplicated(["attempt_id"]).any():
        raise ValueError("Una estructura tiene más de un símbolo o PRE")
    if set(meta.attempt_id) != set(levels.attempt_id):
        raise ValueError("Los attempt_id del nivel no coinciden con Phase V1")
    joined = meta.merge(levels[["attempt_id", "symbol", "pre_closed_ts", "zone_low", "zone_high"]],
                        on="attempt_id", validate="one_to_one", suffixes=("_phase", "_level"))
    if not ((joined.symbol_phase.astype(str) == joined.symbol_level.astype(str)) &
            (joined.pre_closed_ts_phase.astype("int64") == joined.pre_closed_ts_level.astype("int64"))).all():
        raise ValueError("Incompatibilidad de símbolo o corte PRE con zona")
    if (pd.to_numeric(joined.zone_low, errors="coerce") >=
        pd.to_numeric(joined.zone_high, errors="coerce")).any():
        raise ValueError("Resistencia sin banda válida")



def validate_family_export(families: pd.DataFrame, summary: pd.DataFrame,
                           *, root: Path | None = None, manifest: dict | None = None) -> None:
    """Fail closed: 1:1 PRE snapshot, unchanged sources, no inferred POST."""
    keys = ["attempt_id", "symbol", "pre_closed_ts", "scale_atr"]
    needed = set(keys) | {"family_candidate", "family_basis", "status",
        "supporting_observations", "contradicting_observations", "limitations"}
    if not needed.issubset(families.columns):
        raise ValueError("Faltan columnas del intérprete de familias")
    if families.duplicated(["attempt_id", "scale_atr"]).any():
        raise ValueError("Intentos/escalas duplicados en familias PRE")
    if not families.status.eq("DESCRIPTIVE_PRE_ONLY").all():
        raise ValueError("El intérprete debe entregar exclusivamente estados descriptivos PRE")
    if len(families) != len(summary):
        raise ValueError("El intérprete no cubre todos los intentos/escalas de Phase PRE")
    fam = families[keys].copy()
    phase = summary[keys].copy()
    for data in (fam, phase):
        data["attempt_id"] = data["attempt_id"].astype(str)
        data["symbol"] = data["symbol"].astype(str)
        data["pre_closed_ts"] = pd.to_numeric(data["pre_closed_ts"], errors="raise").astype("int64")
        data["scale_atr"] = pd.to_numeric(data["scale_atr"], errors="raise").round(6)
    merged = phase.merge(fam, on=keys, validate="one_to_one", how="outer", indicator=True)
    if not merged._merge.eq("both").all() or len(merged) != len(summary):
        raise ValueError("Las familias no coinciden con los cortes PRE de Phase V1")
    if manifest is not None:
        guard = manifest.get("guardrails", {})
        if not (guard.get("reads_post") is False and
                guard.get("reads_visual_labels") is False and
                guard.get("changes_live") is False and
                guard.get("writes_signals") is False):
            raise ValueError("Manifest de familias no cumple restricciones PRE")
        if root is not None:
            for name, reference in FAMILY_SOURCES.items():
                path = root / reference
                expected = manifest.get("source_sha256", {}).get(name)
                if not expected or not path.is_file():
                    raise ValueError(f"Falta fuente/código de procedencia para {name}")
                h = hashlib.sha256()
                with path.open("rb") as f:
                    for part in iter(lambda: f.read(1024*1024), b""):
                        h.update(part)
                if h.hexdigest() != expected:
                    raise ValueError(f"Fuente PRE modificada desde la ejecución: {name}")



def validate_audit_export(audit: pd.DataFrame, families: pd.DataFrame,
                          *, root: Path, manifest: dict) -> None:
    """Only display a current 50-row PRE audit of the same frozen families."""
    needed = {"attempt_id", "symbol", "level_id", "pre_closed_ts", "family_1p0",
              "family_0p5", "family_1p5", "review_visual", "review_provenance",
              "intercontact_direction_48", "scales_agree", "audit_flags", "audit_priority"}
    if not needed.issubset(audit.columns) or len(audit) != 50 or audit.attempt_id.duplicated().any():
        raise ValueError("Matriz de auditoría incompleta, IDs duplicados o columnas ausentes")
    if not (manifest.get("inputs_are_pre_only") is True and manifest.get("reads_post") is False
            and manifest.get("reads_visual_for_family") is False and manifest.get("modifies_live") is False):
        raise ValueError("La auditoría no certifica aislamiento PRE")
    originals = {
        "families": root / FAMILY_REL / FAMILY_FILE,
        "family_manifest": root / FAMILY_REL / FAMILY_MANIFEST,
        "resistance": root / FAMILY_SOURCES["resistance"],
        "ohlc": root / FAMILY_SOURCES["ohlc"],
    }
    for name, path in originals.items():
        h = hashlib.sha256()
        if not path.is_file():
            raise ValueError(f"Falta fuente original para auditoría: {name}")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != manifest.get("source_sha256", {}).get(name):
            raise ValueError(f"Auditoría desactualizada o fuente cambiada: {name}")
    lookup = families.copy()
    lookup["scale_atr"] = pd.to_numeric(lookup.scale_atr, errors="raise")
    lookup = lookup.loc[lookup.scale_atr.eq(1.0),
                        ["attempt_id", "symbol", "level_id", "pre_closed_ts", "family_candidate"]]
    if len(lookup) != 50 or lookup.attempt_id.duplicated().any():
        raise ValueError("No hay exactamente 50 familias a 1 ATR")
    actual = audit[["attempt_id", "symbol", "level_id", "pre_closed_ts", "family_1p0"]].copy()
    both = actual.merge(lookup, on="attempt_id", how="outer", indicator=True,
                        suffixes=("_audit", "_family"), validate="one_to_one")
    if not both._merge.eq("both").all() or len(both) != 50:
        raise ValueError("Auditoría y familias no cubren los mismos 50 IDs")
    for name in ("symbol", "level_id"):
        if not both[f"{name}_audit"].astype(str).eq(both[f"{name}_family"].astype(str)).all():
            raise ValueError(f"Auditoría y familias tienen {name} diferente")
    if not pd.to_numeric(both.pre_closed_ts_audit).eq(pd.to_numeric(both.pre_closed_ts_family)).all():
        raise ValueError("Auditoría y familias tienen corte PRE diferente")
    if not both.family_1p0.eq(both.family_candidate).all():
        raise ValueError("Familia auditada no coincide con snapshot congelado")


def render_audit_matrix(root: Path, read_cached, family_export: pd.DataFrame) -> None:
    """Optional audit section within this same HLL inspector, no new Streamlit app."""
    import streamlit as st
    path = root / AUDIT_REL / "audit_matrix_pre.csv"
    manifest_path = root / AUDIT_REL / "manifest.json"
    with st.expander("🔎 Auditoría de los 50 · matriz y contraejemplos PRE", expanded=False):
        if not (path.is_file() and manifest_path.is_file()):
            st.info("Para generar la auditoría: `python3 hll_pre_family_audit_v1.py --plan` "
                    "y después `python3 hll_pre_family_audit_v1.py --run`. "
                    "No modifica las familias existentes.")
            return
        try:
            audit = read_cached(str(path), path.stat().st_mtime_ns)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_audit_export(audit, family_export, root=root, manifest=manifest)
        except (OSError, KeyError, ValueError, TypeError, pd.errors.ParserError) as exc:
            st.warning(f"Auditoría no disponible: {exc}")
            return
        c1,c2,c3,c4=st.columns(4)
        inter = audit.intercontact_direction_48.value_counts()
        c1.metric("Intercontact SUBE", int(inter.get("SUBE",0)))
        c2.metric("Intercontact BAJA", int(inter.get("BAJA",0)))
        c3.metric("Sin datos", int(inter.get("SIN_DATOS",0)))
        c4.metric("Escalas coinciden", int(pd.to_numeric(audit.scales_agree).sum()))
        st.caption("50 intentos; 17 referencias visuales de origen diverso (incluye juicios del asistente). "
                   "No es un clasificador bueno/malo y no evalúa rentabilidad. "
                   "La ventana PRE48 ya fue inspeccionada, no es holdout.")
        filter_mode=st.selectbox("Subconjunto de auditoría",
            ["Todos los 50", "Prioridad de revisión", "Sin evaluación visual",
             "Intercontact sin datos", "Escalas inestables"], key="hll_pre_audit_subset")
        selection=audit.copy()
        if filter_mode=="Prioridad de revisión":
            selection=selection.loc[selection.audit_priority.eq("REVISAR")]
        elif filter_mode=="Sin evaluación visual":
            selection=selection.loc[selection.review_visual.eq("SIN_EVALUACION")]
        elif filter_mode=="Intercontact sin datos":
            selection=selection.loc[selection.intercontact_direction_48.eq("SIN_DATOS")]
        elif filter_mode=="Escalas inestables":
            selection=selection.loc[pd.to_numeric(selection.scales_agree).eq(0)]
        st.caption(f"{len(selection)} intentos en este subconjunto. Los filtros son solo navegación visual.")
        shown=["symbol", "attempt_id", "review_visual", "review_provenance",
               "family_1p0", "low_progression_1atr", "high_progression_1atr",
               "intercontact_direction_48", "intercontact_delta_48_atr",
               "net_return_pre48_pct", "scales_agree", "audit_flags"]
        st.dataframe(selection[shown], hide_index=True, use_container_width=True, height=380)
        st.download_button("Exportar matriz completa de los 50 (CSV)",
            audit.to_csv(index=False).encode("utf-8"),
            file_name="hll_audit_matrix_pre_50.csv", mime="text/csv",key="hll_pre_audit_csv")
        st.caption("Los casos SIN_EVALUACION no son negativos. No se usa POST, "
                   "ni las etiquetas visuales para generar las familias.")




def validate_continuity_export(scales: pd.DataFrame, matrix: pd.DataFrame,
                               cycles: pd.DataFrame, manifest: dict,
                               *, root: Path, audit: pd.DataFrame) -> None:
    """Fail closed if continuity report mismatches the frozen 50-row PRE audit.

    The source hashes prevent displaying a result after changing input files.
    Output hashes also protect against a manually edited/partially exported CSV.
    """
    required_scale = {"attempt_id", "symbol", "level_id", "pre_closed_ts",
        "scale_atr", "status", "latest_recovery_retention_frac",
        "completed_up_down_cycles", "complete_givebacks_last3",
        "net_trough_progress_last3_atr", "preserved_fraction_last3",
        "data_limitations"}
    required_matrix = {"attempt_id", "symbol", "level_id", "pre_closed_ts",
        "family_1p0", "retention_1p0", "preserved_fraction_last3_1p0",
        "completed_cycles_1p0", "structure_review_notes"}
    required_cycle = {"attempt_id", "scale_atr", "cycle_index",
        "giveback_ratio", "left_censored", "full_giveback", "status"}
    if not required_scale.issubset(scales.columns) or not required_matrix.issubset(matrix.columns) \
       or not required_cycle.issubset(cycles.columns):
        raise ValueError("Faltan columnas de continuidad PRE")
    if len(scales) != 150 or len(matrix) != 50 or matrix.attempt_id.duplicated().any():
        raise ValueError("Continuidad necesita exactamente 50 intentos y 150 escalas")
    if not (manifest.get("name") == "HLL_PRE_PHASE_CONTINUITY_V1"
            and manifest.get("attempts") == 50 and manifest.get("scale_rows") == 150
            and manifest.get("reads_post") is False
            and manifest.get("reads_visual_for_metrics") is False
            and manifest.get("modifies_live") is False
            and manifest.get("classifies_good_bad") is False
            and manifest.get("phase_window_bars") == 96):
        raise ValueError("Manifiesto de continuidad no certifica investigación PRE")
    for name, relative in CONTINUITY_SOURCES.items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"Fuente del auditor de continuidad ausente: {name}")
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for part in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(part)
        if h.hexdigest() != manifest.get("source_sha256", {}).get(name):
            raise ValueError(f"Fuente de continuidad modificada: {name}")
    for filename in ("continuity_scales_pre.csv", "continuity_matrix_pre.csv", "continuity_cycles_pre.csv"):
        path = root / CONTINUITY_REL / filename
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for part in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(part)
        if h.hexdigest() != manifest.get("output_sha256", {}).get(filename):
            raise ValueError(f"Exportación de continuidad inconsistente: {filename}")
    if (scales.duplicated(["attempt_id", "scale_atr"]).any()
            or not scales.status.eq("DESCRIPTIVE_PRE_ONLY").all()):
        raise ValueError("Escalas duplicadas o sin carácter descriptivo")
    expected = {(str(a), v) for a in audit.attempt_id for v in (0.5,1.0,1.5)}
    actual = {(str(x.attempt_id), float(x.scale_atr)) for x in scales.itertuples(index=False)}
    if expected != actual or set(matrix.attempt_id) != set(audit.attempt_id):
        raise ValueError("Continuidad y auditoría no cubren las mismas escalas o intentos")
    one = scales.loc[pd.to_numeric(scales.scale_atr).eq(1.0)]
    merged = audit[["attempt_id", "symbol", "level_id", "pre_closed_ts", "family_1p0"]].merge(
        matrix[["attempt_id", "symbol", "level_id", "pre_closed_ts", "family_1p0"]],
        on="attempt_id", validate="one_to_one",suffixes=("_audit", "_continuity"))
    if len(merged)!=50:
        raise ValueError("Cruce incompleto con auditoría anterior")
    for field in ("symbol", "level_id", "pre_closed_ts", "family_1p0"):
        a,b=merged[f"{field}_audit"],merged[f"{field}_continuity"]
        if not a.astype(str).eq(b.astype(str)).all():
            raise ValueError(f"Continuidad cambió {field}")
    merged2 = one[["attempt_id","symbol","level_id","pre_closed_ts",
        "latest_recovery_retention_frac", "preserved_fraction_last3", "completed_up_down_cycles"]].merge(
        matrix[["attempt_id","symbol","level_id","pre_closed_ts","retention_1p0",
        "preserved_fraction_last3_1p0","completed_cycles_1p0"]],
        on="attempt_id",suffixes=("_scale","_matrix"),validate="one_to_one")
    if len(merged2)!=50:
        raise ValueError("Una escala falta del resumen")
    for field in ("symbol","level_id","pre_closed_ts"):
        if not merged2[f"{field}_scale"].astype(str).eq(merged2[f"{field}_matrix"].astype(str)).all():
            raise ValueError(f"La escala 1 ATR no coincide en {field}")
    for a,b in (("latest_recovery_retention_frac","retention_1p0"),
                ("preserved_fraction_last3","preserved_fraction_last3_1p0"),
                ("completed_up_down_cycles","completed_cycles_1p0")):
        x,y = pd.to_numeric(merged2[a],errors="coerce"),pd.to_numeric(merged2[b],errors="coerce")
        if not (x.isna().eq(y.isna()).all() and ((x-y).abs().fillna(0)<1e-7).all()):
            raise ValueError(f"Valor de continuidad distinto entre escalas/matriz: {a}")
    if not cycles.empty:
        if cycles.loc[~cycles.attempt_id.isin(matrix.attempt_id)].shape[0]:
            raise ValueError("Ciclos de otros intentos")
        if not pd.to_numeric(cycles.left_censored,errors="raise").isin([0,1]).all():
            raise ValueError("Censura de ciclos inválida")
        if pd.to_numeric(cycles.giveback_ratio,errors="raise").le(0).any():
            raise ValueError("Cesión de ciclo inválida")
        known = cycles.groupby(["attempt_id","scale_atr"])["left_censored"].apply(
            lambda x: int((pd.to_numeric(x)==0).sum())).to_dict()
        for r in scales.itertuples(index=False):
            if int(r.completed_up_down_cycles) != known.get((r.attempt_id,r.scale_atr),0):
                raise ValueError("El número de ciclos confirmados difiere del detalle")


def render_continuity_audit(root: Path, read_cached, family_export: pd.DataFrame):
    """Additive inspector: does not edit any detector, trading signal or CSV."""
    import streamlit as st
    path = root / CONTINUITY_REL / "continuity_matrix_pre.csv"
    summary_path = root / CONTINUITY_REL / "continuity_scales_pre.csv"
    cycles_path = root / CONTINUITY_REL / "continuity_cycles_pre.csv"
    manifest_path = root / CONTINUITY_REL / "manifest.json"
    with st.expander("🧬 Conservación y deterioro de fases · 50 PRE", expanded=False):
        if not all(x.is_file() for x in (path, summary_path, cycles_path, manifest_path)):
            st.info("Generá este reporte con `python3 hll_pre_phase_continuity_audit_v1.py --plan` "
                    "y después `--run`. No modifica el intérprete ni los CSV anteriores.")
            return None
        try:
            matrix = read_cached(str(path), path.stat().st_mtime_ns)
            scales = read_cached(str(summary_path), summary_path.stat().st_mtime_ns)
            cycles = read_cached(str(cycles_path), cycles_path.stat().st_mtime_ns)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            prior_path = root / AUDIT_REL / "audit_matrix_pre.csv"
            prior_manifest_path = root / AUDIT_REL / "manifest.json"
            prior = read_cached(str(prior_path), prior_path.stat().st_mtime_ns)
            prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
            validate_audit_export(prior, family_export, root=root,manifest=prior_manifest)
            validate_continuity_export(scales,matrix,cycles,manifest,root=root,audit=prior)
        except (OSError, KeyError, ValueError, TypeError, pd.errors.ParserError) as exc:
            st.warning(f"Continuidad PRE no disponible: {exc}")
            return None
        one = scales.loc[pd.to_numeric(scales.scale_atr).eq(1.0)]
        k1,k2,k3,k4=st.columns(4)
        k1.metric("Intentos",len(matrix))
        k2.metric("Con ciclos completos",int(pd.to_numeric(one.completed_up_down_cycles).gt(0).sum()))
        k3.metric("Pérdida neta últimos 3 ciclos",int(pd.to_numeric(one.net_trough_progress_last3_atr).lt(0).sum()))
        k4.metric("Con alguna cesión ≥100%",int(pd.to_numeric(one.complete_givebacks_last3).fillna(0).gt(0).sum()))
        st.caption("Muestra histórica exploratoria. Último LOW confirmado elegido por recencia, nunca por rendimiento. "
                   "Una retención alta del rebote reciente puede coexistir con pérdidas anteriores grandes. "
                   "Los 3 ciclos son los últimos 3 UP→DOWN confirmados, sin incluir provisional ni ciclos censurados.")
        subset = st.selectbox("Inspeccionar continuidad",[
            "Todos los 50", "Pérdida neta últimos 3 ciclos", "Alguna devolución completa",
            "Sin ciclos completos", "Sin evaluación visual"],key="hll_continuity_subset")
        shown=matrix.copy()
        if subset=="Pérdida neta últimos 3 ciclos":
            shown=shown.loc[pd.to_numeric(shown.net_trough_progress_last3_atr_1p0).lt(0)]
        elif subset=="Alguna devolución completa":
            shown=shown.loc[pd.to_numeric(shown.complete_givebacks_last3_1p0).fillna(0).gt(0)]
        elif subset=="Sin ciclos completos":
            shown=shown.loc[pd.to_numeric(shown.completed_cycles_1p0).eq(0)]
        elif subset=="Sin evaluación visual":
            shown=shown.loc[shown.review_visual.eq("SIN_EVALUACION")]
        columns=["symbol","attempt_id","review_visual","family_1p0",
            "retention_1p0","preserved_fraction_last3_1p0",
            "net_trough_progress_last3_atr_1p0","complete_givebacks_last3_1p0",
            "max_giveback_last3_1p0","completed_cycles_1p0","latest_low_age_1p0",
            "data_limitations_1p0"]
        st.caption(f"{len(shown)} casos visibles; filtros para navegación, no entradas ni selección rentable.")
        st.dataframe(shown[columns],hide_index=True,use_container_width=True,height=360)
        for filedata, filename, label, key_name in (
            (matrix,"hll_continuity_50.csv","Exportar matriz 50 intentos","hll_continuity_matrix_download"),
            (scales,"hll_continuity_150.csv","Exportar 150 escalas","hll_continuity_scales_download"),
            (cycles,"hll_continuity_cycles.csv","Exportar ciclos detallados","hll_continuity_cycles_download")):
            st.download_button(label,filedata.to_csv(index=False).encode("utf-8"),
                file_name=filename,mime="text/csv",key=key_name)
        options=[f"{r.symbol} · {r.attempt_id}" for r in matrix.itertuples(index=False)]
        selected=st.selectbox("Comparar un intento en las 3 escalas",options,
                              key="hll_continuity_select_attempt")
        aid=selected.rsplit(" · ",1)[-1]
        per_scale=scales.loc[scales.attempt_id.eq(aid)]
        st.dataframe(per_scale[["scale_atr","latest_confirmed_low_age_bars",
            "latest_recovery_retention_frac","previous_recovery_retention_frac",
            "net_trough_progress_last3_atr","preserved_fraction_last3",
            "complete_givebacks_last3","completed_up_down_cycles","data_limitations"]],
            hide_index=True,use_container_width=True)
        selected_cycles=cycles.loc[cycles.attempt_id.eq(aid) & pd.to_numeric(cycles.scale_atr).eq(1.0)]
        if not selected_cycles.empty:
            st.caption("Ciclos UP→DOWN de 1 ATR; los de borde izquierdo se indican y se excluyen del agregado.")
            st.dataframe(selected_cycles[["cycle_index","up_amplitude_atr",
                "down_amplitude_atr","giveback_ratio","undercut_previous_low",
                "left_censored","down_confirmed_known_closed_ts"]],
                hide_index=True,use_container_width=True,height=250)
        st.caption("Retención = avance conservado desde último LOW / máximo avance desde ese LOW. "
                   "Conservación de últimos 3 ciclos = 1 − suma correcciones/suma subidas. "
                   "Puede ser negativa; NO es probabilidad, rentabilidad ni clasificación buena/mala.")
        return scales,cycles


def _continuity_chart_caption(scales: pd.DataFrame, aid: str, scale: float) -> str | None:
    """Small provenance-aware annotation for the existing PRE candle chart."""
    rows=scales.loc[scales.attempt_id.astype(str).eq(aid) &
                    pd.to_numeric(scales.scale_atr).eq(float(scale))]
    if len(rows)!=1:
        return None
    r=rows.iloc[0]
    def fmt(value,ratio=False):
        v=pd.to_numeric(pd.Series([value]),errors="coerce").iloc[0]
        if pd.isna(v):return "sin datos"
        return f"{float(v)*100:+.1f}%" if ratio else f"{float(v):+.2f} ATR"
    age = ("desconocida" if pd.isna(r.latest_confirmed_low_age_bars)
           else f"{int(r.latest_confirmed_low_age_bars)} velas")
    return (f"Conservación desde último LOW confirmado: {fmt(r.latest_recovery_retention_frac,True)} "
            f"(edad {age}) · "
            f"Conservación últimos 3 ciclos: {fmt(r.preserved_fraction_last3,True)} · "
            f"Progreso neto últimos 3 ciclos: {fmt(r.net_trough_progress_last3_atr)}. "
            "Tramo final provisional no contado; métricas descriptivas PRE.")

def family_brief(record) -> str:
    """Display only: no signal, recommendation, probability or fitted score."""
    return str(record["family_candidate"]).replace("_", " ").capitalize()


def family_evidence_text(value: object, *, fallback: str = "sin observaciones específicas") -> str:
    """Render an optional exported CSV field safely; empty fields become NaN.

    This is presentation only: never replace or modify the frozen PRE data.
    """
    if value is None:
        return fallback
    if not isinstance(value, str) and pd.api.types.is_scalar(value) and pd.isna(value):
        return fallback
    result = str(value).strip()
    return result if result and result.lower() not in {"nan", "none", "<na>"} else fallback


def validate_closed_bars(candles: pd.DataFrame, pre_closed_ts: int, lookback: int = 96) -> pd.DataFrame:
    """Only fully closed, contiguous PRE candles. Never fill a data gap."""
    columns = {"timestamp", "open", "high", "low", "close"}
    if not columns.issubset(candles.columns):
        raise ValueError("OHLC 30m incompleto")
    a = candles.copy()
    a["timestamp"] = pd.to_numeric(a.timestamp, errors="raise").astype("int64")
    if a.timestamp.duplicated().any():
        raise ValueError("Timestamps OHLC duplicados")
    a = a.sort_values("timestamp")
    a = a.loc[a.timestamp + TF_MS <= int(pre_closed_ts)].tail(lookback).copy()
    if len(a) != lookback:
        raise ValueError(f"Cobertura PRE insuficiente: {len(a)}/{lookback}")
    stamps = a.timestamp.tolist()
    if stamps[-1] + TF_MS != int(pre_closed_ts):
        raise ValueError("La última vela cerrada no coincide con corte PRE")
    if any(b - a0 != TF_MS for a0, b in zip(stamps, stamps[1:])):
        raise ValueError("Gap de velas 30m en PRE; no se interpola")
    for k in ["open", "high", "low", "close"]:
        a[k] = pd.to_numeric(a[k], errors="raise")
    if not (a[["open", "high", "low", "close"]] > 0).all().all():
        raise ValueError("OHLC contiene precios no positivos")
    if not ((a.high >= a[["open", "close", "low"]].max(axis=1)) &
            (a.low <= a[["open", "close", "high"]].min(axis=1))).all():
        raise ValueError("OHLC inconsistente")
    return a


def display_chart_window(candles: pd.DataFrame, pre_closed_ts: int, *,
                         pre_bars: int = 96, show_post: bool = False,
                         post_bars: int = 24) -> tuple[pd.DataFrame, dict]:
    """Extra chart context, without recomputing any PRE detector features.

    Require a contiguous 96-bar causal core. Older PRE may be partially
    available (explicitly reported); gaps are never interpolated. POST is
    visual-only, starts at the PRE cutoff, and is limited to 96 bars = 48 h.
    """
    if pre_bars not in PRE_VISUAL_OPTIONS:
        raise ValueError(f"Cantidad PRE no admitida: {pre_bars}")
    if post_bars not in POST_VISUAL_OPTIONS:
        raise ValueError(f"Cantidad POST no admitida: {post_bars}")
    pre_closed_ts = int(pre_closed_ts)
    # The source PRE interpretation was built with these 96 complete bars.
    validate_closed_bars(candles, pre_closed_ts, 96)
    times = pd.to_numeric(candles["timestamp"], errors="raise").astype("int64")
    available_pre = int((times + TF_MS <= pre_closed_ts).sum())
    actual_pre = min(pre_bars, available_pre)
    pre = validate_closed_bars(candles, pre_closed_ts, actual_pre)
    actual_post = 0
    shown = pre.copy()
    if show_post:
        future = candles.loc[times.ge(pre_closed_ts)].copy()
        future["timestamp"] = pd.to_numeric(future["timestamp"], errors="raise").astype("int64")
        future = future.sort_values("timestamp").head(post_bars)
        if not future.empty:
            timestamps = future.timestamp.tolist()
            if timestamps[0] != pre_closed_ts or any(
                b-a != TF_MS for a, b in zip(timestamps, timestamps[1:])
            ):
                raise ValueError("Gap en velas POST; no se interpola ni se salta")
            for col in ("open", "high", "low", "close"):
                future[col] = pd.to_numeric(future[col], errors="raise")
            if not (future[["open", "high", "low", "close"]] > 0).all().all() or not (
                (future.high >= future[["open", "close", "low"]].max(axis=1)) &
                (future.low <= future[["open", "close", "high"]].min(axis=1))
            ).all():
                raise ValueError("OHLC POST inconsistente")
            actual_post = len(future)
            shown = pd.concat([pre, future], ignore_index=True)
    return shown, {
        "pre_requested": pre_bars,
        "pre_shown": len(pre),
        "post_requested": post_bars if show_post else 0,
        "post_shown": actual_post,
        "first_pre_close": float(pre.iloc[0]["close"]),
        "last_pre_close": float(pre.iloc[-1]["close"]),
    }


def _origin_vs_resistance(close: float, zone_low: float, zone_high: float) -> str:
    if close < zone_low:
        return "debajo de la resistencia"
    if close > zone_high:
        return "encima de la resistencia"
    return "dentro de la zona de resistencia"


def _at_scale(table: pd.DataFrame, aid: str, scale: float) -> pd.DataFrame:
    z = table.loc[table.attempt_id.astype(str).eq(aid)]
    return z.loc[(z.scale_atr.astype(float) - scale).abs() < 1e-6].copy()


def valid_pivots(table: pd.DataFrame, aid: str, scale: float, pre: int) -> pd.DataFrame:
    a = _at_scale(table, aid, scale)
    if not len(a):
        return a
    if not {"confirmed_known_closed_ts", "pivot_open_ts", "pivot_type", "pivot_price_close"}.issubset(a.columns):
        raise ValueError("Columnas de pivotes faltantes")
    # Guard remains in the view even if source CSV is changed accidentally.
    a = a.loc[(a.status == "CONFIRMED_PRE") &
              (pd.to_numeric(a.confirmed_known_closed_ts) <= int(pre)) &
              (pd.to_numeric(a.pivot_open_ts) + TF_MS <= int(pre))]
    return a.sort_values("pivot_open_ts")


def valid_legs(table: pd.DataFrame, aid: str, scale: float, pre: int) -> pd.DataFrame:
    a = _at_scale(table, aid, scale)
    if not len(a):
        return a
    fields = {"start_pivot_open_ts", "start_pivot_known_closed_ts", "end_extreme_open_ts", "status"}
    if not fields.issubset(a.columns):
        raise ValueError("Columnas de legs faltantes")
    a = a.loc[(pd.to_numeric(a.start_pivot_known_closed_ts) <= int(pre)) &
              (pd.to_numeric(a.end_extreme_open_ts) + TF_MS <= int(pre)) &
              (a.status.isin(["CONFIRMED_PRE", "PROVISIONAL"]))].copy()
    if "end_extreme_confirmed_known_closed_ts" in a.columns:
        known = pd.to_numeric(a.end_extreme_confirmed_known_closed_ts, errors="coerce")
        a = a.loc[(a.status == "PROVISIONAL") | (known <= int(pre))]
    return a.sort_values("leg_index")


def build_phase_chart(candles: pd.DataFrame, pre_closed_ts: int, zone_low: float, zone_high: float,
                      pivots: pd.DataFrame, legs: pd.DataFrame, *, show_post: bool = False,
                      post_bars: int = 24, pre_bars: int = 96, show_pivots: bool = True,
                      show_legs: bool = True, show_candidates: bool = False,
                      candidates: pd.DataFrame | None = None):
    """Pure Plotly chart; layers use only causally available PRE detections."""
    import plotly.graph_objects as go
    pre_closed_ts = int(pre_closed_ts)
    shown, visual_info = display_chart_window(
        candles, pre_closed_ts, pre_bars=pre_bars,
        show_post=show_post, post_bars=post_bars)
    t = pd.to_datetime(shown.timestamp, unit="ms", utc=True)
    fig = go.Figure(go.Candlestick(x=t, open=shown.open, high=shown.high,
                     low=shown.low, close=shown.close, name="OHLC 30m",
                     increasing_line_color="#169774", decreasing_line_color="#d35e68"))
    fig.add_hrect(y0=float(zone_low), y1=float(zone_high), opacity=0.16,
                  fillcolor="#d6a24c", line_width=0)
    fig.add_hline(y=float(zone_low), line_dash="dot", line_color="#bb8b38", line_width=1)
    fig.add_hline(y=float(zone_high), line_dash="dot", line_color="#bb8b38", line_width=1)
    pre_time = pd.Timestamp(pre_closed_ts, unit="ms", tz="UTC")
    fig.add_vline(x=pre_time, line_dash="dash", line_color="#8b5cf6", line_width=2)
    # Earlier candles are visual context. The frozen detections use only PRE-96.
    if pre_bars > 96:
        core_start = pd.Timestamp(pre_closed_ts - 96 * TF_MS, unit="ms", tz="UTC")
        fig.add_vline(x=core_start, line_dash="dot", line_color="#8290a2", line_width=1)
    if show_legs and not legs.empty:
        for status in ["CONFIRMED_PRE", "PROVISIONAL"]:
            for direction, clr in [("UP", "#278f78"), ("DOWN", "#cf8341")]:
                s = legs.loc[(legs.status == status) & (legs.leg_direction == direction)]
                if s.empty:
                    continue
                xx, yy = [], []
                for r in s.itertuples():
                    xx += [pd.Timestamp(int(r.start_pivot_open_ts), unit="ms", tz="UTC"),
                           pd.Timestamp(int(r.end_extreme_open_ts), unit="ms", tz="UTC"), None]
                    yy += [float(r.start_pivot_price_close), float(r.end_extreme_price_close), None]
                fig.add_trace(go.Scatter(x=xx, y=yy, mode="lines",
                    line=dict(color=clr, width=2.3 if status == "PROVISIONAL" else 1.6,
                              dash="dash" if status == "PROVISIONAL" else "solid"),
                    name=f'{"Provisional" if status == "PROVISIONAL" else "Confirmado"} {direction}',
                    hoverinfo="skip"))
    if show_pivots and not pivots.empty:
        for kind, clr, symbol in [("LOW", "#29a880", "triangle-up"),
                                  ("HIGH", "#e9a356", "triangle-down")]:
            q = pivots.loc[pivots.pivot_type == kind]
            if q.empty:
                continue
            tx = pd.to_datetime(q.pivot_open_ts.astype("int64"), unit="ms", utc=True)
            known = pd.to_datetime(q.confirmed_known_closed_ts.astype("int64"), unit="ms", utc=True)
            hover = [f'Pivote {kind} (retroactivo)<br>Conocido: {dt:%Y-%m-%d %H:%M} UTC'
                     for dt in known]
            fig.add_trace(go.Scatter(x=tx, y=q.pivot_price_close, mode="markers",
                marker=dict(size=9, color=clr, symbol=symbol), name=f"Pivote {kind} confirmado",
                text=hover, hovertemplate="%{text}<br>Precio cierre: %{y}<extra></extra>"))
    if show_candidates and candidates is not None and not candidates.empty:
        q = candidates.loc[pd.to_numeric(candidates.anchor_low_known_closed_ts) <= pre_closed_ts]
        fig.add_trace(go.Scatter(x=pd.to_datetime(q.anchor_low_open_ts.astype("int64"),unit="ms",utc=True),
            y=q.anchor_low_close, mode="markers", name="Anclas candidatas (sin selección)",
            marker=dict(color="#748dec", size=7, symbol="circle-open"),
            hovertemplate="Mínimo candidato: %{y}<extra></extra>"))
    fig.update_layout(height=395, xaxis_rangeslider_visible=False, showlegend=True,
        margin=dict(l=12, r=12, t=24, b=14),
        legend=dict(orientation="h", y=-0.21, font=dict(size=9)),
        xaxis_title="UTC · corte PRE morado", yaxis_title="Precio",
        # Prevent pivots from expanding the requested visual window.
        xaxis=dict(range=[pd.Timestamp(int(shown.timestamp.iloc[0]), unit="ms", tz="UTC"),
                          pd.Timestamp(int(shown.timestamp.iloc[-1]) + TF_MS, unit="ms", tz="UTC")]),
        meta=visual_info)
    return fig


def _review_table(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "attempt_id": a, "revision_visual": label,
                         "origen_revision": origin} for s, a, label, origin in rows])


def render_hll_phase_batch(root) -> None:
    """Render inside existing HLL view (no new dashboard page)."""
    import streamlit as st

    root = Path(root)
    src = _required_sources(root)
    with st.expander("🧩 HLL · Tandas PRE · detecciones de fases (CORE15)", expanded=False):
        st.caption("Mismo inspector HLL. Auditoría de 50 intentos y 3 escalas ATR, solo lectura. "
                   "Las fases NO son una clasificación de calidad ni una señal de trading.")
        absent = [str(p.relative_to(root)) for p in src.values() if not p.is_file()]
        if absent:
            st.info("Falta ejecutar Phase Reconstruction PRE V1 o copiar sus resultados al VPS.")
            st.code("\n".join(absent))
            return

        @st.cache_data(show_spinner=False, max_entries=24)
        def read_cached(path_s: str, mtime_ns: int) -> pd.DataFrame:
            return _read_csv(Path(path_s))

        try:
            data = {name: read_cached(str(path), path.stat().st_mtime_ns) for name, path in src.items()}
            validate_sources(data["summary"], data["levels"])
        except (OSError, KeyError, ValueError, pd.errors.ParserError) as exc:
            st.error(f"No se puede abrir esta auditoría PRE: {exc}")
            return
        family_export = None
        family_path = root / FAMILY_REL / FAMILY_FILE
        manifest_path = root / FAMILY_REL / FAMILY_MANIFEST
        if family_path.is_file() and manifest_path.is_file():
            try:
                fam = read_cached(str(family_path), family_path.stat().st_mtime_ns)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                validate_family_export(fam, data["summary"], root=root, manifest=manifest)
                family_export = fam
            except (OSError, KeyError, ValueError, TypeError, pd.errors.ParserError) as exc:
                st.warning(f"Interpretaciones PRE no disponibles: {exc}. Regenerá el análisis sin alterar Phase V1.")
        else:
            st.caption("Interpretaciones experimentales todavía no generadas. "
                       "Ejecutá `python3 hll_pre_family_interpreter.py --plan` y `--run`. "
                       "Los gráficos Phase V1 siguen disponibles.")
        counts = len(data["summary"]), data["summary"].attempt_id.nunique()
        st.caption(f"Cargados: {counts[1]} intentos, {counts[0]} combinaciones intento × escala. "
                   "Muestra histórica exploratoria: ejemplos cercanos pueden compartir casi todas las velas.")
        group = st.selectbox("Tanda para revisar", list(TANDAS) + [ALL], key="hll_phase_group")
        m1, m2, m3 = st.columns([1, 1, 1])
        with m1:
            scale = st.selectbox("Sensibilidad de swings (ATR)", [0.5, 1.0, 1.5], index=1,
                                 key="hll_phase_scale")
        with m2:
            per_page = st.selectbox("Gráficos por página", [2, 4, 6], index=1,
                                    key="hll_phase_per_page")
        with m3:
            post = st.checkbox("Revelar velas POST", value=False, key="hll_phase_reveal_post")
        b1, b2 = st.columns(2)
        with b1:
            pre_bars = st.selectbox("Velas PRE para visualizar", list(PRE_VISUAL_OPTIONS),
                                    index=PRE_VISUAL_OPTIONS.index(DEFAULT_PRE_VISUAL_BARS),
                                    format_func=lambda n: f"{n} velas · {n // 2} h",
                                    key="hll_phase_pre_bars")
        with b2:
            post_bars = st.selectbox("Velas POST (si se revelan)", list(POST_VISUAL_OPTIONS),
                                     index=POST_VISUAL_OPTIONS.index(DEFAULT_POST_VISUAL_BARS),
                                     format_func=lambda n: f"{n} velas · {n / 2:g} h",
                                     key="hll_phase_post_bars", disabled=not post)
        st.caption("La cantidad PRE y POST cambia SOLO lo visible en las velas. "
                   "Los pivotes, las fases y las familias fueron calculados con 96 velas PRE "
                   "cerradas (48 h) y no se recalculan al cambiar la ventana. "
                   "Si pedís más de 96 PRE, el inicio de esas 96 se marca con línea gris. "
                   "La banda de resistencia se proyecta hacia atrás solo como referencia visual; "
                   "no implica que el nivel estuviera confirmado entonces.")
        c1, c2, c3 = st.columns(3)
        with c1:
            with_pivots = st.checkbox("Pivotes confirmados", value=True, key="hll_phase_pivots")
        with c2:
            with_legs = st.checkbox("Tramos y fase provisional", value=True, key="hll_phase_legs")
        with c3:
            with_candidates = st.checkbox("Todos los mínimos candidatos", value=False,
                                           key="hll_phase_candidates")
        st.caption("Pivotes dibujados retrospectivamente sobre su vela de origen: "
                   "el hover indica cuándo quedaron confirmados. Tramos discontinuos = provisionales en PRE. "
                   "El POST, si se revela, es solo visual y nunca cambia las detecciones.")
        continuity_export = None
        if family_export is not None:
            subset_fam = family_export.loc[family_export.scale_atr.astype(float).eq(float(scale))].copy()
            st.caption("Familias experimentales: describen la geometría de PRE, NO calidad, "
                       "probabilidad ni señal. No se entrenaron con etiquetas visuales. "
                       "El contexto de 48 velas ya fue explorado y no es validación independiente.")
            with st.expander("📊 Resultado del intérprete y estabilidad multiescala", expanded=False):
                counts_fam = subset_fam.family_candidate.value_counts().rename_axis("Familia").reset_index(name="Intentos")
                st.dataframe(counts_fam, hide_index=True, use_container_width=True)
                pivot = family_export.pivot(index="attempt_id", columns="scale_atr", values="family_candidate")
                agree = pivot.nunique(axis=1).eq(1)
                st.caption(f"Escalas idénticas: {int(agree.sum())}/{len(agree)} intentos. "
                           "Los cambios de familia según ATR son parte del resultado, no errores ocultos.")
                st.dataframe(subset_fam[["symbol","attempt_id","family_candidate",
                    "terminal_phase","confirmed_low_progression","confirmed_high_progression",
                    "intercontact_trough_direction_48"]],hide_index=True,use_container_width=True)
            render_audit_matrix(root, read_cached, family_export)
            continuity_export = render_continuity_audit(root, read_cached, family_export)
        if group == ALL:
            metadata = data["summary"].loc[data["summary"].scale_atr.eq(1.0)]
            symbols = sorted(metadata.symbol.unique())
            sym = st.selectbox("Símbolo", ["Todos"] + symbols, key="hll_phase_symbol")
            if sym != "Todos":
                metadata = metadata.loc[metadata.symbol.eq(sym)]
            if family_export is not None:
                families_for_scale = family_export.loc[family_export.scale_atr.astype(float).eq(float(scale))]
                chosen_family = st.selectbox("Familia PRE experimental · solo navegación",
                    ["TODAS"] + sorted(families_for_scale.family_candidate.unique()),
                    key="hll_phase_family_navigation")
                if chosen_family != "TODAS":
                    ids_matching = set(families_for_scale.loc[
                        families_for_scale.family_candidate.eq(chosen_family), "attempt_id"].astype(str))
                    metadata = metadata.loc[metadata.attempt_id.astype(str).isin(ids_matching)]
            metadata = metadata.sort_values(["symbol", "pre_closed_ts", "attempt_id"])
            review_lookup={aid: (label, origin) for entries in TANDAS.values()
                for _,aid,label,origin in entries}
            rows = [(str(r.symbol), str(r.attempt_id),
                     *review_lookup.get(str(r.attempt_id), ("Sin evaluación visual", "Sin etiqueta")))
                    for r in metadata.itertuples()]
        else:
            rows = TANDAS[group]
        if not rows:
            st.info("No hay casos bajo esta selección.")
            return
        n_pages = math.ceil(len(rows) / per_page)
        page = st.number_input(f"Página (1–{n_pages})", min_value=1, max_value=n_pages,
                               value=1, step=1, key=f"hll_phase_page_{group}_{per_page}")
        st.dataframe(_review_table(rows), hide_index=True, use_container_width=True, height=min(320, 34 + len(rows) * 32))
        selected = rows[(int(page) - 1) * per_page:int(page) * per_page]
        for start in range(0, len(selected), 2):
            left, right = st.columns(2)
            for col, item in zip((left, right), selected[start:start+2]):
                symbol, aid, label, origin = item
                with col:
                    st.markdown(f"**{symbol}** · `{aid}`")
                    st.caption(f"Visual: {label} · {origin}")
                    sr = _at_scale(data["summary"], aid, float(scale))
                    levels = data["levels"].loc[data["levels"].attempt_id.astype(str).eq(aid)]
                    if len(sr) != 1 or len(levels) != 1:
                        st.error("Intento ausente o duplicado en fuente PRE")
                        continue
                    r = sr.iloc[0]
                    lev = levels.iloc[0]
                    candle_path = root / CANDLES_REL / f"{symbol}_30m.csv"
                    if not candle_path.is_file():
                        st.warning(f"Sin OHLC: {symbol}")
                        continue
                    try:
                        candles = read_cached(str(candle_path), candle_path.stat().st_mtime_ns)
                        pv = valid_pivots(data["pivots"], aid, float(scale), int(r.pre_closed_ts))
                        lg = valid_legs(data["legs"], aid, float(scale), int(r.pre_closed_ts))
                        cand = _at_scale(data["candidates"], aid, float(scale))
                        fig = build_phase_chart(candles, int(r.pre_closed_ts), float(lev.zone_low),
                             float(lev.zone_high), pv, lg, show_post=post,
                             pre_bars=pre_bars, post_bars=post_bars, show_pivots=with_pivots,
                             show_legs=with_legs, show_candidates=with_candidates, candidates=cand)
                        st.plotly_chart(fig, use_container_width=True, key=f"hll_phase_{aid}_{scale}")
                        info = fig.layout.meta
                        initial_side = _origin_vs_resistance(info["first_pre_close"],
                                                             float(lev.zone_low), float(lev.zone_high))
                        st.caption(f"Origen PRE visible: {initial_side}. "
                                   f"PRE mostrado: {info['pre_shown']}/{info['pre_requested']} velas. "
                                   + (f"POST mostrado: {info['post_shown']}/{info['post_requested']} velas. "
                                      if post else "POST oculto. ")
                                   + "Los datos extra son contexto visual, no nuevas detecciones.")
                        if info["pre_shown"] < info["pre_requested"]:
                            st.info("No existe todo el histórico PRE solicitado para este intento; "
                                    "se muestran solo velas contiguas disponibles, sin rellenar huecos.")
                        if post and info["post_shown"] < info["post_requested"]:
                            st.info("No existe todo el POST solicitado en este CSV; "
                                    "se muestran solo las velas contiguas disponibles.")
                        if continuity_export is not None:
                            continuity_note=_continuity_chart_caption(continuity_export[0],aid,float(scale))
                            if continuity_note:
                                st.caption(continuity_note)
                        tip = str(r.terminal_leg_direction)
                        st.caption(f"Terminal PRE: {tip} PROVISIONAL · "
                                   f"pivotes confirmados: {int(r.confirmed_pivots)} · "
                                   f"mínimos candidatos: {int(r.recovery_candidates)}")
                        if family_export is not None:
                            sel_family = family_export.loc[
                                family_export.attempt_id.astype(str).eq(aid) &
                                family_export.scale_atr.astype(float).eq(float(scale))]
                            if len(sel_family) != 1:
                                st.error("Familia PRE ausente o duplicada para este intento/escala")
                            else:
                                fr = sel_family.iloc[0]
                                st.markdown(f"**Familia PRE candidata:** {family_brief(fr)}")
                                st.caption(f"Motivo: {family_evidence_text(fr.family_basis, fallback='sin motivo registrado')} "
                                           "· sin clasificación buena/mala")
                                with st.expander("Evidencias y contradicciones de la familia", expanded=False):
                                    st.write("**A favor:** " + family_evidence_text(fr.supporting_observations))
                                    st.write("**En contra:** " + family_evidence_text(fr.contradicting_observations))
                                    st.write("**Limitaciones:** " + family_evidence_text(
                                        fr.limitations, fallback="sin limitaciones adicionales"))
                                    same = family_export.loc[family_export.attempt_id.astype(str).eq(aid),
                                        ["scale_atr", "family_candidate", "confirmed_low_progression",
                                         "confirmed_high_progression", "intercontact_trough_direction_48"]]
                                    st.dataframe(same.sort_values("scale_atr"), hide_index=True,
                                                 use_container_width=True)
                        with st.expander("Ver detecciones detalladas", expanded=False):
                            st.dataframe(lg[["leg_index", "leg_role", "status", "amplitude_atr",
                                             "duration_to_extreme_bars"]], hide_index=True,
                                         use_container_width=True)
                            if not cand.empty:
                                cs = ["candidate_index", "anchor_low_open_ts", "age_from_low_bars",
                                      "net_change_from_low_atr", "gap_reduction_atr",
                                      "zone_touch_episodes_since_low", "later_lowest_close_undercut"]
                                st.dataframe(cand[cs], hide_index=True, use_container_width=True)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        st.error(f"No se pudo representar {aid}: {exc}")
        st.caption("Ningún umbral de calidad aplicado. Ninguna modificación de señales, clasificadores o LIVE.")
