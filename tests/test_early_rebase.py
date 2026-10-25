"""BD-126: before a review round the dispatcher checks (read-only) whether the branch lags or
conflicts with current main; if so it skips the review and wakes the ticket's own agent to rebase
in its task folder. Real git, fake herdr, temp FLEET_HOME."""

import pytest

from fleet import dispatch as dp
from fleet import review_watch as rw
from test_dispatch import calls, git, herdr_log, projects  # noqa: F401
from test_moved_branch import _clean_rereviews, reviewed, scripted  # noqa: F401
from test_review_watch import Rig, no_dialog, ntfy, rig  # noqa: F401



def prompts(log):
    return [c for c in calls(log) if c[:2] == ["agent", "prompt"]] if log.exists() else []


def main_commit(rig, name, text="x"):
    """A commit on main made from a throwaway checkout, so the main checkout is never touched."""
    other = rig.repo.parent / f"other-{name}"
    git(rig.repo, "worktree", "add", "-q", "--detach", str(other), "main")
    (other / f"{name}.txt").write_text(text)
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", name)
    sha = git(other, "rev-parse", "HEAD").strip()
    git(rig.repo, "worktree", "remove", "--force", str(other))
    git(rig.repo, "update-ref", "refs/heads/main", sha)
    return sha


def ready(rig):
    rig.passes()
    rig.summary()
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "review"


def test_main_moving_elsewhere_is_not_stale(scripted):
    main_commit(scripted, "unrelated")
    assert dp.stale_base(scripted.repo, "task/demo") is None


def test_overlap_and_conflict_are_stale(scripted):
    main_commit(scripted, "first", "from main")     # the branch also has first.txt, with other text
    got = dp.stale_base(scripted.repo, "task/demo")
    assert got and got[0] == git(scripted.repo, "rev-parse", "main").strip()


# (9) main moves between review and ship => the verdict is not trusted, the agent rebases
def test_main_moving_after_pass_sends_it_back_for_a_rebase(scripted, herdr_log, env):
    ready(scripted)
    main_commit(scripted, "first", "from main")
    scripted.cycle(1)
    t = scripted.get()
    assert t["status"] == "claimed"
    assert any(n["text"].startswith("Rebase needed") for n in t["notes"])
    assert len(prompts(herdr_log)) == 1 and "rebase" in prompts(herdr_log)[0][3]
    rw.wait_rereviews()
    assert reviewed(env) == []                      # no review of a stale base
    scripted.cycle(2)
    assert len(prompts(herdr_log)) == 1             # asked once per (tip, main)


# (11) the agent rewrites history: the new sha is re-reviewed, old verdicts are ignored
def test_rebased_commit_is_reviewed_on_its_new_sha(scripted, herdr_log, env):
    ready(scripted)
    old = scripted.tip()
    main_commit(scripted, "first", "from main")
    scripted.cycle(1)
    git(scripted.wt, "rebase", "-q", "-X", "theirs", "main")   # the agent resolves the conflict
    new = scripted.tip()
    assert new != old and dp.stale_base(scripted.repo, "task/demo") is None
    scripted.cycle(2)
    rw.wait_rereviews()
    assert reviewed(env) == [new]
    scripted.cycle(3)
    t = scripted.get()
    assert t["verdict"]["commit"] == new and t["status"] == "review"


# (10) a conflict in a guard file stays human-ship: the risk level is untouched by the rebase wake
def test_guard_file_conflict_wakes_the_agent_but_risk_stays_high(scripted, herdr_log):
    (scripted.wt / ".claude" / "hooks").mkdir(parents=True)
    (scripted.wt / ".claude/hooks/guard_bash.py").write_text("branch\n")
    git(scripted.wt, "add", "-A")
    git(scripted.wt, "commit", "-q", "-m", "guard")
    main_commit(scripted, ".claude-guard", "x")
    other = scripted.repo.parent / "o"
    git(scripted.repo, "worktree", "add", "-q", "--detach", str(other), "main")
    (other / ".claude/hooks").mkdir(parents=True)
    (other / ".claude/hooks/guard_bash.py").write_text("main\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "g")
    git(scripted.repo, "update-ref", "refs/heads/main", git(other, "rev-parse", "HEAD").strip())
    git(scripted.repo, "worktree", "remove", "--force", str(other))
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    assert "conflicts" in dp.stale_base(scripted.repo, "task/demo")[1]
    assert scripted.get()["status"] == "claimed" and len(prompts(herdr_log)) == 1


# (12) the main checkout is never touched
def test_main_checkout_is_untouched(scripted, herdr_log):
    ready(scripted)
    main_commit(scripted, "first", "from main")
    before = (git(scripted.repo, "status", "--porcelain"), git(scripted.repo, "rev-parse", "HEAD"),
              git(scripted.repo, "stash", "list"))
    scripted.cycle(1)
    assert before == (git(scripted.repo, "status", "--porcelain"), git(scripted.repo, "rev-parse", "HEAD"),
                      git(scripted.repo, "stash", "list"))


def test_clean_merge_with_shared_file_is_stale_by_overlap(scripted):
    main_commit(scripted, "shared file", "1\n2\n3\n4\n5\n6\n7\n8\n9\n")   # a name with a space
    git(scripted.wt, "merge", "-q", "main")
    (scripted.wt / "shared file.txt").write_text("one\n2\n3\n4\n5\n6\n7\n8\n9\n")
    git(scripted.wt, "add", "-A")
    git(scripted.wt, "commit", "-q", "-m", "edit top")
    main_commit(scripted, "shared file", "1\n2\n3\n4\n5\n6\n7\n8\nnine\n")
    got = dp.stale_base(scripted.repo, "task/demo")
    assert got and "changed files" in got[1] and "shared file.txt" in got[1]


def test_agent_that_stops_without_rebasing_still_fails_by_the_stall_rules(scripted, herdr_log):
    ready(scripted)
    main_commit(scripted, "first", "from main")
    scripted.cycle(1)
    assert scripted.get()["status"] == "claimed" and len(prompts(herdr_log)) == 1
    scripted.set_agent("idle")
    scripted.cycle(2)
    scripted.cycle(60)    # an hour with no new commit: the normal stall rule closes it
    assert scripted.get()["status"] != "claimed"
