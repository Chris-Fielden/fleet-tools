"""The `ticket` command."""

from __future__ import annotations

import argparse
import json
import sys

from . import ticket as tk


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
    new.add_argument("--parent")

    ls = sub.add_parser("list", help="list tickets, newest first")
    ls.add_argument("--status", choices=tk.STATUSES)
    ls.add_argument("--repo")
    ls.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one ticket")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")

    approve = sub.add_parser("approve", help="approve a proposed build ticket (humans only)")
    approve.add_argument("id")
    return p


def _cmd_new(args) -> int:
    if args.body_file is not None:
        try:
            with open(args.body_file, encoding="utf-8") as fh:
                body = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise tk.TicketInvalid([f"cannot read body file {args.body_file}: {exc}"]) from None
    else:
        body = args.body
    fields = {
        "type": args.type, "repo": args.repo, "branch": args.branch,
        "title": args.title, "body": body,
        "commit": args.commit, "parent": args.parent,
    }
    print(tk.create_ticket(fields)["id"])
    return 0


def _cmd_list(args) -> int:
    tickets = tk.list_tickets()
    if args.status:
        tickets = [t for t in tickets if t["status"] == args.status]
    if args.repo:
        tickets = [t for t in tickets if t["repo"] == args.repo]
    if args.json:
        print(json.dumps(tickets, indent=2))
    else:
        for t in tickets:
            print("  ".join([t["id"], t["status"], t["type"], t["repo"], t["branch"], t["title"]]))
    return 0


def _cmd_show(args) -> int:
    t = tk.load_ticket(args.id)
    if args.json:
        print(json.dumps(t, indent=2))
        return 0
    print(f"{t['id']}  {t['title']}")
    for name in ("status", "type", "repo", "branch", "commit", "parent",
                 "created_by", "assignee", "verdict", "created_at", "updated_at"):
        value = t[name]
        print(f"  {name + ':':<12} {'-' if value is None else value}")
    print()
    print(t["body"])
    return 0


def _cmd_approve(args) -> int:
    t = tk.approve_ticket(args.id)
    print(f"{t['id']} approved (now {t['status']})")
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    handler = {"new": _cmd_new, "list": _cmd_list, "show": _cmd_show, "approve": _cmd_approve}
    try:
        return handler[args.command](args)
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
