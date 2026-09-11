import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

if str(BASE_DIR) not in sys.path:
    sys.path.insert(
        0,
        str(BASE_DIR),
    )


from config.market_sectors import (
    BINANCE_BASE_SYMBOL_ALIASES,
    COINGECKO_ID_OVERRIDES,
    DEFAULT_SECTOR,
    PRIMARY_SECTOR_PRIORITY,
    SECTOR_DEFINITIONS,
    get_all_coingecko_category_ids,
    get_sector_for_category,
    select_primary_sector,
)
from config.strategies.v1 import SYMBOLS
from services.market_context.coingecko_client import (
    CoinGeckoClient,
)


DEFAULT_CATALOG_PATH = (
    BASE_DIR
    / "data"
    / "market_sector_catalog.json"
)

DEFAULT_REPORT_PATH = (
    BASE_DIR
    / "reports"
    / "market_sector_catalog_report.csv"
)


def normalize_binance_base_symbol(
    symbol,
):
    symbol = str(symbol).upper().strip()

    if symbol.endswith("USDT"):
        base_symbol = symbol[:-4]
    else:
        base_symbol = symbol

    return BINANCE_BASE_SYMBOL_ALIASES.get(
        base_symbol,
        base_symbol,
    )


def sector_sort_key(sector):
    try:
        return PRIMARY_SECTOR_PRIORITY.index(
            sector
        )
    except ValueError:
        return len(PRIMARY_SECTOR_PRIORITY)


def build_category_index(client):
    coins_by_id = {}
    coin_ids_by_symbol = defaultdict(set)

    category_ids = (
        get_all_coingecko_category_ids()
    )

    for index, category_id in enumerate(
        category_ids,
        start=1,
    ):
        sector = get_sector_for_category(
            category_id
        )

        print(
            "[SECTOR CATALOG] "
            f"category={index}/{len(category_ids)} "
            f"id={category_id} "
            f"sector={sector}"
        )

        category_coins = (
            client.get_coins_by_category(
                category_id
            )
        )

        print(
            "[SECTOR CATALOG] "
            f"id={category_id} "
            f"coins={len(category_coins)}"
        )

        for coin in category_coins:
            coin_id = str(
                coin.get("id") or ""
            ).strip()

            coin_symbol = str(
                coin.get("symbol") or ""
            ).upper().strip()

            if not coin_id or not coin_symbol:
                continue

            market_cap = coin.get(
                "market_cap"
            )

            try:
                market_cap = float(
                    market_cap or 0
                )
            except (TypeError, ValueError):
                market_cap = 0.0

            stored = coins_by_id.setdefault(
                coin_id,
                {
                    "id": coin_id,
                    "symbol": coin_symbol,
                    "name": coin.get("name"),
                    "market_cap": market_cap,
                    "category_ids": set(),
                    "sectors": set(),
                },
            )

            stored["market_cap"] = max(
                stored["market_cap"],
                market_cap,
            )

            stored["category_ids"].add(
                category_id
            )

            if sector:
                stored["sectors"].add(
                    sector
                )

            coin_ids_by_symbol[
                coin_symbol
            ].add(coin_id)

    return (
        coins_by_id,
        coin_ids_by_symbol,
    )
    
def build_binance_futures_index(
    client,
):
    tickers = (
        client.get_binance_futures_tickers()
    )

    futures_coin_ids = {}
    ignored_tickers = {}

    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue

        symbol = str(
            ticker.get("symbol") or ""
        ).upper().strip()

        coin_id = str(
            ticker.get("coin_id") or ""
        ).strip()

        contract_type = str(
            ticker.get("contract_type") or ""
        ).lower().strip()

        target = str(
            ticker.get("target") or ""
        ).upper().strip()

        if not symbol:
            continue

        if contract_type != "perpetual":
            ignored_tickers[symbol] = (
                f"contract_type:{contract_type}"
            )
            continue

        if target and target != "USDT":
            ignored_tickers[symbol] = (
                f"target:{target}"
            )
            continue

        if not coin_id:
            ignored_tickers[symbol] = (
                "missing_coin_id"
            )
            continue

        futures_coin_ids[symbol] = coin_id

    print(
        "[SECTOR CATALOG] "
        "binance_futures_tickers="
        f"{len(tickers)} "
        "resolved_contracts="
        f"{len(futures_coin_ids)} "
        "ignored_contracts="
        f"{len(ignored_tickers)}"
    )

    return futures_coin_ids


def select_coin_candidate(
    binance_symbol,
    base_symbol,
    coins_by_id,
    coin_ids_by_symbol,
    futures_coin_ids,
):
    override_id = (
        COINGECKO_ID_OVERRIDES.get(
            binance_symbol
        )
    )

    if override_id:
        coin = coins_by_id.get(
            override_id
        )

        if coin is None:
            coin = {
                "id": override_id,
                "symbol": base_symbol,
                "name": None,
                "market_cap": 0.0,
                "category_ids": set(),
                "sectors": set(),
            }

        return (
            coin,
            "manual_override",
            [override_id],
        )

    futures_coin_id = (
        futures_coin_ids.get(
            binance_symbol
        )
    )

    if futures_coin_id:
        coin = coins_by_id.get(
            futures_coin_id
        )

        if coin is None:
            coin = {
                "id": futures_coin_id,
                "symbol": base_symbol,
                "name": None,
                "market_cap": 0.0,
                "category_ids": set(),
                "sectors": set(),
            }

        return (
            coin,
            "resolved_binance_futures",
            [futures_coin_id],
        )

    candidate_ids = sorted(
        coin_ids_by_symbol.get(
            base_symbol,
            set(),
        )
    )

    if not candidate_ids:
        return (
            None,
            "not_in_selected_categories",
            [],
        )

    if len(candidate_ids) > 1:
        return (
            None,
            "ambiguous_symbol_needs_review",
            candidate_ids,
        )

    selected_id = candidate_ids[0]

    return (
        coins_by_id[selected_id],
        "resolved_unique_symbol_fallback",
        candidate_ids,
    )


