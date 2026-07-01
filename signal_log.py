# signal_log.py
# Append-only signal journal for Project Alpha.
#
# Writes ONE JSON line per signal to signals_log.jsonl.
# Every symbol scan is recorded — including NEUTRAL, BLOCKED, and low-confidence
# signals — so you can evaluate why the system did or didn't trade.
#
# File location: <project_dir>/signals_log.jsonl
#
# Load in pandas:
#   import pandas as pd
#   df = pd.read_json("signals_log.jsonl", lines=True)
#
# Tail live:
#   Get-Content signals_log.jsonl -Wait -Tail 20    (PowerShell)

import json
import os
from datetime import datetime, timezone

LOG_FILE = os.path.join(os.path.dirname(__file__), "signals_log.jsonl")


def append(signal: dict, *,
           action: str = "",
           action_reason: str = "",
           flow_signal: str = "",
           adj_conf: int = 0):
    """
    Append one signal record to signals_log.jsonl.

    Extra keyword args (all optional):
        action        — what the system did: "SCAN_ONLY", "CANDIDATE",
                        "APPROVE", "REJECT", "HOLD", "KILL_SWITCH"
        action_reason — Claude's reason or kill-switch message
        flow_signal   — options flow direction for this symbol ("BULLISH" etc.)
        adj_conf      — flow-adjusted confidence (int %)
    """
    record = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "symbol":       signal.get("symbol"),
        "signal":       signal.get("signal"),
        "confidence":   signal.get("confidence"),
        "vivek_state":  signal.get("vivek_state"),
        "qtrend_state": signal.get("qtrend_state"),
        "vivek_rsi2":   signal.get("vivek_rsi2"),
        "regime_state": signal.get("regime_state"),
        "regime_tradeable": signal.get("regime_tradeable"),
        "vol_ratio":    signal.get("vol_ratio"),
        "volume_zscore":signal.get("volume_zscore"),
        "kf_velocity":  signal.get("kf_velocity"),
        "fisher_value": signal.get("fisher_value"),
        "channel_converging": signal.get("channel_converging"),
        "price":        signal.get("price"),
        "bid":          signal.get("bid"),
        "ask":          signal.get("ask"),
        "spread_pct":   signal.get("spread_pct"),
        "buying_power": signal.get("buying_power"),
        # option details (populated on second Claude pass)
        "option_type":  signal.get("option_type"),
        "option_strike":signal.get("option_strike"),
        "option_expiry":signal.get("option_expiry"),
        "option_dte":   signal.get("option_dte"),
        "option_cost":  signal.get("option_cost"),
        "option_delta": signal.get("option_delta"),
        "option_iv":    signal.get("option_iv"),
        "option_mid":   signal.get("option_mid"),
        # system decision
        "action":        action,
        "action_reason": action_reason,
        "flow_signal":   flow_signal,
        "adj_conf":      adj_conf or signal.get("confidence"),
    }
    # Strip None values to keep the file compact
    record = {k: v for k, v in record.items() if v is not None}

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        # Never crash the daemon over a log write failure
        print(f"[signal_log] WARNING: could not write to {LOG_FILE}: {e}")


def tail(n: int = 20) -> list[dict]:
    """Return the last n records from the log (for debugging / quick review)."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        return [json.loads(l) for l in lines[-n:] if l.strip()]
    except Exception:
        return []


if __name__ == "__main__":
    records = tail(10)
    if not records:
        print(f"No records yet in {LOG_FILE}")
    else:
        print(f"Last {len(records)} signals:\n")
        for r in records:
            ts     = r.get("ts", "")[:19].replace("T", " ")
            sym    = r.get("symbol", "?")
            sig    = r.get("signal", "?")
            conf   = r.get("confidence", "?")
            action = r.get("action", "")
            reason = r.get("action_reason", "")
            print(f"  {ts}  {sym:6s}  {sig:7s}  {conf:>3}%  [{action}]  {reason[:60]}")
