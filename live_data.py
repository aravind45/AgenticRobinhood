# live_data.py
# Pulls live market data from Robinhood MCP and computes stationary features
# for Claude's entry/exit decisions.

import json
import math
import requests
from datetime import datetime, timedelta, timezone
from config import get_robinhood_token, ACCOUNT_NUMBER, MCP_URL
import vivek as vivek_ind
import qtrend as qtrend_ind

def _call(token: str, tool: str, args: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        MCP_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers=headers,
    )
    data = json.loads(r.text.split("data: ", 1)[1])
    result = data.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"{tool} error: {result['content'][0]['text']}")
    return json.loads(result["content"][0]["text"])


def _rolling_std(values: list[float], window: int) -> list[float | None]:
    out = [None] * len(values)
    for i in range(window - 1, len(values)):
        chunk = values[i - window + 1: i + 1]
        mean = sum(chunk) / window
        variance = sum((x - mean) ** 2 for x in chunk) / window
        out[i] = math.sqrt(variance)
    return out


def _fisher_transform(closes: list[float], period: int = 20) -> tuple[float, float]:
    """
    Ehlers Fisher Transform — maps price into a Gaussian normal distribution.

    Instead of asking "is RSI < 10?", Fisher asks "is this a statistically
    extreme reading?" The output is symbol-agnostic: ±1.5 means the same
    thing on SPY, NVDA, AMD etc., making confidence scores comparable across
    the whole watchlist.

    Returns:
        fisher_value  — current bar's value (negative = oversold, positive = overbought)
        fisher_signal — previous bar's value (crossover = timing trigger)

    Typical ranges: ±0.5 = normal, ±1.0 = notable, ±2.0+ = rare extreme
    """
    if len(closes) < period + 2:
        return 0.0, 0.0

    value_prev  = 0.0
    fisher_prev = 0.0
    fisher_curr = 0.0

    for i in range(period, len(closes)):
        window  = closes[i - period: i + 1]
        highest = max(window)
        lowest  = min(window)
        rng     = highest - lowest

        raw   = (2 * ((closes[i] - lowest) / rng) - 1) if rng > 0 else 0.0
        value = 0.33 * raw + 0.67 * value_prev          # Ehlers' smoothing step
        value = max(-0.999, min(0.999, value))           # clamp — avoids log(0)

        fisher_prev = fisher_curr
        fisher_curr = (0.5 * math.log((1 + value) / (1 - value))
                       + 0.5 * fisher_prev)              # recursive smoothing
        value_prev  = value

    return round(fisher_curr, 4), round(fisher_prev, 4)


def _kalman_filter(closes: list[float], returns_daily: list[float]) -> tuple[float, float]:
    """
    Adaptive Kalman Filter — tracks price level and velocity (trend direction + speed).

    State vector: [level, velocity]
    Observation: close price (level only)

    Process noise Q scales with realized volatility so the filter adapts fast
    in high-vol regimes and stays smooth in quiet markets.
    """
    if len(closes) < 10:
        return closes[-1], 0.0

    # Adaptive Q based on last-20-day realized vol
    realized_vol = 0.0
    window = returns_daily[-20:] if len(returns_daily) >= 20 else returns_daily
    if window:
        mean_r = sum(window) / len(window)
        realized_vol = math.sqrt(sum((r - mean_r) ** 2 for r in window) / len(window))

    q_scale = max(0.001, realized_vol * closes[-1])  # dollar vol
    Q = [[q_scale * 0.5, 0.0],
         [0.0,            q_scale * 0.1]]  # velocity noise is smaller

    R = (closes[-1] * 0.002) ** 2  # observation noise ~0.2% of price

    # State and covariance — warm-start from first close
    level = closes[0]
    velocity = 0.0
    P = [[1.0, 0.0],
         [0.0, 0.1]]

    for i in range(1, len(closes)):
        # Predict: state = F * state
        level_pred    = level + velocity
        velocity_pred = velocity
        # Predict covariance: P = F P F^T + Q
        P00 = P[0][0] + P[1][0] + P[0][1] + P[1][1] + Q[0][0]
        P01 = P[0][1] + P[1][1] + Q[0][1]
        P10 = P[1][0] + P[1][1] + Q[1][0]
        P11 = P[1][1] + Q[1][1]

        # Kalman gain: K = P H^T / (H P H^T + R)  where H=[1,0]
        S = P00 + R
        K0 = P00 / S
        K1 = P10 / S

        # Update
        innov = closes[i] - level_pred
        level    = level_pred    + K0 * innov
        velocity = velocity_pred + K1 * innov

        # Update covariance
        P[0][0] = P00 - K0 * P00
        P[0][1] = P01 - K0 * P01
        P[1][0] = P10 - K1 * P00
        P[1][1] = P11 - K1 * P10

    return round(level, 4), round(velocity, 6)


