import json
import os
import re
import subprocess
import sys

import pytest

from conftest import ROOT
from fleet import ticket as tk

IDEA = ROOT / "bin" / "idea"
ALERT = ROOT / "bin" / "alert"


def sh(exe, *args, extra_env=None):
    e = dict(os.environ)
    e.update(extra_env or {})
    return subprocess.run([sys.executable, str(exe), *args], capture_output=True,
                          text=True, env=e)


def test_idea_files_an_investigate_ticket():
    r = sh(IDEA, "look", "into", "caching")
    assert r.returncode == 0, r.stderr
    tid = r.stdout.strip()
    assert tid == "ID-1"
    t = tk.load_ticket(tid)
    assert (t["type"], t["repo"], t["status"]) == ("investigate", "ideas", "proposed")
    assert t["title"] == t["body"] == "look into caching"
    assert re.fullmatch(r"task/idea-\d{20}", t["branch"])
    assert "priority" not in t


def test_idea_title_is_first_100_characters_body_is_all():
    text = "x" * 150
    t = tk.load_ticket(sh(IDEA, text).stdout.strip())
    assert t["title"] == "x" * 100 and t["body"] == text


def test_idea_multiline_text_gives_single_line_title():
    t = tk.load_ticket(sh(IDEA, "first line\nsecond line").stdout.strip())
    assert "\n" not in t["title"] and t["body"] == "first line\nsecond line"


@pytest.mark.parametrize("agent_env", [{}, {"CLAUDECODE": "1"}])
def test_idea_is_proposed_whoever_files_it(agent_env):
    assert tk.load_ticket(sh(IDEA, "an idea", extra_env=agent_env).stdout.strip())["status"] == "proposed"


@pytest.mark.parametrize("args", [[], [""], ["  ", "\n"]])
def test_idea_without_text_prints_usage(args):
    r = sh(IDEA, *args)
    assert r.returncode == 2 and r.stdout == "" and "usage: idea TEXT" in r.stderr
    assert "Traceback" not in r.stderr and tk.list_tickets() == []


def test_alert_files_an_urgent_build_ticket():
    r = sh(ALERT, "myrepo", "disk", "is", "full")
    assert r.returncode == 0, r.stderr
    tid = r.stdout.strip()
    assert tid == "EM-1"
    t = tk.load_ticket(tid)
    assert (t["type"], t["repo"], t["priority"], t["status"]) == ("build", "myrepo", "urgent", "open")
    assert t["title"] == t["body"] == "disk is full"
    assert re.fullmatch(r"task/alert-\d{20}", t["branch"])


def test_alert_title_is_first_100_characters():
    t = tk.load_ticket(sh(ALERT, "myrepo", "y" * 120).stdout.strip())
    assert t["title"] == "y" * 100 and t["body"] == "y" * 120


def test_alert_multiline_title_is_first_line_only():
    text = "fleet up/down\nRe-filed from BD-41 as urgent; BD-4"
    t = tk.load_ticket(sh(ALERT, "myrepo", text).stdout.strip())
    assert t["title"] == "fleet up/down" and t["body"] == text


def test_alert_skips_leading_blank_lines_and_trims_long_first_line():
    t = tk.load_ticket(sh(ALERT, "myrepo", "\n\n" + "z" * 120 + "\nmore").stdout.strip())
    assert t["title"] == "z" * 100


def test_fix_titles_retitles_squashed_open_tickets(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    body = "disk full\nsecond line"
    squashed = tk.create_ticket({"type": "build", "repo": "r", "branch": "task/a-1",
                                 "title": "disk full second line", "body": body})
    manual = tk.create_ticket({"type": "build", "repo": "r", "branch": "task/a-2",
                               "title": "my own title", "body": body})
    assert [t["id"] for t in tk.fix_titles()] == [squashed["id"]]
    assert tk.load_ticket(squashed["id"])["title"] == "disk full"
    assert tk.load_ticket(manual["id"])["title"] == "my own title"
    assert tk.fix_titles() == []


@pytest.mark.parametrize("args", [[], ["myrepo"], ["myrepo", " "]])
def test_alert_without_text_prints_usage(args):
    r = sh(ALERT, *args)
    assert r.returncode == 2 and r.stdout == "" and "usage: alert REPO TEXT" in r.stderr
    assert tk.list_tickets() == []


def test_alert_bad_repo_is_a_plain_error():
    r = sh(ALERT, "bad repo!", "text")
    assert r.returncode == 2 and "repo must be" in r.stderr and "Traceback" not in r.stderr


def test_alert_body_too_long_is_a_plain_error():
    r = sh(ALERT, "myrepo", "z" * 5000)
    assert r.returncode == 2 and "body must be" in r.stderr


def test_alert_from_agent_is_proposed_and_urgent():
    t = tk.load_ticket(sh(ALERT, "myrepo", "boom", extra_env={"CODEX_SANDBOX": "x"}).stdout.strip())
    assert (t["created_by"], t["status"], t["priority"]) == ("codex", "proposed", "urgent")


def test_tickets_filed_back_to_back_get_different_branches():
    from fleet.shortcuts_cli import alert_main, idea_main
    for main, args in ((alert_main, ["myrepo", "{}"]), (idea_main, ["{}"])):
        for word in ("alpha", "quokka", "zebra"):  # different titles: the duplicate guard stays quiet
            assert main([a.format(word * 3) for a in args]) == 0
    branches = [t["branch"] for t in tk.list_tickets()]
    assert len(branches) == 6 and len(set(branches)) == 6
    assert all(tk.BRANCH_RE.fullmatch(b) and len(b) - 5 <= 41 for b in branches)


def test_title_keeps_other_whitespace():
    t = tk.load_ticket(sh(IDEA, "a  b\tc\r\nd").stdout.strip())
    assert t["title"] == "a  b\tc d"
