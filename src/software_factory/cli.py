"""Command line: `factory <command>`. Run `factory --help` for the list."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from software_factory import git
from software_factory.backends import make_backend
from software_factory.config import load_config, load_role
from software_factory.forge import Forge, GitHubForge, LocalForge
from software_factory.learning import learn
from software_factory.models import Stage, Ticket
from software_factory.pipeline import Factory
from software_factory.publisher import Bundle, Outbox, PublishError, flush_outbox, publish_bundle
from software_factory.state import Store

ROLES = ("planner", "coder", "qa", "reviewer", "retro")


def _forge(root: Path) -> Forge:
    cfg = load_config(root)
    return GitHubForge(cfg) if cfg.repo else LocalForge(root / ".factory", cfg)


def _setup(args: argparse.Namespace) -> tuple[Path, Factory, Forge]:
    """Agent side. With --defer-publish the forge is wrapped so this process can read but never write."""
    root = Path(args.root).resolve()
    cfg = load_config(root)
    if args.backend:
        cfg.backend = args.backend
    state = root / ".factory"
    forge = _forge(root)
    defer = getattr(args, "defer_publish", False)
    if defer:
        forge = Outbox(forge, state / "outbox.jsonl")
    factory = Factory(root, cfg, Store(state), forge, make_backend(cfg.backend), qa=not args.no_qa, defer_publish=defer)
    return root, factory, forge


def cmd_run(args: argparse.Namespace) -> None:
    _, factory, forge = _setup(args)
    tickets = [t for t in forge.open_tickets() if args.ticket in (None, t.id)]
    for ticket in tickets:
        r = factory.handle(ticket)
        print(f"{ticket.id}: {r.stage.value} (run {r.id})")
    if not tickets:
        print("no open tickets with the trigger label")


def cmd_watch(args: argparse.Namespace) -> None:
    while True:
        cmd_run(args)
        time.sleep(args.interval)


def cmd_status(args: argparse.Namespace) -> None:
    store = Store(Path(args.root).resolve() / ".factory")
    for run_id, ticket, stage in store.runs():
        tokens = store.get(run_id).tokens()
        print(f"{run_id}  {ticket:20} {stage:18} tokens {tokens.input_tokens}/{tokens.output_tokens}")


def cmd_ticket(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    forge = LocalForge(root / ".factory", load_config(root))
    print(forge.add_ticket(args.id, args.title, args.body, args.author).id)


def cmd_approve(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    LocalForge(root / ".factory", load_config(root)).approve(args.id)
    print(f"approved {args.id}; run `factory run` to continue")


def cmd_learn(args: argparse.Namespace) -> None:
    root, factory, forge = _setup(args)
    if args.run:
        r = factory.store.get(args.run)
        pr = next((ev["pr"] for ev in r.trace() if ev.get("stage") == Stage.PUBLISHED.value), None)
        if pr is None:
            raise SystemExit(f"run {r.id} is {r.stage.value}; learning reads a published run's PR review")
        applied, rejected = learn(root, factory.cfg, factory.backend, forge.review_comments(pr), run=r)
    else:
        applied, rejected = learn(
            root, factory.cfg, factory.backend, forge.review_comments(args.pr), pr_text=forge.pr_text(args.pr)
        )
    for lesson in applied:
        print(f"learned  [{lesson.role}] {lesson.text}")
    for lesson, why in rejected:
        print(f"rejected [{lesson.role}] {lesson.text} ({why})")
    if args.bundle and applied:
        git.git(root, "add", "--intent-to-add", "agents/lessons")  # so new lesson files show up in the diff
        diff = git.git(root, "diff", "--", "agents/lessons")
        files = git.git(root, "diff", "--name-only", "--", "agents/lessons").split()
        source = args.pr or args.run
        bundle = Bundle(
            kind="lessons", run_id=f"learn-{source.rsplit('/', 1)[-1]}",
            ticket=Ticket(id="lessons", title="Lessons", body="", url=source),
            branch=f"factory/lessons-{int(time.time())}",
            title=f"Agent lessons from {source}", draft=False, allowed_files=files, diff=diff,
            body="Lessons derived by the learning loop. Each line was validated: no permission, tool, path or "
            f"approval changes.\n\nSource: {source}\n\n" + "\n".join(f"- [{x.role}] {x.text}" for x in applied),
        )  # fmt: skip
        out = root / ".factory" / "lessons-bundle.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(bundle.model_dump_json(indent=2))
        print(f"lessons bundle written to {out}")


def cmd_publish(args: argparse.Namespace) -> None:
    """Publisher side: post queued comments, open PRs for ready runs, publish a lessons bundle."""
    root = Path(args.root).resolve()
    cfg, state = load_config(root), root / ".factory"
    forge = _forge(root)
    print(f"posted {flush_outbox(forge, state / 'outbox.jsonl')} queued comments")
    store = Store(state)
    for run_id, _, stage in store.runs():
        if stage != Stage.READY.value:
            continue
        r = store.get(run_id)
        bundle = r.load("bundle", Bundle)
        try:
            url = publish_bundle(root, cfg, forge, bundle)
        except PublishError as exc:
            r.advance(Stage.FAILED, reason=f"publish check failed: {exc}")
            forge.comment(bundle.ticket, f"The publisher refused run `{r.id}`: {exc}")
            continue
        r.advance(Stage.PUBLISHED, pr=url, draft=bundle.draft)
        forge.comment(bundle.ticket, f"Opened {'a draft ' if bundle.draft else ''}pull request: {url}")
        print(f"{bundle.ticket.id}: {url}")
    lessons = state / "lessons-bundle.json"
    if lessons.is_file():
        bundle = Bundle.model_validate_json(lessons.read_text())
        url = publish_bundle(root, cfg, forge, bundle)
        if cfg.auto_merge_lessons and isinstance(forge, GitHubForge):
            forge.merge_pr(url)  # lesson-only, re-validated diff: the one change that needs no human
        lessons.unlink()
        print(f"lessons: {url}")


def cmd_cards(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    print(json.dumps([load_role(root, name).card() for name in ROLES], indent=2))


def cmd_demo(args: argparse.Namespace) -> None:
    from software_factory.demo import run_demo

    dest = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="factory-demo-"))
    out = run_demo(Path(args.root).resolve(), dest, qa=not args.no_qa)
    print(f"\nEverything the demo wrote is in {out}/.factory (runs/, prs/, tickets/) and {out}/agents/lessons/")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="factory", description=__doc__)
    p.add_argument("--root", default=".", help="repo root containing factory.yaml")
    p.add_argument("--backend", choices=["litellm", "claude-code"], help="override factory.yaml")
    p.add_argument("--no-qa", action="store_true", help="skip browser QA")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="process open tickets once")
    s.add_argument("--ticket")
    s.add_argument("--defer-publish", action="store_true", help="queue comments and PRs for `factory publish`")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("watch", help="poll for tickets")
    s.add_argument("--ticket")
    s.add_argument("--interval", type=int, default=60)
    s.set_defaults(fn=cmd_watch)
    sub.add_parser("status", help="list runs").set_defaults(fn=cmd_status)
    s = sub.add_parser("ticket", help="add a local ticket")
    s.add_argument("id")
    s.add_argument("title")
    s.add_argument("body")
    s.add_argument("--author", default="local")
    s.set_defaults(fn=cmd_ticket)
    s = sub.add_parser("approve", help="approve a gated local ticket")
    s.add_argument("id")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("learn", help="derive lessons from a published run or a PR and its review")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--run")
    g.add_argument("--pr", help="PR URL (GitHub) or PR file (local)")
    s.add_argument("--bundle", action="store_true", help="also write a lessons bundle for `factory publish`")
    s.set_defaults(fn=cmd_learn)
    sub.add_parser("publish", help="publisher side: flush comments, open PRs for ready runs").set_defaults(
        fn=cmd_publish
    )
    sub.add_parser("cards", help="print A2A-style agent cards").set_defaults(fn=cmd_cards)
    s = sub.add_parser("demo", help="offline end-to-end demo with a scripted model")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_demo)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
