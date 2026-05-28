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
        orders = self.exchange.get_open_orders(symbol)

        stop_types = {"STOP", "STOP_MARKET"}

        stop_orders = [
            order for order in orders
            if order.get("type") in stop_types
        ]

        print(f"🔎 Found {len(stop_orders)} stop orders")

        for order in stop_orders:
            self.exchange.cancel_order(
                symbol=symbol,
                order_id=order["orderId"]
            )

        print("🧹 Stop cancel requests sent")