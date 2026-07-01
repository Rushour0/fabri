import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Protocol

from fabri.core.logging_setup import get_logger

logger = get_logger()


class Provider(StrEnum):
    """Canonical LLM provider ids -- the single source of truth for which
    providers exist. Adding a provider = add a member here, a default api-key
    entry in config._PROVIDER_DEFAULT_API_KEY_ENV (or leave it out for
    chain-auth providers like Bedrock), and a dispatch branch in
    runtime._instantiate. StrEnum so a member compares/hashes as its lowercase
    string value -- it matches the `llm.provider` yaml field directly, works as
    a dict key looked up by plain string, and serializes back to yaml cleanly."""

    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    BEDROCK = "bedrock"

    @classmethod
    def coerce(cls, value: "str | Provider | None") -> "Provider | None":
        """Map a raw `llm.provider` value to a member (case-insensitive), or None
        if absent/unknown. Callers decide how to treat None -- the default
        (`gemini`) lives in the caller; an unknown string surfaces as the
        user-facing 'unknown llm provider' error at dispatch."""
        if value is None:
            return None
        try:
            return cls(str(value).lower())
        except ValueError:
            return None

# Upper bound for the one-shot retry after a max_tokens truncation. fabri uses
# non-streaming requests, which risk SDK HTTP timeouts on very long completions,
# so the retry cap is held to a non-streaming-comfortable ceiling rather than
# the model's full output limit. Beyond this, the run fails loud and the agent
# is told to split the turn.
MAX_TOKENS_RETRY_CEILING = 16000


class LLMError(RuntimeError):
    """Unrecoverable problem talking to the LLM provider: an API error, a rate
    limit that survived retries, or a response truncated/empty enough that we
    refuse to treat it as a real answer. The agent loop maps this to
    Outcome.FAILED and ends the run cleanly instead of crashing with a raw
    provider traceback."""


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None  # Anthropic's tool_use id, needed to round-trip a proper tool_result block


@dataclass
class LLMUsage:
    """Per-call token accounting. All fields default to 0 so a scripted backend
    that doesn't fill them in still aggregates cleanly in run_agent's totals.

    `model` carries the model id the tokens were billed under so run_agent can
    price a mixed-model run (e.g. Sonnet orchestrator + Haiku decompose) per
    model instead of assuming a single rate. None on a scripted backend or any
    call where the model is unknown -> priced as 0 with a logged warning."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model: str | None = None


@dataclass
class LLMResponse:
    # `tool_call` is a convenience for constructing a single-call response (used
    # by the ScriptedLLMBackend and tests); __post_init__ folds it into
    # `tool_calls`, which is what the agent loop reads so it can dispatch
    # parallel tool calls a model may emit in one turn.
    tool_call: ToolCall | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str | None = None
    stop_reason: str | None = None
    # Inline reasoning text the model emitted alongside its tool_use blocks
    # ("Let me check existing characters first…"). Empty when there's no
    # accompanying text or when the response was a pure final answer. The
    # agent loop logs this as a `thought` trace event before the
    # tool_call(s) it preceded; host UIs can render it as reasoning context.
    thinking_text: str | None = None
    # Filled in by real provider backends so run_agent can aggregate a per-run
    # `usage` event. Optional so ScriptedLLMBackend stays a one-liner.
    usage: LLMUsage | None = None

    def __post_init__(self) -> None:
        if self.tool_call is not None and not self.tool_calls:
            self.tool_calls = [self.tool_call]


class LLMBackend(Protocol):
    def step(self, system: str, messages: list[dict]) -> LLMResponse: ...

    def set_tools(self, tool_defs: list[dict]) -> None:
        """Replace the backend's tool list mid-construction (A1: retrieved tool
        descriptions). Real provider backends override; ScriptedLLMBackend
        ignores. `tool_defs` is the universal Anthropic-shaped list
        (`{name, description, input_schema}`) -- providers that want a
        different wire format convert internally."""
        ...


def _call_with_retry(fn: Callable, transient: tuple[type[Exception], ...], attempts: int = 3, base_delay: float = 0.5):
    """Call `fn`, retrying transient provider errors (rate limit, connection,
    5xx) with exponential backoff. Non-transient exceptions propagate
    unchanged; exhausting the retries raises LLMError."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except transient as e:
            last = e
            if i < attempts - 1:
                delay = base_delay * (2**i)
                logger.warning("llm call transient error (attempt %d/%d), retrying in %.1fs: %s", i + 1, attempts, delay, e)
                time.sleep(delay)
    raise LLMError(f"llm call failed after {attempts} attempts: {last}") from last


