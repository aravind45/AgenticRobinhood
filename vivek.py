# vivek.py
# Python reimplementation of "Vivek + RSI Filter" Pine Script indicator.
# Computes state directly from OHLCV bars (from Robinhood MCP).
# Parameters match Pine Script defaults exactly.

from __future__ import annotations
from dataclasses import dataclass

# ─── Parameters (mirror Pine Script defaults) ────────────────────────────────

EMA_FAST1      = 10
EMA_FAST2      = 20
TREND_SMA_LEN  = 40
ATR_MULT       = 0.618
LONG_TREND_LEN = 200
MR_RSI_LEN     = 2
MR_OVERSOLD    = 10
MR_OVERBOUGHT  = 90
DIV_LEFT       = 3
DIV_RIGHT      = 3
STRONG_LOOKBACK = 8


# ─── Output ──────────────────────────────────────────────────────────────────

@dataclass
class VivekState:
    state: int          # 1 = long, -1 = short, 0 = neutral
    dir_trend: int      # 1 = bullish, -1 = bearish, 0 = in-range
    long_entry: bool
    short_entry: bool
    long_exit: bool
    short_exit: bool
    strong_long: bool
    strong_short: bool
    ema1: float
    ema2: float
    trend_sma: float
    ch_top: float
    ch_bot: float
    mr_rsi: float
    long_trend_ma: float


# ─── Math helpers ─────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float | None]:
    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(values)
    started = False
    for i, v in enumerate(values):
        if v is None:
            continue
        if not started:
            out[i] = v
            started = True
        else:
            prev = next((out[j] for j in range(i - 1, -1, -1) if out[j] is not None), None)
            if prev is not None:
                out[i] = v * k + prev * (1 - k)
            else:
                out[i] = v
    return out


def _sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        if all(v is not None for v in window):
            out[i] = sum(window) / period
    return out


def _rma(values: list[float | None], period: int) -> list[float | None]:
    """Wilder's smoothing — used by ATR and RSI in Pine Script."""
    alpha = 1 / period
    out: list[float | None] = [None] * len(values)
    seed = None
    seed_count = 0
    seed_sum = 0.0
    for i, v in enumerate(values):
        if v is None:
            continue
        if seed is None:
            seed_sum += v
            seed_count += 1
            if seed_count == period:
                seed = seed_sum / period
                out[i] = seed
        else:
            seed = alpha * v + (1 - alpha) * seed
            out[i] = seed
    return out


def _atr(highs, lows, closes, period: int) -> list[float | None]:
    tr: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if None not in (h, l, pc):
            tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    return _rma(tr, period)


def _rsi(closes: list[float | None], period: int) -> list[float | None]:
    gains: list[float | None] = [None] * len(closes)
    losses: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] is not None and closes[i - 1] is not None:
            d = closes[i] - closes[i - 1]
            gains[i]  = max(d, 0.0)
            losses[i] = max(-d, 0.0)
    avg_g = _rma(gains, period)
    avg_l = _rma(losses, period)
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        ag, al = avg_g[i], avg_l[i]
        if ag is not None and al is not None:
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def _pivot_lows(lows: list[float], left: int, right: int) -> list[float | None]:
    """Returns the low value at each confirmed pivot low bar, else None."""
    n = len(lows)
    out: list[float | None] = [None] * n
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if all(v is not None for v in window) and lows[i] == min(window):
            out[i] = lows[i]
    return out


def _pivot_highs(highs: list[float], left: int, right: int) -> list[float | None]:
    n = len(highs)
    out: list[float | None] = [None] * n
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if all(v is not None for v in window) and highs[i] == max(window):
            out[i] = highs[i]
    return out


def _value_when(condition: list[bool], series: list[float | None],
                occurrence: int = 0) -> list[float | None]:
    """Mimics ta.valuewhen: returns the series value at the Nth most recent True."""
    out: list[float | None] = [None] * len(condition)
    history: list[float | None] = []
    for i, c in enumerate(condition):
        if c and series[i] is not None:
            history.append(series[i])
        if len(history) > occurrence:
            out[i] = history[-1 - occurrence]
    return out


def _bars_since(condition: list[bool]) -> list[int | None]:
    out: list[int | None] = [None] * len(condition)
    last = None
    for i, c in enumerate(condition):
        if c:
            last = i
        out[i] = (i - last) if last is not None else None
    return out


