"""The `fleet` command. Currently one subcommand: `fleet usage [--json]`."""

from __future__ import annotations

import argparse
import json
import sys

from . import usage as us


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"fleet: {message}", file=sys.stderr)
        raise SystemExit(2)


def main(argv=None) -> int:
    p = _Parser(prog="fleet")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    u = sub.add_parser("usage", help="each AI tool's usage against its limits (local, read-only)")
    u.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    results = us.collect()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(us.line(r))
    return 0