class ScriptedLLMBackend:
    """Deterministic stub backend: returns a pre-defined sequence of responses.
    Used to exercise the agent loop without needing API access."""

    def __init__(self, script: list[LLMResponse]):
        self._script = list(script)
        self._i = 0

    def set_tools(self, tool_defs: list[dict]) -> None:
        # No-op: scripted backend doesn't model the provider tool list.
        return None

    def prewarm(self, system: str) -> LLMUsage:
        # No provider cache to warm; uniform no-op so callers don't special-case.
        return LLMUsage()

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        if self._i >= len(self._script):
            raise RuntimeError("ScriptedLLMBackend script exhausted")
        resp = self._script[self._i]
        self._i += 1
        return resp


class AnthropicLLMBackend:
    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        api_key_env: str = "ANTHROPIC_API_KEY",
        enable_prompt_cache: bool = True,
        cache_messages: bool = False,
    ):
        import os

        import anthropic

        self._client = anthropic.Anthropic(api_key=os.environ.get(api_key_env))
        self._model = model
        self._tools = tools or []
        self._max_tokens = max_tokens
        # The system prompt + tool list are identical across every step of a
        # given run; Anthropic's ephemeral cache (5-min TTL) cuts the re-sent
        # prefix to ~10% billing on cache hits. Flag exists so cost-sensitive
        # smoke tests / scripted backends can opt out.
        self._enable_prompt_cache = enable_prompt_cache
        # G21: also cache the *conversation history prefix*. Anthropic permits
        # up to 4 cache breakpoints; we use 2 (system, tools) by default and
        # add a 3rd on the last message when this is on. Most useful on
        # multi-step runs where each step re-sends the growing history; the
        # cache cuts the per-step re-bill on the prefix from ~100% to ~10%.
        # Off by default (untested at scale; the user opts in via config).
        self._cache_messages = cache_messages and enable_prompt_cache

    def set_tools(self, tool_defs: list[dict]) -> None:
        self._tools = list(tool_defs or [])

    def _build_system(self, system: str):
        if not self._enable_prompt_cache:
            return system
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _build_tools(self):
        # Mark the last tool with cache_control; Anthropic caches every block
        # at and before the marker, so one tag covers the whole tool list.
        if not self._enable_prompt_cache or not self._tools:
            return self._tools
        marked = [dict(t) for t in self._tools]
        marked[-1] = {**marked[-1], "cache_control": {"type": "ephemeral"}}
        return marked

    def _build_messages(self, messages: list[dict]) -> list[dict]:
        """G21: when cache_messages is on, mark the LAST message's last content
        block with cache_control. Anthropic caches everything up to and
        including the marker, so on the next turn the whole conversation
        prefix reads from cache (~0.1x input cost) instead of being re-billed.

        We mutate a shallow copy — caller's messages stay untouched.
        """
        if not self._cache_messages or not messages:
            return messages
        out = [dict(m) for m in messages]
        last = out[-1]
        content = last.get("content")
        if isinstance(content, str):
            # Promote to block form so we have somewhere to attach cache_control.
            last["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(content, list) and content:
            new_content = [dict(b) if isinstance(b, dict) else b for b in content]
            tail = new_content[-1]
            if isinstance(tail, dict):
                tail["cache_control"] = {"type": "ephemeral"}
            new_content[-1] = tail
            last["content"] = new_content
        return out

    def prewarm(self, system: str) -> LLMUsage:
        """Write the static system+tools prefix into Anthropic's ephemeral cache
        without generating output, via a `max_tokens=0` request. The next real
        `step()` with the same prefix then reads the cache (~0.1x input) instead
        of paying full price + the 1.25x cache-write.

        IMPORTANT: this trims first-call LATENCY, not cost -- the cache-write is
        paid once by whoever hits the cold prefix first (this call or a real
        step), and the cache TTL is ~5 minutes. Fire it just before a burst of
        runs that share this prefix; a 24/7 warmer just pays write premiums for
        nothing. No-op (returns zero usage) when prompt caching is disabled.
        Returns the call's LLMUsage so a caller can confirm the write landed
        (cache_creation_input_tokens > 0) or that it was already warm
        (cache_read_input_tokens > 0)."""
        if not self._enable_prompt_cache:
            return LLMUsage(model=self._model)
        import anthropic

        try:
            resp = _call_with_retry(
                lambda: self._client.messages.create(
                    model=self._model,
                    system=self._build_system(system),
                    messages=[{"role": "user", "content": "warmup"}],
                    tools=self._build_tools(),
                    max_tokens=0,
                ),
                transient=(anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError),
            )
        except anthropic.APIError as e:
            raise LLMError(f"anthropic prewarm error: {e}") from e
        u = resp.usage
        cc = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        logger.info("anthropic prewarm: model=%s cache_create=%d cache_read=%d", self._model, cc, cr)
        return LLMUsage(
            input_tokens=u.input_tokens or 0,
            cache_creation_input_tokens=cc,
            cache_read_input_tokens=cr,
            model=self._model,
        )

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        import anthropic

        def _create(max_tokens: int):
            return _call_with_retry(
                lambda: self._client.messages.create(
                    model=self._model,
                    system=self._build_system(system),
                    messages=self._build_messages(messages),
                    tools=self._build_tools(),
                    max_tokens=max_tokens,
                ),
                transient=(anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError),
            )

        t0 = time.monotonic()
        truncated_attempt = None
        try:
            resp = _create(self._max_tokens)
            # A single oversized turn shouldn't nuke a whole multi-step run. On a
            # max_tokens truncation, retry ONCE at a higher cap (bounded to a
            # non-streaming-safe ceiling) before giving up. This recovers the
            # common case -- one content-heavy turn (e.g. writing several files)
            # -- while still failing loud below if even the bigger cap truncates.
            # We never accept a truncated answer as success.
            if resp.stop_reason == "max_tokens":
                retry_cap = min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)
                if retry_cap > self._max_tokens:
                    logger.warning(
                        "anthropic response truncated at max_tokens=%d; retrying once at %d",
                        self._max_tokens,
                        retry_cap,
                    )
                    truncated_attempt = resp  # still billed -- fold into usage below
                    resp = _create(retry_cap)
        except anthropic.APIError as e:
            raise LLMError(f"anthropic API error: {e}") from e

        elapsed = time.monotonic() - t0
        usage = resp.usage
        # Cache usage fields land on `usage` when prompt caching fires; surface
        # them so a run's trace shows whether the cache is hitting. A discarded
        # truncated attempt was still billed, so fold its tokens in -- COGS must
        # reflect what Anthropic charged, not just the kept response.
        def _u(field: str, src) -> int:
            return getattr(src, field, 0) or 0 if src is not None else 0

        cache_create = (getattr(usage, "cache_creation_input_tokens", 0) or 0) + _u(
            "cache_creation_input_tokens", truncated_attempt and truncated_attempt.usage
        )
        cache_read = (getattr(usage, "cache_read_input_tokens", 0) or 0) + _u(
            "cache_read_input_tokens", truncated_attempt and truncated_attempt.usage
        )
        t_usage = truncated_attempt.usage if truncated_attempt is not None else None
        call_usage = LLMUsage(
            input_tokens=(usage.input_tokens or 0) + (_u("input_tokens", t_usage)),
            output_tokens=(usage.output_tokens or 0) + (_u("output_tokens", t_usage)),
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
            model=self._model,
        )
        logger.info(
            "anthropic call: model=%s latency=%.2fs input_tokens=%d output_tokens=%d cache_create=%d cache_read=%d stop=%s",
            self._model,
            elapsed,
            usage.input_tokens,
            usage.output_tokens,
            cache_create,
            cache_read,
            resp.stop_reason,
        )

        # A response truncated at the token cap can't be trusted: a tool_use
        # block may carry partial/invalid args, and a cut-off text answer is not
        # a real final answer. We already retried once at a higher cap above, so
        # reaching here means even that truncated -- fail rather than dispatch
        # garbage or report a half-answer as success.
        if resp.stop_reason == "max_tokens":
            raise LLMError(
                f"anthropic response truncated at max_tokens even after retry to "
                f"{min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)}; raise llm.max_tokens "
                f"or split this turn into smaller actions (fewer/lighter tool calls)"
            )

        # Claude can return several content blocks in one turn (reasoning text
        # plus one or more tool_use blocks, or parallel tool_use blocks). Collect
        # every tool_use; only treat the response as final when there are none.
        # When tool_use blocks are present, the accompanying text blocks carry
        # the model's reasoning ("Let me check X first..."); capture them as
        # thinking_text so the host UI can surface the chain of thought
        # instead of dropping the prose on the floor.
        tool_calls = [
            ToolCall(name=b.name, args=b.input, id=b.id) for b in resp.content if b.type == "tool_use"
        ]
        text_blocks = "".join(b.text for b in resp.content if b.type == "text").strip()
        if tool_calls:
            return LLMResponse(
                tool_calls=tool_calls,
                stop_reason=resp.stop_reason,
                thinking_text=text_blocks or None,
                usage=call_usage,
            )

        return LLMResponse(
            final_text=text_blocks or None,
            stop_reason=resp.stop_reason,
            usage=call_usage,
        )


