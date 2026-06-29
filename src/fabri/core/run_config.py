"""A single value object for the scalar orchestration knobs `run_agent`
consumes. Built once from a loaded config and threaded into every entry point
(cli run / cli replay / the agent-as-tool runner) so the three can't drift —
historically each re-listed ~20 kwargs by hand and `replay`/`agent_runner`
silently omitted the planner, tool-retrieval, and budget knobs, running under
different orchestration settings than `run`.

The role LLM backends (main / decompose / planner / narrator) are NOT held
here — they need tool defs and live provider clients, so they're built
separately via `runtime.build_run_llms`. This object is pure config: cheap to
build, trivial to `dataclasses.replace` for a sub-agent's tighter budget.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from fabri.core.agent import DEFAULT_MAX_PARALLEL_SPAWNS
from fabri.core.decompose import DEFAULT_MAX_SUBQUESTIONS
from fabri.orchestrator.retrieval import DEFAULT_TOOL_TOP_K, DEFAULT_TOP_K


def planner_mode_from_cfg(planner_cfg: dict) -> str:
    """Translate the agent.planner block (which carries both an `enabled` flag
    and a `mode` string for back-compat) into the run_agent.planner_mode arg.
    `enabled: false` wins over any non-off mode, so a stale `mode` value in a
    config can't surprise-activate the planner."""
    mode = planner_cfg.get("mode", "off")
    if mode in ("auto", "force", "off"):
        if not planner_cfg.get("enabled", False) and mode != "off":
            return "off"
        return mode
    return "off"


@dataclass(frozen=True)
class AgentRunConfig:
    """Scalar orchestration knobs for a single `run_agent` invocation."""

    max_steps: int = 10
    top_k: int = DEFAULT_TOP_K
    max_subquestions: int = DEFAULT_MAX_SUBQUESTIONS
    max_parallel_spawns: int = DEFAULT_MAX_PARALLEL_SPAWNS
    system_prompt: str = ""
    system_prompt_prefix: str = ""
    result_format: str = "toon"
    output_format: str = "json"
    planner_mode: str = "off"
    planner_max_items: int = 8
    planner_auto_token_threshold: int = 80
    tool_retrieval_enabled: bool = False
    tool_retrieval_top_k: int = DEFAULT_TOOL_TOP_K
    tool_retrieval_always_include: tuple[str, ...] = ()
    max_cost_usd: float | None = None
    response_schema: dict | None = None
    response_retries: int = 1
    error_strategy: str = "strict"
    response_fallback: object | None = None
    repair: dict | None = None

    @classmethod
    def from_config(cls, config: dict) -> "AgentRunConfig":
        """Project a loaded (DEFAULT_CONFIG-merged) config onto the run knobs.
        Tolerates a partially-specified config by falling back to each field's
        default, so a hand-built dict in a test still works."""
        agent = config.get("agent", {})
        tools = config.get("tools", {})
        mem = config.get("memory", {})
        decompose = tools.get("decompose", {})
        retrieval = tools.get("retrieval", {})
        planner = agent.get("planner", {})
        return cls(
            max_steps=agent.get("max_steps", cls.max_steps),
            top_k=mem.get("top_k", cls.top_k),
            max_subquestions=decompose.get("max_subquestions", cls.max_subquestions),
            max_parallel_spawns=tools.get(
                "max_parallel_spawns", cls.max_parallel_spawns
            ),
            system_prompt=agent.get("system_prompt", ""),
            system_prompt_prefix=agent.get("system_prompt_prefix", ""),
            result_format=tools.get("result_format", cls.result_format),
            output_format=agent.get("output_format", cls.output_format),
            planner_mode=planner_mode_from_cfg(planner),
            planner_max_items=planner.get("max_items", cls.planner_max_items),
            planner_auto_token_threshold=planner.get(
                "auto_token_threshold", cls.planner_auto_token_threshold
            ),
            tool_retrieval_enabled=retrieval.get("enabled", False),
            tool_retrieval_top_k=retrieval.get("top_k", DEFAULT_TOOL_TOP_K),
            tool_retrieval_always_include=tuple(retrieval.get("always_include", [])),
            max_cost_usd=agent.get("max_cost_usd"),
            response_schema=agent.get("response_schema"),
            response_retries=agent.get("response_retries", cls.response_retries),
            error_strategy=agent.get("error_strategy", cls.error_strategy),
            response_fallback=agent.get("response_fallback"),
            repair=agent.get("repair"),
        )

    def for_subagent(self, max_steps: int, max_cost_usd: float | None) -> "AgentRunConfig":
        """A copy with the child's budget swapped in (agent.subagent.* overrides).
        Everything else — planner, retrieval, prompts — is inherited so a child
        orchestrates the same way the parent does."""
        return replace(self, max_steps=max_steps, max_cost_usd=max_cost_usd)

    def as_kwargs(self) -> dict:
        """The keyword arguments `run_agent` accepts for these knobs. The caller
        adds the positional/runtime args (task, llm, tools, store, session_id)
        and the role LLM backends (decompose_llm, planner_llm, narrator_llm)."""
        return {
            "max_steps": self.max_steps,
            "top_k": self.top_k,
            "max_subquestions": self.max_subquestions,
            "max_parallel_spawns": self.max_parallel_spawns,
            "system_prompt": self.system_prompt,
            "system_prompt_prefix": self.system_prompt_prefix,
            "result_format": self.result_format,
            "output_format": self.output_format,
            "planner_mode": self.planner_mode,
            "planner_max_items": self.planner_max_items,
            "planner_auto_token_threshold": self.planner_auto_token_threshold,
            "tool_retrieval_enabled": self.tool_retrieval_enabled,
            "tool_retrieval_top_k": self.tool_retrieval_top_k,
            "tool_retrieval_always_include": self.tool_retrieval_always_include,
            "max_cost_usd": self.max_cost_usd,
            "response_schema": self.response_schema,
            "response_retries": self.response_retries,
            "error_strategy": self.error_strategy,
            "response_fallback": self.response_fallback,
            "repair": self.repair,
        }
