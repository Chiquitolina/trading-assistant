# Runner delayed entry audit — 2026-10-14 exploratory cohort

## Purpose
Check whether 5/10-minute delayed or simple causal confirmation entries change TP2/TP3 outcomes for the **same frozen 72 four-factor reaction signals**, comparing with the 1,221 room-available controls. **This is not a new holdout**: the ideas were motivated by these same observations. Do not deploy to live or optimize thresholds here.

## Definitions
- `ORIGINAL_OPEN`: enter at first 1m candle OPEN after retest; original 180m path.
- `WAIT_5_ALL`: ignore initial five complete 1m candles, enter at sixth OPEN, regardless of their movements.
- `WAIT_5_FAVORABLE_CLOSE`: same entry, but only if fifth candle's close is favorable vs original entry OPEN (strictly positive side-adjusted). No peeking at the sixth candle.
- `WAIT_10_ALL` and `WAIT_10_FAVORABLE_CLOSE`: similarly from the eleventh 1m OPEN.
- No stop exists while waiting because no position is open. Not a re-entry after an actual executed SL.
- Each entered trade receives a **new** TP and SL based on its actual delayed entry price, and a full 180-minute horizon **from the delayed entry** (complete 360-bar archive supports all windows).
- Strict original engine semantics: TP requires strictly > TP%; SL >= SL%; simultaneous OHLC = `AMBIGUOUS_SL_FIRST`, treated conservatively as stop, since order within a 1m bar is unknown.
- Commission assumption: 0.05% per side (0.10% round trip). Ideal barrier executions, no spread/slippage/funding/latency. Individual trades independent: **not one-slot portfolio**.
- Confirmation uses close of the previous fully closed 1m bar and executes at the very next bar OPEN. Execution at that boundary is an optimistic latency assumption.

## How to run
Upload ZIP to `/root/runner_delayed_entry_audit_20261014.zip` on VPS and:

```bash
mkdir -p /root/runner_delayed_entry_audit_20261014
unzip -o /root/runner_delayed_entry_audit_20261014.zip -d /root/runner_delayed_entry_audit_20261014
cd /root/runner_delayed_entry_audit_20261014
python3 -m unittest -v test_runner_delayed_entry_audit.py
python3 run_runner_delayed_entry_audit.py --project /home/multiservices-1m --out /root/runner_delayed_entry_results_20261014
```

Expected parity: `1311 of 1311` paths, 72 selected, and baseline `TP2_SL1`: 18 TP, 49 SL, 5 NO_HIT. Script stops on missing/corrupt/non-contiguous paths or parity failures. DB loaded SQLite read-only and never modified.

Outputs: `entry_mode_matrix_summary.csv`, `matched_vs_immediate.csv`, `per_event_entry_modes.csv`, `issues.csv`. For shipping results:

```bash
cd /root/runner_delayed_entry_results_20261014
zip -j /root/runner_delayed_entry_results_20261014.zip entry_mode_matrix_summary.csv matched_vs_immediate.csv per_event_entry_modes.csv issues.csv
```

`matched_vs_immediate.csv` compares each delayed scenario against the ORIGINAL_OPEN **on the same Event IDs that entered under that delayed mode**. Do not mistake an improvement from dropping entries for an improvement solely due to changing execution time.

**Important**: One market regime over ~39h, overlapping cross-symbol signals, data-derived hypotheses. Must re-test fixed rules prospectively over days/weeks before judging predictive edge.