class OpenAILLMBackend:
    """Second provider proving LLMBackend is provider-agnostic: same step()
    signature as AnthropicLLMBackend. The agent's message history is kept in
    Anthropic's tool_use/tool_result block shape, so this backend translates it
    into OpenAI's assistant.tool_calls + role:"tool" schema on the way out --
    without that, multi-step tool use never round-trips and the model re-issues
    or hallucinates calls. Not feature-complete (no streaming/vision)."""

    def __init__(
        self,
        model: str = "gpt-4o",
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
    ):
        import openai
        import os

        # `base_url` lets this backend talk to any OpenAI-API-compatible
        # endpoint -- in particular OpenRouter (https://openrouter.ai/api/v1).
        # OpenRouter's wire protocol IS OpenAI's chat-completions + tools
        # schema, so the rest of this class is identical between the two.
        client_kwargs: dict = {"api_key": os.environ.get(api_key_env)}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)
        self._model = model
        self._max_tokens = max_tokens
        self._base_url = base_url
        self._tools: list[dict] = []
        self.set_tools(tools or [])

    @staticmethod
    def _to_openai_tools(tool_defs: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t.get("input_schema") or {"type": "object"},
                },
            }
            for t in tool_defs
        ]

    def set_tools(self, tool_defs: list[dict]) -> None:
        self._tools = self._to_openai_tools(tool_defs or [])

    def prewarm(self, system: str) -> LLMUsage:
        # OpenAI prompt caching is automatic and not a separate write step the
        # way Anthropic's ephemeral cache is; no explicit warm path. No-op.
        return LLMUsage(model=self._model)

    @staticmethod
    def _to_openai(m: dict) -> list[dict]:
        """Translate one agent-history message (string content, or a list of
        Anthropic-style tool_use/tool_result blocks) into one or more
        OpenAI-shaped messages."""
        content = m["content"]
        role = m["role"]
        if isinstance(content, str):
            return [{"role": role, "content": content}]

        if role == "assistant":
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            msg: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return [msg]

        # user turn carrying tool_result blocks -> one role:"tool" message each
        out = []
        for b in content:
            if b.get("type") == "tool_result":
                c = b["content"]
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b["tool_use_id"],
                        "content": c if isinstance(c, str) else json.dumps(c),
                    }
                )
            else:
                out.append({"role": "user", "content": json.dumps(b)})
        return out

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        import openai

        t0 = time.monotonic()
        oa_messages = [{"role": "system", "content": system}]
        for m in messages:
            oa_messages.extend(self._to_openai(m))

        def _create(max_tokens: int):
            return _call_with_retry(
                lambda: self._client.chat.completions.create(
                    model=self._model,
                    messages=oa_messages,
                    tools=self._tools or None,
                    max_tokens=max_tokens,
                ),
                transient=(openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError),
            )

        truncated_attempt = None
        try:
            resp = _create(self._max_tokens)
            # Parity with the Anthropic backend: retry ONCE at a higher cap on a
            # length truncation before failing the whole run; never accept a
            # truncated answer as success.
            if resp.choices[0].finish_reason == "length":
                retry_cap = min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)
                if retry_cap > self._max_tokens:
                    logger.warning(
                        "openai response truncated at max_tokens=%d; retrying once at %d",
                        self._max_tokens,
                        retry_cap,
                    )
                    truncated_attempt = resp
                    resp = _create(retry_cap)
        except openai.OpenAIError as e:
            raise LLMError(f"openai API error: {e}") from e

        elapsed = time.monotonic() - t0
        choice = resp.choices[0]
        logger.info("openai call: model=%s latency=%.2fs finish=%s", self._model, elapsed, choice.finish_reason)

        if choice.finish_reason == "length":
            raise LLMError(
                f"openai response truncated at max_tokens even after retry to "
                f"{min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)}; raise llm.max_tokens "
                f"or split this turn into smaller actions"
            )

        oai_usage = getattr(resp, "usage", None)
        # OpenAI surfaces cached prompt tokens under prompt_tokens_details; pull
        # them onto cache_read so a cached run isn't priced at full input rate.
        oai_details = getattr(oai_usage, "prompt_tokens_details", None)
        oai_cache_read = getattr(oai_details, "cached_tokens", 0) or 0
        # Fold the discarded truncated attempt's tokens in -- it was still billed.
        t_usage = getattr(truncated_attempt, "usage", None) if truncated_attempt is not None else None
        call_usage = LLMUsage(
            input_tokens=(getattr(oai_usage, "prompt_tokens", 0) or 0)
            + (getattr(t_usage, "prompt_tokens", 0) or 0),
            output_tokens=(getattr(oai_usage, "completion_tokens", 0) or 0)
            + (getattr(t_usage, "completion_tokens", 0) or 0),
            cache_read_input_tokens=oai_cache_read,
            model=self._model,
        )

        message = choice.message
        if message.tool_calls:
            return LLMResponse(
                tool_calls=[
                    ToolCall(name=c.function.name, args=json.loads(c.function.arguments), id=c.id)
                    for c in message.tool_calls
                ],
                stop_reason=choice.finish_reason,
                usage=call_usage,
            )
        return LLMResponse(
            final_text=message.content or None,
            stop_reason=choice.finish_reason,
            usage=call_usage,
        )


