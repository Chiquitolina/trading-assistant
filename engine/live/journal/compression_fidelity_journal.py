import json
import os
from pathlib import Path
from datetime import datetime


class CompressionFidelityJournal:
    def __init__(
        self,
        base_dir="compression_fidelity_journal",
    ):
        clock_mode = os.getenv(
            "MARKET_CLOCK_MODE",
            "real",
        ).strip().lower()

        self.mode = (
            "replay"
            if clock_mode == "replay"
            else "live"
        )

        self.base_dir = (
            Path(base_dir)
            / self.mode
        )

        self.base_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    def log(
        self,
        symbol: str,
        data: dict,
    ):
        path = (
            self.base_dir
            / f"{symbol}.jsonl"
        )

        payload = {
            "logged_at": datetime.utcnow().isoformat(
                timespec="milliseconds"
            ),
            "mode": self.mode,
            "symbol": symbol,
            **data,
        }

        with open(
            path,
            "a",
            encoding="utf-8",
        ) as f:
            f.write(
                json.dumps(
                    payload,
                    default=str,
                )
                + "\n"
            )