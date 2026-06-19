"""TOON (Token-Oriented Object Notation): a compact, indentation-based encoding
of JSON-shaped data that costs fewer LLM tokens than JSON -- no braces, no
repeated keys for uniform arrays (a single header row instead), no per-string
quoting unless a value is ambiguous.

The framework uses it on both sides of the model: JSON tool results are encoded
to TOON before they enter the context (`encode`), and structured data the model
emits as TOON is decoded back to JSON for downstream consumers (`decode`). The
two are exact inverses for any JSON value whose top level is an object or array
(always true for tool results) -- see tests/test_toon.py.

Dialect (comma-delimited):
  - scalars: null / true / false / numbers bare; strings bare unless they are
    empty, padded, contain a comma/quote/newline/tab, start with a structural
    marker ([ { - # "), or would be mistaken for a number/bool/null -- then
    JSON-double-quoted.
  - object:        key: value           (nested object: `key:` then an indented block)
  - primitive arr: key[3]: a,b,c
  - uniform table: key[2]{id,name}:      (every element a flat object, same keys)
                     1,Alice
                     2,Bob
  - other arrays:  key[2]:               (one `- ` item per element)
                     - ...
                     - ...
Indentation is two spaces per level. The top-level value must be an object or
array.
"""
import json
import re

__all__ = ["encode", "decode"]

_INDENT = "  "
_ARRAY_HEADER = re.compile(r"^\[(\d+)\](?:\{([^}]*)\})?:(.*)$")


# --------------------------------------------------------------------------- #
# encode
# --------------------------------------------------------------------------- #
def encode(obj) -> str:
    """Encode a JSON-shaped value (dict/list at the top level) as TOON text."""
    if not isinstance(obj, (dict, list)):
        raise ValueError("TOON top-level value must be an object or array")
    lines: list[str] = []
    if isinstance(obj, dict):
        _emit_members(lines, 0, obj)
    else:
        _emit_array(lines, 0, "", obj)
    return "\n".join(lines)


def _emit_members(lines: list[str], indent: int, d: dict) -> None:
    pad = _INDENT * indent
    for key, value in d.items():
        kk = _enc_key(key)
        if isinstance(value, dict):
            if value:
                lines.append(f"{pad}{kk}:")
                _emit_members(lines, indent + 1, value)
            else:
                lines.append(f"{pad}{kk}:")  # empty object: header with no children
        elif isinstance(value, list):
            _emit_array(lines, indent, kk, value)
        else:
            lines.append(f"{pad}{kk}: {_enc_scalar(value)}")


def _emit_array(lines: list[str], indent: int, prefix: str, lst: list) -> None:
    pad = _INDENT * indent
    n = len(lst)
    if n == 0:
        lines.append(f"{pad}{prefix}[0]:")
        return
    if all(_is_primitive(x) for x in lst):
        lines.append(f"{pad}{prefix}[{n}]: " + ",".join(_enc_scalar(x) for x in lst))
        return
    fields = _tabular_fields(lst)
    if fields is not None:
        lines.append(f"{pad}{prefix}[{n}]{{{','.join(fields)}}}:")
        row_pad = _INDENT * (indent + 1)
        for row in lst:
            lines.append(row_pad + ",".join(_enc_scalar(row[f]) for f in fields))
        return
    lines.append(f"{pad}{prefix}[{n}]:")
    for item in lst:
        _emit_item(lines, indent + 1, item)


def _emit_item(lines: list[str], indent: int, item) -> None:
    pad = _INDENT * indent
    if isinstance(item, dict):
        if item:
            lines.append(f"{pad}-")
            _emit_members(lines, indent + 1, item)
        else:
            lines.append(f"{pad}-")  # empty object element
    elif isinstance(item, list):
        _emit_array(lines, indent, "- ", item)  # "- [N]..." header
    else:
        lines.append(f"{pad}- {_enc_scalar(item)}")


def _is_primitive(v) -> bool:
    return v is None or isinstance(v, (bool, int, float, str))


def _tabular_fields(lst: list) -> list | None:
    """Field order if `lst` is a non-empty array of flat objects that all share
    the same (quote-free) key set -- the condition for the compact table form."""
    if not all(isinstance(x, dict) for x in lst):
        return None
    first = list(lst[0].keys())
    if not first:
        return None
    keyset = set(first)
    for x in lst:
        if set(x.keys()) != keyset or not all(_is_primitive(v) for v in x.values()):
            return None
    if any(_key_needs_quote(k) for k in first):
        return None
    return first


def _enc_scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return _enc_str(v)


def _enc_str(s: str) -> str:
    return json.dumps(s) if _value_needs_quote(s) else s


def _enc_key(k) -> str:
    if not isinstance(k, str):
        k = str(k)
    return json.dumps(k) if _key_needs_quote(k) else k


