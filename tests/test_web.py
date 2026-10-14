"""EM-13: approve and ship from the web endpoint, refusing the machine the agents
run on; and ntfy pings that carry a Click URL, action buttons and a reason.
A fake tailscale, a fake ntfy and a fake Kaneo; temporary repos and FLEET_HOME."""

import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from fake_kaneo import FakeKaneo
from fleet import notify, risk as rk, ticket as tk, web, webconf
from fleet.ship_cli import main as ship_main
from test_dispatch import claim, git, make_repo, mk, projects  # noqa: F401
from test_kaneo import API_KEY, write_config as write_kaneo_config
from test_notify import FakeNtfy, write_config as write_ntfy_config

FINISH = """#!/bin/bash
# Fake finish-task: records its stdin answer, merges the task branch.
read -r answer
echo "$PWD|$*|$answer" >> "$FAKE_LOG"
git -c user.name=T -c user.email=t@example.com merge -q --no-ff "task/$1" -m "Merge task/$1"
"""

TAILSCALE = """#!/bin/sh
case "$1" in
  whois) cat "$FAKE_WHOIS"; exit "${FAKE_WHOIS_EXIT:-0}" ;;
  status) echo '{"Self": {"ID": "n-loic", "StableID": "s-loic", "HostName": "loic", "DNSName": "loic.tail.ts.net."}}' ;;
esac
"""

PHONE = {"Node": {"ID": "n-phone", "StableID": "s-phone", "Name": "phone.tail.ts.net."}}
LOIC = {"Node": {"ID": "n-loic", "StableID": "s-loic", "Name": "loic.tail.ts.net."}}


def as_node(node, monkeypatch, exit_code=0):
    monkeypatch.setenv("FAKE_WHOIS", str(Path(os.environ["FLEET_HOME"]).parent / "whois.json"))
    Path(os.environ["FAKE_WHOIS"]).write_text(json.dumps(node) if isinstance(node, dict) else node)
    monkeypatch.setenv("FAKE_WHOIS_EXIT", str(exit_code))


@pytest.fixture
def ntfy():
    fake = FakeNtfy()
    write_ntfy_config(fake.url)
    yield fake
    fake.stop()


@pytest.fixture
def endpoint(env, monkeypatch):
    ts = env / "tailscale"
    ts.write_text(TAILSCALE)
    ts.chmod(ts.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FLEET_TAILSCALE", str(ts))
    as_node(PHONE, monkeypatch)
    server = web.serve({"host": "127.0.0.1", "port": 0})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    Path(os.environ["FLEET_HOME"]).mkdir(parents=True, exist_ok=True)
    (Path(os.environ["FLEET_HOME"]) / "web.json").write_text(json.dumps(
        {"listen": f"127.0.0.1:{server.server_port}", "url": "http://loic.tail.ts.net:8787"}))
    yield base
    server.shutdown()
    server.server_close()


def call(base, path, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data), timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def confirm(base, path, **extra):
    """Open the page, then press Confirm (with whatever else is typed)."""
    status, page = call(base, path)
    assert status == 200, page
    form = dict(re.findall(r"name=(\w+) value=([0-9a-f-]+)", page))
    form.update(extra)
    return call(base, path, form)


def reviewed(wt, repo, kinds=("codex",), verdict="PASS"):
    full = git(wt, "rev-parse", "HEAD").strip()
    (wt / ".reviews").mkdir(exist_ok=True)
    names = {"codex": "codex", "claude": "claude opus"}
    for kind in kinds:
        (wt / ".reviews" / f"task-demo-{full[:7]}-{kind}.md").write_text(
            f"Reviewer: {names[kind]}\nCommit: {full}\n\nVERDICT: {verdict}\nSUMMARY: ok\n")
    return full


@pytest.fixture
def rig(projects, env, monkeypatch):
    """A repo with a fake finish-task, a task worktree with a commit, and a ticket in review."""
    repo = make_repo(projects)
    (repo / "scripts" / "finish-task.sh").write_text(FINISH)
    (repo / ".gitignore").write_text(".worktrees/\n.reviews/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fake finish")
    wt = repo / ".worktrees" / "demo"
    git(repo, "worktree", "add", "-q", "-b", "task/demo", str(wt))
    (wt / "work.txt").write_text("w")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "work")
    log = env / "finish.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    t = mk()
    claim(t)
    tk.set_status(t["id"], "review", allowed_from=("claimed",))
    return type("Rig", (), {"repo": repo, "wt": wt, "t": t, "log": log})


