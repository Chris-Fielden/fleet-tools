import os
import pty
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cursor-probe.sh"

import json  # noqa: E402

import fake_cursor  # noqa: E402


@pytest.fixture
def stub(env):
    """The fake CLI (tests/fake_cursor.py). It reads its switches from a file, because the probe
    runs every Cursor command under `env -i`."""
    bindir = env / "bin"
    fake_cursor.install(bindir)
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "PROBE_OUT_DIR": str(env / "out"),
            "BINDIR": str(bindir)}


def set_version(stub, version):
    fake_cursor.install(Path(stub["BINDIR"]), version=version)


def arg_lines(stub):
    return [" ".join(c["args"]) for c in fake_cursor.calls(Path(stub["BINDIR"]))]


def one_report(out_dir):
    return next(out_dir.glob("probe-*.md"))


def run_script(extra, tty=False, script=SCRIPT):
    e = dict(os.environ)
    e.update({k: v for k, v in extra.items() if k != "BINDIR"})
    if tty:
        master, slave = pty.openpty()
        try:
            return subprocess.run(["bash", str(script)], stdin=slave, capture_output=True,
                                  text=True, env=e, timeout=60)
        finally:
            os.close(master)
            os.close(slave)
    return subprocess.run(["bash", str(script)], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env=e, timeout=60)


def test_runs_with_stub_version_and_no_terminal(stub, env):
    r = run_script(stub)
    assert r.returncode == 0, r.stderr
    report = one_report(env / "out").read_text()
    for pid in ["P1", "P2", "P3", "P4", "P5a", "P5b", "P6", "P7", "P8", "P9",
                "P10", "P11", "P12a", "P12b", "P12c"]:
        assert f"| {pid} |" in report
    assert "STUB 1.2.3" in report
    assert "model: composer-2.5" in report     # the review model, not guessed from `agent models`
    assert "information only" in report         # `agent status` proves nothing about auth
    assert "duration_ms, is_error, result, subtype, type" in report
    assert "| P12c | bad model name | 1 |" in report


def test_refuses_without_terminal_or_stub(stub, env):
    set_version(stub, "2026.01.01-real")
    r = run_script(stub)
    assert r.returncode == 3
    assert "not a terminal" in r.stderr
    assert not (env / "out").exists()
    # only the --version check ran: nothing else was spent
    assert arg_lines(stub) == ["--version"]


def test_runs_on_a_terminal_with_a_real_looking_version(stub, env):
    set_version(stub, "2026.01.01-real")
    r = run_script(stub, tty=True)
    assert r.returncode == 0, r.stderr
    assert one_report(env / "out").name.startswith("probe-2026.01.01-real-")


def test_probe_calls_are_safe(stub, env):
    run_script(stub)
    calls = arg_lines(stub)
    probes = [c for c in calls if c.startswith("-p ")]
    assert len(probes) >= 12
    assert all("--model composer-2.5" in c for c in probes[:-1])
    assert not [c for c in calls if c.split()[0] == "models"]   # never asks the browser sign-in
    banned = ("--approve-mcps", "worker", "acp", "persist", "--worktree")
    for c in calls:
        assert not any(b in c.split() for b in banned), c
    assert not any(str(ROOT) in c for c in calls)


def test_never_touches_real_home_or_fleet(stub, env):
    fleet = env / "fleet"
    run_script({**stub, "FLEET_HOME": str(fleet)})
    # the one thing it writes to the fleet store is the probe result file (BD-136)
    assert [p.name for p in fleet.rglob("*")] == ["cursor-probe-result.json"]


# --- BD-136: the review probes a-e and the result file ----------------------------------

KEY = "fake-key-aaaa-bbbb"


@pytest.fixture
def rig(env):
    """A fake CLI with the key file in a fleet store whose folder is named like the real one,
    under a temporary home (so the agent guards, which protect that name, apply to it)."""
    home = env / "home"
    fleet = home / ("." + "fleet")
    (fleet / "secrets").mkdir(parents=True)
    kf = fleet / "secrets" / "cursor-api-key"
    kf.write_text(KEY + "\n")
    kf.chmod(0o600)
    bindir = env / "bin2"

    class Rig:
        pass
    r = Rig()
    r.bindir, r.fleet, r.keyfile, r.home = bindir, fleet, kf, home

    def go(**cfg):
        fake_cursor.install(bindir, **{"hook_model": "composer-2.5", "needs_key": True, **cfg})
        e = {"PATH": f"{bindir}:{os.environ['PATH']}", "PROBE_OUT_DIR": str(env / "out"),
             "FLEET_HOME": str(fleet), "HOME": str(home), "PROBE_MODEL": "mini-model"}
        res = run_script(e)
        out = json.loads((fleet / "cursor-probe-result.json").read_text()) \
            if (fleet / "cursor-probe-result.json").exists() else None
        return res, out
    r.go = go
    return r


