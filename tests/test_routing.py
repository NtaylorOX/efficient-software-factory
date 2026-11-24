import json
from pathlib import Path

import pytest

from software_factory.config import load_config
from software_factory.forge import GitHubForge, LocalForge, RepoScopeError
from software_factory.models import Ticket, TicketKind, Triage
from software_factory.policy import route
from software_factory.publisher import Outbox, flush_outbox

ROOT = Path(__file__).parents[1]


def triage(kind: TicketKind, size: str = "S", missing: list[str] | None = None) -> Triage:
    return Triage(kind=kind, size=size, summary="s", missing_info=missing or [])  # type: ignore[arg-type]


def test_route_sends_small_bugs_fast_and_features_complex() -> None:
    cfg = load_config(ROOT)
    assert route(cfg, triage(TicketKind.BUG, "S")).lane == "fast"
    assert route(cfg, triage(TicketKind.CHORE, "M")).lane == "fast"
    assert route(cfg, triage(TicketKind.BUG, "L")).lane == "complex"
    assert route(cfg, triage(TicketKind.FEATURE, "S")).lane == "complex"
    assert route(cfg, triage(TicketKind.QUESTION)).lane is None
    assert route(cfg, triage(TicketKind.OUT_OF_SCOPE)).lane is None
    assert route(cfg, triage(TicketKind.BUG, missing=["repro?"])).lane is None


def test_model_routing_by_lane_and_escalation() -> None:
    cfg = load_config(ROOT)
    assert cfg.model_for("triage") == ("fast", "claude-haiku")
    assert cfg.model_for("planner", "fast") == ("fast", "claude-haiku")
    assert cfg.model_for("spec", "complex") == ("frontier", "claude-opus")  # the spec is where the thinking goes
    assert cfg.model_for("planner", "complex") == ("standard", "claude-sonnet")
    assert cfg.model_for("coder", "complex", attempt=1) == ("standard", "claude-sonnet")
    assert cfg.model_for("coder", "complex", attempt=2) == ("frontier", "claude-opus")
    assert cfg.model_for("coder", "complex", attempt=5) == ("frontier", "claude-opus")  # capped at the top tier


def test_factory_refuses_repos_outside_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_REPO", "someone-else/their-repo")
    with pytest.raises(ValueError, match="allowed_repos"):
        load_config(ROOT)
    monkeypatch.setenv("FACTORY_REPO", "NtaylorOX/efficient-software-factory")
    forge = GitHubForge(load_config(ROOT), token="x")
    with pytest.raises(RepoScopeError):
        forge.review_comments("https://github.com/someone-else/their-repo/pull/1")
    with pytest.raises(RepoScopeError):
        forge.comment(Ticket(id="gh1", title="t", body="", url="https://github.com/evil/repo/issues/1"), "hi")
    assert forge._number_from("https://github.com/NtaylorOX/efficient-software-factory/pull/7") == "7"


def test_safe_outputs_are_capped(tmp_path: Path) -> None:
    cfg = load_config(ROOT)
    local = LocalForge(tmp_path, cfg)
    ticket = local.add_ticket("t1", "Title", "Body", "NtaylorOX")
    box = Outbox(local, tmp_path / "outbox.jsonl")
    for i in range(10):
        box.comment(ticket, f"comment {i}")
        box.upsert_status(ticket, f"status {i}")
    box.add_labels(ticket, ["kind:bug", "admin", "lane:fast"])
    done = flush_outbox(local, tmp_path / "outbox.jsonl", cfg.safe_outputs)
    assert done["comment"] == cfg.safe_outputs.max_comments_per_ticket
    assert done["status"] == 1 and "status 9" in (tmp_path / "tickets" / "t1.status.md").read_text()
    labels = (tmp_path / "tickets" / "t1.md").read_text()
    assert "kind:bug" in labels and "lane:fast" in labels and "admin" not in labels
    assert done["dropped"] == 10 - cfg.safe_outputs.max_comments_per_ticket + 1
    assert not (tmp_path / "outbox.jsonl").exists() and json.dumps(done)


def test_claude_code_backend_gets_its_own_model_names() -> None:
    cfg = load_config(ROOT)
    cfg.backend = "claude-code"
    assert cfg.model_for("coder", "complex", attempt=2) == ("frontier", "opus")