def wait_for(condition, seconds=40):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.2)
    return False


def pings(ntfy, title_part):
    return [r["body"] for r in ntfy.requests if title_part in r["body"]["title"]]


# --- the device check ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["/ship/{id}", "/approve/{id}", "/nothing"])
def test_request_from_loic_is_refused_before_anything_else(endpoint, rig, monkeypatch, path):
    as_node(LOIC, monkeypatch)
    reviewed(rig.wt, rig.repo)
    path = path.format(id=rig.t["id"])
    assert call(endpoint, path)[0] == 403
    assert call(endpoint, path, {"token": "x", "commit": "y"})[0] == 403
    assert not rig.log.exists()
    assert tk.load_ticket(rig.t["id"])["status"] == "review"


@pytest.mark.parametrize("answer, code", [("", 0), ("not json", 0), ("{}", 0), ('{"Node": {}}', 0),
                                          ('{"Node": {"ID": "n-phone"}}', 1)])
def test_unclear_who_is_refused(endpoint, rig, monkeypatch, answer, code):
    as_node(answer, monkeypatch, exit_code=code)
    assert call(endpoint, f"/ship/{rig.t['id']}")[0] == 403


def test_a_loic_page_cannot_be_used_from_loic_with_a_token(endpoint, rig, monkeypatch, ntfy):
    reviewed(rig.wt, rig.repo)
    status, page = call(endpoint, f"/ship/{rig.t['id']}")
    assert status == 200
    form = dict(re.findall(r"name=(\w+) value=([0-9a-f-]+)", page))
    as_node(LOIC, monkeypatch)   # the same page, now POSTed from the agents' machine
    assert call(endpoint, f"/ship/{rig.t['id']}", form)[0] == 403
    assert not rig.log.exists() and ntfy.requests == []


# --- ship --------------------------------------------------------------------------

def test_other_node_with_confirmation_ships_in_the_background_and_pings(endpoint, rig, ntfy):
    full = reviewed(rig.wt, rig.repo)
    status, page = call(endpoint, f"/ship/{rig.t['id']}")
    assert status == 200
    for shown in (rig.t["id"], "MEDIUM", full, "codex: PASS"):
        assert shown in page
    status, body = confirm(endpoint, f"/ship/{rig.t['id']}")
    assert status == 202 and "close this page" in body
    assert wait_for(lambda: tk.load_ticket(rig.t["id"])["status"] == "done")
    assert rig.log.read_text().strip() == f"{rig.repo.resolve()}|demo --ticket {rig.t['id']}|y"
    assert list((Path(os.environ["FLEET_HOME"]) / "web").glob(f"ship-{rig.t['id']}-*.log"))
    assert wait_for(lambda: len(ntfy.requests) == 1)
    (ping,) = ntfy.requests
    assert ping["body"]["title"] == f"Shipped: {rig.t['id']}" and "merged" in ping["body"]["message"]


def test_missing_review_refuses_and_says_why(endpoint, rig, ntfy):
    status, page = confirm(endpoint, f"/ship/{rig.t['id']}")
    assert status == 409 and "Not allowed" in page and "no review" in page
    assert not rig.log.exists() and tk.load_ticket(rig.t["id"])["status"] == "review"
    assert len(pings(ntfy, "Ship refused")) == 1


def test_failing_review_refuses(endpoint, rig):
    reviewed(rig.wt, rig.repo, verdict="FAIL")
    assert confirm(endpoint, f"/ship/{rig.t['id']}")[0] == 409
    assert not rig.log.exists()


def test_high_risk_needs_the_typed_id(endpoint, rig, ntfy):
    tk.raise_risk(rig.t["id"], "high")
    reviewed(rig.wt, rig.repo, kinds=("codex", "claude"))
    path = f"/ship/{rig.t['id']}"
    assert "HIGH" in call(endpoint, path)[1]
    assert confirm(endpoint, path)[0] == 400
    assert confirm(endpoint, path, typed="wrong")[0] == 400
    assert not rig.log.exists()
    assert confirm(endpoint, path, typed=rig.t["id"].lower())[0] == 202   # case-insensitive
    assert wait_for(lambda: tk.load_ticket(rig.t["id"])["status"] == "done")
    # finish-task's own prompt is answered with the ticket id, as at a terminal
    assert rig.log.read_text().strip().endswith(f"|demo --ticket {rig.t['id']}|{rig.t['id']}")