def _looks_numeric(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        return False


def _value_needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if any(c in s for c in (",", '"', "\n", "\t")):
        return True
    if s[0] in "[{-#\"'":
        return True
    if s in ("true", "false", "null") or _looks_numeric(s):
        return True
    return False


def _key_needs_quote(k: str) -> bool:
    if k == "" or k != k.strip():
        return True
    if any(c in k for c in (",", ":", '"', "[", "]", "{", "}", " ", "\n", "\t")):
        return True
    if k[0] in "-#\"'":
        return True
    return False


# --------------------------------------------------------------------------- #
# decode
# --------------------------------------------------------------------------- #
def decode(s: str):
    """Decode TOON text back to JSON-shaped data (inverse of `encode`)."""
    lines = []
    for raw in s.split("\n"):
        if raw.strip() == "":
            continue
        indent = (len(raw) - len(raw.lstrip(" "))) // 2
        lines.append((indent, raw.strip()))
    if not lines:
        return {}
    dec = _Decoder(lines)
    first = lines[0][1]
    if _ARRAY_HEADER.match(first):
        dec.i = 1
        n, fields, inline = _parse_array_header(first)
        return dec.parse_array_body(0, n, fields, inline)
    return dec.parse_members(0)


class _Decoder:
    def __init__(self, lines: list[tuple[int, str]]):
        self.lines = lines
        self.i = 0

    def _peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def _next(self):
        line = self.lines[self.i]
        self.i += 1
        return line

    def parse_members(self, indent: int) -> dict:
        out: dict = {}
        while True:
            cur = self._peek()
            if cur is None or cur[0] != indent or _is_item(cur[1]):
                break
            self._next()
            key, n, fields, inline = _parse_member(cur[1])
            if n is not None:
                out[key] = self.parse_array_body(indent, n, fields, inline)
            elif inline is not None:
                out[key] = _dec_scalar(inline)
            else:
                nxt = self._peek()
                if nxt is not None and nxt[0] == indent + 1 and not _is_item(nxt[1]):
                    out[key] = self.parse_members(indent + 1)
                else:
                    out[key] = {}  # empty object
        return out

    def parse_array_body(self, indent: int, n: int, fields, inline):
        if inline is not None:
            return [_dec_scalar(t) for t in _split_top(inline)]
        if n == 0:
            return []
        if fields is not None:
            rows = []
            for _ in range(n):
                cells = _split_top(self._next()[1])
                rows.append({f: _dec_scalar(c) for f, c in zip(fields, cells)})
            return rows
        return [self.parse_item(indent + 1) for _ in range(n)]

    def parse_item(self, indent: int):
        text = self._next()[1]
        if text == "-":
            nxt = self._peek()
            if nxt is not None and nxt[0] == indent + 1 and not _is_item(nxt[1]):
                return self.parse_members(indent + 1)
            return {}
        rest = text[2:]  # drop "- "
        if _ARRAY_HEADER.match(rest):
            n, fields, inline = _parse_array_header(rest)
            return self.parse_array_body(indent, n, fields, inline)
        return _dec_scalar(rest)


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _parse_member(text: str):
    """(key, n, fields, inline) for one object-member line. n/fields signal an
    array; inline is the post-colon payload (None means a structural `key:`)."""
    if text.startswith('"'):
        key, rest = _read_quoted(text)
    else:
        j = 0
        while j < len(text) and text[j] not in "[:":
            j += 1
        key, rest = text[:j], text[j:]
    if rest.startswith("["):
        return (key, *_parse_array_header(rest))
    after = rest[1:]  # drop ':'
    if after.strip() == "":
        return key, None, None, None
    return key, None, None, after.lstrip(" ")


def _parse_array_header(text: str):
    """(n, fields|None, inline|None) from a `[N]{fields}: inline` header."""
    m = _ARRAY_HEADER.match(text)
    if not m:
        raise ValueError(f"malformed TOON array header: {text!r}")
    n = int(m.group(1))
    fields = _split_top(m.group(2)) if m.group(2) is not None else None
    inline_raw = m.group(3)
    inline = None if inline_raw.strip() == "" else inline_raw.lstrip(" ")
    return n, fields, inline


def _read_quoted(text: str):
    i = 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            break
        i += 1
    return json.loads(text[: i + 1]), text[i + 1 :]


def _split_top(s: str, delim: str = ",") -> list[str]:
    """Split on `delim` at the top level, honoring JSON double-quoted spans."""
    out, buf, i, in_quote = [], "", 0, False
    while i < len(s):
        c = s[i]
        if in_quote:
            buf += c
            if c == "\\" and i + 1 < len(s):
                buf += s[i + 1]
                i += 2
                continue
            if c == '"':
                in_quote = False
        elif c == '"':
            in_quote = True
            buf += c
        elif c == delim:
            out.append(buf)
            buf = ""
        else:
            buf += c
        i += 1
    out.append(buf)
    return [t.strip() for t in out]


def _dec_scalar(tok: str):
    tok = tok.strip()
    if tok == "":
        return ""
    if tok[0] == '"':
        return json.loads(tok)
    if tok == "null":
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return tok
