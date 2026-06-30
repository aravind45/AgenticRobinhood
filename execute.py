# execute.py
# Full trade loop: signal → validate → execute via Robinhood MCP (direct HTTP)

import json
import os
import requests
from datetime import datetime, timezone
from config import get_robinhood_token, ACCOUNT_NUMBER, MCP_URL
from trade_signal import get_signal
from agent import validate_signal

POSITION_FILE = os.path.join(os.path.dirname(__file__), "position.json")


def place_order_direct(signal: dict, skip_save: bool = False):
    """Call Robinhood MCP endpoint directly via HTTP."""

    token = get_robinhood_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # ── Compute limit price from live bid/ask ──────────────────────────────
    side = signal["signal"].lower()
    ask  = signal.get("ask", 0)
    bid  = signal.get("bid", 0)
    last = signal.get("price", 0)

    if side == "buy":
        # Bid below market — try to get filled at bid (better than paying ask)
        raw_limit = (bid if bid > 0 else last) - 0.05
    else:
        # Offer above market — try to get filled at ask (better than receiving bid)
        raw_limit = (ask if ask > 0 else last) + 0.05

    limit_price = str(round(raw_limit, 2))
    print(f"Limit price: ${limit_price}  (side={side}, bid={bid}, ask={ask}, last={last})")

    order_args = {
        "account_number": ACCOUNT_NUMBER,
        "symbol":         signal["symbol"],
        "side":           side,
        "quantity":       "1",
        "type":           "limit",
        "limit_price":    limit_price,
        "time_in_force":  "gtc",   # good till cancelled — survives after hours
    }

    # Step 1: Review the order
    print("\nCalling review_equity_order...")
    review_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "review_equity_order", "arguments": order_args}
    }
    r = requests.post(MCP_URL, json=review_payload, headers=headers)
    print(f"Review response [{r.status_code}]:")
    print(r.text[:1000])

    if r.status_code != 200:
        print(f"\nAuth failed. Response: {r.text}")
        return

    # Step 2: Place the order
    print("\nCalling place_equity_order...")
    place_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "place_equity_order", "arguments": order_args}
    }
    r2 = requests.post(MCP_URL, json=place_payload, headers=headers)
    print(f"Place response [{r2.status_code}]:")
    print(r2.text[:1000])

    # Save position for monitor to track (skip on close orders)
    if not skip_save and r2.status_code == 200:
        try:
            data     = json.loads(r2.text.split("data: ", 1)[1])
            order    = data["result"]["content"][0]
            order_data = json.loads(order["text"]).get("data", {}).get("order", {})
            if not data["result"].get("isError"):
                save_position(signal, order_data)
        except Exception as e:
            print(f"Warning: could not save position ({e})")


def save_position(signal: dict, order: dict):
    """Persist open position so monitor.py / daemon.py can track it."""
    position = {
        "symbol":       signal["symbol"],
        "side":         signal["signal"].lower(),   # 'buy' or 'sell'
        "quantity":     "1",
        "entry_price":  order.get("price"),
        "order_id":     order.get("id"),
        "entry_time":   datetime.now(timezone.utc).isoformat(),
        "entry_signal": signal,
    }
    with open(POSITION_FILE, "w") as f:
        json.dump(position, f, indent=2)
    print(f"Position saved → {POSITION_FILE}")


def load_position() -> dict | None:
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE) as f:
            return json.load(f)
    return None


def clear_position():
    if os.path.exists(POSITION_FILE):
        os.remove(POSITION_FILE)


def close_position(position: dict):
    """Execute the closing (opposite) order for an open position."""
    close_side = "sell" if position["side"] == "buy" else "buy"
    close_signal = {**position["entry_signal"], "signal": close_side.upper()}
    print(f"\nClosing position: {close_side.upper()} {position['symbol']}")
    place_order_direct(close_signal, skip_save=True)
    clear_position()
    print("Position closed and cleared.")


def run():
    print("=" * 50)
    print("Project Alpha — Trade Loop")
    print("=" * 50)

    # Step 1: Build live signal
    print("Step 1: Building live signal from Robinhood...")
    signal = get_signal("SPY")

    print(f"  Signal:     {signal['signal']} {signal['symbol']}")
    print(f"  Price:      ${signal['price']}")
    print(f"  Confidence: {signal['confidence']}")
    print(f"  Vivek:      state={signal['vivek_state']}  dir={signal['vivek_dir_trend']}")
    print(f"  QTrend:     state={signal['qtrend_state']}  last={signal['qtrend_last_signal']}")
    print(f"  Fisher:     {signal.get('fisher_value')}  (signal={signal.get('fisher_signal')})")
    print(f"  KF vel:     {signal.get('kf_velocity')}")
    print(f"  Vol ratio:  {signal.get('vol_ratio')}  Vol z: {signal.get('volume_zscore')}")
    print(f"  Regime:     {signal.get('regime_state')}  ({signal.get('regime_reason', '')})")
    print("-" * 50)

    if signal["signal"] == "NEUTRAL":
        print("No confluence signal. Trade not executed.")
        return
    if signal["signal"] == "BLOCKED":
        print(f"Trade blocked by regime filter: {signal.get('regime_reason')}")
        return

    # Step 2: Claude validation
    print("Step 2: AI Decision Engine validating...")
    decision, reason = validate_signal(signal)
    print(f"Decision: {decision}")
    if reason:
        print(f"Reason: {reason}")

    if decision != "APPROVE":
        print("Trade not executed.")
        return

    # Step 3: Execute
    print("-" * 50)
    print("Step 3: Executing via Robinhood MCP...")
    place_order_direct(signal)
    print("=" * 50)


if __name__ == "__main__":
    run()
