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

        algo_orders = self.exchange.client.futures_get_open_algo_orders(symbol=symbol)

        tp = None
        sl = None

        for order in algo_orders:

            otype = order.get("orderType")
            trigger = float(order.get("triggerPrice", 0))

            if otype in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
                tp = trigger

            elif otype in ("STOP", "STOP_MARKET"):
                sl = trigger

        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "tp": tp,
            "sl": sl
        }