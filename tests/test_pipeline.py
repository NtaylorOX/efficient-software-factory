"""End-to-end through the real pipeline with the scripted backend: git, policy, checks, ledger, traces."""

import json
from pathlib import Path

import pytest

from software_factory.backends.mock import MockBackend
from software_factory.config import load_config, load_lessons
from software_factory.demo import TICKETS, make_sandbox_repo, run_demo, scripts
from software_factory.forge import LocalForge
from software_factory.git import git
from software_factory.models import Stage
from software_factory.pipeline import Factory
from software_factory.qa import chromium_path
from software_factory.state import Store

ROOT = Path(__file__).parents[1]


def stages(root: Path) -> dict[str, str]:
    return {ticket: stage for _, ticket, stage in Store(root / ".factory").runs()}


def test_demo_without_browser(tmp_path: Path) -> None:
    out = run_demo(ROOT, tmp_path / "demo", qa=False)
    assert stages(out) == {
        "t1-search": "published",
        "t2-admin": "awaiting_approval",
        "t3-injection": "rejected",
    }
    store = Store(out / ".factory")
    run = store.get(store.runs("t1-search")[0][0])
    verifies = [e for e in run.trace() if e["kind"] == "verify"]
    assert [v["passed"] for v in verifies] == [False, True]  # lint failure fed back, fixed on attempt 2
    assert "factory/t1-search-" in git(out, "branch", "--list", "factory/*")
    assert not run.workspace.exists()  # worktree removed after publish
    lessons = load_lessons(out / "agents" / "lessons" / "coder.md")
    assert len(lessons) == 2 and not (out / "agents" / "lessons" / "reviewer.md").exists()
    assert "sk-" not in (run.dir / "trace.jsonl").read_text()


def test_approval_resumes_the_same_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    store, forge = Store(state), LocalForge(state, cfg)
    _, title, body, author = TICKETS[1]
    ticket = forge.add_ticket("t2-admin", title, body, author)
    script = scripts(repo)["t2-admin"]
    script["coder"] = [{"demo_app/app.py": "import os\n"}] * 2  # fails lint twice
    factory = Factory(repo, cfg, store, forge, MockBackend(script), qa=False)
    first = factory.handle(ticket)
    assert first.stage == Stage.AWAITING_APPROVAL
    assert factory.handle(ticket).id == first.id and first.stage == Stage.AWAITING_APPROVAL  # no human yet
    forge.approve("t2-admin")
    assert factory.handle(ticket).id == first.id
    assert first.stage.value == "failed"  # bounded retries: two attempts, then stop and tell the ticket
    assert "still failing after 2 attempts" in (state / "tickets" / "t2-admin.comments.md").read_text()


def test_coder_cannot_touch_files_outside_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    forge = LocalForge(state, cfg)
    ticket = forge.add_ticket("t1", *TICKETS[0][1:])
    script = scripts(repo)["t1-search"]
    script["coder"] = [{"factory.yaml": "backend: evil\n"}]
    r = Factory(repo, cfg, Store(state), forge, MockBackend(script), qa=False).handle(ticket)
    assert r.stage == Stage.FAILED
    assert any(e["kind"] == "error" and "protected" in e["error"] for e in r.trace())


@pytest.mark.browser
@pytest.mark.skipif(chromium_path() is None, reason="needs Chromium (set FACTORY_CHROMIUM)")
def test_demo_with_browser_qa(tmp_path: Path) -> None:
    out = run_demo(ROOT, tmp_path / "demo", qa=True)
    store = Store(out / ".factory")
    run = store.get(store.runs("t1-search")[0][0])
    qa = json.loads((run.dir / "qa.json").read_text())
    assert qa["passed"] and all(qa["criteria"].values()) and (run.dir / "qa.png").is_file()


def test_deferred_publish_and_tampered_bundle(tmp_path: Path) -> None:
    """Agent side writes a bundle and queues comments; the publisher re-checks the diff it is handed."""
    from software_factory.publisher import Bundle, Outbox, PublishError, flush_outbox, publish_bundle

    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    local = LocalForge(state, cfg)
    ticket = local.add_ticket("t1", *TICKETS[0][1:])
    outbox = Outbox(local, state / "outbox.jsonl")
    r = Factory(repo, cfg, Store(state), outbox, MockBackend(scripts(repo)["t1-search"]), qa=False, defer_publish=True)
    r_ = r.handle(ticket)
    assert r_.stage == Stage.READY and not (state / "prs" / "t1.md").exists()

    bundle = r_.load("bundle", Bundle)
    evil = bundle.model_copy(update={"branch": "factory/evil", "diff": bundle.diff + _protected_patch(repo)})
    with pytest.raises(PublishError, match=r"protected path changed: factory\.yaml"):
        publish_bundle(repo, cfg, local, evil)

    url = publish_bundle(repo, cfg, local, bundle)
    assert Path(url).is_file() and "factory/t1-" in git(repo, "branch", "--list", "factory/t1-*")
    assert flush_outbox(local, state / "outbox.jsonl") == 0  # trusted author, no gate: nothing was queued


def _protected_patch(repo: Path) -> str:
    """A real git diff that edits factory.yaml, as a compromised agent job might append to its bundle."""
    original = (repo / "factory.yaml").read_text()
    (repo / "factory.yaml").write_text("backend: evil\n" + original)
    try:
        return git(repo, "diff", "--", "factory.yaml")
    finally:
        (repo / "factory.yaml").write_text(original)


def test_demo_with_relative_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = run_demo(ROOT, Path("rel-demo"), qa=False)
    assert stages(out)["t1-search"] == "published"
