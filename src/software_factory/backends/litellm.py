"""Any OpenAI-compatible endpoint, normally a LiteLLM proxy (see litellm_config.yaml).

The proxy holds the provider keys. This process only holds LITELLM_API_KEY, which should be a virtual key
with a budget and an expiry, so a leak is cheap and revocable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from software_factory.backends.base import BackendError, T, parse_json, schema_instruction
from software_factory.config import Role
from software_factory.models import Usage
from software_factory.sandbox import Workspace
from software_factory.tools import call_tool, openai_tool_specs


def _usage(resp: Any) -> Usage:
    u = getattr(resp, "usage", None)
    return Usage(
        input_tokens=getattr(u, "prompt_tokens", 0) or 0,
        output_tokens=getattr(u, "completion_tokens", 0) or 0,
    )


class LiteLLMBackend:
    name = "litellm"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, client: Any = None) -> None:
        self.client: Any = client or OpenAI(
            base_url=base_url or os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1"),
            api_key=api_key or os.environ.get("LITELLM_API_KEY", "sk-local"),
        )

    def structured(self, role: Role, user: str, schema: type[T], model: str) -> tuple[T, Usage]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": role.system_prompt() + "\n\n" + schema_instruction(schema)},
            {"role": "user", "content": user},
        ]
        usage = Usage()
        for attempt in range(2):  # one repair attempt on malformed JSON, then fail loudly
            resp = self.client.chat.completions.create(
                model=model, messages=messages, max_tokens=role.max_output_tokens, temperature=0
            )
            usage = usage + _usage(resp)
            text = resp.choices[0].message.content or ""
            try:
                return parse_json(text, schema), usage
            except BackendError as exc:
                if attempt == 1:
                    raise
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"Invalid: {exc}"},
                ]
        raise AssertionError("unreachable")

    def agentic(self, role: Role, task: str, ws: Workspace, model: str, home: Path) -> tuple[str, Usage]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": role.system_prompt()},
            {"role": "user", "content": task},
        ]
        usage, specs = Usage(), openai_tool_specs(role.tools)
        for _ in range(role.max_turns):
            resp = self.client.chat.completions.create(
                model=model, messages=messages, tools=specs, max_tokens=role.max_output_tokens, temperature=0
            )
            usage = usage + _usage(resp)
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            messages.append(msg.model_dump(exclude_none=True))
            if not calls:
                return msg.content or "", usage
            for call in calls:
                result = call_tool(ws, role.tools, call.function.name, call.function.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        raise BackendError(f"{role.name} hit its turn budget ({role.max_turns}) without finishing")
