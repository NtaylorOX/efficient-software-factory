from pathlib import Path

from software_factory.config import FactoryConfig, load_config
from software_factory.models import Change, Plan, Ticket
from software_factory.policy import check_change, gate_plan

ROOT = Path(__file__).parents[1]


def cfg() -> FactoryConfig:
    return load_config(ROOT)


def plan(*files: str, risk: str = "") -> Plan:
    return Plan(
        summary="Add search",
        files_to_change=list(files),
        steps=["do it"],
        acceptance_criteria=["works"],
        risk_notes=risk,
    )


def ticket(author: str = "NtaylorOX", body: str = "please add search") -> Ticket:
    return Ticket(id="t1", title="Search", body=body, author=author)


def test_trusted_simple_plan_needs_no_human() -> None:
    d = gate_plan(cfg(), ticket(), plan("demo_app/app.py"), approved=False)
    assert not d.needs_human and not d.blocked


def test_protected_path_is_blocked_even_with_approval() -> None:
    d = gate_plan(cfg(), ticket(), plan("demo_app/app.py", ".github/workflows/x.yml"), approved=True)
    assert d.blocked and d.needs_human


def test_path_outside_scope_is_blocked() -> None:
    assert gate_plan(cfg(), ticket(), plan("README.md"), approved=True).blocked


def test_untrusted_author_and_keywords_need_approval() -> None:
    d = gate_plan(
        cfg(), ticket(author="stranger", body="add a password reset"), plan("demo_app/app.py"), approved=False
    )
    assert d.needs_human and not d.blocked
    assert any("stranger" in r for r in d.reasons) and "keyword: password" in d.reasons
    assert not gate_plan(cfg(), ticket(author="stranger"), plan("demo_app/app.py"), approved=True).needs_human


def test_check_change_catches_scope_secrets_and_size() -> None:
    p = plan("demo_app/app.py")
    secret_diff = "+++ b/demo_app/app.py\n+KEY = 'sk-abcdefghijklmnopqrstuvwxyz'\n"
    problems = check_change(
        cfg(),
        p.files_to_change,
        Change(agent_summary="", files_changed=["demo_app/app.py", "agents/coder.md"], diff=secret_diff),
    )
    assert "file not in plan: agents/coder.md" in problems
    assert "protected path changed: agents/coder.md" in problems
    assert any("secret" in x for x in problems)
    big = "\n".join(f"+line {i}" for i in range(500))
    assert any(
        "too large" in x
        for x in check_change(
            cfg(), p.files_to_change, Change(agent_summary="", files_changed=["demo_app/app.py"], diff=big)
        )
    )


def test_removed_secret_is_not_flagged() -> None:
    diff = "--- a/demo_app/app.py\n-KEY = 'sk-abcdefghijklmnopqrstuvwxyz'\n"
    assert (
        check_change(
            cfg(),
            ["demo_app/app.py"],
            Change(agent_summary="", files_changed=["demo_app/app.py"], diff=diff),
        )
        == []
    )