def test_high_risk_with_only_one_reviewer_refuses(endpoint, rig):
    tk.raise_risk(rig.t["id"], "high")
    reviewed(rig.wt, rig.repo, kinds=("codex",))
    assert confirm(endpoint, f"/ship/{rig.t['id']}", typed=rig.t["id"])[0] == 409
    assert not rig.log.exists()


def test_page_is_stale_when_the_branch_moves(endpoint, rig):
    reviewed(rig.wt, rig.repo)
    path = f"/ship/{rig.t['id']}"
    form = dict(re.findall(r"name=(\w+) value=([0-9a-f-]+)", call(endpoint, path)[1]))
    (rig.wt / "more.txt").write_text("m")
    git(rig.wt, "add", "-A")
    git(rig.wt, "commit", "-q", "-m", "later")
    reviewed(rig.wt, rig.repo)
    assert call(endpoint, path, form)[0] == 409
    assert not rig.log.exists()


def test_pages_cannot_be_framed(endpoint, rig):
    reviewed(rig.wt, rig.repo)
    with urllib.request.urlopen(f"{endpoint}/ship/{rig.t['id']}") as r:
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Frame-Options"] == "DENY"


def test_forged_token_is_refused(endpoint, rig):
    full = reviewed(rig.wt, rig.repo)
    assert call(endpoint, f"/ship/{rig.t['id']}", {"token": "0" * 64, "commit": full})[0] == 409
    assert not rig.log.exists()


def test_ship_only_from_review(endpoint, rig):
    reviewed(rig.wt, rig.repo)
    tk.set_status(rig.t["id"], "done", allowed_from=("review",))
    assert call(endpoint, f"/ship/{rig.t['id']}")[0] == 409


def test_non_interactive_ship_refuses_a_moved_commit(rig, ntfy):
    reviewed(rig.wt, rig.repo)
    assert ship_main([rig.t["id"], "--non-interactive", "0" * 40]) == 1
    assert not rig.log.exists()
    (ping,) = ntfy.requests
    assert ping["body"]["title"] == f"Ship refused: {rig.t['id']}" and "not the 00000000" in ping["body"]["message"]


def test_non_interactive_ship_never_runs_a_review(rig, ntfy):
    full = git(rig.wt, "rev-parse", "HEAD").strip()
    assert ship_main([rig.t["id"], "--non-interactive", full]) == 1
    assert not rig.log.exists()
    assert "no review" in ntfy.requests[0]["body"]["message"]


# --- approve -----------------------------------------------------------------------

def proposed(**over):
    return tk.create_ticket({"type": "build", "repo": "app", "branch": "task/p", "title": "Idea",
                             "body": "b", **over}, needs_approval=True)


def test_approve_from_another_node(endpoint, projects):
    t = proposed()
    assert t["status"] == "proposed"
    status, body = confirm(endpoint, f"/approve/{t['id']}")
    assert status == 200 and "approved" in body
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_approve_from_loic_is_refused(endpoint, projects, monkeypatch):
    t = proposed()
    as_node(LOIC, monkeypatch)
    assert call(endpoint, f"/approve/{t['id']}", {"token": "x"})[0] == 403
    assert tk.load_ticket(t["id"])["status"] == "proposed"


def test_approve_high_risk_needs_the_typed_id(endpoint, projects):
    t = proposed()
    tk.raise_risk(t["id"], "high")
    assert confirm(endpoint, f"/approve/{t['id']}")[0] == 400
    assert tk.load_ticket(t["id"])["status"] == "proposed"
    assert confirm(endpoint, f"/approve/{t['id']}", typed=t["id"].lower())[0] == 200
    assert tk.load_ticket(t["id"])["status"] == "open"


# --- configuration -----------------------------------------------------------------

