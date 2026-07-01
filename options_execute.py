# options_execute.py
# Options execution layer for Project Alpha.
#
# Strategy:
#   BUY signal  → buy ATM CALL, nearest expiry 7-21 DTE
#   SELL signal → buy ATM PUT,  nearest expiry 7-21 DTE
#
# Always buys-to-open 1 contract. Sell-to-close on exit signal.
# Limit orders at mid-price (bid+ask)/2, GTC.

import json
import os
import requests
from datetime import date, datetime, timezone, timedelta
from config import get_robinhood_token, ACCOUNT_NUMBER, MCP_URL

POSITION_FILE = os.path.join(os.path.dirname(__file__), "position.json")

DTE_MIN = 7    # don't buy options expiring in < 7 days (too much theta)
DTE_MAX = 21   # don't go further than 3 weeks (too expensive, less sensitive)


# ── MCP helper ────────────────────────────────────────────────────────────────

def _call(token: str, tool: str, args: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args}
    }, headers=headers)
    data = json.loads(r.text.split("data: ", 1)[1])
    result = data.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result['content'][0]['text'][:300]}")
    return json.loads(result["content"][0]["text"])


# ── Contract finder ───────────────────────────────────────────────────────────

def find_atm_option(token: str, symbol: str, direction: str,
                    current_price: float) -> dict:
    """
    Find the best ATM option contract for a given signal direction.

    direction: "BUY" → call, "SELL" → put
    Returns a dict with: contract_id, strike, expiry, option_type,
                         bid, ask, mid_price, delta, iv, dte
    Raises RuntimeError if no suitable contract found.
    """
    opt_type = "call" if direction == "BUY" else "put"
    today    = date.today()

    # ── 1. Get chain ──────────────────────────────────────────────────────
    chain_resp = _call(token, "get_option_chains", {"underlying_symbol": symbol})
    chains = chain_resp.get("data", {}).get("chains", [])
    if not chains:
        raise RuntimeError(f"No option chain found for {symbol}")

    chain_id    = chains[0]["id"]
    all_expiries = chains[0].get("expiration_dates", [])

    # ── 2. Filter expiries to DTE window ──────────────────────────────────
    valid_expiries = []
    for d in all_expiries:
        exp_date = datetime.strptime(d, "%Y-%m-%d").date()
        dte = (exp_date - today).days
        if DTE_MIN <= dte <= DTE_MAX:
            valid_expiries.append((dte, d))

    if not valid_expiries:
        raise RuntimeError(
            f"No expiry in {DTE_MIN}-{DTE_MAX} DTE window for {symbol}. "
            f"Available: {all_expiries[:5]}"
        )

    # Pick nearest qualifying expiry (lowest DTE that meets minimum)
    valid_expiries.sort()
    target_expiry = valid_expiries[0][1]
    target_dte    = valid_expiries[0][0]
    print(f"  [{symbol}] Target expiry: {target_expiry} ({target_dte} DTE)")

    # ── 3. Find ATM contract ──────────────────────────────────────────────
    # Try $5 increments first (most stocks), then $1
    contract_id = None
    strike_used = None

    for inc in (5.0, 1.0):
        atm_f   = round(current_price / inc) * inc
        # Try ATM, then 1 strike OTM (slightly cheaper, still high delta)
        candidates = [atm_f, atm_f + inc, atm_f - inc]  # ATM, OTM, ITM

        for strike_f in candidates:
            strike_str = f"{strike_f:.4f}"
            resp = _call(token, "get_option_instruments", {
                "chain_id":         chain_id,
                "expiration_dates": target_expiry,
                "type":             opt_type,
                "strike_price":     strike_str,
                "state":            "active",
                "tradability":      "tradable",
            })
            instruments = resp.get("data", {}).get("instruments", [])
            if instruments:
                contract_id = instruments[0]["id"]
                strike_used = strike_f
                break

        if contract_id:
            break

    if not contract_id:
        raise RuntimeError(f"No tradable {opt_type} contracts found near ATM "
                           f"(${current_price:.2f}) for {symbol} {target_expiry}")

    print(f"  [{symbol}] Contract: {opt_type.upper()} ${strike_used} exp {target_expiry}  id={contract_id[:8]}...")

    # ── 4. Get live quote for the contract ────────────────────────────────
    quote_resp = _call(token, "get_option_quotes", {"instrument_ids": [contract_id]})
    results = quote_resp.get("data", {}).get("results", [])
    if not results:
        raise RuntimeError(f"No quote returned for contract {contract_id}")

    q = results[0].get("quote", {})
    bid = float(q.get("bid_price") or 0)
    ask = float(q.get("ask_price") or 0)
    mid = round((bid + ask) / 2, 2) if bid and ask else None
    iv  = float(q.get("implied_volatility") or 0)
    delta = float(q.get("delta") or 0)
    theta = float(q.get("theta") or 0)

    if not mid or mid <= 0:
        raise RuntimeError(f"Invalid mid-price (bid={bid}, ask={ask}) for {symbol} {opt_type}")

    print(f"  [{symbol}] Quote: bid={bid}  ask={ask}  mid={mid}  "
          f"delta={delta:.3f}  IV={iv:.1%}  theta={theta:.3f}/day")

    return {
        "contract_id": contract_id,
        "symbol":      symbol,
        "option_type": opt_type,
        "strike":      strike_used,
        "expiry":      target_expiry,
        "dte":         target_dte,
        "bid":         bid,
        "ask":         ask,
        "mid_price":   mid,
        "delta":       delta,
        "iv":          iv,
        "theta":       theta,
        "cost":        round(mid * 100, 2),   # 1 contract = 100 shares
    }