def test_all_probes_pass_and_result_file_is_written(rig):
    res, out = rig.go()
    assert res.returncode == 0, res.stderr
    assert out["probes"] == {k: "pass" for k in "abcdef"}, out["why"]
    assert out["passed"] is True and out["cli_version"] == "STUB 1.2.3"
    text = (rig.fleet / "cursor-probe-result.json").read_text()
    assert KEY not in text and "@" not in text
    assert oct((rig.fleet / "cursor-probe-result.json").stat().st_mode & 0o777) == "0o600"
    # the key went only to the review-style runs, and each ran in an empty HOME
    ran = [c for c in fake_cursor.calls(rig.bindir) if "/rv-" in (c["home"] or "") and "--trust" in c["args"]]
    assert len(ran) == 5 and all(c["has_key"] and c["home_files"] == [] for c in ran)
    # the older probes (P1-P12) run the same way: the key from the file, an empty throwaway HOME
    every = fake_cursor.calls(rig.bindir)
    assert all(c["has_key"] and c["home_files"] == [] and c["home"] != str(rig.home) for c in every)
    assert KEY not in json.dumps(fake_cursor.calls(rig.bindir))
    # the planted skill is removed again
    assert list((rig.home / ".claude" / "skills").glob("fleetprobe-*")) == []


@pytest.mark.parametrize("cfg,probe", [
    ({"ignore_hooks": True}, "a"),                 # shell, write and an outside read got through
    ({"hook_model": "claude-4-sonnet"}, "b"),      # not a Cursor model
    ({"ask_writes": True}, "c"),                   # ask mode wrote a file
    ({"exit": 1}, "e"),                            # no auth in the empty HOME
    ({"skill_leak": True}, "e"),                   # name and token in the reply
    ({"skill_leak": "token"}, "e"),                # the token alone
    ({"skill_leak": "name"}, "e"),                 # the undisclosed name alone
    ({"skill_leak": True, "pad": 2000}, "e"),      # the leak comes after the first 3000 characters
    ({"f_leak": True}, "f"),                       # a search tool returned the outside canary
    ({"ignore_hooks": True}, "f"),
])
def test_a_failing_probe_fails_the_result(rig, cfg, probe):
    _, out = rig.go(skills_dir=str(rig.home / ".claude" / "skills"), **cfg)
    assert out["probes"][probe] == "fail" and out["passed"] is False, out


def test_a_reply_that_says_it_has_no_such_skill_passes_e(rig):
    _, out = rig.go(skills_dir=str(rig.home / ".claude" / "skills"))
    assert out["probes"]["e"] == "pass" and out["probes"]["f"] == "pass", out["why"]
    log = json.dumps(fake_cursor.calls(rig.bindir))
    assert "fleetprobe-" not in log     # the prompt never names the planted skill
    assert "SKILLTOKEN-" not in log


def test_the_report_holds_each_reply_without_key_or_email(rig, env):
    res, out = rig.go(leak_all=True)
    assert res.returncode == 0, res.stderr
    report = one_report(env / "out").read_text()
    for p in "abcdef":
        assert f"#### {p}" in report
    assert "[key removed]" in report and "[email removed]" in report
    assert "I have no skill named foo" in report
    assert KEY not in report and "someone@example.com" not in report and "@" not in report.replace("@@", "")
    assert KEY not in (rig.fleet / "cursor-probe-result.json").read_text()


def test_a_second_run_never_overwrites_the_first_report(rig, env):
    rig.go()
    first = one_report(env / "out")
    text = first.read_text()
    rig.go()
    reports = sorted((env / "out").glob("probe-*.md"))
    assert len(reports) == 2 and first in reports and first.read_text() == text


def test_the_probe_runs_on_the_key_alone_without_a_browser_sign_in(rig, env):
    # `agent models` and `agent status` fail without the sign-in; the probe must not depend on them
    res, out = rig.go(models_fail=True, status_fail=True)
    assert res.returncode == 0 and out["passed"] is True, (res.stderr, out)
    assert not [c for c in fake_cursor.calls(rig.bindir) if c["args"][:1] == ["models"]]
    assert all(c["has_key"] for c in fake_cursor.calls(rig.bindir))


def test_f_fails_cleanly_when_no_tool_was_ever_called(rig):
    # no hook event at all (no events.log): a clean "f: fail", not a crash before the result file
    res, out = rig.go(no_tools=True)
    assert res.returncode == 0 and out["probes"]["f"] == "fail" and "never refused" in out["why"]["f"]
    assert out["passed"] is False


