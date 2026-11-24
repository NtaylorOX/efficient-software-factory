"""Scripted backend for offline tests and the free `factory demo`. No network, no tokens spent.

Structured roles pop JSON strings from a queue; the agentic coder pops a {path: content} dict and applies
it through the Workspace, so the mock is held to the same path rules as a real model.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from software_factory.backends.base import BackendError, T, parse_json
from software_factory.config import Role
from software_factory.models import Usage
from software_factory.sandbox import Workspace


class MockBackend:
    name = "mock"

    def __init__(self, script: dict[str, list[str | dict[str, str]]]) -> None:
        self.queues: dict[str, deque[str | dict[str, str]]] = defaultdict(deque)
        for role, items in script.items():
            self.queues[role].extend(items)
        self.calls: list[tuple[str, str]] = []

    def _next(self, role: str) -> str | dict[str, str]:
        if not self.queues[role]:
            raise BackendError(f"mock script has no more responses for role {role!r}")
        return self.queues[role].popleft()

    def structured(self, role: Role, user: str, schema: type[T], model: str) -> tuple[T, Usage]:
        self.calls.append((role.name, user))
        item = self._next(role.name)
        if not isinstance(item, str):
            raise BackendError(f"mock response for {role.name} must be a JSON string")
        return parse_json(item, schema), Usage(input_tokens=len(user) // 4, output_tokens=len(item) // 4)

    def agentic(self, role: Role, task: str, ws: Workspace, model: str, home: Path) -> tuple[str, Usage]:
        self.calls.append((role.name, task))
        item = self._next(role.name)
        if not isinstance(item, dict):
            raise BackendError(f"mock response for {role.name} must be a dict of file writes")
        for path, content in item.items():
            ws.write(path, content)
        out = sum(len(c) for c in item.values()) // 4
        return f"Edited {', '.join(item)}.", Usage(input_tokens=len(task) // 4, output_tokens=out)
