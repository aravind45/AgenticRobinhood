# regime.py
# Hidden Markov Model regime detector.
# States: MEAN_REVERTING (trade), TRENDING (avoid), VOLATILE (avoid)
#
# Each call to update_regime():
#   1. Loads prior belief from regime_state.json
#   2. Predicts next state via transition matrix (Markov step)
#   3. Updates belief using observed features as emission probabilities (Bayes)
#   4. Saves posterior and returns current regime

import json
import math
import os

REGIME_FILE = os.path.join(os.path.dirname(__file__), "regime_state.json")

STATES = ["MEAN_REVERTING", "TRENDING", "VOLATILE"]

# Transition matrix P(next | current) — regimes persist more than they switch
TRANSITION = {
    "MEAN_REVERTING": {"MEAN_REVERTING": 0.75, "TRENDING": 0.15, "VOLATILE": 0.10},
    "TRENDING":       {"MEAN_REVERTING": 0.20, "TRENDING": 0.65, "VOLATILE": 0.15},
    "VOLATILE":       {"MEAN_REVERTING": 0.25, "TRENDING": 0.15, "VOLATILE": 0.60},
}

# Default prior — start slightly biased toward mean-reverting (our strategy assumption)
DEFAULT_PRIOR = {"MEAN_REVERTING": 0.60, "TRENDING": 0.25, "VOLATILE": 0.15}


def _emission(features: dict) -> dict:
    """
    P(features | regime) — likelihood of observing these features in each regime.

    Key design decision: we trade SHORT-TERM mean reversion inside any broader trend.
    TRENDING only blocks when SHORT-TERM momentum is strong, not because of a
    multi-week SMA gap. RSI(2) extremes are the primary MR signal.
    """
    vol_ratio   = features.get("vol_ratio")   or 1.0
    momentum    = abs(features.get("momentum_norm") or 0)   # 1-day return / 20d vol
    volume_z    = abs(features.get("volume_zscore")  or 0)
    rsi2        = features.get("vivek_rsi2")  or 50.0
    rsi_extreme = abs(rsi2 - 50) / 50        # 0=neutral centre, 1=max extreme

    scores = {}

    # ── MEAN_REVERTING ────────────────────────────────────────────────────
    # Dominant evidence: RSI extreme + compressed short-term vol
    mr = 1.0
    mr *= 0.5 + rsi_extreme * 2.0            # RSI(2) at 10 → extreme=0.8 → ×2.1 (strongest factor)
    mr *= max(0.1, 1.5 - vol_ratio)          # vol_ratio 0.55 → ×0.95 (mild boost)
    mr *= max(0.2, 1.2 - momentum * 0.5)    # low 1-day momentum favours MR
    scores["MEAN_REVERTING"] = max(0.01, mr)

    # ── TRENDING ──────────────────────────────────────────────────────────
    # Only flag TRENDING when short-term momentum is genuinely strong (not just SMA gap)
    tr = 1.0
    tr *= max(0.05, momentum * 1.2)          # must have actual recent momentum
    tr *= max(0.1, 1.0 - rsi_extreme * 1.5) # extreme RSI makes trend unlikely
    tr *= max(0.1, vol_ratio * 0.8)          # moderate vol supports trend
    scores["TRENDING"] = max(0.01, tr)

    # ── VOLATILE ──────────────────────────────────────────────────────────
    vl = 1.0
    vl *= max(0.05, vol_ratio - 0.5)         # elevated vol ratio
    vl *= max(0.05, 0.7 + volume_z * 0.4)   # volume spikes
    scores["VOLATILE"] = max(0.01, vl)

    total = sum(scores.values())
    return {s: scores[s] / total for s in STATES}


def update_regime(features: dict) -> dict:
    """
    Run one step of the HMM filter.
    Returns {"state": str, "belief": {state: probability}, "confidence": float}
    """
    # Load prior belief
    if os.path.exists(REGIME_FILE):
        with open(REGIME_FILE) as f:
            prior = json.load(f)
        # Validate keys
        if not all(s in prior for s in STATES):
            prior = DEFAULT_PRIOR.copy()
    else:
        prior = DEFAULT_PRIOR.copy()

    # Step 1: Markov prediction — blur the prior through transition matrix
    predicted = {j: sum(prior[i] * TRANSITION[i][j] for i in STATES) for j in STATES}

    # Step 2: Bayesian update — weight predicted by emission likelihoods
    emissions = _emission(features)
    updated = {s: predicted[s] * emissions[s] for s in STATES}

    # Normalize to get posterior
    total = sum(updated.values())
    belief = {s: round(updated[s] / total, 4) for s in STATES}

    # Save posterior for next call
    with open(REGIME_FILE, "w") as f:
        json.dump(belief, f, indent=2)

    state = max(belief, key=belief.get)
    confidence = round(belief[state] * 100, 1)

    return {"state": state, "belief": belief, "confidence": confidence}


def is_tradeable(regime: dict) -> tuple[bool, str]:
    """
    Return (tradeable, reason).
    VOLATILE = hard block (unpredictable, can't mean-revert into chaos).
    TRENDING = soft warning, confidence reduced via Bayesian step but trade allowed.
    MEAN_REVERTING = ideal, full confidence.
    """
    state = regime["state"]
    belief = regime["belief"]

    if state == "VOLATILE" and belief["VOLATILE"] > 0.55:
        return False, f"Regime VOLATILE ({belief['VOLATILE']*100:.0f}% probable) — hard block"
    if state == "TRENDING":
        return True, f"Regime TRENDING ({belief['TRENDING']*100:.0f}% probable) — confidence penalised"
    return True, f"Regime {state} ({belief[state]*100:.0f}% probable) — proceed"


if __name__ == "__main__":
    # Quick test with sample features
    sample = {
        "vol_ratio": 0.55, "momentum_norm": 0.3,
        "volume_zscore": 0.5, "sma5_vs_sma20": -0.2,
        "vivek_rsi2": 9.9,
    }
    result = update_regime(sample)
    print(f"Regime: {result['state']}  ({result['confidence']}% confidence)")
    print(f"Belief: {result['belief']}")
    tradeable, reason = is_tradeable(result)
    print(f"Tradeable: {tradeable} — {reason}")
