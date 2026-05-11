from engine.live.execution.strategies.base_execution_strategy import BaseExecutionStrategy
from enums.actions import Action


class DefaultExecutionStrategy(BaseExecutionStrategy):

    # ==========================================================
    # SIGNAL HANDLING (ENGINE VIEJO EXACTO)
    # ==========================================================
    def on_signal(self, execution_engine, trade_action, plan):

        if trade_action.action == Action.HOLD:
            return False

        pos = execution_engine.position

        # NO POSITION → OPEN
        if not pos:
            print("[DEFAULT] open position")
            return execution_engine.open_position(plan)

        # SAME SIDE → IGNORE
        if pos.side == plan.side:
            print("[DEFAULT] same side -> HOLD")
            return False

        # ❌ NO FLIP
        print("[DEFAULT] opposite signal ignored")
        return False

    # ==========================================================
    # PRICE UPDATE (ENGINE VIEJO COMPLETO)
    # ==========================================================
    def on_price_update(self, execution_engine, price, timestamp):

        pos = execution_engine.position
        if not pos:
            return

        pnl = (
            (price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - price) / pos.real_entry * 100
        )

        pos.current_pnl = pnl
        pos.mae = min(pos.mae, pnl)
        pos.mfe = max(pos.mfe, pnl)

        # ==========================
        # BREAK EVEN
        # ==========================
        BE_TRIGGER = 0.20

        if pnl >= BE_TRIGGER:

            if pos.side == "LONG" and pos.sl < pos.real_entry:
                pos.sl = pos.real_entry
                print("[DEFAULT][BE] LONG -> entry")

            elif pos.side == "SHORT" and pos.sl > pos.real_entry:
                pos.sl = pos.real_entry
                print("[DEFAULT][BE] SHORT -> entry")

        # ==========================
        # TRAILING STOP
        # ==========================
        TRAIL_TRIGGER = 0.40

        if pnl >= TRAIL_TRIGGER:

            offset = 0.15

            if pos.side == "LONG":
                new_sl = pos.real_entry * (1 + (pnl - offset) / 100)
                if new_sl > pos.sl:
                    pos.sl = new_sl
                    print(f"[DEFAULT][TRAIL] LONG SL -> {new_sl:.4f}")

            else:
                new_sl = pos.real_entry * (1 - (pnl - offset) / 100)
                if new_sl < pos.sl:
                    pos.sl = new_sl
                    print(f"[DEFAULT][TRAIL] SHORT SL -> {new_sl:.4f}")

    # ==========================================================
    # CANDLE CLOSE → TIME STOP
    # ==========================================================
    def on_candle_close(self, execution_engine, closed_candle_ts, close_price):

        pos = execution_engine.position
        if not pos:
            return

        candles = execution_engine._calc_candles_in_trade(
            closed_candle_ts,
            tf_minutes=15
        )

        pos.candles_in_trade = candles

        if candles >= 24:

            print("[DEFAULT][TIME STOP] closing position")

            execution_engine._close_position(
                price=close_price,
                timestamp=closed_candle_ts,
                reason="TIME_STOP"
            )

    # ==========================================================
    # CONTEXT UPDATE (ENGINE VIEJO EXACTO)
    # ==========================================================
    def update_position_context(
        self,
        execution_engine,
        trend,
        direction,
        momentum,
        current_price
    ):

        pos = execution_engine.position
        if not pos:
            return

        pos.current_trend = trend
        pos.current_direction = direction
        pos.current_momentum = momentum

        pnl = (
            (current_price - pos.real_entry) / pos.real_entry * 100
            if pos.side == "LONG"
            else (pos.real_entry - current_price) / pos.real_entry * 100
        )

        pos.current_pnl = pnl
        pos.mae = min(pos.mae, pnl)
        pos.mfe = max(pos.mfe, pnl)