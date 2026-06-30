# qtrend.py
# Python reimplementation of "Q-Trend refined" Pine Script indicator.
# Computes state directly from OHLCV bars (from Robinhood MCP).
# Parameters match Pine Script defaults exactly.

from __future__ import annotations
from dataclasses import dataclass

# ─── Parameters (mirror Pine Script defaults) ────────────────────────────────

TREND_PERIOD  = 200   # p: highest/lowest lookback
ATR_PERIOD    = 14    # atr_p
ATR_MULT      = 1.0   # mult (epsilon sensitivity)
MODE          = "A"   # "A" = crossover/crossunder only, "B" = cross (either direction)
STRONG_WINDOW = 5     # Pine: sb or sb[1]..sb[4]  → look back 4 bars = window of 5


# ─── Output ──────────────────────────────────────────────────────────────────

@dataclass
class QTrendState:
    state: int            #  1 = BUY trend, -1 = SELL trend, 0 = undefined
    change_up: bool       # new BUY signal this bar
    change_down: bool     # new SELL signal this bar
    strong_buy: bool      # BUY signal near range extreme bottom
    strong_sell: bool     # SELL signal near range extreme top
    trend_line: float     # current value of m
    last_signal: str      # "B" or "S"


# ─── Math helpers ─────────────────────────────────────────────────────────────

def _atr(highs, lows, closes, period: int) -> list[float | None]:
    """True Range then Wilder RMA — Pine uses atr[1] (previous bar)."""
    tr: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if None not in (h, l, pc):
            tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    # Wilder smoothing
    alpha = 1 / period
    out: list[float | None] = [None] * len(tr)
    seed, count, total = None, 0, 0.0
    for i, v in enumerate(tr):
        if v is None:
            continue
        if seed is None:
            total += v
            count += 1
            if count == period:
                seed = total / period
                out[i] = seed
        else:
            seed = alpha * v + (1 - alpha) * seed
            out[i] = seed
    return out


