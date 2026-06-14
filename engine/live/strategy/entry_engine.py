import pandas as pd
import csv
from pathlib import Path

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
from signals.utils.market_metrics import build_liquidity_context

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
        
            
    def _log_blocked_signal(self, trade_action: TradeAction, reason: str):
        signal = trade_action.signal

        self.status_writer.write_plan(
            status="BLOCKED",
            reason=reason,
            side=trade_action.action.value,
            entry=getattr(signal, "signal_price", None),
        )

        self._append_paper_signal_csv(trade_action, reason)

        print(
            f"[PAPER SIGNAL] {reason} | "
            f"{getattr(signal, 'symbol', self.symbol)} | "
            f"{trade_action.action.value} | "
            f"strategy={trade_action.strategy_name} | "
            f"router_reason={trade_action.reason}"
        )
        
    def _append_paper_signal_csv(self, trade_action: TradeAction, reason: str):
        signal = trade_action.signal

        path = Path("paper_signals.csv")
        exists = path.exists()

        row = {
            "ts": getattr(signal, "signal_ts", None),
            "symbol": getattr(signal, "symbol", self.symbol),
            "side": trade_action.action.value,
            "reason": reason,
            "strategy_name": trade_action.strategy_name,
            "router_reason": trade_action.reason,
            "signal_price": getattr(signal, "signal_price", None),

            "signal_trend": getattr(getattr(signal, "trend", None), "value", None),
            "signal_direction": getattr(getattr(signal, "direction", None), "value", None),
            "signal_momentum": getattr(getattr(signal, "momentum", None), "value", None),

            "btc_velocity_15m": getattr(signal, "btc_velocity_15m", None),
            "btc_velocity_1h": getattr(signal, "btc_velocity_1h", None),
            "btc_direction_15m": getattr(signal, "btc_direction_15m", None),
            "btc_direction_1h": getattr(signal, "btc_direction_1h", None),
            "btc_context_state": getattr(signal, "btc_context_state", None),
            "btc_context_reason": getattr(signal, "btc_context_reason", None),
        }

        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())

            if not exists:
                writer.writeheader()

            writer.writerow(row)
        
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
        
        allow_longs = self.config.get("allow_longs", True)
        allow_shorts = self.config.get("allow_shorts", True)
        log_blocked = self.config.get("log_blocked_signals", True)

        if side == "LONG" and not allow_longs:
            if log_blocked:
                self._log_blocked_signal(trade_action, reason="LONG_DISABLED")
            return None

        if side == "SHORT" and not allow_shorts:
            if log_blocked:
                self._log_blocked_signal(trade_action, reason="SHORT_DISABLED_PAPER_ONLY")
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

        entry = self.buffer.last_price(plan_symbol)

        if entry is None:
            entry = signal_price

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
        
        #print(
        #    f"[ENTRY FILTER DEBUG] "
        #    f"symbol={plan_symbol} "
        #    f"side={side} "
        #    f"entry_tf={self.entry_tf} "
        #    f"atr_tf={self.atr_tf} "
        #    f"entry={entry:.6f} "
        #    f"atr={atr:.6f} "
        #    f"atr_pct={atr_pct:.4f}% "
        #    f"min_atr={self.min_atr} "
        #    f"min_atr_pct={self.min_atr_pct}"
        #)

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
            "dist_ema50_15m_pct": getattr(signal, "dist_ema50_15m_pct", None),
            "dist_ema99_15m_pct": getattr(signal, "dist_ema99_15m_pct", None),

            "dist_ema50_1h_pct": getattr(signal, "dist_ema50_1h_pct", None),
            "dist_ema99_1h_pct": getattr(signal, "dist_ema99_1h_pct", None),

            "dist_ema50_4h_pct": getattr(signal, "dist_ema50_4h_pct", None),
            "dist_ema99_4h_pct": getattr(signal, "dist_ema99_4h_pct", None),
            
            "dist_ema20_15m_pct": getattr(signal, "dist_ema20_15m_pct", None),
            "dist_ema20_1h_pct": getattr(signal, "dist_ema20_1h_pct", None),
            "dist_ema20_4h_pct": getattr(signal, "dist_ema20_4h_pct", None),
            
            # ==========================
            # RECENT MOVE CONTEXT - 15m
            # ==========================
            "move_5_bars_pct": getattr(signal, "move_5_bars_pct", None),
            "move_10_bars_pct": getattr(signal, "move_10_bars_pct", None),

            "green_candles_last_10": getattr(signal, "green_candles_last_10", None),
            "red_candles_last_10": getattr(signal, "red_candles_last_10", None),
            
            # ==========================
            # HTF SWING CONTEXT
            # ==========================
            "dist_swing_low_15m_pct": getattr(signal, "dist_swing_low_15m_pct", None),
            "dist_swing_high_15m_pct": getattr(signal, "dist_swing_high_15m_pct", None),

            "dist_swing_low_1h_pct": getattr(signal, "dist_swing_low_1h_pct", None),
            "dist_swing_high_1h_pct": getattr(signal, "dist_swing_high_1h_pct", None),

            "dist_swing_low_4h_pct": getattr(signal, "dist_swing_low_4h_pct", None),
            "dist_swing_high_4h_pct": getattr(signal, "dist_swing_high_4h_pct", None),

            "near_swing_low_15m": getattr(signal, "near_swing_low_15m", None),
            "near_swing_high_15m": getattr(signal, "near_swing_high_15m", None),

            "near_swing_low_1h": getattr(signal, "near_swing_low_1h", None),
            "near_swing_high_1h": getattr(signal, "near_swing_high_1h", None),

            "near_swing_low_4h": getattr(signal, "near_swing_low_4h", None),
            "near_swing_high_4h": getattr(signal, "near_swing_high_4h", None),
            
            "swing_low_15m": getattr(signal, "swing_low_15m", None),
            "swing_high_15m": getattr(signal, "swing_high_15m", None),

            "swing_low_1h": getattr(signal, "swing_low_1h", None),
            "swing_high_1h": getattr(signal, "swing_high_1h", None),

            "swing_low_4h": getattr(signal, "swing_low_4h", None),
            "swing_high_4h": getattr(signal, "swing_high_4h", None),
            
            # ==========================
            # BTC SWING CONTEXT
            # ==========================
            "btc_dist_swing_low_1h_pct": getattr(signal, "btc_dist_swing_low_1h_pct", None),
            "btc_dist_swing_high_1h_pct": getattr(signal, "btc_dist_swing_high_1h_pct", None),
            "btc_near_swing_low_1h": getattr(signal, "btc_near_swing_low_1h", None),
            "btc_near_swing_high_1h": getattr(signal, "btc_near_swing_high_1h", None),

            "btc_dist_swing_low_4h_pct": getattr(signal, "btc_dist_swing_low_4h_pct", None),
            "btc_dist_swing_high_4h_pct": getattr(signal, "btc_dist_swing_high_4h_pct", None),
            "btc_near_swing_low_4h": getattr(signal, "btc_near_swing_low_4h", None),
            "btc_near_swing_high_4h": getattr(signal, "btc_near_swing_high_4h", None),

            "btc_dist_swing_low_1d_pct": getattr(signal, "btc_dist_swing_low_1d_pct", None),
            "btc_dist_swing_high_1d_pct": getattr(signal, "btc_dist_swing_high_1d_pct", None),
            "btc_near_swing_low_1d": getattr(signal, "btc_near_swing_low_1d", None),
            "btc_near_swing_high_1d": getattr(signal, "btc_near_swing_high_1d", None),
        }
        
        
        # ==========================
        # LIQUIDITY CONTEXT
        # ==========================
        df_15m = pd.DataFrame(
            self.buffer.get_candles(plan_symbol, "15m")
        )

        df_1h = pd.DataFrame(
            self.buffer.get_candles(plan_symbol, "1h")
        )

        df_4h = pd.DataFrame(
            self.buffer.get_candles(plan_symbol, "4h")
        )

        quote_volume_24h = getattr(signal, "quote_volume_24h", None)
        
        #print(
        #    f"\n[LIQUIDITY DEBUG] {plan_symbol}"
        #)

        print(
            "15m columns:",
            df_15m.columns.tolist()
        )

        if not df_15m.empty:
            print(
                "15m last candle:",
                df_15m.tail(1).to_dict("records")[0]
            )

        liquidity_context = build_liquidity_context(
            df_15m=df_15m,
            df_1h=df_1h,
            df_4h=df_4h,
            quote_volume_24h=quote_volume_24h,
            lookback=20,
        )

        signal_context.update(liquidity_context)

        plan = TradePlan(
            symbol=plan_symbol,
            quantity=0.001,
            side=side,
            entry=float(entry),
            sl=float(sl),
            tp=float(tp),
            sl_pct=round(sl_pct, 3),
            tp_pct=round(tp_pct, 3),
            atr=float(atr),
            atr_pct=round(atr_pct, 4),
            timestamp=entry_candle["timestamp"],
            reason=trade_action.strategy_name,
            signal_price=float(signal_price),
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