"""LLM-driven QA: an agent drives the app in headless Chromium, one step per call.

The agent never gets a browser handle. It sees a compact accessibility snapshot and returns one QAStep;
this module executes the step with Playwright and reports the outcome. The browser may only reach the app
under test on localhost: every other request is aborted.
"""

from __future__ import annotations

import functools
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from software_factory.backends.base import Backend
from software_factory.config import Role
from software_factory.models import QAReport, QAStep, Usage
from software_factory.sandbox import scrubbed_env

SNAPSHOT_CHARS = 3000  # the snapshot is the bulk of each QA call, so it is capped


@functools.cache
def chromium_path() -> str | None:
    """FACTORY_CHROMIUM if set, else a preinstalled Chromium, else the one `playwright install` downloaded."""
    for path in (os.environ.get("FACTORY_CHROMIUM", ""), "/opt/pw-browsers/chromium"):
        if path and Path(path).exists():
            return path
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            bundled = str(pw.chromium.executable_path)
    except ImportError:
        return None
    return bundled if Path(bundled).exists() else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class AppServer:
    """Runs the workspace's app with uvicorn, with a scrubbed env, bound to localhost only."""

    def __init__(self, workspace: Path, app: str, home: Path) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cmd = [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(self.port)]
        self.proc = subprocess.Popen(
            cmd, cwd=workspace, env=scrubbed_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

    def __enter__(self) -> AppServer:
        for _ in range(100):
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode() if self.proc.stderr else ""
                raise RuntimeError(f"app exited during startup: {err[-800:]}")
            try:
                httpx.get(self.url, timeout=0.5)
                return self
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("app did not start within 10s")

    def __exit__(self, *exc: object) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)


def _locate(page: Any, kind: str, name: str) -> Any:
    """Find an element the way a person would describe it: by role and accessible name, then by label."""
    roles = {
        "fill": ["textbox", "searchbox"],
        "press": ["textbox", "searchbox"],
        "click": ["button", "link"],
    }[kind]
    for role in roles:
        loc = page.get_by_role(role, name=name)
        if loc.count():
            return loc.first
    for loc in (page.get_by_label(name), page.get_by_placeholder(name)):
        if loc.count():
            return loc.first
    raise LookupError(f"no {'/'.join(roles)} named {name!r}")


def execute(page: Any, base_url: str, step: QAStep) -> str:
    try:
        if step.action == "goto":
            page.goto(base_url + (step.target if step.target.startswith("/") else "/" + step.target))
        elif step.action == "fill":
            _locate(page, "fill", step.target).fill(step.value)
        elif step.action == "click":
            _locate(page, "click", step.target).click()
        elif step.action == "press":
            _locate(page, "press", step.target).press(step.value or "Enter")
        elif step.action == "expect_text":
            page.get_by_text(step.value).first.wait_for(state="visible", timeout=2000)
        elif step.action == "expect_no_text":
            page.wait_for_timeout(300)
            if page.get_by_text(step.value).first.is_visible():
                return f"FAIL: {step.value!r} is still visible"
        else:
            return f"FAIL: unknown action {step.action!r}"
        page.wait_for_load_state("networkidle")
        return "ok"
    except Exception as exc:  # Playwright raises many types; the outcome goes back to the agent
        return f"FAIL: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


def run_qa(
    backend: Backend, role: Role, model: str, workspace: Path, app: str, criteria: list[str], out_dir: Path
) -> tuple[QAReport, Usage]:
    from playwright.sync_api import sync_playwright

    usage, history, steps = Usage(), list[str](), list[QAStep]()
    with AppServer(workspace, app, out_dir / "home") as server, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path())
        page = browser.new_page()
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(server.url) else r.abort())
        page.goto(server.url + "/")
        page.wait_for_load_state("networkidle")
        verdict: dict[str, bool] | None = None
        confirmed = 0  # expect_* steps that actually passed in the browser
        for _ in range(role.max_turns):
            snapshot = page.locator("body").aria_snapshot()[:SNAPSHOT_CHARS]
            prompt = (
                "Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria)
                + "\n\nSteps so far:\n" + ("\n".join(history[-8:]) or "(none)")
                + f"\n\nCurrent page ({page.url.removeprefix(server.url) or '/'}):\n{snapshot}"
            )  # fmt: skip
            step, u = backend.structured(role, prompt, QAStep, model)
            usage = usage + u
            steps.append(step)
            if step.action == "done":
                verdict = _parse_verdict(step.value, criteria)
                break
            result = execute(page, server.url, step)
            confirmed += step.action.startswith("expect") and result == "ok"
            history.append(f"{step.action} {step.target!r} {step.value!r} -> {result}")
        shot = out_dir / "qa.png"
        page.screenshot(path=str(shot), full_page=True)
        browser.close()
    notes = "" if verdict is not None else "QA agent ran out of steps"
    verdict = verdict or dict.fromkeys(criteria, False)
    passed = bool(verdict) and all(verdict.values())
    if passed and confirmed == 0:  # the agent's verdict alone is not evidence
        passed, notes = False, "QA agent claimed a pass without a successful expect step"
    return QAReport(passed=passed, criteria=verdict, steps_taken=steps, notes=notes, screenshot=shot.name), usage


def _parse_verdict(raw: str, criteria: list[str]) -> dict[str, bool]:
    """Criteria the agent did not mention count as failed, so a short verdict cannot pass by omission."""
    try:
        given = json.loads(raw)
    except json.JSONDecodeError:
        given = {}
    if not isinstance(given, dict):
        given = {}
    return {c: given.get(c) is True for c in criteria}
