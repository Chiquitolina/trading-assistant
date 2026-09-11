import math
from collections import defaultdict
from statistics import median

from config.market_sectors import (
    DEFAULT_SECTOR,
    MIN_SECTOR_SYMBOLS,
)


class MarketSectorFlowAnalyzer:
    def __init__(
        self,
        catalog,
        min_sector_symbols=(
            MIN_SECTOR_SYMBOLS
        ),
    ):
        if catalog is None:
            raise ValueError(
                "sector catalog is required"
            )

        self.catalog = catalog

        self.min_sector_symbols = int(
            min_sector_symbols
        )

        if self.min_sector_symbols < 1:
            raise ValueError(
                "min_sector_symbols "
                "must be positive"
            )

    def enrich_snapshot(
        self,
        snapshot,
    ):
        if not isinstance(snapshot, dict):
            raise TypeError(
                "snapshot must be a dict"
            )

        raw_symbols = snapshot.get(
            "symbols"
        )

        if not isinstance(raw_symbols, dict):
            raise ValueError(
                "snapshot symbols "
                "must be a dict"
            )

        timeframe = str(
            snapshot.get("timeframe")
            or ""
        ).lower().strip()

        if timeframe != "4h":
            raise ValueError(
                "sector flow analyzer "
                "only supports 4h snapshots"
            )

        btc_return = self._to_finite_float(
            snapshot.get(
                "btc_return_pct_4h"
            )
        )

        normalized_symbols = {}
        sector_rows = defaultdict(list)

        for symbol, raw_metrics in (
            raw_symbols.items()
        ):
            if not isinstance(
                raw_metrics,
                dict,
            ):
                continue

            metrics = dict(raw_metrics)

            catalog_item = (
                self.catalog.get(symbol)
            )

            if catalog_item is None:
                metrics.update(
                    self._empty_symbol_context()
                )

                metrics[
                    "sector_catalog_available"
                ] = False

                normalized_symbols[
                    symbol
                ] = metrics

                continue

            primary_sector = (
                catalog_item[
                    "primary_sector"
                ]
            )

            all_sectors = list(
                catalog_item[
                    "all_sectors"
                ]
            )

            metrics.update({
                "sector_catalog_available": (
                    True
                ),
                "coingecko_id": (
                    catalog_item[
                        "coingecko_id"
                    ]
                ),
                "primary_sector": (
                    primary_sector
                ),
                "all_sectors": all_sectors,
                **self._empty_sector_metrics(),
            })

            normalized_symbols[
                symbol
            ] = metrics

            return_pct = (
                self._to_finite_float(
                    metrics.get(
                        "return_pct_4h"
                    )
                )
            )

            quote_volume = (
                self._to_finite_float(
                    metrics.get(
                        "quote_volume_4h"
                    )
                )
            )

            median_quote_volume = (
                self._to_finite_float(
                    metrics.get(
                        "median_quote_volume_4h"
                    )
                )
            )

            if (
                primary_sector
                == DEFAULT_SECTOR
            ):
                continue

            if (
                return_pct is None
                or quote_volume is None
                or median_quote_volume is None
                or quote_volume < 0
                or median_quote_volume <= 0
            ):
                continue

            sector_rows[
                primary_sector
            ].append({
                "symbol": symbol,
                "return_pct_4h": (
                    return_pct
                ),
                "quote_volume_4h": (
                    quote_volume
                ),
                "median_quote_volume_4h": (
                    median_quote_volume
                ),
            })

        configured_sector_sizes = (
            self._configured_sector_sizes()
        )

        sectors = {}
        excluded_sectors = {}

        for sector, configured_size in (
            configured_sector_sizes.items()
        ):
            rows = sector_rows.get(
                sector,
                [],
            )

            valid_size = len(rows)

            coverage_pct = (
                valid_size
                / configured_size
                * 100
                if configured_size
                else 0.0
            )

            if (
                valid_size
                < self.min_sector_symbols
            ):
                excluded_sectors[sector] = {
                    "reason": (
                        "insufficient_symbols"
                    ),
                    "configured_symbols": (
                        configured_size
                    ),
                    "valid_symbols": (
                        valid_size
                    ),
                    "minimum_symbols": (
                        self.min_sector_symbols
                    ),
                    "coverage_pct": round(
                        coverage_pct,
                        4,
                    ),
                }

                continue

            returns = [
                row["return_pct_4h"]
                for row in rows
            ]

            positive_symbols = sum(
                value > 0
                for value in returns
            )

            sector_return = float(
                median(returns)
            )

            sector_breadth = (
                positive_symbols
                / valid_size
                * 100
            )

            quote_volume_total = sum(
                row["quote_volume_4h"]
                for row in rows
            )

            baseline_volume_total = sum(
                row[
                    "median_quote_volume_4h"
                ]
                for row in rows
            )

            sector_relative_volume = (
                quote_volume_total
                / baseline_volume_total
                if baseline_volume_total > 0
                else None
            )

            strength_vs_btc = (
                sector_return
                - btc_return
                if btc_return is not None
                else None
            )

            sectors[sector] = {
                "sector": sector,
                "configured_symbols": (
                    configured_size
                ),
                "valid_symbols": valid_size,
                "coverage_pct": round(
                    coverage_pct,
                    4,
                ),
                "positive_symbols": (
                    positive_symbols
                ),
                "sector_return_pct_4h": (
                    round(
                        sector_return,
                        4,
                    )
                ),
                "sector_breadth_4h": round(
                    sector_breadth,
                    4,
                ),
                "sector_relative_volume_4h": (
                    round(
                        sector_relative_volume,
                        4,
                    )
                    if sector_relative_volume
                    is not None
                    else None
                ),
                "sector_return_rank_pct_4h": (
                    None
                ),
                "sector_strength_vs_btc_4h": (
                    round(
                        strength_vs_btc,
                        4,
                    )
                    if strength_vs_btc
                    is not None
                    else None
                ),
                "quote_volume_4h": round(
                    quote_volume_total,
                    4,
                ),
                "baseline_quote_volume_4h": (
                    round(
                        baseline_volume_total,
                        4,
                    )
                ),
                "return_aggregation": (
                    "median"
                ),
            }

        sector_ranks = (
            self._percentile_ranks({
                sector: metrics[
                    "sector_return_pct_4h"
                ]
                for sector, metrics
                in sectors.items()
            })
        )

        for sector, rank in (
            sector_ranks.items()
        ):
            sectors[sector][
                "sector_return_rank_pct_4h"
            ] = round(rank, 4)

        for symbol, metrics in (
            normalized_symbols.items()
        ):
            primary_sector = (
                metrics.get(
                    "primary_sector"
                )
            )

            sector_metrics = sectors.get(
                primary_sector
            )

            if sector_metrics is None:
                continue

            symbol_return = (
                self._to_finite_float(
                    metrics.get(
                        "return_pct_4h"
                    )
                )
            )

            sector_return = (
                sector_metrics[
                    "sector_return_pct_4h"
                ]
            )

            symbol_strength = (
                symbol_return
                - sector_return
                if symbol_return is not None
                else None
            )

            metrics.update({
                "sector_return_pct_4h": (
                    sector_return
                ),
                "sector_breadth_4h": (
                    sector_metrics[
                        "sector_breadth_4h"
                    ]
                ),
                "sector_relative_volume_4h": (
                    sector_metrics[
                        "sector_relative_volume_4h"
                    ]
                ),
                "sector_return_rank_pct_4h": (
                    sector_metrics[
                        "sector_return_rank_pct_4h"
                    ]
                ),
                "sector_strength_vs_btc_4h": (
                    sector_metrics[
                        "sector_strength_vs_btc_4h"
                    ]
                ),
                "symbol_strength_vs_sector_4h": (
                    round(
                        symbol_strength,
                        4,
                    )
                    if symbol_strength
                    is not None
                    else None
                ),
            })

        enriched_snapshot = dict(
            snapshot
        )

        enriched_snapshot[
            "symbols"
        ] = normalized_symbols

        enriched_snapshot[
            "sector_context_available"
        ] = bool(sectors)

        enriched_snapshot[
            "sector_catalog_generated_at"
        ] = self.catalog.generated_at

        enriched_snapshot[
            "sector_assignment_method"
        ] = "primary_sector"

        enriched_snapshot[
            "sector_return_aggregation"
        ] = "median"

        enriched_snapshot[
            "min_sector_symbols"
        ] = self.min_sector_symbols

        enriched_snapshot[
            "sector_count"
        ] = len(sectors)

        enriched_snapshot[
            "sectors"
        ] = dict(
            sorted(sectors.items())
        )

        enriched_snapshot[
            "excluded_sectors"
        ] = dict(
            sorted(
                excluded_sectors.items()
            )
        )

        return enriched_snapshot

    def _configured_sector_sizes(
        self,
    ):
        sizes = defaultdict(int)

        for item in (
            self.catalog.symbols.values()
        ):
            sector = item[
                "primary_sector"
            ]

            if sector == DEFAULT_SECTOR:
                continue

            sizes[sector] += 1

        return dict(
            sorted(sizes.items())
        )

    def _percentile_ranks(
        self,
        values_by_name,
    ):
        ordered = sorted(
            values_by_name.items(),
            key=lambda item: (
                item[1],
                item[0],
            ),
        )

        count = len(ordered)

        if count == 0:
            return {}

        ranks = {}
        index = 0

        while index < count:
            end_index = index

            while (
                end_index + 1 < count
                and ordered[
                    end_index + 1
                ][1]
                == ordered[index][1]
            ):
                end_index += 1

            average_rank = (
                (
                    index + 1
                    + end_index + 1
                )
                / 2
            )

            percentile = (
                average_rank
                / count
                * 100
            )

            for item_index in range(
                index,
                end_index + 1,
            ):
                name = ordered[
                    item_index
                ][0]

                ranks[name] = percentile

            index = end_index + 1

        return ranks

    def _empty_symbol_context(
        self,
    ):
        return {
            "sector_catalog_available": (
                False
            ),
            "coingecko_id": None,
            "primary_sector": None,
            "all_sectors": [],
            **self._empty_sector_metrics(),
        }

    def _empty_sector_metrics(
        self,
    ):
        return {
            "sector_return_pct_4h": None,
            "sector_breadth_4h": None,
            "sector_relative_volume_4h": (
                None
            ),
            "sector_return_rank_pct_4h": (
                None
            ),
            "sector_strength_vs_btc_4h": (
                None
            ),
            "symbol_strength_vs_sector_4h": (
                None
            ),
        }

    def _to_finite_float(
        self,
        value,
    ):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(value):
            return None

        return value