@pytest.mark.parametrize("listen", ["0.0.0.0:8787", ":::8787", "8787", "100.64.0.5", "host:80", "100.64.0.5:0"])
def test_listen_must_be_one_address(listen):
    home = Path(os.environ["FLEET_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "web.json").write_text(json.dumps({"listen": listen, "url": "http://x:1"}))
    with pytest.raises(webconf.WebConfigError):
        webconf.load_config()


def test_off_without_config():
    with pytest.raises(webconf.NotConfigured):
        webconf.load_config()
    assert webconf.action_url("ship", "BD-1") is None


# --- Kaneo card links and card address ------------------------------------------------

@pytest.fixture
def kaneo(endpoint):
    fake = FakeKaneo(API_KEY).start()
    write_kaneo_config(fake.url)
    yield fake
    fake.stop()


def test_cards_get_links_and_the_card_address_is_saved(kaneo, rig):
    from fleet import kaneo as kn
    p = proposed()
    for tid in (p["id"], rig.t["id"]):
        outcome = dict(kn.sync([tk.load_ticket(tid)]))[tid]
        assert outcome == "created"
    cards = {c["title"].split("]")[0][1:]: c for c in kaneo.tasks.values()}
    assert f"**[Approve](http://loic.tail.ts.net:8787/approve/{p['id']})**" in cards[p["id"]]["description"]
    assert f"**[Ship](http://loic.tail.ts.net:8787/ship/{rig.t['id']})**" in cards[rig.t["id"]]["description"]
    url = kn.card_url(rig.t["id"])
    task_id = cards[rig.t["id"]]["id"]
    assert url.endswith(f"/project/{kaneo.projects[0]['id']}/task/{task_id}") and "/workspace/ws1/" in url


def test_open_ticket_card_has_no_link(kaneo, rig):
    from fleet import kaneo as kn
    t = mk(branch="task/other")
    list(kn.sync([t]))
    (card,) = [c for c in kaneo.tasks.values() if t["id"] in c["title"]]
    assert "approve/" not in card["description"] and "ship/" not in card["description"]


# --- ntfy: Click, Actions, and a message that explains itself ----------------------------

def save_card(ticket_id, url="http://kaneo.example/dashboard/workspace/ws1/project/p1/task/k1"):
    path = Path(os.environ["FLEET_HOME"]) / "kaneo-cards.json"
    path.write_text(json.dumps({ticket_id: url}))
    return url


def test_ship_ping_has_click_open_card_and_ship_action(endpoint, rig, ntfy):
    url = save_card(rig.t["id"])
    notify.send(tk.load_ticket(rig.t["id"]), "ship")
    (req,) = ntfy.requests
    body = req["body"]
    assert body["click"] == url
    assert body["actions"] == [
        {"action": "view", "label": "Open card", "url": url},
        {"action": "view", "label": "Ship", "url": f"http://loic.tail.ts.net:8787/ship/{rig.t['id']}"}]


def test_proposed_ping_gets_an_approve_action(endpoint, projects, tmp_path):
    write_ntfy_config(FakeNtfy().url, follow_ups=True)
    t = proposed()
    ntfy = FakeNtfy()
    write_ntfy_config(ntfy.url, follow_ups=True)
    notify.send(t, "proposed")
    labels = [a["label"] for a in ntfy.requests[0]["body"]["actions"]]
    assert labels == ["Approve"] and "click" not in ntfy.requests[0]["body"]
    ntfy.stop()


@pytest.mark.parametrize("event", ["ready", "urgent", "failed", "investigated", "oncall", "needs_human",
                                   "review_unrecorded", "shipped", "ship_failed"])
def test_every_ping_opens_the_card(projects, ntfy, event):
    t = mk()
    url = save_card(t["id"])
    notify.send(t, event, "x")
    body = ntfy.requests[0]["body"]
    assert body["click"] == url and body["actions"][0] == {"action": "view", "label": "Open card", "url": url}
    assert not any(a["label"] == "Ship" for a in body["actions"])


def test_no_card_means_no_click(projects, ntfy):
    t = mk()
    notify.send(t, "ready")
    assert "click" not in ntfy.requests[0]["body"] and "actions" not in ntfy.requests[0]["body"]


@pytest.mark.parametrize("event", ["failed", "oncall", "needs_human"])
def test_failure_ping_says_why(projects, ntfy, event, env):
    repo = make_repo(projects, "app")
    wt = repo / ".worktrees" / "demo"
    git(repo, "worktree", "add", "-q", "-b", "task/demo", str(wt))
    t = mk()
    claim(t)
    full = git(wt, "rev-parse", "HEAD").strip()
    (wt / ".reviews").mkdir()
    (wt / ".reviews" / f"task-demo-{full[:7]}-codex.md").write_text(
        f"Reviewer: codex gpt\nCommit: {full}\n\nVERDICT: FAIL\nMUST FIX:\n- 1. the lock is never released\n"
        "- second problem\nSHOULD FIX:\n- nothing\nSUMMARY: bad\n")
    tk.record_verdict("task/demo", "app", "FAIL", full, "codex gpt")
    tk.set_status(t["id"], "failed", allowed_from=("claimed",),
                  close_note="Failed: going in circles: 3 FAIL verdicts on different commits; "
                             "failed by: codex gpt; FAIL rounds: 3")
    notify.send(tk.load_ticket(t["id"]), event, "OC-1")
    message = ntfy.requests[0]["body"]["message"]
    assert "Reason: going in circles: 3 FAIL verdicts on different commits" in message
    assert "Reviewer: codex gpt" in message
    assert "FAIL rounds: 1" in message
    assert "MUST FIX: the lock is never released" in message
    assert "second problem" not in message


def test_failure_ping_withholds_a_line_with_a_secret(projects, ntfy, env, monkeypatch):
    t = mk()
    claim(t)
    leak = env / "gl-leak"
    leak.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n")
    leak.chmod(leak.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FLEET_GITLEAKS", str(leak))
    tk.set_status(t["id"], "failed", allowed_from=("claimed",), close_note="Failed: stalled")
    notify.send(tk.load_ticket(t["id"]), "failed")
    assert "Reason" not in ntfy.requests[0]["body"]["message"]


def test_shipped_ping_carries_the_result(projects, ntfy):
    t = mk()
    notify.send(t, "ship_failed", "no review of this exact commit; run scripts/review.sh")
    body = ntfy.requests[0]["body"]
    assert body["title"] == f"Ship refused: {t['id']}"
    assert "no review of this exact commit" in body["message"] and body["priority"] == 4


def test_web_paths_accept_any_case_of_the_ticket_id(endpoint, rig):
    reviewed(rig.wt, rig.repo)
    status, page = call(endpoint, f"/ship/{rig.t['id'].lower()}")
    assert status == 200 and rig.t["id"] in page


def test_web_action_word_stays_case_sensitive(endpoint, rig):
    status, _ = call(endpoint, f"/SHIP/{rig.t['id']}")
    assert status == 404


# --- audit log ---------------------------------------------------------------------

def audit_lines():
    path = Path(os.environ["FLEET_HOME"]) / "web" / "audit.log"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_audit_log_records_each_outcome_without_secrets(endpoint, rig, monkeypatch):
    as_node({**PHONE, "UserProfile": {"LoginName": "chris@example.com"}}, monkeypatch)
    full = reviewed(rig.wt, rig.repo)
    page = call(endpoint, f"/ship/{rig.t['id']}")[1]
    token = re.search(r"name=token value=([0-9a-f-]+)", page).group(1)
    assert audit_lines() == []  # opening the page is not an event
    call(endpoint, f"/ship/{rig.t['id']}", {"token": "forged-secret", "commit": full})
    status, _ = confirm(endpoint, f"/ship/{rig.t['id']}")
    assert status == 202
    lines = audit_lines()
    assert [x["result"] for x in lines] == ["refused: page out of date", "ship started"]
    assert all(x["peer"] == "127.0.0.1" and "phone" in x["who"] and "chris@example.com" in x["who"]
               and x["ticket"] == rig.t["id"] for x in lines)
    assert lines[1]["commit"] == full
    path = Path(os.environ["FLEET_HOME"]) / "web" / "audit.log"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert token not in text and "forged-secret" not in text


def test_audit_log_records_gate_refusals(endpoint, rig, monkeypatch):
    as_node(LOIC, monkeypatch)
    assert call(endpoint, f"/ship/{rig.t['id']}")[0] == 403
    (line,) = audit_lines()
    assert line["result"].startswith("refused:") and line["peer"] == "127.0.0.1"


def test_audit_write_failure_does_not_block_the_response(endpoint, rig, monkeypatch):
    full = reviewed(rig.wt, rig.repo)
    (Path(os.environ["FLEET_HOME"]) / "web").mkdir(parents=True, exist_ok=True)
    (Path(os.environ["FLEET_HOME"]) / "web" / "audit.log").mkdir()  # a directory: open fails
    status, _ = confirm(endpoint, f"/ship/{rig.t['id']}")
    assert status == 202


def test_audit_log_records_refused_get_and_never_the_submitted_commit(endpoint, rig):
    assert call(endpoint, "/ship/BD-99999")[0] == 404
    full = reviewed(rig.wt, rig.repo)
    call(endpoint, f"/ship/{rig.t['id']}", {"token": "x", "commit": "SECRET-IN-COMMIT"})
    lines = audit_lines()
    assert lines[0]["result"] == "refused: no such ticket" and lines[0]["action"] == "ship"
    assert lines[1]["commit"] == full
    assert "SECRET-IN-COMMIT" not in (Path(os.environ["FLEET_HOME"]) / "web" / "audit.log").read_text()
