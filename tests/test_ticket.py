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
    ({"CLAUDECODE": "1"}, "claude", "open"),
    ({"CODEX_SANDBOX": "seatbelt"}, "codex", "open"),
    ({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, "codex", "open"),
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
    assert a["id"] == "BD-1"
    assert b["id"] == "BD-2"


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

def test_approve_human_on_proposed_ticket(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    tid = make(type="investigate")["id"]
    assert tk.load_ticket(tid)["status"] == "proposed"
    monkeypatch.delenv("CLAUDECODE")
    r = run("approve", tid)
    assert r.returncode == 0, r.stderr
    t = tk.load_ticket(tid)
    assert t["status"] == "open" and t["created_by"] == "claude"


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
def test_approve_refused_for_agents(monkeypatch, var):
    monkeypatch.setenv("CLAUDECODE", "1")
    tid = make(type="investigate")["id"]
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
    tid = make(type="investigate")["id"]
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
    assert make(type="investigate")["status"] == "proposed"


def test_same_second_ordered_by_stored_seq_not_id():
    d = tk.tickets_dir()
    d.mkdir(parents=True)
    base = make()
    # ids that would sort the wrong way if anything read meaning from them
    for tid, seq in (("T-20250101-999", 5), ("T-20250101-1000", 6)):
        t = dict(base, id=tid, seq=seq, created_at="2025-01-01T00:00:00Z",
                 updated_at="2025-01-01T00:00:00Z")
        (d / f"{tid}.json").write_text(json.dumps(t))
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


# Short ids -------------------------------------------------------------------

@pytest.mark.parametrize("over, prefix", [
    ({"type": "build"}, "BD"),
    ({"type": "fix", "parent": "BD-1"}, "FX"),
    ({"type": "review", "parent": "BD-1", "commit": "abc1234"}, "RV"),
    ({"type": "question"}, "QN"),
])
def test_prefix_for_each_type(over, prefix):
    make()  # BD-1, the parent
    t = make(**over)
    assert re.fullmatch(prefix + r"-\d+", t["id"])
    assert tk.load_ticket(t["id"])["id"] == t["id"]


def test_counters_count_up_per_prefix_without_padding():
    ids = [make()["id"], make(type="question")["id"], make()["id"], make(type="question")["id"]]
    assert ids == ["BD-1", "QN-1", "BD-2", "QN-2"]
    for _ in range(9):
        last = make()["id"]
    assert last == "BD-11"


def test_lost_counter_file_cannot_duplicate_an_id():
    make(), make()
    (tk.tickets_dir() / tk.COUNTERS_FILE).unlink()
    assert make()["id"] == "BD-3"


def test_damaged_counter_file_is_refused():
    make()
    (tk.tickets_dir() / tk.COUNTERS_FILE).write_text("[1]")
    with pytest.raises(tk.TicketError):
        make()


def test_counter_file_is_not_a_ticket():
    make()
    assert [t["id"] for t in tk.list_tickets()] == ["BD-1"]


def test_concurrent_new_short_ids_unique_and_counted():
    import os
    procs = [subprocess.Popen([sys.executable, str(BIN), "new", *new_args(title=f"t{i}")],
                              stdout=subprocess.PIPE, text=True, env=dict(os.environ))
             for i in range(12)]
    ids = sorted(p.communicate()[0].strip() for p in procs)
    assert all(p.returncode == 0 for p in procs)
    assert sorted(ids, key=lambda i: int(i[3:])) == [f"BD-{n}" for n in range(1, 13)]
    assert sorted(t["seq"] for t in tk.list_tickets()) == list(range(1, 13))


def test_old_ids_keep_working(monkeypatch):
    d = tk.tickets_dir()
    d.mkdir(parents=True)
    old = dict(make(), id="T-20250101-001", created_at="2025-01-01T00:00:00Z",
               updated_at="2025-01-01T00:00:00Z")
    del old["seq"]
    (d / "T-20250101-001.json").write_text(json.dumps(old))
    assert tk.load_ticket("T-20250101-001")["id"] == "T-20250101-001"
    child = make(type="fix", parent="T-20250101-001")
    assert child["parent"] == "T-20250101-001"
    assert run("show", "T-20250101-001").returncode == 0
    monkeypatch.setenv("CLAUDECODE", "1")
    prop = make(type="investigate", branch="task/prop")
    monkeypatch.delenv("CLAUDECODE")
    assert tk.approve_ticket(prop["id"])["status"] == "open"
    assert tk.close_ticket("T-20250101-001", "done")["status"] == "done"
    assert tk.close_ticket(child["id"], "cancelled")["status"] == "cancelled"


def test_bad_ids_refused():
    for bad in ("bd-1", "BD1", "BD-0", "BD-01", "BD-X", "BD--1", "T-1-1", "../BD-1", "B-1"):
        assert run("show", bad).returncode == 2


def test_ordering_by_created_at_then_seq_old_first():
    from fleet import dispatch
    old = {"id": "T-20990101-009", "created_at": "2025-01-01T00:00:00Z"}
    same_a = {"id": "ZZ-9", "created_at": "2025-01-02T00:00:00Z", "seq": 1}
    same_b = {"id": "AA-1", "created_at": "2025-01-02T00:00:00Z", "seq": 2}
    later = {"id": "AA-2", "created_at": "2025-01-03T00:00:00Z", "seq": 1}
    got = sorted([later, same_b, same_a, old], key=tk.oldest_first_key)
    assert got == [old, same_a, same_b, later]
    assert not hasattr(dispatch, "_oldest_first_key")


@pytest.mark.parametrize("damage", ["delete", "stale"])
def test_seq_survives_missing_or_stale_counters(monkeypatch, damage):
    monkeypatch.setattr(tk, "_now", lambda: "2025-01-01T00:00:00Z")
    a, b = make(branch="task/one"), make(type="question")
    path = tk.tickets_dir() / tk.COUNTERS_FILE
    if damage == "delete":
        path.unlink()
    else:
        path.write_text(json.dumps({"BD": 0, "QN": 0, "seq": 0}))
    c = make(branch="task/three")
    assert c["id"] == "BD-2" and (a["seq"], b["seq"], c["seq"]) == (1, 2, 3)
    got = sorted([c, b, a], key=tk.oldest_first_key)
    assert [t["id"] for t in got] == [a["id"], b["id"], c["id"]]


def test_kaneo_title_uses_new_id():
    from fleet import kaneo
    assert kaneo.card_title(make()).startswith("[BD-1] ")


# Lookup table, investigate and priority ------------------------------------

def test_prefix_table_has_every_entry():
    assert tk.TYPE_PREFIX == {"build": "BD", "fix": "FX", "review": "RV",
                              "question": "QN", "investigate": "ID"}
    assert tk.EMERGENCY_PREFIX == "EM"
    assert set(tk.TYPE_PREFIX) == set(tk.TYPES)


def test_investigate_and_urgent_prefixes():
    assert make(type="investigate")["id"] == "ID-1"
    assert make(type="investigate")["id"] == "ID-2"
    urgent = make(priority="urgent")
    assert urgent["id"] == "EM-1"
    assert tk.load_ticket("EM-1")["priority"] == "urgent"
    assert make(type="fix", parent=urgent["id"], priority="urgent")["id"] == "EM-2"
    assert make(type="investigate", priority="urgent")["id"] == "EM-3"
    assert make(priority="normal")["id"] == "BD-1"
    # The prefix is fixed at creation: changing priority later keeps the id.
    path = tk._ticket_path("EM-1")
    data = json.loads(path.read_text())
    data["priority"] = "normal"
    path.write_text(json.dumps(data))
    assert tk.load_ticket("EM-1")["id"] == "EM-1"


# Priority and investigate ---------------------------------------------------

@pytest.mark.parametrize("value, ok", [("normal", True), ("urgent", True), (None, True),
                                       ("high", False), ("URGENT", False), ("", False), (1, False)])
def test_priority_validation(value, ok):
    if ok:
        t = make(priority=value)
        assert t.get("priority") == value if value is not None else "priority" not in t
        assert tk.load_ticket(t["id"]) == t
    else:
        with pytest.raises(tk.TicketInvalid):
            make(priority=value)


def test_priority_is_validated_when_read():
    t = make()
    path = tk._ticket_path(t["id"])
    data = json.loads(path.read_text())
    data["priority"] = "high"
    path.write_text(json.dumps(data))
    with pytest.raises(tk.TicketError):
        tk.load_ticket(t["id"])


def test_priority_flag_on_cli():
    r = run("new", *new_args(), "--priority", "urgent")
    assert r.returncode == 0 and r.stdout.strip() == "EM-1"
    assert json.loads(run("show", "EM-1", "--json").stdout)["priority"] == "urgent"
    assert run("new", *new_args(), "--priority", "high").returncode == 2


def test_investigate_rules():
    t = make(type="investigate")
    assert t["status"] == "proposed" and t["commit"] is None and t["parent"] is None
    assert make(type="investigate", parent=t["id"])["parent"] == t["id"]
    with pytest.raises(tk.TicketInvalid):
        make(type="investigate", commit="abc1234")


@pytest.mark.parametrize("agent_env", [{}, {"CLAUDECODE": "1"}, {"CODEX_SANDBOX": "x"}])
def test_investigate_always_starts_proposed(monkeypatch, agent_env):
    for k, v in agent_env.items():
        monkeypatch.setenv(k, v)
    assert make(type="investigate")["status"] == "proposed"
    assert make(type="investigate", priority="urgent")["status"] == "proposed"


@pytest.mark.parametrize("agent_env", [{}, {"CLAUDECODE": "1"}, {"CODEX_SANDBOX": "x"}])
def test_build_starts_open_whoever_files_it(monkeypatch, agent_env):
    for k, v in agent_env.items():
        monkeypatch.setenv(k, v)
    assert make()["status"] == "open"


def test_ids_are_never_created_without_the_hyphen():
    make(), make(type="question"), make(type="investigate"), make(priority="urgent")
    make(type="fix", parent="BD-1"), make(type="review", parent="BD-1", commit="abc1234")
    names = [t["id"] for t in tk.list_tickets()]
    assert len(names) == 6
    assert all(re.fullmatch(r"[A-Z]{2}-[1-9]\d*", i) for i in names)
    assert all(re.fullmatch(r"[A-Z]{2}-[1-9]\d*\.json", p.name)
               for p in tk.tickets_dir().glob("*.json"))


def test_old_proposed_build_ticket_can_still_be_approved():
    t = make()
    path = tk._ticket_path(t["id"])
    data = json.loads(path.read_text())
    data["status"] = "proposed"
    path.write_text(json.dumps(data))
    assert tk.approve_ticket(t["id"])["status"] == "open"
