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
    find_atm_option, place_option, close_option, days_to_expiry,
    get_option_order_status, clear_option_position,
    cancel_option_order, reprice_entry,
)
from db import (
    init_db, log_signal, log_decision,
    open_trade, mark_closing, close_trade, get_open_trade,
)
from risk_checks import pre_trade_checks
from config import WATCHLIST, MAX_POSITIONS
from flow_filter import get_flow_scores, adjusted_confidence
import signal_log

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
    line = f"[{ts}]  {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows cp1252 console can't handle emoji/Unicode in Claude's responses
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)


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
    Pull a signal for one symbol. Logs EVERY signal to signals_log.jsonl,
    regardless of outcome. Returns the signal dict only if actionable
    (BUY/SELL, confidence >= MIN_CONFIDENCE), else None.
    """
    try:
        signal = get_signal(symbol)
    except Exception as e:
        _log(f"  {symbol}: ERROR — {e}")
        traceback.print_exc()
        return None

    conf = signal["confidence"]
    sig  = signal["signal"]
    vel  = signal.get("kf_velocity", 0) or 0
    _log(f"  {symbol}: {sig:7s}  conf={conf}%  "
         f"Vivek={signal['vivek_state']}  QTrend={signal['qtrend_state']}  "
         f"Regime={signal['regime_state']}  KF_vel={vel:.4f}")

    # Log every scan — including NEUTRAL/BLOCKED/low-confidence
    if sig in ("NEUTRAL", "BLOCKED"):
        signal_log.append(signal, action="SCAN_ONLY",
                          action_reason=f"signal={sig}")
        return None
    if conf < MIN_CONFIDENCE:
        signal_log.append(signal, action="SCAN_ONLY",
                          action_reason=f"conf={conf}% below MIN={MIN_CONFIDENCE}%")
        return None

    # Actionable — caller will update action to CANDIDATE/APPROVE/REJECT
    return signal


# ── Entry fill-confirmation ────────────────────────────────────────────────────
#
# Two-phase entry state machine mirrors the two-phase exit:
#
#   Phase 1 (try_entry):
#     scan → validate → pre_trade_checks → place_option()
#     → save position.json with status="opening", entry_order_id, etc.
#     → return True (switch to poll mode)
#
#   Phase 2 (_check_entry_fill):
#     poll get_option_order_status()
#     - filled:    open_trade() + promote position to status="open" → True
#     - pending, < ENTRY_FILL_TIMEOUT: wait → False
#     - pending, >= ENTRY_FILL_TIMEOUT and reprice_count < MAX_REPRICE_ATTEMPTS:
#                  cancel + reprice toward ask → update position.json → False
#     - pending, reprice_count >= MAX_REPRICE_ATTEMPTS: cancel + abort → False
#     - cancelled/failed/rejected: clear position.json → False
#
# DB open_trade() is ONLY called after fill is confirmed — never on order placement.
#
ENTRY_FILL_TIMEOUT  = 10 * 60  # seconds to wait at mid before repricing toward ask
MAX_REPRICE_ATTEMPTS = 2        # max ask reprice attempts before aborting entry


def _check_entry_fill(position: dict) -> bool:
    """
    Phase 2 of entry: poll for fill confirmation. Handles reprice and abort.
    Returns True when fill is confirmed and position is promoted to 'open'.
    Returns False if still pending, repriced, or aborted (back to scan).
    """
    try:
        from config import get_robinhood_token as _tok
        token = _tok()
    except Exception as e:
        _log(f"WARNING: could not get token for entry fill check ({e})")
        return False

    symbol         = position.get("symbol", "?")
    order_id       = position.get("entry_order_id", "")
    order_time_str = position.get("entry_order_time", "")
    reprice_count  = position.get("reprice_count", 0)
    contract_id    = position.get("contract_id", "")
    sig_id         = position.get("sig_id")

    _log(f"Checking entry fill: {symbol}  order={order_id[:8] if order_id else '?'}")

    # Poll broker
    try:
        status = get_option_order_status(token, order_id)
    except Exception as e:
        _log(f"WARNING: could not poll entry order status ({e}) — retrying next cycle.")
        return False

    state      = status["state"]
    fill_price = status["fill_price"]
    _log(f"Entry order state={state}  fill_price=${fill_price}  qty={status['filled_qty']}")

    # ── Fill confirmed ────────────────────────────────────────────────────
    if state == "filled":
        actual_fill = fill_price or position.get("intended_mid", 0)
        trade_id = open_trade(
            symbol,
            "buy",                              # always buy-to-open for options
            actual_fill,
            order_id,
            sig_id,
            trade_type="option",
            signal_dir=position.get("signal_dir", ""),
        )
        # Promote position.json from "opening" to "open"
        position["status"]      = "open"
        position["entry_price"] = actual_fill
        position["trade_id"]    = trade_id
        position.pop("entry_order_id",   None)
        position.pop("entry_order_time", None)
        position.pop("reprice_count",    None)
        position.pop("sig_id",           None)
        position.pop("intended_mid",     None)
        _save_position(position)
        _log(f"Entry fill confirmed: {symbol}  fill=${actual_fill}  trade_id={trade_id}")
        return True

    # ── Order dead ────────────────────────────────────────────────────────
    if state in ("cancelled", "failed", "rejected"):
        _log(f"Entry order {state} — no fill. Clearing pending position, returning to scan.")
        clear_option_position()
        return False

    # ── Still pending — check age ─────────────────────────────────────────
    order_age = 0.0
    if order_time_str:
        try:
            ts = datetime.fromisoformat(order_time_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            order_age = (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            pass

    _log(f"Entry order pending — age={order_age:.0f}s  reprice_count={reprice_count}")

    if order_age < ENTRY_FILL_TIMEOUT:
        _log("Within fill timeout window — waiting.")
        return False

    # ── Order stale: abort if reprice budget exhausted ────────────────────
    if reprice_count >= MAX_REPRICE_ATTEMPTS:
        _log(f"Max reprice attempts ({MAX_REPRICE_ATTEMPTS}) reached — cancelling and aborting entry.")
        cancel_option_order(token, order_id)
        clear_option_position()
        return False

    # ── Cancel and reprice toward ask ─────────────────────────────────────
    _log(f"Stale order ({order_age:.0f}s) — cancelling and repricing "
         f"(attempt {reprice_count + 1}/{MAX_REPRICE_ATTEMPTS})...")
    cancel_option_order(token, order_id)

    # Small wait: order might fill in the same moment as cancel
    time.sleep(3)
    try:
        recheck = get_option_order_status(token, order_id)
        if recheck["state"] == "filled":
            actual_fill = recheck["fill_price"] or position.get("intended_mid", 0)
            trade_id = open_trade(
                symbol, "buy", actual_fill, order_id, sig_id,
                trade_type="option", signal_dir=position.get("signal_dir", ""),
            )
            position["status"]      = "open"
            position["entry_price"] = actual_fill
            position["trade_id"]    = trade_id
            position.pop("entry_order_id",   None)
            position.pop("entry_order_time", None)
            position.pop("reprice_count",    None)
            position.pop("sig_id",           None)
            position.pop("intended_mid",     None)
            _save_position(position)
            _log(f"Filled during cancel window: {symbol} @ ${actual_fill}")
            return True
    except Exception:
        pass

    # Place new ask-price order
    try:
        new_order_id, new_price = reprice_entry(token, contract_id, symbol)
    except Exception as e:
        _log(f"ERROR repricing entry: {e} — aborting.")
        clear_option_position()
        return False

    position["entry_order_id"]   = new_order_id
    position["entry_order_time"] = datetime.now(timezone.utc).isoformat()
    position["reprice_count"]    = reprice_count + 1
    _save_position(position)
    _log(f"Repriced entry: new_order={new_order_id[:8] if new_order_id else '?'}  price=${new_price}")
    return False


def try_entry() -> bool:
    """
    Scan all watchlist symbols. Pick the highest-confidence signal.
    Validate with Claude. Run pre-trade kill switches. Place order.
    Saves a status='opening' pending position — fill confirmed in _check_entry_fill().
    Returns True if an order was placed (even if not yet filled).
    """
    # Don't enter if already holding or waiting for entry fill
    pos = load_position()
    if pos:
        _log(f"Already holding {pos['symbol']} (status={pos.get('status','open')}) — skipping entry scan.")
        return False

    # Refresh options flow scores once per day
    _refresh_flow_scores()

    _log(f"Scanning {len(WATCHLIST)} symbols: {', '.join(WATCHLIST)}")

    candidates = []
    for sym in WATCHLIST:
        sig = _scan_symbol(sym)
        if sig:
            candidates.append(sig)

    if not candidates:
        _log("No actionable signals across watchlist.")
        return False

    # Rank by flow-adjusted confidence — flow-aligned signals get a 25% boost
    for sig in candidates:
        sig["_adj_conf"] = adjusted_confidence(sig, _flow_scores)
        flow = _flow_scores.get(sig["symbol"], {}).get("flow_signal", "UNKNOWN")
        sig["_flow_signal"] = flow
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

    # Log all candidates; non-selected ones end as CANDIDATE (not traded)
    for sig in candidates[1:]:
        signal_log.append(sig, action="CANDIDATE",
                          action_reason="not selected (lower adj_conf)",
                          flow_signal=sig.get("_flow_signal", ""),
                          adj_conf=sig["_adj_conf"])

    sig_id = log_signal(best)

    # ── Claude validation pass 1 (signal only) ───────────────────────────
    _log(f"Asking Claude to validate {best['symbol']} {best['signal']}...")
    try:
        decision, reason = validate_signal(best)
    except Exception as e:
        _log(f"ERROR from Claude: {e}")
        signal_log.append(best, action="ERROR",
                          action_reason=f"Claude error pass1: {e}",
                          flow_signal=best.get("_flow_signal", ""),
                          adj_conf=best["_adj_conf"])
        return False

    log_decision(sig_id, "ENTRY", decision, reason)
    _log(f"Claude: {decision}  {reason}")

    if decision != "APPROVE":
        signal_log.append(best, action=decision,
                          action_reason=f"[pass1] {reason}",
                          flow_signal=best.get("_flow_signal", ""),
                          adj_conf=best["_adj_conf"])
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
        signal_log.append(best, action="ERROR",
                          action_reason=f"find_atm_option failed: {e}",
                          flow_signal=best.get("_flow_signal", ""),
                          adj_conf=best["_adj_conf"])
        return False

    # Enrich signal with option details for Claude's second pass
    best["option_type"]   = contract["option_type"]
    best["option_strike"] = contract["strike"]
    best["option_expiry"] = contract["expiry"]
    best["option_dte"]    = contract["dte"]
    best["option_cost"]   = contract["cost"]
    best["option_delta"]  = contract["delta"]
    best["option_iv"]     = contract["iv"]
    best["option_mid"]    = contract["mid_price"]

    # ── Claude validation pass 2 (with option cost / IV / delta) ─────────
    _log("Re-validating with Claude (option details included)...")
    try:
        decision, reason = validate_signal(best)
    except Exception as e:
        _log(f"ERROR from Claude: {e}")
        signal_log.append(best, action="ERROR",
                          action_reason=f"Claude error pass2: {e}",
                          flow_signal=best.get("_flow_signal", ""),
                          adj_conf=best["_adj_conf"])
        return False

    log_decision(sig_id, "ENTRY", decision, reason)
    _log(f"Claude: {decision}  {reason}")

    if decision != "APPROVE":
        signal_log.append(best, action=decision,
                          action_reason=f"[pass2] {reason}",
                          flow_signal=best.get("_flow_signal", ""),
                          adj_conf=best["_adj_conf"])
        return False

    # ── Pre-trade kill switches ───────────────────────────────────────────
    ok, kill_reason = pre_trade_checks(best)
    if not ok:
        _log(f"PRE-TRADE CHECK FAILED: {kill_reason}")
        log_decision(sig_id, "ENTRY", "REJECT", f"[kill switch] {kill_reason}")
        signal_log.append(best, action="KILL_SWITCH",
                          action_reason=kill_reason,
                          flow_signal=best.get("_flow_signal", ""),
                          adj_conf=best["_adj_conf"])
        return False
    _log("Pre-trade checks passed.")

    # ── Place GTC limit order at mid-price ───────────────────────────────
    _log("Placing entry order (GTC limit at mid)...")
    try:
        order_id, limit_price = place_option(best, contract)
    except Exception as e:
        _log(f"ERROR placing option: {e}")
        return False

    # ── Save pending position (status="opening") ──────────────────────────
    # open_trade() and full position.json are ONLY written after fill confirmed.
    pending = {
        "status":            "opening",
        "trade_type":        "option",
        "symbol":            best["symbol"],
        "side":              "buy",
        "signal_dir":        best["signal"],
        "option_type":       contract["option_type"],
        "contract_id":       contract["contract_id"],
        "strike":            contract["strike"],
        "expiry":            contract["expiry"],
        "dte":               contract["dte"],
        "quantity":          1,
        "intended_mid":      contract["mid_price"],
        "entry_order_id":    order_id,
        "entry_order_time":  datetime.now(timezone.utc).isoformat(),
        "reprice_count":     0,
        "sig_id":            sig_id,
        "entry_signal":      {k: v for k, v in best.items() if not k.startswith("_")},
    }
    _save_position(pending)

    signal_log.append(best, action="APPROVE",
                      action_reason=f"order_id={order_id[:8] if order_id else '?'}  limit=${limit_price}",
                      flow_signal=best.get("_flow_signal", ""),
                      adj_conf=best["_adj_conf"])

    _log(f"Entry order placed: {best['symbol']} {contract['option_type'].upper()} "
         f"${contract['strike']} exp {contract['expiry']}  "
         f"order_id={order_id[:8] if order_id else '?'}  limit=${limit_price}. "
         f"Waiting for fill confirmation...")
    return True


# ── Exit cycle ────────────────────────────────────────────────────────────────
#
# Two-phase state machine:
#
#   Phase 1 (position open, no pending close order):
#     - Get signal → Claude evaluate → hard risk checks
#     - If CLOSE: call close_option() → save order_id to position.json
#                 → mark DB status='closing' → return False (wait for fill)
#
#   Phase 2 (position has close_order_id):
#     - Poll get_option_order_status()
#     - If filled:   call close_trade() + clear position.json → return True
#     - If pending:  log "waiting for fill" → return False
#     - If failed:   remove close_order_id (retry next cycle) → return False
#
# This ensures: DB never says 'closed' before fill is confirmed,
#               position.json never erased on an unfilled GTC order.

def _save_position(pos: dict):
    """Write updated position dict back to position.json."""
    with open(POSITION_FILE, "w") as f:
        json.dump(pos, f, indent=2)


def _fetch_option_mid(contract_id: str) -> float | None:
    """Return current mid-price for a contract, or None on failure."""
    try:
        from config import get_robinhood_token as _tok
        from options_execute import _call as _oc
        t = _tok()
        r = _oc(t, "get_option_quotes", {"instrument_ids": [contract_id]})
        res = r.get("data", {}).get("results", [])
        q = res[0].get("quote", {}) if res else {}
        bid = float(q.get("bid_price") or 0)
        ask = float(q.get("ask_price") or 0)
        return round((bid + ask) / 2, 2) if bid and ask else None
    except Exception as e:
        _log(f"Warning: could not fetch option mid ({e})")
        return None


def try_exit(position: dict) -> bool:
    """
    Monitor and manage the open position.
    Returns True only when the position is fully closed (fill confirmed).
    """
    is_option = position.get("trade_type") == "option"
    symbol    = position.get("symbol", "?")
    trade_id  = position.get("trade_id")

    # ── Phase 2: pending close order — poll for fill ──────────────────────
    pending_order_id = position.get("close_order_id")
    if pending_order_id:
        _log(f"Checking fill status of close order {pending_order_id[:8]}... ({symbol})")
        try:
            from config import get_robinhood_token as _tok
            token = _tok()
            status = get_option_order_status(token, pending_order_id)
        except Exception as e:
            _log(f"WARNING: could not poll order status ({e}) — will retry next cycle.")
            return False

        state      = status["state"]
        fill_price = status["fill_price"]
        _log(f"Order state={state}  fill_price=${fill_price}  qty={status['filled_qty']}")

        if state == "filled":
            close_reason = position.get("close_reason", "close order filled")
            if trade_id:
                close_trade(trade_id, fill_price or position.get("close_mid", 0),
                            exit_order=pending_order_id, exit_reason=close_reason)
            clear_option_position()
            _log(f"Fill confirmed — {symbol} position closed @ ${fill_price}. DB updated.")
            return True

        elif state in ("cancelled", "failed", "rejected"):
            _log(f"Close order {state} — removing order_id, will retry next cycle.")
            position.pop("close_order_id", None)
            position.pop("close_mid", None)
            position.pop("close_reason", None)
            _save_position(position)
            return False

        else:
            # still queued / confirmed / partially_filled — wait
            _log(f"Close order still pending (state={state}) — checking again next cycle.")
            return False

    # ── Phase 1: evaluate whether to close ───────────────────────────────
    _log(f"Exit check: {symbol} {position.get('option_type','').upper()} "
         f"${position.get('strike','?')} exp {position.get('expiry','?')}")

    # Pull current signal
    try:
        signal = get_signal(symbol)
    except Exception as e:
        _log(f"ERROR getting exit signal: {e}")
        return False

    _log(f"Signal={signal['signal']}  Conf={signal['confidence']}%  "
         f"Vivek={signal['vivek_state']}  QTrend={signal['qtrend_state']}  "
         f"KF_vel={signal.get('kf_velocity', 0):.4f}")

    sig_id = log_signal(signal)

    # Claude evaluation
    try:
        action, reason = evaluate_exit(position, signal)
    except Exception as e:
        _log(f"ERROR from Claude exit evaluator: {e}")
        return False

    log_decision(sig_id, "EXIT", action, reason)
    _log(f"Claude exit: {action}  {reason}")

    # ── Hard risk controls (can override Claude HOLD → CLOSE) ────────────
    if is_option:
        # 1. DTE: don't hold into expiry week
        dte = days_to_expiry(position)
        _log(f"DTE remaining: {dte}")
        if dte <= 3:
            action = "CLOSE"
            reason = f"DTE={dte} — forced close to avoid expiry theta burn"
            _log(f"Hard close: {reason}")

        # 2. Stop loss: close if option lost >50% of entry premium
        if action != "CLOSE":
            current_mid = _fetch_option_mid(position.get("contract_id", ""))
            entry_price = position.get("entry_price", 0)
            if current_mid is not None and entry_price:
                _log(f"Option price: mid=${current_mid}  entry=${entry_price}")
                if current_mid < entry_price * 0.50:
                    loss_pct = (1 - current_mid / entry_price) * 100
                    action = "CLOSE"
                    reason = (f"Hard stop: option down {loss_pct:.0f}% "
                              f"(entry=${entry_price}, now=${current_mid})")
                    _log(f"Stop loss triggered: {reason}")

    # ── Execute close: place order, save order_id, wait for fill ─────────
    if action == "CLOSE":
        if is_option:
            _log("Placing close order...")
            try:
                order_id, mid = close_option(position)
            except Exception as e:
                _log(f"ERROR placing close order: {e}")
                return False

            # Save pending close state — position.json stays until fill confirmed
            position["close_order_id"] = order_id
            position["close_mid"]      = mid
            position["close_reason"]   = reason
            _save_position(position)

            if trade_id:
                mark_closing(trade_id, order_id)

            _log(f"Close order placed (order_id={order_id[:8] if order_id else 'unknown'}  "
                 f"mid=${mid}). Waiting for fill confirmation next cycle.")
            return False   # not closed yet — fill not confirmed

        else:
            # Stock position — synchronous close
            try:
                close_position(position)
                exit_price = float(signal.get("price") or 0)
            except Exception as e:
                _log(f"ERROR closing stock position: {e}")
                return False
            if trade_id:
                close_trade(trade_id, exit_price, exit_reason=reason)
            _log(f"Stock position closed @ ${exit_price}")
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

            # -- Check for existing position ------------------------------------------
            position = load_position()
            if position:
                sym    = position.get("symbol", "?")
                status = position.get("status", "open")

                if status == "opening":
                    # Phase 2 entry: waiting for fill confirmation
                    _log(f"Pending entry fill: {sym} "
                         f"{position.get('option_type','').upper()} "
                         f"${position.get('strike','?')} -- polling broker...")
                    filled = _check_entry_fill(position)
                    if filled:
                        _log(f"Entry confirmed for {sym} -- switching to exit monitor.")
                    else:
                        _log(f"Entry still pending or repriced -- rechecking in {MONITOR_INTERVAL}s.")
                    time.sleep(MONITOR_INTERVAL)
                    continue

                # status == "open" -- monitor for exit
                _log(f"Open position: {sym} {position.get('side','').upper()} "
                     f"@ ${position.get('entry_price', '?')}  "
                     f"{position.get('option_type','').upper()} ${position.get('strike','?')}"
                     f" exp {position.get('expiry','?')}")
                closed = try_exit(position)
                if closed:
                    _log(f"{sym} position closed -- back to scanning for entries.")
                time.sleep(MONITOR_INTERVAL)
                continue

            # -- No position -- scan all symbols for entry ----------------------------
            traded = try_entry()
            if traded:
                _log(f"Entry order placed. Polling for fill every {MONITOR_INTERVAL}s.")
                time.sleep(MONITOR_INTERVAL)
            else:
                _log(f"No trade. Next scan in {SIGNAL_INTERVAL//60} min.")
                time.sleep(SIGNAL_INTERVAL)

        except KeyboardInterrupt:
            _log("Daemon interrupted by user. Goodbye.")
            import sys
            sys.exit(0)

        except Exception:
            _log("UNHANDLED EXCEPTION -- will retry in 60s:")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    run()
