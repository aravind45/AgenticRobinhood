# daemon.py
# Project Alpha — Autonomous Trading Daemon
#
# Runs the full cycle without human input:
#   1. Wait until NYSE market hours (9:35–15:45 ET)
#   2. Check for open position → monitor it
#   3. If no position → look for a new signal → trade if APPROVE
#   4. Sleep and repeat until market close
#
# Launch: python daemon.py
# Or via Windows Task Scheduler (see setup_scheduler.ps1)

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

# ── Timezone helper (no pytz needed — works on Python 3.9+) ──────────────────
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows has no built-in tzdata — install with: pip install tzdata
    # Fallback: UTC-4 (EDT, summer) / UTC-5 (EST, winter)
    # Simple DST approximation: EDT Mar-Nov, EST Dec-Feb
    _m = datetime.now().month
    _offset = -4 if 3 <= _m <= 11 else -5
    ET = timezone(timedelta(hours=_offset))

# ── Project imports ───────────────────────────────────────────────────────────
from trade_signal import get_signal
from agent import validate_signal, evaluate_exit
from execute import (
    place_order_direct, save_position, load_position,
    clear_position, close_position, POSITION_FILE
)
from options_execute import (
    find_atm_option, place_option, close_option, days_to_expiry
)
from db import init_db, log_signal, log_decision, open_trade, close_trade, get_open_trade
from config import WATCHLIST, MAX_POSITIONS
from flow_filter import get_flow_scores, adjusted_confidence

# ── Flow score cache (refreshed once per trading day) ─────────────────────────
_flow_scores: dict  = {}
_flow_date:   str   = ""

# ── Config ────────────────────────────────────────────────────────────────────
SIGNAL_INTERVAL  = 5 * 60      # seconds between signal checks when flat (5 min)
MONITOR_INTERVAL = 60          # seconds between exit checks when in position (1 min)
MARKET_OPEN_ET  = (9, 35)      # don't trade before 9:35 AM ET
MARKET_CLOSE_ET = (15, 45)     # stop trading at 3:45 PM ET
MIN_CONFIDENCE  = 70            # minimum Bayesian confidence to execute


def now_et() -> datetime:
    return datetime.now(ET)


def market_is_open() -> bool:
    """True between 9:35 and 15:45 ET, weekdays only."""
    t = now_et()
    if t.weekday() >= 5:        # Saturday=5, Sunday=6
        return False
    hm = (t.hour, t.minute)
    return MARKET_OPEN_ET <= hm < MARKET_CLOSE_ET


def seconds_until_open() -> float:
    """Seconds until next market open (9:35 ET). Handles overnight and weekends."""
    t = now_et()
    # Build today's open time
    target = t.replace(hour=MARKET_OPEN_ET[0], minute=MARKET_OPEN_ET[1],
                       second=0, microsecond=0)
    if t >= target:
        # Already past today's open — jump to next weekday
        target += timedelta(days=1)
    # Skip weekends
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return (target - t).total_seconds()


def _log(msg: str):
    ts = now_et().strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"[{ts}]  {msg}", flush=True)


# ── Entry cycle ───────────────────────────────────────────────────────────────

def _refresh_flow_scores():
    """Fetch options flow once per trading day and cache it."""
    global _flow_scores, _flow_date
    today = now_et().strftime("%Y-%m-%d")
    if _flow_date == today and _flow_scores:
        return  # already fresh
    _log(f"Refreshing options flow scores for {len(WATCHLIST)} symbols...")
    try:
        _flow_scores = get_flow_scores(WATCHLIST)
        _flow_date   = today
        _log("Flow scores ready.")
    except Exception as e:
        _log(f"Flow score refresh failed ({e}) — proceeding without flow filter.")
        _flow_scores = {}


def _scan_symbol(symbol: str) -> dict | None:
    """
    Pull a signal for one symbol. Returns the signal dict if actionable
    (BUY/SELL, confidence >= MIN_CONFIDENCE), else None.
    """
    try:
        signal = get_signal(symbol)
    except Exception as e:
        _log(f"  {symbol}: ERROR — {e}")
        return None

    conf = signal["confidence"]
    sig  = signal["signal"]
    vel  = signal.get("kf_velocity", 0) or 0
    _log(f"  {symbol}: {sig:7s}  conf={conf}%  "
         f"Vivek={signal['vivek_state']}  QTrend={signal['qtrend_state']}  "
         f"Regime={signal['regime_state']}  KF_vel={vel:.4f}")

    if sig in ("NEUTRAL", "BLOCKED"):
        return None
    if conf < MIN_CONFIDENCE:
        return None
    return signal


