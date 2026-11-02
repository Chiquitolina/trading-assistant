#!/usr/bin/env python3
"""HLL — huella estructural DINAMICA PRE V1.

Reconstruye una huella de 35 variables para cada CROSS_UP de un episodio.
La comparacion PRINCIPAL es siempre el corte seleccionado frente al #1 y el
inmediato anterior. El modo CAUSAL oculta todas las huellas posteriores al
corte elegido; el modo HISTORICO puede mostrar cortes posteriores pero queda
marcado expresamente como retrospectivo.

NO genera senales, calidad, probabilidad, POST, PnL ni atributos de un cruce futuro.
Para la vista dinamica de Discovery el caller suministra miembros del MISMO
Episodio y solo velas 30m originales. Los datos incompletos NO se imputan.

Tambien permite reproducir una galeria HTML sobre los 15 episodios de referencia:
  python3 hll_structural_fingerprint_evolution_v1.py --plan
  python3 hll_structural_fingerprint_evolution_v1.py --run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import hll_structural_fingerprint_v1 as fp

VERSION = "HLL_STRUCTURAL_FINGERPRINT_EVOLUTION_PRE_V1"
BAR_MS = fp.anatomy.BAR_MS
OUT = Path("reports/horizontal_level_structural_fingerprint_evolution_v1/reference_15_v1")
REQUIRED = ("attempt_id", "symbol", "episode_id", "level_id", "episode_ordinal",
            "pre_closed_ts", "atr_pre", "zone_low", "zone_high")


def _good_number(value):
    try:
        number = float(value)
        return math.isfinite(number)
    except (TypeError, ValueError, OverflowError):
        return False


def validate_episode(members):
    """Validated ordered episode. A full episode catalog is necessary for #1.

    The full catalog can be retrospective, but the fingerprint engine always
    truncates by the selected cut BEFORE computing features in CAUSAL mode.
    """
    if not members:
        raise ValueError("Episodio vacio")
    rows = [dict(row) for row in members]
    for row in rows:
        missing = set(REQUIRED).difference(row)
        if missing:
            raise ValueError("Columnas faltantes del episodio: " + ", ".join(sorted(missing)))
        for key in ("attempt_id", "symbol", "episode_id", "level_id"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"ID faltante en {key}")
        if not all(_good_number(row[k]) for k in ("pre_closed_ts", "atr_pre", "zone_low", "zone_high")):
            raise ValueError("ATR, zona o corte PRE no numerico")
        row["pre_closed_ts"] = int(row["pre_closed_ts"])
        row["episode_ordinal"] = int(row["episode_ordinal"])
        row["atr_pre"] = float(row["atr_pre"])
        row["zone_low"] = float(row["zone_low"])
        row["zone_high"] = float(row["zone_high"])
        if (row["pre_closed_ts"] <= 0 or row["pre_closed_ts"] % BAR_MS
            or row["episode_ordinal"] < 1 or row["atr_pre"] <= 0
            or row["zone_low"] <= 0 or row["zone_high"] < row["zone_low"]):
            raise ValueError("Metadata de corte PRE invalida")
    if len({r["attempt_id"] for r in rows}) != len(rows):
        raise ValueError("Attempt ID repetido")
    if len({(r["symbol"], r["episode_id"], r["level_id"]) for r in rows}) != 1:
        raise ValueError("Se mezclaron simbolos, niveles o episodios")
    rows.sort(key=lambda r: r["episode_ordinal"])
    if [r["episode_ordinal"] for r in rows] != list(range(1, len(rows)+1)):
        raise ValueError("Ordinales no consecutivos; falta el primer intento o algun corte")
    if any(b["pre_closed_ts"] <= a["pre_closed_ts"] for a, b in zip(rows, rows[1:])):
        raise ValueError("Los cortes PRE deben crecer estrictamente en UTC")
    return rows


def _empty_snapshot(meta, scale, reason):
    return {
        "fingerprint_version": fp.VERSION,
        "symbol": meta["symbol"], "episode_id": meta["episode_id"],
        "attempt_id": meta["attempt_id"], "level_id": meta["level_id"],
        "pre_closed_ts": meta["pre_closed_ts"],
        "episode_ordinal_report_only": meta["episode_ordinal"],
        "atr_pre_report_only": meta["atr_pre"],
        "zone_low_report_only": meta["zone_low"],
        "zone_high_report_only": meta["zone_high"],
        "scale_atr": scale, "status": reason,
        "observed_features": 0, "total_features": fp.NUM_FEATURES,
        **{f"coverage_{family}": f"0/{sum(meta[0]==family for meta in fp.FEATURES.values())}" for family in fp.FAMILIES},
        **{key: None for key in fp.FEATURES},
    }


def build_timeline(members, candles, selected_attempt_id, scale_atr=1.0, mode="CAUSAL"):
    """Return snapshots and the selected cut. No features see later attempts.

    mode=CAUSAL: compute only ordinal <= selected; no future rows returned.
    mode=HISTORICAL: compute all attempts, each with its OWN causal PRE window.
    """
    if mode not in ("CAUSAL", "HISTORICAL"):
        raise ValueError("Modo desconocido")
    if scale_atr not in (0.5, 1.0, 1.5):
        raise ValueError("Escala ATR desconocida")
    ordered = validate_episode(members)
    selected = next((r for r in ordered if r["attempt_id"] == selected_attempt_id), None)
    if selected is None:
        raise ValueError("El Attempt ID seleccionado no pertenece al episodio")
    chosen_ord = selected["episode_ordinal"]
    visible = ordered if mode == "HISTORICAL" else ordered[:chosen_ord]
    snapshots = []
    for meta in visible:
        pre = meta["pre_closed_ts"]
        b96 = fp.anatomy.take_pre(candles, pre, 96)
        b288 = fp.anatomy.take_pre(candles, pre, 288)
        if b96 is None or b288 is None:
            reason = "INCOMPLETE_PRE_96" if b96 is None else "INCOMPLETE_PRE_288"
            row = _empty_snapshot(meta, scale_atr, reason)
        elif meta.get("status") not in (None, "", "COMPLETE_PRE"):
            row = _empty_snapshot(meta, scale_atr, "SOURCE_PRE_NOT_COMPLETE")
        else:
            row = fp.build_fingerprint(meta, b96, b288, scale_atr)
        row["is_future_relative_to_selected"] = (meta["episode_ordinal"] > chosen_ord)
        row["evolution_version"] = VERSION
        snapshots.append(row)
    # Report just the historical number of attempts as browsing metadata, NEVER a feature.
    return {
        "version": VERSION, "mode": mode, "selected_attempt_id": selected_attempt_id,
        "selected_ordinal": chosen_ord, "total_episode_attempts_report_only": len(ordered),
        "symbol": selected["symbol"], "episode_id": selected["episode_id"],
        "snapshots": snapshots,
    }


def get_comparison(evolution):
    """Return selected, #1 and immediately prior rows; no silent skip of gaps."""
    seen = {int(r["episode_ordinal_report_only"]): r for r in evolution["snapshots"]}
    ord_ = evolution["selected_ordinal"]
    return {"first": seen[1], "previous": seen.get(ord_-1) if ord_>1 else None,
            "selected": seen[ord_]}


