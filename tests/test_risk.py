"""Risk levels: rules, ticket field, `ticket risk`, Kaneo label, review.sh and
the merge script's gate. Everything uses a temporary FLEET_HOME and throwaway
git repos."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT, new_args, run
from fake_kaneo import FakeKaneo
from fleet import ticket as tk
from fleet import risk as rk

SCRIPTS = ROOT / "scripts"
TICKET = str(ROOT / "bin" / "ticket")
ROOT_RULES = ROOT / "risk-rules.json"


@pytest.fixture
def rules():
    return rk.load_rules(ROOT_RULES.read_text())


# --- rules file --------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "k8s/app/deploy.yaml", ".claude/settings.json", ".codex/config.toml", "CLAUDE.md", "AGENTS.md",
    "scripts/check.sh", "scripts/finish-task.sh", "scripts/review.sh", "scripts/new-task.sh",
    "scripts/release-tag.sh",
    "fleet/secrets.py", "docs/secret-notes.md", "fleet/oauth.py", "src/Auth/login.py",
    ".env", "app/.env.example", "requirements.txt", "requirements-dev.txt", "svc/requirements.txt",
    "pyproject.toml", "web/package.json", "go.mod", "compose.yml",
    "fleet/dispatch.py", "fleet/dispatch_cli.py", "risk-rules.json", "fleet/ticket.py", "fleet/ticket_cli.py", "bin/ticket",
    "fleet/ship_cli.py", "bin/ship", "fleet/supervisor.py", "fleet/supervisor_run.py",
])
def test_high_paths(rules, path):
    assert rk.classify(path, rules)[0] == "high"


@pytest.mark.parametrize("path", ["docs/ticket-spec.md", "docs/a/b/c.md", "tests/test_x.py"])
def test_low_paths(rules, path):
    assert rk.classify(path, rules)[0] == "low"


@pytest.mark.parametrize("path", [
    "fleet/usage_cli.py", "README.md", "bin/idea", "scripts/ask-codex.sh", "fleet/usage.py",
    "scripts/check.sh.bak", "k8s", "fleet/sub/dispatch.py", "compose.yaml"])
def test_medium_paths(rules, path):
    assert rk.classify(path, rules)[0] == "medium", path


def test_highest_matching_rule_wins(rules):
    # under tests/ (low) but with 'auth' in the name (high)
    assert rk.classify("tests/test_auth.py", rules) == ("high", "contains 'auth'")
    assert rk.classify("docs/x.md", rules) == ("low", "docs/**")


def test_overall_level_is_highest_and_empty_is_low(rules):
    rows, level = rk.classify_all(["docs/a.md", "fleet/usage_cli.py"], rules)
    assert level == "medium" and len(rows) == 2
    assert rk.classify_all([], rules)[1] == "low"
    assert rk.classify_all(["docs/a.md", "k8s/x.yaml"], rules)[1] == "high"


def test_bad_rules_files_are_refused():
    for bad in ['[]', '{"default": "huge"}', '{"default": "low", "rules": [{"level": "low"}]}',
                '{"default": "low", "rules": [{"level": "x", "glob": "a"}]}',
                '{"default": "low", "rules": [{"level": "low", "glob": "a", "contains": "b"}]}',
                '{"default": "low", "rules": 5}', '{"default": "low", "ticket_start": 5}',
                '{"default": "low", "rules": false}', '{"default": "low", "rules": {}}',
                '{"default": "low", "ticket_start": false}', '{"default": "low", "ticket_start": {"by_type": []}}',
                'nope']:
        with pytest.raises(rk.RiskError):
            rk.load_rules(bad)


def test_the_real_rules_file_loads():
    assert rk.load_rules(ROOT_RULES.read_text())["default"] == "medium"


# --- ticket field ------------------------------------------------------------

@pytest.mark.parametrize("over,level", [
    ({"type": "investigate"}, "low"),
    ({"type": "question"}, "low"),
    ({"type": "build"}, "medium"),
    ({"type": "fix", "parent": "BD-1"}, "medium"),
    ({"type": "build", "repo": "sandbox"}, "low"),
    ({"type": "build", "repo": "fleet-tools"}, "medium"),
])
def test_starting_level(over, level):
    run("new", *new_args())  # BD-1, a parent for the fix case
    r = run("new", *new_args(**{"branch": "task/other", "title": "A different job", **over}))
    assert r.returncode == 0, r.stderr
    assert tk.load_ticket(r.stdout.split()[0])["risk"] == level


def test_nobody_can_choose_the_starting_level():
    assert run("new", *new_args(), "--risk", "low").returncode == 2
    with pytest.raises(tk.TicketInvalid):
        tk.create_ticket({"type": "build", "repo": "r", "branch": "task/x", "title": "t",
                          "body": "b", "risk": "low"})


def test_old_tickets_without_risk_still_load_as_default():
    tid = run("new", *new_args()).stdout.split()[0]
    path = tk.tickets_dir() / f"{tid}.json"
    data = json.loads(path.read_text())
    del data["risk"]
    path.write_text(json.dumps(data))
    assert rk.stored_level(tk.load_ticket(tid)) == "medium"


def test_bad_stored_risk_is_rejected():
    tid = run("new", *new_args()).stdout.split()[0]
    path = tk.tickets_dir() / f"{tid}.json"
    data = json.loads(path.read_text())
    data["risk"] = "extreme"
    path.write_text(json.dumps(data))
    assert "risk must be one of" in run("show", tid).stderr


def test_show_prints_risk():
    tid = run("new", *new_args()).stdout.split()[0]
    assert "risk:" in run("show", tid).stdout


# --- raising -----------------------------------------------------------------

def test_human_can_raise_but_never_lower():
    tid = run("new", *new_args(type="investigate")).stdout.split()[0]  # low
    r = run("risk", tid, "--raise", "high")
    assert r.returncode == 0, r.stderr
    assert tk.load_ticket(tid)["risk"] == "high"
    for level in ("high", "medium", "low"):
        r = run("risk", tid, "--raise", level)
        assert r.returncode == 1 and "can only be raised" in r.stderr
    assert tk.load_ticket(tid)["risk"] == "high"


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_agents_can_raise_but_never_lower(var):
    tid = run("new", *new_args()).stdout.split()[0]
    r = run("risk", tid, "--raise", "high", extra_env={var: "1"})
    assert r.returncode == 0
    assert tk.load_ticket(tid)["risk"] == "high"
    r = run("risk", tid, "--raise", "low", extra_env={var: "1"})
    assert r.returncode == 1 and "can only be raised" in r.stderr
    assert tk.load_ticket(tid)["risk"] == "high"


def test_raise_needs_a_known_level_and_id():
    tid = run("new", *new_args()).stdout.split()[0]
    assert run("risk", tid, "--raise", "huge").returncode == 2
    assert run("risk", "--raise", "high").returncode == 2
    assert run("risk").returncode == 2


# --- throwaway git repos -----------------------------------------------------

def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=check).stdout.strip()


def write(repo, rel, text="x\n"):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def repo(env):
    r = env / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Tester")
    write(r, "README.md")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


def branch_with(repo, name, files, rules_text=None):
    """Commit `files` on a new branch off main, then return to main."""
    git(repo, "switch", "-qc", name)
    for rel in files:
        write(repo, rel, f"{rel}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "work")
    git(repo, "switch", "-q", "main")


def test_diff_prints_level_and_rule_per_file(repo):
    branch_with(repo, "task/a", ["docs/a.md", "fleet/usage_cli.py", "k8s/app.yaml"])
    r = run("risk", "--diff", "main...task/a", "--dir", str(repo))
    assert r.returncode == 0, r.stderr
    assert "low    docs/a.md   (docs/**)" in r.stdout
    assert "medium fleet/usage_cli.py   (no rule (default))" in r.stdout
    assert "high   k8s/app.yaml   (k8s/**)" in r.stdout
    assert r.stdout.strip().endswith("risk: high")


def test_diff_docs_only_is_low(repo):
    branch_with(repo, "task/a", ["docs/a.md", "tests/test_a.py"])
    assert run("risk", "--diff", "main...task/a", "--dir", str(repo)).stdout.strip().endswith("risk: low")


def test_diff_judges_deletes_and_renames_by_both_paths(repo):
    write(repo, "k8s/old.yaml")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "k")
    git(repo, "switch", "-qc", "task/mv")
    (repo / "docs").mkdir()
    git(repo, "mv", "k8s/old.yaml", "docs/new.md")
    git(repo, "commit", "-qm", "mv")
    git(repo, "switch", "-q", "main")
    r = run("risk", "--diff", "main...task/mv", "--dir", str(repo))
    assert "k8s/old.yaml" in r.stdout and r.stdout.strip().endswith("risk: high")


def test_diff_bad_range_and_unknown_branch(repo):
    assert run("risk", "--diff", "main", "--dir", str(repo)).returncode == 1
    assert run("risk", "--diff", "main...nope", "--dir", str(repo)).returncode == 1


def test_stored_level_wins_over_a_lower_diff(repo):
    branch_with(repo, "task/a", ["docs/a.md"])
    tid = run("new", *new_args()).stdout.split()[0]  # medium
    r = run("risk", tid, "--diff", "main...task/a", "--dir", str(repo))
    assert "diff level: low" in r.stdout and "stored level: medium" in r.stdout
    assert r.stdout.strip().endswith("risk: medium")
    # and the diff wins when it is higher
    branch_with(repo, "task/b", ["k8s/a.yaml"])
    assert run("risk", tid, "--diff", "main...task/b", "--dir", str(repo)).stdout.strip().endswith("risk: high")


def test_diff_command_never_writes_the_ticket(repo):
    branch_with(repo, "task/a", ["k8s/a.yaml"])
    tid = run("new", *new_args()).stdout.split()[0]
    run("risk", tid, "--diff", "main...task/a", "--dir", str(repo))
    assert tk.load_ticket(tid)["risk"] == "medium"


def test_branch_cannot_weaken_its_own_rules(repo):
    """Rules come from the base branch, not from the branch being judged."""
    write(repo, "risk-rules.json", ROOT_RULES.read_text())
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "rules")
    git(repo, "switch", "-qc", "task/sneaky")
    write(repo, "risk-rules.json", json.dumps({"default": "low", "rules": []}))
    write(repo, "k8s/a.yaml")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "weaken")
    git(repo, "switch", "-q", "main")
    r = run("risk", "--diff", "main...task/sneaky", "--dir", str(repo))
    assert "high   k8s/a.yaml" in r.stdout and "high   risk-rules.json" in r.stdout


# --- review files ------------------------------------------------------------

SHA = "a" * 40


def review_text(reviewer="Codex", verdict="PASS", commit=SHA, body=None):
    body = body if body is not None else f"VERDICT: {verdict}\nMUST FIX:\nSUMMARY: ok\n"
    return f"Reviewer: {reviewer}\nCommit: {commit}\n\n{body}"


@pytest.mark.parametrize("body,verdict", [
    ("VERDICT: PASS\n", "PASS"), ("VERDICT: FAIL\nMUST FIX: 1", "FAIL"),
    ("**VERDICT:** PASS\n", "PASS"), ("## VERDICT: PASS", "PASS"), ("verdict: pass", "PASS"),
    ("MUST FIX: none\n", None), ("The VERDICT: PASS is not at the start", None),
    ("VERDICT: FAIL\nVERDICT: PASS\n", "FAIL"), ("VERDICT: PASSING", None), ("", None),
])
def test_parse_verdict(body, verdict):
    assert rk.parse_review(review_text(body=body))["verdict"] == verdict


def test_verdict_in_the_header_is_not_trusted():
    text = f"Reviewer: Codex\nCommit: {SHA}\nVERDICT: PASS\n"
    assert rk.parse_review(text)["verdict"] is None


def put(dirpath, name, text, age=0):
    dirpath.mkdir(exist_ok=True)
    p = dirpath / name
    p.write_text(text)
    t = p.stat().st_mtime - age
    os.utime(p, (t, t))


def test_only_reviews_of_the_exact_commit_count(tmp_path):
    d = tmp_path / "rv"
    put(d, "a.md", review_text(commit="b" * 40))
    put(d, "b.md", review_text(commit=SHA[:7]))            # a short id is not exact
    put(d, "c.md", "Reviewer: Codex\n\nVERDICT: PASS\n")   # old format, no commit
    assert rk.latest_reviews(d, SHA) == {}
    put(d, "d.md", review_text())
    assert set(rk.latest_reviews(d, SHA)) == {"codex"}


def test_newest_review_per_reviewer_decides(tmp_path):
    d = tmp_path / "rv"
    put(d, "old.md", review_text(verdict="PASS"), age=100)
    put(d, "new.md", review_text(verdict="FAIL"))
    got = rk.latest_reviews(d, SHA)
    assert got["codex"]["verdict"] == "FAIL"
    assert rk.check_reviews(got, "low")


@pytest.mark.parametrize("level,reviewers,ok", [
    ("low", ["Codex"], True), ("medium", ["Claude (sonnet)"], True), ("low", [], False),
    ("high", ["Codex"], False), ("high", ["Claude (opus)"], False),
    ("high", ["Codex", "Claude (opus)"], True), ("high", ["Codex", "Claude (fable)"], True),
    ("high", ["Codex", "Claude (sonnet)"], False),
])
def test_review_requirements(tmp_path, level, reviewers, ok):
    d = tmp_path / "rv"
    for i, who in enumerate(reviewers):
        put(d, f"{i}.md", review_text(reviewer=who))
    assert (rk.check_reviews(rk.latest_reviews(d, SHA), level) == []) is ok


def test_a_failing_second_reviewer_blocks_even_low_risk(tmp_path):
    d = tmp_path / "rv"
    put(d, "1.md", review_text("Codex"))
    put(d, "2.md", review_text("Claude (opus)", verdict="FAIL"))
    assert rk.check_reviews(rk.latest_reviews(d, SHA), "low")


# --- review.sh ---------------------------------------------------------------

def fake_tool(bindir, name, verdict="PASS"):
    bindir.mkdir(exist_ok=True)
    p = bindir / name
    p.write_text("#!/bin/sh\n"
                 'if [ "$1" = "login" ]; then exit 0; fi\n'
                 f'echo "args: $*" >&2\necho "VERDICT: {verdict}"\necho "SUMMARY: fake"\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def task(repo, env):
    """A throwaway repo with the real scripts, a task worktree with one commit."""
    (repo / "scripts").mkdir()
    for s in ("review.sh", "finish-task.sh", "release-tag.sh"):
        shutil.copy(SCRIPTS / s, repo / "scripts" / s)
    (repo / "scripts" / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    write(repo, ".gitignore", ".worktrees/\n.reviews/\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "scripts")
    wt = repo / ".worktrees" / "demo"
    git(repo, "worktree", "add", "-q", "-b", "task/demo", str(wt))
    return wt


def commit_in(wt, rel, text="y\n"):
    write(wt, rel, text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", f"edit {rel}")
    return git(wt, "rev-parse", "HEAD")


def run_review(wt, bindir, **extra):
    e = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    e.update(extra)
    return subprocess.run(["bash", "scripts/review.sh"], cwd=wt, capture_output=True, text=True, env=e)


def test_review_sh_writes_commit_header(task, env):
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    r = run_review(task, env / "bin", REVIEWER="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    out = task / ".reviews" / f"task-demo-{full[:7]}-codex.md"
    info = rk.parse_review(out.read_text())
    assert (info["kind"], info["commit"], info["verdict"]) == ("codex", full, "PASS")


def test_review_sh_both_runs_codex_and_stronger_claude(task, env):
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    fake_tool(env / "bin", "claude")
    r = run_review(task, env / "bin", REVIEWER="both")
    assert r.returncode == 0, r.stdout + r.stderr
    got = rk.latest_reviews(task / ".reviews", full)
    assert set(got) == {"codex", "claude"}
    assert "opus" in got["claude"]["reviewer"]
    assert rk.check_reviews(got, "high") == []


def claude_models(env):
    """A fake claude that records the --model it was given."""
    log = env / "models.txt"
    p = env / "bin" / "claude"
    p.parent.mkdir(exist_ok=True)
    p.write_text("#!/bin/sh\n"
                 'while [ $# -gt 0 ]; do\n'
                 f'  if [ "$1" = --model ]; then echo "$2" >> {log}; fi\n'
                 '  shift\ndone\n'
                 'echo "VERDICT: PASS"\necho "SUMMARY: fake"\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return log


def test_review_sh_both_codex_fail_runs_sonnet_not_opus(task, env):
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex", "FAIL")
    log = claude_models(env)
    r = run_review(task, env / "bin", REVIEWER="both")
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().split() == ["sonnet"]
    got = rk.latest_reviews(task / ".reviews", full)
    assert got["codex"]["verdict"] == "FAIL" and "sonnet" in got["claude"]["reviewer"]


def test_review_sh_both_codex_pass_runs_opus(task, env):
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex", "PASS")
    log = claude_models(env)
    assert run_review(task, env / "bin", REVIEWER="both").returncode == 0
    assert log.read_text().split() == ["opus"]
    assert rk.check_reviews(rk.latest_reviews(task / ".reviews", full), "high") == []


def test_review_sh_both_codex_unable_runs_opus(task, env):
    commit_in(task, "fleet/x.py")
    log = claude_models(env)
    shim = env / "bin" / "codex"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    assert run_review(task, env / "bin", REVIEWER="both").returncode == 1
    assert log.read_text().split() == ["opus"]


def test_review_sh_both_review_model_overrides_the_round_choice(task, env):
    commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex", "FAIL")
    log = claude_models(env)
    run_review(task, env / "bin", REVIEWER="both", REVIEW_MODEL="fable")
    assert log.read_text().split() == ["fable"]


def test_gate_rejects_codex_fail_with_sonnet_pass(task, env):
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex", "FAIL")
    claude_models(env)
    run_review(task, env / "bin", REVIEWER="both")
    problems = rk.check_reviews(rk.latest_reviews(task / ".reviews", full), "high")
    assert any("stronger Claude model" in p for p in problems)


def test_review_sh_both_fails_if_one_cannot_run(task, env):
    commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "claude")  # no codex on the (fake) path first; real one may exist
    shim = env / "bin" / "codex"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    r = run_review(task, env / "bin", REVIEWER="both")
    assert r.returncode == 1
    assert set(rk.latest_reviews(task / ".reviews", git(task, "rev-parse", "HEAD"))) == {"claude"}


def test_review_sh_both_fails_when_a_reviewer_dies_after_login(task, env):
    commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    broken = env / "bin" / "claude"
    broken.write_text("#!/bin/sh\necho partial\nexit 3\n")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    r = run_review(task, env / "bin", REVIEWER="both")
    assert r.returncode == 1 and "claude review failed" in r.stdout
    assert set(rk.latest_reviews(task / ".reviews", git(task, "rev-parse", "HEAD"))) == {"codex"}


def test_review_sh_single_failure_is_a_failure(task, env):
    commit_in(task, "fleet/x.py")
    broken = env / "bin" / "codex"
    broken.parent.mkdir(exist_ok=True)
    broken.write_text('#!/bin/sh\n[ "$1" = login ] && exit 0\nexit 3\n')
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    assert run_review(task, env / "bin", REVIEWER="codex").returncode == 1


def test_review_sh_discards_a_review_if_head_moved(task, env):
    commit_in(task, "fleet/x.py")
    slow = env / "bin" / "codex"
    slow.parent.mkdir(exist_ok=True)
    slow.write_text('#!/bin/sh\n[ "$1" = login ] && exit 0\n'
                    f'git -C {task} commit --allow-empty -qm moved\necho "VERDICT: PASS"\n')
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    r = run_review(task, env / "bin", REVIEWER="codex")
    assert r.returncode == 1 and "HEAD changed" in r.stdout
    assert not list((task / ".reviews").glob("*.md"))


def test_review_sh_pass_then_failure_never_publishes_a_review(task, env):
    commit_in(task, "fleet/x.py")
    seen = env / "seen.txt"
    odd = env / "bin" / "codex"
    odd.parent.mkdir(exist_ok=True)
    odd.write_text('#!/bin/sh\n[ "$1" = login ] && exit 0\n'
                   'echo "VERDICT: PASS"\nsleep 1\n'
                   f'ls .reviews > {seen}\nexit 3\n')
    odd.chmod(odd.stat().st_mode | stat.S_IEXEC)
    r = run_review(task, env / "bin", REVIEWER="codex")
    assert r.returncode == 1
    # while the reviewer was still running, nothing the gate could read existed
    assert not [n for n in seen.read_text().split() if n.endswith(".md")]
    assert list((task / ".reviews").iterdir()) == []


def test_review_sh_rejects_unknown_reviewer(task, env):
    commit_in(task, "fleet/x.py")
    assert run_review(task, env / "bin", REVIEWER="nobody").returncode == 1


# --- the merge script --------------------------------------------------------

def finish(repo, answers, *flags, extra_env=None):
    e = dict(os.environ, FLEET_TICKET=TICKET, FLEET_HOME=os.environ["FLEET_HOME"])
    # these tests are about the merge gate; the quiesce step (tested in test_k1_stays_put.py)
    # must not depend on whatever `ship` is installed on this machine
    e.setdefault("FLEET_SHIP", "true")
    e.update(extra_env or {})
    return subprocess.run(["bash", "scripts/finish-task.sh", "demo", *flags], cwd=repo,
                          input=answers, capture_output=True, text=True, env=e)


def merged(repo):
    return "Merge task/demo" in git(repo, "log", "main", "--format=%s")


def file_ticket(branch="task/demo", repo="repo", **over):
    r = run("new", *new_args(branch=branch, repo=repo, **over))
    assert r.returncode == 0, r.stderr
    return r.stdout.split()[0]


def pass_review(wt, kind="codex", model=None, verdict="PASS", commit=None):
    full = commit or git(wt, "rev-parse", "HEAD")
    who = {"codex": "Codex", "claude": f"Claude ({model or 'opus'})"}[kind]
    put(wt / ".reviews", f"task-demo-{full[:7]}-{kind}.md", review_text(who, verdict, full))


def test_refuses_without_any_review(task, repo):
    commit_in(task, "docs/a.md")
    r = finish(repo, "y\n")
    assert r.returncode == 1 and "no review of this exact commit" in r.stdout
    assert not merged(repo)


def test_medium_merges_with_pass_review_and_y(task, repo):
    commit_in(task, "fleet/x.py")
    pass_review(task)
    r = finish(repo, "y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Risk level: MEDIUM" in r.stdout and "fleet/x.py" in r.stdout
    assert (repo / "fleet" / "x.py").exists()


def test_low_risk_needs_only_a_pass_and_a_y(task, repo):
    commit_in(task, "docs/a.md")
    pass_review(task, "claude", "sonnet")
    r = finish(repo, "y\n")
    assert r.returncode == 0 and "Risk level: LOW" in r.stdout


@pytest.mark.parametrize("path", ["docs/a.md", "fleet/x.py"])
def test_below_high_risk_asks_nothing_even_with_a_no_on_stdin(task, repo, path):
    commit_in(task, path)
    pass_review(task)
    r = finish(repo, "n\n")  # nothing reads it: there is no y/N prompt any more
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[y/N]" not in r.stdout and "no confirmation below high risk" in r.stdout
    assert merged(repo)


@pytest.mark.parametrize("path", ["docs/a.md", "fleet/x.py"])
def test_below_high_risk_does_not_hang_on_closed_stdin(task, repo, path):
    commit_in(task, path)
    pass_review(task)
    r = finish(repo, "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert merged(repo)


def test_review_of_an_older_commit_does_not_count(task, repo):
    commit_in(task, "docs/a.md")
    pass_review(task)
    commit_in(task, "docs/b.md")  # new commit after the review
    r = finish(repo, "y\n")
    assert r.returncode == 1 and "no review of this exact commit" in r.stdout
    assert not merged(repo)


def test_a_failing_review_blocks(task, repo):
    commit_in(task, "docs/a.md")
    pass_review(task, verdict="FAIL")
    r = finish(repo, "y\n")
    assert r.returncode == 1 and "not PASS" in r.stdout and not merged(repo)


def test_a_forged_short_id_header_does_not_count(task, repo):
    full = commit_in(task, "docs/a.md")
    put(task / ".reviews", "x.md", review_text("Codex", "PASS", full[:7]))
    assert finish(repo, "y\n").returncode == 1


def test_high_risk_needs_both_reviewers(task, repo):
    commit_in(task, "k8s/app.yaml")
    pass_review(task, "codex")
    r = finish(repo, "y\n")
    assert r.returncode == 1 and "needs a PASS review from Claude" in r.stdout
    assert not merged(repo)


def test_high_risk_needs_the_ticket_id_not_y(task, repo):
    tid = file_ticket()
    commit_in(task, "k8s/app.yaml")
    pass_review(task, "codex"); pass_review(task, "claude")
    r = finish(repo, "y\n")
    assert "HIGH RISK" in r.stdout and f"({tid})" in r.stdout
    assert "Cancelled" in r.stdout and not merged(repo)
    assert "k8s/**" in r.stdout
    r = finish(repo, f"{tid}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert merged(repo) and (repo / "k8s" / "app.yaml").exists()


@pytest.mark.parametrize("fold", [str.lower, str.swapcase, str.capitalize])
def test_high_risk_confirmation_ignores_case(task, repo, fold):
    tid = file_ticket()
    commit_in(task, "k8s/app.yaml")
    pass_review(task, "codex"); pass_review(task, "claude")
    r = finish(repo, f"{fold(tid)}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert merged(repo)


def test_high_risk_wrong_id_still_cancels(task, repo):
    file_ticket()
    commit_in(task, "k8s/app.yaml")
    pass_review(task, "codex"); pass_review(task, "claude")
    r = finish(repo, "bd-9999\n")
    assert "Cancelled" in r.stdout and not merged(repo)


def test_high_risk_without_a_ticket_types_the_task_name(task, repo):
    commit_in(task, "scripts/check.sh", "#!/usr/bin/env bash\n# changed\nexit 0\n")
    pass_review(task, "codex"); pass_review(task, "claude")
    r = finish(repo, "demo\n")
    assert r.returncode == 0, r.stdout + r.stderr


def test_high_risk_with_weak_claude_model_is_refused(task, repo):
    commit_in(task, "k8s/app.yaml")
    pass_review(task, "codex"); pass_review(task, "claude", "sonnet")
    r = finish(repo, "demo\n")
    assert r.returncode == 1 and "stronger Claude model" in r.stdout


def test_stored_ticket_level_makes_a_docs_change_high(task, repo):
    tid = file_ticket()
    run("risk", tid, "--raise", "high")
    commit_in(task, "docs/a.md")
    pass_review(task, "codex")
    r = finish(repo, "y\n")
    assert r.returncode == 1 and "Risk level: HIGH" in r.stdout
    assert "raised by the ticket" in r.stdout


def test_new_commit_while_deciding_is_not_merged(task, repo, env):
    """The script merges the exact commit it checked; a commit added while the
    human is at the prompt makes it stop."""
    commit_in(task, "docs/a.md")
    pass_review(task)
    sneaky = env / "sneak.sh"
    # FLEET_TICKET wrapper: after the gate has run, move the branch tip.
    sneaky.write_text(
        "#!/bin/sh\n"
        f'python3 {TICKET} "$@"; rc=$?\n'
        f'case "$*" in *"--field confirm"*) git -C {task} commit --allow-empty -qm late;; esac\n'
        "exit $rc\n")
    sneaky.chmod(sneaky.stat().st_mode | stat.S_IEXEC)
    r = finish(repo, "y\n", extra_env={"FLEET_TICKET": str(sneaky)})
    assert r.returncode == 1 and "changed while you were deciding" in r.stdout
    assert not merged(repo)


def test_cannot_judge_risk_without_the_ticket_tool(task, repo):
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "y\n", extra_env={"FLEET_TICKET": "/nonexistent/ticket"})
    assert r.returncode == 1 and not merged(repo)


def test_emergency_override_is_loud_logged_and_still_needs_typing(task, repo, env):
    commit_in(task, "k8s/app.yaml")  # high risk and no review at all
    r = finish(repo, "y\n", "--emergency", "prod is down")
    assert "EMERGENCY OVERRIDE" in r.stdout and "Cancelled" in r.stdout
    assert not merged(repo) and not (env / "fleet" / "override.log").exists()
    r = finish(repo, "OVERRIDE\n", "--emergency", "prod is down")
    assert r.returncode == 0, r.stdout + r.stderr
    assert merged(repo)
    log = (env / "fleet" / "override.log").read_text()
    assert "branch=task/demo" in log and "risk=high" in log and "reason=prod is down" in log
    assert "EMERGENCY OVERRIDE (risk high): prod is down" in git(repo, "log", "-1", "--format=%B", "HEAD~1")   # HEAD is the release commit


def test_emergency_flag_is_harmless_when_reviews_are_fine(task, repo):
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "y\n", "--emergency", "unneeded")
    assert r.returncode == 0 and "EMERGENCY" not in r.stdout
    assert not (Path(os.environ["FLEET_HOME"]) / "override.log").exists()


def test_emergency_needs_a_reason(task, repo):
    commit_in(task, "docs/a.md")
    assert finish(repo, "y\n", "--emergency").returncode == 1


# --- Kaneo label -------------------------------------------------------------

KEY = "fake-key-for-tests-0001"


@pytest.fixture
def kaneo():
    fake = FakeKaneo(KEY).start()
    home = Path(os.environ["FLEET_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "kaneo.json"
    cfg.write_text(json.dumps({"url": fake.url, "workspace_id": "ws1", "api_key": KEY}))
    cfg.chmod(0o600)
    yield fake
    fake.stop()


def test_card_carries_the_risk_label(kaneo):
    tid = run("new", *new_args(type="investigate")).stdout.split()[0]
    (task_id,) = kaneo.tasks
    assert kaneo.label_names(task_id) == ["risk: low"]
    assert [l["name"] for l in kaneo.labels] == ["risk: low"]


def test_raising_swaps_the_label_and_sync_is_then_quiet(kaneo):
    tid = run("new", *new_args()).stdout.split()[0]
    (task_id,) = kaneo.tasks
    assert kaneo.label_names(task_id) == ["risk: medium"]
    r = run("risk", tid, "--raise", "high")
    assert r.returncode == 0, r.stderr
    assert kaneo.label_names(task_id) == ["risk: high"]
    assert all(r["path"].startswith(("/label", "/project", "/column", "/task")) for r in kaneo.requests)
    kaneo.requests.clear()
    assert f"{tid}  unchanged" in run("sync").stdout
    assert kaneo.writes == []


def test_sync_adds_the_label_to_an_old_card_and_dry_run_writes_nothing(kaneo):
    tid = run("new", *new_args()).stdout.split()[0]
    (task_id,) = kaneo.tasks
    kaneo.labels.clear()
    kaneo.requests.clear()
    assert f"{tid}  would update" in run("sync", "--dry-run").stdout
    assert kaneo.writes == [] and kaneo.label_names(task_id) == []
    assert f"{tid}  updated" in run("sync").stdout
    assert kaneo.label_names(task_id) == ["risk: medium"]
    assert len(kaneo.labels) == 1


def test_label_failure_after_card_creation_does_not_duplicate_the_card(kaneo):
    kaneo.fail_labels = True
    r = run("new", *new_args())
    tid = r.stdout.split()[0]
    assert "not shown on Kaneo" in r.stderr and len(kaneo.tasks) == 1
    assert json.loads((Path(os.environ["FLEET_HOME"]) / "kaneo-map.json").read_text())
    kaneo.fail_labels = False
    assert f"{tid}  updated" in run("sync").stdout
    (task_id,) = kaneo.tasks
    assert kaneo.label_names(task_id) == ["risk: medium"]


def test_hook_blocks_agents_from_raising():
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard_bash", ROOT / ".claude/hooks/guard_bash.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.scan_command("ticket risk BD-1 --raise high", [Path("/tmp")], 0, True) is not None
    assert mod.scan_command("ticket risk --diff main...task/x", [Path("/tmp")], 0, True) is None


def test_tickets_of_another_repo_do_not_count(task, repo):
    other = file_ticket(repo="other-repo")
    run("risk", other, "--raise", "high")
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Risk level: LOW" in r.stdout


def test_emergency_merge_still_reconciles_the_ticket(task, repo):
    from fleet import dispatch
    base = git(repo, "rev-parse", "HEAD")
    commit_in(task, "k8s/app.yaml")
    assert not dispatch.is_merged(repo, "task/demo", base)
    r = finish(repo, "OVERRIDE\n", "--emergency", "prod is down")
    assert r.returncode == 0, r.stdout + r.stderr
    assert dispatch.is_merged(repo, "task/demo", base)
    assert not dispatch.is_merged(repo, "task/other", base)


def test_reconcile_still_needs_the_exact_branch_in_an_override_message(repo):
    from fleet import dispatch
    base = git(repo, "rev-parse", "HEAD")
    branch_with(repo, "task/demo", ["docs/a.md"])
    git(repo, "merge", "--no-ff", "task/demo", "-m", "Merge task/demo-2\n\nEMERGENCY OVERRIDE (risk high): x")
    assert not dispatch.is_merged(repo, "task/demo", base)


def test_label_failure_does_not_stop_the_card_update(kaneo):
    tid = run("new", *new_args()).stdout.split()[0]
    (task_id,) = kaneo.tasks
    kaneo.tasks[task_id]["title"] = "stale"
    kaneo.fail_labels = True
    r = run("sync")
    assert "risk label was not" in r.stdout
    assert kaneo.tasks[task_id]["title"] != "stale"


def test_fake_kaneo_rejects_the_old_label_paths(kaneo):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    req = Request(kaneo.url + "/tasks/t1/labels", headers={"Authorization": f"Bearer {KEY}"})
    with pytest.raises(HTTPError) as err:
        urlopen(req)
    assert err.value.code == 404


# --- the written MUST FIX bar --------------------------------------------------

def prompt_recorder(bindir, name, log):
    """A fake reviewer that saves every argument it was given (the prompt)."""
    bindir.mkdir(exist_ok=True)
    p = bindir / name
    p.write_text("#!/bin/sh\n"
                 'if [ "$1" = "login" ]; then exit 0; fi\n'
                 f'for a in "$@"; do printf "%s\\n" "$a"; done >> {log}\n'
                 'echo "VERDICT: PASS"\necho "SUMMARY: fake"\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_review_prompt_has_the_must_fix_bar(task, env, kind):
    commit_in(task, "fleet/x.py")
    log = env / f"{kind}-prompt.txt"
    prompt_recorder(env / "bin", kind, log)
    r = run_review(task, env / "bin", REVIEWER=kind)
    assert r.returncode == 0, r.stdout + r.stderr
    text = log.read_text()
    assert "MUST FIX bar" in text
    for reason in ("SECURITY", "DATA LOSS", "WRONG BEHAVIOR", "TESTS"):
        assert reason in text
    assert "guard or the merge gate" in text
    assert "failing or weakened tests" in text
    assert "rare or\nsimultaneous failures" in text and "SHOULD FIX" in text
    assert "which of the four reasons applies" in text


def test_both_reviewers_get_the_same_bar(task, env):
    commit_in(task, "fleet/x.py")
    logs = {k: env / f"{k}-p.txt" for k in ("codex", "claude")}
    for k, log in logs.items():
        prompt_recorder(env / "bin", k, log)
    assert run_review(task, env / "bin", REVIEWER="both").returncode == 0
    bar = lambda t: t[t.index("The MUST FIX bar"):t.index("SUMMARY: two")]
    assert bar(logs["codex"].read_text()) == bar(logs["claude"].read_text())


# --- several tickets share one branch (BD-45) --------------------------------

def shared_branch_tickets(n=3):
    """Tickets sharing task/demo: the duplicate guard stops `ticket new` now, but old ones exist."""
    from fleet import ticket as tk
    f = {"type": "build", "repo": "repo", "branch": "task/demo", "body": "Details."}
    return [tk.create_ticket({**f, "title": t})["id"]
            for t in ["Alpha widget", "Quartz harbour", "Mellow zebra crossing"][:n]]


def test_high_risk_confirm_word_is_the_ticket_given(task, repo):
    first, _, newest = shared_branch_tickets()
    commit_in(task, "k8s/app.yaml")
    pass_review(task, "codex"); pass_review(task, "claude")
    r = finish(repo, f"{newest}\n", "--ticket", first)
    assert f"({first})" in r.stdout and "Cancelled" in r.stdout and not merged(repo)
    r = finish(repo, f"{first}\n", "--ticket", first)
    assert r.returncode == 0, r.stdout + r.stderr
    assert merged(repo)


def test_other_unfinished_tickets_on_the_branch_are_listed_first(task, repo):
    first, second, third = shared_branch_tickets()
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "n\n", "--ticket", first)
    assert "WARNING" in r.stdout and second in r.stdout and third in r.stdout
    assert r.stdout.index("WARNING") < r.stdout.index("== 3/5")
    assert f"  {first} (" not in r.stdout.split("WARNING")[1].split("==")[0]


def test_a_ticket_on_another_branch_is_refused(task, repo):
    mine = file_ticket()
    other = file_ticket(branch="task/elsewhere", title="Quartz harbour")
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "y\n", "--ticket", other)
    assert r.returncode == 1 and "NOT MERGING" in r.stdout and "task/elsewhere" in r.stdout
    assert not merged(repo)
    # an emergency override does not get round it
    r = finish(repo, "OVERRIDE\n", "--ticket", other, "--emergency", "why")
    assert r.returncode == 1 and not merged(repo)
    assert finish(repo, "y\n", "--ticket", mine).returncode == 0


def test_an_unknown_ticket_is_refused(task, repo):
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "y\n", "--ticket", "BD-999")
    assert r.returncode == 1 and "no such ticket" in r.stdout and not merged(repo)


def test_finished_tickets_on_the_branch_are_not_warned_about(task, repo):
    from fleet import ticket as tk
    first, second = shared_branch_tickets(2)
    tk.set_status(second, "claimed", allowed_from=("open",))
    tk.set_status(second, "done", allowed_from=("claimed",))
    commit_in(task, "docs/a.md")
    pass_review(task)
    r = finish(repo, "n\n", "--ticket", first)
    assert "WARNING" not in r.stdout


# --- BD-124 / BD-136: voters are data-driven; cursor counts only when its header says so ----

def cursor_text(counted="yes", model="composer-2.5", verdict="PASS", commit=SHA):
    return (f"Reviewer: Cursor (composer-2.5)\nCommit: {commit}\nModel: {model}\nCounted: {counted}\n\n"
            f"VERDICT: {verdict}\nMUST FIX:\nSUMMARY: ok\n")


def test_cursor_review_gates_only_when_counted(tmp_path):
    assert rk.parse_review(review_text("Cursor composer"))["kind"] == "cursor"
    assert rk.voter_kind("Cursor (composer-2.5)") == "cursor" and rk.voter_kind("gpt") is None
    assert rk.voter_kind("Codex gpt-5") == "codex" and rk.voter_kind("Claude (opus)") == "claude"
    assert {v["company"] for v in rk.VOTERS.values()} == {"openai", "anthropic", "cursor"}
    # no "Counted: yes" header, a non-Cursor model, or no model: not counted
    for text in (review_text("Cursor composer"), cursor_text(counted="no"),
                 cursor_text(model="claude-4-sonnet"), cursor_text(model="none")):
        assert rk.parse_review(text)["counted"] is False
    assert rk.parse_review(cursor_text())["counted"] is True
    d = tmp_path / "rv"
    put(d, "1.md", review_text("Codex"))
    put(d, "2.md", review_text("Claude (opus)"))
    put(d, "3.md", cursor_text(counted="no", verdict="FAIL"), age=10)
    got = rk.latest_reviews(d, SHA)
    assert "cursor" in got
    assert rk.check_reviews(got, "high") == []          # the uncounted FAIL does not gate
    put(d, "4.md", cursor_text(verdict="FAIL"), age=0)
    assert rk.check_reviews(rk.latest_reviews(d, SHA), "high")   # a counted FAIL does
    # a cursor PASS cannot stand in for the Claude review high risk needs
    only = {"codex": rk.parse_review(review_text("Codex")), "cursor": rk.parse_review(cursor_text())}
    only["codex"]["file"] = only["cursor"]["file"] = "x"
    assert any("Claude" in p for p in rk.check_reviews(only, "high"))
    assert rk.counted("cursor", {"counted": True}) and rk.counted("codex") and rk.counted("claude")
    assert not rk.counted("cursor") and not rk.counted("cursor", {"result": "PASS"})   # no proof flag: advisory
    assert not rk.counted("cursor", {"counted": False}) and not rk.counted("nobody")


def test_agents_and_creators_exclude_cursor():
    assert "cursor" in tk.REVIEW_KINDS
    assert "cursor" not in tk.AGENTS and "cursor" not in tk.CREATORS
