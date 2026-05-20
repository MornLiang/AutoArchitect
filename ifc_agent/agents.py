"""
Multi-role Agent system for IFC QA and editing.

Architecture inspired by Text2BIM (multi-agent roles + structured prompts)
and GenArtist (LLM as planner with tool catalogs).

SDK / Provider selection is driven by the ``LLM_PROVIDER`` env var (set in
``.env``).  Four SDK types are supported:

  - openai     OpenAI native API (gpt-4o, gpt-4o-mini, o3-mini, o4-mini …)
  - deepseek   DeepSeek V4-series (deepseek-chat / deepseek-reasoner)
               — OpenAI-compatible wire format, dedicated endpoint
  - claude     Anthropic Claude (claude-sonnet-4-* / claude-opus-4-*)
  - gemini     Google Gemini / Vertex AI

Any other OpenAI-compatible service (MiniMax, GLM, Qwen, …) can still be
used with ``LLM_PROVIDER=openai`` + a custom ``OPENAI_BASE_URL``.
"""

from __future__ import annotations

import re
import json
import logging
import os
from typing import Any, Callable, Optional

import dotenv

logger = logging.getLogger(__name__)

# System env vars take priority; .env is an optional fallback.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dotenv.load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)


# ---------------------------------------------------------------------------
# LLM Backend Abstraction
# ---------------------------------------------------------------------------

SUPPORTED_PROVIDERS = ["openai", "deepseek", "claude", "gemini"]

# DeepSeek's public API is OpenAI-wire-compatible at this endpoint.
# https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
_DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"          # V4 Pro (thinking-enabled)
_DEEPSEEK_DEFAULT_REASONING_EFFORT = "high"          # high | max
# DeepSeek's thinking mode does NOT support these sampling params (per docs).
_DEEPSEEK_THINKING_FORBIDDEN_PARAMS = {
    "temperature", "top_p", "presence_penalty", "frequency_penalty",
}


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var that accepts 1/0, true/false, yes/no, on/off."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y", "t")


