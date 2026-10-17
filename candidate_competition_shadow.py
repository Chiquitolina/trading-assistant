    #!/usr/bin/env python3
"""Offline read-only shadow generator. No live-market or exchange interactions."""
import argparse
import json
from pathlib import Path

from dashboard.services.reaction_lab_competition_shadow import export_shadow


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--ledger-dir", required=True, type=Path)
    p.add_argument("--events", required=True, type=Path)
    p.add_argument("--factors", required=True, type=Path)
    p.add_argument("--cache-dir", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    print(json.dumps(export_shadow(args.run_dir, args.ledger_dir, args.events,
                                   args.factors, args.cache_dir, args.output),
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
