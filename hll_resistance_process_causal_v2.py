#!/usr/bin/env python3
"""HLL | Resistance processes V2 — physical deduplication and causal checkpoints.

READ-ONLY input: frozen V1 cohort reports, original 30m candles and formation ATR.
Output: new self-contained audit reports. NEVER changes Flip/Continuation/Reclaim,
PRE fingerprints, rankings, entry events, or LIVE. No historical outcome is an
entry signal. Execution scenarios are theoretical 30m OHLC next-open paths.

Two distinct clocks:
  1. Physical process is a retrospective grouping of unique physical contact bars
     by exact (symbol, level_id, zone) across overlapping attempts/episodes.
  2. A checkpoint has immutable knowledge time (last CLOSED 30m candle) and
     exclusively features observable on/before that close. Next OPEN is at
     precisely that time; outcomes always start there, never at breakout OPEN.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from hll_detector_miss_audit_v1 import CANDLES, EPISODES, PRE, V29, load_bars, millis, read_rows, sha256
from hll_resistance_process_cohort_v1 import LEVELS, OUTPUT as V1_OUTPUT, load_level_atr, formation_atr

TF = 1_800_000
VERSION = 'HLL_RESISTANCE_PROCESSES_CAUSAL_V2'
DEFAULT_V1 = V1_OUTPUT / 'pilot15_seed20261103_v1'
DEFAULT_OUTPUT = Path('reports/horizontal_level_resistance_process_cohort_v2/pilot15_seed20261103_v2')
SCENARIOS = (
    ('TP2_SL1_24', 24, 2.0, 1.0),
    ('TP3_SL1_48', 48, 3.0, 1.0),
    ('TP3_SL1p5_96', 96, 3.0, 1.5),
    ('TP5_SL2_96', 96, 5.0, 2.0),
)
V1_FILES = ('attempt_processes.csv', 'contact_processes.csv', 'episode_rollup.csv', 'summary.json')
FEE_ROUNDTRIP_PCT = 0.10
MAX_HORIZON = 96


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def as_bool(value):
    if isinstance(value, bool):
        return value
    if str(value) in ('True', 'true', '1'):
        return True
    if str(value) in ('False', 'false', '0', ''):
        return False
    raise ValueError(f'Invalid boolean: {value!r}')


def positive(value, field):
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Invalid numeric {field}: {value!r}') from exc
    if not math.isfinite(n) or n <= 0:
        raise ValueError(f'Invalid numeric {field}: {value!r}')
    return n


def stable_id(*values):
    return hashlib.sha256('|'.join(map(str, values)).encode('utf-8')).hexdigest()[:24]


def validate_v1(root, v1_dir, *, check_hash):
    v1 = v1_dir if v1_dir.is_absolute() else root / v1_dir
    missing = [str(v1 / name) for name in (*V1_FILES, 'manifest.json') if not (v1/name).is_file()]
    if missing:
        raise FileNotFoundError('Missing V1 artifacts: ' + ', '.join(missing))
    manifest = read_json(v1/'manifest.json')
    if manifest.get('version') != 'HLL_RESISTANCE_PROCESS_COHORT_V1':
        raise ValueError('Unexpected V1 manifest version')
    summary = read_json(v1/'summary.json')
    if summary.get('attempt_status_counts', {}).get('ERROR', 0):
        raise ValueError('V1 has unresolved audit errors')
    if check_hash:
        for name in V1_FILES:
            expected = manifest.get('files_sha256', {}).get(name)
            if not expected or sha256(v1/name) != expected:
                raise ValueError('V1 artifact hash mismatch: ' + name)
        for relative, field in ((PRE, 'pre_sha256'), (V29, 'v29_sha256'), (EPISODES, 'episodes_sha256')):
            if not (root/relative).is_file() or sha256(root/relative) != manifest.get(field):
                raise ValueError('Canonical source changed: ' + str(relative))
        import inspect
        from hll_resistance_universal_audit_v1 import price_behavior
        if sha256(Path(inspect.getfile(price_behavior))) != manifest.get('engine_sha256'):
            raise ValueError('Universal audit engine changed since V1')
    return v1, summary, manifest


def load_attempts(v1):
    rows = list(read_rows(v1/'attempt_processes.csv'))
    if not rows:
        raise ValueError('Empty V1 attempt cohort')
    by_id = {}
    levels = defaultdict(list)
    for r in rows:
        aid, sym, level, episode = (r.get(k, '') for k in ('attempt_id','symbol','level_id','episode_id'))
        if not all((aid, sym, level, episode)) or aid in by_id:
            raise ValueError('Missing/duplicate attempt identity')
        ts = millis(r['pre_closed_ts'])
        if ts % TF or millis(r['breakout_closed_ts']) != ts + TF:
            raise ValueError('Invalid attempt clocks: ' + aid)
        obs = int(r['observed_post_bars'])
        if obs < 0 or obs > MAX_HORIZON or (obs == 0 and r['audit_state'] == 'OK'):
            raise ValueError('Invalid observed window')
        if r['audit_state'] not in ('OK','CENSORED'):
            raise ValueError('Unusable V1 attempt: ' + aid)
        low, high = positive(r['zone_low'], 'zone_low'), positive(r['zone_high'], 'zone_high')
        if high < low:
            raise ValueError('Inverted resistance zone')
        r['_pre'] = ts
        r['_end'] = ts + obs * TF
        r['_zone'] = (low, high)
        by_id[aid] = r
        levels[(sym, level)].append(r)
    for (sym, level), members in levels.items():
        anchor = members[0]['_zone']
        if any(m['_zone'] != anchor for m in members):
            raise ValueError(f'Conflicting resistance zones for {sym}/{level}')
    return by_id, levels


def validate_v1_identity(root, attempts):
    """Cross-check V1 summary rows with exact frozen source identity/statuses."""
    found_pre=set(); found_episode=set(); seen_routes=set()
    for row in read_rows(root/PRE):
        aid=row.get('attempt_id')
        if aid not in attempts:continue
        a=attempts[aid]
        if aid in found_pre or row['symbol']!=a['symbol'] or row['level_id']!=a['level_id']:
            raise ValueError('PRE identity mismatch: '+aid)
        if (millis(row['pre_closed_ts']) != a['_pre'] or
            millis(row['breakout_closed_ts']) != a['_pre']+TF or
            (float(row['zone_low']),float(row['zone_high'])) != a['_zone'] or
            row['v26_status']!=a['v26_status']):
            raise ValueError('Frozen PRE content mismatch: '+aid)
        found_pre.add(aid)
    for row in read_rows(root/EPISODES):
        aid=row.get('attempt_id')
        if aid not in attempts:continue
        a=attempts[aid]
        if aid in found_episode or row['symbol']!=a['symbol'] or row['level_id']!=a['level_id'] or row['episode_id']!=a['episode_id']:
            raise ValueError('Episode map identity mismatch: '+aid)
        if (millis(row['pre_closed_ts'])!=a['_pre'] or
            millis(row['breakout_closed_ts'])!=a['_pre']+TF):
            raise ValueError('Episode map clock mismatch: '+aid)
        found_episode.add(aid)
    branches={'BREAKOUT_CONTINUATION':'continuation_status',
              'RECLAIM_CONTINUATION':'reclaim_status'}
    for row in read_rows(root/V29):
        aid=row.get('attempt_id')
        if aid not in attempts:continue
        branch=row['branch'];a=attempts[aid]
        if branch not in branches or (aid,branch) in seen_routes:
            raise ValueError('Duplicate/unknown V2.9 route: '+aid)
        if row['origin_symbol'] != a['symbol'] or row['status']!=a[branches[branch]]:
            raise ValueError('Frozen V2.9 status mismatch: '+aid)
        seen_routes.add((aid,branch))
    if found_pre!=set(attempts) or found_episode!=set(attempts) or len(seen_routes)!=2*len(attempts):
        raise ValueError('V1 identity incomplete in frozen original sources')


def validate_v1_processes(v1, attempts):
    n = 0
    for r in read_rows(v1/'contact_processes.csv'):
        n += 1
        aid = r['attempt_id']
        if aid not in attempts:
            raise ValueError('Unknown attempt in V1 contact process')
        a = attempts[aid]
        if r['symbol'] != a['symbol'] or r['episode_id'] != a['episode_id']:
            raise ValueError('V1 contact identity mismatch')
        left, right = int(r['start_offset']), int(r['last_offset'])
        if not 0 <= left <= right < int(a['observed_post_bars']):
            raise ValueError('V1 process exceeds observed time horizon')
        if millis(r['first_known_closed_ts']) != a['_pre'] + (left+1)*TF:
            raise ValueError('V1 process start clock mismatch')
        if millis(r['last_known_closed_ts']) != a['_pre'] + (right+1)*TF:
            raise ValueError('V1 process end clock mismatch')
    return n


def split_symbol(symbol, seed=20261103):
    digest = hashlib.sha256(f'{seed}|{symbol}'.encode()).hexdigest()
    return 'HOLDOUT_SYMBOL' if int(digest[:8],16) % 4 == 0 else 'DISCOVERY_SYMBOL'


def build_windows(attempts, candle_times):
    """Observed V1 windows only. A gap in a previously verified prefix is corruption."""
    seen = set()
    for a in attempts:
        for ts in range(a['_pre'], a['_end'], TF):
            if ts not in candle_times:
                raise ValueError(f'Missing original candle INSIDE a verified V1 prefix: {a["attempt_id"]} {ts}')
            seen.add(ts)
    return seen


def build_physical_groups(root, levels, *, gap=2):
    """One bar counted once per symbol+level, even across different episodes.

    A group stops at any missing/unobserved candle or >gap empty contact bars.
    Every group is retrospective; causal timestamps are computed separately.
    """
    if not isinstance(gap, int) or gap < 0 or gap > 12:
        raise ValueError('Bad grouping gap')
    by_symbol = defaultdict(list)
    for (symbol, level), members in levels.items():
        by_symbol[symbol].append((level, members))
    physical = []
    links = []
    level_prices, source_state = load_level_atr(root, set(by_symbol))
    for i, (symbol, groups) in enumerate(sorted(by_symbol.items()), 1):
        candles = load_bars(root/CANDLES/f'{symbol}_30m.csv')
        for level, members in groups:
            members = sorted(members, key=lambda x: (x['_pre'], x['attempt_id']))
            lo, hi = members[0]['_zone']
            atr, atr_status = formation_atr(level_prices, members[0])
            # An unavailable formation ATR never becomes a fake zero ATR/defense.
            eligible = build_windows(members, candles)
            # V1 did not hash individual OHLC files: reconcile contacts per attempt.
            for a in members:
                raw_count=sum(
                    candles[t][2] <= hi and candles[t][1] >= lo
                    for t in range(a['_pre'], a['_end'], TF))
                if raw_count != int(a['raw_physical_contacts']):
                    raise ValueError(
                      f'V1/OHLC physical-contact mismatch: {a["attempt_id"]} '
                      f'{raw_count} != {a["raw_physical_contacts"]}')
            contacts = sorted(t for t in eligible if candles[t][2] <= hi and candles[t][1] >= lo)
            chunks = []
            for t in contacts:
                if not chunks or (t - chunks[-1][-1])//TF - 1 > gap or any(
                     q not in eligible for q in range(chunks[-1][-1]+TF, t, TF)):
                    chunks.append([t])
                else:
                    chunks[-1].append(t)
            for pos, touch_times in enumerate(chunks):
                first, last = touch_times[0], touch_times[-1]
                # Do not attribute a later physical touch to an attempt outside
                # its actually observed 96-bar window.
                related = [a for a in members if a['_pre'] <= last and a['_end'] > first]
                if not related:
                    raise ValueError('Canonical contact group has no observed attempt')
                episodes = sorted({a['episode_id'] for a in related})
                pid = stable_id('PHYSICAL_V2', symbol, level, lo, hi, first, gap)
                after = chunks[pos+1][0] if pos+1 < len(chunks) else None
                # +4 bars for follow-up confirmation, never crossing next group.
                max_open = min(last+4*TF, (after-TF) if after else last+4*TF)
                trace_ts = []
                for ts in range(first, max_open+TF, TF):
                    if ts not in eligible or ts not in candles:
                        break
                    trace_ts.append(ts)
                record = dict(process_id=pid, symbol=symbol,level_id=level,
                    zone_low=lo,zone_high=hi,formation_atr=atr,
                    formation_atr_status=atr_status,first_contact_open_ts=first,
                    first_contact_known_closed_ts=first+TF,
                    last_contact_known_closed_ts=last+TF,
                    physical_touch_bars=len(touch_times),
                    grouped_contact_span_bars=(last-first)//TF+1,
                    related_attempt_count=len(related),related_episode_count=len(episodes),
                    related_attempt_ids='|'.join(a['attempt_id'] for a in related),
                    related_episode_ids='|'.join(episodes),
                    process_grouping='RETROSPECTIVE_PHYSICAL',
                    split=split_symbol(symbol),
                    _members=related,_candles=candles,_trace_ts=trace_ts,
                    _contacts=set(touch_times),_eligible=eligible)
                physical.append(record)
                for a in related:
                    links.append(dict(process_id=pid,attempt_id=a['attempt_id'],
                        symbol=symbol,level_id=level,episode_id=a['episode_id'],
                        attempt_pre_closed_ts=a['_pre'],attempt_observed_until=a['_end'],
                        attempt_v26_status=a['v26_status'],
                        attempt_continuation_status=a['continuation_status'],
                        attempt_reclaim_status=a['reclaim_status'],
                        link_basis='OVERLAPPING_OBSERVED_TIME_RETROSPECTIVE'))
        print('HLL_PROCESS_V2_SYMBOL',json.dumps(dict(i=i,total=len(by_symbol),symbol=symbol,
                    physical_processes=sum(p['symbol']==symbol for p in physical))),flush=True)
    if len({p['process_id'] for p in physical}) != len(physical):
        raise ValueError('Canonical physical process ID collision')
    return physical, links, source_state


def contact_bucket(n):
    return '1' if n <= 1 else ('2-3' if n <= 3 else '4+')


def zone_bucket(process):
    atr = process['formation_atr']
    if atr is None:
        return 'ATR_UNAVAILABLE'
    x = (process['zone_high']-process['zone_low']) / atr
    return 'NARROW_0p5ATR' if x <= .5 else ('MID_1ATR' if x <= 1.0 else 'WIDE_GT1ATR')


def causal_trace(process):
    """Emit FIRST closed-candle checkpoints; all features are prefix-only.

    No frozen mechanism outcomes or future physical process sizes enter the
    checkpoint signature. The grouping can be audited after the fact, but an
    event itself is knowable only at event.known_closed_ts.
    """
    times = process['_trace_ts']
    candles = process['_candles']
    lo, hi, atr = process['zone_low'], process['zone_high'], process['formation_atr']
    first_ts = process['first_contact_open_ts']
    members = process['_members']
    checkpoint_types = set()
    checkpoints = []
    evol = []
    touches = 0
    losses = 0
    recoveries = 0
    pending = []
    previous_loss = False
    for ts in times:
        o,h,l,c = candles[ts]
        prev = candles.get(ts-TF)
        prev_close = prev[3] if prev else None
        physical_touch = l <= hi and h >= lo
        from_above = bool(prev_close is not None and prev_close > hi and physical_touch)
        is_above = c > hi
        loss = bool(prev_close is not None and prev_close >= lo and c < lo)
        recovery = bool(prev_close is not None and prev_close <= hi and c > hi)
        two_above = bool(prev_close is not None and prev_close > hi and is_above)
        # Prefix counters are updated at the close of this candle.
        touches += int(physical_touch)
        losses += int(loss)
        recoveries += int(recovery)
        if from_above and c >= lo and atr is not None:
            pending.append(ts)
        defended = False
        updated_pending=[]
        for start in pending:
            if start == ts:
                updated_pending.append(start)
                continue
            if ts-start > 4*TF or c < lo:
                continue
            if c > hi + .5*atr:
                defended = True
            else:
                updated_pending.append(start)
        pending = [] if defended else updated_pending
        known = ts+TF
        is_intermit_known = any(
            as_bool(a.get('early4_intermit')) and a.get('early4_state')=='KNOWN'
            and a.get('known_closed_ts') and millis(a['known_closed_ts']) <= known
            and a['_pre'] <= ts for a in members)
        event_types=[]
        if physical_touch: event_types.append('FIRST_CONTACT')
        if from_above: event_types.append('CONTACT_FROM_ABOVE')
        if two_above: event_types.append('TWO_ABOVE_AFTER_CONTACT')
        if loss: event_types.append('ZONE_LOSS')
        if recovery:
            event_types.append('ZONE_RECOVERY_CLOSE')
            if previous_loss: event_types.append('RECOVERY_AFTER_LOSS')
        if defended: event_types.append('DESCRIPTIVE_DEFENSE')
        if is_intermit_known and any(
            as_bool(a.get('early4_intermit')) and a.get('early4_state')=='KNOWN'
            and a.get('known_closed_ts') and millis(a['known_closed_ts'])==known
            and a['_pre']<=ts for a in members):
            event_types.append('INTERMITTENT4_KNOWN')
        # No future labels in snapshot or matching signature.
        relation = 'ABOVE' if is_above else ('BELOW' if c < lo else 'IN_ZONE')
        snapshot=dict(known_closed_ts=known,observed_offset=(ts-first_ts)//TF,
            current_relation=relation,contact_bars_known=touches,
            loss_crosses_known=losses,recovery_crosses_known=recoveries,
            loss_observed_to_date=losses > 0,
            intermittent4_known=is_intermit_known,
            zone_width_bucket=zone_bucket(process))
        evol.append(dict(process_id=process['process_id'],symbol=process['symbol'],
            level_id=process['level_id'],**snapshot,
            physical_touch_now=physical_touch,touch_from_above_now=from_above,
            two_above_now=two_above,loss_now=loss,recovery_now=recovery,
            defense_now=defended,stage_is_descriptive=True))
        for kind in event_types:
            if kind in checkpoint_types:
                continue
            checkpoint_types.add(kind)
            cid=stable_id('CHECKPOINT_V2',process['process_id'],kind,known)
            signature='|'.join((kind,relation,contact_bucket(touches),
                'LOSS_Y' if losses else 'LOSS_N',
                'INTERMIT_Y' if is_intermit_known else 'INTERMIT_N',
                zone_bucket(process)))
            checkpoints.append(dict(checkpoint_id=cid,process_id=process['process_id'],
                symbol=process['symbol'],level_id=process['level_id'],
                event_type=kind,
                hypothetical_next_open_ts=known,**snapshot,
                signature=signature,split=process['split'],
                reference_policy='NEXT_30M_OPEN_AFTER_CLOSED_EVENT',
                retrospective_process_link=True,no_entry_signal=True))
        if loss:
            previous_loss=True
    return checkpoints,evol


def scenario_path(candles, entry_ts, *, horizon, tp_pct, sl_pct, fee_pct=FEE_ROUNDTRIP_PCT):
    """Theoretical next-open long path. Intra-candle TP+SL => unknown ordering.

    If the 30m OPEN gaps past TP/SL, that OPEN (not the threshold) is used.
    No liquidity, funding, spread or slippage model. Missing history censored.
    """
    if horizon <= 0 or tp_pct <= 0 or not 0 < sl_pct < 100 or fee_pct < 0:
        raise ValueError('Bad scenario')
    ref = candles.get(entry_ts)
    if ref is None:
        return dict(state='NO_REFERENCE_OPEN',reference_open='',exit_price='',
             exit_ts='',observed_bars=0,mfe_pct='',mae_pct='',gross_pct='',net_pct='')
    entry = ref[0]
    if not entry > 0:
        raise ValueError('Invalid reference OPEN')
    take, stop = entry*(1+tp_pct/100), entry*(1-sl_pct/100)
    max_high, min_low = entry, entry
    for j in range(horizon):
        ts=entry_ts+j*TF
        bar=candles.get(ts)
        if bar is None:
            return dict(state='CENSORED_PATH',reference_open=entry,exit_price='',
                exit_ts='',observed_bars=j,
                mfe_pct=round(100*(max_high/entry-1),6),
                mae_pct=round(100*(min_low/entry-1),6),gross_pct='',net_pct='')
        o,h,l,c=bar
        max_high=max(max_high,h); min_low=min(min_low,l)
        if o >= take:
            state,exit_price='TP_GAP_OPEN',o
        elif o <= stop:
            state,exit_price='SL_GAP_OPEN',o
        elif h >= take and l <= stop:
            state,exit_price='AMBIGUOUS_SAME_BAR',None
        elif h >= take:
            state,exit_price='TP_FIRST',take
        elif l <= stop:
            state,exit_price='SL_FIRST',stop
        else:
            continue
        gross=round(100*(exit_price/entry-1),6) if exit_price is not None else ''
        return dict(state=state,reference_open=entry,
            exit_price=exit_price if exit_price is not None else '',exit_ts=ts,
            observed_bars=j+1,mfe_pct=round(100*(max_high/entry-1),6),
            mae_pct=round(100*(min_low/entry-1),6),gross_pct=gross,
            net_pct=round(gross-fee_pct,6) if gross!='' else '')
    last=candles[entry_ts+(horizon-1)*TF][3]
    gross=round(100*(last/entry-1),6)
    return dict(state='TIMEOUT',reference_open=entry,exit_price=last,
        exit_ts=entry_ts+(horizon-1)*TF,
        observed_bars=horizon,mfe_pct=round(100*(max_high/entry-1),6),
        mae_pct=round(100*(min_low/entry-1),6),gross_pct=gross,
        net_pct=round(gross-fee_pct,6))


def match_controls(checkpoints, scenario_rows, *, scenario='TP3_SL1_48', max_pairs=40):
    """Post-hoc example selection only. Matching signature frozen pre-outcome.

    Pair a TP-FIRST checkpoint and SL-FIRST checkpoint in DISCOVERY from
    distinct symbols/processes. Never use selected pairs to claim a hit rate.
    """
    meta={r['checkpoint_id']:r for r in checkpoints}
    groups=defaultdict(lambda:defaultdict(list))
    for r in scenario_rows:
        if r['scenario'] != scenario or r['split'] != 'DISCOVERY_SYMBOL':
            continue
        if r['state'] not in ('TP_FIRST','TP_GAP_OPEN','SL_FIRST','SL_GAP_OPEN'):
            continue
        m=meta[r['checkpoint_id']]
        bucket='TP' if r['state'].startswith('TP') else 'SL'
        groups[m['signature']][bucket].append(m)
    pairs=[]; used=set()
    for sig in sorted(groups):
        wins=sorted(groups[sig]['TP'],key=lambda r:(r['process_id'],r['checkpoint_id']))
        losses=sorted(groups[sig]['SL'],key=lambda r:(r['process_id'],r['checkpoint_id']))
        for w in wins:
            if w['process_id'] in used: continue
            match=next((l for l in losses if l['process_id'] not in used and
                        l['process_id']!=w['process_id'] and l['symbol']!=w['symbol']),None)
            if match is None:continue
            pairs.append(dict(signature=sig,scenario=scenario,
                 good_checkpoint_id=w['checkpoint_id'],good_process_id=w['process_id'],
                 good_symbol=w['symbol'],bad_checkpoint_id=match['checkpoint_id'],
                 bad_process_id=match['process_id'],bad_symbol=match['symbol'],
                 selection='RETROSPECTIVE_REVIEW_ONLY_NOT_MODEL_VALIDATION'))
            used.update((w['process_id'],match['process_id']))
            if len(pairs)>=max_pairs:return pairs
    return pairs


def summarize_frozen_signatures(checkpoints, scenario_rows):
    """Fixed pre-outcome signatures, reported by SYMBOL split and by scenario.

    Do not pool multiple event types or count episodes/attempts as independent.
    A physical process contributes at most once to a specific event_type+scenario.
    Rate is descriptive among RESOLVED TP/SL first barriers only, not a hit rate
    of a live strategy or proof of predictive power.
    """
    cp = {c['checkpoint_id']: c for c in checkpoints}
    if len(cp) != len(checkpoints):
        raise ValueError('Duplicate checkpoint ID')
    by = defaultdict(list)
    for r in scenario_rows:
        c=cp[r['checkpoint_id']]
        by[(r['split'],r['scenario'],c['event_type'],c['signature'])].append(r)
    out=[]
    for (split,scene,event,signature),rows in sorted(by.items()):
        processes={r['process_id'] for r in rows}
        if len(processes)!=len(rows):
            raise ValueError('Duplicate process within event/scenario cohort')
        counts=Counter(r['state'] for r in rows)
        wins=sum(n for state,n in counts.items() if state.startswith('TP_'))
        losses=sum(n for state,n in counts.items() if state.startswith('SL_'))
        determinate=wins+losses
        out.append(dict(split=split,scenario=scene,event_type=event,
            fixed_causal_signature=signature,physical_processes=len(processes),
            tp_first=wins,sl_first=losses,determinate_first_barriers=determinate,
            tp_share_resolved_pct=round(100*wins/determinate,4) if determinate else '',
            timeout=counts.get('TIMEOUT',0),
            ambiguous_same_bar=counts.get('AMBIGUOUS_SAME_BAR',0),
            censored=counts.get('CENSORED_PATH',0),
            no_reference_open=counts.get('NO_REFERENCE_OPEN',0),
            status='EXPLORATORY_NOT_A_TRADE_BACKTEST'))
    return out


def write_csv(path, rows, columns=None):
    if columns is None:
        columns=list(dict.fromkeys(k for r in rows for k in r))
    if not columns:
        raise ValueError('Cannot write empty CSV without declared columns')
    with Path(path).open('w', encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        w.writeheader();w.writerows(rows)


def run(root, *, v1_dir=DEFAULT_V1, output_dir=DEFAULT_OUTPUT, merge_gap=2, seed=20261103):
    root=Path(root).resolve()
    v1, v1_summary, v1_manifest=validate_v1(root,Path(v1_dir),check_hash=True)
    attempts, levels=load_attempts(v1)
    validate_v1_identity(root,attempts)
    v1_count=validate_v1_processes(v1,attempts)
    if len(attempts) != v1_summary.get('attempts') or v1_count!=v1_summary.get('contact_process_rows'):
        raise ValueError('V1 summary/cardinality mismatch')
    destination=Path(output_dir)
    if not destination.is_absolute():destination=root/destination
    source_dirs=(v1.resolve(),(root/CANDLES).resolve(),(root/PRE.parent).resolve(),
                 (root/V29.parent).resolve(),(root/EPISODES.parent).resolve(),
                 (root/LEVELS.parent).resolve())
    dest_resolved=destination.resolve()
    if any(dest_resolved==src or src in dest_resolved.parents for src in source_dirs):
        raise ValueError('Refusing to place V2 outputs inside any source directory')
    if destination.exists():raise FileExistsError('Refusing overwrite: '+str(destination))
    physical, links, atr_source=build_physical_groups(root,levels,gap=merge_gap)
    for p in physical:p['split']=split_symbol(p['symbol'],seed)
    cps=[]; evolution=[]; scenarios=[]
    for p in physical:
        cp,e=causal_trace(p)
        cps.extend(cp); evolution.extend(e)
        for c in cp:
            for name,horizon,tp,sl in SCENARIOS:
                outcome=scenario_path(p['_candles'],c['hypothetical_next_open_ts'],
                    horizon=horizon,tp_pct=tp,sl_pct=sl)
                scenarios.append(dict(checkpoint_id=c['checkpoint_id'],
                  process_id=p['process_id'],symbol=p['symbol'],level_id=p['level_id'],
                  event_type=c['event_type'],split=c['split'],scenario=name,
                  known_closed_ts=c['known_closed_ts'],
                  hypothetical_entry_open_ts=c['hypothetical_next_open_ts'],
                  target_pct=tp,stop_pct=sl,horizon_30m=horizon,
                  fee_roundtrip_pct=FEE_ROUNDTRIP_PCT,**outcome))
    signature_outcomes=summarize_frozen_signatures(cps,scenarios)
    comparison=match_controls(cps,scenarios)
    # Detector comparison is RETROSPECTIVE only. Original frozen statuses of
    # later attempts never influence checkpoints or event signatures.
    detector_crosswalk=[]
    for p in physical:
        members=p['_members']
        detector_crosswalk.append(dict(
          process_id=p['process_id'],symbol=p['symbol'],level_id=p['level_id'],
          linked_attempts=len(members),linked_episodes=len({a['episode_id'] for a in members}),
          flip_confirmed_attempts=sum(a['v26_status'].startswith('FLIP_') for a in members),
          continuation_confirmed_attempts=sum(a['continuation_status']=='CONFIRMED' for a in members),
          reclaim_confirmed_attempts=sum(a['reclaim_status']=='CONFIRMED' for a in members),
          flip_states='|'.join(sorted({a['v26_status'] for a in members})),
          continuation_states='|'.join(sorted({a['continuation_status'] for a in members})),
          reclaim_states='|'.join(sorted({a['reclaim_status'] for a in members})),
          visibility='RETROSPECTIVE_ONLY_NOT_A_CAUSAL_FEATURE'))
    # No future aggregate columns are allowed in checkpoint rows.
    for c in cps:
        if set(c).intersection({'total_touch_bars','final_outcome','last_contact_known_closed_ts'}):
            raise AssertionError('Future data in checkpoint')
    summary={
      'version':VERSION,'mode':v1_summary['mode'],'symbols':len(set(a['symbol'] for a in attempts.values())),
      'source_attempts':len(attempts),'source_episodes':len({(a['symbol'],a['episode_id']) for a in attempts.values()}),
      'v1_per_attempt_contact_process_rows':v1_count,
      'unique_physical_processes_by_symbol_level':len(physical),
      'unique_physical_touch_bars':sum(p['physical_touch_bars'] for p in physical),
      'cross_episode_linked_physical_processes':sum(p['related_episode_count']>1 for p in physical),
      'physical_attempt_links':len(links),'causal_checkpoint_rows':len(cps),
      'source_v1_intermittent4_attempts':v1_summary.get('early4_intermit_n'),
      'source_v1_intermittent4_episodes':v1_summary.get('early4_intermit_episodes'),
      'processes_with_intermit4_checkpoint':len({c['process_id'] for c in cps
          if c['event_type']=='INTERMITTENT4_KNOWN'}),
      'causal_evolution_rows':len(evolution),'scenario_rows':len(scenarios),
      'comparisons_for_review':len(comparison),
      'original_frozen_detector_crosswalk_rows':len(detector_crosswalk),
      'signature_outcome_rows':len(signature_outcomes),
      'checkpoints_by_type':dict(Counter(c['event_type'] for c in cps)),
      'scenario_state_counts':{name:dict(Counter(r['state'] for r in scenarios if r['scenario']==name))
            for name,_,_,_ in SCENARIOS},
      'split_symbols':dict(Counter(split_symbol(sym,seed) for sym in {a['symbol'] for a in attempts.values()})),
      'formation_atr_source':atr_source,'merge_gap':merge_gap,'seed':seed,
      'scenarios':[dict(name=n,horizon_30m=h,tp_pct=tp,sl_pct=sl) for n,h,tp,sl in SCENARIOS],
      'roundtrip_fee_pct':FEE_ROUNDTRIP_PCT,
      'limitations':[
        'One physical contact process can still link several attempts and heuristic episodes.',
        'Processes are retrospectively grouped by unique original 30m OHLC bars; not a live detector.',
        'All checkpoints are closed-candle observations; no detector confirmation or executable signal is inferred.',
        'Matched examples are selected retrospectively for visual review, NOT for performance estimation.',
        'Raw 30m OHLC does not resolve within-candle TP/SL ordering or real fills.',
        'Fixed fee estimate only; funding, slippage, order book and portfolio dependence excluded.',
        'Repeated checkpoints from the same process are NOT independent samples.',
        'Symbol holdout is reserved for validation; do not tune based on holdout outcome.',
      ]}
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.hll-process-v2-stage-',dir=destination.parent) as d:
        stage=Path(d)
        files={
           'physical_processes.csv':[{k:v for k,v in p.items() if not k.startswith('_')} for p in physical],
           'process_attempt_links.csv':links,
           'process_detector_crosswalk.csv':detector_crosswalk,
           'signature_outcome_summary.csv':signature_outcomes,
           'process_evolution.csv':evolution,
           'process_checkpoints.csv':cps,
           'checkpoint_scenarios.csv':scenarios,
           'matched_review_pairs.csv':comparison,
        }
        cols={
         'physical_processes.csv':['process_id','symbol','level_id','zone_low','zone_high',
            'formation_atr','formation_atr_status','first_contact_open_ts',
            'first_contact_known_closed_ts','last_contact_known_closed_ts',
            'physical_touch_bars','grouped_contact_span_bars','related_attempt_count',
            'related_episode_count','related_attempt_ids','related_episode_ids',
            'process_grouping','split'],
         'process_attempt_links.csv':['process_id','attempt_id','symbol','level_id',
            'episode_id','attempt_pre_closed_ts','attempt_observed_until',
            'attempt_v26_status','attempt_continuation_status','attempt_reclaim_status','link_basis'],
         'process_detector_crosswalk.csv':['process_id','symbol','level_id',
           'linked_attempts','linked_episodes','flip_confirmed_attempts',
           'continuation_confirmed_attempts','reclaim_confirmed_attempts',
           'flip_states','continuation_states','reclaim_states','visibility'],
         'signature_outcome_summary.csv':['split','scenario','event_type',
           'fixed_causal_signature','physical_processes','tp_first','sl_first',
           'determinate_first_barriers','tp_share_resolved_pct','timeout',
           'ambiguous_same_bar','censored','no_reference_open','status'],
         'process_evolution.csv':['process_id','symbol','level_id','known_closed_ts',
            'observed_offset','current_relation','contact_bars_known','loss_crosses_known',
            'recovery_crosses_known','loss_observed_to_date','intermittent4_known',
            'zone_width_bucket','physical_touch_now','touch_from_above_now',
            'two_above_now','loss_now','recovery_now','defense_now','stage_is_descriptive'],
         'process_checkpoints.csv':['checkpoint_id','process_id','symbol','level_id',
            'event_type','known_closed_ts','hypothetical_next_open_ts',
            'observed_offset','current_relation','contact_bars_known','loss_crosses_known',
            'recovery_crosses_known','loss_observed_to_date','intermittent4_known',
            'zone_width_bucket','signature','split','reference_policy',
            'retrospective_process_link','no_entry_signal'],
         'checkpoint_scenarios.csv':['checkpoint_id','process_id','symbol','level_id',
            'event_type','split','scenario','known_closed_ts','hypothetical_entry_open_ts',
            'target_pct','stop_pct','horizon_30m','fee_roundtrip_pct',
            'state','reference_open','exit_price','exit_ts','observed_bars',
            'mfe_pct','mae_pct','gross_pct','net_pct'],
         'matched_review_pairs.csv':['signature','scenario','good_checkpoint_id',
            'good_process_id','good_symbol','bad_checkpoint_id','bad_process_id','bad_symbol',
            'selection'],
        }
        for filename,rows in files.items():write_csv(stage/filename,rows,cols[filename])
        (stage/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
        manifest={
          'version':VERSION,'mode':'OFFLINE_NO_LIVE','source_v1':str(v1),
          'source_v1_manifest_sha256':sha256(v1/'manifest.json'),
          'candle_folder':str(root/CANDLES),
          'candle_source_sha256':{s:sha256(root/CANDLES/f'{s}_30m.csv')
                 for s in sorted({a['symbol'] for a in attempts.values()})},
          'levels_sha256':sha256(root/LEVELS) if (root/LEVELS).is_file() else None,
          'v1_source_sha256':v1_manifest.get('pre_sha256'),
          'engine_v1_sha256':v1_manifest.get('engine_sha256'),
          'no_detector_changes':True,'no_pre_rerank':True,'no_live':True,
          'files_sha256':{n:sha256(stage/n) for n in [*files,'summary.json']},
        }
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
        stage.rename(destination)
    final={'status':'DONE','version':VERSION,'attempts':len(attempts),
          'physical_processes':len(physical),'v1_rows':v1_count,
          'checkpoints':len(cps),'scenario_rows':len(scenarios),
          'review_pairs':len(comparison),'output':str(destination)}
    print('HLL_PROCESS_V2_FINAL',json.dumps(final,ensure_ascii=False),flush=True)
    return final


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    modes=ap.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true')
    modes.add_argument('--run',action='store_true')
    ap.add_argument('--root',type=Path,default=Path('.'))
    ap.add_argument('--v1-dir',type=Path,default=DEFAULT_V1)
    ap.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    ap.add_argument('--merge-gap',type=int,default=2)
    ap.add_argument('--seed',type=int,default=20261103)
    args=ap.parse_args(argv)
    root=args.root.resolve()
    if args.plan:
        v1,summary,manifest=validate_v1(root,args.v1_dir,check_hash=False)
        print('HLL_PROCESS_V2_PLAN',json.dumps(dict(version=VERSION,mode='OFFLINE_NO_LIVE',
            source=str(v1),source_attempts=summary['attempts'],
            source_v1_contact_rows=summary['contact_process_rows'],
            candle_folder_present=(root/CANDLES).is_dir(),
            levels_file_present=(root/LEVELS).is_file(),
            output=str((root/args.output_dir).resolve()),
            merge_gap=args.merge_gap,source_hashes_verified=False,
            writes_source_files=False),ensure_ascii=False))
    else:
        run(root,v1_dir=args.v1_dir,output_dir=args.output_dir,
            merge_gap=args.merge_gap,seed=args.seed)

if __name__=='__main__':
    main()