def build_catalog(client):
    
    futures_coin_ids = (
        build_binance_futures_index(
            client
        )
    )
    
    (
        coins_by_id,
        coin_ids_by_symbol,
    ) = build_category_index(client)

    unique_symbols = list(
        dict.fromkeys(SYMBOLS)
    )

    catalog_symbols = {}
    report_rows = []

    for binance_symbol in unique_symbols:
        base_symbol = (
            normalize_binance_base_symbol(
                binance_symbol
            )
        )

        (
            selected_coin,
            resolution_status,
            candidate_ids,
        ) = select_coin_candidate(
            binance_symbol=binance_symbol,
            base_symbol=base_symbol,
            coins_by_id=coins_by_id,
            coin_ids_by_symbol=(
                coin_ids_by_symbol
            ),
            futures_coin_ids=(
                futures_coin_ids
            ),
        )

        if selected_coin is None:
            coin_id = None
            coin_name = None
            market_cap = None
            category_ids = []
            all_sectors = []
            primary_sector = DEFAULT_SECTOR

        else:
            coin_id = selected_coin["id"]
            coin_name = selected_coin["name"]
            market_cap = selected_coin[
                "market_cap"
            ]

            category_ids = sorted(
                selected_coin[
                    "category_ids"
                ]
            )

            all_sectors = sorted(
                selected_coin["sectors"],
                key=sector_sort_key,
            )

            primary_sector = (
                select_primary_sector(
                    all_sectors
                )
            )

        catalog_symbols[binance_symbol] = {
            "binance_symbol": (
                binance_symbol
            ),
            "base_symbol": base_symbol,
            "coingecko_id": coin_id,
            "coingecko_name": coin_name,
            "coingecko_market_cap": (
                market_cap
            ),
            "primary_sector": (
                primary_sector
            ),
            "all_sectors": all_sectors,
            "coingecko_category_ids": (
                category_ids
            ),
            "resolution_status": (
                resolution_status
            ),
            "candidate_ids": (
                candidate_ids
            ),
        }

        report_rows.append({
            "binance_symbol": (
                binance_symbol
            ),
            "base_symbol": base_symbol,
            "coingecko_id": coin_id,
            "coingecko_name": coin_name,
            "primary_sector": (
                primary_sector
            ),
            "all_sectors": "|".join(
                all_sectors
            ),
            "resolution_status": (
                resolution_status
            ),
            "candidate_count": len(
                candidate_ids
            ),
            "candidate_ids": "|".join(
                candidate_ids
            ),
        })

    status_counts = defaultdict(int)
    sector_counts = defaultdict(int)

    for item in catalog_symbols.values():
        status_counts[
            item["resolution_status"]
        ] += 1

        sector_counts[
            item["primary_sector"]
        ] += 1

    return {
        "type": "market_sector_catalog",
        "version": 1,
        "generated_at": int(
            time.time() * 1000
        ),
        "source": "coingecko",
        "configured_symbols": len(SYMBOLS),
        "unique_symbols": len(
            unique_symbols
        ),
        "selected_category_ids": list(
            get_all_coingecko_category_ids()
        ),
        "resolution_status_counts": dict(
            sorted(status_counts.items())
        ),
        "primary_sector_counts": dict(
            sorted(sector_counts.items())
        ),
        "symbols": catalog_symbols,
    }, report_rows


def write_json_atomic(
    path,
    payload,
):
    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(
        f"{path.suffix}.tmp"
    )

    with open(
        temporary_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            payload,
            file,
            indent=2,
            ensure_ascii=False,
        )

    temporary_path.replace(path)


def write_csv_report(
    path,
    rows,
):
    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "binance_symbol",
        "base_symbol",
        "coingecko_id",
        "coingecko_name",
        "primary_sector",
        "all_sectors",
        "resolution_status",
        "candidate_count",
        "candidate_ids",
    ]

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--catalog-path",
        default=str(
            DEFAULT_CATALOG_PATH
        ),
    )

    parser.add_argument(
        "--report-path",
        default=str(
            DEFAULT_REPORT_PATH
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    client = CoinGeckoClient()

    catalog, report_rows = (
        build_catalog(client)
    )

    write_json_atomic(
        args.catalog_path,
        catalog,
    )

    write_csv_report(
        args.report_path,
        report_rows,
    )

    print()
    print(
        json.dumps(
            {
                "catalog_path": (
                    args.catalog_path
                ),
                "report_path": (
                    args.report_path
                ),
                "configured_symbols": (
                    catalog[
                        "configured_symbols"
                    ]
                ),
                "unique_symbols": (
                    catalog[
                        "unique_symbols"
                    ]
                ),
                "resolution_status_counts": (
                    catalog[
                        "resolution_status_counts"
                    ]
                ),
                "primary_sector_counts": (
                    catalog[
                        "primary_sector_counts"
                    ]
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()