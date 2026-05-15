import pandas as pd

from signals.indicators.atr import add_atr
from signals.strategy.filters import min_expected_tp_ok
from signals.strategy.risk import compute_levels
from engine.live.strategy.trade_plan import TradePlan
from config.strategies.v1 import (
    LONG,
    SHORT,
    LONG_AGGRESSIVE,
    SHORT_AGGRESSIVE,
)
from engine.live.status_writer import StatusWriter

from models.signals import Signal
from models.trade_action import TradeAction

class EntryEngine:
    def __init__(self, buffer, debug=True, status_writer=None, config=None, symbol=None):
        self.buffer = buffer
        self.debug = debug
        self.status_writer = status_writer or StatusWriter()
        
        self.config = config or {}
        
        self.symbol = symbol
        
        self.max_hold_candles = int(
            self.config.get("max_hold_candles", 24)
        )

        self.entry_tf = self.config.get("entry_tf", "15m")
        self.atr_tf = self.config.get("atr_tf", "15m")
        self.min_atr = self.config.get("min_atr", None)

        self.min_atr_pct = self.config.get(
            "min_atr_pct",
            None
        )
        
    def _calculate_atr_pct(
        self,
        atr: float,
        price: float
    ) -> float:

        if price <= 0:
            return 0.0

        return (atr / price) * 100

    def generate_entry(self, trade_action: TradeAction):

        if not trade_action:
            self.status_writer.write_plan(
                status="SKIPPED",
                reason="no_signal",
            )
            return None

        signal = trade_action.signal

        side = trade_action.action.value

        if side not in ("LONG", "SHORT"):
            self.status_writer.write_plan(
                status="SKIPPED",
                reason="invalid_side",
                side=side,
            )
            return None

        # ==========================
        # DATA
        # ==========================
        plan_symbol = getattr(signal, "symbol", None) or self.symbol

        if not plan_symbol:
            raise ValueError("EntryEngine: missing symbol from signal and self.symbol")

        df_entry = pd.DataFrame(
            self.buffer.get_candles(plan_symbol, self.entry_tf)
        )

        df_atr = pd.DataFrame(
            self.buffer.get_candles(plan_symbol, self.atr_tf)
        )

        if len(df_entry) < 20:
            self.status_writer.write_plan(
                status="SKIPPED",
                reason=f"not_enough_{self.entry_tf}_data",
                side=side,
            )
            return None

        if len(df_atr) < 20:
            self.status_writer.write_plan(
                status="SKIPPED",
                reason=f"not_enough_{self.atr_tf}_data",
                side=side,
            )
            return None

        df_atr = add_atr(df_atr, period=14)

        entry_candle = df_entry.iloc[-1]

        signal_price = signal.signal_price
        signal_ts = signal.signal_ts

        entry = entry_candle["open"]

        atr = df_atr.iloc[-1]["atr"]

        if pd.isna(atr):
            self.status_writer.write_plan(
                status="SKIPPED",
                reason="atr_nan",
                side=side,
                entry=round(entry, 2),
                atr=None,
                atr_pct=None
            )
            return None
        
        atr_pct = self._calculate_atr_pct(
            atr,
            entry
        )

        strategy_name = (trade_action.strategy_name or "").lower()

        if "aggressive" in strategy_name:
            cfg = LONG_AGGRESSIVE if side == "LONG" else SHORT_AGGRESSIVE
        elif strategy_name in ("default_strategy", "direction_strategy"):
            cfg = LONG if side == "LONG" else SHORT
        else:
            cfg = LONG if side == "LONG" else SHORT

        # ==========================
        # LEVELS
        # ==========================
        sl, tp, sl_pct, tp_pct = compute_levels(
            side=side,
            entry=entry,
            atr=atr,
            cfg=cfg
        )

        # ==========================
        # 🔴 VOLATILITY FILTER
        # ==========================

        if self.min_atr_pct is not None:

            if atr_pct < self.min_atr_pct:

                if self.debug:
                    print(
                        f"⛔ Entry descartado: ATR% insuficiente "
                        f"({atr_pct:.4f}% < {self.min_atr_pct:.4f}%) | "
                        f"atr={atr:.2f} | "
                        f"price={entry:.2f}"
                    )

                self.status_writer.write_plan(
                    status="DISCARDED",
                    reason="low_atr_pct",
                    side=side,
                    entry=round(entry, 2),
                    tp=round(tp, 2),
                    sl=round(sl, 2),
                    atr=round(atr, 2),
                    atr_pct=round(atr_pct, 4)
                )

                return None

        elif self.min_atr is not None:

            if atr < self.min_atr:

                if self.debug:
                    print(
                        f"⛔ Entry descartado: ATR insuficiente "
                        f"({atr:.2f} < {self.min_atr})"
                    )

                self.status_writer.write_plan(
                    status="DISCARDED",
                    reason="low_atr",
                    side=side,
                    entry=round(entry, 2),
                    tp=round(tp, 2),
                    sl=round(sl, 2),
                    atr=round(atr, 2),
                    atr_pct=round(atr_pct, 4)
                )

                return None

        # ==========================
        # TP ESPERADO
        # ==========================
        ok, expected_tp_pct = min_expected_tp_ok(
            entry,
            atr,
            cfg["tp_mult"],
            cfg["min_tp"]
        )

        if not ok:
            if self.debug:
                print(f"⛔ Entry descartado: TP esperado insuficiente ({expected_tp_pct:.2f}% < {cfg['min_tp']}%)")

            self.status_writer.write_plan(
                status="DISCARDED",
                reason="min_tp_not_met",
                side=side,
                entry=round(entry, 2),
                tp=round(tp, 2),
                sl=round(sl, 2),
                atr=round(atr, 2),
                atr_pct=round(atr_pct, 4)
            )
            return None


        # ==========================
        # CONTEXT
        # ==========================
        signal_context = {
            "symbol": plan_symbol,
            "trend": signal.trend.value,
            "direction": signal.direction.value,
            "momentum": signal.momentum.value,

            "strategy_name": trade_action.strategy_name,
             # 🔥 ROUTER
            "router_reason": trade_action.reason,
            "strategy_reason": trade_action.reason,

            "momentum_prev1": (
                signal.momentum_prev1.value
                if signal.momentum_prev1 else None
            ),

            "momentum_prev2": (
                signal.momentum_prev2.value
                if signal.momentum_prev2 else None
            ),

            "momentum_sequence": [
                m.value if m else None
                for m in signal.momentum_sequence
            ],

            "signal_atr": float(atr),
            "signal_atr_pct": round(float(atr_pct), 4),

            "atr": float(atr),
            "atr_pct": round(float(atr_pct), 4),
            
            "risk_config": "AGGRESSIVE" if "aggressive" in strategy_name else "DEFAULT",
            "sl_mult": cfg["sl_mult"],
            "tp_mult": cfg["tp_mult"],
            "min_tp": cfg["min_tp"],
            
            # ==========================
            # HTF EXTENSION CONTEXT
            # ==========================
            "dist_ema50_15m_pct": signal.dist_ema50_15m_pct,
            "dist_ema99_15m_pct": signal.dist_ema99_15m_pct,

            "dist_ema50_1h_pct": signal.dist_ema50_1h_pct,
            "dist_ema99_1h_pct": signal.dist_ema99_1h_pct,

            "dist_ema50_4h_pct": signal.dist_ema50_4h_pct,
            "dist_ema99_4h_pct": signal.dist_ema99_4h_pct,
        }

        plan = TradePlan(
            symbol=plan_symbol,
            quantity=0.001,
            side=side,
            entry=round(entry, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            sl_pct=round(sl_pct, 3),
            tp_pct=round(tp_pct, 3),
            atr=round(atr, 2),
            atr_pct=round(atr_pct, 4),
            timestamp=entry_candle["timestamp"],
            reason=trade_action.strategy_name,
            signal_price=round(signal_price, 2),
            signal_ts=signal_ts,
            signal_context=signal_context,
            max_hold_candles=self.max_hold_candles
        )
        
        # ==========================
        # ✅ PLAN READY
        # ==========================
        self.status_writer.write_plan(
            status="READY",
            reason=trade_action.reason,
            side=plan.side,
            entry=plan.entry,
            tp=plan.tp,
            sl=plan.sl,
            atr=plan.atr,  # 🔥 CLAVE
            atr_pct=round(atr_pct, 4)
        )

        if self.debug:
            print(plan.pretty())

        return plan