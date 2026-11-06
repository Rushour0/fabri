"""Plain text mail messages for http sources with ``body_format: rfc822``.

An http source can declare ``body_format: rfc822``. Its tool then takes the
named arguments ``to``, ``cc``, ``bcc``, ``subject`` and ``body`` and sends
one RFC 822 message (``Content-Type: message/rfc822``) built from them,
instead of a JSON body. Gmail's media upload endpoints take exactly that.

The message is plain text only (one ``text/plain`` part). Header injection
is refused: ``to``, ``cc``, ``bcc`` and ``subject`` may not hold a line
break or any other control character, and every address is parsed and
written back out, so a value can never add a header of its own. They may
not hold an RFC 2047 encoded word (``=?charset?q?...?=``) either: the card
shows header values decoded, so an encoded word could hide a line break or
a control character that only appears after decoding. The message itself
still encodes non-ASCII text; only pre-encoded input is refused.

``describe_message`` turns the bytes back into the text a person reads on
an approval card: the recipients, the subject and the body, decoded. It
refuses a message it cannot show in full, and checks the decoded header
values again with the same rules as the input. Error messages name the
field, never its value.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, getaddresses
from typing import Any

CONTENT_TYPE = "message/rfc822"
FIELDS = ("to", "cc", "bcc", "subject", "body")
ADDRESS_FIELDS = ("to", "cc", "bcc")
MAX_RECIPIENTS = 100
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 200_000

# The headers build_message writes. A message with any other header, or one
# of these twice, is not shown on a card.
_SHOWN_HEADERS = ("To", "Cc", "Bcc", "Subject")
_ALLOWED_HEADERS = frozenset({"to", "cc", "bcc", "subject", "mime-version", "content-type",
                              "content-transfer-encoding"})
_ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
_BLOCKED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# Any "=?" ... "?=" span, so no encoded word reaches the message pre-encoded.
_ENCODED_WORD = re.compile(r"=\?.*?\?=", re.DOTALL)


class MessageError(ValueError):
    """The arguments do not make a valid message. The text names the field only."""


class MessageNotShowable(ValueError):
    """The bytes are not a message the approval card can show in full."""


def _header_problem(field: str, value: str) -> str | None:
    """Why a header value cannot be used or shown, or None. Names the field only."""
    for ch in value:
        if ch != "\t" and unicodedata.category(ch) in _BLOCKED_CATEGORIES:
            return f"{field} may not hold a line break or a control character"
    if _ENCODED_WORD.search(value):
        return f"{field} may not hold an RFC 2047 encoded word (=?...?=); send the text itself"
    return None


def _header_text(field: str, value: Any, *, cap: int) -> str:
    if not isinstance(value, str):
        raise MessageError(f"{field} must be text")
    problem = _header_problem(field, value)
    if problem:
        raise MessageError(problem)
    if len(value) > cap:
        raise MessageError(f"{field} is longer than {cap} characters")
    return value


def _addresses(field: str, value: Any, *, required: bool) -> list[str]:
    if value is None or value == "" or value == []:
        if required:
            raise MessageError(f"{field} needs at least one address")
        return []
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise MessageError(f"{field} must be text or a list of text")
        value = ", ".join(value)
    text = _header_text(field, value, cap=20_000)
    out = []
    try:
        pairs = getaddresses([text])
    except Exception:  # noqa: BLE001 -- any parse failure is a bad address list
        raise MessageError(f"{field} holds an address that is not valid") from None
    for name, addr in pairs:
        if not addr or not _ADDRESS.fullmatch(addr):
            raise MessageError(f"{field} holds an address that is not valid")
        if name:
            problem = _header_problem(field, name)
            if problem:
                raise MessageError(problem)
        try:
            out.append(formataddr((name, addr)) if name else addr)
        except Exception:  # noqa: BLE001 -- formataddr raises ValueError and others on odd names
            raise MessageError(f"{field} holds an address that is not valid") from None
    if not out:
        raise MessageError(f"{field} needs at least one address")
    return out


def build_message(args: Mapping[str, Any]) -> bytes:
    """The RFC 822 bytes for ``to``, ``cc``, ``bcc``, ``subject`` and
    ``body``. ``to``, ``subject`` and ``body`` are required. Raises
    MessageError before anything is built when an argument is missing,
    unknown or not valid."""
    if not isinstance(args, Mapping):
        raise MessageError("the arguments must be an object")
    unknown = sorted(str(k) for k in args if k not in FIELDS)
    if unknown:
        raise MessageError(f"unknown arguments {unknown}; a message takes {', '.join(FIELDS)}")
    recipients = {field: _addresses(field, args.get(field), required=field == "to") for field in ADDRESS_FIELDS}
    if sum(len(v) for v in recipients.values()) > MAX_RECIPIENTS:
        raise MessageError(f"a message may have at most {MAX_RECIPIENTS} recipients")
    if "subject" not in args:
        raise MessageError("subject is required")
    subject = _header_text("subject", args["subject"], cap=MAX_SUBJECT_CHARS)
    body = args.get("body")
    if not isinstance(body, str):
        raise MessageError("body is required and must be text")
    if "\x00" in body:
        raise MessageError("body may not hold a NUL character")
    if len(body) > MAX_BODY_CHARS:
        raise MessageError(f"body is longer than {MAX_BODY_CHARS} characters")
    try:
        msg = EmailMessage(policy=policy.SMTP)
        for field, header in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
            if recipients[field]:
                msg[header] = ", ".join(recipients[field])
        msg["Subject"] = subject
        msg.set_content(body.replace("\r\n", "\n").replace("\r", "\n"))
        return msg.as_bytes()
    except Exception:  # noqa: BLE001 -- the email package raises ValueError, TypeError and its own errors
        raise MessageError("the arguments do not make a valid mail message") from None


def describe_message(content: bytes) -> str:
    """The readable form of a message ``build_message`` made: one line per
    recipient header and the subject, decoded, then a blank line and the
    body text. Raises MessageNotShowable for anything else (more than one
    part, a type other than plain text, an unexpected or repeated header,
    or a parse defect)."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(content)
    except Exception:  # noqa: BLE001 -- any parse failure means the card cannot show it
        raise MessageNotShowable("the mail message cannot be parsed") from None
    if msg.defects:
        raise MessageNotShowable("the mail message has format defects")
    if msg.is_multipart() or msg.get_content_type() != "text/plain":
        raise MessageNotShowable("the mail message is not one plain text part")
    names = [k.lower() for k in msg.keys()]
    if len(names) != len(set(names)) or set(names) - _ALLOWED_HEADERS:
        raise MessageNotShowable("the mail message has headers the card does not show")
    lines = []
    try:
        for header in _SHOWN_HEADERS:
            value = msg[header]
            if value is not None:
                shown = str(value)
                # The decoded value goes through the input rules again: an
                # encoded word could decode to a line break, a control or
                # bidi character, or another encoded word.
                if _header_problem(header, shown):
                    raise MessageNotShowable(f"the mail message {header} header cannot be shown as is")
                lines.append(f"{header}: {shown}")
        text = msg.get_content()
    except MessageNotShowable:
        raise
    except Exception:  # noqa: BLE001 -- a header or body that does not decode is not shown
        raise MessageNotShowable("the mail message does not decode") from None
    if any(getattr(msg[h], "defects", None) for h in _SHOWN_HEADERS if msg[h] is not None):
        raise MessageNotShowable("the mail message has header defects")
    lines.append("")
    lines.append(text.replace("\r\n", "\n"))
    return "\n".join(lines)


__all__ = ["ADDRESS_FIELDS", "CONTENT_TYPE", "FIELDS", "MessageError", "MessageNotShowable", "build_message",
           "describe_message"]
