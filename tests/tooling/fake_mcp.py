"""A tiny MCP server for tests: stdio (default) or one JSON-RPC handler reused by an HTTP server."""

import json
import sys

TOOLS = [
    {
        "name": "company",
        "description": "Company sector by domain.",
        "inputSchema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": ["domain"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_company",
        "description": "Delete a company.",
        "inputSchema": {"type": "object", "properties": {"domain": {"type": "string"}}},
        "annotations": {"readOnlyHint": False},
    },
]
SECTORS = {"acme.com": "software", "beta.io": "logistics"}


def handle(msg):
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None  # notification
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        if name == "company":
            sector = SECTORS.get(args.get("domain"))
            result = {
                "content": [{"type": "text", "text": sector or "unknown domain"}],
                "isError": sector is None,
            }
        else:
            result = {"content": [{"type": "text", "text": "deleted"}]}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


if __name__ == "__main__":
    print("fake mcp server starting", flush=True)  # not JSON: the client must skip it
    for line in sys.stdin:
        if line.strip():
            out = handle(json.loads(line))
            if out is not None:
                print(json.dumps(out), flush=True)
