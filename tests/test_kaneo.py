import json
import os
import socket
from pathlib import Path

import pytest

from conftest import new_args, run
from fake_kaneo import FakeKaneo

API_KEY = "fake-key-for-tests-0001"


def fleet_home() -> Path:
    return Path(os.environ["FLEET_HOME"])


def write_config(url: str, mode: int = 0o600, key: str = API_KEY) -> Path:
    home = fleet_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "kaneo.json"
    path.write_text(json.dumps({"url": url, "workspace_id": "ws1", "api_key": key}))
    path.chmod(mode)
    return path


@pytest.fixture
def kaneo():
    fake = FakeKaneo(API_KEY).start()
    write_config(fake.url)
    yield fake
    fake.stop()


def new_ticket(**over) -> str:
    r = run("new", *new_args(**over))
    assert r.returncode == 0, r.stderr
    return r.stdout.split()[0]


def edit_ticket(tid: str, **changes):
    path = fleet_home() / "tickets" / f"{tid}.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def only_task(kaneo) -> dict:
    assert len(kaneo.tasks) == 1
    return next(iter(kaneo.tasks.values()))


def test_creates_project_columns_and_card(kaneo):
    tid = new_ticket(title="Phase 1 review follow-ups")
    # `ticket new` already mirrored it, so start from an empty board
    kaneo.projects.clear(); kaneo.columns.clear(); kaneo.tasks.clear()
    (fleet_home() / "kaneo-map.json").unlink()
    r = run("sync")
    assert f"{tid}  created" in r.stdout
    assert [p["name"] for p in kaneo.projects] == ["fleet-tools"]
    pid = kaneo.projects[0]["id"]
    cols = {c["name"]: c for c in kaneo.columns[pid]}
    assert list(cols) == ["Proposed", "Urgent", "To Do", "In Progress", "In Review", "Done", "Failed"]
    task = only_task(kaneo)
    assert task["status"] == cols["To Do"]["slug"]
    assert task["title"] == f"[{tid}] Phase 1 review follow-ups"
    assert task["priority"] == "no-priority"
    assert task["description"].startswith("Details.\n\n```\ntype: build\nbranch: task/demo\n")
    assert json.loads((fleet_home() / "kaneo-map.json").read_text()) == {tid: task["id"]}


def test_final_columns_are_marked_final(kaneo):
    tid = new_ticket()
    edit_ticket(tid, status="done")
    run("sync")
    cols = {c["name"]: c for cols in kaneo.columns.values() for c in cols}
    assert cols["Done"]["isFinal"] is True and cols["Failed"]["isFinal"] is True
    assert not any(cols[n]["isFinal"] for n in ("Proposed", "To Do", "In Progress", "In Review"))


def test_second_sync_is_unchanged_with_no_writes(kaneo):
    tid = new_ticket()
    run("sync")
    kaneo.requests.clear()
    r = run("sync")
    assert r.returncode == 0
    assert f"{tid}  unchanged" in r.stdout
    assert kaneo.writes == []


def test_status_title_and_body_changes_update_the_same_card(kaneo):
    tid = new_ticket()
    run("sync")
    task_id = only_task(kaneo)["id"]

    edit_ticket(tid, status="claimed")
    r = run("sync")
    assert f"{tid}  updated" in r.stdout
    assert only_task(kaneo)["status"] == "in-progress"

    edit_ticket(tid, title="New title", body="New body")
    r = run("sync")
    assert f"{tid}  updated" in r.stdout
    task = only_task(kaneo)
    assert task["id"] == task_id
    assert task["title"] == f"[{tid}] New title"
    assert task["description"].startswith("New body\n\n```")


@pytest.mark.parametrize("flag, column", [("--done", "done"), ("--cancelled", "done")])
def test_reclosing_a_failed_ticket_moves_its_card(kaneo, flag, column):
    tid = new_ticket()
    edit_ticket(tid, status="failed", close_note="gave up")
    run("sync")
    failed_slug = only_task(kaneo)["status"]
    r = run("close", tid, flag, "--note", "shipped by on-call")
    assert r.returncode == 0, r.stderr
    assert only_task(kaneo)["status"] != failed_slug
    assert run("sync").stdout.count("unchanged") == 1   # the close already mirrored it


def test_card_deleted_in_kaneo_is_recreated(kaneo):
    tid = new_ticket()
    kaneo.tasks.clear()
    r = run("sync")
    assert f"{tid}  created" in r.stdout
    assert len(kaneo.tasks) == 1