class LLMBackend:
    """Unified LLM interface.

    The provider is determined by (priority high → low):
      1. ``provider`` argument (from CLI ``--provider``)
      2. ``LLM_PROVIDER`` environment variable (from ``.env``)
      3. Hardcoded default ``"openai"``

    MiniMax-specific behaviour (``reasoning_split``, ``<think>`` stripping,
    temperature clamping) is auto-detected from the model name.

    Includes an in-memory prompt cache to avoid duplicate LLM calls.
    """

    def __init__(self, provider: str = None, model: str = None, api_key: str = None):
        self.provider = (
            provider
            or os.environ.get("LLM_PROVIDER", "").strip()
            or "openai"
        ).lower()
        self.model = model
        self.api_key = api_key
        self._client = None
        self._cache: dict[str, str] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._setup()

    @property
    def _is_minimax(self) -> bool:
        """Auto-detect MiniMax models by name prefix."""
        return (self.model or "").lower().startswith("minimax")

    @property
    def _is_deepseek(self) -> bool:
        """Active provider is DeepSeek (regardless of specific model name)."""
        return self.provider == "deepseek"

    @property
    def _is_deepseek_reasoner(self) -> bool:
        """DeepSeek's R1-style reasoner model name prefix."""
        return (self.model or "").lower().startswith("deepseek-reasoner")

    @property
    def _uses_openai_wire(self) -> bool:
        """Both 'openai' and 'deepseek' providers speak the OpenAI wire format."""
        return self.provider in ("openai", "deepseek")

    @property
    def _deepseek_thinking_enabled(self) -> bool:
        """Whether to turn ON DeepSeek's chain-of-thought thinking mode.

        Reads ``DEEPSEEK_THINKING`` env var (default: enabled, matching the
        DeepSeek API default per the official docs).
        """
        return _env_bool("DEEPSEEK_THINKING", True)

    @property
    def _deepseek_reasoning_effort(self) -> str:
        """Reasoning effort for DeepSeek thinking mode.

        Per the docs, ``low`` / ``medium`` are silently promoted to ``high``,
        and ``xhigh`` is promoted to ``max``.  We accept any of these strings
        and let the API normalise.
        """
        raw = (os.environ.get("DEEPSEEK_REASONING_EFFORT", "") or "").strip().lower()
        return raw or _DEEPSEEK_DEFAULT_REASONING_EFFORT

    def _apply_deepseek_thinking(self, kwargs: dict) -> dict:
        """Inject thinking-mode parameters into a chat.completions kwargs dict.

        - Adds ``extra_body={"thinking": {"type": "enabled"|"disabled"}}``
        - Adds ``reasoning_effort`` when thinking is enabled
        - Drops sampling params not supported in thinking mode
          (temperature / top_p / presence_penalty / frequency_penalty)
        """
        enabled = self._deepseek_thinking_enabled
        extra = dict(kwargs.get("extra_body") or {})
        extra["thinking"] = {"type": "enabled" if enabled else "disabled"}
        kwargs["extra_body"] = extra

        if enabled:
            kwargs["reasoning_effort"] = self._deepseek_reasoning_effort
            for forbidden in _DEEPSEEK_THINKING_FORBIDDEN_PARAMS:
                kwargs.pop(forbidden, None)
        return kwargs

    def _setup(self):
        if self.provider == "openai":
            from openai import OpenAI
            self.api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
            self.model = self.model or os.environ.get("OPENAI_MODEL", "gpt-4o")
            base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
            self._client = OpenAI(api_key=self.api_key, base_url=base_url)

        elif self.provider == "deepseek":
            # DeepSeek is OpenAI-wire-compatible but lives at its own endpoint.
            from openai import OpenAI
            self.api_key = (
                self.api_key
                or os.environ.get("DEEPSEEK_API_KEY", "").strip()
                or os.environ.get("OPENAI_API_KEY", "")
            )
            self.model = (
                self.model
                or os.environ.get("DEEPSEEK_MODEL", "").strip()
                or _DEEPSEEK_DEFAULT_MODEL
            )
            base_url = (
                os.environ.get("DEEPSEEK_BASE_URL", "").strip()
                or _DEEPSEEK_DEFAULT_BASE_URL
            )
            self._client = OpenAI(api_key=self.api_key, base_url=base_url)

        elif self.provider == "claude":
            import anthropic
            self.api_key = (
                self.api_key
                or os.environ.get("CLAUDE_API_KEY", "").strip()
                or os.environ.get("ANTHROPIC_API_KEY", "")
            )
            self.model = self.model or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
            base_url = (
                os.environ.get("CLAUDE_BASE_URL", "").strip()
                or os.environ.get("ANTHROPIC_BASE_URL", "").strip()
                or None
            )
            kwargs = {"api_key": self.api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = anthropic.Anthropic(**kwargs)

        elif self.provider == "gemini":
            from google import genai
            from google.genai.types import HttpOptions
            project = os.environ.get("VERTEX_PROJECT", "")
            location = os.environ.get("VERTEX_LOCATION", "global")
            self.model = self.model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")
            self._client = genai.Client(
                vertexai=True, project=project, location=location,
                http_options=HttpOptions(api_version="v1"),
            )

        else:
            raise ValueError(
                f"Unsupported provider: {self.provider}. "
                f"Choose from: {', '.join(SUPPORTED_PROVIDERS)}"
            )

    # -- MiniMax helpers ----------------------------------------------------

    def _effective_temperature(self, temperature: float) -> float:
        """MiniMax requires temperature in (0.0, 1.0]; clamp 0 → 0.01."""
        if self._is_minimax and temperature <= 0.0:
            return 0.01
        return temperature

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """Remove <think>...</think> blocks emitted by MiniMax reasoning models."""
        if not text:
            return text or ""
        return re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()

    def _clean_content(self, text: str) -> str:
        return self._strip_think_tags(text) if self._is_minimax else text

    # -- Generation ---------------------------------------------------------

    def _cache_key(self, prompt: str, temperature: float) -> str:
        import hashlib
        h = hashlib.sha256(f"{self.model}:{temperature}:{prompt}".encode()).hexdigest()
        return h

    def generate(self, prompt: str, stop: list[str] = None, temperature: float = 0) -> str:
        """Generate a text completion (with prompt caching)."""
        if temperature == 0:
            key = self._cache_key(prompt, temperature)
            if key in self._cache:
                self._cache_hits += 1
                logger.debug("LLM cache HIT (%d hits / %d misses)",
                             self._cache_hits, self._cache_misses)
                return self._cache[key]
            self._cache_misses += 1

        result = self._generate_uncached(prompt, stop, temperature)

        if temperature == 0:
            self._cache[key] = result

        return result

    def _generate_uncached(self, prompt: str, stop: list[str] = None, temperature: float = 0) -> str:
        """Raw LLM call without caching."""
        if self._uses_openai_wire:
            messages = [{"role": "user", "content": prompt}]
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
            # OpenAI o-series reasoning models use reasoning_effort instead of temperature.
            if any(tag in self.model for tag in ("o1", "o3", "o4")):
                kwargs["reasoning_effort"] = "medium"
            else:
                kwargs["temperature"] = self._effective_temperature(temperature)
                if stop:
                    kwargs["stop"] = stop
            if self._is_minimax:
                kwargs["extra_body"] = {"reasoning_split": True}
            if self._is_deepseek:
                # Thinking mode strips unsupported sampling params and adds
                # extra_body={"thinking": {"type": "enabled"}} + reasoning_effort.
                kwargs = self._apply_deepseek_thinking(kwargs)

            resp = self._client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            if self._is_deepseek:
                rc = getattr(msg, "reasoning_content", None)
                if rc:
                    logger.debug("[DeepSeek thinking] %s", rc[:300] +
                                 ("…" if len(rc) > 300 else ""))
            return self._clean_content(msg.content or "")

        elif self.provider == "claude":
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=self._effective_temperature(temperature),
                messages=[{"role": "user", "content": prompt}],
                stop_sequences=stop or [],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    return block.text
            return resp.content[0].text if resp.content else ""

        elif self.provider == "gemini":
            from google.genai.types import GenerateContentConfig
            resp = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=8192,
                ),
            )
            return resp.text

    # -- Tool calling -------------------------------------------------------

    def generate_with_tools(
        self,
        prompt: str,
        tools: list[dict],
        available_functions: dict[str, Callable],
        stop: list[str] = None,
    ) -> str:
        """Generate with function/tool calling support."""
        if self._uses_openai_wire:
            # Both OpenAI native and DeepSeek implement OpenAI-style tool calls.
            return self._openai_tool_call(prompt, tools, available_functions, stop)
        elif self.provider == "claude":
            return self._claude_tool_call(prompt, tools, available_functions, stop)
        elif self.provider == "gemini":
            return self.generate(prompt, stop)

    def _openai_tool_call(self, prompt, tools, funcs, stop):
        messages: list[Any] = [{"role": "user", "content": prompt}]
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if self._is_minimax:
            create_kwargs["extra_body"] = {"reasoning_split": True}
        if self._is_deepseek:
            create_kwargs = self._apply_deepseek_thinking(create_kwargs)

        resp = self._client.chat.completions.create(**create_kwargs)
        msg = resp.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return self._clean_content(msg.content or "")

        # Preserve the COMPLETE assistant message in conversation history.
        # • MiniMax requires this for chain-of-thought continuity.
        # • DeepSeek in thinking mode MUST receive reasoning_content back on
        #   every subsequent tool-call turn or the API returns 400.
        messages.append(self._assistant_msg_for_history(msg))

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            if fn_name not in funcs:
                return self.generate(prompt, stop)
            fn_args = json.loads(tc.function.arguments)
            fn_result = funcs[fn_name](**fn_args)
            messages.append({
                "tool_call_id": tc.id,
                "role": "tool",
                "content": str(fn_result),
            })

        follow_kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if self._is_minimax:
            follow_kwargs["extra_body"] = {"reasoning_split": True}
        if self._is_deepseek:
            follow_kwargs = self._apply_deepseek_thinking(follow_kwargs)
        resp2 = self._client.chat.completions.create(**follow_kwargs)
        return self._clean_content(resp2.choices[0].message.content or "")

    def _assistant_msg_for_history(self, msg) -> Any:
        """Return an assistant message safe to append back to the API.

        For DeepSeek thinking mode the ``reasoning_content`` field is
        mandatory in the next turn when there were tool calls; we therefore
        rebuild a plain dict that explicitly carries it.  For other
        providers we return the SDK message object unchanged so all
        provider-specific extras pass through verbatim.
        """
        if not self._is_deepseek:
            return msg
        out: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(msg, "content", None),
        }
        rc = getattr(msg, "reasoning_content", None)
        if rc is not None:
            out["reasoning_content"] = rc
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": getattr(tc, "type", "function"),
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        return out

    def _claude_tool_call(self, prompt, tools, funcs, stop):
        claude_tools = []
        for t in tools:
            fn = t.get("function", t)
            claude_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            })

        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=self._effective_temperature(0),
            messages=[{"role": "user", "content": prompt}],
            tools=claude_tools,
            stop_sequences=stop or [],
        )

        if resp.stop_reason in ("tool_use", "end_turn"):
            tool_use = None
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_use = block
                    break
            if tool_use and tool_use.name in funcs:
                fn_result = funcs[tool_use.name](**tool_use.input)
                # Preserve the COMPLETE response.content (including thinking,
                # text, and tool_use blocks) — required by MiniMax Anthropic
                # API for chain-of-thought continuity.
                resp2 = self._client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    temperature=self._effective_temperature(0),
                    messages=[
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": resp.content},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": tool_use.id,
                             "content": str(fn_result)},
                        ]},
                    ],
                )
                for block in resp2.content:
                    if getattr(block, "type", None) == "text":
                        return block.text
                return resp2.content[0].text if resp2.content else ""

        for block in resp.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return resp.content[0].text if resp.content else ""