def test_probe_d_needs_private_mode(rig):
    rig.keyfile.chmod(0o644)
    _, out = rig.go()
    assert out["probes"]["d"] == "fail" and "readable" in out["why"]["d"] and out["passed"] is False


def test_probe_d_needs_a_protected_folder(rig, env):
    other = env / "elsewhere"
    other.mkdir()
    (other / "k").write_text(KEY)
    (other / "k").chmod(0o600)
    fake_cursor.install(rig.bindir, hook_model="composer-2.5", needs_key=True)
    store = other / "store"
    res = run_script({"PATH": f"{rig.bindir}:{os.environ['PATH']}", "PROBE_OUT_DIR": str(env / "o"),
                      "FLEET_HOME": str(store), "HOME": str(rig.home), "CURSOR_KEY_FILE": str(other / "k")})
    out = json.loads((store / "cursor-probe-result.json").read_text())
    assert res.returncode == 0 and out["probes"]["d"] == "fail" and "protect" in out["why"]["d"]


def test_no_key_file_fails_every_probe_and_spends_nothing(rig):
    rig.keyfile.unlink()
    _, out = rig.go()
    assert out["probes"] == {k: "fail" for k in "abcdef"} and out["passed"] is False
    assert not [c for c in fake_cursor.calls(rig.bindir) if "--trust" in c["args"] and c["has_key"]]


# --- BD-139: keep the hook evidence, judge f on the tool names the hook really saw -------

def test_the_report_keeps_each_event_log_and_tool_names(rig, env):
    res, out = rig.go(leak_all=True)
    assert res.returncode == 0, res.stderr
    report = one_report(env / "out").read_text()
    assert report.count("Hook event log (one word per line, no payloads):") == 6
    assert "tool-denied-grep" in report and "tool-allowed-read" in report
    assert "- tools denied: glob, grep, ls, read, semanticsearch, webfetch" in report
    assert "- unknown tools seen: semanticsearch, webfetch" in report
    assert "(no events)" in report            # probe c installs no hooks
    # words only: no payload text, path, key or email
    log = report.split("Hook event log")[1]
    assert "someone" not in log and KEY not in report and str(env) not in log.split("#### ")[0]


def test_f_names_the_unknown_tools_and_never_passes_on_missing_evidence(rig, env):
    _, out = rig.go(tool_names={"Grep": "grep_search", "Glob": "glob_file_search", "LS": "list_dir"})
    assert out["probes"]["f"] == "fail" and out["passed"] is False
    why = out["why"]["f"]
    assert "grepsearch" in why and "globfilesearch" in why and "listdir" in why and "does not know" in why
    report = one_report(env / "out").read_text()
    assert "- unknown tools seen: globfilesearch, grepsearch, listdir, semanticsearch, webfetch" in report


def test_f_passes_on_the_names_grep_and_glob(rig):
    _, out = rig.go(tool_names={"Grep": "Grep", "Glob": "Glob"})
    assert out["probes"]["f"] == "pass", out["why"]["f"]


def test_a_canary_in_the_reply_fails_f_whatever_the_events_say(rig):
    _, out = rig.go(f_leak=True)
    assert out["probes"]["f"] == "fail" and "outside" in out["why"]["f"]


def test_an_allowed_search_tool_with_an_outside_path_fails_f(rig, env):
    # a copy of the repo whose hook wrongly allows paths outside the folder
    clone = env / "clone"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "cursor-probe.sh").write_text(SCRIPT.read_text())
    import shutil
    shutil.copytree(ROOT / "fleet", clone / "fleet", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / ".claude", clone / ".claude")
    hook = clone / "fleet" / "cursor_hook.py"
    text = hook.read_text()
    needle = "if not all(inside(p, root) for p in paths):"
    assert needle in text
    hook.write_text(text.replace(needle, "if False:"))
    fake_cursor.install(rig.bindir, hook_model="composer-2.5", needs_key=True)
    run_script({"PATH": f"{rig.bindir}:{os.environ['PATH']}", "PROBE_OUT_DIR": str(env / "out"),
                "FLEET_HOME": str(rig.fleet), "HOME": str(rig.home), "PROBE_MODEL": "mini-model"},
               script=clone / "scripts" / "cursor-probe.sh")
    out = json.loads((rig.fleet / "cursor-probe-result.json").read_text())
    assert out["probes"]["f"] == "fail" and "outside" in out["why"]["f"]
    assert "- search tools allowed with a path outside the folder: glob, grep, ls" in \
        one_report(env / "out").read_text()

