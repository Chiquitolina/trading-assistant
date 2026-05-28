class ExchangeStateSync:

    def __init__(self, exchange):
        self.exchange = exchange

    def restore_position_state(self, symbol):

        pos = self.exchange.get_position(symbol)

        if not pos or float(pos["amount"]) == 0:
            return None

        side = "LONG" if float(pos["amount"]) > 0 else "SHORT"
        quantity = abs(float(pos["amount"]))
        entry_price = float(pos["entry_price"])

        # TP LIMIT normal
        open_orders = self.exchange.client.futures_get_open_orders(
            symbol=symbol
        )

        # SL / conditional orders
        algo_orders = self.exchange.client.futures_get_open_algo_orders(
            symbol=symbol
        )

        tp = None
        sl = None

        # ==========================
        # TP LIMIT reduceOnly
        # ==========================
        for order in open_orders:
            otype = order.get("type")
            reduce_only = order.get("reduceOnly", False)
            price = float(order.get("price", 0) or 0)

            if (
                otype == "LIMIT"
                and reduce_only
                and price > 0
            ):
                tp = price

        # ==========================
        # SL / conditional
        # ==========================
        for order in algo_orders:
            otype = order.get("orderType")
            trigger = float(order.get("triggerPrice", 0) or 0)

            if otype in ("STOP", "STOP_MARKET"):
                sl = trigger

            elif otype in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
                tp = trigger

        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "tp": tp,
            "sl": sl
        }