# ---------------------------------------------------------------------------
# Agent Roles
# ---------------------------------------------------------------------------

class BaseAgent:
    """Base class for all agent roles."""

    def __init__(self, llm: LLMBackend, prompt_template: str = ""):
        self.llm = llm
        self.prompt_template = prompt_template
        self.history: list[str] = []

    def _build_prompt(self, **kwargs) -> str:
        from ifc_agent.utils import inject_prompt
        chat_history = "\n".join(self.history) if self.history else ""
        return inject_prompt(
            self.prompt_template,
            chat_history=chat_history,
            **kwargs,
        )


class RouterAgent(BaseAgent):
    """
    Intent classifier (Text2BIM's PO role).

    Determines whether the user's query is QA, Edit, or Mixed, and optionally
    invokes the IFC Expert via function calling (like PO calling plan_designer).
    """

    def __init__(self, llm: LLMBackend, prompt_template: str):
        super().__init__(llm, prompt_template)

    def classify(self, task: str, ifc_context: str) -> dict:
        prompt = self._build_prompt(task=task, ifc_context=ifc_context)
        response = self.llm.generate(prompt)
        self.history.append(f"User: {task}")
        self.history.append(f"Router: {response}")

        from ifc_agent.utils import parse_json_response
        try:
            return parse_json_response(response)
        except ValueError:
            logger.warning("Router failed to parse JSON, defaulting to QA")
            return {"intent": "qa", "reasoning": "parse fallback", "sub_tasks": [task]}


