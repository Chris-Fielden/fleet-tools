"""`fleet doctor`: is this machine set up the way the fleet expects?

Checks the settings, the tools on PATH and whether the tracker backend can be
reached. Read-only, and it never prints a secret: only names, paths and plain
reasons.
"""

from __future__ import annotations

import os
import shlex
import shutil

from . import dispatch as dp
from . import settings as st
from . import ticket as tk
from . import tracker as tr

# (what, required, how to override its location, or None)
TOOLS = (
    ("git", True, None),
    ("gitleaks", True, "FLEET_GITLEAKS"),
    ("herdr", False, "FLEET_HERDR"),
    ("claude", False, None),
    ("codex", False, None),
    ("kubeconform", False, None),
)


def _tool_row(name: str, required: bool, env_var: str | None) -> tuple[str, str, str]:
    command = name
    if env_var and os.environ.get(env_var):
        try:
            command = shlex.split(os.environ[env_var])[0]
        except (ValueError, IndexError):
            return ("fail", f"tool {name}", f"${env_var} is not a usable command")
    found = shutil.which(command)
    if found:
        return ("ok", f"tool {name}", found)
    return ("fail" if required else "warn", f"tool {name}",
            f"'{command}' not found on PATH" + ("" if required else " (optional)"))


def _settings_rows() -> tuple[list[tuple[str, str, str]], dict | None]:
    try:
        cfg = st.load_settings()
    except st.SettingsError as exc:
        return [("fail", "settings", str(exc))], None
    files = [p for p in (st.settings_path(), st.legacy_settings_path()) if p.is_file()]
    rows = [("ok", "settings", ", ".join(str(p) for p in files) if files
             else f"defaults (no {st.settings_path()})")]
    projects = cfg["projects_dir"]
    rows.append(("ok" if projects.is_dir() else "fail", "projects_dir",
                 str(projects) if projects.is_dir() else f"{projects} is not a folder"))
    problem = dp.research_dir_problem(cfg)
    rows.append(("fail" if problem else "ok", "research_dir", problem or str(cfg["research_dir"])))
    rows.append(("ok", "herdr_workspace", cfg["herdr_workspace"]))
    rows.append(("ok", "tracker", cfg["tracker"]))
    return rows, cfg


def checks() -> list[tuple[str, str, str]]:
    rows, cfg = _settings_rows()
    rows += [_tool_row(*t) for t in TOOLS]
    if cfg is not None:
        try:
            rows += tr.get_tracker(settings=cfg).check()
        except (st.SettingsError, tk.TicketError, OSError) as exc:
            rows.append(("fail", "tracker", str(exc)))
    return rows


def main(args=None) -> int:
    rows = checks()
    for state, what, detail in rows:
        print(f"  {state.upper():<4}  {what:<18} {detail}")
    failed = [r for r in rows if r[0] == "fail"]
    print()
    print(f"{len(failed)} problem(s) found" if failed else "all good")
    return 1 if failed else 0
