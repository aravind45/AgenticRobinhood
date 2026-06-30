# config.py — shared constants and token loader, imported by everything
import json
import os

ACCOUNT_NUMBER = "829569177"
MCP_URL = "https://agent.robinhood.com/mcp/trading"

# ── Watchlist ─────────────────────────────────────────────────────────────────
# Symbols scanned every cycle. Add/remove freely.
WATCHLIST = ["AMD", "NVDA", "META", "TSLA", "GOOGL"]

# Maximum concurrent open positions (1 = one trade at a time)
MAX_POSITIONS = 1


def get_robinhood_token() -> str:
    path = os.path.expanduser(r"~\.claude\.credentials.json")
    with open(path) as f:
        data = json.load(f)
    mcp_oauth = data.get("mcpOAuth", {})
    print(f"MCP OAuth servers found: {list(mcp_oauth.keys())}")
    for key, val in mcp_oauth.items():
        if "robinhood" in key.lower():
            print(f"Using token from: {key}")
            return val.get("access_token") or val.get("token") or val.get("accessToken")
    raise RuntimeError("No Robinhood token found in credentials")
