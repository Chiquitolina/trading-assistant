from dataclasses import dataclass
from typing import Optional


@dataclass
class LegProtection:
    leg_id: str
    symbol: str
    side: str
    quantity: float
    tp_price: float
    sl_price: float
    tp_order_id: Optional[int] = None
    sl_algo_id: Optional[int] = None
    sl_client_algo_id: Optional[str] = None
    sl_materialized_order_id: Optional[int] = None


class LegProtectionManager:
    """Virtual OCO pairs for virtual legs in Binance One-Way mode."""

    def __init__(self, exchange, risk_manager):
        self.exchange = exchange
        self.risk_manager = risk_manager
        self.by_leg = {}
        self.order_to_leg = {}
        self.processed_fill_ids = set()

    def translated_prices(self, plan, real_entry, mark_price):
        return self.risk_manager.calculate_tp_sl(plan, real_entry, mark_price)

    def place(self, leg, plan):
        mark = float(self.exchange.get_mark_price(leg.symbol))
        tp, sl = self.translated_prices(plan, leg.real_entry, mark)
        tick = float(self.exchange.get_price_tick_size(leg.symbol))
        if leg.identity.variant.value == "BUCKET_V1" and plan.signal_context.get("management_profile") != "MODERATE_BO_TP1_STRUCTURAL_SL":
            raise ValueError("BUCKET_V1 requires characterized structural management")
        if plan.side == "LONG":
            tp, sl, close_side = max(tp, mark + tick), min(sl, mark - tick), "SELL"
            tp_rounding, sl_rounding = "UP", "DOWN"
        else:
            tp, sl, close_side = min(tp, mark - tick), max(sl, mark + tick), "BUY"
            tp_rounding, sl_rounding = "DOWN", "UP"
        # SL first minimizes the unprotected interval.
        client_algo_id = f"sl-{leg.leg_id.replace('-', '')[:24]}"
        sl_order = self.exchange.place_stop_loss(
            leg.symbol, close_side, leg.remaining_quantity, sl, sl_rounding,
            client_algo_id=client_algo_id,
        )
        try:
            tp_order = self.exchange.place_take_profit_limit(
                leg.symbol, close_side, leg.remaining_quantity, tp, tp_rounding,
            )
        except Exception:
            self._cancel(leg.symbol, self._algo_id(sl_order))
            raise
        protection = LegProtection(
            leg.leg_id, leg.symbol, plan.side, leg.remaining_quantity,
            float(self.exchange.normalize_price(leg.symbol, tp, tp_rounding)),
            float(self.exchange.normalize_price(leg.symbol, sl, sl_rounding)),
            self._order_id(tp_order), self._algo_id(sl_order),
            str(sl_order.get("clientAlgoId") or sl_order.get("clientOrderId") or client_algo_id),
            self._materialized_order_id(sl_order),
        )
        leg.tp, leg.sl = protection.tp_price, protection.sl_price
        leg.tp_order_id = protection.tp_order_id
        leg.sl_order_id = None
        leg.sl_algo_id = protection.sl_algo_id
        leg.sl_client_algo_id = protection.sl_client_algo_id
        leg.sl_materialized_order_id = protection.sl_materialized_order_id
        self.by_leg[leg.leg_id] = protection
        self.order_to_leg[protection.tp_order_id] = leg.leg_id
        if protection.sl_materialized_order_id:
            self.order_to_leg[protection.sl_materialized_order_id] = leg.leg_id
        self.verify_leg(leg)
        return protection

    @staticmethod
    def _order_id(order):
        return int(order.get("orderId") or order.get("algoId"))

    @staticmethod
    def _algo_id(order):
        return int(order.get("algoId") or order.get("orderId"))

    @staticmethod
    def _materialized_order_id(order):
        value = order.get("actualOrderId") or order.get("triggeredOrderId")
        return int(value) if value else None

    def _cancel(self, symbol, order_id):
        cancel = getattr(self.exchange, "cancel_protective_order", self.exchange.cancel_order)
        return cancel(symbol, order_id)

    def verify_leg(self, leg):
        getter = getattr(self.exchange, "get_all_protective_orders", self.exchange.get_open_orders)
        orders = {
            int(x.get("orderId") or x.get("algoId")): x
            for x in getter(leg.symbol)
            if x.get("orderId") or x.get("algoId")
        }
        for order_id in (leg.tp_order_id, leg.sl_algo_id or leg.sl_order_id):
            order = orders.get(int(order_id))
            if not order or not bool(order.get("reduceOnly")):
                raise RuntimeError(f"missing reduce-only protection for leg {leg.leg_id}")
            if float(order.get("origQty", order.get("quantity", 0))) > leg.remaining_quantity + 1e-9:
                raise RuntimeError("protective quantity exceeds leg remaining quantity")
            self.order_to_leg[int(order_id)] = leg.leg_id

    def verify_position(self, position):
        getter = getattr(self.exchange, "get_all_protective_orders", self.exchange.get_open_orders)
        orders = {
            int(x.get("orderId") or x.get("algoId")): x
            for x in getter(position.symbol)
            if x.get("orderId") or x.get("algoId")
        }
        tp_total = sl_total = 0.0
        for leg in position.entry_legs:
            if leg.remaining_quantity <= 1e-9: continue
            self.verify_leg(leg)
            for order_id, family in ((leg.tp_order_id, "tp"), (leg.sl_algo_id or leg.sl_order_id, "sl")):
                order = orders[int(order_id)]
                remaining = float(order.get("origQty", 0)) - float(order.get("executedQty", 0))
                if family == "tp": tp_total += remaining
                else: sl_total += remaining
        tolerance = max(1e-9, float(position.quantity) * 1e-6)
        if tp_total > float(position.quantity) + tolerance or sl_total > float(position.quantity) + tolerance:
            raise RuntimeError("protective orders exceed real position quantity")
        return True

    def process_fill(self, position, leg, fill):
        fill_id = str(fill["id"])
        if fill_id in leg.processed_fill_ids or fill_id in self.processed_fill_ids:
            return False
        quantity = min(float(fill["quantity"]), leg.remaining_quantity)
        if quantity <= 0: return False
        self.processed_fill_ids.add(fill_id)
        leg.processed_fill_ids.append(fill_id)
        leg.remaining_quantity -= quantity
        leg.closed_quantity += quantity
        leg.exit_fills.append({
            "id": fill_id, "order_id": int(fill["order_id"]),
            "quantity": quantity, "price": float(fill.get("price", 0)),
            "timestamp": fill.get("timestamp"), "fee": float(fill.get("fee", 0)),
        })
        leg.exit_fees += float(fill.get("fee", 0))
        leg.validate_quantities()
        filled_order = int(fill["order_id"])
        sl_reference = leg.sl_materialized_order_id or leg.sl_algo_id or leg.sl_order_id
        sibling = sl_reference if filled_order == leg.tp_order_id else leg.tp_order_id
        if leg.remaining_quantity <= 1e-9:
            leg.remaining_quantity = 0
            leg.status = "CLOSED"
            self._cancel(leg.symbol, sibling)
        else:
            leg.status = "PARTIALLY_FILLED"
            self._cancel(leg.symbol, sibling)
            protection = self.by_leg[leg.leg_id]
            close_side = "SELL" if protection.side == "LONG" else "BUY"
            if sibling == sl_reference:
                client_algo_id = f"sl-{leg.leg_id.replace('-', '')[:20]}-{len(leg.exit_fills)}"
                order = self.exchange.place_stop_loss(
                    leg.symbol, close_side, leg.remaining_quantity,
                    leg.sl, "DOWN" if protection.side == "LONG" else "UP",
                    client_algo_id=client_algo_id,
                )
                leg.sl_algo_id = protection.sl_algo_id = self._algo_id(order)
                leg.sl_client_algo_id = protection.sl_client_algo_id = str(
                    order.get("clientAlgoId") or order.get("clientOrderId") or client_algo_id
                )
                leg.sl_materialized_order_id = protection.sl_materialized_order_id = self._materialized_order_id(order)
                if leg.sl_materialized_order_id:
                    self.order_to_leg[leg.sl_materialized_order_id] = leg.leg_id
            else:
                order = self.exchange.place_take_profit_limit(
                    leg.symbol, close_side, leg.remaining_quantity,
                    leg.tp, "UP" if protection.side == "LONG" else "DOWN",
                )
                leg.tp_order_id = protection.tp_order_id = self._order_id(order)
                self.order_to_leg[leg.tp_order_id] = leg.leg_id
        position.quantity = sum(x.remaining_quantity for x in position.entry_legs)
        get_position = getattr(self.exchange, "get_position", None)
        if get_position:
            real = get_position(leg.symbol)
            if real and real != "INVALID_SYMBOL":
                position.quantity = abs(float(real["amount"]))
                position.real_entry = float(real["entry_price"])
        return True
