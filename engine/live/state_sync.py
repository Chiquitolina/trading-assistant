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
        close_side = "SELL" if side == "LONG" else "BUY"

        try:
            open_orders = self.exchange.client.futures_get_open_orders(
                symbol=symbol
            )
        except Exception as e:
            print(f"\033[94m[SYNC]\033[0m ⚠️ Failed getting open orders | {symbol} | {e}")
            open_orders = []

        try:
            algo_orders = self.exchange.client.futures_get_open_algo_orders(
                symbol=symbol
            )
        except Exception as e:
            print(f"\033[94m[SYNC]\033[0m ⚠️ Failed getting algo orders | {symbol} | {e}")
            algo_orders = []

        tp_candidates = []
        sl_candidates = []

        for order in open_orders:
            otype = order.get("type")
            reduce_only = order.get("reduceOnly", False)
            order_side = order.get("side")
            price = float(order.get("price", 0) or 0)

            if (
                otype == "LIMIT"
                and reduce_only
                and order_side == close_side
                and price > 0
            ):
                if side == "LONG" and price > entry_price:
                    tp_candidates.append(price)
                elif side == "SHORT" and price < entry_price:
                    tp_candidates.append(price)

        for order in algo_orders:
            otype = order.get("orderType")
            order_side = order.get("side")
            trigger = float(order.get("triggerPrice", 0) or 0)

            if order_side != close_side or trigger <= 0:
                continue

            if otype in ("STOP", "STOP_MARKET"):
                if side == "LONG" and trigger < entry_price:
                    sl_candidates.append(trigger)
                elif side == "SHORT" and trigger > entry_price:
                    sl_candidates.append(trigger)

            elif otype in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
                if side == "LONG" and trigger > entry_price:
                    tp_candidates.append(trigger)
                elif side == "SHORT" and trigger < entry_price:
                    tp_candidates.append(trigger)

        if side == "LONG":
            tp = min(tp_candidates) if tp_candidates else None
            sl = max(sl_candidates) if sl_candidates else None
        else:
            tp = max(tp_candidates) if tp_candidates else None
            sl = min(sl_candidates) if sl_candidates else None

        print(
            f"\033[94m[SYNC]\033[0m "
            f"restore_state | symbol={symbol} side={side} qty={quantity} "
            f"entry={entry_price} tp={tp} sl={sl} "
            f"open_orders={len(open_orders)} algo_orders={len(algo_orders)}"
        )

        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "tp": tp,
            "sl": sl
        }