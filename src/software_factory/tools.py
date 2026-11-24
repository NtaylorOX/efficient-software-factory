"""File tools for the LiteLLM tool loop. Every call is checked by the Guard first, then runs through
Workspace, so the same rules apply as for Claude Code's hooks.

There is deliberately no shell tool: the coder edits files, and the orchestrator runs the tests itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from software_factory.hooks import Guard
from software_factory.sandbox import SandboxError, Workspace

ToolFn = Callable[[Workspace, dict[str, Any]], str]


def _read_file(ws: Workspace, args: dict[str, Any]) -> str:
    return ws.read(str(args["path"]))


def _write_file(ws: Workspace, args: dict[str, Any]) -> str:
    ws.write(str(args["path"]), str(args["content"]))
    return f"wrote {args['path']}"


def _list_files(ws: Workspace, args: dict[str, Any]) -> str:
    return "\n".join(ws.list_files(str(args.get("path", "."))))


def _search(ws: Workspace, args: dict[str, Any]) -> str:
    needle, hits = str(args["text"]), []
    for rel in ws.list_files():
        try:
            for n, line in enumerate(ws.read(rel).splitlines(), 1):
                if needle in line:
                    hits.append(f"{rel}:{n}: {line.strip()[:160]}")
        except SandboxError:
            continue
        if len(hits) >= 50:
            break
    return "\n".join(hits) or "no matches"


TOOLS: dict[str, tuple[ToolFn, str, dict[str, Any]]] = {
    "read_file": (_read_file, "Read a repo-relative file.", {"path": {"type": "string"}}),
    "write_file": (
        _write_file,
        "Replace a file's whole content. Only files in the plan are writable.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
    ),
    "list_files": (_list_files, "List files under a repo-relative directory.", {"path": {"type": "string"}}),
    "search": (_search, "Find lines containing exact text.", {"text": {"type": "string"}}),
}


def openai_tool_specs(names: list[str]) -> list[dict[str, Any]]:
    specs = []
    for name in names:
        _, desc, props = TOOLS[name]
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": list(props)},
                },
            }
        )
    return specs


def call_tool(guard: Guard, name: str, raw_args: str) -> str:
    """Run one tool call. Errors go back to the model as text so it can recover; nothing is swallowed."""
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError as exc:
        return f"error: arguments are not JSON: {exc}"
    path = args.get("path") if isinstance(args, dict) else None
    if (reason := guard.check(name, str(path) if path else None)) or name not in TOOLS:
        reason = reason or f"unknown tool {name!r}"
        guard.record("tool", tool=name, target=path, allowed=False, reason=reason)
        return f"error: denied: {reason}"
    try:
        out = TOOLS[name][0](guard.ws, args)
    except (SandboxError, KeyError, OSError) as exc:
        out = f"error: {exc}"
    guard.record("tool", tool=name, target=path, allowed=not out.startswith("error"))
    return out
