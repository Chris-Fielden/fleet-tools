import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fleet-backup.sh"

# A stand-in for restic: the repository is a folder, a snapshot is a copy of
# the folder it ran in, and it records how it was called.
FAKE_RESTIC = """#!/bin/sh
echo "$*" >> "$RESTIC_REPOSITORY/calls.log"
echo "pwfile=${RESTIC_PASSWORD_FILE:-} pw=${RESTIC_PASSWORD:+set}" >> "$RESTIC_REPOSITORY/calls.log"
case "$1" in
  cat) test -f "$RESTIC_REPOSITORY/config" ;;
  init) test -n "$FAKE_FAIL_INIT" && exit 1
        touch "$RESTIC_REPOSITORY/config" ;;
  backup) test -n "$FAKE_FAIL_BACKUP" && exit 1
          n=$(ls -d "$RESTIC_REPOSITORY"/snap-* 2>/dev/null | wc -l)
          mkdir "$RESTIC_REPOSITORY/snap-$n" && cp -R . "$RESTIC_REPOSITORY/snap-$n/" ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture
def setup(env):
    fleet = env / "fleet"
    (fleet / "tickets").mkdir(parents=True)
    for tid in ("BD-2", "BD-10", "BD-1"):
        (fleet / "tickets" / f"{tid}.json").write_text(json.dumps({"id": tid}))
    (fleet / "tickets" / ".tmp-abc.json").write_text("{}")
    for name in ("settings.json", "kaneo.json", "notify.json", "kaneo-map.json"):
        (fleet / name).write_text("{}")
    (fleet / "logs").mkdir()
    (fleet / "logs" / "x.log").write_text("log")
    (fleet / "lock").write_text("")
    unit = env / "fleet.service"
    unit.write_text("[Service]\n")
    restic = env / "restic"
    restic.write_text(FAKE_RESTIC)
    restic.chmod(restic.stat().st_mode | stat.S_IEXEC)
    pw = env / "pw"
    pw.write_text("pw\n")
    pw.chmod(0o600)
    repo = env / "repo"
    e = dict(os.environ, FLEET_HOME=str(fleet), FLEET_RESTIC=str(restic),
             FLEET_BACKUP_UNIT=str(unit), FLEET_BACKUP_REPO=str(repo),
             FLEET_BACKUP_PASSWORD_FILE=str(pw))
    e.pop("RESTIC_PASSWORD", None)
    return e, repo, fleet


def run(e):
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=e)


def test_snapshot_contents_and_manifest(setup):
    e, repo, _ = setup
    r = run(e)
    assert r.returncode == 0, r.stderr
    snap = repo / "snap-0"
    names = sorted(p.name for p in snap.iterdir())
    assert names == ["fleet.service", "kaneo-map.json", "kaneo.json", "manifest.json",
                     "notify.json", "settings.json", "tickets"]
    assert sorted(p.name for p in (snap / "tickets").iterdir()) == ["BD-1.json", "BD-10.json", "BD-2.json"]
    manifest = json.loads((snap / "manifest.json").read_text())
    want = hashlib.sha256(b"BD-1\nBD-10\nBD-2\n").hexdigest()
    assert manifest == {"tickets": 3, "ids_sha256": want}
    assert json.loads(r.stdout.splitlines()[-1]) == manifest


def test_second_run_makes_second_snapshot(setup):
    e, repo, _ = setup
    assert run(e).returncode == 0
    assert run(e).returncode == 0
    assert (repo / "snap-1").is_dir()
    assert (repo / "calls.log").read_text().count("init") == 1


def test_refuses_loose_password_file(setup, env):
    e, repo, _ = setup
    Path(e["FLEET_BACKUP_PASSWORD_FILE"]).chmod(0o644)
    r = run(e)
    assert r.returncode != 0 and "0600" in r.stderr
    assert not repo.exists() or not (repo / "snap-0").exists()


def test_refuses_password_file_inside_fleet_home(setup):
    e, _, fleet = setup
    pw = fleet / "pw"
    pw.write_text("pw")
    pw.chmod(0o600)
    e["FLEET_BACKUP_PASSWORD_FILE"] = str(pw)
    r = run(e)
    assert r.returncode != 0 and "outside the fleet home" in r.stderr


def test_refuses_repo_inside_fleet_home(setup):
    e, _, fleet = setup
    e["FLEET_BACKUP_REPO"] = str(fleet / "repo")
    r = run(e)
    assert r.returncode != 0 and "outside the fleet home" in r.stderr


def test_needs_repo_and_password(setup):
    e, _, _ = setup
    e2 = {k: v for k, v in e.items() if k != "FLEET_BACKUP_REPO"}
    e2.pop("RESTIC_REPOSITORY", None)
    assert run(e2).returncode != 0
    e3 = {k: v for k, v in e.items() if k != "FLEET_BACKUP_PASSWORD_FILE"}
    e3.pop("RESTIC_PASSWORD_FILE", None)
    r = run(e3)
    assert r.returncode != 0 and "PASSWORD" in r.stderr


def test_password_from_env(setup):
    e, repo, _ = setup
    e.pop("FLEET_BACKUP_PASSWORD_FILE")
    e["RESTIC_PASSWORD"] = "secret"
    assert run(e).returncode == 0
    assert "pw=set" in (repo / "calls.log").read_text()


def test_missing_restic(setup):
    e, _, _ = setup
    e["FLEET_RESTIC"] = "/no/such/restic"
    r = run(e)
    assert r.returncode != 0 and "restic is not installed" in r.stderr


def test_relative_paths(setup, env):
    e, repo, _ = setup
    e["FLEET_BACKUP_REPO"] = "repo"
    e["FLEET_BACKUP_PASSWORD_FILE"] = "./pw"
    e["FLEET_RESTIC"] = "./restic"
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=e, cwd=env)
    assert r.returncode == 0, r.stderr
    assert (repo / "snap-0").is_dir()


@pytest.mark.parametrize("var", ["FAKE_FAIL_INIT", "FAKE_FAIL_BACKUP"])
def test_restic_failure_is_an_error_and_cleans_up(setup, env, var):
    e, repo, _ = setup
    tmp = env / "tmp"
    tmp.mkdir()
    e["TMPDIR"] = str(tmp)
    e[var] = "1"
    if var == "FAKE_FAIL_BACKUP":
        repo.mkdir()
        (repo / "config").write_text("")
    r = run(e)
    assert r.returncode != 0
    assert "snapshot saved" not in r.stdout
    assert list(tmp.iterdir()) == []


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")
def test_with_real_restic(setup):
    e, repo, _ = setup
    e["FLEET_RESTIC"] = shutil.which("restic")
    r = run(e)
    assert r.returncode == 0, r.stderr
    out = subprocess.run(["restic", "ls", "latest"], capture_output=True, text=True,
                         env=dict(e, RESTIC_REPOSITORY=str(repo),
                                  RESTIC_PASSWORD_FILE=e["FLEET_BACKUP_PASSWORD_FILE"]))
    assert "/manifest.json" in out.stdout and "logs" not in out.stdout
