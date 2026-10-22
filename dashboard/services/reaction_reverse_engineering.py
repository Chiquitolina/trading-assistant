"""Retest reverse-engineering research: discovery-only rule search and chronological diagnostics.

No API, Redis, SQLite writes, orders, sklearn or fit-on-holdout logic. Inputs
must be historical, already-labelled First Touch joined to frozen causal features.
This is an exploratory screen; an interactive holdout ceases to be a true holdout
once used to iterate the rules. Do not treat this as strategy approval.
"""
from __future__ import annotations

from itertools import combinations
import math

import numpy as np
import pandas as pd

REQUIRED = {"event_key", "symbol", "side", "retest_ts", "date_utc", "outcome"}
RESOLVED = {"TP_FIRST", "SL_FIRST"}


def one_per_symbol_wave(frame: pd.DataFrame, minutes: int = 180):
    """First observed episode member per symbol, regardless of direction.

    Uses event time and ID only, never outcome to choose the survivor.
    Returns (non-overlapping events, excluded count); does not merge episodes
    across symbols that may share BTC-wide shocks (these still need day grouping).
    """
    required = REQUIRED - {"outcome"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Missing episode identity: {required - set(frame.columns)}")
    if frame.empty:
        return frame.copy(), 0
    src = frame.sort_values(["retest_ts", "event_key"], kind="stable")
    last_by_symbol = {}
    chosen = []
    for index, row in src.iterrows():
        symbol = str(row["symbol"])
        ts = int(row["retest_ts"])
        last = last_by_symbol.get(symbol)
        if last is None or ts - last >= int(minutes) * 60_000:
            chosen.append(index)
            last_by_symbol[symbol] = ts
    kept = src.loc[chosen].copy()
    return kept, len(frame) - len(kept)


def chronological_split(frame: pd.DataFrame, train_share: float = 0.6):
    """Split on WHOLE dates; never scatter observations of one day across sets."""
    if not REQUIRED.issubset(frame.columns):
        raise ValueError(f"Missing input fields: {REQUIRED - set(frame.columns)}")
    days = sorted(frame["date_utc"].dropna().astype(str).unique().tolist())
    if len(days) < 4:
        raise ValueError("At least four different days required for temporal screening")
    n = max(2, min(len(days) - 2, int(math.floor(len(days) * train_share))))
    training_days = days[:n]
    future_days = days[n:]
    train = frame.loc[frame.date_utc.astype(str).isin(training_days)].copy()
    validation = frame.loc[frame.date_utc.astype(str).isin(future_days)].copy()
    return train, validation, training_days, future_days


def _rate(tp: int, sl: int):
    return tp / (tp + sl) if tp + sl else float("nan")


def metrics(frame: pd.DataFrame, tp_pct: float, sl_pct: float, cost_pct: float = 0.12):
    """Resolved-only TP-first counts and simplistic net proxy; NO_HIT not priced."""
    c = frame.outcome.value_counts()
    tp = int(c.get("TP_FIRST", 0))
    sl = int(c.get("SL_FIRST", 0))
    n = tp + sl
    rate = _rate(tp, sl)
    net = rate * tp_pct - (1 - rate) * sl_pct - cost_pct if n else float("nan")
    return {"events": int(len(frame)), "resolved": n, "tp": tp, "sl": sl,
            "no_hit": int(c.get("NO_HIT", 0)), "ambiguous": int(c.get("AMBIGUOUS", 0)),
            "not_entered": int(c.get("NOT_ENTERED", 0)),
            "tp_rate": rate, "resolved_net_proxy_pct": net}


def _numeric(frame, fields):
    z = frame.copy()
    for name in fields:
        z[name] = pd.to_numeric(z[name], errors="coerce")
        z[name] = z[name].replace([np.inf, -np.inf], np.nan)
    return z


def _wilson_lower(tp, n, z=1.645):
    if n == 0:
        return 0.0
    p = tp / n
    zz = z * z
    return (p + zz / (2 * n) - z * math.sqrt(p * (1 - p) / n + zz / (4 * n * n))) / (1 + zz / n)


def mask_for(frame: pd.DataFrame, rule):
    """Frozen numeric comparisons, with a NaN never satisfying a rule."""
    mask = np.ones(len(frame), dtype=bool)
    for feature, sign, threshold in rule:
        if feature not in frame:
            return np.zeros(len(frame), dtype=bool)
        val = pd.to_numeric(frame[feature], errors="coerce").to_numpy(dtype=float)
        test = (val >= threshold) if sign == ">=" else (val <= threshold)
        mask &= np.isfinite(val) & test
    return pd.Series(mask, index=frame.index)


def describe_rule(rule, names=None):
    names = names or {}
    return " ∧ ".join(f"{names.get(name, name)} {op} {threshold:.5g}"
                       for name, op, threshold in rule)


def _discover_single(train, features, minimum):
    """Only discovery data is allowed to define quantile thresholds."""
    candidates = []
    total_tp = int(train.outcome.eq("TP_FIRST").sum())
    total_sl = int(train.outcome.eq("SL_FIRST").sum())
    base = _rate(total_tp, total_sl)
    for field in features:
        x = train[field].dropna()
        if len(x) < minimum * 2 or x.nunique() < 4:
            continue
        thresholds = sorted(set(float(t) for t in x.quantile([.25, .5, .75]).values))
        for threshold in thresholds:
            for direction in (">=", "<="):
                rule = ((field, direction, threshold),)
                selected = train.loc[mask_for(train, rule)]
                if len(selected) < minimum or len(selected) > len(train) * .9:
                    continue
                tp = int(selected.outcome.eq("TP_FIRST").sum())
                if _rate(tp, len(selected) - tp) <= base:
                    continue
                lower = _wilson_lower(tp, len(selected))
                candidates.append((rule, lower, len(selected)))
    candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)
    return candidates


