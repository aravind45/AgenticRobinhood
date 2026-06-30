# db.py
# SQLite logger for Project Alpha.
# Records every signal, Claude decision, and trade (entry + exit + P&L).
# Use db.py for later backtesting / evaluation.

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "alpha_trades.db")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    con = _conn()
    cur = con.cursor()

    # ── signals ───────────────────────────────────────────────────────────────
    # One row per trade_signal.get_signal() call
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,          -- UTC ISO timestamp
        symbol        TEXT NOT NULL,
        signal        TEXT NOT NULL,          -- BUY / SELL / NEUTRAL / BLOCKED
        confidence    INTEGER,
        vivek_state   INTEGER,
        qtrend_state  INTEGER,
        vivek_rsi2    REAL,
        vol_ratio     REAL,
        volume_zscore REAL,
        kf_velocity   REAL,
        channel_conv  INTEGER,                -- 1 or 0
        regime_state  TEXT,
        mr_belief     REAL,                   -- MEAN_REVERTING probability
        price         REAL,
        bid           REAL,
        ask           REAL,
        raw_json      TEXT                    -- full signal dict as JSON
    )""")

    # ── decisions ─────────────────────────────────────────────────────────────
    # One row per Claude validate_signal() / evaluate_exit() call
    cur.execute("""
    CREATE TABLE IF NOT EXISTS decisions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        signal_id   INTEGER REFERENCES signals(id),
        call_type   TEXT NOT NULL,            -- ENTRY or EXIT
        decision    TEXT NOT NULL,            -- APPROVE / REJECT / HOLD / CLOSE
        reason      TEXT
    )""")

    # ── trades ────────────────────────────────────────────────────────────────
    # One row per completed trade (updated when position closes)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT NOT NULL,
        side          TEXT NOT NULL,          -- buy / sell
        quantity      INTEGER DEFAULT 1,

        entry_ts      TEXT,
        entry_price   REAL,
        entry_order   TEXT,                   -- RH order ID
        entry_sig_id  INTEGER REFERENCES signals(id),

        exit_ts       TEXT,
        exit_price    REAL,
        exit_order    TEXT,
        exit_reason   TEXT,                   -- Claude reason or 'manual'

        pnl           REAL,                   -- (exit - entry) * qty * direction
        pnl_pct       REAL,
        status        TEXT DEFAULT 'open'     -- open / closed
    )""")

    con.commit()
    con.close()


# ── Signal logging ─────────────────────────────────────────────────────────────

def log_signal(signal: dict) -> int:
    """Insert a signal row, return its id."""
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO signals
            (ts, symbol, signal, confidence, vivek_state, qtrend_state,
             vivek_rsi2, vol_ratio, volume_zscore, kf_velocity,
             channel_conv, regime_state, mr_belief, price, bid, ask, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        signal.get("symbol"),
        signal.get("signal"),
        signal.get("confidence"),
        signal.get("vivek_state"),
        signal.get("qtrend_state"),
        signal.get("vivek_rsi2"),
        signal.get("vol_ratio"),
        signal.get("volume_zscore"),
        signal.get("kf_velocity"),
        1 if signal.get("channel_converging") else 0,
        signal.get("regime_state"),
        signal.get("regime_belief", {}).get("MEAN_REVERTING"),
        signal.get("price"),
        signal.get("bid"),
        signal.get("ask"),
        json.dumps(signal, default=str),
    ))
    sig_id = cur.lastrowid
    con.commit()
    con.close()
    return sig_id


# ── Decision logging ───────────────────────────────────────────────────────────

def log_decision(signal_id: int, call_type: str, decision: str, reason: str = "") -> int:
    """Log a Claude decision (ENTRY or EXIT). Returns decision id."""
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO decisions (ts, signal_id, call_type, decision, reason)
        VALUES (?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), signal_id, call_type, decision, reason))
    dec_id = cur.lastrowid
    con.commit()
    con.close()
    return dec_id


# ── Trade entry / exit ─────────────────────────────────────────────────────────

def open_trade(symbol: str, side: str, entry_price: float,
               order_id: str, signal_id: int) -> int:
    """Record a newly opened trade. Returns trade id."""
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO trades
            (symbol, side, entry_ts, entry_price, entry_order, entry_sig_id, status)
        VALUES (?,?,?,?,?,?,'open')
    """, (symbol, side, datetime.now(timezone.utc).isoformat(),
          entry_price, order_id, signal_id))
    trade_id = cur.lastrowid
    con.commit()
    con.close()
    return trade_id


def close_trade(trade_id: int, exit_price: float,
                exit_order: str = "", exit_reason: str = ""):
    """Mark a trade as closed and compute P&L."""
    con = _conn()
    cur = con.cursor()
    row = con.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        con.close()
        return

    direction = 1 if row["side"] == "buy" else -1
    pnl     = (exit_price - row["entry_price"]) * row["quantity"] * direction
    pnl_pct = pnl / (row["entry_price"] * row["quantity"]) * 100 if row["entry_price"] else 0

    cur.execute("""
        UPDATE trades SET
            exit_ts=?, exit_price=?, exit_order=?, exit_reason=?,
            pnl=?, pnl_pct=?, status='closed'
        WHERE id=?
    """, (datetime.now(timezone.utc).isoformat(), exit_price,
          exit_order, exit_reason, round(pnl, 4), round(pnl_pct, 4), trade_id))
    con.commit()
    con.close()


def get_open_trade(symbol: str) -> dict | None:
    """Return the most recent open trade for a symbol, or None."""
    con = _conn()
    row = con.execute(
        "SELECT * FROM trades WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


# ── Summary queries ────────────────────────────────────────────────────────────

def print_summary(symbol: str = "SPY", last_n: int = 20):
    """Print a quick P&L summary to stdout."""
    con = _conn()
    rows = con.execute("""
        SELECT side, entry_price, exit_price, pnl, pnl_pct, status,
               entry_ts, exit_ts, exit_reason
        FROM trades WHERE symbol=? ORDER BY id DESC LIMIT ?
    """, (symbol, last_n)).fetchall()

    print(f"\n{'='*60}")
    print(f"  Project Alpha — Trade History ({symbol}, last {last_n})")
    print(f"{'='*60}")
    total_pnl = 0.0
    wins = losses = 0
    for r in rows:
        if r["status"] == "closed":
            pnl = r["pnl"] or 0
            total_pnl += pnl
            wins   += 1 if pnl > 0 else 0
            losses += 1 if pnl < 0 else 0
            tag = "✓" if pnl > 0 else "✗"
            print(f"  {tag} {r['side'].upper():4s}  entry={r['entry_price']:.2f}  "
                  f"exit={r['exit_price']:.2f}  P&L=${pnl:+.2f} ({r['pnl_pct']:+.2f}%)"
                  f"  [{r['exit_reason'] or ''}]")
        else:
            print(f"  → OPEN  {r['side'].upper():4s}  entry={r['entry_price']:.2f}  "
                  f"opened {r['entry_ts'][:10]}")

    closed = wins + losses
    if closed:
        print(f"{'─'*60}")
        print(f"  Closed: {closed}   Wins: {wins}   Losses: {losses}   "
              f"Win%: {wins/closed*100:.0f}%   Total P&L: ${total_pnl:+.2f}")
    print(f"{'='*60}\n")
    con.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready at: {DB_PATH}")
    print_summary()
