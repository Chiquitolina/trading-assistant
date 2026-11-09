"""Read-only chart geometry for expired HLL history.

The extension is a *visual counterfactual guide*, not lifecycle validity,
a detector event, a defense, or a causal PRE candidate.
"""
from __future__ import annotations


def projection_segments(*, birth_ms: int, end_ms: int, first_ms: int,
                        cutoff_ms: int, enabled: bool) -> tuple[tuple[int,int] | None,
                                                                 tuple[int,int] | None]:
    """Return (observed, historical_extension) half-open millisecond segments.

    If a level expired before the window, only the dashed extension is visible.
    Never render a level before formation, after cutoff, or across a future end.
    """
    birth, end, first, cutoff = map(int, (birth_ms, end_ms, first_ms, cutoff_ms))
    if end < birth:
        raise ValueError('HLL end precedes formation')
    if cutoff <= first:
        raise ValueError('Invalid chart interval')
    if birth >= cutoff:
        return None, None
    observed_start, observed_end = max(first, birth), min(cutoff, end)
    observed = (observed_start, observed_end) if observed_start < observed_end else None
    extension_start = max(first, end, birth)
    extension = ((extension_start, cutoff) if enabled and end < cutoff
                 and extension_start < cutoff else None)
    return observed, extension
