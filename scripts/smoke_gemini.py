#!/usr/bin/env python3
"""Live smoke test for the Gemini backend.

Drives the REAL agent loop (run_agent) through a Gemini API call that is forced
to make a tool call, then checks the things unit tests can't: that the tool
round-trips end to end and the run is priced.

The decisive signal is a per-run RANDOM token hidden in a sandboxed file: the
model can only echo it back if read_file was dispatched AND its result was fed
back to Gemini correctly. Since Gemini returns no tool-call id, fabri synthesizes
one and matches the function_response by NAME on the next turn -- if that mapping
were broken the model would never see the file contents and the token would be
missing. So "token present in the final answer" proves the whole round-trip.

Usage
-----
    export GEMINI_API_KEY=...
    uv run python scripts/smoke_gemini.py                  # default gemini-2.5-pro
    uv run python scripts/smoke_gemini.py --model gemini-2.5-flash
    uv run python scripts/smoke_gemini.py --mock           # no key: validate the harness only

Exit code is 0 on PASS, 1 on FAIL, 2 on setup/usage error. Requires a reachable
Qdrant (default http://localhost:6333; override with --qdrant-url).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

from fabri import (
    GeminiLLMBackend,
    QdrantMemoryStore,
    ScriptedLLMBackend,
    ToolRegistry,
    run_agent,
)
from fabri.core.llm import LLMResponse, ToolCall

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "src" / "fabri" / "tools" / "examples"
SECRET_FILE = "secret.txt"


def _build_tool_defs(registry: ToolRegistry) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema or {"type": "object"}}
        for t in registry.list()
    ]


def _make_backend(mock: bool, model: str, tool_defs: list[dict], token: str):
    if not mock:
        return GeminiLLMBackend(
            model=model, tools=tool_defs, max_tokens=1024, api_key_env="GEMINI_API_KEY"
        )
    # Harness self-check: a scripted backend that behaves like a correct Gemini
    # run -- one read_file tool call, then a final answer echoing the token it
    # "read". Exercises run_agent + tools + Qdrant + the assertions below, so the
    # only untested variable in live mode is the real API call.
    return ScriptedLLMBackend([
        LLMResponse(tool_call=ToolCall(name="read_file", args={"path": SECRET_FILE}, id="read_file-0")),
        LLMResponse(final_text=f"The secret token is {token}."),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="Live smoke test for the Gemini backend.")
    ap.add_argument("--model", default="gemini-2.5-pro", help="Gemini model id (default: gemini-2.5-pro)")
    ap.add_argument("--mock", action="store_true", help="validate the harness with a scripted backend (no API key)")
    ap.add_argument("--qdrant-url", default="http://localhost:6333", help="Qdrant URL")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox dir and Qdrant collection")
    args = ap.parse_args()

    if not args.mock and not os.environ.get("GEMINI_API_KEY"):
        print("FAIL: GEMINI_API_KEY is not set.\n"
              "      export GEMINI_API_KEY=... and re-run, or pass --mock to validate the harness.",
              file=sys.stderr)
        return 2

    mode = "MOCK (scripted backend)" if args.mock else f"LIVE (model={args.model})"
    token = f"FABRI-{uuid.uuid4().hex[:12].upper()}"
    print(f"== Gemini smoke test :: {mode} ==")
    print(f"   random token: {token}")

    sandbox = Path(tempfile.mkdtemp(prefix="fabri_smoke_"))
    (sandbox / SECRET_FILE).write_text(f"The one and only secret token is: {token}\n")
    # path-jail tools read FABRI_SANDBOX_ROOT at subprocess-spawn time.
    os.environ["FABRI_SANDBOX_ROOT"] = str(sandbox)

    collection = f"smoke_gemini_{uuid.uuid4().hex[:10]}"
    store = QdrantMemoryStore(url=args.qdrant_url, collection=collection)
    tools = ToolRegistry(EXAMPLES_DIR)
    tool_defs = _build_tool_defs(tools)

    backend = _make_backend(args.mock, args.model, tool_defs, token)

    task = (
        f"Read the file '{SECRET_FILE}' using the read_file tool, then reply with the "
        f"exact secret token it contains. Do not guess -- you must read the file."
    )

    ok = False
    try:
        result = run_agent(task, backend, tools, store, max_steps=5, result_format="toon")
        usage = result.get("usage") or {}
        final = result.get("final_text") or ""

        checks: list[tuple[str, bool, str]] = []
        checks.append(("outcome is success-ish",
                       result.get("outcome") in ("success", "success_with_recovery"),
                       f"outcome={result.get('outcome')}"))
        checks.append(("a tool step actually ran (step_count >= 2)",
                       usage.get("step_count", 0) >= 2,
                       f"step_count={usage.get('step_count')}"))
        checks.append(("token round-tripped into the final answer",
                       token in final,
                       f"final_text={final!r}"))
        if not args.mock:
            cost = usage.get("cost_usd")
            checks.append(("run was priced (cost_usd not None and > 0)",
                           cost is not None and cost > 0,
                           f"cost_usd={cost}"))
            by_model = usage.get("cost_by_model") or {}
            checks.append(("cost attributed to a gemini model",
                           any("gemini" in m for m in by_model),
                           f"cost_by_model={by_model}"))

        print("\n   checks:")
        for name, passed, detail in checks:
            print(f"     [{'PASS' if passed else 'FAIL'}] {name}  ({detail})")
        ok = all(p for _, p, _ in checks)

        print(f"\n   final answer: {final!r}")
        print(f"   usage: step_count={usage.get('step_count')} "
              f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
              f"cost_usd={usage.get('cost_usd')} total_cost_usd={usage.get('total_cost_usd')}")
    except Exception as e:  # noqa: BLE001 -- smoke test: surface any failure plainly
        print(f"\nFAIL: run_agent raised {type(e).__name__}: {e}", file=sys.stderr)
        ok = False
    finally:
        if not args.keep:
            try:
                store._client.delete_collection(collection)
            except Exception:  # noqa: BLE001 -- best-effort cleanup
                pass
            try:
                (sandbox / SECRET_FILE).unlink(missing_ok=True)
                sandbox.rmdir()
            except Exception:  # noqa: BLE001
                pass
        else:
            print(f"\n   (kept sandbox={sandbox} collection={collection})")

    print(f"\n== {'PASS' if ok else 'FAIL'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