def compare_features(evolution):
    """All 35 descriptor deltas. Numeric sign is NOT a quality or maturity vote."""
    reference = get_comparison(evolution)
    first, prev, selected = reference["first"], reference["previous"], reference["selected"]
    rows = []
    for key, (family, unit, definition, window, method) in fp.FEATURES.items():
        current = selected[key]
        earliest = first[key]
        previous = prev[key] if prev is not None else None
        rows.append({
            "family_id": family, "family": fp.FAMILIES[family],
            "feature_id": key, "unit": unit, "definition": definition,
            "first_value": earliest, "previous_value": previous,
            "selected_value": current,
            "delta_vs_first": (current-earliest if current is not None and earliest is not None else None),
            "delta_vs_previous": (current-previous if current is not None and previous is not None else None),
            "first_available": earliest is not None,
            "previous_available": prev is not None and previous is not None,
            "selected_available": current is not None,
        })
    return rows


def timeline_table(evolution):
    return [{
        "attempt_number": r["episode_ordinal_report_only"],
        "attempt_id": r["attempt_id"],
        "pre_closed_ts": r["pre_closed_ts"],
        "hours_since_first": round((r["pre_closed_ts"]-evolution["snapshots"][0]["pre_closed_ts"])/3_600_000, 2),
        "status": r["status"],
        "features_available": r["observed_features"],
        "features_total": r["total_features"],
        "future_vs_selected": r["is_future_relative_to_selected"],
    } for r in evolution["snapshots"]]


