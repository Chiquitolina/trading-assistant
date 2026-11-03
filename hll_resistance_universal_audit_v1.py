#!/usr/bin/env python3
"""HLL | Universal Resistance Inspector V1: read-only POST research.

Two independent views for an exact historical attempt:
  (A) price behavior at an immutable horizontal zone, from closed 30m OHLC;
  (B) frozen V2.6/V2.9 statuses plus a reproducible detector replay and gate trace.

Nothing here generates an entry, changes PRE features or changes live trading.
The descriptive behavior IS NOT an alternative Flip/Continuation/Reclaim detector.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import math

TF = 1_800_000
VERSION = 'HLL_RESISTANCE_UNIVERSAL_AUDIT_V1'
ROUTES = ('FLIP_1M', 'V29_CONTINUATION_30M', 'V29_RECLAIM_30M')


def ms(value):
    """Require an exact integral epoch millisecond, never round a float date."""
    try:
        n = Decimal(str(value).strip())
        if not n.is_finite() or n != n.to_integral_value():
            raise ValueError('fractional millisecond')
        ans = int(n)
        if ans <= 0 or ans % TF:
            raise ValueError('nonaligned closed bar')
        return ans
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError(f'Invalid closed 30m timestamp: {value!r}') from exc


def bar_map(candles):
    """Accept the already loaded Discovery OHLC mapping (open_ts -> candle)."""
    result = {}
    for raw_ts, raw in candles.items():
        ts = ms(raw_ts)
        if ts in result:
            raise ValueError('Duplicate candle timestamp')
        if int(ms(raw['open_ts'])) != ts or int(ms(raw['close_ts'])) != ts + TF:
            raise ValueError('Mismatched candle clock')
        o, h, l, c = [float(raw[key]) for key in ('open', 'high', 'low', 'close')]
        if not all(math.isfinite(v) for v in (o, h, l, c)) or not (0 < l <= min(o,c) <= max(o,c) <= h):
            raise ValueError('Invalid candle OHLC')
        result[ts] = {'timestamp':ts,'open':o,'high':h,'low':l,'close':c}
    return result


def price_behavior(candles, *, pre_closed_ts, breakout_closed_ts, zone_low, zone_high,
                   formation_atr=None, horizon=96):
    """Describe only what is visible in each CLOSED bar, with an explicit clock.

    A physical touch is *not* a flip. A from-above touch additionally requires
    the previous CLOSED bar to be strictly above the zone high. A descriptive
    defense candidate is known only at the subsequent qualifying CLOSED bar;
    it does not prove that V2.6 would accept a flip.
    """
    pre, breakout = ms(pre_closed_ts), ms(breakout_closed_ts)
    lo, hi = float(zone_low), float(zone_high)
    if not (0 < lo <= hi) or not (4 <= horizon <= 288) or breakout != pre + TF:
        raise ValueError('Invalid zone/window or breakout != PRE + 30m')
    formation = float(formation_atr) if formation_atr is not None else None
    if formation is not None and (not math.isfinite(formation) or formation <= 0):
        raise ValueError('Invalid formation ATR')
    candles = bar_map(candles)
    if pre not in candles or breakout - TF not in candles:
        raise ValueError('Missing exact breakout and PRE candles')
    base = candles[breakout - TF]
    # Some frozen CROSS_UP attempts did not close beyond zone_high. Preserve
    # them as explicitly non-above closes, not missing from the universal audit.
    anchor_closes_above = base['close'] > hi
    rows, events = [], []
    loss_count = 0
    pending_contacts = []
    first_gap = None
    previous = candles.get(pre - TF)
    for offset in range(horizon):
        ts = pre + offset * TF
        if ts not in candles:
            first_gap = offset
            break
        b = candles[ts]
        relation = 'ABOVE' if b['close'] > hi else 'BELOW' if b['close'] < lo else 'IN_ZONE'
        physical_touch = b['low'] <= hi and b['high'] >= lo
        prior_close = previous['close'] if previous is not None else None
        touch_from_above = bool(offset > 0 and prior_close is not None and prior_close > hi and physical_touch)
        cross_up = bool(offset > 0 and prior_close is not None and prior_close <= hi and b['close'] > hi)
        cross_down = bool(offset > 0 and prior_close is not None and prior_close >= lo and b['close'] < lo)
        pair_above = bool(offset > 0 and prior_close is not None and prior_close > hi and b['close'] > hi)
        if relation == 'BELOW':
            loss_count += 1
        row = dict(bar_offset=offset,open_ts=ts,known_closed_ts=ts+TF,
            open=b['open'],high=b['high'],low=b['low'],close=b['close'],
            relation=relation,physical_touch=physical_touch,touch_from_above=touch_from_above,
            cross_up=cross_up,cross_down=cross_down,two_closes_above=pair_above,
            original_first4=offset < 4, candidate_defense=False)
        rows.append(row)
        # Single-bar observations only; timestamp is the CLOSED bar.
        for label, yes in [('CONTACTO_FISICO',physical_touch),
                           ('CONTACTO_DESDE_ARRIBA',touch_from_above),
                           ('DOS_CIERRES_ENCIMA',pair_above),('PERDIDA_ZONA',cross_down),
                           ('RECUPERACION_CIERRE',cross_up)]:
            if yes:
                events.append(dict(event=label,known_closed_ts=ts+TF,bar_offset=offset,
                                   price=b['close'],note='DESCRIPTIVO_POST'))
        # Deferred evidence: contact may be defended within the following 4
        # closes at formation ATR threshold, but only if NO interim close is lost.
        if formation is not None:
            if touch_from_above and b['close'] >= lo:
                pending_contacts.append({'offset':offset,'expires':offset+4})
            alive = []
            defended = False
            for cand in pending_contacts:
                if offset <= cand['offset']:
                    alive.append(cand)
                    continue
                if b['close'] < lo or offset > cand['expires']:
                    continue
                if b['close'] > hi + 0.5*formation:
                    if not defended:
                        row['candidate_defense'] = True
                        events.append(dict(event='DEFENSA_DESCRIPTIVA',known_closed_ts=ts+TF,
                            bar_offset=offset,price=b['close'],note=f'contacto en barra {cand["offset"]}; NO equivale a Flip V2.6'))
                        defended = True
                    continue
                alive.append(cand)
            pending_contacts = [] if defended else alive
        previous = b
    counts = dict(Counter(e['event'] for e in events))
    summary = {'breakout_close_above_zone_high':anchor_closes_above,
        'observed_post_bars':len(rows), 'requested_post_bars':horizon,
        'complete_window':first_gap is None,'first_gap_offset':first_gap,
        'touches_from_above':counts.get('CONTACTO_DESDE_ARRIBA',0),
        'physical_zone_contacts':counts.get('CONTACTO_FISICO',0),
        'two_closes_above_observed':counts.get('DOS_CIERRES_ENCIMA',0),
        'loss_crosses':counts.get('PERDIDA_ZONA',0),
        'recovery_crosses':counts.get('RECUPERACION_CIERRE',0),
        'descriptive_defenses':counts.get('DEFENSA_DESCRIPTIVA',0),
        'closes_below_zone':loss_count}
    return {'version':VERSION,'bars':rows,'events':events,'summary':summary}


def continuation_gates(candles, *, breakout_closed_ts, zone_low, zone_high, atr_pre, params=None):
    """Explain V2.9 criteria bar by bar; replay of original module is separate.

    Keep the frozen 12-bar clock; never reinterpret bars after a zone loss as
    eligible for the same direct continuation.
    """
    from dashboard.views.hll_v29_continuation_lab import Params
    p = params if params is not None else Params()
    original = bar_map(candles)
    anchor = ms(breakout_closed_ts)-TF
    lo,hi,atr = float(zone_low),float(zone_high),float(atr_pre)
    if not 0 < lo <= hi or not math.isfinite(atr) or atr <= 0:
        raise ValueError('Bad continuation band or ATR')
    if anchor not in original:
        return [{'offset':0,'state':'ANCHOR_MISSING'}]
    if original[anchor]['close'] <= hi:
        return [{'offset':0,'state':'NOT_CROSS_ABOVE_ZONE'}]
    prev = original[anchor]
    running_high = prev['high']
    consecutive = 1
    gates=[]
    for offset in range(1,p.confirmation_window_bars+1):
        ts = anchor + offset*TF
        if ts not in original:
            gates.append(dict(offset=offset,known_closed_ts=ts+TF,state='CENSORED_CONTINUATION'))
            break
        b = original[ts]
        if b['close'] < lo:
            gates.append(dict(offset=offset,known_closed_ts=ts+TF,state='LOST_ZONE_BEFORE_CONTINUATION'))
            break
        consecutive = consecutive+1 if b['close']>hi else 0
        displacement=(b['close']-hi)/atr
        close_location=(b['close']-b['low'])/(b['high']-b['low']) if b['high']>b['low'] else (1.0 if b['close']>=b['open'] else 0.0)
        new_high=b['high']>running_high
        flags = dict(two_closes=consecutive>=p.min_consecutive_above,
            min_displacement=displacement>=p.min_displacement_atr,
            max_displacement=displacement<=p.max_displacement_atr,
            strong_close=close_location>=p.min_close_location,
            new_high=new_high)
        confirmed=all(flags.values())
        gates.append(dict(offset=offset,known_closed_ts=ts+TF,
            state='CONFIRMED' if confirmed else 'NOT_CONFIRMED_THIS_BAR',
            consecutive_closes=consecutive,displacement_atr=round(displacement,5),
            close_location=round(close_location,5),**flags))
        if confirmed:break
        running_high=max(running_high,b['high'])
        prev=b
    return gates


def replay_frozen(candles, *, breakout_closed_ts, zone_low, zone_high,
                  formation_atr, atr_pre, frozen, failed_closed_ts=None):
    """Replay ORIGINAL V2.6 and V2.9 functions, compare exact frozen statuses.

    Freeze mismatches explicitly: do not call them false negatives and do not
    overwrite any original status. Reclaim uses ONLY V2.6-eligible failures.
    """
    import pandas as pd
    from dashboard.views.hll_v26_structure_lab import classify_followup, Rules
    from dashboard.views.hll_v29_continuation_lab import first_continuation, reclaim_continuation, Params, FAILURES
    anchor = ms(breakout_closed_ts)-TF
    d = bar_map(candles)
    times = sorted(d)
    if anchor not in d:
        raise ValueError('Breakout candle not found for replay')
    idx=times.index(anchor)
    replay, _ = classify_followup(ms(breakout_closed_ts),idx,times,
        [d[t]['low'] for t in times],[d[t]['high'] for t in times],[d[t]['close'] for t in times],
        float(zone_low),float(zone_high),float(formation_atr), Rules())
    df = pd.DataFrame([d[t] for t in times])
    direct = first_continuation(df,ms(breakout_closed_ts),float(zone_low),float(zone_high),float(atr_pre),Params())
    failure = (int(replay['failed_closed_ts']) if replay['status'] in FAILURES and replay['failed_closed_ts'] else None)
    reclaim, _ = reclaim_continuation(df,ms(breakout_closed_ts),failure,
        float(zone_low),float(zone_high),float(atr_pre),Params())
    originals={r['route']:r['mechanism_status'] for r in frozen}
    if set(originals)!=set(ROUTES) or len(frozen)!=3:
        raise ValueError('Incomplete three-route audited mechanism overlay')
    latest={'FLIP_1M':replay['status'],
            'V29_CONTINUATION_30M':direct['status'],
            'V29_RECLAIM_30M':reclaim['status']}
    table=[]
    for record in frozen:
        route=record['route']
        table.append(dict(route=route,original_status=originals[route],
            replay_status=latest[route],replay_equal=originals[route]==latest[route],
            entry_coverage=record['entry_status'],
            confirmed_closed_ts=record['confirmed_closed_ts'],
            entry_ts=record['entry_ts']))
    return {'rows':table,'all_match':all(r['replay_equal'] for r in table),
            'v26_transitions':replay, 'gate_rows':continuation_gates(candles,
                breakout_closed_ts=breakout_closed_ts,zone_low=zone_low,
                zone_high=zone_high,atr_pre=atr_pre),
            'note':'Replay congelado. Gates V2.9 descriptivos; NO nuevo detector.'}
