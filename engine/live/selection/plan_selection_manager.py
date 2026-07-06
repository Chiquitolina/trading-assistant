import logging
from collections import defaultdict

from engine.live.selection.candidate_scorer import CandidateScorer
from engine.live.selection.trade_candidate import TradeCandidate
from engine.live.strategy.trade_plan import TradePlan
from models.trade_action import TradeAction


logger = logging.getLogger(__name__)


class PlanSelectionManager:
    """
    Recibe TradePlans y los convierte en TradeCandidates.

    Los candidatos quedan acumulados por window_id.
    La ventana se resuelve solamente cuando el main termina
    de procesar el batch completo.
    """

    def __init__(
        self,
        scorer: CandidateScorer,
        max_selected_per_window: int = 2,
        minimum_score: float | None = None,
    ):
        if max_selected_per_window < 1:
            raise ValueError(
                "max_selected_per_window must be >= 1"
            )

        self.scorer = scorer
        self.max_selected_per_window = max_selected_per_window
        self.minimum_score = minimum_score

        self._windows: dict[int, list[TradeCandidate]] = defaultdict(list)

    # ==========================================================
    # CANDIDATE CREATION
    # ==========================================================

    def add_plan(
        self,
        plan: TradePlan,
        trade_action: TradeAction,
        window_id: int,
    ) -> TradeCandidate:

        candidate = TradeCandidate(
            plan=plan,
            trade_action=trade_action,
            window_id=int(window_id),
        )

        score_result = self.scorer.score(candidate)

        candidate.score = score_result.total
        candidate.score_breakdown = score_result.breakdown

        self._windows[candidate.window_id].append(candidate)

        print(
            f"[SELECTION] candidate added | "
            f"window={candidate.window_id} "
            f"symbol={candidate.symbol} "
            f"side={candidate.side} "
            f"score={candidate.score:.4f}"
        )

        return candidate

    # ==========================================================
    # WINDOW RESOLUTION
    # ==========================================================

    def resolve_window(
        self,
        window_id: int,
    ) -> tuple[list[TradeCandidate], list[TradeCandidate]]:

        window_id = int(window_id)

        candidates = self._windows.pop(window_id, [])

        if not candidates:
            return [], []

        # Más adelante el score decidirá el orden.
        # Con NoOpCandidateScorer todos tienen score 0,
        # por lo que Python conserva el orden de llegada.
        ranked = sorted(
            candidates,
            key=lambda candidate: candidate.score,
            reverse=True,
        )

        eligible: list[TradeCandidate] = []

        for rank, candidate in enumerate(ranked, start=1):
            candidate.rank = rank

            if not candidate.eligible:
                if not candidate.rejection_reason:
                    candidate.rejection_reason = "not_eligible"
                continue

            if (
                self.minimum_score is not None
                and candidate.score < self.minimum_score
            ):
                candidate.reject("below_minimum_score")
                continue

            eligible.append(candidate)

        selected = eligible[
            : self.max_selected_per_window
        ]

        overflow = eligible[
            self.max_selected_per_window :
        ]

        for candidate in selected:
            candidate.mark_selected(
                rank=candidate.rank or 0
            )

        for candidate in overflow:
            candidate.reject(
                "max_selected_per_window"
            )

        rejected = [
            candidate
            for candidate in ranked
            if not candidate.selected
        ]

        self._print_resolution(
            window_id=window_id,
            ranked=ranked,
            selected=selected,
            rejected=rejected,
        )

        return selected, rejected

    # ==========================================================
    # WINDOW STATE
    # ==========================================================

    def pending_window_ids(self) -> list[int]:
        return list(self._windows.keys())

    def pending_candidates(
        self,
        window_id: int,
    ) -> list[TradeCandidate]:
        return list(
            self._windows.get(int(window_id), [])
        )

    def clear_window(self, window_id: int) -> None:
        self._windows.pop(int(window_id), None)

    def clear_all(self) -> None:
        self._windows.clear()

    # ==========================================================
    # LOGGING
    # ==========================================================

    @staticmethod
    def _print_resolution(
        window_id: int,
        ranked: list[TradeCandidate],
        selected: list[TradeCandidate],
        rejected: list[TradeCandidate],
    ) -> None:

        print("")
        print(
            f"[SELECTION WINDOW] "
            f"window={window_id} "
            f"candidates={len(ranked)} "
            f"selected={len(selected)} "
            f"rejected={len(rejected)}"
        )

        for candidate in ranked:
            print(
                f"[SELECTION RANK] "
                f"rank={candidate.rank} "
                f"symbol={candidate.symbol} "
                f"side={candidate.side} "
                f"score={candidate.score:.4f} "
                f"selected={candidate.selected} "
                f"reason={candidate.rejection_reason}"
            )

        print("")