def _matched_lift(frame: pd.DataFrame, selected: pd.Series, bin_edges=None):
    """Within-REACTION comparison, conditioned on day, side and volatility.

    Returns a risk-difference weighted by selected examples. The comparison
    sample consists of other REACTIONs, NOT a no-REACTION market control.
    """
    if "pre_realized_vol60_pct" not in frame:
        return float("nan"), 0, 0
    x = pd.to_numeric(frame.pre_realized_vol60_pct, errors="coerce")
    if bin_edges is None:
        finite = x[np.isfinite(x)]
        edges = sorted(set(float(y) for y in finite.quantile([0, .25, .5, .75, 1]).values))
        if len(edges) < 3:
            return float("nan"), 0, 0
        edges[0] = -np.inf
        edges[-1] = np.inf
    else:
        edges = list(bin_edges)
    tmp = frame[["date_utc", "side", "outcome"]].copy()
    tmp["selected"] = selected.to_numpy(dtype=bool)
    tmp["vol_bin"] = pd.cut(x, bins=edges, include_lowest=True, duplicates="drop")
    diff_sum = 0.0
    count = 0
    strata = 0
    for _, sub in tmp.groupby(["date_utc", "side", "vol_bin"], observed=True):
        a = sub.loc[sub.selected]
        b = sub.loc[~sub.selected]
        if len(a) < 3 or len(b) < 3:
            continue
        pa = a.outcome.eq("TP_FIRST").mean()
        pb = b.outcome.eq("TP_FIRST").mean()
        count += len(a)
        diff_sum += len(a) * (pa - pb)
        strata += 1
    return (diff_sum / count if count else float("nan")), count, strata


