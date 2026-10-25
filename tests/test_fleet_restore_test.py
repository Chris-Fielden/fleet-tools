import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fleet import ticket as tk

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fleet-restore-test.sh"

# A stand-in for restic: the repository is a folder holding snap-N folders.
# "corrupt" in the repository makes check fail; "nested" makes restore recreate
# the staging path under the target, as real restic does.
FAKE_RESTIC = """#!/bin/sh
echo "$*" >> "$RESTIC_REPOSITORY/calls.log"
case "$1" in
  check) if [ -e "$RESTIC_REPOSITORY/corrupt" ]; then echo "Fatal: pack abc corrupt $RESTIC_REPOSITORY"; exit 1; fi ;;
  restore)
    shift; target=""
    while [ $# -gt 0 ]; do [ "$1" = "--target" ] && target="$2"; shift; done
    snap=$(ls -d "$RESTIC_REPOSITORY"/snap-* 2>/dev/null | sort -V | tail -1)
    [ -n "$snap" ] || exit 1
    [ -e "$RESTIC_REPOSITORY/restore-fails" ] && exit 1
    [ -e "$RESTIC_REPOSITORY/nested" ] && target="$target/tmp/stage.X" && mkdir -p "$target"
    cp -pR "$snap"/. "$target"/ ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture
def setup(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "tickets").mkdir()
    (live / "marker").write_text("untouched")
    src = tmp_path / "src"
    os.environ["FLEET_HOME"] = str(src)
    ids = []
    for title in ("One", "Two", "Three"):
        ids.append(tk.create_ticket({"type": "build", "repo": "fleet-tools",
                                     "branch": f"task/{title.lower()}", "title": title,
                                     "body": "Details."})["id"])
    repo = tmp_path / "repo"
    snap = repo / "snap-0"
    snap.mkdir(parents=True)
    shutil.copytree(src / "tickets", snap / "tickets")
    for name in ("settings.json", "notify.json"):
        p = snap / name
        p.write_text("{}")
        p.chmod(0o600)
    sha = hashlib.sha256("".join(f"{i}\n" for i in sorted(ids)).encode()).hexdigest()
    (snap / "manifest.json").write_text(json.dumps({"tickets": len(ids), "ids_sha256": sha}))
    restic = tmp_path / "restic"
    restic.write_text(FAKE_RESTIC)
    restic.chmod(restic.stat().st_mode | stat.S_IEXEC)
    pw = tmp_path / "pw"
    pw.write_text("hunter2-secret\n")
    pw.chmod(0o600)
    ntfy = tmp_path / "ntfy.log"
    curl = tmp_path / "curl"
    curl.write_text(f'#!/bin/sh\necho "$*" >> {ntfy}\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").symlink_to(curl)
    e = dict(os.environ, FLEET_HOME=str(live), FLEET_RESTIC=str(restic),
             FLEET_BACKUP_REPO=str(repo), FLEET_BACKUP_PASSWORD_FILE=str(pw),
             FLEET_RESTORE_LOG=str(tmp_path / "state" / "restore.log"),
             FLEET_RESTORE_NTFY_URL="http://ntfy.invalid/fleet",
             PATH=f"{bindir}:{os.environ['PATH']}", TMPDIR=str(tmp_path))
    for v in ("RESTIC_PASSWORD", "RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE"):
        e.pop(v, None)
    return e, repo, tmp_path, ntfy


def run(e, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e)


def last_log(tmp):
    return (tmp / "state" / "restore.log").read_text().splitlines()[-1]


def test_pass_logs_and_stays_quiet(setup):
    e, repo, tmp, ntfy = setup
    r = run(e)
    assert r.returncode == 0, r.stderr
    assert last_log(tmp).split()[1:] == ["PASS", "restored", "3", "tickets"]
    assert not ntfy.exists()
    calls = (repo / "calls.log").read_text()
    assert "check --read-data-subset=" in calls and "restore latest" in calls
    assert "--tag fleet-backup --host fleet" in calls
    assert (tmp / "live" / "marker").read_text() == "untouched"
    assert sorted(p.name for p in (tmp / "live").iterdir()) == ["marker", "tickets"]
    assert not any(p.name.startswith("tmp.") for p in tmp.iterdir())  # temp folder removed


def test_restore_nested_under_staging_path(setup):
    e, repo, tmp, _ = setup
    (repo / "nested").write_text("")
    assert run(e).returncode == 0


def test_temp_setup_failure_is_recorded_and_notified(setup):
    e, repo, tmp, ntfy = setup
    e["TMPDIR"] = str(tmp / "missing-tmp")
    r = run(e)
    assert r.returncode == 1
    assert last_log(tmp).split()[1] == "FAIL"
    assert "temporary folder" in last_log(tmp)
    assert "FAILED" in ntfy.read_text()


def test_corrupted_snapshot_fails_and_notifies(setup):
    e, repo, tmp, ntfy = setup
    (repo / "corrupt").write_text("")
    r = run(e)
    assert r.returncode == 1
    assert last_log(tmp).split()[1:3] == ["FAIL", "restic"]
    assert "Restore test FAILED" in ntfy.read_text()
    assert "pack abc" not in r.stdout + r.stderr + (tmp / "state" / "restore.log").read_text()


def test_corrupted_ticket_file_fails(setup):
    e, repo, tmp, ntfy = setup
    victim = sorted((repo / "snap-0" / "tickets").glob("*.json"))[0]
    victim.write_text("{not json")
    r = run(e)
    assert r.returncode == 1
    assert "FAIL" in last_log(tmp)
    assert ntfy.exists()


def test_missing_ticket_fails_manifest(setup):
    e, repo, tmp, _ = setup
    sorted((repo / "snap-0" / "tickets").glob("*.json"))[0].unlink()
    r = run(e)
    assert r.returncode == 1 and "manifest" in last_log(tmp)


def test_loose_config_mode_fails(setup):
    e, repo, tmp, _ = setup
    (repo / "snap-0" / "notify.json").chmod(0o644)
    r = run(e)
    assert r.returncode == 1 and "notify.json is not mode 600" in last_log(tmp)


def test_restore_failure_fails(setup):
    e, repo, tmp, _ = setup
    (repo / "restore-fails").write_text("")
    assert run(e).returncode == 1
    assert "restore failed" in last_log(tmp)


def test_password_never_printed(setup):
    e, repo, tmp, _ = setup
    (repo / "corrupt").write_text("")
    r = run(e)
    assert "hunter2" not in r.stdout + r.stderr + (tmp / "state" / "restore.log").read_text()


def test_no_ntfy_url_means_no_message(setup):
    e, repo, tmp, ntfy = setup
    e.pop("FLEET_RESTORE_NTFY_URL")
    (repo / "corrupt").write_text("")
    assert run(e).returncode == 1
    assert not ntfy.exists()


def test_needs_repo_and_password(setup):
    e, _, tmp, _ = setup
    e2 = {k: v for k, v in e.items() if k != "FLEET_BACKUP_REPO"}
    assert run(e2).returncode == 1
    e3 = {k: v for k, v in e.items() if k != "FLEET_BACKUP_PASSWORD_FILE"}
    r = run(e3)
    assert r.returncode == 1 and "FAIL" in last_log(tmp)


def stamp(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_log(tmp, *lines):
    (tmp / "state").mkdir(exist_ok=True)
    (tmp / "state" / "restore.log").write_text("".join(f"{t} {v} x\n" for t, v in lines))


def test_check_log_recent_pass_is_quiet(setup):
    e, _, tmp, ntfy = setup
    write_log(tmp, (stamp(7), "PASS"))
    assert run(e, "--check-log").returncode == 0
    assert not ntfy.exists()


def test_check_log_old_pass_notifies(setup):
    e, _, tmp, ntfy = setup
    write_log(tmp, (stamp(9), "PASS"), (stamp(1), "FAIL"))
    assert run(e, "--check-log").returncode == 1
    assert "No restore-test PASS" in ntfy.read_text()


def test_check_log_no_log_notifies(setup):
    e, _, _, ntfy = setup
    assert run(e, "--check-log").returncode == 1
    assert ntfy.exists()


def test_bad_argument(setup):
    e, *_ = setup
    assert run(e, "--nope").returncode == 2
