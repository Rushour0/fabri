"""Generic passthrough to any stdio MCP server, so an existing MCP toolset can
be used without writing a dedicated wrapper tool per server. Requires the
optional `mcp` package (`pip install mcp`) -- imported lazily so it's not a
hard dependency of the framework."""
import asyncio
import json
import sys


async def _call(server_command: list[str], tool_name: str, arguments: dict) -> list:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=server_command[0], args=server_command[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return [c.model_dump() if hasattr(c, "model_dump") else str(c) for c in result.content]


def main() -> int:
    args = json.loads(sys.stdin.read())
    try:
        content = asyncio.run(_call(args["server"], args["tool"], args.get("arguments", {})))
    except ImportError:
        print(json.dumps({"error": "the 'mcp' package is not installed -- pip install mcp"}))
        return 1
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps({"content": content}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
