import argparse
from scanner.market_scanner import scan_market
from config.timeframes import TIMEFRAME_CONFIGS

DEFAULT_TF = "15m"


def main():
    parser = argparse.ArgumentParser(
        description="Crypto Trading Bot CLI"
    )

    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser(
        "scan",
        help="Escanear criptos tradeables"
    )

    scan_parser.add_argument(
        "--tf",
        dest="timeframe",
        default=DEFAULT_TF,
        choices=TIMEFRAME_CONFIGS.keys(),
        help="Timeframe (5m, 15m, 1h)"
    )

    args = parser.parse_args()

    print("ARGS =>", args)

    if args.command == "scan":
        is_default = args.timeframe == DEFAULT_TF
        source = "DEFAULT" if is_default else "CLI"

        # 🔴 SOLO TEXTO, SIN LÓGICA
        side = "LONG / SHORT"

        print(
            f"\n🔎 Scan iniciado | TF: {args.timeframe} ({source}) | SIDE: {side}\n"
        )

        scan_market()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
