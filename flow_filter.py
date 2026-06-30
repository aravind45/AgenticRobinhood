# flow_filter.py
# Options flow pre-filter for Project Alpha.
#
# Fetches ATM call and put volume from Robinhood MCP for each watchlist symbol.
# ATM options have the most meaningful flow — deep ITM/OTM volume is noise.
#
# C/P > 2.0 = unusual call buying = BULLISH flow  (boosts BUY candidate ranking)
# C/P < 0.5 = unusual put buying  = BEARISH flow  (boosts SELL candidate ranking)
#
# Fail-open: errors per symbol are caught — symbol still allowed through as UNKNOWN.

import json
import requests
from datetime import date, datetime
from config import get_robinhood_token, MCP_URL


def _call(token: str, tool: str, args: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args}
    }, headers=headers)
    data = json.loads(r.text.split("data: ", 1)[1])
    result = data.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result['content'][0]['text'][:200]}")
    return json.loads(result["content"][0]["text"])


def _sorted_expiries(expirations: list[str]) -> list[str]:
    """All future expirations sorted nearest-first."""
    today = date.today()
    future = sorted([
        datetime.strptime(d, "%Y-%m-%d").date()
        for d in expirations
        if datetime.strptime(d, "%Y-%m-%d").date() > today
    ])
    return [d.strftime("%Y-%m-%d") for d in future]


def _atm_strike(price: float, increment: float = 5.0) -> str:
    """Round price to nearest strike increment."""
    return str(float(round(price / increment) * increment))


def _score_symbol(token: str, symbol: str) -> dict:
    """
    Pull ATM call and put volume for the nearest weekly expiration.
    Uses ATM options only — they carry the most meaningful flow signal.
    """
    UNKNOWN = {"flow_signal": "UNKNOWN", "call_vol": 0, "put_vol": 0, "cp_ratio": 1.0}

    # ── 1. Get chain ID + expiration dates ────────────────────────────────
    chain_resp = _call(token, "get_option_chains", {"underlying_symbol": symbol})
    chains = chain_resp.get("data", {}).get("chains", [])
    if not chains:
        return UNKNOWN

    chain    = chains[0]
    chain_id = chain.get("id")
    expiries = _sorted_expiries(chain.get("expiration_dates", []))
    if not chain_id or not expiries:
        return UNKNOWN

    # ── 2. Get current equity price ───────────────────────────────────────
    quote_resp = _call(token, "get_equity_quotes", {"symbols": [symbol]})
    results = quote_resp.get("data", {}).get("results", [])
    q = (next((r for r in results), {}) or {}).get("quote", {})
    price = float(q.get("last_trade_price") or q.get("last_non_reg_trade_price") or 0)
    if price <= 0:
        return UNKNOWN

    # ── 3. ATM strikes — auto-detect increment ───────────────────────────
    def fetch_ids(opt_type: str, strike: str, exp: str) -> list[str]:
        resp = _call(token, "get_option_instruments", {
            "chain_id":         chain_id,
            "expiration_dates": exp,
            "type":             opt_type,
            "strike_price":     strike,
            "state":            "active",
            "tradability":      "tradable",
        })
        return [i["id"] for i in resp.get("data", {}).get("instruments", []) if i.get("id")]

    # Try nearest expiry first; if no ATM contracts found, try the next expiry.
    # Also try $5 increment before $1 — SPY/TSLA use $5, individual stocks use $5 or $1.
    call_ids, put_ids, atm, expiry = [], [], "0", expiries[0]
    for expiry_try in expiries[:3]:          # try up to 3 nearest expiries
        for inc in (5.0, 1.0):
            atm_f   = float(_atm_strike(price, inc))
            strikes = [f"{atm_f + i * inc:.4f}" for i in range(-2, 3)]
            c_ids, p_ids = [], []
            for strike in strikes:
                c_ids += fetch_ids("call", strike, expiry_try)
                p_ids += fetch_ids("put",  strike, expiry_try)
            if c_ids or p_ids:
                call_ids, put_ids = c_ids, p_ids
                atm, expiry = str(atm_f), expiry_try
                break
        if call_ids or put_ids:
            break

    if not call_ids and not put_ids:
        return {**UNKNOWN, "expiry": expiry, "price": price, "atm": atm}

    # ── 4. Get volume from quotes ─────────────────────────────────────────
    def fetch_volume(ids: list[str]) -> float:
        if not ids:
            return 0.0
        # get_option_quotes returns {"data": {"results": [{"quote": {...}, "close": {...}}]}}
        resp   = _call(token, "get_option_quotes", {"instrument_ids": ids[:20]})
        quotes = resp.get("data", {}).get("results", [])
        return sum(float(item.get("quote", {}).get("volume") or 0) for item in quotes)

    call_vol = fetch_volume(list(dict.fromkeys(call_ids)))   # deduplicate
    put_vol  = fetch_volume(list(dict.fromkeys(put_ids)))

    # ── 5. Call/put ratio → flow signal ───────────────────────────────────
    if put_vol > 0:
        cp_ratio = call_vol / put_vol
    elif call_vol > 0:
        cp_ratio = 10.0   # all calls, no puts = extreme bullish
    else:
        cp_ratio = 1.0

    min_vol = 10   # minimum contracts to avoid illiquid noise
    if cp_ratio >= 2.0 and call_vol >= min_vol:
        flow_signal = "BULLISH"
    elif cp_ratio <= 0.5 and put_vol >= min_vol:
        flow_signal = "BEARISH"
    else:
        flow_signal = "NEUTRAL"

    return {
        "flow_signal": flow_signal,
        "call_vol":    int(call_vol),
        "put_vol":     int(put_vol),
        "cp_ratio":    round(cp_ratio, 2),
        "expiry":      expiry,   # whichever expiry had liquid ATM contracts
        "price":       round(price, 2),
        "atm":         atm,
    }


