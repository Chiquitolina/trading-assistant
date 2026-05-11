class BaseExecutionStrategy:

    def on_signal(self, execution_engine, trade_action, plan):
        raise NotImplementedError

    def on_price_update(self, execution_engine, price: float, timestamp: int):
        pass

    def on_candle_close(self, execution_engine, closed_candle_ts: int, close_price: float):
        pass

    def update_position_context(
        self,
        execution_engine,
        trend: str,
        direction: str,
        momentum: str,
        current_price: float
    ):
        pass