# JSON-schema keys Gemini's FunctionDeclaration.parameters rejects. The
# universal tool defs are Anthropic-shaped JSON Schema (often emitted by
# pydantic / tooling that adds these), so strip them recursively before
# handing the schema to the google-genai SDK or the call 400s.
_GEMINI_SCHEMA_DROP_KEYS = frozenset(
    {"$schema", "$id", "$ref", "additionalProperties", "title", "default", "examples"}
)


def _sanitize_gemini_schema(node):
    """Recursively drop JSON-schema fields Gemini's parameters schema can't
    parse. Returns a cleaned copy; leaves the caller's tool defs untouched."""
    if isinstance(node, dict):
        return {
            k: _sanitize_gemini_schema(v)
            for k, v in node.items()
            if k not in _GEMINI_SCHEMA_DROP_KEYS
        }
    if isinstance(node, list):
        return [_sanitize_gemini_schema(v) for v in node]
    return node


class GeminiLLMBackend:
    """Third real provider, on Google's native google-genai SDK (not the
    OpenAI-compat endpoint), so native-only features (thinking budgets, Google
    Search grounding) stay reachable later. Same step() contract as the other
    backends: fabri keeps its message history in Anthropic's
    tool_use/tool_result block shape, and this backend translates it into
    Gemini's Content/Part(function_call|function_response) schema on the way
    out, and Gemini's response parts back into ToolCalls on the way in.

    Two Gemini-specific wrinkles drive the translation:
      * The system prompt is a separate `system_instruction`, not a message.
      * A function RESPONSE is matched to its call by function NAME, not by an
        id (Gemini has no tool-call id). fabri's tool_result blocks carry only
        `tool_use_id`, so we rebuild an id->name map from each preceding
        assistant turn's tool_use blocks to fill FunctionResponse.name.
    Not feature-complete (no streaming/vision)."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        api_key_env: str = "GEMINI_API_KEY",
    ):
        import os

        from google import genai

        self._client = genai.Client(api_key=os.environ.get(api_key_env))
        self._model = model
        self._max_tokens = max_tokens
        self._tools: list[dict] = []
        self.set_tools(tools or [])

    def set_tools(self, tool_defs: list[dict]) -> None:
        # Store the sanitized universal defs; defer building the SDK Tool object
        # to step() so the (lazy) types import stays inside call paths and tests
        # can stub the SDK without it being imported at construction here.
        self._tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _sanitize_gemini_schema(t.get("input_schema") or {"type": "object"}),
            }
            for t in (tool_defs or [])
        ]

    def _build_tools(self):
        from google.genai import types

        if not self._tools:
            return None
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        parameters=t["parameters"],
                    )
                    for t in self._tools
                ]
            )
        ]

    def prewarm(self, system: str) -> LLMUsage:
        # Gemini implicit caching is automatic; no explicit warm step like
        # Anthropic's ephemeral cache. Uniform no-op so callers don't special-case.
        return LLMUsage(model=self._model)

    def _to_gemini_contents(self, messages: list[dict]):
        """Translate the agent history into a list of Gemini Contents. Builds an
        id->name map per assistant turn so the function_response that follows can
        be tagged with the right function name (Gemini matches by name)."""
        from google.genai import types

        contents = []
        id_to_name: dict[str, str] = {}
        for m in messages:
            role = m["role"]
            content = m["content"]
            gem_role = "model" if role == "assistant" else "user"

            if isinstance(content, str):
                contents.append(types.Content(role=gem_role, parts=[types.Part(text=content)]))
                continue

            if role == "assistant":
                parts = []
                for b in content:
                    if b.get("type") == "text" and b.get("text"):
                        parts.append(types.Part(text=b["text"]))
                    elif b.get("type") == "tool_use":
                        if b.get("id"):
                            id_to_name[b["id"]] = b["name"]
                        parts.append(
                            types.Part(
                                function_call=types.FunctionCall(name=b["name"], args=b.get("input") or {})
                            )
                        )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            # user turn: tool_result blocks -> function_response parts, plain
            # blocks -> text. Resolve each result's function name via the map.
            parts = []
            for b in content:
                if b.get("type") == "tool_result":
                    raw = b.get("content")
                    resp = raw if isinstance(raw, dict) else {"result": raw if isinstance(raw, str) else json.dumps(raw)}
                    name = id_to_name.get(b.get("tool_use_id"), b.get("tool_use_id") or "tool")
                    parts.append(
                        types.Part(function_response=types.FunctionResponse(name=name, response=resp))
                    )
                elif b.get("type") == "text" and b.get("text"):
                    parts.append(types.Part(text=b["text"]))
                else:
                    parts.append(types.Part(text=json.dumps(b)))
            if parts:
                contents.append(types.Content(role="user", parts=parts))
        return contents

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        from google.genai import errors as genai_errors
        from google.genai import types

        contents = self._to_gemini_contents(messages)

        def _create(max_tokens: int):
            cfg = types.GenerateContentConfig(
                system_instruction=system or None,
                tools=self._build_tools(),
                max_output_tokens=max_tokens,
            )
            return _call_with_retry(
                lambda: self._client.models.generate_content(
                    model=self._model, contents=contents, config=cfg
                ),
                transient=(genai_errors.ServerError,),
            )

        t0 = time.monotonic()
        truncated_attempt = None
        try:
            resp = _create(self._max_tokens)
            # Parity with the other backends: a single oversized turn shouldn't
            # nuke a whole run. Retry ONCE at a higher cap on a MAX_TOKENS finish
            # before failing; never accept a truncated answer as success.
            if self._finish_reason(resp) == "MAX_TOKENS":
                retry_cap = min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)
                if retry_cap > self._max_tokens:
                    logger.warning(
                        "gemini response truncated at max_tokens=%d; retrying once at %d",
                        self._max_tokens,
                        retry_cap,
                    )
                    truncated_attempt = resp
                    resp = _create(retry_cap)
        except genai_errors.APIError as e:
            raise LLMError(f"gemini API error: {e}") from e

        elapsed = time.monotonic() - t0
        finish = self._finish_reason(resp)

        # The discarded truncated attempt was still billed -- fold its tokens in
        # so COGS reflects what Google charged, not just the kept response.
        def _um(src, field: str) -> int:
            um = getattr(src, "usage_metadata", None) if src is not None else None
            return getattr(um, field, 0) or 0 if um is not None else 0

        call_usage = LLMUsage(
            input_tokens=_um(resp, "prompt_token_count") + _um(truncated_attempt, "prompt_token_count"),
            output_tokens=_um(resp, "candidates_token_count") + _um(truncated_attempt, "candidates_token_count"),
            cache_read_input_tokens=_um(resp, "cached_content_token_count")
            + _um(truncated_attempt, "cached_content_token_count"),
            model=self._model,
        )
        logger.info(
            "gemini call: model=%s latency=%.2fs input_tokens=%d output_tokens=%d cache_read=%d finish=%s",
            self._model,
            elapsed,
            call_usage.input_tokens,
            call_usage.output_tokens,
            call_usage.cache_read_input_tokens,
            finish,
        )

        if finish == "MAX_TOKENS":
            raise LLMError(
                f"gemini response truncated at max_tokens even after retry to "
                f"{min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)}; raise llm.max_tokens "
                f"or split this turn into smaller actions"
            )

        # Walk the candidate's parts: collect text (reasoning prose when it
        # accompanies calls, the final answer otherwise) and function_calls.
        # Gemini returns no tool-call id, so synthesize a stable one per call;
        # the next turn matches the response back by function name via the map.
        tool_calls = []
        text_chunks = []
        candidate = (getattr(resp, "candidates", None) or [None])[0]
        parts = getattr(getattr(candidate, "content", None), "parts", None) or []
        for i, part in enumerate(parts):
            fc = getattr(part, "function_call", None)
            if fc is not None:
                tool_calls.append(
                    ToolCall(name=fc.name, args=dict(fc.args or {}), id=f"{fc.name}-{i}")
                )
            elif getattr(part, "text", None):
                text_chunks.append(part.text)
        text = "".join(text_chunks).strip()

        if tool_calls:
            return LLMResponse(
                tool_calls=tool_calls,
                stop_reason=finish,
                thinking_text=text or None,
                usage=call_usage,
            )
        return LLMResponse(
            final_text=text or None,
            stop_reason=finish,
            usage=call_usage,
        )

    @staticmethod
    def _finish_reason(resp) -> str | None:
        candidate = (getattr(resp, "candidates", None) or [None])[0]
        fr = getattr(candidate, "finish_reason", None)
        if fr is None:
            return None
        # google-genai uses a FinishReason enum; normalize to its name string.
        return getattr(fr, "name", str(fr))


# Converse stopReasons that are terminal failures, NOT a recoverable truncation:
# returning the (empty/partial) message as a real answer would be wrong, so the
# backend maps these to LLMError. model_context_window_exceeded means the INPUT
# is too big -- raising maxTokens can't help, so it must NOT go through the
# max_tokens retry path.
_BEDROCK_FAILURE_STOP_REASONS = frozenset(
    {
        "content_filtered",
        "guardrail_intervened",
        "malformed_tool_use",
        "malformed_model_output",
        "model_context_window_exceeded",
    }
)


class BedrockLLMBackend:
    """Fourth real provider, on AWS Bedrock's Converse API via boto3. Converse is
    a single unified request/response shape (incl. tool use) that works across
    every Converse-capable Bedrock model -- Claude, OpenAI gpt-oss, Llama,
    Mistral, etc. -- so one backend covers them all.

    Same step() contract as the other backends: fabri keeps its history in
    Anthropic's tool_use/tool_result block shape, and this backend translates it
    into Converse's toolUse/toolResult content blocks on the way out, and the
    response content blocks back into ToolCalls on the way in. Two Converse
    wrinkles drive the translation:
      * `maxTokens` lives in `inferenceConfig`, not as a top-level arg, and
        `system` is a list of blocks, not a string.
      * Converse requires strict user/assistant alternation and does NOT merge
        consecutive same-role turns, so `_to_converse_messages` coalesces them.

    Credentials resolve via boto3's default chain (env keys, shared profile, IAM
    role, or the AWS_BEARER_TOKEN_BEDROCK API key); region comes from the
    `aws_region` config field or AWS_REGION / AWS_DEFAULT_REGION. Not
    feature-complete (no streaming/vision; prompt caching needs explicit
    cachePoint blocks, which fabri doesn't insert, so the cache usage fields stay
    zero)."""

    def __init__(
        self,
        model: str = "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        region: str | None = None,
    ):
        import os

        import boto3
        from botocore.config import Config

        resolved_region = (
            region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        )
        try:
            # max_attempts=1 so fabri's _call_with_retry owns retry rather than
            # double-retrying on top of botocore's default standard mode. boto3
            # raises NoRegionError here when no region is resolvable -- wrap it so
            # the failure names the fix instead of leaking a botocore traceback.
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=resolved_region,
                config=Config(retries={"mode": "standard", "max_attempts": 1}),
            )
        except Exception as e:
            raise LLMError(
                f"bedrock client init failed (set llm.aws_region or AWS_REGION, and "
                f"AWS credentials): {e}"
            ) from e
        self._model = model
        self._max_tokens = max_tokens
        self._tools: list[dict] = []
        self.set_tools(tools or [])

    def set_tools(self, tool_defs: list[dict]) -> None:
        # Converse wants each tool wrapped in a `toolSpec` with the JSON Schema
        # under `inputSchema.json`. Stored ready-to-send; passed only when
        # non-empty (Converse 400s on an empty toolConfig.tools).
        self._tools = [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": {"json": t.get("input_schema") or {"type": "object"}},
                }
            }
            for t in (tool_defs or [])
        ]

    @staticmethod
    def _to_converse_messages(messages: list[dict]) -> list[dict]:
        """Translate the agent history (string content, or Anthropic-style
        tool_use/tool_result blocks) into Converse messages, coalescing
        consecutive same-role turns into one (Converse requires strict
        alternation and won't merge them itself)."""
        out: list[dict] = []

        def _emit(role: str, blocks: list[dict]) -> None:
            if not blocks:
                return
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            role = "assistant" if m["role"] == "assistant" else "user"
            content = m["content"]
            if isinstance(content, str):
                if content.strip():
                    _emit(role, [{"text": content}])
                continue

            blocks: list[dict] = []
            for b in content:
                btype = b.get("type")
                if btype == "text":
                    if (b.get("text") or "").strip():
                        blocks.append({"text": b["text"]})
                elif btype == "tool_use":
                    blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": b.get("id"),
                                "name": b["name"],
                                "input": b.get("input") or {},
                            }
                        }
                    )
                elif btype == "tool_result":
                    raw = b.get("content")
                    rc = [{"text": raw}] if isinstance(raw, str) else [{"json": raw}]
                    blocks.append(
                        {"toolResult": {"toolUseId": b.get("tool_use_id"), "content": rc}}
                    )
                else:
                    blocks.append({"text": json.dumps(b)})
            _emit(role, blocks)
        return out

    def prewarm(self, system: str) -> LLMUsage:
        # Converse has no separate cache-write step (prompt caching needs explicit
        # cachePoint blocks, which fabri doesn't insert); uniform no-op so callers
        # don't special-case.
        return LLMUsage(model=self._model)

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        import botocore.exceptions

        # Resolve the transient tuple HERE (not in __init__) so a backend built
        # via __new__ in tests -- which skips __init__ -- still works, mirroring
        # the Gemini backend's in-step error import.
        transient = (
            self._client.exceptions.ThrottlingException,
            self._client.exceptions.ModelTimeoutException,
            self._client.exceptions.InternalServerException,
            self._client.exceptions.ServiceUnavailableException,
            self._client.exceptions.ModelNotReadyException,
            botocore.exceptions.EndpointConnectionError,
        )

        conv_messages = self._to_converse_messages(messages)
        system_blocks = [{"text": system}] if system and system.strip() else None

        def _create(max_tokens: int):
            kwargs: dict = {
                "modelId": self._model,
                "messages": conv_messages,
                "inferenceConfig": {"maxTokens": max_tokens},
            }
            if system_blocks:
                kwargs["system"] = system_blocks
            if self._tools:
                kwargs["toolConfig"] = {"tools": self._tools}
            return _call_with_retry(
                lambda: self._client.converse(**kwargs), transient=transient
            )

        t0 = time.monotonic()
        truncated_attempt = None
        try:
            resp = _create(self._max_tokens)
            # Parity with the other backends: retry ONCE at a higher cap on a
            # max_tokens truncation before failing; never accept a truncated
            # answer as success.
            if resp.get("stopReason") == "max_tokens":
                retry_cap = min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)
                if retry_cap > self._max_tokens:
                    logger.warning(
                        "bedrock response truncated at max_tokens=%d; retrying once at %d",
                        self._max_tokens,
                        retry_cap,
                    )
                    truncated_attempt = resp
                    resp = _create(retry_cap)
        except botocore.exceptions.ClientError as e:
            raise LLMError(f"bedrock API error: {e}") from e
        except botocore.exceptions.BotoCoreError as e:
            raise LLMError(
                f"bedrock client error (check AWS region/credentials): {e}"
            ) from e

        elapsed = time.monotonic() - t0
        stop_reason = resp.get("stopReason")

        # The discarded truncated attempt was still billed -- fold its tokens in.
        # Cache keys may be absent on a usage dict, so .get-default them to 0.
        def _u(src, key: str) -> int:
            return ((src or {}).get("usage") or {}).get(key, 0) or 0

        call_usage = LLMUsage(
            input_tokens=_u(resp, "inputTokens") + _u(truncated_attempt, "inputTokens"),
            output_tokens=_u(resp, "outputTokens") + _u(truncated_attempt, "outputTokens"),
            cache_read_input_tokens=_u(resp, "cacheReadInputTokens")
            + _u(truncated_attempt, "cacheReadInputTokens"),
            cache_creation_input_tokens=_u(resp, "cacheWriteInputTokens")
            + _u(truncated_attempt, "cacheWriteInputTokens"),
            model=self._model,
        )
        logger.info(
            "bedrock call: model=%s latency=%.2fs input_tokens=%d output_tokens=%d "
            "cache_read=%d cache_write=%d stop=%s",
            self._model,
            elapsed,
            call_usage.input_tokens,
            call_usage.output_tokens,
            call_usage.cache_read_input_tokens,
            call_usage.cache_creation_input_tokens,
            stop_reason,
        )

        if stop_reason == "max_tokens":
            raise LLMError(
                f"bedrock response truncated at max_tokens even after retry to "
                f"{min(self._max_tokens * 2, MAX_TOKENS_RETRY_CEILING)}; raise llm.max_tokens "
                f"or split this turn into smaller actions"
            )
        if stop_reason in _BEDROCK_FAILURE_STOP_REASONS:
            raise LLMError(
                f"bedrock returned stopReason={stop_reason!r} (model={self._model}); "
                f"cannot treat as a valid answer"
            )

        # Converse response blocks are bare dicts keyed by content type -- there
        # is NO `type` field. Parse by key presence; toolUse.input is already a
        # dict (no json.loads). Text accompanying tool calls is reasoning prose.
        message = (resp.get("output") or {}).get("message") or {}
        tool_calls = []
        text_chunks = []
        for block in message.get("content") or []:
            if "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    ToolCall(name=tu["name"], args=tu.get("input") or {}, id=tu.get("toolUseId"))
                )
            elif block.get("text"):
                text_chunks.append(block["text"])
        text = "".join(text_chunks).strip()

        if tool_calls:
            return LLMResponse(
                tool_calls=tool_calls,
                stop_reason=stop_reason,
                thinking_text=text or None,
                usage=call_usage,
            )
        return LLMResponse(
            final_text=text or None,
            stop_reason=stop_reason,
            usage=call_usage,
        )