def try_entry() -> bool:
    """
    Scan all watchlist symbols. Pick the highest-confidence signal.
    Validate with Claude. Execute if approved.
    Returns True if a trade was placed.
    """
    # Don't enter if already at max positions
    pos = load_position()
    if pos:
        _log(f"Already holding {pos['symbol']} — skipping entry scan.")
        return False

    # Refresh options flow scores once per day
    _refresh_flow_scores()

    _log(f"Scanning {len(WATCHLIST)} symbols: {', '.join(WATCHLIST)}")

    candidates = []
    for sym in WATCHLIST:
        sig = _scan_symbol(sym)
        if sig:
            candidates.append(sig)
            log_signal(sig)   # log every actionable signal to DB

    if not candidates:
        _log("No actionable signals across watchlist.")
        return False

    # Rank by flow-adjusted confidence — flow-aligned signals get a 25% boost
    for sig in candidates:
        sig["_adj_conf"] = adjusted_confidence(sig, _flow_scores)
        flow = _flow_scores.get(sig["symbol"], {}).get("flow_signal", "UNKNOWN")
        _log(f"  {sig['symbol']}: conf={sig['confidence']}%  "
             f"flow={flow}  adj_conf={sig['_adj_conf']}%")

    candidates.sort(key=lambda s: s["_adj_conf"], reverse=True)
    best = candidates[0]
    _log(f"Best signal: {best['symbol']} {best['signal']} @ "
         f"{best['confidence']}% conf / {best['_adj_conf']}% adj")
    if len(candidates) > 1:
        others = ", ".join(
            f"{s['symbol']}({s['_adj_conf']}%)" for s in candidates[1:]
        )
        _log(f"Also fired (not selected): {others}")

    sig_id = log_signal(best)

    # Claude validation
    _log(f"Asking Claude to validate {best['symbol']} {best['signal']}...")
    try:
        decision, reason = validate_signal(best)
    except Exception as e:
        _log(f"ERROR from Claude: {e}")
        return False

    log_decision(sig_id, "ENTRY", decision, reason)
    _log(f"Claude: {decision}  {reason}")

    if decision != "APPROVE":
        return False

    # ── Find ATM option contract ──────────────────────────────────────────
    _log(f"Finding ATM option for {best['symbol']} {best['signal']}...")
    try:
        from config import get_robinhood_token
        token    = get_robinhood_token()
        contract = find_atm_option(token, best["symbol"],
                                   best["signal"], best["price"])
        _log(f"Contract: {contract['option_type'].upper()} "
             f"${contract['strike']} exp {contract['expiry']} "
             f"({contract['dte']} DTE)  mid=${contract['mid_price']}  "
             f"cost=${contract['cost']}  delta={contract['delta']:.3f}")
    except Exception as e:
        _log(f"ERROR finding option contract: {e}")
        return False

    # Enrich signal with option details so Claude can evaluate cost vs buying power
    best["option_type"]  = contract["option_type"]
    best["option_strike"] = contract["strike"]
    best["option_expiry"] = contract["expiry"]
    best["option_dte"]   = contract["dte"]
    best["option_cost"]  = contract["cost"]
    best["option_delta"] = contract["delta"]
    best["option_iv"]    = contract["iv"]
    best["option_mid"]   = contract["mid_price"]

    # Re-validate with Claude (now includes option cost / IV / delta)
    _log("Re-validating with Claude (option details included)...")
    try:
        decision, reason = validate_signal(best)
    except Exception as e:
        _log(f"ERROR from Claude: {e}")
        return False

    log_decision(sig_id, "ENTRY", decision, reason)
    _log(f"Claude: {decision}  {reason}")

    if decision != "APPROVE":
        return False

    # ── Place option order ────────────────────────────────────────────────
    _log(f"Placing option order...")
    try:
        place_option(best, contract)
    except Exception as e:
        _log(f"ERROR placing option: {e}")
        return False

    # Augment position.json with DB trade id
    pos = load_position()
    if pos:
        trade_id = open_trade(best["symbol"], best["signal"].lower(),
                              contract["mid_price"], pos.get("order_id", ""), sig_id)
        pos["trade_id"] = trade_id
        with open(POSITION_FILE, "w") as f:
            json.dump(pos, f, indent=2)
        _log(f"Trade #{trade_id} opened — "
             f"{best['symbol']} {contract['option_type'].upper()} "
             f"${contract['strike']} @ ${contract['mid_price']}")

    return True


