"""EM-32: task branches never write CHANGELOG.md, so stacked ships don't conflict.

Real git repos in tmp_path, like test_release_tag.py. Hooks are run as
programs with the tool call as JSON on stdin.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "scripts" / "release-tag.sh"
HOOKS = ROOT / ".claude" / "hooks"
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent", "RELEASE_DATE": "2026.10.15"}


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def release(repo, ref, title=""):
    r = subprocess.run(["bash", str(RELEASE), ref, title], cwd=repo, capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, r.stderr
    return r


def make_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check.sh", repo / "scripts" / "check.sh")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def check(repo):
    # Only the changelog section matters; other checks may fail in a bare repo.
    r = subprocess.run(["bash", "scripts/check.sh"], cwd=repo, capture_output=True, text=True)
    return r.stdout


def guard_failed(out):
    return "FAIL  changelog guard" in out


def branch(repo, name, base="main"):
    git(repo, "checkout", "-q", "-b", name, base)


def commit_file(repo, name, text, msg="c"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)


def test_stacked_branches_merge_without_conflict(tmp_path):  # T1
    repo = make_repo(tmp_path)
    release(repo, "ID-0", "base")
    branch(repo, "task/a")
    commit_file(repo, "a1.txt", "1\n")
    git(repo, "checkout", "-q", "main")
    branch(repo, "task/b")
    commit_file(repo, "b1.txt", "1\n")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge a", "task/a")
    release(repo, "AA-1", "first")
    git(repo, "checkout", "-q", "task/b")
    git(repo, "merge", "-q", "-m", "prep", "main")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge b", "task/b")
    release(repo, "BB-1", "second")
    text = (repo / "CHANGELOG.md").read_text()
    assert "<<<<" not in text
    assert text.count("- AA-1: first") == 1 and text.count("- BB-1: second") == 1
    assert text.index("BB-1") < text.index("AA-1") < text.index("ID-0")


def test_branch_editing_changelog_is_flagged_main_release_is_not(tmp_path):  # T2, T4, T5
    repo = make_repo(tmp_path)
    branch(repo, "task/x")
    assert not guard_failed(check(repo))
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n- hand written\n")
    out = check(repo)
    assert guard_failed(out) and "written at ship" in out      # uncommitted, even untracked
    git(repo, "add", "CHANGELOG.md")
    git(repo, "commit", "-q", "-m", "oops")
    assert guard_failed(check(repo))
    # main's first-ever release creates the file: not flagged on main.
    git(repo, "checkout", "-q", "main")
    release(repo, "ID-1", "x")
    assert "changelog guard (not a task/* branch)" in check(repo)
    # The task branch merging main in is flagged until it takes main's copy.
    git(repo, "checkout", "-q", "task/x")
    assert guard_failed(check(repo))
    git(repo, "checkout", "-q", "main", "--", "CHANGELOG.md")
    commit_file(repo, "other.txt", "keep\n")
    out = check(repo)
    assert not guard_failed(out) and "changelog guard (CHANGELOG.md untouched)" in out
    assert (repo / "other.txt").read_text() == "keep\n"


def test_branch_that_only_merged_main_is_not_flagged(tmp_path):  # T4
    repo = make_repo(tmp_path)
    release(repo, "ID-1", "x")
    branch(repo, "task/y")
    commit_file(repo, "y.txt", "y\n")
    git(repo, "checkout", "-q", "main")
    release(repo, "ID-2", "z")
    git(repo, "checkout", "-q", "task/y")
    git(repo, "merge", "-q", "-m", "prep", "main")
    assert "untouched" in check(repo)


def test_criss_cross_with_two_merge_bases(tmp_path):  # T4
    repo = make_repo(tmp_path)
    branch(repo, "task/p")
    commit_file(repo, "p.txt", "p\n")                 # p1
    git(repo, "checkout", "-q", "main")
    release(repo, "ID-1", "x")                        # q1 (main), writes CHANGELOG.md
    git(repo, "checkout", "-q", "task/p")
    git(repo, "merge", "-q", "--no-ff", "-m", "p<-main", "main")   # P
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "main<-p1", "task/p~1")  # M
    assert len(git(repo, "merge-base", "--all", "task/p", "main").stdout.split()) == 2
    git(repo, "checkout", "-q", "task/p")
    out = check(repo)
    assert "untouched" in out and not guard_failed(out)
    (repo / "CHANGELOG.md").write_text("changed\n")
    assert guard_failed(check(repo))


def test_same_day_releases_count_up_and_unreleased_stays_empty(tmp_path):  # T6
    repo = make_repo(tmp_path)
    release(repo, "A-1", "one")
    release(repo, "A-2", "two")
    text = (repo / "CHANGELOG.md").read_text()
    assert "[ship-2026.10.15.1]" in text and "[ship-2026.10.15.2]" in text
    unreleased = text.split("## [Unreleased]")[1].split("## [ship-")[0]
    assert unreleased.strip() == ""
    assert git(repo, "tag", "-l", "ship-*").stdout.split() == ["ship-2026.10.15.1", "ship-2026.10.15.2"]


# --- hooks ------------------------------------------------------------------

def hook(name, tool_input, cwd, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_RULES_OFF"}
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, str(HOOKS / name)],
                       input=json.dumps({"tool_input": tool_input, "cwd": str(cwd)}),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if r.stdout.strip() else None


def worktree(tmp_path):
    repo = make_repo(tmp_path)
    wt = repo / ".worktrees" / "t"
    git(repo, "worktree", "add", "-q", "-b", "task/t", str(wt), "main")
    (wt / "docs").mkdir()
    return wt


def test_edit_hook_blocks_root_changelog_only(tmp_path):  # T3, T7
    wt = worktree(tmp_path)
    for tool in ({"file_path": str(wt / "CHANGELOG.md"), "new_string": "x"},
                 {"file_path": "CHANGELOG.md", "content": "x"}):
        reason = hook("guard_edits.py", tool, wt)
        assert reason and "written only at ship" in reason
    assert hook("guard_edits.py", {"file_path": str(wt / "docs" / "CHANGELOG.md"), "content": "x"}, wt) is None
    assert hook("guard_edits.py", {"file_path": str(wt / "tests" / "CHANGELOG.md.fixture"), "content": "x"}, wt) is None


def test_bash_hook_blocks_shell_writes_to_root_changelog(tmp_path):  # T3, T7
    wt = worktree(tmp_path)
    for cmd in ['echo x >> CHANGELOG.md', "sed -i 's/a/b/' CHANGELOG.md", 'printf x | tee -a CHANGELOG.md',
                'cp /tmp/n ./CHANGELOG.md', 'echo x > CHANGELOG.md', 'echo x > "CHANGELOG.md"',
                "echo x >> 'CHANGELOG.md'", 'echo x >"$PWD/CHANGELOG.md"', "mv /tmp/n CHANGELOG.md",
                'bash -c "echo x >> CHANGELOG.md"', 'cd docs && cd .. && echo x >> CHANGELOG.md',
                'env -C . tee -a CHANGELOG.md',
                'env -C docs printf x > CHANGELOG.md', '(cd docs); printf x > CHANGELOG.md',
                '(cd docs && true) && echo x >> CHANGELOG.md', 'cd $SOMEWHERE && echo x >> CHANGELOG.md', "perl -pi -e 's/a/b/' CHANGELOG.md"]:
        reason = hook("guard_bash.py", {"command": cmd}, wt)
        assert reason and "written only at ship" in reason, cmd
    for cmd in ['cat CHANGELOG.md', 'echo x >> docs/CHANGELOG.md', 'git checkout main -- CHANGELOG.md',
                'grep ship CHANGELOG.md', 'cp CHANGELOG.md /tmp/changelog-backup',
                'diff CHANGELOG.md docs/CHANGELOG.md', 'cat < CHANGELOG.md', 'wc -l < CHANGELOG.md',
                'cd docs && echo x >> CHANGELOG.md', 'env -C docs sh -c "echo x >> CHANGELOG.md"',
                'cd docs; sed -i s/a/b/ CHANGELOG.md',
                '(cd docs && echo x >> CHANGELOG.md)', 'true && (cd docs; echo x >> CHANGELOG.md)']:
        assert hook("guard_bash.py", {"command": cmd}, wt) is None, cmd


def test_emergency_override_still_runs_check(tmp_path):  # T8
    wt = worktree(tmp_path)
    (wt / "CHANGELOG.md").write_text("sneaky\n")
    # AGENT_RULES_OFF silences the hooks ...
    assert hook("guard_edits.py", {"file_path": str(wt / "CHANGELOG.md"), "content": "x"}, wt,
                {"AGENT_RULES_OFF": "1"}) is None
    # ... but check.sh does not look at it, so the edit is still caught.
    r = subprocess.run(["bash", "scripts/check.sh"], cwd=wt, capture_output=True, text=True,
                       env={**os.environ, "AGENT_RULES_OFF": "1"})
    assert "FAIL  changelog guard" in r.stdout


def test_no_union_merge_driver():  # T9
    ga = ROOT / ".gitattributes"
    text = ga.read_text() if ga.exists() else ""
    assert not any("CHANGELOG.md" in l and "merge=union" in l for l in text.splitlines())
