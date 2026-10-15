"""BD-101: scripts/release-tag.sh tags a ship and writes a Keep a Changelog entry."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release-tag.sh"
FINISH = SCRIPT.with_name("finish-task.sh")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def release(repo, ref, title="", date="2026.10.15"):
    return subprocess.run(["bash", str(SCRIPT), ref, title], cwd=repo, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo), "RELEASE_DATE": date})


def make_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_creates_changelog_and_annotated_tag(tmp_path):
    repo = make_repo(tmp_path)
    done = release(repo, "BD-101", "Tag every ship")
    assert done.returncode == 0, done.stderr
    text = (repo / "CHANGELOG.md").read_text()
    assert "Keep a Changelog" in text and "## [Unreleased]" in text
    assert "## [ship-2026.10.15.1] - 2026-10-15" in text
    assert "- BD-101: Tag every ship" in text
    assert text.index("[Unreleased]") < text.index("[ship-2026.10.15.1]")
    assert git(repo, "cat-file", "-t", "ship-2026.10.15.1") == "tag"      # annotated, not lightweight
    assert git(repo, "rev-parse", "ship-2026.10.15.1^{commit}") == git(repo, "rev-parse", "HEAD")
    assert "ticket: BD-101" in git(repo, "tag", "-l", "--format=%(contents)", "ship-2026.10.15.1")
    assert git(repo, "status", "--porcelain") == ""


def test_same_day_counts_up_and_newest_is_first(tmp_path):
    repo = make_repo(tmp_path)
    assert release(repo, "BD-1", "one").returncode == 0
    assert release(repo, "BD-2", "two").returncode == 0
    text = (repo / "CHANGELOG.md").read_text()
    assert text.index("ship-2026.10.15.2") < text.index("ship-2026.10.15.1")
    assert git(repo, "tag", "-l", "ship-*").split() == ["ship-2026.10.15.1", "ship-2026.10.15.2"]


def test_refuses_a_dirty_tree_and_makes_no_tag(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    done = release(repo, "BD-3")
    assert done.returncode != 0
    assert git(repo, "tag", "-l") == ""


def test_finish_task_tags_only_after_the_post_merge_checks():
    text = FINISH.read_text()
    checks = text.index("== 4/5 Checks on")
    tagging = text.index("release-tag.sh", checks)
    push = text.index("git push origin", tagging)
    assert checks < tagging < push
    assert "--follow-tags" in text
