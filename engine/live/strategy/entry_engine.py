import pandas as pd

from signals.indicators.atr import add_atr
from signals.strategy.filters import min_expected_tp_ok
from signals.strategy.risk import compute_levels
from engine.live.strategy.trade_plan import TradePlan
from config.strategies.v1 import LONG, SHORT
from engine.live.status_writer import StatusWriter

MIN_ATR = 201


class EntryEngine:
    def __init__(self, buffer, debug=True, status_writer=None):
        self.buffer = buffer
        self.debug = debug
        self.status_writer = status_writer or StatusWriter()

    def generate_entry(self, signal: dict):
        if not signal:
            self.status_writer.write_plan(
                status="SKIPPED",
                reason="no_signal",
            )
            return None

        side = signal["side"]

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
        df_15m = pd.DataFrame(self.buffer.get_candles("15m"))
        if len(df_15m) < 20:
            self.status_writer.write_plan(
                status="SKIPPED",
                reason="not_enough_15m_data",
                side=side,
            )
            return None

        df_15m = add_atr(df_15m, period=14)

        entry_candle = df_15m.iloc[-1]

        signal_price = signal["signal_price"]
        signal_ts = signal["signal_ts"]

        entry = entry_candle["open"]

        atr = df_15m.iloc[-2]["atr"]

        if pd.isna(atr):
            self.status_writer.write_plan(
                status="SKIPPED",
                reason="atr_nan",
                side=side,
                entry=round(entry, 2),
                atr=None,
            )
            return None

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
        # 🔴 FILTRO ATR
        # ==========================
        if atr < MIN_ATR:
            if self.debug:
                print(f"⛔ Entry descartado: ATR insuficiente ({atr:.2f} < {MIN_ATR})")

            self.status_writer.write_plan(
                status="DISCARDED",
                reason="low_atr",
                side=side,
                entry=round(entry, 2),
                tp=round(tp, 2),
                sl=round(sl, 2),
                atr=round(atr, 2),  # 🔥
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
                atr=round(atr, 2),  # 🔥
            )
            return None

        # ==========================
        # CONTEXT
        # ==========================
        signal_context = {
            "trend": signal.get("trend"),
            "direction": signal.get("direction"),
            "momentum": signal.get("momentum"),
            "atr": round(atr, 2),
        }

        plan = TradePlan(
            symbol="BTCUSDT",
            quantity=0.001,
            side=side,
            entry=round(entry, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            sl_pct=round(sl_pct, 3),
            tp_pct=round(tp_pct, 3),
            atr=round(atr, 2),
            timestamp=entry_candle["timestamp"],
            reason="strategy_v1",
            signal_price=round(signal_price, 2),
            signal_ts=signal_ts,
            signal_context=signal_context
        )

        # ==========================
        # ✅ PLAN READY
        # ==========================
        self.status_writer.write_plan(
            status="READY",
            reason="strategy_v1",
            side=plan.side,
            entry=plan.entry,
            tp=plan.tp,
            sl=plan.sl,
            atr=plan.atr,  # 🔥 CLAVE
        )

        if self.debug:
            print(plan.pretty())

        return plan