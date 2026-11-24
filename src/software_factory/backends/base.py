"""One interface for every model backend.

`structured` is a single call that must return JSON matching a pydantic model. `agentic` runs a tool loop
confined to a Workspace and returns the agent's own summary; what it actually changed is read from git
by the orchestrator, never taken from the summary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from software_factory.config import Role
from software_factory.models import Usage
from software_factory.sandbox import Workspace

T = TypeVar("T", bound=BaseModel)


class BackendError(Exception):
    pass


class Backend(Protocol):
    name: str

    def structured(self, role: Role, user: str, schema: type[T], model: str) -> tuple[T, Usage]: ...

    def agentic(self, role: Role, task: str, ws: Workspace, model: str, home: Path) -> tuple[str, Usage]: ...


def schema_instruction(schema: type[BaseModel]) -> str:
    return "Reply with one JSON object and nothing else. It must match this JSON Schema:\n" + json.dumps(
        schema.model_json_schema(), separators=(",", ":")
    )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json(text: str, schema: type[T]) -> T:
    """Validate model output against `schema`, tolerating a code fence or prose around the object."""
    match = _FENCE.search(text)
    candidate = match.group(1) if match else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise BackendError(f"no JSON object in model output: {text[:200]!r}")
    try:
        return schema.model_validate_json(candidate[start : end + 1])
    except ValidationError as exc:
        raise BackendError(f"output does not match {schema.__name__}: {exc}") from exc
