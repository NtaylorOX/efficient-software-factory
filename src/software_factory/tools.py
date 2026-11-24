"""File tools for the LiteLLM tool loop. Every call goes through Workspace, so the path rules apply.

There is deliberately no shell tool: the coder edits files, and the orchestrator runs the tests itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

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


def call_tool(ws: Workspace, allowed: list[str], name: str, raw_args: str) -> str:
    """Run one tool call. Errors go back to the model as text so it can recover; nothing is swallowed."""
    if name not in allowed or name not in TOOLS:
        return f"error: tool {name!r} is not available to this agent"
    try:
        args = json.loads(raw_args or "{}")
        return TOOLS[name][0](ws, args)
    except (SandboxError, KeyError, json.JSONDecodeError, OSError) as exc:
        return f"error: {exc}"