def test_dry_run_sends_no_writes(kaneo):
    tid = new_ticket()
    kaneo.projects.clear(); kaneo.columns.clear(); kaneo.tasks.clear()
    kaneo.requests.clear()
    r = run("sync", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"{tid}  would create" in r.stdout
    assert kaneo.writes == []
    assert not kaneo.tasks and not kaneo.projects

    run("sync")
    edit_ticket(tid, status="claimed")
    kaneo.requests.clear()
    r = run("sync", "--dry-run")
    assert f"{tid}  would update" in r.stdout
    assert kaneo.writes == []
    assert only_task(kaneo)["status"] == "to-do"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_loose_config_is_refused(kaneo, mode):
    tid = new_ticket()
    kaneo.requests.clear()
    write_config(kaneo.url, mode=mode)
    r = run("sync")
    assert r.returncode != 0
    assert "chmod 600" in r.stderr
    assert kaneo.requests == []
    # ticket new still works and warns instead of failing
    r = run("new", *new_args(branch="task/second", title="A second job"))
    assert r.returncode == 0
    assert "chmod 600" in r.stderr
    assert tid


def test_missing_config_means_sync_is_off():
    new_ticket()
    r = run("sync")
    assert r.returncode == 0
    assert "sync is off" in r.stdout
    assert not (fleet_home() / "kaneo-map.json").exists()


def _all_output_and_files(results):
    blobs = [r.stdout + r.stderr for r in results]
    for p in fleet_home().rglob("*"):
        if p.is_file() and p.name != "kaneo.json":
            blobs.append(p.read_text(errors="replace"))
    return "\n".join(blobs)


def test_api_key_never_leaks(kaneo):
    results = [run("new", *new_args()), run("sync"), run("sync", "--dry-run")]
    kaneo.fail_all = True  # the server even echoes the key in its error body
    results += [run("new", *new_args()), run("sync")]
    assert any("error:" in r.stdout or "warning" in r.stderr for r in results)
    kaneo.fail_all = False
    kaneo.url  # keep fixture alive
    # wrong key on the server side -> 401, and a dead port
    write_config(kaneo.url, key="some-other-key-0002")
    bad = run("sync")
    results.append(bad)
    assert "HTTP 401" in bad.stdout
    write_config(kaneo.url.replace(str(kaneo._server.server_port), str(_closed_port())))
    results.append(run("sync"))
    text = _all_output_and_files(results)
    assert API_KEY not in text
    assert "some-other-key-0002" not in text


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_kaneo_down_ticket_new_still_succeeds_with_warning():
    write_config(f"http://127.0.0.1:{_closed_port()}/api")
    r = run("new", *new_args())
    assert r.returncode == 0
    tid = r.stdout.strip()
    assert tid == "BD-1"
    assert r.stderr.count("\n") == 1 and "warning" in r.stderr
    assert "Traceback" not in r.stderr
    assert (fleet_home() / "tickets" / f"{tid}.json").exists()
    # and sync reports a plain per-ticket error
    s = run("sync")
    assert f"{tid}  error:" in s.stdout and "Traceback" not in s.stderr
    assert s.returncode == 1


def test_approve_mirrors_the_card(kaneo):
    tid = new_ticket()
    # created by a (simulated) agent -> proposed
    r = run("new", *new_args(type="investigate", branch="task/inv", title="Look into the other matter"), extra_env={"CLAUDECODE": "1"})
    pid_ticket = r.stdout.strip()
    assert [t["status"] for t in kaneo.tasks.values() if pid_ticket in t["title"]] == ["proposed"]
    r = run("approve", pid_ticket)
    assert r.returncode == 0, r.stderr
    assert [t["status"] for t in kaneo.tasks.values() if pid_ticket in t["title"]] == ["to-do"]
    assert tid


def test_one_failing_ticket_does_not_stop_the_others(kaneo):
    a = new_ticket(title="alpha", branch="task/a1")
    kaneo.tasks.clear()
    (fleet_home() / "kaneo-map.json").unlink()
    b = new_ticket(title="boom", branch="task/a2")  # the mirror after `new` fails; ticket is still saved
    c = new_ticket(title="gamma", branch="task/a3")
    kaneo.fail_title = "boom"
    kaneo.tasks.clear()
    (fleet_home() / "kaneo-map.json").unlink(missing_ok=True)
    r = run("sync")
    assert r.returncode == 1
    lines = {ln.split()[0]: ln for ln in r.stdout.splitlines()}
    assert "created" in lines[a] and "created" in lines[c]
    assert "error:" in lines[b]


def test_authorization_header_on_every_request(kaneo):
    tid = new_ticket()
    run("sync")
    edit_ticket(tid, status="claimed")
    run("sync")
    run("sync", "--dry-run")
    assert len(kaneo.requests) > 6
    assert all(r["auth"] == f"Bearer {API_KEY}" for r in kaneo.requests)


def test_redirects_are_not_followed_so_the_key_is_not_forwarded(kaneo):
    other = FakeKaneo(API_KEY).start()
    try:
        kaneo.redirect_to = other.url + "/project"
        new_ticket()
        r = run("sync")
        assert r.returncode == 1
        assert "HTTP 302" in r.stdout
        assert other.requests == []
    finally:
        other.stop()


@pytest.mark.parametrize("key", [API_KEY + "\n", "has space", "tab\there", ""])
def test_malformed_api_key_is_refused_without_echoing_it(kaneo, key):
    write_config(kaneo.url, key=key)
    for r in (run("sync"), run("new", *new_args())):
        assert r.returncode in (0, 1)
        assert "Traceback" not in r.stderr
        assert "api_key" in r.stderr
        assert key.strip() == "" or key.strip() not in r.stdout + r.stderr
    assert kaneo.requests == []


def test_malformed_url_is_a_plain_error(kaneo):
    write_config("http://[bad/api")
    r = run("sync")
    assert r.returncode == 1 and "Traceback" not in r.stderr and "url" in r.stderr


def test_card_in_the_wrong_project_is_moved_back(kaneo):
    tid = new_ticket()
    task = only_task(kaneo)
    right = task["projectId"]
    task["projectId"] = "somewhere-else"
    r = run("sync")
    assert f"{tid}  updated" in r.stdout
    assert only_task(kaneo)["projectId"] == right
    assert f"{tid}  unchanged" in run("sync").stdout


def test_existing_final_column_that_is_not_final_gets_fixed(kaneo):
    tid = new_ticket()
    edit_ticket(tid, status="done")
    pid = kaneo.projects[0]["id"]
    kaneo.columns[pid].append({"id": "c-old", "projectId": pid, "name": "done",
                               "slug": "done", "isFinal": False})
    kaneo.requests.clear()
    assert run("sync", "--dry-run").returncode == 0
    assert kaneo.writes == []
    run("sync")
    done = [c for c in kaneo.columns[pid] if c["name"] == "done"]
    assert len(done) == 1 and done[0]["isFinal"] is True


def test_sync_uses_the_ticket_as_it_is_now_not_a_stale_snapshot(kaneo, monkeypatch):
    from fleet import kaneo as kn
    from fleet import ticket as tk
    tid = new_ticket()
    stale = tk.load_ticket(tid)
    edit_ticket(tid, status="claimed")
    outcomes = dict(kn.sync([stale]))
    assert outcomes[tid] == "updated"
    assert only_task(kaneo)["status"] == "in-progress"


def names(kaneo):
    return [c["name"] for c in kaneo.columns[kaneo.projects[0]["id"]]]


def slug_of(kaneo, name):
    return next(c["slug"] for c in kaneo.columns[kaneo.projects[0]["id"]] if c["name"] == name)


TITLES = ["Alpha", "Quokka", "Zebra", "Walrus", "Ocelot", "Pangolin"]


def test_each_status_lands_in_its_column(kaneo):
    want = {"proposed": "Proposed", "open": "To Do", "claimed": "In Progress",
            "done": "Done", "failed": "Failed", "cancelled": "Done"}
    ids = {}
    for status in want:
        tid = new_ticket(branch=f"task/s{len(ids)}", title=f"{TITLES[len(ids)]} job")
        edit_ticket(tid, status=status)
        ids[tid] = status
    run("sync")
    assert len(kaneo.tasks) == len(want)
    for t in kaneo.tasks.values():
        status = ids[t["title"].split("]")[0][1:]]
        assert t["status"] == slug_of(kaneo, want[status])
        assert t["title"].endswith("(cancelled)") == (status == "cancelled")
    assert not any(c["name"] in ("open", "claimed", "cancelled")
                   for c in kaneo.columns[kaneo.projects[0]["id"]])


def test_existing_columns_are_reused_case_insensitively_and_order_fixed(kaneo):
    tid = new_ticket()
    pid = kaneo.projects[0]["id"]
    kaneo.tasks.clear()
    (fleet_home() / "kaneo-map.json").unlink()
    kaneo.columns[pid] = [
        {"id": "k1", "projectId": pid, "name": "to do", "slug": "to-do", "isFinal": False, "position": 0},
        {"id": "k2", "projectId": pid, "name": "IN PROGRESS", "slug": "in-progress", "isFinal": False, "position": 1},
        {"id": "k3", "projectId": pid, "name": "In Review", "slug": "in-review", "isFinal": False, "position": 2},
        {"id": "k4", "projectId": pid, "name": "DONE", "slug": "done", "isFinal": False, "position": 3},
    ]
    run("sync")
    cols = kaneo.columns[pid]
    assert [c["name"].lower() for c in cols] == [
        "proposed", "urgent", "to do", "in progress", "in review", "done", "failed"]
    assert [c["position"] for c in cols] == list(range(7))
    assert len(cols) == 7  # nothing duplicated
    assert next(c for c in cols if c["id"] == "k4")["isFinal"] is True
    assert only_task(kaneo)["status"] == "to-do"
    assert any(r["path"] == f"/column/reorder/{pid}" for r in kaneo.writes)
    kaneo.requests.clear()
    assert f"{tid}  unchanged" in run("sync").stdout
    assert kaneo.writes == []  # already in order: no reorder sent


def test_cards_move_out_of_legacy_columns_which_are_then_deleted(kaneo):
    tid = new_ticket()
    pid = kaneo.projects[0]["id"]
    old = {"id": "c-old", "projectId": pid, "name": "open", "slug": "open",
           "isFinal": False, "position": 0}
    kaneo.columns[pid].append(old)
    only_task(kaneo)["status"] = "open"
    r = run("sync")
    assert f"{tid}  updated" in r.stdout
    assert only_task(kaneo)["status"] == "to-do"
    assert "open" not in names(kaneo)


def test_legacy_column_with_a_foreign_card_is_kept(kaneo):
    tid = new_ticket()
    pid = kaneo.projects[0]["id"]
    kaneo.columns[pid].append({"id": "c-old", "projectId": pid, "name": "claimed",
                               "slug": "claimed", "isFinal": False, "position": 9})
    kaneo.tasks["human"] = {"id": "human", "projectId": pid, "status": "claimed",
                            "title": "mine", "position": 0}
    run("sync")
    assert "claimed" in names(kaneo)
    assert "human" in kaneo.tasks
    assert tid


def test_dry_run_changes_no_columns(kaneo):
    new_ticket()
    pid = kaneo.projects[0]["id"]
    kaneo.columns[pid] = [{"id": "c-old", "projectId": pid, "name": "open", "slug": "open",
                           "isFinal": False, "position": 0}]
    kaneo.requests.clear()
    assert run("sync", "--dry-run").returncode == 0
    assert kaneo.writes == []
    assert names(kaneo) == ["open"]


def test_failed_column_setup_is_retried_for_the_next_ticket(kaneo):
    a = new_ticket(title="alpha", branch="task/a1")
    kaneo.projects.clear(); kaneo.columns.clear(); kaneo.tasks.clear()
    (fleet_home() / "kaneo-map.json").unlink()
    b = new_ticket(title="beta", branch="task/a2")  # builds the project again, fine
    kaneo.projects.clear(); kaneo.columns.clear(); kaneo.tasks.clear()
    (fleet_home() / "kaneo-map.json").unlink()
    kaneo.fail_column = "In Progress"
    r = run("sync")
    lines = {ln.split()[0]: ln for ln in r.stdout.splitlines()}
    assert sorted("error:" in ln for ln in lines.values()) == [False, True]
    assert sorted(ln.split()[1] for ln in lines.values()) == ["created", "error:"]
    assert len(kaneo.tasks) == 1 and next(iter(kaneo.tasks.values()))["status"] == "to-do"
    assert len(names(kaneo)) == 7


def test_urgent_ticket_goes_to_urgent_column_with_urgent_priority(kaneo):
    tid = new_ticket(priority="urgent")
    assert tid.startswith("EM-")
    task = only_task(kaneo)
    assert task["status"] == slug_of(kaneo, "Urgent")
    assert task["priority"] == "urgent"
    # then through the normal columns, still urgent
    for status, column in (("claimed", "In Progress"), ("done", "Done")):
        edit_ticket(tid, status=status)
        assert f"{tid}  updated" in run("sync").stdout
        task = only_task(kaneo)
        assert task["status"] == slug_of(kaneo, column) and task["priority"] == "urgent"
    assert f"{tid}  unchanged" in run("sync").stdout


def test_normal_ticket_has_no_priority_and_skips_urgent_column(kaneo):
    tid = new_ticket(priority="normal")
    task = only_task(kaneo)
    assert tid.startswith("BD-")
    assert task["status"] == slug_of(kaneo, "To Do") and task["priority"] == "no-priority"


def test_priority_change_updates_card(kaneo):
    tid = new_ticket()
    edit_ticket(tid, priority="urgent")
    assert f"{tid}  updated" in run("sync").stdout
    task = only_task(kaneo)
    assert task["status"] == slug_of(kaneo, "Urgent") and task["priority"] == "urgent"
