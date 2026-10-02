import json
import re
import subprocess
import sys

import pytest

from fleet import ticket as tk
from conftest import BIN, new_args, run

GOOD = {"type": "build", "repo": "fleet-tools", "branch": "task/demo",
        "title": "Do a thing", "body": "Details."}


def good(**over):
    d = dict(GOOD)
    d.update(over)
    return d


def make(**over):
    return tk.create_ticket(good(**over))


# 1. Field rules: one passing and one failing case each ----------------------

@pytest.mark.parametrize("over, ok", [
    ({"type": "fix", "parent": "PARENT"}, True),
    ({"type": "deploy"}, False),
    ({"repo": "a.b_c-1"}, True),
    ({"repo": "bad repo"}, False),
    ({"repo": "x" * 64}, True),
    ({"repo": "x" * 65}, False),
    ({"repo": ""}, False),
    ({"branch": "task/a-1"}, True),
    ({"branch": "main"}, False),
    ({"branch": "task/Upper"}, False),
    ({"branch": "task/"}, False),
    ({"branch": "task/" + "a" * 65}, False),
    ({"branch": "task/ok\n"}, False),
    ({"type": "review", "parent": "PARENT", "commit": "abc1234"}, True),
    ({"type": "review", "parent": "PARENT", "commit": "ABC1234"}, False),
    ({"type": "review", "parent": "PARENT", "commit": "abc123"}, False),
    ({"type": "review", "parent": "PARENT", "commit": "a" * 41}, False),
    ({"commit": "abc1234"}, False),  # commit only for review
    ({"parent": "T-20250101-001"}, False),  # build has no parent
    ({"title": "x" * 100}, True),
    ({"title": "x" * 101}, False),
    ({"title": ""}, False),
    ({"title": "two\nlines"}, False),
    ({"body": "x" * 4000}, True),
    ({"body": "x" * 4001}, False),
    ({"body": ""}, False),
])
def test_field_rules(over, ok):
    if "PARENT" in over.values():
        parent = make()["id"]
        over = {k: (parent if v == "PARENT" else v) for k, v in over.items()}
    if ok:
        tk.create_ticket(good(**over))
    else:
        with pytest.raises(tk.TicketInvalid):
            tk.create_ticket(good(**over))


def test_question_parent_optional_but_must_exist():
    make(type="question")
    parent = make()["id"]
    assert make(type="question", parent=parent)["parent"] == parent
    with pytest.raises(tk.TicketInvalid):
        make(type="question", parent="T-20250101-999")


def test_all_problems_listed():
    errs = tk.validate_new(good(type="deploy", repo="", title="", body=""))
    assert len(errs) >= 4


# 2. created_by and starting status ------------------------------------------

