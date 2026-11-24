"""Scripted backend for offline tests and the free `factory demo`. No network, no tokens spent.

Structured roles pop JSON strings from a queue. The agentic coder pops one session: a {path: content} dict,
or a list of them when the stop gate is expected to send it back for a second turn. Every write goes through
the Guard like a real model's tool call: denied writes are skipped and traced, and the stop gate runs.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from software_factory.backends.base import BackendError, T, parse_json
from software_factory.config import Role
from software_factory.hooks import Guard
from software_factory.models import Usage

Item = str | dict[str, str] | list[dict[str, str]]


class MockBackend:
    name = "mock"

    def __init__(self, script: dict[str, list[Item]]) -> None:
        self.queues: dict[str, deque[Item]] = defaultdict(deque)
        for role, items in script.items():
            self.queues[role].extend(items)
        self.calls: list[tuple[str, str]] = []

    def _next(self, role: str) -> Item:
        if not self.queues[role]:
            raise BackendError(f"mock script has no more responses for role {role!r}")
        return self.queues[role].popleft()

    def structured(self, role: Role, user: str, schema: type[T], model: str) -> tuple[T, Usage]:
        self.calls.append((role.name, user))
        item = self._next(role.name)
        if not isinstance(item, str):
            raise BackendError(f"mock response for {role.name} must be a JSON string")
        return parse_json(item, schema), Usage(input_tokens=len(user) // 4, output_tokens=len(item) // 4)

    def agentic(self, role: Role, task: str, guard: Guard, model: str, home: Path) -> tuple[str, Usage]:
        self.calls.append((role.name, task))
        item = self._next(role.name)
        turns = [item] if isinstance(item, dict) else item
        if isinstance(turns, str) or not turns:
            raise BackendError(f"mock response for {role.name} must be a dict of file writes, or a list of them")
        written: list[str] = []
        usage = Usage(input_tokens=len(task) // 4)
        for n, turn in enumerate(turns):
            for path, content in turn.items():
                if reason := guard.check("write_file", path):
                    guard.record("tool", tool="write_file", target=path, allowed=False, reason=reason)
                    continue
                guard.ws.write(path, content)
                guard.record("tool", tool="write_file", target=path, allowed=True)
                written.append(path)
            usage = usage + Usage(output_tokens=sum(len(c) for c in turn.values()) // 4)
            # A scripted session cannot react after its last turn, so the gate only sends it back if one is left.
            if guard.stop(already_sent_back=n > 0 or n == len(turns) - 1) is None:
                break
            usage = usage + Usage(input_tokens=200)  # the stop gate's reason goes back to the model
        return f"Edited {', '.join(dict.fromkeys(written)) or 'nothing'}.", usage