# ── Exit cycle ────────────────────────────────────────────────────────────────

def try_exit(position: dict) -> bool:
    """
    Ask Claude whether to hold or close the open position.
    Returns True if position was closed.
    """
    _log(f"Monitoring open {position['side'].upper()} position...")
    try:
        signal = get_signal(position["symbol"])
    except Exception as e:
        _log(f"ERROR getting exit signal: {e}")
        return False

    _log(f"Exit check — Signal={signal['signal']}  Conf={signal['confidence']}%  "
         f"Vivek={signal['vivek_state']}  QTrend={signal['qtrend_state']}  "
         f"KF_vel={signal.get('kf_velocity', 0):.4f}")

    sig_id = log_signal(signal)

    try:
        action, reason = evaluate_exit(position, signal)
    except Exception as e:
        _log(f"ERROR from Claude exit evaluator: {e}")
        return False

    log_decision(sig_id, "EXIT", action, reason)
    _log(f"Claude exit: {action}  {reason}")

    # ── DTE hard-close: don't hold options into expiry week ──────────────
    is_option = position.get("trade_type") == "option"
    if is_option:
        dte = days_to_expiry(position)
        _log(f"Option DTE remaining: {dte}")
        if dte <= 3:
            action = "CLOSE"
            reason = f"DTE={dte} — closing to avoid expiry theta burn"
            _log(f"Hard close triggered: {reason}")

    if action == "CLOSE":
        _log("Closing position...")
        try:
            if is_option:
                exit_price = close_option(position)
            else:
                close_position(position)
                exit_price = float(signal.get("price") or 0)
        except Exception as e:
            _log(f"ERROR closing position: {e}")
            return False

        # Update DB
        trade_id = position.get("trade_id")
        if trade_id:
            close_trade(trade_id, exit_price or 0,
                        exit_reason=reason or "Claude CLOSE")
        _log(f"Position closed  exit=${exit_price}")
        return True

    return False


# ── Main daemon loop ──────────────────────────────────────────────────────────

def run():
    _log("=" * 60)
    _log("Project Alpha — Autonomous Daemon starting")
    _log("=" * 60)

    # Initialise database
    init_db()
    _log(f"Database ready")

    while True:
        try:
            if not market_is_open():
                wait = seconds_until_open()
                _log(f"Market closed. Next open in {wait/3600:.1f}h. Sleeping...")
                time.sleep(min(wait, 1800))   # re-check every 30 min max
                continue

            # ── Check for existing open position ─────────────────────────────
            position = load_position()
            if position:
                sym = position.get("symbol", "?")
                _log(f"Open position: {sym} {position.get('side','').upper()} "
                     f"@ ${position.get('entry_price', '?')}")
                closed = try_exit(position)
                if closed:
                    _log(f"{sym} position closed — back to scanning for entries.")
                time.sleep(MONITOR_INTERVAL)
                continue

            # ── No position — scan all symbols for entry ──────────────────────
            traded = try_entry()
            if traded:
                _log(f"Entry placed. Switching to {MONITOR_INTERVAL}s monitor loop.")
                time.sleep(MONITOR_INTERVAL)
            else:
                _log(f"No trade. Next scan in {SIGNAL_INTERVAL//60} min.")
                time.sleep(SIGNAL_INTERVAL)

        except KeyboardInterrupt:
            _log("Daemon interrupted by user. Goodbye.")
            sys.exit(0)

        except Exception:
            _log("UNHANDLED EXCEPTION — will retry in 60s:")
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    run()