# ── Place option order ────────────────────────────────────────────────────────

def place_option(signal: dict, contract: dict,
                 limit_price_override: str | None = None) -> tuple[str, float]:
    """
    Place a buy-to-open limit order for 1 contract.

    Returns (order_id, limit_price_float) on acceptance.
    Raises RuntimeError on review failure, broker rejection, or HTTP error.

    Does NOT save position.json or call open_trade().
    The caller (daemon._check_entry_fill) does that only after fill is confirmed
    via get_option_order_status(), ensuring the DB and position file only reflect
    confirmed positions — not just accepted orders.

    limit_price_override: pass a string like "3.50" to reprice (used by cancel/reprice flow).
    """
    token = get_robinhood_token()

    mid = contract["mid_price"]
    if limit_price_override:
        limit_price = limit_price_override
    else:
        limit_price = str(round(round(mid / 0.05) * 0.05, 2))

    print(f"\n  Entry limit: ${limit_price}  (mid=${mid}  "
          f"cost ~${float(limit_price)*100:.2f} for 1 contract)")

    legs = [{
        "option":           contract["contract_id"],
        "position_effect":  "open",
        "side":             "buy",
        "ratio_quantity":   1,
    }]

    order_args = {
        "account_number": ACCOUNT_NUMBER,
        "legs":           legs,
        "quantity":       "1",
        "type":           "limit",
        "price":          limit_price,
        "time_in_force":  "gtc",
        "market_hours":   "regular_hours",
    }

    # Review first
    print("\nCalling review_option_order...")
    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "review_option_order", "arguments": {
            **order_args,
            "chain_symbol":    signal["symbol"],
            "underlying_type": "equity",
        }}
    }, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    print(f"Review [{r.status_code}]: {r.text[:300]}")

    if r.status_code != 200:
        raise RuntimeError(f"review_option_order HTTP {r.status_code}")

    # Place
    print("\nCalling place_option_order...")
    r2 = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "place_option_order", "arguments": order_args}
    }, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    print(f"Place [{r2.status_code}]: {r2.text[:300]}")

    if r2.status_code != 200:
        raise RuntimeError(f"place_option_order HTTP {r2.status_code}")

    try:
        data = json.loads(r2.text.split("data: ", 1)[1])
        if data.get("result", {}).get("isError"):
            err = data["result"]["content"][0]["text"][:300]
            raise RuntimeError(f"Entry order rejected by broker: {err}")
        order_data = json.loads(
            data["result"]["content"][0]["text"]
        ).get("data", {}).get("order", {})
        order_id = order_data.get("id", "")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"Warning: could not parse entry order response ({e})")
        order_id = ""

    print(f"Entry order accepted  order_id={order_id or '(unknown)'}  limit=${limit_price}")
    return order_id, float(limit_price)


