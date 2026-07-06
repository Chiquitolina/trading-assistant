from dataclasses import dataclass, field
from typing import Protocol

from engine.live.selection.trade_candidate import TradeCandidate


@dataclass
class CandidateScore:
    total: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


class CandidateScorer(Protocol):
    """
    Contrato para cualquier scorer futuro.
    """

    def score(
        self,
        candidate: TradeCandidate,
    ) -> CandidateScore:
        ...


class NoOpCandidateScorer:
    """
    Scorer temporal.

    No evalúa todavía:
    - contexto BTC
    - swings
    - compresión
    - volumen
    - tendencia

    Solamente permite construir la arquitectura.
    """

    def score(
        self,
        candidate: TradeCandidate,
    ) -> CandidateScore:
        return CandidateScore(
            total=0.0,
            breakdown={},
        )