def get_flow_scores(symbols: list[str]) -> dict[str, dict]:
    """
    Returns {symbol: {flow_signal, call_vol, put_vol, cp_ratio, expiry, price, atm}}.
    Errors caught per-symbol — symbol still passes through as UNKNOWN.
    """
    token  = get_robinhood_token()
    scores = {}

    for sym in symbols:
        try:
            scores[sym] = _score_symbol(token, sym)
            s = scores[sym]
            print(f"  Flow [{sym:5s}]: {s['flow_signal']:8s}  "
                  f"ATM={s.get('atm','?'):8}  "
                  f"C/P={s.get('cp_ratio','?'):5}  "
                  f"calls={s.get('call_vol','?'):5}  puts={s.get('put_vol','?')}")
        except Exception as e:
            scores[sym] = {"flow_signal": "UNKNOWN", "error": str(e)}
            print(f"  Flow [{sym:5s}]: ERROR — {e}")

    return scores


def adjusted_confidence(signal: dict, flow_scores: dict) -> float:
    """
    Multiply Bayesian confidence by a flow alignment factor.
    Aligned flow → boost; opposing flow → penalty. Used to rank candidates.
    """
    base_conf = signal.get("confidence", 0)
    sym       = signal.get("symbol", "")
    direction = signal.get("signal", "NEUTRAL")
    flow      = flow_scores.get(sym, {}).get("flow_signal", "UNKNOWN")

    if direction == "BUY":
        mult = {"BULLISH": 1.25, "NEUTRAL": 1.0, "UNKNOWN": 1.0, "BEARISH": 0.80}[flow]
    elif direction == "SELL":
        mult = {"BEARISH": 1.25, "NEUTRAL": 1.0, "UNKNOWN": 1.0, "BULLISH": 0.80}[flow]
    else:
        mult = 1.0

    return round(base_conf * mult, 1)


if __name__ == "__main__":
    from config import WATCHLIST
    print(f"Options flow scan for: {WATCHLIST}\n")
    scores = get_flow_scores(WATCHLIST)
    print("\n── Summary ─────────────────────────────────────────")
    for sym, s in scores.items():
        print(f"  {sym:6s}: {s.get('flow_signal','?'):8s}  "
              f"ATM={s.get('atm','?')}  C/P={s.get('cp_ratio','?')}  "
              f"expiry={s.get('expiry','?')}")
