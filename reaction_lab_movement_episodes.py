#!/usr/bin/env python3
"""Movement Episodes — conservative, point-in-time REACTION grouping (research only).

Each decision is made from the candidate's available time and previously known
REACTIONs/1m candles whose close time <= that time. No future outcomes are used
in assignment. Snapshot may be rebuilt with new/backfilled events, so episode
IDs aren't a persistent LIVE ledger; don't use as execution state.

Sources: standalone scanner + auto_paths SQLite opened read-only. No PM2,
exchange, REST, Redis, collectors or dependencies beyond Python stdlib and
existing reaction_lab_auto_paths.py.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing

MINUTE_MS = 60_000
SCHEMA_VERSION = "movement-episodes-v1.0"

@dataclass(frozen=True)
class Policy:
    max_gap_min: int = 90              # Last REACTION to candidate observation
    max_age_min: int = 360             # First REACTION to candidate observation
    anchor_invalidation_pct: float = 2.0
    peak_giveback_pct: float = 2.5
    min_peak_for_giveback_pct: float = 1.0

    def __post_init__(self):
        if not (0 < self.max_gap_min <= self.max_age_min <= 360):
            raise ValueError("Require 0 < max_gap_min <= max_age_min <= 360")
        if min(self.anchor_invalidation_pct, self.peak_giveback_pct,
               self.min_peak_for_giveback_pct) <= 0:
            raise ValueError("Movement percentages must be positive")


@dataclass
class Reaction:
    event_key: str
    symbol: str
    side: str
    retest_ts: int
    known_ts: int | None
    first_seen_ms: int | None
    observation_type: str
    retest_close: float | None
    path_state: str
    path_count: int
    entry_ts: int | None
    path_blob: bytes | None
    ready_ms: int | None = None
    ready_quality: str = "UNKNOWN"
    path_quality: str = "NOT_CHECKED"
    bars: list | None = None


@dataclass
class Episode:
    episode_id: str
    first_event_key: str
    symbol: str
    side: str
    origin: str
    start_ready_ms: int
    last_ready_ms: int
    start_retest_ts: int
    anchor: float | None
    bars: list | None
    seed_complete: bool
    peak_favorable_pct: float = 0.0
    bar_cursor: int = 0
    invalidated: bool = False
    members: list = field(default_factory=list)
    complete_count: int = 0
    origins: Counter = field(default_factory=Counter)


def readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def ready_at(known, first_seen, origin, retest):
    """Historical recovered signals are NEVER labeled observed LIVE."""
    if known is None:
        return None, "UNKNOWN_KNOWN_TS"
    if origin == "LIVE_OBSERVED":
        if first_seen is None:
            return None, "UNKNOWN_FIRST_SEEN"
        return max(int(known), int(first_seen)), "LIVE_OBSERVED"
    if origin == "RECOVERED_WINDOW":
        return int(known), "RECOVERED_ASOF_ONLY"
    return None, "UNKNOWN_ORIGIN"


def _sort_sql():
    return """SELECT s.event_key, s.symbol, s.side, s.retest_ts, s.known_ts,
       s.first_seen_ms, s.observation_type, s.causal_json,
       p.entry_ts,p.state,p.count,p.path_blob
       FROM main.events s LEFT JOIN ap.events p ON p.event_key=s.event_key
       ORDER BY CASE
         WHEN s.known_ts IS NULL THEN 9223372036854775807
         WHEN s.observation_type='LIVE_OBSERVED' THEN
             CASE WHEN s.first_seen_ms IS NULL THEN 9223372036854775807
                  WHEN s.first_seen_ms > s.known_ts THEN s.first_seen_ms
                  ELSE s.known_ts END
         WHEN s.observation_type='RECOVERED_WINDOW' THEN s.known_ts
         ELSE 9223372036854775807 END,
         s.retest_ts, s.event_key"""


def iter_reactions(project: Path):
    from reaction_lab_auto_paths import decode_path
    from reaction_lab_runner_discovery_v2 import validate_path

    base = Path(project) / "reports" / "reaction_lab"
    scanner, paths = base / "scanner_events_v1.sqlite3", base / "auto_paths_v1.sqlite3"
    for p in (scanner, paths):
        if not p.is_file():
            raise FileNotFoundError(p)
    with closing(sqlite3.connect(readonly_uri(scanner), uri=True, timeout=15)) as con:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("ATTACH DATABASE ? AS ap", (readonly_uri(paths),))
        con.execute("PRAGMA query_only=ON")
        for item in con.execute(_sort_sql()):
            (key, symbol, side, retest, known, first_seen, origin, causal,
             entry, state, count, blob) = item
            try:
                ref = float(json.loads(causal or "{}").get("retest_close"))
                if not math.isfinite(ref) or ref <= 0:
                    ref = None
            except (ValueError, TypeError, json.JSONDecodeError):
                ref = None
            reaction = Reaction(
                key, str(symbol), str(side), int(retest),
                int(known) if known is not None else None,
                int(first_seen) if first_seen is not None else None,
                str(origin), ref, str(state or "MISSING"), int(count or 0),
                int(entry) if entry is not None else None, blob,
            )
            reaction.ready_ms, reaction.ready_quality = ready_at(
                reaction.known_ts, reaction.first_seen_ms, reaction.observation_type, reaction.retest_ts)
            if state != "COMPLETE" or not blob:
                reaction.path_quality = "NO_COMPLETE_PATH"
            elif ref is None:
                reaction.path_quality = "MISSING_REFERENCE"
            else:
                try:
                    bars = decode_path(blob)
                    quality = validate_path(
                        bars, retest_ts=reaction.retest_ts,
                        entry_ts=entry, count=count)
                    reaction.path_quality = quality
                    if quality == "COMPLETE_VALID":
                        reaction.bars = bars
                except (ValueError, TypeError, OverflowError, IndexError) as exc:
                    reaction.path_quality = "BAD_PATH_" + type(exc).__name__
                except Exception as exc:
                    # Corrupt source rows should not abort the research run.
                    reaction.path_quality = "PATH_ERROR_" + type(exc).__name__
            reaction.path_blob = None  # Do not keep compressed duplicates in RAM.
            yield reaction


def _favorable(side, close, anchor):
    return 100 * ((close / anchor - 1) if side == "LONG" else (1 - close / anchor))


def advance_episode(ep: Episode, until_ms: int, policy: Policy):
    """Move ONLY over fully closed bars before/on candidate availability.

    Returns False when earliest episode 1m source cannot prove continuity.
    Closing a bar after until_ms would be lookahead and is forbidden.
    """
    if ep.bars is None or ep.anchor is None:
        return False, "NO_SEED_PATH"
    bars = ep.bars
    while ep.bar_cursor < len(bars):
        bar = bars[ep.bar_cursor]
        bar_open_ms = int(bar[0])
        if bar_open_ms + MINUTE_MS > int(until_ms):
            break
        close = float(bar[4])
        favorable = _favorable(ep.side, close, ep.anchor)
        ep.peak_favorable_pct = max(ep.peak_favorable_pct, favorable)
        ep.bar_cursor += 1
        if favorable <= -policy.anchor_invalidation_pct:
            ep.invalidated = True
            return True, "ANCHOR_INVALIDATED"
        if (ep.peak_favorable_pct >= policy.min_peak_for_giveback_pct
                and ep.peak_favorable_pct - favorable >= policy.peak_giveback_pct):
            ep.invalidated = True
            return True, "PEAK_GIVEBACK"
    # Need coverage through the last bar that should be closed by until_ms.
    expected_open = (int(until_ms) // MINUTE_MS - 1) * MINUTE_MS
    if expected_open >= int(bars[0][0]):
        if ep.bar_cursor == 0 or bars[ep.bar_cursor - 1][0] < expected_open:
            return False, "NO_CLOSED_BARS_TO_CANDIDATE"
    elif int(until_ms) - ep.start_ready_ms >= MINUTE_MS:
        return False, "NO_CLOSED_BARS_TO_CANDIDATE"
    return True, "CONTINUATION"


def episode_id(first_event_key: str) -> str:
    return "EP-" + hashlib.sha256(first_event_key.encode("utf-8")).hexdigest()[:14]


def group_reactions(reactions, policy=Policy()):
    """Returns (event_rows, episode_rows).

    Candidate order must be chronological by ready_ms (iter_reactions does so).
    An opposite-side REACTION observed before current event closes the prior
    opposite-side active episode, conservatively. Only same-symbol members can
    merge; market-wide synchronicity is a separate study.
    """
    active: dict[str, Episode] = {}
    all_episodes: list[Episode] = []
    rows: list[dict] = []
    last_ready = -1
    for r in reactions:
        # Do not use retest time as fallback for unknown signal availability.
        if r.ready_ms is None:
            reason = r.ready_quality
            ep = None
        else:
            if r.ready_ms < last_ready:
                raise ValueError("Input must be sorted by observation time")
            last_ready = r.ready_ms
            previous = active.get(r.symbol)
            reason = "FIRST_REACTION"
            ep = None
            if previous is not None:
                if previous.side != r.side:
                    reason = "OPPOSITE_REACTION"
                elif previous.origin != r.observation_type:
                    reason = "ORIGIN_TRANSITION"
                elif r.ready_ms - previous.last_ready_ms > policy.max_gap_min * MINUTE_MS:
                    reason = "GAP_BETWEEN_REACTIONS"
                elif r.ready_ms - previous.start_ready_ms > policy.max_age_min * MINUTE_MS:
                    reason = "EPISODE_TOO_OLD"
                elif previous.bars is None:
                    reason = "NO_SEED_PATH"
                else:
                    covered, status = advance_episode(previous, r.ready_ms, policy)
                    if not covered or previous.invalidated:
                        reason = status
                    else:
                        ep = previous
                        reason = "JOIN_CAUSAL_CONTINUATION"
        if ep is None:
            # Release historical OHLC lists for a symbol whose active episode ends.
            # Keep only the small episode metadata for reporting.
            old = active.get(r.symbol)
            if old is not None:
                old.bars = None
            ep = Episode(
                episode_id(r.event_key), r.event_key, r.symbol, r.side, r.observation_type,
                r.ready_ms if r.ready_ms is not None else r.retest_ts,
                r.ready_ms if r.ready_ms is not None else r.retest_ts,
                r.retest_ts, r.retest_close,
                r.bars if r.path_quality == "COMPLETE_VALID" else None,
                r.path_quality == "COMPLETE_VALID",
            )
            all_episodes.append(ep)
        if r.ready_ms is not None:
            ep.last_ready_ms = r.ready_ms
            active[r.symbol] = ep
        ep.members.append(r.event_key)
        ep.complete_count += int(r.path_quality == "COMPLETE_VALID")
        ep.origins[r.observation_type] += 1
        rows.append({
            "event_key":r.event_key,"symbol":r.symbol,"side":r.side,
            "retest_ts":r.retest_ts,"ready_ms":r.ready_ms,
            "ready_quality":r.ready_quality,"observation_type":r.observation_type,
            "path_quality":r.path_quality,
            "episode_id":ep.episode_id,"episode_seed":ep.first_event_key,
            "position_in_episode":len(ep.members),
            "decision":reason,
        })
    episode_rows = []
    for ep in all_episodes:
        episode_rows.append({
            "episode_id":ep.episode_id,"symbol":ep.symbol,"side":ep.side,
            "origin_basis":ep.origin,
            "first_event_key":ep.first_event_key,
            "start_ready_ms":ep.start_ready_ms,"last_ready_ms":ep.last_ready_ms,
            "start_retest_ts":ep.start_retest_ts,
            "reactions":len(ep.members),"complete_paths":ep.complete_count,
            "live_events":ep.origins["LIVE_OBSERVED"],
            "recovered_events":ep.origins["RECOVERED_WINDOW"],
            "verification": "VERIFIED_SEED_PATH" if ep.seed_complete else "SEED_PATH_NOT_COMPLETE",
        })
    # Global order is chronological; output is the same order as decisions.
    return rows, episode_rows


def _atomic_csv(path, rows, fieldnames):
    fd, tmp = tempfile.mkstemp(prefix=".episode_", suffix=".csv", dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=fieldnames)
            writer.writeheader(); writer.writerows(rows)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def build_episodes(project, policy=Policy()):
    """Explicit, manual snapshot. No upstream DB mutation and no live process."""
    project=Path(project).resolve()
    out=project/"reports"/"reaction_lab"/"movement_episodes_v1"
    out.mkdir(parents=True,exist_ok=True)
    event_rows,episode_rows=group_reactions(iter_reactions(project),policy)
    _atomic_csv(out/"events.csv",event_rows,[
        "event_key","symbol","side","retest_ts","ready_ms","ready_quality",
        "observation_type","path_quality","episode_id","episode_seed",
        "position_in_episode","decision",
    ])
    _atomic_csv(out/"episodes.csv",episode_rows,[
        "episode_id","symbol","side","origin_basis","first_event_key","start_ready_ms",
        "last_ready_ms","start_retest_ts","reactions","complete_paths",
        "live_events","recovered_events","verification",
    ])
    counts=Counter(r["decision"] for r in event_rows)
    manifest={
        "schema":SCHEMA_VERSION,
        "created_utc":datetime.now(timezone.utc).isoformat(),
        "policy": vars(policy),
        "counts":{
            "reactions":len(event_rows),"episodes":len(episode_rows),
            "multi_reaction_episodes":sum(r["reactions"]>1 for r in episode_rows),
            "reactions_in_multi_episodes":sum(r["reactions"] for r in episode_rows if r["reactions"]>1),
            "unverified_seeds":sum(r["verification"]!="VERIFIED_SEED_PATH" for r in episode_rows),
            "reasons":dict(counts),
        },
        "notes":[
            "Grouping uses only earlier known REACTIONs and prior fully closed 1m bars.",
            "No matching by future peak_run_pct, TP/SL, or final episode outcome.",
            "Missing/incomplete anchor paths produce singleton episodes, not guessed joins.",
            "Recovered-window historical as-of is not equivalent to LIVE observation; origins never merge.",
            "Episodes are conservative same-symbol directional groupings, not statistically independent market waves.",
            "Backfills can change historical episode assignment on later snapshot regeneration.",
            "Never use episode size or final membership to predict the first REACTION.",
        ],
    }
    fd,tmp=tempfile.mkstemp(prefix=".episode_manifest_",suffix=".json",dir=out)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(manifest,f,ensure_ascii=False,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,out/"manifest.json")
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",default=".")
    parser.add_argument("--max-gap-min",type=int,default=90)
    parser.add_argument("--max-age-min",type=int,default=360)
    args=parser.parse_args(argv)
    report=build_episodes(args.project,Policy(max_gap_min=args.max_gap_min,max_age_min=args.max_age_min))
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":
    main()
