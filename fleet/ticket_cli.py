"""The `ticket` command."""

from __future__ import annotations

import argparse
import json
import os
import sys

from pathlib import Path

from . import areas
from . import dispatch as dp
from . import dupes
from . import kaneo
from . import notify
from . import risk as rk
from . import settings as st
from . import ticket as tk
from . import ticketcost as tc
from . import tracker as tr


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"ticket: {message}", file=sys.stderr)
        raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ticket")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)

    new = sub.add_parser("new", help="create a ticket")
    new.add_argument("--type", required=True, dest="type")
    new.add_argument("--repo", required=True)
    new.add_argument("--branch", required=True)
    new.add_argument("--title", required=True)
    body = new.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file")
    new.add_argument("--commit")
    new.add_argument("--parent", type=tk.normalize_id)
    new.add_argument("--priority", choices=tk.PRIORITIES)
    new.add_argument("--origin", type=tk.normalize_id,
                     help="the handoff ticket this one was filed from (the dispatcher may then auto-open it)")
    new.add_argument("--area", action="append", dest="areas", metavar="GLOB",
                     help="a repo-relative path glob the work touches; repeat for more (file-area lock)")
    new.add_argument("--force", action="store_true",
                     help="file it although a ticket with a very similar title exists (humans only; "
                          "a branch already in use is always refused)")

    claim = sub.add_parser("claim-area", help="a chat that will change a repo files a build ticket "
                           "declaring the areas it touches, instead of editing in parallel")
    claim.add_argument("--repo", required=True)
    claim.add_argument("--branch", required=True)
    claim.add_argument("--title", required=True)
    cbody = claim.add_mutually_exclusive_group(required=True)
    cbody.add_argument("--body")
    cbody.add_argument("--body-file")
    claim.add_argument("--area", action="append", dest="areas", metavar="GLOB", required=True,
                       help="a repo-relative path glob the work touches; repeat for more")

    rel = sub.add_parser("release-area", help="drop a ticket's file-area lock (humans only)")
    rel.add_argument("id", type=tk.normalize_id)

    ls = sub.add_parser("list", help="list tickets, newest first (--status review: oldest first)")
    ls.add_argument("--status", choices=tk.STATUSES)
    ls.add_argument("--repo")
    ls.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one ticket")
    show.add_argument("id", type=tk.normalize_id)
    show.add_argument("--json", action="store_true")

    approve = sub.add_parser("approve", help="approve a proposed ticket (humans only)")
    approve.add_argument("id", type=tk.normalize_id)

    prio = sub.add_parser("priority", help="make an unfinished ticket urgent or normal (humans only)")
    prio.add_argument("id", type=tk.normalize_id)
    prio.add_argument("priority", choices=tk.PRIORITIES)

    sub.add_parser("fix-titles", help="retitle open tickets squashed from multi-line text (humans only)")

    close = sub.add_parser("close", help="close an open, claimed or in-review ticket; --done/--cancelled also re-close a failed one, with --note (humans only)")
    close.add_argument("id", type=tk.normalize_id)
    outcome = close.add_mutually_exclusive_group(required=True)
    for name in tk.CLOSE_OUTCOMES:
        outcome.add_argument(f"--{name}", dest="outcome", action="store_const", const=name)
    close.add_argument("--note")

    submit = sub.add_parser("submit", help="hand a human-built ticket with commits and no agent to review (humans only)")
    submit.add_argument("id", type=tk.normalize_id)

    note = sub.add_parser("note", help="add a note to a ticket (findings, remarks)")
    note.add_argument("id", type=tk.normalize_id)
    note.add_argument("text")

    summ = sub.add_parser("summary", help="set a ticket's plain summary: what it is, why it matters, where it stands")
    summ.add_argument("id", type=tk.normalize_id)
    summ.add_argument("text")

    rr = sub.add_parser("review-result",
                        help="record a saved review's result on a branch's tickets (run by scripts/review.sh)")
    rr.add_argument("--branch", required=True)
    rr.add_argument("--repo", required=True)
    rr.add_argument("--file", required=True, help="a review saved in .reviews/")
    rr.add_argument("--leftovers", action="store_true",
                    help="after a PASS, save the review's SHOULD FIX items as a note on the ticket")
    rr.add_argument("--propose-leftovers", action="store_true",
                    help="with --leftovers, also propose one fix ticket (parent: the reviewed ticket) "
                         "holding those items")

    risk = sub.add_parser("risk", help="show a ticket's risk level, judge a diff, or raise a level")
    risk.add_argument("id", nargs="?", type=tk.normalize_id)
    risk.add_argument("--diff", metavar="BASE...BRANCH",
                      help="level for the files this range changes, with the rule for each")
    risk.add_argument("--dir", default=".", help="the git repo to diff in (default: here)")
    risk.add_argument("--raise", dest="raise_to", choices=rk.LEVELS, metavar="LEVEL",
                      help="raise the stored level (a level can never be lowered)")

    gate = sub.add_parser("gate", help="merge gate: risk level and review check for a task branch")
    gate.add_argument("--base", required=True)
    gate.add_argument("--branch", required=True)
    gate.add_argument("--dir", default=".", help="the git repo (default: here)")
    gate.add_argument("--repo", help="only tickets filed against this repo name count")
    gate.add_argument("--worktree", required=True, help="the task worktree holding .reviews/")
    gate.add_argument("--ticket", type=tk.normalize_id, help="the ticket being shipped: it must be on this branch, and its id "
                                       "is the word to type")
    gate.add_argument("--field", choices=("risk", "confirm", "ok", "reviews", "others"),
                      help="print just one value instead of the report")

    sub.add_parser("dupes", help="list likely duplicate tickets: shared branch or similar title")

    sync = sub.add_parser("sync", help="mirror every ticket onto the Kaneo board")
    sync.add_argument("--dry-run", action="store_true",
                      help="show what would happen without changing Kaneo")
    return p


