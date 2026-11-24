"""Readable tracking: turn a run's trace.jsonl into a step-by-step timeline.

The same Markdown goes to three places: `factory trace <run>` in a terminal, the GitHub Actions job summary,
and one status comment on the issue that the factory edits in place as the run moves.
"""

from __future__ import annotations

import json
from typing import Any

from software_factory.models import Usage
from software_factory.state import Run

ICONS = {
    "published": "✅", "ready": "📦", "failed": "❌", "rejected": "⛔", "declined": "💬", "needs_info": "❓",
    "awaiting_approval": "✋",
}  # fmt: skip
SKIP_KEYS = {"t", "run", "kind", "stage", "usage", "role", "tier", "model", "passed", "blocked", "allowed", "tool"}


def _detail(ev: dict[str, Any]) -> str:
    parts = []
    for key, value in ev.items():
        if key in SKIP_KEYS or value in (None, [], {}, ""):
            continue
        text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        parts.append(f"{key}: {text}")
    return "; ".join(parts).replace("|", "/").replace("\n", " ")[:160]


CHECKS = {"spec_check": "📐 spec check", "plan_check": "🗺️ plan check"}
ICONS_EXTRA = {"archive": "🗄️ archive", "learn": "🎓 learn"}


def rows(r: Run) -> list[dict[str, str]]:
    """One row per event. Allowed tool calls are counted on the agent's row instead of listed one by one."""
    out, prev = [], None
    for ev in r.trace():
        if ev["kind"] == "tool" and ev.get("allowed", True):
            continue
        took = f"{ev['t'] - prev:.1f}s" if prev is not None else ""
        prev = ev["t"]
        if ev["kind"] == "stage":
            step = f"{ICONS.get(ev['stage'], '•')} {ev['stage']}"
        elif ev["kind"] == "agent":
            step = f"🤖 {ev['role']}"
        elif ev["kind"] == "verify":
            step = "🧪 verify " + ("passed" if ev["passed"] else "failed")
        elif ev["kind"] in CHECKS:
            step = CHECKS[ev["kind"]] + (" passed" if ev["passed"] else " failed")
        elif ev["kind"] == "tool":
            step = f"🛡️ denied {ev.get('tool')}"
        elif ev["kind"] == "stop_gate":
            step = "🛑 stop gate: " + (
                "sent back" if ev["blocked"] else "passed" if not ev["problems"] else "let through"
            )
        else:
            step = ICONS_EXTRA.get(ev["kind"], ev["kind"])
        usage = ev.get("usage") or {}
        tokens = f"{usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}" if usage else ""
        model = f"{ev['tier']}: {ev['model']}" if "model" in ev else ""
        out.append({"step": step, "detail": _detail(ev), "model": model, "tokens": tokens, "took": took})
    return out


def lane_of(r: Run) -> str:
    for ev in r.trace():
        if ev.get("stage") == "triaged":
            return str(ev.get("lane") or "none")
    return "-"


def timeline(r: Run) -> str:
    ticket, tokens = r.ticket(), r.tokens()
    head = (
        f"**Factory run `{r.id}`** · ticket `{ticket.id}` · lane **{lane_of(r)}** · "
        f"stage **{r.stage.value}** · tokens in/out {tokens.input_tokens}/{tokens.output_tokens}\n\n"
        "| step | detail | model | tokens | took |\n|---|---|---|---|---|\n"
    )
    body = "\n".join(f"| {x['step']} | {x['detail']} | {x['model']} | {x['tokens']} | {x['took']} |" for x in rows(r))
    return head + body


def usage_by_role(r: Run) -> dict[str, Usage]:
    out: dict[str, Usage] = {}
    for ev in r.trace():
        if ev["kind"] == "agent" and "usage" in ev:
            out[ev["role"]] = out.get(ev["role"], Usage()) + Usage.model_validate(ev["usage"])
    return out


def summary_json(r: Run) -> str:
    """Machine-readable roll-up, for dashboards or comparing lanes across runs."""
    return json.dumps(
        {
            "run": r.id, "ticket": r.ticket_id, "lane": lane_of(r), "stage": r.stage.value,
            "usage_by_role": {k: v.model_dump() for k, v in usage_by_role(r).items()},
        }
    )  # fmt: skip
