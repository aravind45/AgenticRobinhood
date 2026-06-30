# discover_tools.py
# Lists all available Robinhood MCP tools and their parameters.

import json
import requests
from execute import get_robinhood_token

MCP_URL = "https://agent.robinhood.com/mcp/trading"

token = get_robinhood_token()
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

r = requests.post(
    MCP_URL,
    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    headers=headers,
)

data = json.loads(r.text.split("data: ", 1)[1])
tools = data.get("result", {}).get("tools", [])

print(f"\n{len(tools)} tools available on Robinhood MCP:\n")
for t in tools:
    print(f"  {t['name']}")
    desc = t.get("description", "")
    if desc:
        print(f"    {desc[:120]}")
    props = t.get("inputSchema", {}).get("properties", {})
    if props:
        required = t.get("inputSchema", {}).get("required", [])
        for p, schema in props.items():
            req = " *" if p in required else ""
            ptype = schema.get("type", "?")
            pdesc = schema.get("description", "")
            print(f"    [{ptype}{req}] {p}: {pdesc[:80]}")
    print()
