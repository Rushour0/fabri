"""A tiny stdio MCP server for tests: NDJSON JSON-RPC, four tools, no network."""
import json
import os
import sys

TOOLS = [
    {"name": "search_issues", "description": "Search issues by text.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "create_issue", "description": "Create an issue.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
    {"name": "archive_issue", "description": "Archive an issue.", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "send_message", "description": "Send a chat message.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
]


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            args = msg["params"].get("arguments") or {}
            text = json.dumps({"tool": msg["params"]["name"], "args": args, "token_set": bool(os.environ.get("FAKE_TOKEN"))})
            result = {"content": [{"type": "text", "text": text}]}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                         "error": {"code": -32601, "message": "unknown"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
