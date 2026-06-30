# trade_signal.py
# Builds a live signal dict from Robinhood MCP data + computed indicators.
# Replaces the old hardcoded SPY_SIGNAL.

import math
from datetime import datetime, timezone
from live_data import get_live_features
from regime import update_regime, is_tradeable


def get_signal(symbol: str = "SPY") -> dict:
    """
    Pull live market data, compute Vivek + QTrend indicators,
    and return a unified signal dict for the agent to validate.
    """
    f = get_live_features(symbol)

    # ── Regime filter (Markov HMM) ────────────────────────────────────────
    regime = update_regime(f)
    tradeable, regime_reason = is_tradeable(regime)

    # ── Channel converging ────────────────────────────────────────────────
    ema_dist = abs(f["vivek_ema10"] - f["vivek_ema20"])
    channel_width = f["vivek_ch_top"] - f["vivek_ch_bot"]
    channel_converging = channel_width > 0 and (ema_dist / channel_width) < 0.20

    # ── Confluence signal ─────────────────────────────────────────────────
    vivek_red   = f["vivek_state"] == -1
    vivek_green = f["vivek_state"] == 1
    qt_buy  = f["qtrend_state"] == 1
    qt_sell = f["qtrend_state"] == -1

    if vivek_red and qt_buy:
        signal_dir = "BUY"
    elif vivek_green and qt_sell:
        signal_dir = "SELL"
    else:
        signal_dir = "NEUTRAL"

    # Override to NEUTRAL if regime filter blocks the trade
    if not tradeable and signal_dir != "NEUTRAL":
        signal_dir = "BLOCKED"

    # ── Bayesian confidence (log-odds update) ─────────────────────────────
    # Prior: 55% base win rate for this exhaustion strategy
    log_odds = math.log(0.55 / 0.45)

    is_buy = signal_dir == "BUY"
    rsi2     = f.get("vivek_rsi2") or 50.0
    vol_ratio = f.get("vol_ratio") or 1.0
    volume_z  = f.get("volume_zscore") or 0.0

    # ── Fisher Transform — primary exhaustion detector ────────────────────
    # Replaces stepped RSI thresholds with a continuous, symbol-agnostic weight.
    # BUY needs fisher_value < 0 (price at bottom of range = oversold)
    # SELL needs fisher_value > 0 (price at top of range = overbought)
    fisher_val = f.get("fisher_value") or 0.0
    fisher_sig = f.get("fisher_signal") or 0.0
    if signal_dir in ("BUY", "SELL"):
        # Signed extreme: positive = aligned with trade direction
        fisher_extreme = -fisher_val if is_buy else fisher_val
        if fisher_extreme > 2.0:   log_odds += math.log(4.0)   # rare statistical extreme
        elif fisher_extreme > 1.5: log_odds += math.log(3.0)   # strong exhaustion
        elif fisher_extreme > 1.0: log_odds += math.log(2.0)   # notable extreme
        elif fisher_extreme > 0.5: log_odds += math.log(1.3)   # mild extreme
        else:                      log_odds += math.log(0.7)   # not extreme — penalty

        # Fisher crossover confirmation: fisher crossed its signal line this bar
        # BUY: fisher was more negative than signal → now turning up (bottom confirmed)
        # SELL: fisher was more positive than signal → now turning down (top confirmed)
        crossed_buy  = is_buy  and fisher_val > fisher_sig and fisher_sig < -0.5
        crossed_sell = not is_buy and fisher_val < fisher_sig and fisher_sig > 0.5
        if crossed_buy or crossed_sell:
            log_odds += math.log(1.5)   # crossover = timing confirmation

    # RSI(2) — kept as secondary cross-check (catches cases Fisher misses on very short windows)
    if signal_dir in ("BUY", "SELL"):
        rsi_dist = (50 - rsi2) if is_buy else (rsi2 - 50)
        if rsi_dist > 40:   log_odds += math.log(1.4)   # RSI(2) corroborates Fisher
        elif rsi_dist < 10: log_odds += math.log(0.85)  # RSI not extreme — mild drag

    # Channel convergence
    log_odds += math.log(1.5) if channel_converging else math.log(0.85)

    # Volatility regime
    if 0.7 < vol_ratio < 1.3:  log_odds += math.log(1.2)   # stable vol
    elif vol_ratio > 1.5:       log_odds += math.log(0.65)  # chaotic

    # Volume confirmation
    if abs(volume_z) > 1.5:    log_odds += math.log(1.3)   # elevated volume

    # Vivek pivot divergence (strongest technical confirmation)
    if (is_buy and f.get("vivek_strong_long")) or (not is_buy and f.get("vivek_strong_short")):
        log_odds += math.log(2.0)

    # QTrend near range extreme
    if (is_buy and f.get("qtrend_strong_buy")) or (not is_buy and f.get("qtrend_strong_sell")):
        log_odds += math.log(1.5)

    # Kalman velocity — confirms or contradicts mean-reversion thesis
    # For BUY: velocity should be negative (price drifting down = exhaustion) → boost confidence
    # For SELL: velocity should be positive (price drifting up = exhaustion) → boost confidence
    kf_vel = f.get("kf_velocity") or 0.0
    if signal_dir in ("BUY", "SELL"):
        vel_aligned = (is_buy and kf_vel < 0) or (not is_buy and kf_vel > 0)
        vel_magnitude = abs(kf_vel) / max(f.get("last_price", 500), 1) * 1000  # normalise to per-mille
        if vel_aligned and vel_magnitude > 0.3:
            log_odds += math.log(1.4)   # velocity confirms exhaustion direction
        elif not vel_aligned and vel_magnitude > 0.5:
            log_odds += math.log(0.75)  # velocity fighting the trade — mild penalty

    # Regime belief boosts/penalises
    mr_prob = regime["belief"]["MEAN_REVERTING"]
    log_odds += math.log(max(0.1, mr_prob / 0.5))  # scales around neutral prior of 50%

    posterior = 1 / (1 + math.exp(-log_odds))
    confidence = round(posterior * 100)
    # For NEUTRAL/BLOCKED, confidence reflects uncertainty
    if signal_dir in ("NEUTRAL", "BLOCKED"):
        confidence = round((1 - posterior) * 100) if posterior > 0.5 else round(posterior * 100)

    return {
        "symbol":              symbol,
        "timeframe":           "1D",
        "signal":              signal_dir,
        "signal_time":         datetime.now(timezone.utc).isoformat(),
        "price":               f["last_price"],
        "confidence":          min(confidence, 100),

        # Vivek
        "vivek_state":         f["vivek_state"],
        "vivek_dir_trend":     f["vivek_dir_trend"],
        "vivek_long_entry":    f["vivek_long_entry"],
        "vivek_short_entry":   f["vivek_short_entry"],
        "vivek_strong_long":   f["vivek_strong_long"],
        "vivek_strong_short":  f["vivek_strong_short"],
        "vivek_rsi2":          f["vivek_rsi2"],
        "vivek_ema10":         f["vivek_ema10"],
        "vivek_ema20":         f["vivek_ema20"],
        "vivek_ch_top":        f["vivek_ch_top"],
        "vivek_ch_bot":        f["vivek_ch_bot"],
        "vivek_sma200":        f["vivek_sma200"],

        # QTrend
        "qtrend_state":        f["qtrend_state"],
        "qtrend_last_signal":  f["qtrend_last_signal"],
        "qtrend_strong_buy":   f["qtrend_strong_buy"],
        "qtrend_strong_sell":  f["qtrend_strong_sell"],
        "qtrend_trend_line":   f["qtrend_trend_line"],

        # Fisher Transform
        "fisher_value":        f.get("fisher_value"),   # neg=oversold, pos=overbought
        "fisher_signal":       f.get("fisher_signal"),  # previous bar (crossover ref)

        # Kalman Filter
        "kf_level":            f.get("kf_level"),
        "kf_velocity":         f.get("kf_velocity"),

        # Market features
        "channel_converging":  channel_converging,
        "vol_ratio":           f.get("vol_ratio"),
        "volume_zscore":       f.get("volume_zscore"),
        "spread_pct":          f.get("spread_pct"),
        "buying_power":        f.get("buying_power"),
        "open_position":       f.get("open_position"),

        # Regime
        "regime_state":        regime["state"],
        "regime_belief":       regime["belief"],
        "regime_tradeable":    tradeable,
        "regime_reason":       regime_reason,

        "strategy_version":    "2.0",
    }


# Backwards-compatible alias used by execute.py / monitor.py
SPY_SIGNAL = {}   # populated at runtime — see get_signal()


if __name__ == "__main__":
    import pprint
    print("Building live signal...\n")
    sig = get_signal("SPY")
    pprint.pprint(sig)
    print(f"\n→ Signal: {sig['signal']}  Confidence: {sig['confidence']}")
