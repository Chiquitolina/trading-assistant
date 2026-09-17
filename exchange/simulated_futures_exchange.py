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
        
        self._current_timestamp_ms = 0

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
            
    def set_market_timestamp(
        self,
        timestamp_ms: int,
    ):
        self._current_timestamp_ms = int(
            timestamp_ms
        )


    def _next_id(self):
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id


    def cancel_all_orders(
        self,
        symbol: str,
    ):
        # Replay:
        # no simulamos infraestructura de cancelación.
        # Solo limpiamos órdenes simuladas si existen.
        self.open_orders.pop(symbol, None)

        return []


    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity,
    ):
        symbol = str(symbol).upper()
        side = str(side).upper()
        quantity = float(quantity)

        if side not in ("BUY", "SELL"):
            raise ValueError(
                f"Invalid market side: {side}"
            )

        if quantity <= 0:
            raise ValueError(
                f"Invalid quantity: {quantity}"
            )

        price = float(
            self.get_price(symbol)
        )

        order_id = self._next_id()

        timestamp_ms = int(
            self._current_timestamp_ms
        )

        if timestamp_ms <= 0:
            raise RuntimeError(
                "Replay market timestamp "
                "was not initialized"
            )

        current = self.positions.get(symbol)

        # ==========================================
        # OPEN POSITION
        # ==========================================

        if current is None:

            signed_qty = (
                quantity
                if side == "BUY"
                else -quantity
            )

            self.positions[symbol] = {
                "symbol": symbol,
                "amount": signed_qty,
                "entry_price": price,
                "leverage": int(
                    self.leverages.get(
                        symbol,
                        1,
                    )
                ),
            }

            realized_pnl = 0.0

        else:
            raise RuntimeError(
                "Replay V1 does not support "
                "adding to an existing position | "
                f"symbol={symbol}"
            )

        # ==========================================
        # ENTRY FILL
        # ==========================================

        notional = quantity * price

        commission = (
            notional
            * self.taker_fee_pct
            / 100.0
        )

        fill = {
            "symbol": symbol,
            "orderId": order_id,
            "side": side,
            "price": price,
            "qty": quantity,
            "commission": commission,
            "realizedPnl": realized_pnl,
            "time": timestamp_ms,
        }

        self.fills.setdefault(
            symbol,
            []
        ).append(fill)

        print(
            "[REPLAY ENTRY] "
            f"symbol={symbol} "
            f"side={side} "
            f"qty={quantity} "
            f"price={price} "
            f"fee={commission:.8f} "
            f"ts={timestamp_ms}"
        )

        return {
            "symbol": symbol,
            "orderId": order_id,
            "status": "FILLED",
            "side": side,
            "type": "MARKET",
            "origQty": str(quantity),
            "executedQty": str(quantity),
            "avgPrice": str(price),
            "updateTime": timestamp_ms,
        }
        
    def place_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity,
        stop_price,
        price_rounding="DOWN",
    ):
        symbol = str(symbol).upper()
        side = str(side).upper()
        quantity = float(quantity)
        stop_price = float(stop_price)

        if side not in ("BUY", "SELL"):
            raise ValueError(
                f"Invalid stop-loss side: {side}"
            )

        if quantity <= 0:
            raise ValueError(
                f"Invalid stop-loss quantity: {quantity}"
            )

        order_id = self._next_id()

        order = {
            "symbol": symbol,
            "orderId": order_id,
            "type": "STOP_MARKET",
            "side": side,
            "quantity": quantity,
            "stopPrice": stop_price,
            "status": "NEW",
            "reduceOnly": True,
            "createdTime": int(
                self._current_timestamp_ms
            ),
        }

        symbol_orders = self.open_orders.setdefault(
            symbol,
            {}
        )

        symbol_orders["SL"] = order

        print(
            "[REPLAY SL] "
            f"symbol={symbol} "
            f"side={side} "
            f"qty={quantity} "
            f"stop={stop_price} "
            f"order_id={order_id}"
        )

        return order


    def place_take_profit_limit(
        self,
        symbol: str,
        side: str,
        quantity,
        price,
        price_rounding="UP",
    ):
        symbol = str(symbol).upper()
        side = str(side).upper()
        quantity = float(quantity)
        price = float(price)

        if side not in ("BUY", "SELL"):
            raise ValueError(
                f"Invalid take-profit side: {side}"
            )

        if quantity <= 0:
            raise ValueError(
                f"Invalid take-profit quantity: {quantity}"
            )

        order_id = self._next_id()

        order = {
            "symbol": symbol,
            "orderId": order_id,
            "type": "TAKE_PROFIT_LIMIT",
            "side": side,
            "quantity": quantity,
            "price": price,
            "stopPrice": price,
            "status": "NEW",
            "reduceOnly": True,
            "createdTime": int(
                self._current_timestamp_ms
            ),
        }

        symbol_orders = self.open_orders.setdefault(
            symbol,
            {}
        )

        symbol_orders["TP"] = order

        print(
            "[REPLAY TP] "
            f"symbol={symbol} "
            f"side={side} "
            f"qty={quantity} "
            f"price={price} "
            f"order_id={order_id}"
        )

        return order
    
    def get_simulated_protective_orders(
        self,
        symbol: str,
    ):
        symbol = str(symbol).upper()

        orders = self.open_orders.get(
            symbol,
            {}
        )

        return {
            key: dict(value)
            for key, value in orders.items()
        }

    def process_candle(
        self,
        symbol: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        timestamp_ms: int,
    ):
        symbol = str(symbol).upper()

        open_price = float(open_price)
        high = float(high)
        low = float(low)
        close = float(close)
        timestamp_ms = int(timestamp_ms)

        position = self.positions.get(symbol)

        if not position:
            return None

        orders = self.open_orders.get(
            symbol,
            {}
        )

        sl_order = orders.get("SL")
        tp_order = orders.get("TP")

        if not sl_order and not tp_order:
            return None

        amount = float(
            position["amount"]
        )

        is_long = amount > 0

        sl_hit = False
        tp_hit = False

        # ==========================================
        # LONG
        # ==========================================

        if is_long:

            if sl_order:
                sl_price = float(
                    sl_order["stopPrice"]
                )

                sl_hit = low <= sl_price

            if tp_order:
                tp_price = float(
                    tp_order["price"]
                )

                tp_hit = high >= tp_price

        # ==========================================
        # SHORT
        # ==========================================

        else:

            if sl_order:
                sl_price = float(
                    sl_order["stopPrice"]
                )

                sl_hit = high >= sl_price

            if tp_order:
                tp_price = float(
                    tp_order["price"]
                )

                tp_hit = low <= tp_price

        # ==========================================
        # NOTHING HIT
        # ==========================================

        if not sl_hit and not tp_hit:
            return None

        # ==========================================
        # DETERMINE EXIT
        # ==========================================

        ambiguous = (
            sl_hit
            and tp_hit
        )

        # V1 conservative policy:
        # if TP and SL are touched inside the same
        # 1m candle, assume SL happened first.
        if sl_hit:
            exit_reason = "SL"
            exit_order = sl_order

            exit_price = float(
                sl_order["stopPrice"]
            )

        else:
            exit_reason = "TP"
            exit_order = tp_order

            exit_price = float(
                tp_order["price"]
            )

        return self._execute_protective_exit(
            symbol=symbol,
            exit_order=exit_order,
            exit_price=exit_price,
            exit_reason=exit_reason,
            timestamp_ms=timestamp_ms,
            ambiguous=ambiguous,
        )
        
    def _execute_protective_exit(
        self,
        symbol: str,
        exit_order: dict,
        exit_price: float,
        exit_reason: str,
        timestamp_ms: int,
        ambiguous: bool = False,
    ):
        position = self.positions.get(symbol)

        if not position:
            raise RuntimeError(
                "Replay protective exit without "
                f"position | symbol={symbol}"
            )

        amount = float(
            position["amount"]
        )

        quantity = abs(amount)

        entry_price = float(
            position["entry_price"]
        )

        is_long = amount > 0

        exit_side = (
            "SELL"
            if is_long
            else "BUY"
        )

        exit_price = float(exit_price)

        # ==========================================
        # REALIZED PNL
        # ==========================================

        if is_long:
            realized_pnl = (
                exit_price
                - entry_price
            ) * quantity

        else:
            realized_pnl = (
                entry_price
                - exit_price
            ) * quantity

        # ==========================================
        # EXIT FEE
        # ==========================================

        notional = (
            quantity
            * exit_price
        )

        commission = (
            notional
            * self.taker_fee_pct
            / 100.0
        )

        # ==========================================
        # EXIT FILL
        # ==========================================

        fill = {
            "symbol": symbol,
            "orderId": exit_order["orderId"],
            "side": exit_side,
            "price": exit_price,
            "qty": quantity,
            "commission": commission,
            "realizedPnl": realized_pnl,
            "time": int(timestamp_ms),
        }

        self.fills.setdefault(
            symbol,
            []
        ).append(fill)

        # ==========================================
        # WALLET
        # ==========================================

        self.wallet_balance += (
            realized_pnl
            - commission
        )

        # ==========================================
        # CLOSE POSITION + CANCEL SIBLING ORDER
        # ==========================================

        self.positions.pop(
            symbol,
            None,
        )

        self.open_orders.pop(
            symbol,
            None,
        )

        event = {
            "symbol": symbol,
            "exit_reason": exit_reason,
            "side": exit_side,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "commission": commission,
            "timestamp": int(
                timestamp_ms
            ),
            "ambiguous": bool(
                ambiguous
            ),
        }

        print(
            "[REPLAY EXIT] "
            f"symbol={symbol} "
            f"reason={exit_reason} "
            f"side={exit_side} "
            f"qty={quantity} "
            f"entry={entry_price} "
            f"exit={exit_price} "
            f"pnl={realized_pnl:.8f} "
            f"fee={commission:.8f} "
            f"ambiguous={ambiguous} "
            f"ts={timestamp_ms}"
        )

        return event

    def get_recent_fills(
        self,
        symbol: str,
        limit: int = 1000,
    ):
        symbol = str(symbol).upper()

        fills = self.fills.get(
            symbol,
            []
        )

        return list(
            fills[-int(limit):]
        )