def _rolling_min(series: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(series)
    for i in range(window - 1, len(series)):
        chunk = [v for v in series[i - window + 1: i + 1] if v is not None]
        out[i] = min(chunk) if chunk else None
    return out


def _rolling_max(series: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(series)
    for i in range(window - 1, len(series)):
        chunk = [v for v in series[i - window + 1: i + 1] if v is not None]
        out[i] = max(chunk) if chunk else None
    return out


# ─── Main computation ─────────────────────────────────────────────────────────

def compute(bars: list[dict]) -> VivekState:
    """
    bars: list of dicts with keys open, high, low, close, volume (floats).
          Must be in chronological order (oldest first).
          Needs at least 210 bars for reliable SMA(200); 60+ for core signals.
    Returns the VivekState for the LAST bar.
    """
    opens  = [float(b["open"])   for b in bars]
    highs  = [float(b["high"])   for b in bars]
    lows   = [float(b["low"])    for b in bars]
    closes = [float(b["close"])  for b in bars]

    n = len(closes)

    # ── EMAs / SMAs ───────────────────────────────────────────────────────────
    ema1      = _ema(closes, EMA_FAST1)
    ema2      = _ema(closes, EMA_FAST2)
    trend_sma = _sma(closes, TREND_SMA_LEN)
    long_ma   = _sma(closes, LONG_TREND_LEN)
    atr_vals  = _atr(highs, lows, closes, TREND_SMA_LEN)

    ch_top = [
        (trend_sma[i] + atr_vals[i] * ATR_MULT)
        if trend_sma[i] is not None and atr_vals[i] is not None else None
        for i in range(n)
    ]
    ch_bot = [
        (trend_sma[i] - atr_vals[i] * ATR_MULT)
        if trend_sma[i] is not None and atr_vals[i] is not None else None
        for i in range(n)
    ]

    # ── Dir trend ─────────────────────────────────────────────────────────────
    in_range = [
        (ch_top[i] is not None and ch_bot[i] is not None and
         min(opens[i], closes[i]) >= ch_bot[i] and
         max(opens[i], closes[i]) <= ch_top[i])
        for i in range(n)
    ]
    dir_trend = [
        0 if in_range[i] else (1 if closes[i] >= trend_sma[i] else -1)
        if trend_sma[i] is not None else 0
        for i in range(n)
    ]

    # ── RSI(2) ────────────────────────────────────────────────────────────────
    mr_rsi = _rsi(closes, MR_RSI_LEN)

    # ── Pivot divergence ──────────────────────────────────────────────────────
    piv_lows  = _pivot_lows(lows,   DIV_LEFT, DIV_RIGHT)
    piv_highs = _pivot_highs(highs, DIV_LEFT, DIV_RIGHT)

    # RSI sampled at pivot bar (divRight bars before "now" when pivot is confirmed)
    rsi_at_piv_low  = [mr_rsi[i] if piv_lows[i]  is not None else None for i in range(n)]
    rsi_at_piv_high = [mr_rsi[i] if piv_highs[i] is not None else None for i in range(n)]

    piv_low_cond  = [piv_lows[i]  is not None for i in range(n)]
    piv_high_cond = [piv_highs[i] is not None for i in range(n)]

    prev_piv_low       = _value_when(piv_low_cond,  [piv_lows[i]  for i in range(n)], 1)
    prev_piv_high      = _value_when(piv_high_cond, [piv_highs[i] for i in range(n)], 1)
    prev_rsi_piv_low   = _value_when(piv_low_cond,  rsi_at_piv_low,  1)
    prev_rsi_piv_high  = _value_when(piv_high_cond, rsi_at_piv_high, 1)

    bull_div = [
        (piv_lows[i] is not None and
         prev_piv_low[i] is not None and
         rsi_at_piv_low[i] is not None and
         prev_rsi_piv_low[i] is not None and
         piv_lows[i] < prev_piv_low[i] and
         rsi_at_piv_low[i] > prev_rsi_piv_low[i])
        for i in range(n)
    ]
    bear_div = [
        (piv_highs[i] is not None and
         prev_piv_high[i] is not None and
         rsi_at_piv_high[i] is not None and
         prev_rsi_piv_high[i] is not None and
         piv_highs[i] > prev_piv_high[i] and
         rsi_at_piv_high[i] < prev_rsi_piv_high[i])
        for i in range(n)
    ]

    bs_bull = _bars_since(bull_div)
    bs_bear = _bars_since(bear_div)

    bull_div_recent = [
        (bs_bull[i] is not None and bs_bull[i] <= STRONG_LOOKBACK)
        for i in range(n)
    ]
    bear_div_recent = [
        (bs_bear[i] is not None and bs_bear[i] <= STRONG_LOOKBACK)
        for i in range(n)
    ]

    rsi_min = _rolling_min(mr_rsi, STRONG_LOOKBACK + 1)
    rsi_max = _rolling_max(mr_rsi, STRONG_LOOKBACK + 1)

    oversold_recent   = [rsi_min[i] is not None and rsi_min[i] <= MR_OVERSOLD  for i in range(n)]
    overbought_recent = [rsi_max[i] is not None and rsi_max[i] >= MR_OVERBOUGHT for i in range(n)]

    strong_long_ready  = [
        (long_ma[i] is not None and closes[i] > long_ma[i] and
         bull_div_recent[i] and oversold_recent[i])
        for i in range(n)
    ]
    strong_short_ready = [
        (long_ma[i] is not None and closes[i] < long_ma[i] and
         bear_div_recent[i] and overbought_recent[i])
        for i in range(n)
    ]

    # ── State machine ─────────────────────────────────────────────────────────
    long_setup  = [dir_trend[i] == 1  and ema1[i] is not None and ema2[i] is not None and ema1[i] > ema2[i] for i in range(n)]
    short_setup = [dir_trend[i] == -1 and ema1[i] is not None and ema2[i] is not None and ema1[i] < ema2[i] for i in range(n)]
    long_exit_setup  = [dir_trend[i] == 1  and ema1[i] is not None and ema2[i] is not None and ema1[i] < ema2[i] for i in range(n)]
    short_exit_setup = [dir_trend[i] == -1 and ema1[i] is not None and ema2[i] is not None and ema1[i] > ema2[i] for i in range(n)]

    states = [0] * n
    for i in range(1, n):
        prev = states[i - 1]
        if prev != 1 and long_setup[i]:
            states[i] = 1
        elif prev != -1 and short_setup[i]:
            states[i] = -1
        elif prev != 0 and (long_exit_setup[i] or short_exit_setup[i] or dir_trend[i] == 0):
            states[i] = 0
        else:
            states[i] = prev

    long_entry  = [states[i] == 1  and (states[i - 1] != 1  if i > 0 else False) for i in range(n)]
    short_entry = [states[i] == -1 and (states[i - 1] != -1 if i > 0 else False) for i in range(n)]
    long_exit   = [states[i] == 0  and (states[i - 1] == 1  if i > 0 else False) for i in range(n)]
    short_exit  = [states[i] == 0  and (states[i - 1] == -1 if i > 0 else False) for i in range(n)]

    strong_long_signal  = [strong_long_ready[i]  and (not strong_long_ready[i - 1]  if i > 0 else False) for i in range(n)]
    strong_short_signal = [strong_short_ready[i] and (not strong_short_ready[i - 1] if i > 0 else False) for i in range(n)]

    # ── Return last bar ───────────────────────────────────────────────────────
    i = n - 1
    return VivekState(
        state        = states[i],
        dir_trend    = dir_trend[i],
        long_entry   = long_entry[i],
        short_entry  = short_entry[i],
        long_exit    = long_exit[i],
        short_exit   = short_exit[i],
        strong_long  = strong_long_signal[i],
        strong_short = strong_short_signal[i],
        ema1         = ema1[i] or 0.0,
        ema2         = ema2[i] or 0.0,
        trend_sma    = trend_sma[i] or 0.0,
        ch_top       = ch_top[i] or 0.0,
        ch_bot       = ch_bot[i] or 0.0,
        mr_rsi       = mr_rsi[i] or 0.0,
        long_trend_ma = long_ma[i] or 0.0,
    )


if __name__ == "__main__":
    # Quick sanity check using RH live data
    import json, requests
    from datetime import datetime, timedelta, timezone
    from config import get_robinhood_token, MCP_URL

    token = get_robinhood_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=400)   # enough for SMA(200)

    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_equity_historicals", "arguments": {
            "symbols": ["SPY"],
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval": "day", "bounds": "regular",
        }}
    }, headers=headers)

    data  = json.loads(r.text.split("data: ", 1)[1])
    raw   = data["result"]["content"][0]["text"]
    hist  = json.loads(raw)
    results = hist.get("data", {}).get("results", [])
    symbol_obj = next((d for d in results if d.get("symbol") == "SPY"), results[0] if results else {})
    bars_raw = symbol_obj.get("bars", [])

    bars = [{"open":   float(b["open_price"]),
             "high":   float(b["high_price"]),
             "low":    float(b["low_price"]),
             "close":  float(b["close_price"]),
             "volume": float(b["volume"])} for b in bars_raw]

    print(f"Bars loaded: {len(bars)}")
    vs = compute(bars)
    print(f"\nVivek State on last bar:")
    print(f"  state        : {vs.state}  (1=long, -1=short, 0=neutral)")
    print(f"  dir_trend    : {vs.dir_trend}")
    print(f"  long_entry   : {vs.long_entry}")
    print(f"  short_entry  : {vs.short_entry}")
    print(f"  long_exit    : {vs.long_exit}")
    print(f"  short_exit   : {vs.short_exit}")
    print(f"  strong_long  : {vs.strong_long}")
    print(f"  strong_short : {vs.strong_short}")
    print(f"  EMA10/20     : {vs.ema1:.2f} / {vs.ema2:.2f}")
    print(f"  Trend SMA40  : {vs.trend_sma:.2f}")
    print(f"  Channel      : {vs.ch_bot:.2f} – {vs.ch_top:.2f}")
    print(f"  RSI(2)       : {vs.mr_rsi:.2f}")
    print(f"  SMA(200)     : {vs.long_trend_ma:.2f}")
