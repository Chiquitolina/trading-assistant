#!/usr/bin/env python3
"""Offline research entry point. No engine/exchange calls; no source DB writes."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from dashboard.services.reaction_lab_portfolio import (
    PortfolioSettings, run_comparison, export_comparison, source_digest)
from dashboard.services.reaction_lab_runner_optimizer import load_live, DEFAULT_COSTS, LIVE_START_MS


def main(argv=None):
    p = argparse.ArgumentParser(description="Reaction Lab portfolio replay (read-only source)")
    p.add_argument("--project", default=str(Path(__file__).resolve().parent))
    p.add_argument("--output-root", default=None)
    p.add_argument("--initial-equity", type=float, default=200)
    p.add_argument("--margin-fraction", type=float, default=.8)
    p.add_argument("--leverage", type=float, default=3)
    p.add_argument("--maker-fee-pct", type=float, default=DEFAULT_COSTS["maker_fee_pct"])
    p.add_argument("--taker-fee-pct", type=float, default=DEFAULT_COSTS["taker_fee_pct"])
    p.add_argument("--taker-slippage-bps", type=float, default=DEFAULT_COSTS["taker_slippage_bps"])
    p.add_argument("--cutoff-ms", type=int, default=LIVE_START_MS)
    p.add_argument("--dry-run", action="store_true", help="Display results, do not write reports")
    args = p.parse_args(argv)
    project = Path(args.project).resolve()
    settings = PortfolioSettings(args.initial_equity, args.margin_fraction, args.leverage).validate()
    costs = {"maker_fee_pct": args.maker_fee_pct,
             "taker_fee_pct": args.taker_fee_pct,
             "taker_slippage_bps": args.taker_slippage_bps}
    records, quality = load_live(project, cutoff_ms=args.cutoff_ms)
    if not records:
        raise SystemExit("No hay eventos LIVE maduros con contexto y paths completos. No se escribe un ledger vacío.")
    outcome = run_comparison(records, settings=settings, costs=costs)
    result = {"quality": quality, "source_digest_sha256": source_digest(records),
              "strategies": {k: v["summary"] for k, v in outcome.items()}}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.dry_run:
        root = Path(args.output_root or project / "reports/reaction_lab/runner_portfolio_v1")
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + result["source_digest_sha256"][:10]
        dest = export_comparison(outcome, records, root / name,
                                 provenance={"project": str(project), "data_quality": quality,
                                             "cutoff_ms": args.cutoff_ms})
        print("\nSaved NEW research run:", dest)
    return result


if __name__ == "__main__":
    main()
