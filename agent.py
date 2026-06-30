# agent.py
# Sends signal to Claude → gets APPROVE / REJECT

import anthropic
import json
import os
from trade_signal import SPY_SIGNAL

DECISION_PROMPT = """
You are the Decision Engine for an autonomous trading system called Project Alpha.
You receive both the Pine Script channel signal AND live market features pulled from Robinhood.

This system trades large-cap US equities (AMD, NVDA, META, TSLA, GOOGL) using OPTIONS —
buying ATM calls on BUY signals and ATM puts on SELL signals, ~14 DTE, 1 contract.

The underlying strategy is EXHAUSTION / MEAN REVERSION:

BUY signal (bearish exhaustion — expect bounce up):
- vivek_state = -1 (Vivek RED/bearish) AND qtrend_state = 1 (QTrend in Buy)
- Channels converging (EMA10/EMA20 squeezing together)
- RSI(2) oversold (vivek_rsi2 < 15) adds confidence

SELL signal (bullish exhaustion — expect drop down):
- vivek_state = 1 (Vivek GREEN/bullish) AND qtrend_state = -1 (QTrend in Sell)
- Channels converging (EMA10/EMA20 squeezing together)
- RSI(2) overbought (vivek_rsi2 > 85) adds confidence

Pine Script has already confirmed confluence. Risk Engine has already passed all hard rules.

Your ONLY job — check operational context:
1. Do vivek_state and qtrend_state match the signal direction per the rules above?
2. Is confidence >= 70?
3. Are channels converging (channel_converging = True is ideal, but not a hard block)?
4. Check the regime filter:
   - regime_state = MEAN_REVERTING → favourable, proceed
   - regime_state = TRENDING → mean reversion trades fail in trends, REJECT unless confidence > 85
   - regime_state = VOLATILE → unpredictable, HOLD
   - regime_tradeable = False → REJECT with the regime_reason
5. Do the live market features support the signal? Consider:
   - fisher_value: Ehlers Fisher Transform — symbol-agnostic exhaustion score
       BUY signal: fisher_value should be negative (< -1.0 = strong, < -1.5 = very strong)
       SELL signal: fisher_value should be positive (> +1.0 = strong, > +1.5 = very strong)
       fisher_value crossing fisher_signal adds timing confirmation
   - kf_velocity: Kalman trend velocity — should point toward the trade direction
       BUY: negative velocity (downtrend exhausting) is bullish for mean reversion
       SELL: positive velocity (uptrend exhausting) is bearish for mean reversion
   - vol_ratio > 1.2 = short-term vol spike (momentum may be exhausted, good for mean reversion)
   - volume_zscore > 1.5 = abnormally high volume (confirms exhaustion signal)
   - spread_pct > 0.05 = wide spread (execution slippage risk, be cautious)
   - option_cost = premium × 100 (total $ for 1 contract). REJECT if option_cost > buying_power × 0.6
     (never risk more than 60% of capital on a single option)
   - buying_power < 300 = insufficient capital, REJECT (if null/unknown, do NOT reject on this basis)
   - iv > 1.5 (150% implied volatility) = dangerously expensive premium, be cautious
   - delta should be 0.4–0.6 for ATM (if much lower the option is too OTM)
5. Any obvious reason NOT to trade?

Respond with EXACTLY one of:
APPROVE
REJECT - one sentence reason
HOLD - one sentence reason (temporary issues only)

Never invent trades. Never change signal direction.
"""


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Try reading from credentials file next to config
        cred_path = os.path.expanduser(r"~\.claude\.credentials.json")
        try:
            import json as _json
            with open(cred_path) as f:
                creds = _json.load(f)
            api_key = creds.get("claudeAiApiKey") or creds.get("apiKey") or creds.get("api_key")
        except Exception:
            pass
    if not api_key:
        raise RuntimeError(
            "Anthropic API key not found. Set ANTHROPIC_API_KEY environment variable:\n"
            "  $env:ANTHROPIC_API_KEY = 'sk-ant-...'"
        )
    return anthropic.Anthropic(api_key=api_key)


def validate_signal(signal: dict, live_features: dict | None = None) -> tuple[str, str]:
    client = _client()

    payload = {"signal": signal}
    if live_features:
        payload["live_market_features"] = live_features

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=DECISION_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Validate this signal:\n{json.dumps(payload, indent=2)}"
            }
        ]
    )
    
    result = response.content[0].text.strip().replace("*", "").replace("#", "").strip()

    if "APPROVE" in result:
        return "APPROVE", ""
    elif result.startswith("REJECT"):
        return "REJECT", result[6:].strip(" :-")
    elif result.startswith("HOLD"):
        return "HOLD", result[4:].strip(" :-")
    else:
        return "REJECT", f"Unexpected AI response: {result}"

EXIT_PROMPT = """
You are the Exit Engine for Project Alpha, an autonomous SPY trading system using an EXHAUSTION / MEAN REVERSION strategy.

Entry rules (for reference):
- BUY entered when qtrend=RED, vivek=RED, channels converging → expect bounce up
- SELL entered when qtrend=GREEN, vivek=GREEN, channels converging → expect drop down

Exit rules — CLOSE the position when the OPPOSITE exhaustion signal appears:
- Close a BUY when qtrend=GREEN, vivek=GREEN, channels converging (bullish exhaustion = top)
- Close a SELL when qtrend=RED, vivek=RED, channels converging (bearish exhaustion = bottom)

Also consider closing early (CLOSE with reason) if:
- Channels are diverging significantly (momentum fading)
- Confidence dropped below 60
- vol_ratio < 0.8 (volatility collapsing — mean reversion already happened)
- volume_zscore < -1 (volume drying up — no conviction, move may be done)
- spread_pct > 0.1 (liquidity deteriorating)
- kf_velocity has flipped direction against the position (Kalman trend now fighting the trade)
- abs(kf_velocity) is very large (strong Kalman-detected momentum against the position)
- Market conditions suggest the thesis is broken (e.g. strong trend continuation against the position)

Respond with EXACTLY one of:
HOLD - one sentence reason (position still valid, keep it)
CLOSE - one sentence reason (exit now)
"""


def evaluate_exit(position: dict, current_signal: dict, live_features: dict | None = None) -> tuple[str, str]:
    """Ask Claude whether to hold or close the open position."""
    client = _client()

    context = {
        "open_position": {
            "symbol": position["symbol"],
            "side": position["side"],
            "entry_price": position.get("entry_price"),
            "entry_time": position.get("entry_time"),
        },
        "current_signal": current_signal,
    }
    if live_features:
        context["live_market_features"] = live_features

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=EXIT_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Evaluate exit for this position:\n{json.dumps(context, indent=2)}"
            }
        ]
    )

    result = response.content[0].text.strip().replace("*", "").replace("#", "").strip()
    if "CLOSE" in result:
        return "CLOSE", result[result.index("CLOSE") + 5:].strip(" :-")
    else:
        return "HOLD", result[result.index("HOLD") + 4:].strip(" :-") if "HOLD" in result else result


if __name__ == "__main__":
    print("Validating signal...")
    decision, reason = validate_signal(SPY_SIGNAL)
    print(f"Decision: {decision}")
    if reason:
        print(f"Reason: {reason}")