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
        "t1-blank": "published",
        "t2-search": "published",
        "t3-admin": "awaiting_approval",
        "t4-injection": "rejected",
        "t5-vague": "needs_info",
    }
    store = Store(out / ".factory")
    search = store.get(store.runs("t2-search")[0][0])
    trace = search.trace()
    assert [v["passed"] for v in trace if v["kind"] == "verify"] == [True]  # the stop gate caught lint in-session
    assert [g["blocked"] for g in trace if g["kind"] == "stop_gate"] == [True, False]
    denied = [e for e in trace if e["kind"] == "tool" and not e["allowed"]]
    assert [e["target"] for e in denied] == ["specs/notes/spec.md"]  # agents cannot edit the living spec
    branch = git(out, "branch", "--list", "factory/t2-search-*").strip()
    assert branch
    pr_files = git(out, "diff", "--name-only", "main", branch).split()
    assert "specs/notes/spec.md" in pr_files and any(f.endswith("-t2-search/spec.md") for f in pr_files)
    assert "### Requirement: Search notes" in git(out, "show", f"{branch}:specs/notes/spec.md")
    assert not search.workspace.exists()  # worktree removed after publish
    lessons = load_lessons(out / "agents" / "lessons" / "coder.md")
    assert len(lessons) == 2 and not (out / "agents" / "lessons" / "reviewer.md").exists()
    assert "sk-" not in (search.dir / "trace.jsonl").read_text()


def test_fast_lane_needs_a_test_that_fails_before_the_fix(tmp_path: Path) -> None:
    out = run_demo(ROOT, tmp_path / "demo", qa=False)
    store = Store(out / ".factory")
    bug = store.get(store.runs("t1-blank")[0][0])
    verifies = [e for e in bug.trace() if e["kind"] == "verify"]
    assert verifies[0]["passed"] is False and "does not reproduce" in verifies[0]["violations"][0]
    assert verifies[1]["passed"] is True
    coders = [e for e in bug.trace() if e.get("role") == "coder"]
    assert [c["tier"] for c in coders] == ["standard", "frontier"]  # escalated after the failed attempt
    triage = next(e for e in bug.trace() if e.get("role") == "triage")
    assert triage["tier"] == "fast"
    assert "lane:fast" in (out / ".factory" / "tickets" / "t1-blank.md").read_text()
    status = (out / ".factory" / "tickets" / "t1-blank.status.md").read_text()
    assert "lane **fast**" in status and "frontier: claude-opus" in status


def test_approval_resumes_the_same_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    store, forge = Store(state), LocalForge(state, cfg)
    _, title, body, author = TICKETS[2]
    ticket = forge.add_ticket("t3-admin", title, body, author)
    script = scripts(repo)["t3-admin"]
    script["coder"] = [{"demo_app/app.py": "import os\n"}] * 3  # fails lint and has no tagged test, every time
    factory = Factory(repo, cfg, store, forge, MockBackend(script), qa=False)
    first = factory.handle(ticket)
    assert first.stage == Stage.AWAITING_APPROVAL
    assert factory.handle(ticket).id == first.id and first.stage == Stage.AWAITING_APPROVAL  # no human yet
    forge.approve("t3-admin")
    assert factory.handle(ticket).id == first.id
    assert first.stage.value == "failed"  # bounded retries: the complex lane allows three attempts
    assert "still failing after 3 attempts" in (state / "tickets" / "t3-admin.comments.md").read_text()


def test_coder_cannot_touch_files_outside_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    forge = LocalForge(state, cfg)
    ticket = forge.add_ticket("t2", *TICKETS[1][1:])
    script = scripts(repo)["t2-search"]
    script["coder"] = [{"factory.yaml": "backend: evil\n", "demo_app/README.md": "x\n"}]
    r = Factory(repo, cfg, Store(state), forge, MockBackend(script), qa=False).handle(ticket)
    assert r.stage == Stage.FAILED
    denied = {e["target"]: e["reason"] for e in r.trace() if e["kind"] == "tool" and not e["allowed"]}
    assert "protected" in denied["factory.yaml"] and "outside the plan" in denied["demo_app/README.md"]
    assert "backend: evil" not in (repo / "factory.yaml").read_text()


