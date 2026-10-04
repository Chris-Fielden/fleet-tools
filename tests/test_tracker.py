"""The tracker interface, the single settings file and `fleet doctor`."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, new_args, run
from fake_kaneo import FakeKaneo
from fleet import settings as st
from fleet import ticket as tk
from fleet import tracker as tr
from test_kaneo import API_KEY, edit_ticket, fleet_home, only_task, slug_of, write_config

FLEET = ROOT / "bin" / "fleet"


def fields(**over):
    f = {"type": "build", "repo": "fleet-tools", "branch": "task/demo",
         "title": "Do a thing", "body": "Details."}
    f.update(over)
    return f


def write_settings(name="settings.json", **given):
    fleet_home().mkdir(parents=True, exist_ok=True)
    (fleet_home() / name).write_text(json.dumps(given))


def fleet(*args):
    return subprocess.run([sys.executable, str(FLEET), *args], capture_output=True, text=True,
                          env=dict(os.environ))


# --- settings ------------------------------------------------------------------

def test_defaults_are_documented_values():
    cfg = st.load_settings()
    assert cfg["herdr_workspace"] == "fleet" and cfg["tracker"] == "local"
    assert cfg["id_prefixes"] == tk.TYPE_PREFIX and cfg["emergency_prefix"] == "EM"
    assert cfg["projects_dir"] == Path("~/projects").expanduser().absolute()


def test_settings_json_wins_over_legacy_dispatch_json():
    write_settings("dispatch.json", herdr_workspace="old", max_running=2)
    write_settings(herdr_workspace="new")
    cfg = st.load_settings()
    assert cfg["herdr_workspace"] == "new" and cfg["max_running"] == 2


NULL_FILE = {"settings.json": "null"}


@pytest.mark.parametrize("given", [
    {"nope": 1}, {"tracker": "jira"}, {"id_prefixes": {"build": "b"}},
    {"id_prefixes": {"wat": "AB"}}, {"emergency_prefix": "EMG"},
    {"kaneo": {"api_key": "x"}}, {"kaneo": {"url": "http://evil"}}, NULL_FILE, {"id_prefixes": {"fix": "BD"}},
])
def test_bad_settings_are_a_plain_error(given):
    if given is NULL_FILE:
        fleet_home().mkdir(parents=True, exist_ok=True)
        (fleet_home() / "settings.json").write_text("null")
    else:
        write_settings(**given)
    with pytest.raises(st.SettingsError):
        st.load_settings()


def test_id_prefixes_come_from_settings():
    write_settings(id_prefixes={"build": "XB"}, emergency_prefix="XX")
    assert run("new", *new_args()).stdout.startswith("XB-")
    assert run("new", *new_args(branch="task/two", priority="urgent")).stdout.startswith("XX-")


def test_bad_settings_stop_ticket_new_with_a_message():
    write_settings(tracker="jira")
    r = run("new", *new_args())
    assert r.returncode == 1 and "tracker" in r.stderr


def test_kaneo_workspace_may_live_in_settings_but_not_the_key_or_url():
    fake = FakeKaneo(API_KEY).start()
    try:
        home = fleet_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "kaneo.json").write_text(json.dumps({"url": fake.url, "api_key": API_KEY}))
        (home / "kaneo.json").chmod(0o600)
        write_settings(kaneo={"workspace_id": "ws1", "project_icon": "Box"})
        assert run("new", *new_args()).returncode == 0
        assert len(fake.tasks) == 1
        made = [w for w in fake.writes if w["path"].endswith("/project")]
        assert made and made[0]["body"]["icon"] == "Box"
    finally:
        fake.stop()


# --- the tracker ---------------------------------------------------------------

def test_tracker_round_trip():
    t = tr.get_tracker(lambda m: None)
    assert isinstance(t, tr.Tracker) and t.name == "local"
    made = t.create(fields())
    assert t.show(made["id"])["title"] == "Do a thing"
    assert [x["id"] for x in t.list_tickets()] == [made["id"]]
    t.add_note(made["id"], "hello")
    assert t.show(made["id"])["notes"][0]["text"] == "hello"
    assert t.set_risk(made["id"], "high")["risk"] == "high"
    claimed = t.claim(made["id"], "claude", base_commit="a" * 40)
    assert claimed["status"] == "claimed" and claimed["assignee"] == "claude"
    t.set_status(made["id"], "open", allowed_from=("claimed",), assignee=None)
    assert t.close(made["id"], "cancelled", "nope")["status"] == "cancelled"


def test_list_eligible_matches_the_dispatcher(env):
    projects = env / "projects"
    write_settings(projects_dir=str(projects))
    t = tr.get_tracker(lambda m: None)
    t.create(fields())
    cfg = st.load_settings()
    # no repo folder: not eligible
    assert t.list_eligible(cfg) == []


def test_backend_rules_still_apply():
    t = tr.get_tracker(lambda m: None)
    made = t.create(fields())
    with pytest.raises(tk.TicketError):
        t.claim(made["id"], "claude", allowed_from=("proposed",))


def test_mirror_trouble_is_a_warning_not_a_failure(env):
    write_config("http://127.0.0.1:9")  # nothing listens
    warnings = []
    made = tr.get_tracker(warnings.append).create(fields())
    assert tk.load_ticket(made["id"])["status"] == "open"
    assert warnings


def test_claimed_urgent_ticket_leaves_the_urgent_column():
    fake = FakeKaneo(API_KEY).start()
    try:
        write_config(fake.url)
        t = tr.get_tracker(lambda m: None)
        made = t.create(fields(priority="urgent"))
        assert only_task(fake)["status"] == slug_of(fake, "Urgent")
        t.claim(made["id"], "claude")
        assert only_task(fake)["status"] == slug_of(fake, "In Progress")
        edit_ticket(made["id"], status="review")
        t.add_note(made["id"], "x")
        task = only_task(fake)
        assert task["status"] == slug_of(fake, "In Review") and task["priority"] == "urgent"
    finally:
        fake.stop()


# --- fleet doctor --------------------------------------------------------------

def test_doctor_passes_on_a_sane_setup(env):
    (env / "projects").mkdir()
    write_settings(projects_dir=str(env / "projects"), research_dir=str(env / "research"))
    r = fleet("doctor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and "all good" in r.stdout


def test_doctor_reports_bad_settings_and_missing_tools(env, monkeypatch):
    write_settings(projects_dir=str(env / "missing"))
    monkeypatch.setenv("FLEET_GITLEAKS", "no-such-gitleaks")
    r = fleet("doctor")
    assert r.returncode == 1
    assert "projects_dir" in r.stdout and "no-such-gitleaks" in r.stdout


def test_doctor_checks_kaneo_without_printing_the_key():
    fake = FakeKaneo(API_KEY).start()
    try:
        write_config(fake.url)
        r = fleet("doctor")
        assert "kaneo reachable" in r.stdout and API_KEY not in r.stdout + r.stderr
    finally:
        fake.stop()
    r = fleet("doctor")  # the fake is gone now
    assert r.returncode == 1 and "kaneo reachable" in r.stdout and API_KEY not in r.stdout + r.stderr


def test_doctor_rejects_loose_kaneo_file_without_the_key():
    write_config("http://127.0.0.1:9", mode=0o644)
    r = fleet("doctor")
    assert r.returncode == 1 and "chmod 600" in r.stdout and API_KEY not in r.stdout


def test_every_setting_backend_has_an_implementation():
    assert set(st.TRACKERS) == set(tr.BACKENDS)


def test_doctor_survives_a_missing_git(env, monkeypatch):
    empty = env / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    r = subprocess.run([sys.executable, str(FLEET), "doctor"], capture_output=True, text=True,
                       env=dict(os.environ))
    assert r.returncode == 1 and "Traceback" not in r.stderr and "tool git" in r.stdout
