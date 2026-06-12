class OrderExecutor:
    def __init__(self, exchange):
        self.exchange = exchange

    def set_leverage(self, symbol, leverage):
        return self.exchange.set_leverage(symbol, leverage)

    def market_order(self, symbol, side, quantity):
        return self.exchange.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity
        )

    def cancel_all(self, symbol):
        return self.exchange.cancel_all_orders(symbol)
    
    def cancel_stop_orders(self, symbol):
        cancelled = 0

        # normal stop orders
        orders = self.exchange.get_open_orders(symbol)

        stop_types = {"STOP", "STOP_MARKET"}

        stop_orders = [
            order for order in orders
            if order.get("type") in stop_types
        ]

        print(f"🔎 Found {len(stop_orders)} normal stop orders")

        for order in stop_orders:
            self.exchange.cancel_order(
                symbol=symbol,
                order_id=order["orderId"]
            )
            cancelled += 1

        # algo / conditional stop orders
        try:
            algo_orders = self.exchange.client.futures_get_open_algo_orders(
                symbol=symbol
            ) or []

            algo_stop_orders = [
                order for order in algo_orders
                if order.get("orderType") in stop_types
            ]

            print(f"🔎 Found {len(algo_stop_orders)} algo stop orders")

            for order in algo_stop_orders:
                self.exchange.client.futures_cancel_algo_order(
                    symbol=symbol,
                    algoId=order["algoId"]
                )
                cancelled += 1

        except Exception as e:
            print(f"⚠️ Algo stop cancel error | symbol={symbol} | error={e}")

        print(f"🧹 Stop cancel requests sent | cancelled={cancelled}")