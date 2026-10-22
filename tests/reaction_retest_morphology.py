"""Three hypothesis-led REACTION shapes, with strict knowledge-time separation.

PRE_* uses only complete 1m bars strictly before retest. KNOWN_* uses the
closed 1m retest candle and a previously confirmed swing level if available.
POST_* uses 1m future Auto Paths and is retrospective DESCRIPTION ONLY.
No future-derived value is valid for a PRE/KNOWN trading prediction.
"""
from __future__ import annotations
import math

MORPH_NUMERIC=(
    'pre_adverse_5m_pct','pre_adverse_15m_pct','pre_approach_speed_ratio',
    'pre_adverse_previous5m_pct','pre_adverse_deceleration_pct',
    'pre_quote_recent5_vs_previous5','pre_trades_recent5_vs_previous5',
    'pre_taker_side_recent5','pre_taker_side_previous5','pre_taker_relief5',
    'known_swing_penetration_pct','known_retest_closes_beyond_level',
    'post_reclaim_minute_15m',
)
MORPH_LABELS=('pre_morphology','known_morphology','post_morphology')
MORPH_FIELDS=MORPH_NUMERIC+MORPH_LABELS
MORPH_VERSION='morphology-v1.0'


def safe_ratio(a,b):
    return a/b if b and math.isfinite(b) else None


def pct(a,b):
    return 100*(b/a-1) if a and a>0 else None


def _imbalance(bars,sign):
    quote=sum(b[6] for b in bars)
    taker=sum(b[9] for b in bars)
    return sign*(2*taker/quote-1) if quote>0 else None


def calculate_morphology(side,pre,retest=None,swing_level=None,post=None):
    """Classify three shapes without labeling missing bars as losing events.

    Each input pre bar has the audited canonical 1m tuple:
    (open_ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base,taker_buy_quote).
    post requires contiguous 1m bars starting the minute after retest, and must
    NEVER be used as a model predictor or in an entry selector.
    """
    if side not in ('LONG','SHORT'):
        raise ValueError('side must be LONG or SHORT')
    d=1 if side=='LONG' else -1
    res={k:None for k in MORPH_FIELDS}
    res['pre_morphology']='MISSING_PRE'
    res['known_morphology']='NOT_KNOWN'
    res['post_morphology']='NO_VALID_POST'
    if len(pre)<16 or any(int(b[0])!=int(pre[i-1][0])+60_000 for i,b in enumerate(pre[1:],1)):
        return res
    close5=pre[-6][4];close15=pre[-16][4];last=pre[-1][4]
    prev5=pre[-11][4]
    adverse5=-d*pct(close5,last)
    adverse15=-d*pct(close15,last)
    adverse_prev5=-d*pct(prev5,close5)
    recent=pre[-5:]; previous=pre[-10:-5]
    qrecent=sum(b[6] for b in recent);qprev=sum(b[6] for b in previous)
    trecent=sum(b[7] for b in recent);tprev=sum(b[7] for b in previous)
    ir=_imbalance(recent,d);ip=_imbalance(previous,d)
    res.update(pre_adverse_5m_pct=adverse5,pre_adverse_15m_pct=adverse15,
      pre_approach_speed_ratio=safe_ratio(adverse5*3,adverse15) if adverse15>0 else None,
      pre_adverse_previous5m_pct=adverse_prev5,
      pre_adverse_deceleration_pct=adverse_prev5-adverse5,
      pre_quote_recent5_vs_previous5=safe_ratio(qrecent,qprev),
      pre_trades_recent5_vs_previous5=safe_ratio(trecent,tprev),
      pre_taker_side_recent5=ir,pre_taker_side_previous5=ip,
      pre_taker_relief5=ir-ip if ir is not None and ip is not None else None)
    # Distinct PRE hypotheses; overlap takes priority as rapid if accelerating.
    rapid=(adverse5>=.35 and adverse15>=.20 and
           res['pre_approach_speed_ratio'] is not None and
           res['pre_approach_speed_ratio']>=1.5)
    # Slow approach: still toward the level, deceleration AND diminishing
    # volume. No use of retest candle or future in this classification.
    slow=(adverse15>=.25 and adverse5>=-.05 and adverse_prev5>0 and
          adverse5<=adverse_prev5*.65 and
          res['pre_quote_recent5_vs_previous5'] is not None and
          res['pre_quote_recent5_vs_previous5']<=.85)
    res['pre_morphology']='FAST_APPROACH' if rapid else ('SLOW_FADING' if slow else 'OTHER_PRE')
    if retest is None:
        return res
    op,hi,lo,cl=(float(x) for x in retest[1:5])
    span=hi-lo
    strength=(cl-lo)/span if d==1 and span>0 else ((hi-cl)/span if span>0 else .5)
    rejection=(min(op,cl)-lo)/span if d==1 and span>0 else (
        (hi-max(op,cl))/span if span>0 else 0)
    level=None
    try:
        if swing_level is not None and math.isfinite(float(swing_level)) and float(swing_level)>0:
            level=float(swing_level)
    except (TypeError,ValueError):
        pass
    penetration=None
    beyond=None
    if level is not None:
        penetration=(level-lo)/level*100 if d==1 else (hi-level)/level*100
        beyond=int(cl<level) if d==1 else int(cl>level)
        res['known_swing_penetration_pct']=penetration
        res['known_retest_closes_beyond_level']=beyond
    # A break that closes outside the confirmed swing has its own class.
    deep=penetration is not None and penetration>=.15 and beyond==1
    if deep:
        res['known_morphology']='DEEP_BREAK_OUTSIDE'
    elif rapid and rejection>=.30 and strength>=.65:
        res['known_morphology']='FAST_REJECTION'
    elif slow and strength>=.50:
        res['known_morphology']='SLOW_EXHAUSTION'
    elif level is None and not (rapid or slow):
        res['known_morphology']='NO_SWING_LEVEL_OTHER'
    else:
        res['known_morphology']='OTHER_KNOWN'
    if not deep:
        res['post_morphology']='NOT_DEEP_BREAK'
        return res
    if post is None:
        return res
    # POST is only defined if ALL 15 future 1m bars are continuous.
    if len(post)<15 or any(int(bar[0])!=int(retest[0])+(i+1)*60_000 for i,bar in enumerate(post[:15])):
        return res
    reclaim=next((i+1 for i,bar in enumerate(post[:15])
                  if ((bar[4]>=level) if d==1 else (bar[4]<=level))),None)
    res['post_reclaim_minute_15m']=reclaim
    if reclaim is None:
        res['post_morphology']='POST_NO_RECLAIM_15M'
    elif reclaim>=3:
        res['post_morphology']='POST_LATE_RECLAIM_3_TO_15M'
    else:
        res['post_morphology']='POST_EARLY_RECLAIM_1_TO_2M'
    return res
