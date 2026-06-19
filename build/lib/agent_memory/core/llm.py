import json
import time
from dataclasses import dataclass
from typing import Protocol

from agent_memory.core.logging_setup import get_logger

logger = get_logger()


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None  # Anthropic's tool_use id, needed to round-trip a proper tool_result block


@dataclass
class LLMResponse:
    tool_call: ToolCall | None = None
    final_text: str | None = None


class LLMBackend(Protocol):
    def step(self, system: str, messages: list[dict]) -> LLMResponse: ...


class ScriptedLLMBackend:
    """Deterministic stub backend: returns a pre-defined sequence of responses.
    Used to exercise the agent loop without needing API access."""

    def __init__(self, script: list[LLMResponse]):
        self._script = list(script)
        self._i = 0

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
    ):
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model
        self._tools = tools or []
        self._max_tokens = max_tokens

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        t0 = time.monotonic()
        resp = self._client.messages.create(
            model=self._model,
            system=system,
            messages=messages,
            tools=self._tools,
            max_tokens=self._max_tokens,
        )
        elapsed = time.monotonic() - t0
        usage = resp.usage
        logger.info(
            "anthropic call: model=%s latency=%.2fs input_tokens=%d output_tokens=%d",
            self._model,
            elapsed,
            usage.input_tokens,
            usage.output_tokens,
        )
        for block in resp.content:
            if block.type == "tool_use":
                return LLMResponse(tool_call=ToolCall(name=block.name, args=block.input, id=block.id))
            if block.type == "text":
                return LLMResponse(final_text=block.text)
        return LLMResponse(final_text="")


class OpenAILLMBackend:
    """Stub second provider proving LLMBackend is provider-agnostic: same step()
    signature as AnthropicLLMBackend, translating OpenAI's tool-call/content
    response into the same LLMResponse shape. Not feature-complete (no
    streaming, vision, etc.) -- enough to prove `provider: openai` in config
    works end-to-end with a real call."""

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
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t.get("input_schema") or {"type": "object"},
                },
            }
            for t in (tools or [])
        ]

    def step(self, system: str, messages: list[dict]) -> LLMResponse:
        t0 = time.monotonic()
        oa_messages = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            oa_messages.append({"role": m["role"], "content": content if isinstance(content, str) else json.dumps(content)})

        resp = self._client.chat.completions.create(
            model=self._model,
            messages=oa_messages,
            tools=self._tools or None,
            max_tokens=self._max_tokens,
        )
        elapsed = time.monotonic() - t0
        choice = resp.choices[0].message
        logger.info("openai call: model=%s latency=%.2fs", self._model, elapsed)

        if choice.tool_calls:
            call = choice.tool_calls[0]
            return LLMResponse(
                tool_call=ToolCall(name=call.function.name, args=json.loads(call.function.arguments), id=call.id)
            )
        return LLMResponse(final_text=choice.content or "")