def _save_option_position(signal: dict, contract: dict, order: dict):
    position = {
        "trade_type":  "option",
        "symbol":      signal["symbol"],
        "side":        "buy",
        "option_type": contract["option_type"],   # "call" or "put"
        "contract_id": contract["contract_id"],
        "strike":      contract["strike"],
        "expiry":      contract["expiry"],
        "dte":         contract["dte"],
        "quantity":    "1",
        "entry_price": contract["mid_price"],
        "entry_cost":  contract["cost"],          # total $ paid
        "order_id":    order.get("id", ""),
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "entry_signal": signal,
    }
    with open(POSITION_FILE, "w") as f:
        json.dump(position, f, indent=2)
    print(f"Option position saved → {POSITION_FILE}")


# ── Close option position ─────────────────────────────────────────────────────

def close_option(position: dict) -> tuple[str, float]:
    """
    Place a sell-to-close limit order at mid-price.

    Returns (order_id, mid_price) on acceptance.
    Raises RuntimeError on broker rejection or HTTP error.

    IMPORTANT: this function does NOT clear position.json or update the DB.
    The caller (daemon.py try_exit) must:
      1. Save order_id to position.json as "close_order_id"
      2. Call db.mark_closing(trade_id, order_id)
      3. On the NEXT cycle, call get_option_order_status() to confirm fill
      4. Only then call db.close_trade() and clear position.json

    This two-phase design ensures the DB never says "closed" before the fill
    is confirmed, and position.json is never erased on an unfilled GTC order.
    """
    token = get_robinhood_token()

    # Get current quote for the contract
    contract_id = position["contract_id"]
    try:
        quote_resp = _call(token, "get_option_quotes", {"instrument_ids": [contract_id]})
        results = quote_resp.get("data", {}).get("results", [])
        q = results[0].get("quote", {}) if results else {}
        bid = float(q.get("bid_price") or 0)
        ask = float(q.get("ask_price") or 0)
        mid = round((bid + ask) / 2, 2) if bid and ask else position.get("entry_price", 1.0)
    except Exception:
        mid = position.get("entry_price", 1.0)

    limit_price = str(round(round(mid / 0.05) * 0.05, 2))
    print(f"\nPlacing close order: SELL {position['symbol']} "
          f"{position['option_type'].upper()} ${position['strike']} "
          f"exp {position['expiry']}  limit=${limit_price}  (mid={mid})")

    legs = [{
        "option":          contract_id,
        "position_effect": "close",
        "side":            "sell",
        "ratio_quantity":  1,
    }]

    order_args = {
        "account_number": ACCOUNT_NUMBER,
        "legs":           legs,
        "quantity":       "1",
        "type":           "limit",
        "price":          limit_price,
        "time_in_force":  "gtc",
        "market_hours":   "regular_hours",
    }

    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "place_option_order", "arguments": order_args}
    }, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    print(f"Close order [{r.status_code}]: {r.text[:500]}")

    if r.status_code != 200:
        raise RuntimeError(f"Close order HTTP error: {r.status_code}")

    try:
        data = json.loads(r.text.split("data: ", 1)[1])
        if data.get("result", {}).get("isError"):
            err = data["result"]["content"][0]["text"][:300]
            raise RuntimeError(f"Close order rejected by broker: {err}")
        order_data = json.loads(
            data["result"]["content"][0]["text"]
        ).get("data", {}).get("order", {})
        order_id = order_data.get("id", "")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"Warning: could not parse close order response ({e}) — order may still be placed.")
        order_id = ""

    print(f"Close order accepted  order_id={order_id or '(unknown)'}  mid=${mid}")
    return order_id, mid


