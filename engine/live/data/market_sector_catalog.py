import json
from collections import defaultdict
from pathlib import Path

from config.market_sectors import (
    DEFAULT_SECTOR,
)


DEFAULT_CATALOG_PATH = (
    Path(__file__)
    .resolve()
    .parents[3]
    / "data"
    / "market_sector_catalog.json"
)


class MarketSectorCatalog:
    SUPPORTED_TYPE = (
        "market_sector_catalog"
    )

    SUPPORTED_VERSION = 1

    ALLOWED_RESOLUTION_STATUSES = {
        "resolved_binance_futures",
        "manual_override",
    }

    def __init__(
        self,
        path=None,
        required_symbols=None,
    ):
        self.path = Path(
            path or DEFAULT_CATALOG_PATH
        )

        self.required_symbols = {
            self._normalize_symbol(symbol)
            for symbol in (
                required_symbols or []
            )
        }

        self.generated_at = None
        self.source = None
        self.symbols = {}

        self._load()

    def _load(self):
        if not self.path.exists():
            raise FileNotFoundError(
                "Market sector catalog "
                f"not found: {self.path}"
            )

        try:
            with open(
                self.path,
                "r",
                encoding="utf-8",
            ) as file:
                payload = json.load(file)

        except json.JSONDecodeError as exc:
            raise ValueError(
                "Market sector catalog "
                "contains invalid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise ValueError(
                "Market sector catalog "
                "must be a dict"
            )

        if (
            payload.get("type")
            != self.SUPPORTED_TYPE
        ):
            raise ValueError(
                "Invalid market sector "
                "catalog type"
            )

        try:
            version = int(
                payload["version"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                "Invalid market sector "
                "catalog version"
            ) from exc

        if version != self.SUPPORTED_VERSION:
            raise ValueError(
                "Unsupported market sector "
                f"catalog version: {version}"
            )

        raw_symbols = payload.get(
            "symbols"
        )

        if not isinstance(raw_symbols, dict):
            raise ValueError(
                "Market sector catalog "
                "symbols must be a dict"
            )

        normalized_symbols = {}

        for raw_symbol, raw_item in (
            raw_symbols.items()
        ):
            symbol = self._normalize_symbol(
                raw_symbol
            )

            item = self._normalize_item(
                symbol=symbol,
                raw_item=raw_item,
            )

            normalized_symbols[symbol] = item

        catalog_symbol_set = set(
            normalized_symbols
        )

        if self.required_symbols:
            missing_symbols = sorted(
                self.required_symbols
                - catalog_symbol_set
            )

            unexpected_symbols = sorted(
                catalog_symbol_set
                - self.required_symbols
            )

            if missing_symbols:
                raise ValueError(
                    "Market sector catalog "
                    "is missing configured "
                    "symbols: "
                    + ",".join(
                        missing_symbols
                    )
                )

            if unexpected_symbols:
                raise ValueError(
                    "Market sector catalog "
                    "contains unexpected "
                    "symbols: "
                    + ",".join(
                        unexpected_symbols
                    )
                )

        generated_at = payload.get(
            "generated_at"
        )

        try:
            generated_at = int(
                generated_at
            )
        except (
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                "Invalid catalog generated_at"
            ) from exc

        self.generated_at = generated_at
        self.source = payload.get(
            "source"
        )
        self.symbols = normalized_symbols

    def _normalize_item(
        self,
        symbol,
        raw_item,
    ):
        if not isinstance(raw_item, dict):
            raise ValueError(
                "Invalid sector catalog "
                f"item: {symbol}"
            )

        stored_symbol = (
            self._normalize_symbol(
                raw_item.get(
                    "binance_symbol"
                )
            )
        )

        if stored_symbol != symbol:
            raise ValueError(
                "Sector catalog symbol "
                f"mismatch: {symbol}"
            )

        coin_id = str(
            raw_item.get(
                "coingecko_id"
            )
            or ""
        ).strip()

        if not coin_id:
            raise ValueError(
                "Missing CoinGecko ID "
                f"for {symbol}"
            )

        resolution_status = str(
            raw_item.get(
                "resolution_status"
            )
            or ""
        ).strip()

        if (
            resolution_status
            not in self
            .ALLOWED_RESOLUTION_STATUSES
        ):
            raise ValueError(
                "Untrusted resolution "
                f"for {symbol}: "
                f"{resolution_status}"
            )

        primary_sector = str(
            raw_item.get(
                "primary_sector"
            )
            or DEFAULT_SECTOR
        ).strip()

        raw_all_sectors = (
            raw_item.get(
                "all_sectors"
            )
            or []
        )

        if not isinstance(
            raw_all_sectors,
            list,
        ):
            raise ValueError(
                "Invalid all_sectors "
                f"for {symbol}"
            )

        all_sectors = tuple(
            dict.fromkeys(
                str(sector).strip()
                for sector
                in raw_all_sectors
                if str(sector).strip()
            )
        )

        if (
            primary_sector
            != DEFAULT_SECTOR
            and primary_sector
            not in all_sectors
        ):
            raise ValueError(
                "Primary sector missing "
                f"from all_sectors: {symbol}"
            )

        return {
            "binance_symbol": symbol,
            "base_symbol": (
                raw_item.get(
                    "base_symbol"
                )
            ),
            "coingecko_id": coin_id,
            "coingecko_name": (
                raw_item.get(
                    "coingecko_name"
                )
            ),
            "primary_sector": (
                primary_sector
            ),
            "all_sectors": all_sectors,
            "resolution_status": (
                resolution_status
            ),
        }

    def get(
        self,
        symbol,
    ):
        symbol = self._normalize_symbol(
            symbol
        )

        item = self.symbols.get(symbol)

        if item is None:
            return None

        return dict(item)

    def get_primary_sector(
        self,
        symbol,
    ):
        item = self.get(symbol)

        if item is None:
            return None

        return item["primary_sector"]

    def symbols_by_primary_sector(
        self,
    ):
        grouped = defaultdict(list)

        for symbol, item in (
            self.symbols.items()
        ):
            grouped[
                item["primary_sector"]
            ].append(symbol)

        return {
            sector: tuple(
                sorted(symbols)
            )
            for sector, symbols
            in sorted(grouped.items())
        }

    def __len__(self):
        return len(self.symbols)

    def _normalize_symbol(
        self,
        symbol,
    ):
        symbol = str(
            symbol or ""
        ).upper().strip()

        if not symbol:
            raise ValueError(
                "symbol is required"
            )

        return symbol