def screen_rules(frame: pd.DataFrame, feature_names,
                 tp_pct: float = 1.0, sl_pct: float = .5,
                 cost_pct: float = .12, min_train: int = 60,
                 max_pair_features: int = 7, top: int = 30,
                 purge_minutes: int = 180):
    """Training-only search, untouched later-date scoring and day robustness.

    Never chooses a threshold or selects candidates using the later-date labels.
    The later period remains an *exploratory* holdout once viewed in the UI.
    """
    features = [name for name in feature_names if name in frame.columns]
    missing = REQUIRED - set(frame.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    pre_n = len(frame)
    # CRITICAL: freeze episode membership BEFORE observing any TP/SL outcome.
    # Otherwise excluding NO_HIT first can accidentally promote a later member
    # of the same wave, which is an outcome-dependent sampling decision.
    if purge_minutes:
        frame, purged = one_per_symbol_wave(frame, minutes=purge_minutes)
    else:
        purged = 0
    frame = frame.loc[frame.outcome.isin(RESOLVED)].copy()
    frame = _numeric(frame, features)
    train, held, train_days, held_days = chronological_split(frame)
    if not len(train) or not len(held):
        raise ValueError("Empty discovery or later-date cohort")
    minimum = min(int(min_train), max(20, int(len(train) * .15)))
    singles = _discover_single(train, features, minimum)
    unique = []
    for rule, _, _ in singles:
        if rule[0][0] not in unique:
            unique.append(rule[0][0])
        if len(unique) >= max_pair_features:
            break
    # Conjunctions are discovered exclusively in the earlier period.
    pair_pool = [rule for rule, _, _ in singles if rule[0][0] in unique]
    pairs = []
    seen_pairs = set()
    for a,b in combinations(pair_pool, 2):
        if a[0][0] == b[0][0]:
            continue
        rule = tuple(sorted(a + b))
        if rule in seen_pairs:
            continue
        seen_pairs.add(rule)
        chosen = train.loc[mask_for(train, rule)]
        if len(chosen) < minimum:
            continue
        tp = int(chosen.outcome.eq("TP_FIRST").sum())
        lower = _wilson_lower(tp, len(chosen))
        if tp / len(chosen) > train.outcome.eq("TP_FIRST").mean():
            pairs.append((rule, lower, len(chosen)))
    tests = (singles + pairs)
    tests.sort(key=lambda item: (item[1], item[2]), reverse=True)
    train_base = metrics(train, tp_pct, sl_pct, cost_pct)
    held_base = metrics(held, tp_pct, sl_pct, cost_pct)
    # Freeze volatility quantile boundaries from DISCOVERY only for matching.
    v = pd.to_numeric(train.get("pre_realized_vol60_pct"), errors="coerce")
    edges = None
    if v is not None:
        clean = v[np.isfinite(v)]
        cuts = sorted(set(float(q) for q in clean.quantile([0, .25, .5, .75, 1]).values)) if len(clean) else []
        if len(cuts) >= 3:
            cuts[0] = -np.inf
            cuts[-1] = np.inf
            edges = cuts
    rows = []
    for rule, lower, _ in tests[:max(100, int(top) * 4)]:
        mtrain = mask_for(train, rule)
        mheld = mask_for(held, rule)
        a = metrics(train.loc[mtrain],tp_pct,sl_pct,cost_pct)
        b = metrics(held.loc[mheld],tp_pct,sl_pct,cost_pct)
        byday = held.loc[mheld].groupby("date_utc").outcome.agg(
            n="size", tp=lambda s: int(s.eq("TP_FIRST").sum()))
        consistent = int((byday.tp / byday.n > held_base["tp_rate"]).sum()) if len(byday) else 0
        matched, matched_n, strata = _matched_lift(held,mheld,edges)
        rows.append({
            "regla":describe_rule(rule), "rule":rule,
            "etapas":len(rule), "train_n":a["resolved"],
            "train_tp_rate":a["tp_rate"], "train_proxy_pct":a["resolved_net_proxy_pct"],
            "train_wilson_lower":lower,
            "held_n":b["resolved"], "held_tp":b["tp"], "held_sl":b["sl"],
            "held_tp_rate":b["tp_rate"],"held_proxy_pct":b["resolved_net_proxy_pct"],
            "held_delta_base_pp":100*(b["tp_rate"]-held_base["tp_rate"]) if b["resolved"] else np.nan,
            "held_days_present":len(byday),"held_days_above_base":consistent,
            "held_matched_delta_pp":100*matched if math.isfinite(matched) else np.nan,
            "held_matched_n":matched_n,"held_matched_strata":strata,
            "held_coverage_pct":100*b["resolved"]/held_base["resolved"],
        })
    summary = {
        "start_events":pre_n, "excluded_nearby":purged, "resolved_after_purge":len(frame),
        "train_events":len(train),"held_events":len(held),
        "train_days":train_days,"held_days":held_days,
        "train_baseline":train_base,"held_baseline":held_base,
        "searched_single_rules":len(singles),"searched_pair_rules":len(pairs),
        "candidate_features":len(features),"minimum_train":minimum,
    }
    return pd.DataFrame(rows), summary, train, held


def evaluate_rule_days(heldout: pd.DataFrame, rule, tp_pct=1., sl_pct=.5, cost_pct=.12):
    selected = heldout.loc[mask_for(heldout,rule)]
    rows=[]
    for day, part in heldout.groupby("date_utc"):
        a=metrics(selected.loc[selected.date_utc.eq(day)],tp_pct,sl_pct,cost_pct)
        b=metrics(part,tp_pct,sl_pct,cost_pct)
        rows.append({"dia":str(day),"seleccionadas":a["resolved"],"TP":a["tp"],"SL":a["sl"],
                     "tasa_seleccionada":a["tp_rate"],"tasa_base":b["tp_rate"],
                     "proxy_seleccionada_pct":a["resolved_net_proxy_pct"]})
    return pd.DataFrame(rows)
