"""Persistent, PRE-only, manually authored blind HLL structure reviews.

This module never reads POST, model predictions or original visual labels.
Reviews are append-only JSON snapshots, kept separately from every HLL audit.
The dashboard reveals model/previous labels only *after* a manual save.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REVIEW_REL = Path("reports/horizontal_level_second_ascent_pre/core15_manual_blind_reviews_v1")
SCHEMA_VERSION = "hll_blind_review_v1"
ATTEMPT_RE = re.compile(r"^[a-f0-9]{24}$")
QUALITY = {
    "CONSTRUCTIVA": "Reconstrucción constructiva",
    "DETERIORADA": "Reconstrucción deteriorada",
    "OSCILACION_REBOTE": "Oscilación o rebote sin construcción clara",
    "DUDOSA": "Dudosa / evidencia insuficiente",
}
MORPHOLOGY = {
    "ASCENSO_ESCALONADO": "Ascenso escalonado hacia resistencia",
    "SEGUNDA_SUBIDA": "Segunda subida tras corrección",
    "BASE_Y_RECUPERACION": "Base y recuperación",
    "RANGO_REBOTES": "Rango con rebotes",
    "OTRA": "Otra estructura",
    "INDETERMINADA": "No se puede determinar",
}
ORIGIN = {
    "DESDE_ABAJO": "Viene principalmente desde abajo",
    "DESDE_ARRIBA": "Viene principalmente desde arriba",
    "DENTRO_ZONA": "Viene del entorno de la zona",
    "MIXTO": "Origen mixto / múltiples transiciones",
    "INDETERMINADO": "No se puede determinar",
}
PREVIOUS_EXPOSURE = {
    "NO_RECUERDO": "No recuerdo haber evaluado este caso",
    "SI": "Sí, ya vi este caso o sus etiquetas",
    "NO_SE": "No estoy seguro",
}
CONFIDENCE = {
    "ALTA": "Alta",
    "MEDIA": "Media",
    "BAJA": "Baja",
}
CSV_FIELDS = (
    "attempt_id", "symbol", "pre_closed_ts", "cohort_sha256", "review_schema",
    "quality", "morphology", "origin", "confidence", "previous_exposure", "pre_bars_shown",
    "review_note", "reviewer", "saved_at_utc", "review_event_id", "blind_at_save",
    "post_visible_at_save", "model_visible_at_save", "historical_labels_visible_at_save",
)


def _cohort_rows(summary: pd.DataFrame, levels: pd.DataFrame) -> list[dict]:
    """One source-anchored item per PRE attempt; 3 complete ATR scales required."""
    req = {"attempt_id", "symbol", "pre_closed_ts", "scale_atr", "status"}
    zone = {"attempt_id", "symbol", "pre_closed_ts", "zone_low", "zone_high"}
    if not req.issubset(summary) or not zone.issubset(levels):
        raise ValueError("Fuente PRE incompleta para revisión ciega")
    if summary.duplicated(["attempt_id", "scale_atr"]).any():
        raise ValueError("Intentos/escalas duplicados")
    if not summary.status.eq("COMPLETE_PRE").all():
        raise ValueError("Fases PRE incompletas")
    if levels.attempt_id.duplicated().any():
        raise ValueError("Zonas PRE duplicadas")
    grouped = summary.groupby("attempt_id", sort=False)
    if len(grouped) != 50 or len(levels) != 50 or len(summary) != 150:
        raise ValueError("La cohorte ciega debe contener 50 intentos y 150 escalas")
    for aid, g in grouped:
        if not ATTEMPT_RE.fullmatch(str(aid)):
            raise ValueError("Attempt ID inválido")
        if set(pd.to_numeric(g.scale_atr)) != {0.5, 1.0, 1.5}:
            raise ValueError("Escalas ATR incompletas en cohorte")
        if g[["symbol", "pre_closed_ts"]].drop_duplicates().shape[0] != 1:
            raise ValueError("Símbolo/corte PRE inconsistente entre escalas")
    one = summary.loc[pd.to_numeric(summary.scale_atr).eq(1.0),
                      ["attempt_id", "symbol", "pre_closed_ts"]]
    joined = one.merge(levels[list(zone)], on="attempt_id", validate="one_to_one",
                       suffixes=("_phase", "_zone"), how="outer", indicator=True)
    if not joined._merge.eq("both").all() or len(joined) != 50:
        raise ValueError("Intentos de zona y fase no coinciden")
    if not joined.symbol_phase.astype(str).eq(joined.symbol_zone.astype(str)).all():
        raise ValueError("Símbolo incompatible con zona PRE")
    if not pd.to_numeric(joined.pre_closed_ts_phase).eq(pd.to_numeric(joined.pre_closed_ts_zone)).all():
        raise ValueError("Corte incompatible con zona PRE")
    rows=[]
    for r in joined.itertuples(index=False):
        lo, hi = float(r.zone_low), float(r.zone_high)
        if not (0 < lo < hi):
            raise ValueError("Zona de resistencia inválida")
        rows.append({"attempt_id":str(r.attempt_id), "symbol":str(r.symbol_phase),
                     "pre_closed_ts":int(r.pre_closed_ts_phase),
                     "zone_low":lo, "zone_high":hi})
    # Deterministic, symbol-independent order to reduce grouped-label priming.
    return sorted(rows, key=lambda x: hashlib.sha256(
        (SCHEMA_VERSION + ":" + x["attempt_id"]).encode("ascii")).hexdigest())


def cohort_sha256(rows: list[dict]) -> str:
    material = [{"attempt_id":r["attempt_id"], "symbol":r["symbol"],
                 "pre_closed_ts":r["pre_closed_ts"], "zone_low":r["zone_low"],
                 "zone_high":r["zone_high"]} for r in sorted(rows,key=lambda r:r["attempt_id"])]
    encoded=json.dumps(material,sort_keys=True,ensure_ascii=False,separators=(",", ":")).encode("utf8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_review(record: dict, by_id: dict[str, dict], cohort_hash: str) -> None:
    aid=record.get("attempt_id")
    if aid not in by_id or not ATTEMPT_RE.fullmatch(str(aid)):
        raise ValueError("Attempt ID fuera de la cohorte PRE")
    original=by_id[aid]
    if (record.get("symbol") != original["symbol"] or
        record.get("pre_closed_ts") != original["pre_closed_ts"] or
        record.get("cohort_sha256") != cohort_hash or
        record.get("review_schema") != SCHEMA_VERSION):
        raise ValueError("Revisión no coincide con cohorte/corte PRE congelados")
    for key, options in (("quality",QUALITY),("morphology",MORPHOLOGY),
                         ("origin",ORIGIN),("confidence",CONFIDENCE),("previous_exposure",PREVIOUS_EXPOSURE)):
        if record.get(key) not in options:
            raise ValueError(f"Valor de revisión inválido: {key}")
    if record.get("pre_bars_shown") not in (48,96,144,192,288,384):
        raise ValueError("Ventana visual PRE inválida")
    if not isinstance(record.get("review_note"),str) or len(record["review_note"]) > 1200:
        raise ValueError("Nota inválida o demasiado extensa")
    if record.get("reviewer") != "MANUAL_HLL":
        raise ValueError("Procedencia de revisión inválida")
    if (record.get("blind_at_save") is not True or
        record.get("post_visible_at_save") is not False or
        record.get("model_visible_at_save") is not False or
        record.get("historical_labels_visible_at_save") is not False):
        raise ValueError("Revisión no es ciega o contiene información POST")
    if not re.fullmatch(r"[a-f0-9]{32}",str(record.get("review_event_id",""))):
        raise ValueError("Identificador de revisión inválido")
    if not isinstance(record.get("saved_at_utc"),str):
        raise ValueError("Fecha de revisión ausente")
    try:
        dt=datetime.fromisoformat(record["saved_at_utc"].replace("Z","+00:00"))
        if dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
            raise ValueError("Fecha sin UTC")
    except (TypeError, ValueError) as exc:
        raise ValueError("Fecha de revisión inválida") from exc


def make_review(rows: list[dict], *, attempt_id: str, quality: str, morphology: str,
                origin: str, confidence: str, previous_exposure: str, pre_bars_shown: int, note: str,
                post_visible: bool = False, model_visible: bool = False,
                historical_labels_visible: bool = False) -> dict:
    """Create strictly blind manual event; reject any exposed future or predictions."""
    if post_visible or model_visible or historical_labels_visible:
        raise ValueError("No se pueden guardar revisiones ciegas tras revelar POST o etiquetas")
    originals={x["attempt_id"]:x for x in rows}
    if attempt_id not in originals:
        raise ValueError("Intento desconocido")
    rec={"attempt_id":attempt_id, "symbol":originals[attempt_id]["symbol"],
         "pre_closed_ts":originals[attempt_id]["pre_closed_ts"],
         "cohort_sha256":cohort_sha256(rows),"review_schema":SCHEMA_VERSION,
         "quality":quality,"morphology":morphology,"origin":origin,
         "confidence":confidence,"previous_exposure":previous_exposure,"pre_bars_shown":int(pre_bars_shown),
         "review_note":note.strip(),"reviewer":"MANUAL_HLL",
         "saved_at_utc":datetime.now(timezone.utc).isoformat(timespec="microseconds"),
         "review_event_id":uuid.uuid4().hex,
         "blind_at_save":True,"post_visible_at_save":False,
         "model_visible_at_save":False,"historical_labels_visible_at_save":False}
    _validate_review(rec, originals, cohort_sha256(rows))
    return rec


def save_event(root: Path, record: dict, rows: list[dict]) -> Path:
    """Append-only write to a dedicated directory; existing research is untouched."""
    _validate_review(record,{x["attempt_id"]:x for x in rows},cohort_sha256(rows))
    base=Path(root) / REVIEW_REL / "events"
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    filename=f"{record['attempt_id']}_{record['review_event_id']}.json"
    destination=base/filename
    raw=(json.dumps(record,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode("utf8")
    # Exclusive creation; no overwrite even if two processes click simultaneously.
    fd=os.open(destination,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def load_events(root: Path, rows: list[dict]) -> list[dict]:
    base=Path(root)/REVIEW_REL/"events"
    if not base.exists():
        return []
    by_id={x["attempt_id"]:x for x in rows}
    hash_id=cohort_sha256(rows)
    events=[]
    for p in sorted(base.glob("*.json")):
        if p.is_symlink():
            raise ValueError("No se aceptan enlaces simbólicos en revisiones")
        try:
            record=json.loads(p.read_text(encoding="utf8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"Archivo de revisión ilegible: {p.name}") from exc
        if not isinstance(record,dict) or p.name != f"{record.get('attempt_id')}_{record.get('review_event_id')}.json":
            raise ValueError(f"Nombre o contenido de revisión inconsistente: {p.name}")
        _validate_review(record,by_id,hash_id)
        events.append(record)
    return events


def latest_reviews(events: list[dict]) -> dict[str, dict]:
    result={}
    for e in sorted(events,key=lambda e:(e["saved_at_utc"],e["review_event_id"])):
        result[e["attempt_id"]]=e
    return result


def review_frame(events: list[dict], *, latest_only: bool = True) -> pd.DataFrame:
    selected=list(latest_reviews(events).values()) if latest_only else events
    return pd.DataFrame([{k:e.get(k) for k in CSV_FIELDS} for e in selected],columns=list(CSV_FIELDS))