def get_option_order_status(token: str, order_id: str) -> dict:
    """
    Poll the fill status of an option order.

    Returns a dict:
        state       — "filled", "partially_filled", "confirmed", "queued",
                      "cancelled", "failed", "rejected", or "unknown"
        fill_price  — average fill price (0 if not filled yet)
        filled_qty  — number of contracts filled so far
    """
    if not order_id:
        return {"state": "unknown", "fill_price": 0.0, "filled_qty": 0}
    try:
        resp = _call(token, "get_option_orders", {"order_id": order_id})
        # Try common response shapes
        order = (resp.get("data", {}).get("order")
                 or resp.get("data", {})
                 or resp)
        if isinstance(order, list):
            order = order[0] if order else {}
        state      = order.get("state", "unknown")
        fill_price = float(order.get("average_price") or order.get("price") or 0)
        filled_qty = int(float(order.get("filled_quantity") or order.get("quantity") or 0))
        return {"state": state, "fill_price": fill_price, "filled_qty": filled_qty}
    except Exception as e:
        print(f"Warning: get_option_order_status failed ({e}) — state unknown")
        return {"state": "unknown", "fill_price": 0.0, "filled_qty": 0}


def cancel_option_order(token: str, order_id: str) -> bool:
    """
    Cancel a pending option order.
    Returns True if the cancellation request was accepted, False otherwise.
    A True return means the cancel was submitted, not necessarily that the order
    wasn't already filled — always re-check status after cancelling.
    """
    if not order_id:
        return False
    try:
        _call(token, "cancel_option_order", {"order_id": order_id})
        print(f"Cancel request sent for order {order_id[:8]}...")
        return True
    except Exception as e:
        print(f"Warning: cancel_option_order failed ({e})")
        return False


def reprice_entry(token: str, contract_id: str, symbol: str,
                  price_override: float | None = None) -> tuple[str, float]:
    """
    Get a fresh ask quote and place a new buy-to-open order at that price.
    Used when a mid-price entry order sits unfilled — repricing toward the ask
    improves fill probability at the cost of a slightly higher premium.

    Returns (new_order_id, new_limit_price).
    Raises RuntimeError on failure.
    """
    # Get fresh quote
    try:
        qresp = _call(token, "get_option_quotes", {"instrument_ids": [contract_id]})
        results = qresp.get("data", {}).get("results", [])
        q = results[0].get("quote", {}) if results else {}
        ask = float(q.get("ask_price") or 0)
        mid = float(q.get("ask_price") or 0)
        bid = float(q.get("bid_price") or 0)
        mid = round((bid + ask) / 2, 2) if bid and ask else 0
    except Exception:
        ask = mid = 0

    new_price = price_override or ask or (mid * 1.05) if (price_override or ask or mid) else None
    if not new_price:
        raise RuntimeError("Cannot reprice: no valid ask price available")

    limit_str = str(round(round(new_price / 0.05) * 0.05, 2))
    print(f"Repricing entry: ask=${ask}  new limit=${limit_str}  symbol={symbol}")

    legs = [{
        "option":          contract_id,
        "position_effect": "open",
        "side":            "buy",
        "ratio_quantity":  1,
    }]
    order_args = {
        "account_number": ACCOUNT_NUMBER,
        "legs":           legs,
        "quantity":       "1",
        "type":           "limit",
        "price":          limit_str,
        "time_in_force":  "gtc",
        "market_hours":   "regular_hours",
    }

    r = requests.post(MCP_URL, json={
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": "place_option_order", "arguments": order_args}
    }, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    if r.status_code != 200:
        raise RuntimeError(f"Reprice order HTTP {r.status_code}")

    try:
        data = json.loads(r.text.split("data: ", 1)[1])
        if data.get("result", {}).get("isError"):
            raise RuntimeError("Reprice order rejected: " +
                               data["result"]["content"][0]["text"][:200])
        order_data = json.loads(
            data["result"]["content"][0]["text"]
        ).get("data", {}).get("order", {})
        new_order_id = order_data.get("id", "")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"Warning: could not parse reprice response ({e})")
        new_order_id = ""

    print(f"Reprice order accepted: {new_order_id[:8] if new_order_id else '?'}  limit=${limit_str}")
    return new_order_id, float(limit_str)


def clear_option_position():
    """Remove position.json. Call ONLY after fill is confirmed."""
    if os.path.exists(POSITION_FILE):
        os.remove(POSITION_FILE)
    print("Position file cleared (fill confirmed).")


# ── DTE check (used by daemon monitor) ───────────────────────────────────────

def days_to_expiry(position: dict) -> int:
    """How many calendar days until the option expires."""
    try:
        exp = datetime.strptime(position["expiry"], "%Y-%m-%d").date()
        return (exp - date.today()).days
    except Exception:
        return 99
