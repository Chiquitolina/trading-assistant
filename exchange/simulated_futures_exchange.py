from decimal import Decimal, ROUND_DOWN, ROUND_UP
class SimulatedFuturesExchange:

    def __init__(
        self,
        market_data,
        initial_balance: float = 1000.0,
        maker_fee_pct: float = 0.02,
        taker_fee_pct: float = 0.05,
    ):
        self.market_data = market_data

        self.initial_balance = float(
            initial_balance
        )

        self.wallet_balance = float(
            initial_balance
        )

        self.maker_fee_pct = float(
            maker_fee_pct
        )

        self.taker_fee_pct = float(
            taker_fee_pct
        )

        self.positions = {}
        self.open_orders = {}
        self.fills = {}

        self.leverages = {}

        self._next_order_id = 1

        # ExecutionEngine consulta esto.
        self.testnet = True

        print(
            "[SIMULATED EXCHANGE] initialized | "
            f"balance={self.wallet_balance:.2f}"
        )

    # =====================================================
    # CONNECTION / ACCOUNT
    # =====================================================

    def ping(self):
        return {}

    def check_account(self):
        return {
            "simulated": True,
            "wallet_balance": self.wallet_balance,
        }

    def get_balance(self):
        return self.wallet_balance

    def get_wallet_balance(self):
        return self.wallet_balance

    # =====================================================
    # MARKET DATA
    # =====================================================

    def get_price(self, symbol: str) -> float:
        price = self.market_data.last_price(
            symbol
        )

        if price is None:
            raise RuntimeError(
                "No replay price available | "
                f"symbol={symbol}"
            )

        return float(price)

    def get_mark_price(
        self,
        symbol: str,
    ) -> float:
        # V1:
        # usamos último precio del replay.
        # Más adelante podremos alimentar mark-price
        # histórico real.
        return self.get_price(symbol)

    # =====================================================
    # FEES
    # =====================================================

    def get_futures_fees(
        self,
        symbol="BTCUSDT",
    ):
        return {
            "maker": self.maker_fee_pct,
            "taker": self.taker_fee_pct,
        }

    # =====================================================
    # POSITIONS
    # =====================================================

    def get_position(
        self,
        symbol: str,
    ):
        position = self.positions.get(symbol)

        if not position:
            return None

        amount = float(
            position["amount"]
        )

        if abs(amount) < 1e-9:
            return None

        return {
            "symbol": symbol,
            "amount": amount,
            "entry_price": float(
                position["entry_price"]
            ),
            "unrealized_pnl": 0.0,
        }

    def get_position_size(
        self,
        symbol: str,
    ):
        position = self.get_position(symbol)

        if not position:
            return 0.0

        return float(
            position["amount"]
        )

    def get_open_positions(self):
        result = []

        for symbol in sorted(
            self.positions.keys()
        ):
            position = self.get_position(
                symbol
            )

            if not position:
                continue

            amount = float(
                position["amount"]
            )

            result.append(
                {
                    "symbol": symbol,
                    "side": (
                        "LONG"
                        if amount > 0
                        else "SHORT"
                    ),
                    "amount": amount,
                    "quantity": abs(amount),
                    "entry_price": float(
                        position[
                            "entry_price"
                        ]
                    ),
                    "mark_price": (
                        self.get_mark_price(
                            symbol
                        )
                    ),
                    "unrealized_pnl": 0.0,
                    "leverage": int(
                        self.leverages.get(
                            symbol,
                            1,
                        )
                    ),
                    "isolated": False,
                }
            )

        return result

    # =====================================================
    # LEVERAGE
    # =====================================================

    def set_leverage(
        self,
        symbol: str,
        leverage: int,
    ):
        leverage = int(leverage)

        self.leverages[symbol] = leverage

        return {
            "symbol": symbol,
            "leverage": leverage,
        }

    # =====================================================
    # EXCHANGE FILTERS
    # =====================================================

    def get_quantity_step_size(
        self,
        symbol: str,
    ) -> float:
        # V1 temporal.
        # Luego cargaremos filtros históricos/reales
        # de Binance Futures.
        return 0.001

    def get_min_quantity(
        self,
        symbol: str,
    ) -> float:
        return 0.001

    def get_max_quantity(
        self,
        symbol: str,
    ) -> float:
        return 1_000_000.0

    def get_price_tick_size(
        self,
        symbol: str,
    ) -> float:
        # V1 temporal.
        return 0.0001

    def normalize_quantity(
        self,
        symbol: str,
        quantity: float,
    ) -> str:
        step = Decimal(
            str(
                self.get_quantity_step_size(
                    symbol
                )
            )
        )

        quantity_dec = Decimal(
            str(quantity)
        )

        adjusted = (
            (
                quantity_dec
                / step
            ).quantize(
                Decimal("1"),
                rounding=ROUND_DOWN,
            )
            * step
        )

        decimals = max(
            0,
            -step.as_tuple().exponent,
        )

        return (
            f"{adjusted:.{decimals}f}"
        )

    def adjust_price_to_tick(
        self,
        price: float,
        tick_size: float,
        side: str = "DOWN",
    ) -> Decimal:
        price_dec = Decimal(str(price))
        tick_dec = Decimal(str(tick_size))

        rounding = (
            ROUND_DOWN
            if side == "DOWN"
            else ROUND_UP
        )

        return (
            (
                price_dec
                / tick_dec
            ).quantize(
                Decimal("1"),
                rounding=rounding,
            )
            * tick_dec
        )

    def normalize_price(
        self,
        symbol: str,
        price: float,
        side: str = "DOWN",
    ) -> str:
        tick = Decimal(
            str(
                self.get_price_tick_size(
                    symbol
                )
            )
        )

        adjusted = (
            self.adjust_price_to_tick(
                price,
                float(tick),
                side,
            )
        )

        decimals = max(
            0,
            -tick.as_tuple().exponent,
        )

        return (
            f"{adjusted:.{decimals}f}"
        )