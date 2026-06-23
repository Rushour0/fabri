import json
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from fabri.core.logging_setup import get_logger

logger = get_logger()

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
                    messages=messages,
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
    ):
        import openai
        import os

        self._client = openai.OpenAI(api_key=os.environ.get(api_key_env))
        self._model = model
        self._max_tokens = max_tokens
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
