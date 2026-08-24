import time
from threading import Lock
from uuid import uuid4

from config.strategies.v1 import BUCKET_EXECUTION
from exchange.fill_query import coerce_fill_query_result
from exchange.order_history import OrderHistoryResult
from models.entry_leg import EntryLeg, EntryLegIdentity
from models.execution_variant import ExecutionVariant
from models.position import Position
from services.position_sizer import PositionSizer


class BucketExecutionService:
    def __init__(self, engine, protection_manager):
        self.engine = engine
        self.exchange = engine.exchange
        self.protection = protection_manager
        self.position_sizer = PositionSizer(
            total_usage_pct=BUCKET_EXECUTION["total_usage_pct"],
            max_positions=BUCKET_EXECUTION["max_unique_symbols"],
            buffer=BUCKET_EXECUTION["buffer"], min_notional=105,
        )
        self._locks = {}
        self._exposure_lock = Lock()
        self.manual_closed_symbols = set()
        self._clock = time.time

    def _mark_pending_reconciliation(self, position, result):
        position.reconciliation_status = "PENDING_RECONCILIATION"
        position.reconciliation_attempts += 1
        delay = min(300.0, float(2 ** min(position.reconciliation_attempts - 1, 8)))
        position.next_reconciliation_ts = self._clock() + delay
        position.last_reconciliation_error = result.error or result.status.value
        self.engine.snapshot_manager.save_position(position)

    @staticmethod
    def _mark_reconciliation_ready(position):
        position.reconciliation_status = "READY"
        position.reconciliation_attempts = 0
        position.next_reconciliation_ts = 0.0
        position.last_reconciliation_error = None

    @staticmethod
    def _sl_explicit_order_ids(leg, history):
        ids = set()
        if leg.sl_materialized_order_id:
            ids.add(int(leg.sl_materialized_order_id))
        for algo in history.algo_orders:
            algo_id = algo.get("algoId")
            client_id = algo.get("clientAlgoId") or algo.get("clientOrderId")
            if not (
                (leg.sl_algo_id and algo_id and int(algo_id) == int(leg.sl_algo_id))
                or (leg.sl_client_algo_id and client_id == leg.sl_client_algo_id)
            ):
                continue
            actual = algo.get("actualOrderId") or algo.get("triggeredOrderId")
            if actual:
                ids.add(int(actual))
        for order in history.orders:
            client_id = order.get("clientOrderId") or order.get("origClientOrderId")
            if leg.sl_client_algo_id and client_id == leg.sl_client_algo_id and order.get("orderId"):
                ids.add(int(order["orderId"]))
        return ids

    def _resolve_unmatched_fill(self, raw, position, history, fills):
        order_id = int(raw.get("orderId", 0))
        explicit = [
            leg for leg in position.entry_legs
            if order_id in self._sl_explicit_order_ids(leg, history)
        ]
        if len(explicit) == 1:
            explicit[0].sl_materialized_order_id = order_id
            return explicit[0]
        if len(explicit) > 1:
            return None

        order = next((x for x in history.orders if int(x.get("orderId", 0)) == order_id), None)
        if not order or not bool(order.get("reduceOnly")):
            return None
        grouped_qty = sum(
            abs(float(x.get("qty", 0))) for x in fills
            if int(x.get("orderId", 0)) == order_id
        )
        fill_time = int(raw.get("time", 0))
        candidates = []
        for leg in position.entry_legs:
            close_side = "SELL" if position.side == "LONG" else "BUY"
            qty_tolerance = max(1e-9, leg.initial_quantity * 1e-6)
            if (
                order.get("symbol", leg.symbol) == leg.symbol
                and order.get("side") == close_side
                and fill_time >= leg.entry_ts
                and abs(grouped_qty - leg.remaining_quantity) <= qty_tolerance
            ):
                candidates.append(leg)
        if len(candidates) == 1:
            candidates[0].sl_materialized_order_id = order_id
            return candidates[0]
        return None

    def _resolve_manual_close(self, position, history):
        close_side = "SELL" if position.side == "LONG" else "BUY"
        known_ids = {
            int(value) for leg in position.entry_legs
            for value in (leg.tp_order_id, leg.sl_materialized_order_id)
            if value
        }
        open_legs = [leg for leg in position.entry_legs if leg.remaining_quantity > 1e-9]
        if len(open_legs) != 1:
            return None
        leg = open_legs[0]
        candidates = []
        for order in history.orders:
            order_id = int(order.get("orderId", 0))
            quantity = abs(float(order.get("executedQty", 0) or 0))
            event_ts = int(order.get("updateTime", order.get("time", 0)) or 0)
            tolerance = max(1e-9, leg.remaining_quantity * 1e-6)
            if (
                order_id not in known_ids
                and order.get("symbol", leg.symbol) == leg.symbol
                and order.get("side") == close_side
                and bool(order.get("reduceOnly"))
                and order.get("status") == "FILLED"
                and event_ts >= leg.entry_ts
                and abs(quantity - leg.remaining_quantity) <= tolerance
                and float(order.get("avgPrice", 0) or 0) > 0
            ):
                candidates.append((leg, order, quantity, event_ts))
        return candidates[0] if len(candidates) == 1 else None

    def _journal_closed_leg(self, position, leg, normalized, reason):
        leg.exit_ts = leg.exit_ts or normalized["timestamp"]
        leg.exit_reason = leg.exit_reason or reason
        leg.status = "CLOSED"
        self.engine.snapshot_manager.save_position(position)
        self.engine.journal.log_leg(position, leg)
        leg.status = "JOURNALED"
        self.engine.snapshot_manager.save_position(position)

    def _exchange_state(self, symbol):
        return self.engine.position_manager.sync(symbol)

    def _safety_close_added(self, symbol, side, added, before_qty):
        close_side = "SELL" if side == "LONG" else "BUY"
        try:
            self.exchange.close_position(symbol, close_side, added)
        except Exception:
            pass
        state = self._exchange_state(symbol)
        tolerance = max(1e-9, added * 1e-6)
        if state is not None and float(state["quantity"]) > before_qty + tolerance:
            # The added exposure cannot be isolated reliably: close the physical position.
            self.exchange.close_position(symbol, close_side, float(state["quantity"]))
            state = self._exchange_state(symbol)
        if state is not None and float(state["quantity"]) > before_qty + tolerance:
            raise RuntimeError("unable to close unprotected bucket exposure")
        return state

    def execute(self, plan):
        lock = self._locks.setdefault(plan.symbol, Lock())
        if not lock.acquire(blocking=False):
            return False
        if not self._exposure_lock.acquire(blocking=False):
            lock.release()
            return False
        try:
            return self._execute(plan)
        finally:
            self._exposure_lock.release()
            lock.release()

    def _execute(self, plan):
        if plan.execution_variant not in {ExecutionVariant.BUCKET_V1, ExecutionVariant.BUCKET_V2}:
            raise ValueError("BucketExecutionService only accepts bucket variants")
        local = self.engine.positions.get(plan.symbol)
        before = self._exchange_state(plan.symbol)
        if before == "INVALID_SYMBOL": return False
        if local and local.execution_variant is ExecutionVariant.LEGACY_UNKNOWN:
            return False
        if local:
            local.validate_entry_legs()
            if before is None or before["side"] != plan.side:
                return False
            if any(x.deduplication_key == plan.deduplication_key for x in local.entry_legs):
                return False
            if any(x.variant is plan.execution_variant for x in local.entry_legs):
                return False
            if len(local.entry_legs) >= BUCKET_EXECUTION["max_legs_per_symbol"]:
                return False
            try:
                self.protection.verify_position(local)
            except Exception:
                return False
        elif before is not None:
            # A real orphan/legacy position cannot be increased.
            return False
        unique_symbols = len(self.engine.positions)
        if not local and unique_symbols >= BUCKET_EXECUTION["max_unique_symbols"]:
            return False
        balance = float(self.exchange.get_wallet_balance())
        price = float(self.exchange.get_price(plan.symbol))
        size = self.position_sizer.calculate(
            balance, price, BUCKET_EXECUTION["leverage"],
            open_positions_count=unique_symbols if not local else 0,
            size_fraction=plan.size_fraction,
            fixed_max_positions=BUCKET_EXECUTION["max_unique_symbols"],
        )
        valid, _ = self.engine.position_sizer.validate(size)
        if not valid: return False
        quantity = float(self.exchange.normalize_quantity(plan.symbol, size["quantity"]))
        if quantity <= 0: return False
        before_qty = float(before["quantity"]) if before else 0.0
        before_average = float(before["entry_price"]) if before else 0.0
        side = "BUY" if plan.side == "LONG" else "SELL"
        try:
            self.exchange.set_leverage(plan.symbol, BUCKET_EXECUTION["leverage"])
            order = self.exchange.place_market_order(plan.symbol, side, quantity)
        except Exception as exc:
            if getattr(exc, "code", None) != -1007:
                return False
        after = self._exchange_state(plan.symbol)
        if not after or after == "INVALID_SYMBOL" or after["side"] != plan.side:
            return False
        added = float(after["quantity"]) - before_qty
        tolerance = max(1e-9, quantity * 1e-6)
        if added <= tolerance:
            return False
        leg_real_entry = (
            (float(after["entry_price"]) * float(after["quantity"]) - before_average * before_qty)
            / added
        )
        now = int(time.time() * 1000)
        aggregate_id = local.aggregate_position_id if local else str(uuid4())
        identity = EntryLegIdentity(
            str(uuid4()), aggregate_id, plan.symbol, plan.setup_id,
            plan.execution_variant, plan.signal_reason,
            plan.arm_reason, plan.execution_reason,
            int(plan.signal_ts), now,
        )
        leg = EntryLeg(
            identity, float(plan.entry), leg_real_entry,
            added, added, plan.size_fraction, float(plan.tp), float(plan.sl),
            management_profile=(plan.signal_context or {}).get("management_profile"),
            signal_context=plan.signal_context,
        )
        order_id = order.get("orderId") if isinstance(locals().get("order"), dict) else None
        if order_id:
            fee_query = coerce_fill_query_result(
                self.exchange.get_recent_fills(plan.symbol, limit=100)
            )
            if fee_query.ok:
                leg.entry_fees = sum(
                    abs(float(fill.get("commission", 0)))
                    for fill in fee_query.fills
                    if int(fill.get("orderId", 0)) == int(order_id)
                )
        if local is None:
            local = Position(
                symbol=plan.symbol, side=plan.side, quantity=float(after["quantity"]),
                entry_price=float(plan.entry), real_entry=float(after["entry_price"]),
                tp=float(plan.tp), sl=float(plan.sl), entry_ts=now,
                signal_price=float(plan.signal_price), signal_ts=int(plan.signal_ts),
                leverage=BUCKET_EXECUTION["leverage"], signal_context=plan.signal_context,
                aggregate_position_id=aggregate_id,
            )
            local.add_entry_leg(leg)
            self.engine.positions[plan.symbol] = local
        else:
            local.add_entry_leg(leg)
            local.quantity = float(after["quantity"])
            local.real_entry = float(after["entry_price"])
        try:
            self.protection.place(leg, plan)
            for existing_leg in local.entry_legs:
                if existing_leg.leg_id != leg.leg_id and existing_leg.remaining_quantity > 0:
                    self.protection.verify_leg(existing_leg)
            self.protection.verify_position(local)
        except Exception:
            pending = getattr(self.engine, "bucket_touch_manager", None)
            if pending is not None:
                pending.cancel(plan.symbol, "FAILED", "emergency_protection_close")
            reconciled = self._safety_close_added(
                plan.symbol, plan.side, added, before_qty,
            )
            local.entry_legs = [x for x in local.entry_legs if x.leg_id != leg.leg_id]
            if reconciled is None:
                self.engine.positions.pop(plan.symbol, None)
            else:
                local.quantity = float(reconciled["quantity"])
                local.real_entry = float(reconciled["entry_price"])
                local.refresh_aggregate_identity()
            return False
        local.refresh_aggregate_identity()
        self.engine.snapshot_manager.save_position(local)
        print(
            f"[BUCKET EXECUTION] symbol={plan.symbol} "
            f"variant={plan.execution_variant.value} reason={plan.execution_reason} "
            f"quantity={added} size_fraction={plan.size_fraction} timestamp={now}"
        )
        return True

    def reconcile_fills(self, symbol):
        position = self.engine.positions.get(symbol)
        if not position or not position.entry_legs:
            return
        if (
            position.reconciliation_status == "PENDING_RECONCILIATION"
            and self._clock() < position.next_reconciliation_ts
        ):
            return None
        fill_query = coerce_fill_query_result(
            self.exchange.get_recent_fills(symbol, limit=1000)
        )
        if not fill_query.ok:
            self._mark_pending_reconciliation(position, fill_query)
            return fill_query
        self._mark_reconciliation_ready(position)
        fills = fill_query.fills
        order_to_leg = {}
        for leg in position.entry_legs:
            if leg.tp_order_id: order_to_leg[int(leg.tp_order_id)] = leg
            if leg.sl_materialized_order_id:
                order_to_leg[int(leg.sl_materialized_order_id)] = leg
        legs_by_id = {leg.leg_id: leg for leg in position.entry_legs}
        for order_id, leg_id in self.protection.order_to_leg.items():
            if leg_id in legs_by_id:
                order_to_leg[int(order_id)] = legs_by_id[leg_id]
        matched = False
        history = None
        for raw in sorted(fills, key=lambda x: int(x.get("time", 0))):
            close_side = "SELL" if position.side == "LONG" else "BUY"
            if raw.get("side") and raw.get("side") != close_side:
                continue
            if int(raw.get("time", 0)) < min(leg.entry_ts for leg in position.entry_legs):
                continue
            order_id = int(raw.get("orderId", 0))
            leg = order_to_leg.get(order_id)
            if leg is None:
                if history is None:
                    getter = getattr(self.exchange, "get_reconciliation_history", None)
                    history = getter(symbol) if getter else OrderHistoryResult.success()
                if not history.ok:
                    self._mark_pending_reconciliation(position, history)
                    return history
                leg = self._resolve_unmatched_fill(raw, position, history, fills)
            if leg is None:
                self._mark_pending_reconciliation(
                    position,
                    OrderHistoryResult(
                        status=history.status,
                        error=f"unresolved or ambiguous fill orderId={order_id}",
                    ),
                )
                return history
            matched = True
            normalized = {
                "id": f"{order_id}:{raw.get('id', raw.get('tradeId'))}",
                "order_id": order_id, "quantity": abs(float(raw.get("qty", 0))),
                "price": float(raw.get("price", 0)), "timestamp": int(raw.get("time", 0)),
                "fee": abs(float(raw.get("commission", 0))),
            }
            changed = self.protection.process_fill(position, leg, normalized)
            if leg.remaining_quantity <= 1e-9 and leg.status != "JOURNALED":
                self._journal_closed_leg(
                    position, leg, normalized,
                    "TP" if order_id == leg.tp_order_id else "SL",
                )
        state = self._exchange_state(symbol)
        if state is None:
            if not matched:
                if history is None:
                    getter = getattr(self.exchange, "get_reconciliation_history", None)
                    history = getter(symbol) if getter else OrderHistoryResult.success()
                if not history.ok:
                    self._mark_pending_reconciliation(position, history)
                    return history
                manual = self._resolve_manual_close(position, history)
                if manual:
                    leg, order, quantity, event_ts = manual
                    normalized = {
                        "id": f"manual-order:{order['orderId']}",
                        "order_id": int(order["orderId"]), "quantity": quantity,
                        "price": float(order["avgPrice"]), "timestamp": event_ts, "fee": 0.0,
                    }
                    self.protection.process_fill(position, leg, normalized)
                    if leg.remaining_quantity <= 1e-9 and leg.status != "JOURNALED":
                        self._journal_closed_leg(position, leg, normalized, "MANUAL")
                    self.manual_closed_symbols.add(symbol)
                    matched = True
                else:
                    self._mark_pending_reconciliation(
                        position,
                        OrderHistoryResult(
                            status=history.status,
                            error="flat position without unique closing-order evidence",
                        ),
                    )
                    return history
            all_journaled = all(leg.status == "JOURNALED" for leg in position.entry_legs)
            if all_journaled:
                self.engine.positions.pop(symbol, None)
                self.engine.snapshot_manager.clear(symbol)
            else:
                self.engine.snapshot_manager.save_position(position)
        else:
            position.quantity = float(state["quantity"])
            position.real_entry = float(state["entry_price"])
            self.engine.snapshot_manager.save_position(position)
        return fill_query
