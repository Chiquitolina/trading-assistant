#!/usr/bin/env python3
"""Research-only, offline factor snapshots and simultaneous candidate comparisons."""
import argparse
import json
from pathlib import Path
from dashboard.services.reaction_lab_candidate_selection import export_selection


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--shadow-dir", type=Path, required=True)
    p.add_argument("--factors", type=Path, required=True)
    p.add_argument("--market-snapshots", type=Path, nargs="*", default=[])
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(export_selection(args.shadow_dir, args.factors, args.output,
                                      args.market_snapshots), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