def get_live_features(symbol: str = "SPY") -> dict:
    """
    Fetches 450 calendar days of OHLCV bars + a live quote.
    Computes Vivek + QTrend indicator states AND stationary market features.
    Returns a flat dict ready to inject into the agent prompt.
    """
    token = get_robinhood_token()

    # ── 1. Historical bars (450 calendar days — enough for SMA(200)) ────────
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=450)
    hist = _call(token, "get_equity_historicals", {
        "symbols": [symbol],
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval":   "day",
        "bounds":     "regular",
    })

    # Shape: {"data": {"results": [{"symbol": "SPY", "bars": [...]}]}, "guide": ...}
    results = hist.get("data", {}).get("results", [])
    symbol_obj = next((d for d in results if d.get("symbol") == symbol), results[0] if results else {})
    bars = symbol_obj.get("bars", [])
    if not bars:
        raise RuntimeError(f"No historical bars returned for {symbol}")
    print(f"  Bar count: {len(bars)}")

    # ── 2. Compute Vivek + QTrend indicators ────────────────────────────────
    ohlcv = [{"open":   float(b["open_price"]),
              "high":   float(b["high_price"]),
              "low":    float(b["low_price"]),
              "close":  float(b["close_price"]),
              "volume": float(b["volume"])} for b in bars]

    vs = vivek_ind.compute(ohlcv)
    qs = qtrend_ind.compute(ohlcv)

    closes  = [b["close"]  for b in ohlcv]
    volumes = [b["volume"] for b in ohlcv]
    highs   = [b["high"]   for b in ohlcv]
    lows    = [b["low"]    for b in ohlcv]

    # ── 2. Stationary features ─────────────────────────────────────────────
    def ret(n): return (closes[-1] - closes[-1 - n]) / closes[-1 - n]

    returns_daily = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
    ]

    stds5  = _rolling_std(returns_daily, 5)
    stds20 = _rolling_std(returns_daily, 20)

    vol_5  = stds5[-1]
    vol_20 = stds20[-1]

    # Volume z-score (20-day)
    vol_window = volumes[-20:]
    vol_mean = sum(vol_window) / len(vol_window)
    vol_std  = math.sqrt(sum((v - vol_mean) ** 2 for v in vol_window) / len(vol_window))
    vol_zscore = (volumes[-1] - vol_mean) / vol_std if vol_std else 0

    # Normalised range
    recent_ranges = [
        (highs[i] - lows[i]) / closes[i] for i in range(-20, 0)
    ]
    avg_range = sum(recent_ranges) / len(recent_ranges)
    today_range = (highs[-1] - lows[-1]) / closes[-1]
    range_norm = today_range / avg_range if avg_range else 1

    # SMA ratio
    sma5  = sum(closes[-5:])  / 5
    sma20 = sum(closes[-20:]) / 20

    # ── 2b. Fisher Transform + Kalman Filter ─────────────────────────────
    fisher_value, fisher_signal = _fisher_transform(closes, period=20)
    kf_level, kf_velocity = _kalman_filter(closes, returns_daily)

    # ── 3. Live quote ──────────────────────────────────────────────────────
    quote_resp = _call(token, "get_equity_quotes", {"symbols": [symbol]})
    quotes = quote_resp.get("data", {}).get(symbol) or quote_resp.get(symbol, {})
    if isinstance(quotes, list):
        quotes = quotes[0]

    last_price = float(quotes.get("last_trade_price") or closes[-1])
    bid        = float(quotes.get("bid_price", 0))
    ask        = float(quotes.get("ask_price", 0))
    spread_pct = (ask - bid) / last_price * 100 if last_price else 0

    # ── 4. Buying power from get_accounts ─────────────────────────────────
    try:
        acct_resp = _call(token, "get_accounts", {})
        # Shape: {"data": {"results": [{"buying_power": "1000.00", ...}]}}
        acct_data = acct_resp.get("data") or acct_resp
        accounts = (acct_data.get("results") if isinstance(acct_data, dict) else None) or []
        if not accounts and isinstance(acct_data, list):
            accounts = acct_data
        acct = next((a for a in accounts if str(a.get("account_number")) == ACCOUNT_NUMBER), accounts[0] if accounts else {})
        bp_raw = acct.get("buying_power") or acct.get("cash") or acct.get("portfolio_cash") or None
        buying_power = float(bp_raw) if bp_raw is not None else None
    except Exception as e:
        buying_power = None

    # ── 5. Open position (if any) ──────────────────────────────────────────
    try:
        pos_resp = _call(token, "get_equity_positions", {"account_number": ACCOUNT_NUMBER})
        pdata = pos_resp.get("data") or pos_resp
        positions = (pdata.get("results") if isinstance(pdata, dict) else []) or []
        open_position = next(
            (p for p in positions if p.get("symbol") == symbol and float(p.get("quantity", 0)) > 0),
            None
        )
    except Exception:
        open_position = None

    return {
        # ── Vivek indicator ──────────────────────────────────────────────
        "vivek_state":        vs.state,        # 1=long, -1=short, 0=neutral
        "vivek_dir_trend":    vs.dir_trend,    # 1=bull, -1=bear, 0=range
        "vivek_long_entry":   vs.long_entry,
        "vivek_short_entry":  vs.short_entry,
        "vivek_long_exit":    vs.long_exit,
        "vivek_short_exit":   vs.short_exit,
        "vivek_strong_long":  vs.strong_long,
        "vivek_strong_short": vs.strong_short,
        "vivek_ema10":        round(vs.ema1, 4),
        "vivek_ema20":        round(vs.ema2, 4),
        "vivek_trend_sma40":  round(vs.trend_sma, 4),
        "vivek_ch_top":       round(vs.ch_top, 4),
        "vivek_ch_bot":       round(vs.ch_bot, 4),
        "vivek_rsi2":         round(vs.mr_rsi, 4),
        "vivek_sma200":       round(vs.long_trend_ma, 4),

        # ── Q-Trend indicator ────────────────────────────────────────────
        "qtrend_state":       qs.state,        # 1=BUY trend, -1=SELL trend
        "qtrend_last_signal": qs.last_signal,  # "B" or "S"
        "qtrend_change_up":   qs.change_up,
        "qtrend_change_down": qs.change_down,
        "qtrend_strong_buy":  qs.strong_buy,
        "qtrend_strong_sell": qs.strong_sell,
        "qtrend_trend_line":  round(qs.trend_line, 4),

        # ── Price ────────────────────────────────────────────────────────
        "symbol":           symbol,
        "last_price":       round(last_price, 4),
        "bid":              round(bid, 4),
        "ask":              round(ask, 4),
        "spread_pct":       round(spread_pct, 4),

        # Returns
        "return_1d":        round(ret(1),  6),
        "return_5d":        round(ret(5),  6),
        "return_20d":       round(ret(20), 6),

        # Volatility
        "vol_5d":           round(vol_5,  6) if vol_5  else None,
        "vol_20d":          round(vol_20, 6) if vol_20 else None,
        "vol_ratio":        round(vol_5 / vol_20, 4) if vol_5 and vol_20 else None,
        "momentum_norm":    round(ret(1) / vol_20, 4) if vol_20 else None,

        # Volume
        "volume_today":     int(volumes[-1]),
        "volume_zscore":    round(vol_zscore, 4),

        # Structure
        "range_norm":       round(range_norm, 4),
        "sma5_vs_sma20":    round((sma5 / sma20 - 1) * 100, 4),  # % above/below

        # Fisher Transform (Ehlers)
        "fisher_value":     fisher_value,  # current: neg=oversold, pos=overbought; ±1.5+ = extreme
        "fisher_signal":    fisher_signal, # previous bar — crossover = timing trigger

        # Kalman Filter
        "kf_level":         kf_level,      # Kalman-smoothed price level
        "kf_velocity":      kf_velocity,   # trend velocity ($/day); positive = upward drift

        # Account
        "buying_power":     round(buying_power, 2) if buying_power is not None else None,
        "open_position":    open_position,
    }


if __name__ == "__main__":
    import pprint
    print("Fetching live market features for SPY...\n")
    features = get_live_features("SPY")
    pprint.pprint(features)
