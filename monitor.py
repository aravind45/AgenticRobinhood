# monitor.py
# Polls for exit conditions and lets Claude decide when to close the position.
# Run this after execute.py has opened a position.
# Usage: python monitor.py

import json
import time
from datetime import datetime, timezone

from agent import evaluate_exit
from execute import load_position, close_position
from trade_signal import get_signal

# How often to check (seconds). 60 = every minute.
POLL_INTERVAL = 60


def get_current_signal(position: dict) -> dict:
    """Pull a fresh live signal computed from RH data + Vivek + QTrend."""
    return get_signal(position["symbol"])


def run():
    print("=" * 50)
    print("Project Alpha — Position Monitor")
    print("=" * 50)

    position = load_position()
    if not position:
        print("No open position found. Run execute.py first.")
        return

    print(f"Monitoring: {position['side'].upper()} {position['symbol']}")
    print(f"Entry price : {position.get('entry_price', 'unknown')}")
    print(f"Entry time  : {position.get('entry_time', 'unknown')}")
    print(f"Checking every {POLL_INTERVAL}s. Press Ctrl+C to stop.\n")

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        current_signal = get_current_signal(position)

        print(f"[{now}] Vivek={current_signal['vivek_state']}  "
              f"QTrend={current_signal['qtrend_state']}  "
              f"Price=${current_signal['price']}  "
              f"Conf={current_signal['confidence']}", end="  →  ")

        decision, reason = evaluate_exit(position, current_signal, live_features=None)
        print(f"Claude: {decision}" + (f" ({reason})" if reason else ""))

        if decision == "CLOSE":
            print("\nAuto-closing position...")
            close_position(position)
            print("Done. Monitor exiting.")
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