@pytest.mark.parametrize("envvars, who, build_status", [
    ({}, "human", "open"),
    ({"CLAUDECODE": "1"}, "claude", "proposed"),
    ({"CODEX_SANDBOX": "seatbelt"}, "codex", "proposed"),
    ({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, "codex", "proposed"),
])
def test_creator_and_status(monkeypatch, envvars, who, build_status):
    for k, v in envvars.items():
        monkeypatch.setenv(k, v)
    build = make()
    assert (build["created_by"], build["status"]) == (who, build_status)
    for t, extra in (("review", {"commit": "abc1234"}), ("fix", {}), ("question", {})):
        tick = make(type=t, parent=build["id"], **extra)
        assert tick["created_by"] == who and tick["status"] == "open"
        assert tick["assignee"] is None and tick["verdict"] is None


def test_id_format_and_sequence():
    a, b = make(), make()
    assert re.fullmatch(r"T-\d{8}-001", a["id"])
    assert b["id"].endswith("-002")


# 3. Caller can't set tool-owned fields --------------------------------------

@pytest.mark.parametrize("field", ["id", "created_by", "assignee", "status", "verdict", "created_at"])
def test_library_rejects_tool_owned_fields(field):
    with pytest.raises(tk.TicketInvalid, match=field):
        tk.create_ticket(good(**{field: "x"}))


@pytest.mark.parametrize("field", ["id", "created-by", "assignee", "status"])
def test_cli_has_no_such_flags(field):
    r = run("new", *new_args(), f"--{field}", "x")
    assert r.returncode == 2


# 4. review needs commit and parent; parent must exist -----------------------

def test_review_requires_commit_and_parent():
    parent = make()["id"]
    with pytest.raises(tk.TicketInvalid) as e:
        make(type="review")
    assert len(e.value.errors) == 2
    with pytest.raises(tk.TicketInvalid):
        make(type="review", parent=parent)
    with pytest.raises(tk.TicketInvalid):
        make(type="review", commit="abc1234")
    assert make(type="review", parent=parent, commit="abc1234")["type"] == "review"


def test_parent_must_exist():
    with pytest.raises(tk.TicketInvalid, match="does not exist"):
        make(type="fix", parent="T-20250101-001")
    with pytest.raises(tk.TicketInvalid):
        make(type="fix", parent="../../etc/passwd")


# 5. Secret check ------------------------------------------------------------

def test_gitleaks_finding_rejects(leaky_gitleaks):
    with pytest.raises(tk.TicketInvalid, match="secret"):
        make()
    assert tk.list_tickets() == []


def test_gitleaks_missing_rejects(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_GITLEAKS", str(tmp_path / "nope"))
    with pytest.raises(tk.TicketInvalid, match="not installed"):
        make()
    assert tk.list_tickets() == []


def test_gitleaks_receives_title_and_body(monkeypatch, tmp_path):
    fake = tmp_path / "gl-record"
    fake.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/args\ncat > {tmp_path}/stdin\n')
    fake.chmod(0o755)
    monkeypatch.setenv("FLEET_GITLEAKS", str(fake))
    make(title="TITLE-X", body="BODY-Y")
    assert (tmp_path / "args").read_text().strip() == "stdin --no-banner --redact --log-level error"
    seen = (tmp_path / "stdin").read_text()
    assert "TITLE-X" in seen and "BODY-Y" in seen


# 6. Concurrency -------------------------------------------------------------

def test_concurrent_new_get_different_ids():
    import os
    procs = [subprocess.Popen([sys.executable, str(BIN), "new", *new_args(title=f"t{i}")],
                              stdout=subprocess.PIPE, text=True, env=dict(os.environ))
             for i in range(12)]
    ids = [p.communicate()[0].strip() for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert len(set(ids)) == 12
    assert len(tk.list_tickets()) == 12


# 7. approve -----------------------------------------------------------------

def test_approve_human_on_proposed_build(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    tid = make()["id"]
    assert tk.load_ticket(tid)["status"] == "proposed"
    monkeypatch.delenv("CLAUDECODE")
    r = run("approve", tid)
    assert r.returncode == 0, r.stderr
    t = tk.load_ticket(tid)
    assert t["status"] == "open" and t["created_by"] == "claude"


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
def test_approve_refused_for_agents(monkeypatch, var):
    monkeypatch.setenv("CLAUDECODE", "1")
    tid = make()["id"]
    monkeypatch.delenv("CLAUDECODE")
    r = run("approve", tid, extra_env={var: "1"})
    assert r.returncode == 1 and "human" in r.stderr
    assert tk.load_ticket(tid)["status"] == "proposed"


def test_approve_refused_for_other_cases(monkeypatch):
    open_build = make()["id"]  # human build: already open
    review = make(type="review", parent=open_build, commit="abc1234")["id"]
    for tid in (open_build, review):
        assert run("approve", tid).returncode == 1
    assert run("approve", "T-20250101-999").returncode == 1
    assert run("approve", "bogus").returncode == 2
    monkeypatch.setenv("CLAUDECODE", "1")
    tid = make()["id"]
    monkeypatch.delenv("CLAUDECODE")
    assert run("approve", tid).returncode == 0
    assert run("approve", tid).returncode == 1  # no longer proposed


# 8. Unknown field on read ---------------------------------------------------

def test_unknown_field_rejected_on_read():
    tid = make()["id"]
    path = tk.tickets_dir() / f"{tid}.json"
    data = json.loads(path.read_text())
    data["surprise"] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(tk.TicketError, match="unknown field: surprise"):
        tk.load_ticket(tid)
    r = run("show", tid)
    assert r.returncode == 1 and "surprise" in r.stderr


def test_missing_field_rejected_on_read():
    tid = make()["id"]
    path = tk.tickets_dir() / f"{tid}.json"
    data = json.loads(path.read_text())
    del data["body"]
    path.write_text(json.dumps(data))
    with pytest.raises(tk.TicketError, match="missing field"):
        tk.load_ticket(tid)


# CLI behaviour --------------------------------------------------------------

def test_cli_new_list_show():
    r = run("new", *new_args(title="First"))
    assert r.returncode == 0
    first = r.stdout.strip()
    second = run("new", *new_args(title="Second", repo="other")).stdout.strip()
    out = run("list").stdout.splitlines()
    assert out[0].split("  ")[0] == second and out[1].split("  ")[0] == first
    assert out[1] == f"{first}  open  build  fleet-tools  task/demo  First"
    assert [t["id"] for t in json.loads(run("list", "--json").stdout)] == [second, first]
    assert run("list", "--repo", "other").stdout.count("\n") == 1
    assert run("list", "--status", "done").stdout == ""
    assert run("list", "--status", "bogus").returncode == 2
    show = run("show", first)
    assert show.returncode == 0 and "Details." in show.stdout
    assert json.loads(run("show", first, "--json").stdout)["id"] == first


def test_cli_invalid_lists_every_problem():
    r = run("new", "--type", "x", "--repo", "", "--branch", "main", "--title", "", "--body", "")
    assert r.returncode == 2
    assert len(r.stderr.strip().splitlines()) >= 5


def test_cli_body_file(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("from file")
    r = run("new", *new_args(body=None), "--body-file", str(f))
    assert r.returncode == 0
    assert tk.load_ticket(r.stdout.strip())["body"] == "from file"
    assert run("new", *new_args(body=None), "--body-file", str(tmp_path / "missing")).returncode == 2
    assert run("new", *new_args(), "--body-file", str(f)).returncode == 2  # both given


def test_cli_body_file_too_large_is_refused_unread(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * (tk.BODY_MAX * 4 + 1))
    r = run("new", *new_args(body=None), "--body-file", str(f))
    assert r.returncode == 2
    assert "too large" in r.stderr


def test_cli_body_file_just_over_char_limit_still_rejected(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("x" * (tk.BODY_MAX + 1))
    assert run("new", *new_args(body=None), "--body-file", str(f)).returncode == 2


@pytest.mark.parametrize("stamp,ok", [
    ("2025-01-01T00:00:00Z", True),
    ("2024-02-29T23:59:59Z", True),
    ("2025-13-01T00:00:00Z", False),
    ("2025-02-30T00:00:00Z", False),
    ("2025-01-01T24:00:00Z", False),
    ("2025-01-01T00:60:00Z", False),
    ("0000-01-01T00:00:00Z", False),
])
def test_stored_timestamps_are_parsed(stamp, ok):
    data = tk.load_ticket(make()["id"])
    data["created_at"] = stamp
    errors = tk.validate_stored(data)
    assert (not any("created_at" in e for e in errors)) is ok


def test_cli_show_missing_and_bad_id():
    assert run("show", "T-20250101-001").returncode == 1
    assert run("show", "../x").returncode == 2


def test_no_stray_temp_files():
    make()
    names = {p.name for p in tk.tickets_dir().iterdir()}
    assert not [n for n in names if n.startswith(".tmp-")]


def test_tests_never_touch_real_home(tmp_path):
    make()
    assert str(tk.fleet_home()).startswith(str(tmp_path))


# Review follow-ups ----------------------------------------------------------

def test_functions_take_no_creator_override():
    with pytest.raises(TypeError):
        tk.create_ticket(good(), creator="human")
    with pytest.raises(TypeError):
        tk.approve_ticket("T-20250101-001", creator="human")


def test_agent_cannot_become_human_through_library(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert make()["status"] == "proposed"


def test_newest_first_beyond_999_same_second():
    d = tk.tickets_dir()
    d.mkdir(parents=True)
    base = make()
    for seq in (999, 1000):
        t = dict(base, id=f"T-20250101-{seq:03d}", created_at="2025-01-01T00:00:00Z",
                 updated_at="2025-01-01T00:00:00Z")
        (d / f"{t['id']}.json").write_text(json.dumps(t))
    ids = [t["id"] for t in tk.list_tickets()]
    assert ids.index("T-20250101-1000") < ids.index("T-20250101-999")


def test_invalid_utf8_and_bad_timestamp_rejected_on_read():
    tid = make()["id"]
    path = tk.tickets_dir() / f"{tid}.json"
    good_data = json.loads(path.read_text())
    good_data["created_at"] = "yesterday"
    path.write_text(json.dumps(good_data))
    with pytest.raises(tk.TicketError, match="created_at"):
        tk.load_ticket(tid)
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(tk.TicketError, match="cannot read"):
        tk.load_ticket(tid)
    r = run("show", tid)
    assert r.returncode == 1 and "Traceback" not in r.stderr