@pytest.mark.browser
@pytest.mark.skipif(chromium_path() is None, reason="needs Chromium (set FACTORY_CHROMIUM)")
def test_demo_with_browser_qa(tmp_path: Path) -> None:
    out = run_demo(ROOT, tmp_path / "demo", qa=True)
    store = Store(out / ".factory")
    run = store.get(store.runs("t2-search")[0][0])
    qa = json.loads((run.dir / "qa.json").read_text())
    assert qa["passed"] and all(qa["criteria"].values()) and (run.dir / "qa.png").is_file()
    assert [k.split(":")[0] for k in qa["criteria"]] == ["S1", "S2"]  # only the scenarios the plan gave to QA


def test_deferred_publish_and_tampered_bundle(tmp_path: Path) -> None:
    """Agent side writes a bundle and queues comments; the publisher re-checks the diff it is handed."""
    from software_factory.publisher import Bundle, Outbox, PublishError, flush_outbox, publish_bundle

    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    local = LocalForge(state, cfg)
    ticket = local.add_ticket("t2", *TICKETS[1][1:])
    outbox = Outbox(local, state / "outbox.jsonl")
    r = Factory(repo, cfg, Store(state), outbox, MockBackend(scripts(repo)["t2-search"]), qa=False, defer_publish=True)
    r_ = r.handle(ticket)
    assert r_.stage == Stage.READY and not (state / "prs" / "t1.md").exists()

    bundle = r_.load("bundle", Bundle)
    evil = bundle.model_copy(update={"branch": "factory/evil", "diff": bundle.diff + _protected_patch(repo)})
    with pytest.raises(PublishError, match=r"protected path changed: factory\.yaml"):
        publish_bundle(repo, cfg, local, evil)

    url = publish_bundle(repo, cfg, local, bundle)
    assert Path(url).is_file() and "factory/t2-" in git(repo, "branch", "--list", "factory/t2-*")
    done = flush_outbox(local, state / "outbox.jsonl", cfg.safe_outputs)
    assert done["labels"] == 1 and done["status"] == 1  # labels from triage; only the newest status is posted


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
    assert stages(out)["t2-search"] == "published"


def test_spec_question_without_assumption_asks_the_author(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    forge = LocalForge(state, cfg)
    ticket = forge.add_ticket("t2", *TICKETS[1][1:])
    script = scripts(repo)["t2-search"]
    spec = json.loads(script["spec"][0])  # type: ignore[arg-type]
    spec["open_questions"] = [{"question": "Search titles only, or note text too?", "assumption": ""}]
    script["spec"] = [json.dumps(spec)]
    r = Factory(repo, cfg, Store(state), forge, MockBackend(script), qa=False).handle(ticket)
    assert r.stage == Stage.NEEDS_INFO and not r.has("plan")
    assert "Search titles only" in (state / "tickets" / "t2.comments.md").read_text()


def test_bad_spec_goes_back_once_with_the_problems(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_sandbox_repo(ROOT, repo)
    cfg, state = load_config(repo), repo / ".factory"
    forge = LocalForge(state, cfg)
    ticket = forge.add_ticket("t2", *TICKETS[1][1:])
    script = scripts(repo)["t2-search"]
    bad = json.loads(script["spec"][0])  # type: ignore[arg-type]
    bad["requirements"][0]["op"] = "MODIFIED"  # "Search notes" is not in the living spec yet
    backend = MockBackend({**script, "spec": [json.dumps(bad), script["spec"][0]]})
    r = Factory(repo, cfg, Store(state), forge, backend, qa=False).handle(ticket)
    assert r.stage == Stage.PUBLISHED
    second_prompt = [u for role, u in backend.calls if role == "spec"][1]
    assert "no such requirement in specs/notes/spec.md" in second_prompt
