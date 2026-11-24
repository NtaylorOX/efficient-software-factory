"""Spec-driven checks: the spec, the plan's coverage of it, test traceability, and the archive merge."""

from pathlib import Path

from software_factory.models import Change, DeltaOp, OpenQuestion, Plan, Requirement, Scenario, ScenarioCheck, Spec
from software_factory.policy import check_plan, check_spec, check_traceability
from software_factory.specs import apply_delta, parse_living

ROOT = Path(__file__).parents[1]
LIVING = {"notes": (ROOT / "specs" / "notes" / "spec.md").read_text()}


def req(op: DeltaOp, title: str, text: str = "The board SHALL work.", n: int = 1) -> Requirement:
    scenarios = [Scenario(id=f"S{i + 1}", name="it works", when="x", then="y") for i in range(n)]
    return Requirement(op=op, capability="notes", title=title, text=text, scenarios=scenarios)


def test_spec_check_holds_the_delta_to_the_living_spec() -> None:
    ok = Spec(why="w", requirements=[req(DeltaOp.MODIFIED, "Add a note"), req(DeltaOp.ADDED, "Search notes")])
    problems, _ = check_spec(ok, LIVING)
    assert problems == ["duplicate scenario id S1"]  # ids are unique across the whole change
    bad = Spec(
        why="w",
        requirements=[
            req(DeltaOp.ADDED, "List notes"),  # exists already
            req(DeltaOp.MODIFIED, "Edit a note"),  # does not exist
            Requirement(op=DeltaOp.ADDED, capability="Notes!", title="t", text="SHALL", scenarios=[]),
        ],
    )
    problems, _ = check_spec(bad, LIVING)
    assert any("use MODIFIED" in p for p in problems)
    assert any("no such requirement" in p for p in problems)
    assert any("lowercase slug" in p for p in problems)
    weak = Spec(why="w", requirements=[Requirement(op=DeltaOp.ADDED, capability="notes", title="t", text="should")])
    assert {p.split(": ")[-1] for p in check_spec(weak, LIVING)[0]} == {
        "the requirement text must use SHALL", "needs at least one scenario"
    }  # fmt: skip


def test_open_question_without_assumption_blocks() -> None:
    spec = Spec(
        why="w",
        requirements=[req(DeltaOp.ADDED, "Search notes")],
        open_questions=[OpenQuestion(question="Partial words?", assumption="yes"), OpenQuestion(question="Limit?")],
    )
    assert check_spec(spec, LIVING) == ([], ["Limit?"])


def test_plan_must_cover_every_scenario() -> None:
    spec = Spec(why="w", requirements=[req(DeltaOp.ADDED, "Search notes", n=2)])
    plan = Plan(summary="s", files_to_change=[], tasks=[], checks=[ScenarioCheck(scenario="S1", by="qa")])
    assert check_plan(spec, plan, qa_available=False) == [
        "scenario S2 has no check in the plan",
        "scenario S1: this lane has no browser QA, check it by test",
    ]


def test_traceability_reads_tags_from_added_test_lines_only() -> None:
    spec = Spec(why="w", requirements=[req(DeltaOp.ADDED, "Search notes", n=2)])
    plan = Plan(summary="s", files_to_change=[], tasks=[],
                checks=[ScenarioCheck(scenario="S1", by="test"), ScenarioCheck(scenario="S2", by="test")])  # fmt: skip
    diff = (
        "+++ b/demo_app/app.py\n+# scenario: S2\n"  # not a test file: does not count
        "+++ b/demo_app/tests/test_app.py\n+# scenario: S1\n+def test_a(): ...\n- # scenario: S2\n"
    )
    change = Change(agent_summary="", files_changed=[], diff=diff)
    assert check_traceability(spec, plan, change, ["demo_app/tests/*"]) == [
        "scenario S2 is checked by test, but no added test has a '# scenario: S2' tag"
    ]


def test_archive_merges_the_delta_in_place() -> None:
    spec = Spec(
        why="w",
        requirements=[
            req(DeltaOp.MODIFIED, "Add a note", "The board SHALL trim notes."),
            req(DeltaOp.REMOVED, "List notes", "Replaced by search.", n=0),
            req(DeltaOp.ADDED, "Search notes"),
        ],
    )
    preamble, blocks = parse_living(apply_delta("notes", LIVING["notes"], spec))
    assert list(blocks) == ["Add a note", "Search notes"]
    assert "SHALL trim notes" in blocks["Add a note"] and "S1" not in blocks["Search notes"]  # ids stay in changes/
    assert preamble.startswith("# notes")
    tag = req(DeltaOp.ADDED, "Tag a note").model_copy(update={"capability": "tags"})
    fresh = apply_delta("tags", "", Spec(why="w", requirements=[tag]))
    assert fresh.startswith("# tags") and "### Requirement: Tag a note" in fresh