def _warn(message: str) -> None:
    print(f"ticket: {message}", file=sys.stderr)


def _tracker() -> tr.Tracker:
    return tr.get_tracker(_warn)


def guard_duplicates(fields: dict, force: bool = False, prog: str = "ticket") -> None:
    """Refuse a branch another unfinished ticket uses. For a very similar title, a
    human at a terminal is asked (`force` skips the question); an agent, or no
    terminal, gets a refusal naming the ticket: add a note there instead."""
    tickets = tk.list_tickets()
    for t in dupes.branch_conflicts(fields, tickets):
        raise tk.TicketInvalid([f"branch {fields['branch']} is already used by {t['id']} "
                                f"({t['status']}: {t['title']}); add a note to {t['id']} instead"])
    similar = dupes.similar_titles(fields, tickets)
    human = tk.current_creator() == "human"
    if not similar or force and human:
        return
    named = ", ".join(f"{t['id']} ({r:.2f}: {t['title']})" for t, r in similar)
    if human and sys.stdin.isatty():
        print(f"{prog}: the title is very similar to {named}", file=sys.stderr)
        print("File it anyway? [y/N] ", end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip().lower() in ("y", "yes"):
            return
        raise tk.TicketInvalid(["not filed"])
    force = "--force" if prog == "ticket" else "`ticket new --force`"
    hint = f"use {force} to file it anyway" if human else f"add a note to {similar[0][0]['id']} instead"
    raise tk.TicketInvalid([f"the title is very similar to {named}; {hint}"])


def _cmd_dupes(args) -> int:
    found = dupes.pairs(tk.list_tickets())
    for p in found:
        print(dupes.pair_line(p))
    if not found:
        print("no likely duplicates")
    return 0


def _read_body(args) -> str:
    if args.body_file is not None:
        try:
            # Body is at most BODY_MAX characters (up to 4 bytes each in UTF-8);
            # refuse oversized files before reading them into memory.
            limit = tk.BODY_MAX * 4
            if os.path.getsize(args.body_file) > limit:
                raise tk.TicketInvalid([f"body file {args.body_file} is too large (over {limit} bytes)"])
            with open(args.body_file, encoding="utf-8") as fh:
                body = fh.read(limit + 1)
        except (OSError, UnicodeDecodeError) as exc:
            raise tk.TicketInvalid([f"cannot read body file {args.body_file}: {exc}"]) from None
    else:
        body = args.body
    return body


def _cmd_new(args) -> int:
    body = _read_body(args)
    fields = {
        "type": args.type, "repo": args.repo, "branch": args.branch,
        "title": args.title, "body": body,
        "commit": args.commit, "parent": args.parent, "priority": args.priority,
        "origin": args.origin, "areas": args.areas,
    }
    guard_duplicates(fields, force=args.force)
    ticket = _tracker().create(fields)
    print(ticket["id"])
    notify.notify_quietly(ticket, notify.event_for_new(ticket), _warn)
    return 0


def _cmd_claim_area(args) -> int:
    """File the build ticket a chat works through, and say who it will queue behind: tickets of
    the same repo, still unfinished, whose declared areas meet these."""
    fields = {"type": "build", "repo": args.repo, "branch": args.branch, "title": args.title,
              "body": _read_body(args), "areas": args.areas}
    guard_duplicates(fields)
    ahead = [t for t in tk.list_tickets()
             if t["repo"] == args.repo and t["status"] in tk.ACTIVE_STATUSES and t.get("areas")
             and areas.first_overlap(args.areas, t["areas"])]
    ticket = _tracker().create(fields)
    print(ticket["id"])
    notify.notify_quietly(ticket, notify.event_for_new(ticket), _warn)
    if ahead:
        print(f"{ticket['id']} queues behind {', '.join(t['id'] for t in ahead)} (overlapping areas); "
              "do not edit until it is claimed", file=sys.stderr)
    print(f"{ticket['id']}: change the repo only through this ticket's task folder once it is claimed",
          file=sys.stderr)
    return 0


def _cmd_release_area(args) -> int:
    creator = tk.current_creator()
    if creator != "human":
        raise tk.TicketError(f"only a human can release an area lock (running as {creator})")
    print(f"{args.id} area lock released" if areas.release(args.id) else f"{args.id} holds no area lock")
    return 0


def _cmd_list(args) -> int:
    tickets = _tracker().list_tickets()
    if args.status:
        tickets = [t for t in tickets if t["status"] == args.status]
        if args.status == "review":
            tickets.reverse()  # the review queue: oldest first
    if args.repo:
        tickets = [t for t in tickets if t["repo"] == args.repo]
    if args.json:
        print(json.dumps(tickets, indent=2))
    else:
        for t in tickets:
            print("  ".join([t["id"], t["status"], t["type"], t["repo"], t["branch"], t["title"]]))
    return 0


def _usage_line(t: dict) -> str:
    """Token use from the local session files; never stops `ticket show`."""
    try:
        return tc.usage_line(tc.for_ticket(t))
    except Exception:   # noqa: BLE001 - informational only
        return "unavailable"


def _cmd_show(args) -> int:
    t = _tracker().show(args.id)
    if args.json:
        print(json.dumps(t, indent=2))
        return 0
    print(f"{t['id']}  {t['title']}")
    print()
    print(tk.summary_text(t))
    print()
    for name in ("status", "priority", "risk", "type", "repo", "branch", "commit", "parent",
                 "created_by", "assignee", "verdict", "close_note", "base_commit", "created_at", "updated_at"):
        value = tk.verdict_line(t.get(name)) if name == "verdict" else t.get(name)
        print(f"  {name + ':':<12} {'-' if value is None else value}")
    print(f"  {'usage:':<12} {_usage_line(t)}")
    print()
    print(t["body"])
    for n in t.get("notes", []):
        print(f"\n--- note by {n['by']}, {n['at']} ---")
        print(n["text"])
    return 0


def _cmd_summary(args) -> int:
    t = _tracker().set_summary(args.id, args.text)
    print(f"{t['id']} summary saved")
    return 0


def _cmd_approve(args) -> int:
    t = _tracker().approve(args.id)
    print(f"{t['id']} approved (now {t['status']})")
    return 0


def _cmd_priority(args) -> int:
    t = _tracker().set_priority(args.id, args.priority)
    print(f"{t['id']} priority is {t.get('priority', 'normal')}")
    return 0


def _cmd_fix_titles(args) -> int:
    changed = _tracker().fix_titles()
    for t in changed:
        print(f"{t['id']} title is now: {t['title']}")
    print(f"{len(changed)} ticket(s) retitled")
    return 0


def _cmd_close(args) -> int:
    t = _tracker().close(args.id, args.outcome, args.note)
    print(f"{t['id']} closed (now {t['status']})")
    notify.notify_quietly(t, "failed" if t["status"] == "failed" else None, _warn)
    return 0


def _cmd_submit(args) -> int:
    creator = tk.current_creator()
    if creator != "human":
        raise tk.TicketError(f"only a human can submit a ticket for review (running as {creator})")
    t = _tracker().show(args.id)
    repo = st.load_settings()["projects_dir"] / t["repo"]
    main = dp.main_branch(repo)
    if main is None:
        raise tk.TicketError(f"{t['repo']} has no main or master branch")
    ref = f"refs/heads/{t['branch']}"
    tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    ahead = dp._git(repo, "rev-list", "--count", f"{main}..{ref}")
    if tip.returncode != 0 or ahead.returncode != 0:
        raise tk.TicketError(f"branch {t['branch']} not found in {t['repo']}")
    if int(ahead.stdout.strip()) == 0:
        raise tk.TicketError(f"branch {t['branch']} has no commits that {main} lacks; nothing to review")
    base = dp._git(repo, "merge-base", main, ref).stdout.strip() or None
    t = _tracker().submit(args.id, tip.stdout.strip(), base)
    print(f"{t['id']} submitted (now {t['status']}); once its tip has a passing review it moves to review")
    return 0


def _cmd_note(args) -> int:
    t = _tracker().add_note(args.id, args.text)
    print(f"{t['id']} note added ({len(t['notes'])} so far)")
    return 0


def _cmd_review_result(args) -> int:
    try:
        review_text = Path(args.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise tk.TicketInvalid([f"cannot read review file {args.file}: {exc}"]) from None
    info = rk.parse_review(review_text)
    if info["verdict"] is None or not info["commit"]:
        raise tk.TicketInvalid([f"{args.file} has no VERDICT line or no Commit header"])
    changed = tk.record_verdict(args.branch, args.repo, info["verdict"], info["commit"], info["reviewer"],
                                counted=info["counted"])
    if not info["counted"]:
        print(f"the {info['reviewer']} review is not counted; it is saved but no vote was recorded")
    elif not changed:
        print(f"no open, claimed or in-review ticket on {args.branch} in {args.repo}; nothing recorded")
    for t in changed:
        print(f"{t['id']} verdict {t['verdict']['result']} recorded")
        kaneo.mirror_quietly(t, _warn)
    if args.leftovers and info["verdict"] == "PASS" and changed:
        _save_leftovers(args, info, changed, review_text)
    return 0


def _save_leftovers(args, info: dict, changed: list, review_text: str) -> None:
    """Keep a PASS review's SHOULD FIX items where they will not be lost.
    Never changes the verdict or the exit status: a problem here is a warning."""
    items = rk.should_fix_items(review_text)
    if not items:
        return
    label = f"SHOULD FIX leftovers from the {info['reviewer']} review of {info['commit'][:7]} (PASS):\n"
    text = label + items
    if len(text) > tk.NOTE_MAX:
        text = text[:tk.NOTE_MAX - 15].rstrip() + "\n[...truncated]"
    for t in changed:
        try:
            if any(n["text"] == text for n in t.get("notes", [])):
                continue  # the same review was recorded twice
            _tracker().add_note(t["id"], text)
            print(f"{t['id']} SHOULD FIX leftovers saved as a note")
        except (tk.TicketError, tk.TicketInvalid, st.SettingsError, OSError, UnicodeDecodeError) as exc:
            _warn(f"could not save the SHOULD FIX leftovers on {t['id']}: {exc}")
    if not args.propose_leftovers:
        return
    parent = changed[0]
    try:
        heading = f"From the {info['reviewer']} review of {info['commit'][:7]}:"
        # One grouped ticket per reviewed ticket (the helper reuses an active one, or files it)
        ticket, what = tk.file_leftovers_items(parent, heading, items, _tracker().create)
        if what == "created":
            print(f"{ticket['id']} proposed for the leftover SHOULD FIX items")
        elif what == "added":
            print(f"{ticket['id']} leftover items added from the {info['reviewer']} review")
            kaneo.mirror_quietly(ticket, _warn)
    except (tk.TicketError, tk.TicketInvalid, st.SettingsError, OSError, UnicodeDecodeError) as exc:
        _warn(f"could not propose a leftovers ticket for {parent['id']}: {exc}")


def _cmd_risk(args) -> int:
    if args.raise_to:
        if not args.id or args.diff:
            raise tk.TicketInvalid(["--raise needs a ticket id and cannot be combined with --diff"])
        t = _tracker().set_risk(args.id, args.raise_to)
        print(f"{t['id']} risk raised to {t['risk']}")
        return 0
    if not args.id and not args.diff:
        raise tk.TicketInvalid(["give a ticket id, --diff BASE...BRANCH, or both"])
    stored = tk.load_ticket(args.id) if args.id else None
    level = rk.stored_level(stored) if stored else None
    if args.diff:
        rules = rk.rules_for_range(args.dir, args.diff)
        rows, diff_level = rk.classify_all(rk.changed_files(args.dir, args.diff), rules)
        for path, lv, rule in rows:
            print(f"  {lv:<6} {path}   ({rule})")
        if not rows:
            print("  (no files change)")
        print(f"diff level: {diff_level}")
        level = rk.higher(level, diff_level)
        if stored:
            print(f"stored level: {rk.stored_level(stored)}")
    print(f"risk: {level}")
    return 0


def _accepted_commit(args, tickets: list, tip: str, reviews: dict) -> tuple[str | None, str | None]:
    """(commit to tolerate a Codex FAIL on, or None; why the request was refused, or None).
    The human's merge with leftovers (EM-8 step G) names the commit in the environment; this
    checks it again on its own, so the merge script's gate is a second, independent check:
    a human is running, it is exactly the tip, the ticket being merged carries the
    supervisor's mark for exactly that tip, and its recorded verdict is Claude PASS with
    Codex FAIL on it. The risk level and the review files are judged by rk.check_reviews."""
    named = os.environ.get(rk.ACCEPT_ENV)
    if not named:
        return None, None
    from . import supervisor_run as sr   # late: only this path needs the supervisor's state
    if tk.current_creator() != "human":
        return None, "only a human can accept leftovers"
    if not tk.COMMIT_RE.fullmatch(named) or len(named) != 40 or named != tip:
        return None, f"the commit named ({named[:40]}) is not the branch tip {tip[:8]}"
    mine = next((t for t in tickets if t["id"] == (args.ticket or "")), None)
    if mine is None:
        return None, "it needs --ticket: the ticket whose mark covers this commit"
    try:
        marked = sr.leftovers_for(sr.load_state(), mine["id"], tip)
    except Exception as exc:   # an unreadable state is never a mark
        return None, f"the supervisor's marks cannot be read ({type(exc).__name__})"
    if marked is None:
        return None, f"{mine['id']} has no ready-with-leftovers mark on {tip[:8]}"
    v = mine.get("verdict") or {}
    got = {k: r.get("result") for k, r in (v.get("reviews") or {}).items()}
    if v.get("commit") != tip or got.get("claude") != "PASS" or got.get("codex") != "FAIL":
        return None, f"{mine['id']}'s recorded verdict is not Claude PASS with Codex FAIL on {tip[:8]}"
    return tip, None


def _gate(args) -> dict:
    """Everything the merge script needs: level, reasons, review problems, and
    the word the human must type to confirm."""
    rev_range = f"{args.base}...{args.branch}"
    rules = rk.rules_for_range(args.dir, rev_range)
    rows, diff_level = rk.classify_all(rk.changed_files(args.dir, rev_range), rules)
    tickets = tk.list_tickets()
    stored, ids = rk.level_for_branch(tickets, args.branch, rules, args.repo)
    level = rk.higher(stored, diff_level)
    full = rk._git(args.dir, "rev-parse", "--verify", f"{args.branch}^{{commit}}").strip()
    reviews = rk.latest_reviews(Path(args.worktree) / ".reviews", full)
    accept, refused = _accepted_commit(args, tickets, full, reviews)
    problems = ([f"merging with leftovers is not allowed: {refused}"] if refused else []) \
        + rk.check_reviews(reviews, level, accept)
    # The ticket whose id is typed to confirm: the one being shipped, else the highest-risk one
    # on the branch.
    best = max((t for t in tickets if t["id"] in ids),
               key=lambda t: rk.rank(rk.stored_level(t, rules)), default=None)
    others = []
    if args.ticket:
        mine = next((t for t in tickets if t["id"] == args.ticket), None)
        if mine is None:
            raise tk.TicketError(f"no such ticket: {args.ticket}")
        if mine["branch"] != args.branch or (args.repo and mine["repo"] != args.repo):
            raise tk.TicketError(f"{mine['id']} is on {mine['repo']} {mine['branch']}, "
                                 f"not {args.branch}")
        best = mine
        others = [t for t in tickets if t["id"] in ids and t["id"] != mine["id"]
                  and t["status"] != "done"]
    return {"level": level, "diff_level": diff_level, "stored": stored, "ids": ids, "rows": rows,
            "commit": full, "reviews": reviews, "problems": problems,
            "confirm": best["id"] if best else args.branch.removeprefix("task/"),
            "others": [f"{t['id']} ({t['status']}) {t['title']}" for t in others]}


def _cmd_gate(args) -> int:
    g = _gate(args)
    if args.field == "risk":
        print(g["level"])
    elif args.field == "confirm":
        print(g["confirm"])
    elif args.field == "others":
        print("\n".join(g["others"]))
    elif args.field == "ok":
        print("yes" if not g["problems"] else "no")
    elif args.field == "reviews":
        print(" ".join(sorted(g["reviews"])))
    else:
        print(f"Risk level: {g['level'].upper()}  (files say {g['diff_level']}, "
              f"ticket says {g['stored'] or 'nothing: no ticket found'})")
        if g["level"] != g["diff_level"]:
            print(f"  raised by the ticket ({', '.join(g['ids'])}), not by the files")
        shown = [r for r in g["rows"] if r[1] == g["diff_level"]]
        for path, lv, rule in shown[:15]:
            print(f"  {lv:<6} {path}   ({rule})")
        if len(shown) > 15:
            print(f"  ... and {len(shown) - 15} more")
        print(f"Commit: {g['commit']}")
        for kind, info in sorted(g["reviews"].items()):
            print(f"  review by {info['reviewer']}: {info['verdict'] or 'no verdict'}")
        for p in g["problems"]:
            print(f"  BLOCKED: {p}")
    return 0 if not g["problems"] else 1


def _cmd_sync(args) -> int:
    try:
        tickets = tk.list_tickets()
        failed = False
        for ticket_id, outcome in kaneo.sync(tickets, dry_run=args.dry_run):
            print(f"{ticket_id}  {outcome}")
            failed = failed or outcome.startswith("error: ")
    except kaneo.NotConfigured:
        print(f"Kaneo sync is off: no {kaneo.config_path()} (create it to turn sync on).")
        return 0
    return 1 if failed else 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    handler = {"new": _cmd_new, "list": _cmd_list, "show": _cmd_show, "approve": _cmd_approve,
               "priority": _cmd_priority, "fix-titles": _cmd_fix_titles, "close": _cmd_close, "submit": _cmd_submit, "note": _cmd_note, "summary": _cmd_summary, "review-result": _cmd_review_result, "risk": _cmd_risk, "gate": _cmd_gate,
               "dupes": _cmd_dupes, "sync": _cmd_sync,
               "claim-area": _cmd_claim_area, "release-area": _cmd_release_area}
    try:
        return handler[args.command](args)
    except (rk.RiskError, st.SettingsError) as exc:
        print(f"ticket: {exc}", file=sys.stderr)
        return 1
    except kaneo.KaneoError as exc:
        print(f"ticket: {exc}", file=sys.stderr)
        return 1
    except tk.TicketInvalid as exc:
        for line in exc.errors:
            print(f"ticket: {line}", file=sys.stderr)
        return 2
    except tk.TicketError as exc:
        print(f"ticket: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ticket: {exc}", file=sys.stderr)
        return 1
