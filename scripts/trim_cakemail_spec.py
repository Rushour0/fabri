"""Trim the full Cakemail OpenAPI document to the part the connector ships.

    python scripts/trim_cakemail_spec.py SOURCE.json [DEST.json]

DEST defaults to src/fabri/tools/connector_catalog/specs/cakemail.json.

Kept: every GET, POST and PATCH operation whose operationId survives the
connector's deny list and whose path does not start with /token, /users,
/accounts or /brands and does not contain "pats". The two sender reads
under /brands/default/senders are the one exception. Non-GET operations
get ``x-fabri-approval: always_approve``. ``components.schemas`` is pruned
to the schemas the kept operations reach through ``$ref``, and
``components.securitySchemes`` is kept because every operation names it.
Top level keys other than openapi, info, servers and components are
dropped, and ``x-fabri-trimmed`` records the source version.

The output is compact JSON with sorted keys and a trailing newline, so the
same input always gives the same bytes.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import sys
from pathlib import Path
from typing import Any

from fabri.tools.connector_catalog.cakemail import (
    DENY,
    DROP_PATH_PARTS,
    DROP_PATH_PREFIXES,
    KEEP_UNDER_DROPPED,
    TRIM_METHODS,
)

DEFAULT_DEST = Path(__file__).resolve().parent.parent / "src/fabri/tools/connector_catalog/specs/cakemail.json"
SCRIPT = "scripts/trim_cakemail_spec.py"


def keep_operation(method: str, path: str, op_id: str) -> bool:
    method = method.upper()
    if method not in TRIM_METHODS:
        return False
    if any(fnmatch.fnmatchcase(op_id, pattern) for pattern in DENY):
        return False
    if (method, path) in KEEP_UNDER_DROPPED:
        return True
    if path.startswith(DROP_PATH_PREFIXES):
        return False
    return not any(part in path for part in DROP_PATH_PARTS)


def _refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.add(ref)
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def _schema_closure(paths: dict, schemas: dict) -> dict:
    prefix = "#/components/schemas/"
    pending: set[str] = set()
    _refs(paths, pending)
    kept: dict[str, Any] = {}
    while pending:
        ref = pending.pop()
        if not ref.startswith(prefix):
            raise SystemExit(f"unsupported $ref {ref}")
        name = ref[len(prefix):]
        if name in kept:
            continue
        if name not in schemas:
            raise SystemExit(f"$ref {ref} does not resolve")
        kept[name] = schemas[name]
        found: set[str] = set()
        _refs(schemas[name], found)
        pending |= {r for r in found if r[len(prefix):] not in kept}
    return kept


def trim(doc: dict) -> dict:
    paths: dict[str, Any] = {}
    for path, item in (doc.get("paths") or {}).items():
        kept_item: dict[str, Any] = {}
        for method, op in item.items():
            if not isinstance(op, dict) or "operationId" not in op:
                continue
            if not keep_operation(method, path, str(op["operationId"])):
                continue
            op = copy.deepcopy(op)
            if method.upper() != "GET":
                op["x-fabri-approval"] = "always_approve"
            kept_item[method] = op
        if kept_item:
            if "parameters" in item:
                kept_item["parameters"] = copy.deepcopy(item["parameters"])
            paths[path] = kept_item
    components = doc.get("components") or {}
    out_components: dict[str, Any] = {"schemas": _schema_closure(paths, components.get("schemas") or {})}
    if "securitySchemes" in components:
        out_components["securitySchemes"] = copy.deepcopy(components["securitySchemes"])
    return {
        "openapi": doc["openapi"],
        "info": copy.deepcopy(doc["info"]),
        "servers": copy.deepcopy(doc.get("servers") or []),
        "paths": paths,
        "components": out_components,
        "x-fabri-trimmed": {"from": str(doc["info"]["version"]), "script": SCRIPT},
    }


def dump(doc: dict) -> str:
    import json

    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trim the Cakemail OpenAPI document for the connector.")
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path, nargs="?", default=DEFAULT_DEST)
    args = parser.parse_args(argv)
    import json

    doc = json.loads(args.source.read_text(encoding="utf-8"))
    text = dump(trim(doc))
    args.dest.parent.mkdir(parents=True, exist_ok=True)
    args.dest.write_text(text, encoding="utf-8")
    ops = sum(len([m for m in item if m != "parameters"]) for item in trim(doc)["paths"].values())
    print(f"wrote {args.dest} ({len(text)} bytes, {ops} operations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