class IFCExpertAgent(BaseAgent):
    """
    Domain knowledge provider (Text2BIM's Architect role).

    Provides IFC schema knowledge and building analysis expertise.
    Can be invoked by the Router or Workflow as a function call.
    """

    def __init__(self, llm: LLMBackend, prompt_template: str):
        super().__init__(llm, prompt_template)

    def analyze(self, task: str, ifc_context: str = "") -> str:
        prompt = self._build_prompt(task=task, ifc_context=ifc_context)
        response = self.llm.generate(prompt)
        return response


class CoderAgent(BaseAgent):
    """
    Code generation agent (Text2BIM's Programmer role).

    Generates Python code that calls pre-defined tools to accomplish tasks.
    Supports auto-retry on execution failure (up to max_retries).
    """

    def __init__(self, llm: LLMBackend, prompt_template: str, max_retries: int = 3):
        super().__init__(llm, prompt_template)
        self.max_retries = max_retries

    def generate_code(self, task: str, ifc_context: str, all_tools: str) -> str:
        prompt = self._build_prompt(
            task=task,
            ifc_context=ifc_context,
            all_tools=all_tools,
        )
        response = self.llm.generate(prompt)
        self.history.append(f"Task: {task}")
        self.history.append(f"Programmer: {response}")
        return response

    def execute_with_retry(
        self,
        task: str,
        ifc_context: str,
        all_tools: str,
        tool_registry: dict,
    ) -> tuple[Any, str]:
        """Generate and execute code, retrying on failure."""
        from ifc_agent.utils import extract_code_blocks

        last_error = None
        code_text = ""

        for attempt in range(self.max_retries):
            if attempt == 0:
                code_text = self.generate_code(task, ifc_context, all_tools)
            else:
                retry_task = (
                    f"Fix the error in the previous code.\n"
                    f"Error: {last_error}\n"
                    f"Previous code:\n{code_text}\n"
                    f"Original task: {task}"
                )
                code_text = self.generate_code(retry_task, ifc_context, all_tools)

            code_blocks = extract_code_blocks(code_text)
            if not code_blocks:
                last_error = "No code block found in response."
                continue

            code = code_blocks[0]
            try:
                result = self._execute_code(code, tool_registry)
                return result, code_text
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Code execution attempt {attempt + 1} failed: {e}")

        return f"Failed after {self.max_retries} attempts. Last error: {last_error}", code_text

    def _execute_code(self, code: str, tool_registry: dict) -> Any:
        """Execute generated code in a restricted environment."""
        import builtins
        safe_builtins = {
            k: getattr(builtins, k) for k in (
                "len", "range", "enumerate", "zip", "map", "filter",
                "list", "dict", "set", "tuple", "str", "int", "float",
                "bool", "sorted", "min", "max", "sum", "abs", "round",
                "print", "isinstance", "type", "hasattr", "getattr",
                "True", "False", "None",
            ) if hasattr(builtins, k)
        }
        safe_builtins["__import__"] = __import__

        env = {"__builtins__": safe_builtins}
        env.update(tool_registry)

        exec(code, env)
        return env.get("result", "Code executed successfully (no result variable set).")