def csv_safe(val):
    return "" if val is None else val


def write_csv(path, rows):
    if not rows:
        raise ValueError("CSV sin filas")
    with Path(path).open("x", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), extrasaction="raise")
        w.writeheader()
        w.writerows([{k: csv_safe(v) for k, v in row.items()} for row in rows])


def _utc(ts):
    return datetime.fromtimestamp(int(ts)/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def html_gallery(evolutions):
    """Standalone browser preview. Uses real measured data, no external packages."""
    payload = []
    for evo in evolutions:
        rows = evo["snapshots"]
        payload.append({"symbol":evo["symbol"], "episode_id":evo["episode_id"],
                        "snapshots": [{**{k:r[k] for k in ("attempt_id", "pre_closed_ts", "episode_ordinal_report_only", "status", "observed_features")},
                                       **{k:r[k] for k in fp.FEATURES}} for r in rows]})
    catalog = {k: {"family":f,"unit":u,"definition":d} for k,(f,u,d,w,m) in fp.FEATURES.items()}
    families = fp.FAMILIES
    dataset = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    feature_json = json.dumps(catalog, ensure_ascii=False).replace("</", "<\\/")
    families_json = json.dumps(families, ensure_ascii=False)
    return '''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>HLL · Huella Estructural Dinámica</title>
<style>body{background:#101820;color:#eff3f6;font:14px/1.5 system-ui,-apple-system,sans-serif;max-width:1150px;margin:32px auto;padding:0 22px}h1{font-size:27px;margin-bottom:4px}.muted{color:#a4b7c4}.note{background:#222b32;border-left:3px solid #d2a85d;padding:12px 15px;margin:17px 0}select,input{background:#1b2b35;color:#eaf5fa;border:1px solid #547281;border-radius:7px;padding:9px;max-width:100%}label{display:block;margin-bottom:4px;font-size:13px;color:#b8cdd8}.controls{display:flex;gap:15px;align-items:end;flex-wrap:wrap;margin:25px 0}.controls>div{min-width:180px;flex:1}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #314451;vertical-align:top}th{color:#c8dce6}tr.selected{background:#204c48}tr.future{color:#c7a26d}.panel{border:1px solid #375361;border-radius:11px;padding:15px;margin:17px 0;overflow:auto}code{font-size:11px;color:#e5ca88;overflow-wrap:anywhere}small{color:#a9bbc4}.numbers{display:flex;gap:18px;flex-wrap:wrap}.numbers>div{background:#20313a;padding:10px 14px;border-radius:8px}svg{width:100%;height:210px}.empty{color:#d7ae70}</style></head><body>
<h1>HLL · Huella Estructural Dinámica</h1><p class="muted">Datos reales · 35 descriptores por intento · 6 familias · sin POST ni nota global.</p>
<div class="note">Modo causal: solo cortes #1…#N. Histórico: permite ver los posteriores al seleccionado, marcados como retrospectivos. Comparar valores/deltas NO equivale a evaluar calidad ni predice una ruptura. Los huecos son datos insuficientes.</div>
<div class="controls"><div><label>Referencia</label><select id="symbol"></select></div><div><label>Corte elegido</label><select id="cut"></select></div><div><label>Visualización</label><select id="mode"><option value="CAUSAL">Hasta el corte elegido (causal)</option><option value="HISTORICAL">Todo el episodio (retrospectivo)</option></select></div></div>
<div id="header"></div><section class="panel"><h3>Línea de tiempo</h3><div id="timeline"></div></section>
<div class="controls"><div><label>Familia</label><select id="family"></select></div><div><label>Variable para la evolución</label><select id="feature"></select></div></div>
<section class="panel"><h3 id="charttitle">Evolución de variable</h3><div id="chart"></div></section><section class="panel"><h3>Comparación: #1 · anterior · seleccionado</h3><div id="comparison"></div></section>
<script>
const DATA=__DATA__, FEATURES=__FEATURES__, FAMILIES=__FAMILIES__;
const byId=id=>document.getElementById(id), select=(id,options,show)=>{const el=byId(id);el.innerHTML='';for(const x of options){const opt=document.createElement('option');opt.value=x;opt.textContent=show(x);el.append(opt)}return el};
const fmt=n=>n===null||n===undefined||!Number.isFinite(n)?'—':Number(n).toLocaleString('es-AR',{maximumFractionDigits:4});
const date=ts=>new Date(ts).toISOString().slice(0,16).replace('T',' ')+' UTC';
const clean=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function current(){return DATA.find(x=>x.episode_id===byId('symbol').value)||DATA[0]}
function redraw(){const e=current(),cut=+byId('cut').value,mode=byId('mode').value,visible=e.snapshots.filter(x=>mode==='HISTORICAL'||x.episode_ordinal_report_only<=cut);
const first=e.snapshots[0],now=e.snapshots.find(x=>x.episode_ordinal_report_only===cut),prev=e.snapshots.find(x=>x.episode_ordinal_report_only===cut-1);
byId('header').innerHTML='<div class="numbers"><div><b>'+clean(e.symbol)+'</b><br><small>'+clean(e.episode_id)+'</small></div><div><b>Intento #'+cut+' / '+e.snapshots.length+'</b><br><small>'+date(now.pre_closed_ts)+'</small></div><div><b>'+now.observed_features+' / '+Object.keys(FEATURES).length+'</b><br><small>variables disponibles</small></div></div>';
let t='<table><thead><tr><th>Intento</th><th>PRE UTC</th><th>Attempt ID</th><th>Disponibles</th></tr></thead><tbody>';for(let x of visible){const cls=x.episode_ordinal_report_only>cut?'future':x.episode_ordinal_report_only===cut?'selected':'';t+='<tr class="'+cls+'"><td>#'+x.episode_ordinal_report_only+(cls==='future'?' · posterior':'')+'</td><td>'+date(x.pre_closed_ts)+'</td><td><code>'+clean(x.attempt_id)+'</code></td><td>'+x.observed_features+'</td></tr>'}byId('timeline').innerHTML=t+'</tbody></table>';
const family=byId('family').value, keys=Object.keys(FEATURES).filter(k=>FEATURES[k].family===family);const wanted=byId('feature').value;
select('feature',keys,k=>k);byId('feature').value=keys.includes(wanted)?wanted:keys[0];const feature=byId('feature').value;
byId('charttitle').textContent='Evolución: '+feature+' ('+FEATURES[feature].unit+')';
const values=visible.map(x=>x[feature]).filter(x=>typeof x==='number'&&Number.isFinite(x));let svg='';if(values.length){let lo=Math.min(...values),hi=Math.max(...values);if(hi===lo){lo-=1;hi+=1}const minT=visible[0].pre_closed_ts,maxT=visible.at(-1).pre_closed_ts;
const x=x=>50+((x.pre_closed_ts-minT)/(maxT-minT||1))*830;const y=v=>165-(v-lo)/(hi-lo)*130;
svg='<svg viewBox="0 0 930 210" role="img" aria-label="Gráfico de evolución por tiempo UTC"><path d="M50 18V165H880" stroke="#587789" fill="none"/><text x="4" y="32" fill="#adbec9" font-size="12">'+fmt(hi)+'</text><text x="4" y="165" fill="#adbec9" font-size="12">'+fmt(lo)+'</text>';let line='';for(let i=0;i<visible.length;i++){let z=visible[i],v=z[feature];if(typeof v!=='number'){line='';continue}let xx=x(z),yy=y(v);if(line){svg+='<path d="M'+line+' L'+xx.toFixed(1)+' '+yy.toFixed(1)+'" stroke="#46bfa5" stroke-width="2.5" fill="none"/>'}line=xx.toFixed(1)+' '+yy.toFixed(1);svg+='<circle cx="'+xx+'" cy="'+yy+'" r="5" fill="'+(z.episode_ordinal_report_only>cut?'#d6ae66':'#46bfa5')+'"><title>Intento #'+z.episode_ordinal_report_only+' · '+date(z.pre_closed_ts)+' · '+fmt(v)+'</title></circle>'}svg+='</svg><p class="muted">Posición horizontal según horas reales entre intentos; no presupone intervalos iguales.</p>'}else svg='<p class="empty">Ningún corte tiene esta variable disponible.</p>';byId('chart').innerHTML=svg;
let cmp='<p class="muted">'+clean(FEATURES[feature].definition)+'</p><table><tr><th>Variable</th><th>#1</th><th>Anterior</th><th>Seleccionado</th><th>Δ vs #1</th><th>Δ vs anterior</th><th>Unidad</th></tr>';
for(const k of keys){const a=first[k],b=prev?.[k]??null,c=now[k],d1=typeof a==='number'&&typeof c==='number'?c-a:null,d2=typeof b==='number'&&typeof c==='number'?c-b:null;cmp+='<tr><td title="'+clean(FEATURES[k].definition)+'"><code>'+clean(k)+'</code></td><td>'+fmt(a)+'</td><td>'+fmt(b)+'</td><td>'+fmt(c)+'</td><td>'+fmt(d1)+'</td><td>'+fmt(d2)+'</td><td>'+clean(FEATURES[k].unit)+'</td></tr>'}byId('comparison').innerHTML=cmp+'</table><p class="muted">Δ = diferencia aritmética en unidades originales. Ni positivo ni negativo implica mejora; ATR y ventana pueden cambiar entre cortes.</p>';
}
const sym=select('symbol',DATA.map(x=>x.episode_id),id=>DATA.find(x=>x.episode_id===id).symbol+' · '+id.slice(0,10));
function updateCut(){const e=current();select('cut',e.snapshots.map(x=>String(x.episode_ordinal_report_only)),id=>'#'+id+' · '+date(e.snapshots[+id-1].pre_closed_ts));byId('cut').value=String(e.snapshots.length);redraw()}
select('family',Object.keys(FAMILIES),k=>FAMILIES[k]);sym.addEventListener('change',updateCut);byId('cut').addEventListener('change',redraw);byId('family').addEventListener('change',redraw);byId('feature').addEventListener('change',redraw);byId('mode').addEventListener('change',redraw);updateCut();
</script></body></html>'''.replace('__DATA__', dataset).replace('__FEATURES__', feature_json).replace('__FAMILIES__', families_json)


def reference_run(source_zip, full_map, scale):
    # The reference fingerprint V1 had only 40 selected/recovered cuts.
    # The authoritative FULL episode map permits recovering EVERY intermediate
    # attempt for the 15 same episodes, without assigning human grades to #1.
    first, studied_rows, candles, source_hash, map_hash = fp.load_reference_inputs(source_zip, full_map)
    wanted = {r["episode_id"] for r in first}
    full = defaultdict(list)
    with Path(full_map).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not set(REQUIRED).issubset(reader.fieldnames):
            raise ValueError("Mapa completo de episodios invalido")
        for row in reader:
            if row["episode_id"] in wanted:
                full[row["episode_id"]].append(row)
    if set(full) != wanted:
        raise ValueError("No se encontraron los 15 episodios completos")
    studied_ids = {r["attempt_id"] for r in studied_rows}
    complete_ids = {r["attempt_id"] for rows in full.values() for r in rows}
    if not studied_ids <= complete_ids:
        raise ValueError("La muestra historica no coincide con el mapa completo")
    evolutions = []
    for eid, members in sorted(full.items(), key=lambda item:item[1][0]["symbol"]):
        seq = validate_episode(members)
        evolutions.append(build_timeline(seq,candles[seq[0]["symbol"]],seq[-1]["attempt_id"],scale,"CAUSAL"))
    return evolutions, source_hash, map_hash


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    p.add_argument("--source-zip", type=Path, default=fp.DEFAULT_ZIP)
    p.add_argument("--full-map", type=Path, default=fp.DEFAULT_MAP)
    p.add_argument("--output", type=Path, default=OUT)
    p.add_argument("--scale-atr", type=float, choices=(0.5,1.0,1.5), default=1.0)
    args = p.parse_args(argv)
    if args.run and args.output.exists():
        raise SystemExit("NO_OVERWRITE: " + str(args.output))
    evolutions, source_hash, map_hash = reference_run(args.source_zip,args.full_map,args.scale_atr)
    snapshots = [r for e in evolutions for r in e["snapshots"]]
    plan = {"version":VERSION,"episodes":len(evolutions), "cuts":len(snapshots),
            "features_per_cut":fp.NUM_FEATURES, "scale_atr":args.scale_atr,
            "reads_post":False, "uses_review_labels":False,"modifies_live":False,
            "output":str(args.output),"source_zip_sha256":source_hash,
            "episode_map_sha256":map_hash}
    print("HLL_FINGERPRINT_EVOLUTION_PLAN",json.dumps(plan,ensure_ascii=False),flush=True)
    if args.plan: return
    comparisons=[]
    timeline=[]
    for e in evolutions:
        for row in timeline_table(e):
            timeline.append({"symbol":e["symbol"],"episode_id":e["episode_id"],**row})
        for row in compare_features(e):
            comparisons.append({"symbol":e["symbol"],"episode_id":e["episode_id"],
                                "selected_attempt_id":e["selected_attempt_id"],**row})
    # All validations and calculations finish before output directory is created.
    args.output.mkdir(parents=True,exist_ok=False)
    write_csv(args.output/"fingerprint_timeline_pre.csv",snapshots)
    write_csv(args.output/"episode_timeline_index.csv",timeline)
    write_csv(args.output/"latest_vs_first_and_previous.csv",comparisons)
    (args.output/"dynamic_fingerprints_preview_pre.html").write_text(html_gallery(evolutions),encoding="utf8")
    (args.output/"feature_catalog.json").write_text(json.dumps(fp.feature_catalog(),ensure_ascii=False,indent=2),encoding="utf8")
    manifest={**plan,"status":"DONE","available_feature_cells":sum(r["observed_features"] for r in snapshots),
              "not_a_quality_score":True,"future_rows_in_causal_mode":0,
              "attempt_ids_sha256":hashlib.sha256("\n".join(r["attempt_id"] for r in snapshots).encode()).hexdigest()}
    (args.output/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    print("HLL_FINGERPRINT_EVOLUTION_DONE",json.dumps({"status":"DONE","episodes":len(evolutions),
        "cuts":len(snapshots),"available_feature_cells":manifest["available_feature_cells"],
        "output":str(args.output)},ensure_ascii=False),flush=True)


if __name__=="__main__":
    main()
