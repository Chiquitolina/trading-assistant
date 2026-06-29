from engine.live.execution.strategies.direction_execution_strategy import DirectionExecutionStrategy


class CompressionExecutionStrategy(DirectionExecutionStrategy):
    
    BE_TRIGGER = None

    # ==========================================================
    # SIGNAL → OPEN / IGNORE
    # ==========================================================
    def on_signal(self, execution_engine, trade_action, plan):

        pos = execution_engine.get_position(plan.symbol)

        if not pos:
            print("[COMPRESSION] open position")
            return execution_engine.open_position(plan)

        if pos.side == plan.side:
            print("[COMPRESSION] same side -> HOLD")
            return False

        print("[COMPRESSION] opposite signal ignored")
        return False

    # ==========================================================
    # PRICE UPDATE → usa lógica heredada de Direction
    # ==========================================================
    # Hereda:
    # - current_pnl
    # - mae / mfe
    # - BE si BE_TRIGGER no es None

    # ==========================================================
    # CANDLE CLOSE → reservado para compresión
    # ==========================================================
    def on_candle_close(self, execution_engine, closed_candle_ts, close_price):
        return