class PlannerAgent(BaseAgent):
    """
    Edit planner (GenArtist's LLM planner role).

    Given an editing task and tool catalog, outputs a structured JSON
    command sequence that command_parse.py can translate into tool calls.
    """

    def __init__(self, llm: LLMBackend, prompt_template: str):
        super().__init__(llm, prompt_template)

    def plan(self, task: str, ifc_context: str) -> str:
        prompt = self._build_prompt(task=task, ifc_context=ifc_context)
        response = self.llm.generate(prompt)
        self.history.append(f"User: {task}")
        self.history.append(f"Planner: {response}")
        return response


class ReviewerAgent(BaseAgent):
    """
    Result verification agent (Text2BIM's Reviewer + GenArtist's correction).

    Verifies edit results against user intent and produces correction commands.
    """

    def __init__(self, llm: LLMBackend, prompt_template: str):
        super().__init__(llm, prompt_template)

    def review(
        self,
        original_task: str,
        operations_log: str,
        post_state: str,
        validation: str,
    ) -> dict:
        prompt = self._build_prompt(
            original_task=original_task,
            operations_log=operations_log,
            post_state=post_state,
            validation=validation,
        )
        response = self.llm.generate(prompt)

        from ifc_agent.utils import parse_json_response
        try:
            return parse_json_response(response)
        except ValueError:
            logger.warning("Reviewer failed to parse JSON response")
            return {"satisfied": True, "assessment": response, "corrections": []}


class CorrectionAgent(BaseAgent):
    """
    Self-correction agent (GenArtist's verification loop).

    Compares pre/post model states and decides if corrections are needed.
    """

    def __init__(self, llm: LLMBackend, prompt_template: str):
        super().__init__(llm, prompt_template)

    def verify(
        self,
        task: str,
        pre_context: str,
        post_context: str,
        operations_log: str,
    ) -> dict:
        prompt = self._build_prompt(
            task=task,
            pre_context=pre_context,
            post_context=post_context,
            operations_log=operations_log,
        )
        response = self.llm.generate(prompt)

        from ifc_agent.utils import parse_json_response
        try:
            return parse_json_response(response)
        except ValueError:
            return {"correct": True, "explanation": response, "corrections": []}