def _highest(series: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(series)
    for i in range(period - 1, len(series)):
        out[i] = max(series[i - period + 1: i + 1])
    return out


def _lowest(series: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(series)
    for i in range(period - 1, len(series)):
        out[i] = min(series[i - period + 1: i + 1])
    return out


# ─── Main computation ─────────────────────────────────────────────────────────

def compute(bars: list[dict]) -> QTrendState:
    """
    bars: list of dicts with keys open, high, low, close (floats), oldest first.
          Needs at least TREND_PERIOD + ATR_PERIOD bars (~215 daily bars).
    Returns QTrendState for the LAST bar.
    """
    opens  = [float(b["open"])  for b in bars]
    highs  = [float(b["high"])  for b in bars]
    lows   = [float(b["low"])   for b in bars]
    closes = [float(b["close"]) for b in bars]
    n = len(closes)

    src = closes  # input source = close (default)

    # ── Range ─────────────────────────────────────────────────────────────────
    h_arr = _highest(src, TREND_PERIOD)   # rolling highest
    l_arr = _lowest(src,  TREND_PERIOD)   # rolling lowest

    # ── ATR (shifted 1 bar as in Pine: atr[1]) ────────────────────────────────
    atr_raw = _atr(highs, lows, closes, ATR_PERIOD)
    # atr[1] in Pine = previous bar's ATR
    atr_shifted: list[float | None] = [None] + list(atr_raw[:-1])
    epsilon = [ATR_MULT * v if v is not None else None for v in atr_shifted]

    # ── Trend line (m) ────────────────────────────────────────────────────────
    # Initial value: m = (h + l) / 2  for bar_index <= p, then persists
    m: list[float | None] = [None] * n
    for i in range(n):
        h = h_arr[i]
        l = l_arr[i]
        if h is None or l is None:
            m[i] = m[i - 1] if i > 0 else None
        elif i <= TREND_PERIOD:
            m[i] = (h + l) / 2
        else:
            m[i] = m[i - 1]  # will update below with signals

    # ── Signal computation ────────────────────────────────────────────────────
    ls: list[str] = [""] * n        # last signal: "B" or "S"
    change_up   = [False] * n
    change_down = [False] * n
    strong_buy  = [False] * n
    strong_sell = [False] * n

    for i in range(1, n):
        if m[i] is None or epsilon[i] is None or h_arr[i] is None or l_arr[i] is None:
            ls[i] = ls[i - 1]
            continue

        s   = src[i]
        m_i = m[i]
        eps = epsilon[i]

        if MODE == "A":
            cu = s > m_i + eps and src[i - 1] <= (m[i - 1] or m_i) + eps
            cd = s < m_i - eps and src[i - 1] >= (m[i - 1] or m_i) - eps
        else:  # Type B: cross = crossover OR crossunder (either direction through band)
            cu = (s > m_i + eps) != (src[i - 1] > (m[i - 1] or m_i) + eps)
            cd = (s < m_i - eps) != (src[i - 1] < (m[i - 1] or m_i) - eps)
        # Also true if already past band (Pine: "or src > m + epsilon")
        cu = cu or s > m_i + eps
        cd = cd or s < m_i - eps

        # Update trend line
        if cu or cd:
            if m_i != m[i - 1]:
                pass  # already updated
            elif cu:
                m[i] = m_i + eps
            elif cd:
                m[i] = m_i - eps
        else:
            m[i] = m[i - 1]

        # Recompute with updated m
        m_now = m[i]
        if MODE == "A":
            cu = s > m_now + eps and src[i - 1] <= (m[i - 1] or m_now) + eps
            cd = s < m_now - eps and src[i - 1] >= (m[i - 1] or m_now) - eps
        cu = cu or s > m_now + eps
        cd = cd or s < m_now - eps

        change_up[i]   = cu
        change_down[i] = cd

        ls[i] = "B" if cu else ("S" if cd else ls[i - 1])

        # Strong signals: open near bottom/top of range
        d = h_arr[i] - l_arr[i]
        sb_now = opens[i] < l_arr[i] + d / 8 and opens[i] >= l_arr[i]
        ss_now = opens[i] > h_arr[i] - d / 8 and opens[i] <= h_arr[i]

        # Look back STRONG_WINDOW-1 bars for sb/ss
        sb_any = sb_now or any(
            (opens[j] < (l_arr[j] or 0) + ((h_arr[j] or 0) - (l_arr[j] or 0)) / 8
             and opens[j] >= (l_arr[j] or 0))
            for j in range(max(0, i - (STRONG_WINDOW - 1)), i)
            if h_arr[j] is not None and l_arr[j] is not None
        )
        ss_any = ss_now or any(
            (opens[j] > (h_arr[j] or 0) - ((h_arr[j] or 0) - (l_arr[j] or 0)) / 8
             and opens[j] <= (h_arr[j] or 0))
            for j in range(max(0, i - (STRONG_WINDOW - 1)), i)
            if h_arr[j] is not None and l_arr[j] is not None
        )

        strong_buy[i]  = sb_any
        strong_sell[i] = ss_any

    # ── Return last bar ───────────────────────────────────────────────────────
    i = n - 1
    last_ls = ls[i]

    # Deduplicate: only emit change_up if previous ls was not already "B"
    effective_change_up   = change_up[i]   and ls[i - 1] != "B"
    effective_change_down = change_down[i] and ls[i - 1] != "S"

    state = 1 if last_ls == "B" else (-1 if last_ls == "S" else 0)

    return QTrendState(
        state        = state,
        change_up    = effective_change_up,
        change_down  = effective_change_down,
        strong_buy   = effective_change_up  and strong_buy[i],
        strong_sell  = effective_change_down and strong_sell[i],
        trend_line   = m[i] or 0.0,
        last_signal  = last_ls,
    )


if __name__ == "__main__":
    import json, requests
    from datetime import datetime, timedelta, timezone
    from config import get_robinhood_token, MCP_URL

    token = get_robinhood_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=450)

    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_equity_historicals", "arguments": {
            "symbols": ["SPY"],
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval": "day", "bounds": "regular",
        }}
    }, headers=headers)

    data     = json.loads(r.text.split("data: ", 1)[1])
    raw      = data["result"]["content"][0]["text"]
    hist     = json.loads(raw)
    results = hist.get("data", {}).get("results", [])
    symbol_obj = next((d for d in results if d.get("symbol") == "SPY"), results[0] if results else {})
    bars_raw = symbol_obj.get("bars", [])

    bars = [{"open":   float(b["open_price"]),
             "high":   float(b["high_price"]),
             "low":    float(b["low_price"]),
             "close":  float(b["close_price"])} for b in bars_raw]

    print(f"Bars loaded: {len(bars)}")
    qs = compute(bars)
    print(f"\nQ-Trend State on last bar:")
    print(f"  state        : {qs.state}  (1=BUY, -1=SELL, 0=undef)")
    print(f"  last_signal  : {qs.last_signal}")
    print(f"  change_up    : {qs.change_up}")
    print(f"  change_down  : {qs.change_down}")
    print(f"  strong_buy   : {qs.strong_buy}")
    print(f"  strong_sell  : {qs.strong_sell}")
    print(f"  trend_line   : {qs.trend_line:.4f}")
