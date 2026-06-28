"""B2 -- tool-writer: scaffold a *tightened* tool, validate it, and run it
locally, with no hand-written JSON or ``echo | python`` plumbing.

Three operations, surfaced under the CLI's ``tool`` subcommand group:

- :func:`new_tool` -- ``fabri tool new``. Build a manifest + executable stub.
  * ``--from-signature <file.py>`` parses the first top-level function with the
    stdlib ``ast`` module and maps its typed parameters to a real
    ``input_schema`` (required = params without defaults) plus a generated stub
    that reads stdin JSON and calls the function. NO LLM, fully deterministic.
  * ``--from "<desc>"`` sets the description and a sane default object schema,
    optionally enriched by an LLM when one is supplied (the CLI only builds a
    backend when an API key is present; no key -> deterministic fallback).
  * neither flag -> today's ``tool init`` behaviour with the tightened scaffold.
- :func:`validate_manifest` -- ``fabri tool validate``. Check the manifest shape,
  that both schemas are valid JSON-Schema-subset (the same subset
  ``core/structured.py`` understands), and that the command's script resolves.
- :func:`test_tool` -- ``fabri tool test``. Run the tool through the existing
  ``ToolRegistry`` / sandbox and return the normalized ``{ok, result?, error?}``
  envelope.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from fabri.core import structured
from fabri.core.logging_setup import get_logger
from fabri.tool_scaffold import write_tool
from fabri.tools.manifest_schema import ToolManifest, _is_path_shaped

logger = get_logger()

# JSON-Schema-subset type tokens, sourced from the one validator we ship so the
# tool-writer and the agent loop never drift on what "valid schema" means.
_SUPPORTED_TYPES = set(structured._TYPE_CHECKS)

# Python annotation base name -> JSON-Schema type. Anything unmapped (custom
# classes, unannotated params) becomes an untyped `{}` property -- a tightened
# schema asserts only what it can prove from the signature.
_ANNOTATION_TYPES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "bytes": "string",
    "list": "array",
    "List": "array",
    "tuple": "array",
    "Tuple": "array",
    "set": "array",
    "Set": "array",
    "Sequence": "array",
    "dict": "object",
    "Dict": "object",
    "Mapping": "object",
}

# Subscript bases that just wrap an inner type (Optional[int], Union[int, None]).
_UNWRAP_BASES = {"Optional", "Union"}


def _default_object_schema() -> dict:
    """The tightened (not opaque) default: an object whose properties the user
    fills in, rather than a bare ``{}`` that asserts nothing."""
    return {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# --from-signature: ast -> input/output schema + a calling stub
# ---------------------------------------------------------------------------


def _annotation_base_name(node: ast.AST | None) -> str | None:
    """Best-effort base name of an annotation node: ``int`` -> "int",
    ``list[str]`` -> "list", ``typing.List[int]`` -> "List". Returns None when
    there's no annotation we can read."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):  # typing.List -> "List"
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_base_name(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # PEP 563 / quoted annotation -- parse the inner string.
        try:
            inner = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return node.value
        return _annotation_base_name(inner)
    return None


def _annotation_to_schema(node: ast.AST | None) -> dict:
    """Map a parameter/return annotation node to a JSON-Schema-subset fragment.
    Unwraps ``Optional[...]`` / ``Union[..., None]`` to the inner type so an
    optional int still types as integer. Unknown types -> ``{}`` (no assertion).
    """
    base = _annotation_base_name(node)
    if base in _UNWRAP_BASES and isinstance(node, ast.Subscript):
        return _annotation_to_schema(_first_subscript_element(node))
    if base in _ANNOTATION_TYPES:
        return {"type": _ANNOTATION_TYPES[base]}
    return {}


def _first_subscript_element(node: ast.Subscript) -> ast.AST | None:
    """The first meaningful type arg of a subscript, skipping ``None`` in a
    Union/Optional (``Optional[int]`` -> the ``int`` node)."""
    sl = node.slice
    elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
    for elt in elts:
        if isinstance(elt, ast.Constant) and elt.value is None:
            continue
        return elt
    return None


def _first_function(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise ValueError("no top-level function found in the signature file")


def parse_signature(path: Path) -> dict:
    """Parse the FIRST top-level function in `path` and derive a tool spec.

    Returns a dict with:
      - ``func_name``: the function's name
      - ``input_schema``: object schema (one property per param; ``required`` =
        params without a default)
      - ``output_schema``: ``{"type": "object", "properties": {"result": ...}}``
        where ``result`` carries the mapped return type
      - ``stub``: a Python executable that reads stdin JSON, calls the function,
        and prints ``{"result": <return>}``
    """
    source = Path(path).read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ValueError(f"could not parse {path}: {e}") from e

    func = _first_function(tree)
    func_src = ast.get_source_segment(source, func) or ""

    # Collect positional params (posonly + normal) with their defaults aligned
    # to the tail, then keyword-only params. *args/**kwargs are ignored -- a
    # tool's JSON args map cleanly only to named parameters.
    positional = list(func.args.posonlyargs) + list(func.args.args)
    pos_defaults = list(func.args.defaults)
    n_required_pos = len(positional) - len(pos_defaults)

    properties: dict[str, dict] = {}
    required: list[str] = []
    call_parts: list[str] = []

    for i, arg in enumerate(positional):
        has_default = i >= n_required_pos
        _add_param(arg, has_default, properties, required, call_parts)

    for arg, default in zip(func.args.kwonlyargs, func.args.kw_defaults):
        _add_param(arg, default is not None, properties, required, call_parts)

    input_schema = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required

    output_schema = {
        "type": "object",
        "properties": {"result": _annotation_to_schema(func.returns)},
    }

    stub = _SIGNATURE_STUB.format(
        func_name=func.name,
        func_src=func_src,
        call_args=", ".join(call_parts),
    )
    return {
        "func_name": func.name,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "stub": stub,
    }


def _add_param(
    arg: ast.arg,
    has_default: bool,
    properties: dict,
    required: list,
    call_parts: list,
) -> None:
    properties[arg.arg] = _annotation_to_schema(arg.annotation)
    if has_default:
        call_parts.append(f'{arg.arg}=args.get("{arg.arg}")')
    else:
        required.append(arg.arg)
        call_parts.append(f'{arg.arg}=args["{arg.arg}"]')


_SIGNATURE_STUB = '''\
"""fabri tool generated by `fabri tool new --from-signature`.

Reads one JSON object from stdin, calls {func_name}(...), and prints
{{"result": <return value>}} as one JSON object on stdout."""
import json
import sys


{func_src}


def main() -> int:
    args = json.loads(sys.stdin.read())
    result = {func_name}({call_args})
    print(json.dumps({{"result": result}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------------------------------------------------------------------------
# --from "<desc>": optional LLM schema enrichment with a deterministic fallback
# ---------------------------------------------------------------------------

_ENRICH_SYSTEM = (
    "You design JSON-Schema-subset schemas for a small tool. Reply with ONE "
    "JSON object: {\"input_schema\": {...}, \"output_schema\": {...}}. Each "
    "schema must be an object schema using only these keys: type, properties, "
    "required, items, enum. Allowed types: object, array, string, integer, "
    "number, boolean, null. No prose, no code fences."
)

# Shape the model's reply must satisfy before we trust it.
_ENRICH_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    },
    "required": ["input_schema", "output_schema"],
}


def _enrich_schemas(name: str, description: str, llm) -> tuple[dict, dict]:
    """Ask `llm` for tightened input/output schemas. Returns the default object
    schemas on any failure (no backend, network/SDK error, malformed or invalid
    reply) -- enrichment is always best-effort and never required."""
    if llm is None:
        return _default_object_schema(), _default_object_schema()
    prompt = (
        f"Tool name: {name}\nDescription: {description}\n"
        "Design input_schema (the args the tool reads) and output_schema "
        "(the object it prints)."
    )
    try:
        resp = llm.step(_ENRICH_SYSTEM, [{"role": "user", "content": prompt}])
    except Exception as e:  # network/SDK/anything -- degrade, don't crash
        logger.warning("tool new: LLM schema enrichment failed (%s); using defaults", e)
        return _default_object_schema(), _default_object_schema()

    value, errors = structured.parse_response(resp.final_text or "", _ENRICH_REPLY_SCHEMA)
    if errors or not isinstance(value, dict):
        logger.warning("tool new: LLM reply not usable (%s); using defaults", errors or "no JSON")
        return _default_object_schema(), _default_object_schema()

    in_schema = value["input_schema"]
    out_schema = value["output_schema"]
    if check_schema(in_schema) or check_schema(out_schema):
        logger.warning("tool new: LLM produced an invalid schema; using defaults")
        return _default_object_schema(), _default_object_schema()
    logger.info("tool new: enriched schemas for %r via LLM", name)
    return in_schema, out_schema


# ---------------------------------------------------------------------------
# new_tool: the `fabri tool new` entry point
# ---------------------------------------------------------------------------


def new_tool(
    name: str,
    *,
    lang: str = "python",
    from_signature: str | None = None,
    from_desc: str | None = None,
    target_dir: str | Path = "tools/agent_tools",
    timeout_s: float = 10.0,
    force: bool = False,
    llm=None,
) -> dict:
    """Scaffold a schema-tightened tool. Exactly one mode is chosen by the
    flags: `from_signature` (deterministic, Python-only), `from_desc`
    (optionally LLM-enriched), or neither (the tightened default scaffold).
    Returns the {"created", "skipped", "language", "name"} dict from
    `write_tool`, plus a "mode" key describing which path ran.
    """
    target = Path(target_dir)

    if from_signature is not None:
        if lang != "python":
            raise ValueError("--from-signature only supports --lang python")
        spec = parse_signature(Path(from_signature))
        description = (
            f"Tool {name} -- generated from {spec['func_name']}() in "
            f"{Path(from_signature).name}. Describe what it does."
        )
        result = write_tool(
            name, "python", target,
            description=description,
            input_schema=spec["input_schema"],
            output_schema=spec["output_schema"],
            stub_source=spec["stub"],
            timeout_s=timeout_s,
            force=force,
        )
        result["mode"] = "from-signature"
        return result

    if from_desc is not None:
        in_schema, out_schema = _enrich_schemas(name, from_desc, llm)
        result = write_tool(
            name, lang, target,
            description=from_desc,
            input_schema=in_schema,
            output_schema=out_schema,
            timeout_s=timeout_s,
            force=force,
        )
        result["mode"] = "from-desc"
        return result

    # Default: today's `tool init` shape, but with a tightened (object +
    # empty properties) schema instead of an opaque `{}`.
    result = write_tool(
        name, lang, target,
        description=f"Tool {name} -- describe what this does in one sentence.",
        input_schema=_default_object_schema(),
        output_schema=_default_object_schema(),
        timeout_s=timeout_s,
        force=force,
    )
    result["mode"] = "default"
    return result


# ---------------------------------------------------------------------------
# validate_manifest: the `fabri tool validate` entry point
# ---------------------------------------------------------------------------


def check_schema(schema: object, path: str = "$") -> list[str]:
    """Return human-readable errors if `schema` is not a valid JSON-Schema-
    subset schema (the subset `core/structured.py` validates against): only
    object/array/string/integer/number/boolean/null types, with `properties`,
    `required`, `items`, and `enum` recursing. Empty list == valid."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object, got {type(schema).__name__}"]

    t = schema.get("type")
    if t is not None:
        tokens = t if isinstance(t, list) else [t]
        for tok in tokens:
            if tok not in _SUPPORTED_TYPES:
                errors.append(
                    f"{path}.type: {tok!r} is not a supported type "
                    f"({sorted(_SUPPORTED_TYPES)})"
                )

    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            errors.append(f"{path}.properties: must be an object")
        else:
            for key, sub in props.items():
                errors.extend(check_schema(sub, f"{path}.properties.{key}"))

    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(k, str) for k in required)
    ):
        errors.append(f"{path}.required: must be a list of property-name strings")

    items = schema.get("items")
    if items is not None:
        errors.extend(check_schema(items, f"{path}.items"))

    enum = schema.get("enum")
    if enum is not None and not isinstance(enum, list):
        errors.append(f"{path}.enum: must be a list")

    return errors


def validate_manifest(path: str | Path) -> tuple[bool, list[str]]:
    """Validate a tool manifest at `path`. Returns ``(ok, lines)`` where `lines`
    is a human-readable pass/fail report. Checks the manifest shape, that both
    schemas are valid JSON-Schema-subset, and that the command's script file
    resolves on disk."""
    path = Path(path)
    lines: list[str] = []
    ok = True

    def fail(msg: str) -> None:
        nonlocal ok
        ok = False
        lines.append(f"FAIL  {msg}")

    if not path.is_file():
        return False, [f"FAIL  manifest not found: {path}"]
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return False, [f"FAIL  manifest is not valid JSON: {e}"]
    if not isinstance(data, dict):
        return False, ["FAIL  manifest must be a JSON object"]

    # Required string fields.
    for field in ("name", "description"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            fail(f"{field!r} must be a non-empty string")
        else:
            lines.append(f"ok    {field}: {data[field][:60]!r}")

    # command: non-empty list of strings.
    command = data.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        fail("'command' must be a non-empty list of strings")
        command = []
    else:
        lines.append(f"ok    command: {command}")

    # timeout_s: optional positive number.
    timeout = data.get("timeout_s", 10.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        fail("'timeout_s' must be a positive number")
    else:
        lines.append(f"ok    timeout_s: {timeout}")

    # Schemas: must be objects and valid JSON-Schema-subset.
    for field in ("input_schema", "output_schema"):
        schema = data.get(field, {})
        if not isinstance(schema, dict):
            fail(f"{field!r} must be an object")
            continue
        schema_errors = check_schema(schema, field)
        if schema_errors:
            for err in schema_errors:
                fail(err)
        else:
            lines.append(f"ok    {field}: valid JSON-Schema-subset")

    # Command's script file must resolve relative to the manifest dir.
    script_tokens = [c for c in command if _is_path_shaped(c)]
    if not script_tokens:
        lines.append("ok    command: no script-path token to resolve (bare executable)")
    for tok in script_tokens:
        resolved = (path.parent / tok)
        if Path(tok).is_absolute() and Path(tok).is_file():
            lines.append(f"ok    script resolves: {tok}")
        elif resolved.is_file():
            lines.append(f"ok    script resolves: {resolved}")
        else:
            fail(f"command script not found: {tok} (looked in {path.parent})")

    lines.append("")
    lines.append("PASS" if ok else "FAILED")
    return ok, lines


# ---------------------------------------------------------------------------
# test_tool: the `fabri tool test` entry point
# ---------------------------------------------------------------------------


def test_tool(
    name: str,
    args: dict | None = None,
    target_dir: str | Path = "tools/agent_tools",
) -> dict:
    """Load the tool `name` from `target_dir` and run it through the existing
    `ToolRegistry` (LocalSandbox -> tools/runner.run_tool). Returns the
    normalized ``{ok, result?, error?, stderr?}`` envelope. Raises ValueError
    if the manifest isn't found so the CLI can report it cleanly."""
    from fabri.tools.registry import ToolRegistry

    directory = Path(target_dir)
    manifest_path = directory / f"{name}.json"
    if not manifest_path.is_file():
        raise ValueError(f"no manifest for tool {name!r} at {manifest_path}")

    registry = ToolRegistry(directory)
    if name not in registry.tools:
        raise ValueError(
            f"manifest {manifest_path} does not declare a tool named {name!r}"
        )
    return registry.invoke(name, args or {})
