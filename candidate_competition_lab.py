#!/usr/bin/env python3
"""Phase-1 Candidate Competition Lab: read-only frozen ledger attribution."""
import argparse
import json
from pathlib import Path
from dashboard.services.reaction_lab_competition import export_readonly


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True, help="Original frozen portfolio run")
    p.add_argument("--output", type=Path, required=True, help="NEW output folder; overwrite forbidden")
    p.add_argument("--events", type=Path, help="Optional original merged replay_events.csv")
    p.add_argument("--factors", type=Path, help="Optional original historical_factors.csv")
    args = p.parse_args()
    result = export_readonly(args.run_dir, args.output,
                             events_path=args.events, factors_path=args.factors)
    print(json.dumps({"output": str(args.output), "strategies": result["strategies"],
                      "coverage": result["coverage"], "ttl_source": result["ttl_source"]},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
