"""`fleet ops`: open or reattach the persistent tmux session `fleet` on this
machine, with two windows: `ops` (a shell for fleet commands, with a loud
[LOIC ops] prompt) and `herdr` (herdr attached read-only, for watching).

herdr has no read-only mode of its own, so herdr runs in a second, detached
tmux session (`fleet-herdr`) and the `herdr` window attaches to that with
`tmux attach -r`: a read-only client ignores every key. Agents are spoken to
only through ticket notes, never by typing into herdr.

The programs can be swapped for tests: $FLEET_TMUX and $FLEET_HERDR.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from . import service as sv
from . import ticket as tk

SESSION = "fleet"
HERDR_SESSION = "fleet-herdr"
PROMPT = r"\[\e[1;97;41m\] [LOIC ops] \[\e[0m\] \w \$ "


def _tmux(*args: str):
    return sv._run(sv._command("FLEET_TMUX", "tmux") + list(args))


def _has(session: str) -> bool:
    return _tmux("has-session", "-t", session).returncode == 0


def _must(*args: str) -> None:
    proc = _tmux(*args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise sv.ServiceError(f"tmux {args[0]} failed" + (f": {detail[0][:200]}" if detail else ""))


def rcfile_text() -> str:
    return ("[ -f ~/.bashrc ] && . ~/.bashrc\n"
            f"PS1='{PROMPT}'\n"
            "echo 'LOIC ops shell: type `fleet help`. Agents are spoken to only through ticket notes.'\n")


def _write_rcfile() -> str:
    path = tk.fleet_home() / "ops.bashrc"
    tk.refuse_real_store_in_tests(path)
    sv._write(path, rcfile_text())
    return str(path)


def cmd_ops(attach: bool = True) -> int:
    sv._refuse_agent()
    tmux = sv._command("FLEET_TMUX", "tmux")
    if not shutil.which(tmux[0]):
        raise sv.ServiceError("tmux is not installed; run: sudo apt install -y tmux")
    herdr = shlex.join(sv._command("FLEET_HERDR", "herdr"))
    if not _has(HERDR_SESSION):
        _must("new-session", "-d", "-s", HERDR_SESSION, herdr)
    created = not _has(SESSION)
    if created:
        rc = _write_rcfile()
        _must("new-session", "-d", "-s", SESSION, "-n", "ops", f"bash --rcfile {shlex.quote(rc)} -i")
        _must("new-window", "-t", SESSION, "-n", "herdr", shlex.join(tmux + ["attach", "-r", "-t", HERDR_SESSION]))
        _must("select-window", "-t", f"{SESSION}:ops")
    print(f"tmux session '{SESSION}': " + ("created (windows: ops, herdr)" if created else "already running"))
    if not attach:
        return 0
    if os.environ.get("TMUX"):
        return _tmux("switch-client", "-t", SESSION).returncode
    return subprocess.run(tmux + ["attach", "-t", SESSION]).returncode
