# risk_checks.py
# Account-level kill switches — called before every new entry order.
#
# These are hard gates that cannot be overridden by Claude or signal confidence.
# If any check fails, no new trade is placed that cycle.

from datetime import date
from db import _conn

# ── Thresholds (tune as you gather data) ─────────────────────────────────────
MAX_DAILY_LOSS      = -150.0   # $ — halt new entries if today's closed P&L < this
MAX_CONSEC_LOSSES   = 3        # halt after N consecutive losing trades
MIN_BUYING_POWER    = 200.0    # $ — reject if buying power below this
MAX_OPTION_COST_PCT = 0.60     # reject if option_cost > buying_power × this


def today_realized_pnl() -> float:
    """Sum of today's closed-trade P&L from the DB."""
    try:
        con = _conn()
        today = date.today().isoformat()
        row = con.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE status='closed' AND exit_ts >= ?",
            (today + "T00:00:00",)
        ).fetchone()
        con.close()
        return float(row[0] or 0)
    except Exception:
        return 0.0


def consecutive_losses() -> int:
    """Count of consecutive losing closed trades (most recent first)."""
    try:
        con = _conn()
        rows = con.execute(
            "SELECT pnl FROM trades WHERE status='closed' ORDER BY id DESC LIMIT 10"
        ).fetchall()
        con.close()
        count = 0
        for r in rows:
            if (r[0] or 0) < 0:
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def pre_trade_checks(signal: dict) -> tuple[bool, str]:
    """
    Run all account-level kill switches before placing a new entry.
    Returns (ok, reason). If ok is False, skip this trade.

    Checks (in order):
      1. Buying power known and above minimum
      2. Option cost does not exceed configured % of buying power
      3. Today's realized losses haven't hit the daily limit
      4. Last N closed trades aren't all losses (cooling-off rule)
    """
    bp          = signal.get("buying_power")
    option_cost = signal.get("option_cost", 0)

    # 1. Buying power
    if bp is None:
        return False, "Buying power unknown — cannot size risk, skipping trade"
    if bp < MIN_BUYING_POWER:
        return False, (f"Buying power ${bp:.0f} below minimum ${MIN_BUYING_POWER:.0f}")

    # 2. Option cost vs buying power
    if option_cost and bp and option_cost > bp * MAX_OPTION_COST_PCT:
        pct = option_cost / bp * 100
        return False, (f"Option cost ${option_cost:.0f} = {pct:.0f}% of buying power ${bp:.0f} "
                       f"(limit {MAX_OPTION_COST_PCT * 100:.0f}%) — too much capital at risk")

    # 3. Daily loss limit
    today_pnl = today_realized_pnl()
    if today_pnl < MAX_DAILY_LOSS:
        return False, (f"Daily loss limit hit: ${today_pnl:.0f} "
                       f"(limit ${MAX_DAILY_LOSS:.0f}) — no new trades today")

    # 4. Consecutive losses (cooling-off)
    consec = consecutive_losses()
    if consec >= MAX_CONSEC_LOSSES:
        return False, (f"{consec} consecutive losses — cooling off, "
                       f"no new entries until a winner or manual reset")

    return True, "ok"
