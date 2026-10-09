"""BD-68: the supervisor's model call, classify only. A fake claude, real git, a temporary FLEET_HOME."""
import json
import os
import stat
import sys
import time

import pytest

from fleet import dispatch as dp
from fleet import supervisor as sv
from fleet import ticket as tk
from test_dispatch import claim, git, make_repo, mk, projects  # noqa: F401

FAKE = """#!{python}
import json, os, sys
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read()}}) + "\\n")
open({pidfile!r}, "w").write(str(os.getpid()))
mode = open({mode!r}).read().strip()
if mode == "hang":
    import time; time.sleep(60)
if mode == "fail":
    sys.exit(3)
print(json.dumps({{"result": open({reply!r}).read(), "usage": {{"input_tokens": 1200, "output_tokens": 80}}}}))
"""

PROGRESS = "CLASSIFICATION: PROGRESS\nREASON: different files each round\n"
LOOP = ("CLASSIFICATION: LOOP\nREASON: same locking flaw\n"
        "DESIGN CHANGE: the retry logic is spread over three callers; move it into one function.\n")


@pytest.fixture
def model(env, monkeypatch):
    script = env / "fake-claude"
    files = {k: env / f"claude.{k}" for k in ("log", "reply", "mode", "pid")}
    files["mode"].write_text("ok")
    files["reply"].write_text(PROGRESS)
    script.write_text(FAKE.format(python=sys.executable, log=str(files["log"]), reply=str(files["reply"]),
                                  mode=str(files["mode"]), pidfile=str(files["pid"])))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FLEET_CLAUDE", str(script))

    class M:
        def say(self, text):
            files["reply"].write_text(text)

        def mode(self, m):
            files["mode"].write_text(m)

        def calls(self):
            return [json.loads(x) for x in files["log"].read_text().splitlines()] if files["log"].exists() else []

        pid = files["pid"]
    return M()


@pytest.fixture
def branch(projects):
    repo = make_repo(projects)
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "task/demo")
    (repo / "test_x.py").write_text("def test_alpha():\n    pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add alpha")
    return repo, base, git(repo, "rev-parse", "HEAD").strip()


def ticket(**over):
    t = mk(**over)
    claim(t)
    return tk.load_ticket(t["id"])


def test_flags_and_scrubbed_environment(model, monkeypatch, env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("FLEET_GITLEAKS", "x")
    text, usage = sv.run_model("hello prompt", env)
    assert text == PROGRESS and usage["output_tokens"] == 80
    (call,) = model.calls()
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert "hello prompt" not in argv and call["stdin"] == "hello prompt"   # prompt never in argv
    assert "--mcp-config" not in argv
    for var in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "FLEET_GITLEAKS", "FLEET_HOME", "FLEET_CLAUDE"):
        assert var not in call["env"]
    assert call["env"]["AGENT_RULES_OFF"] == "1" and call["env"]["HOME"] == os.environ["HOME"]


def test_prompt_size_cap(model, env):
    with pytest.raises(sv.SupervisorError, match="cap"):
        sv.run_model("x" * (sv.PROMPT_MAX + 1), env)
    assert model.calls() == []


def test_build_prompt_is_capped_and_keeps_the_newest(branch):
    repo, base, tip = branch
    t = ticket(body="B" * 3900)
    big = "z" * 100000
    history = [{"commit": f"{i:040x}", "reviews": {"codex": {"reviewer": "Codex", "verdict": "FAIL",
                                                              "must_fix": big}}} for i in range(40)]
    for i in range(60):
        t.setdefault("notes", []).append({"at": "t", "by": "agent", "text": "n" * 4000})
    prompt = sv.build_prompt(t, repo, base, tip, history)
    assert len(prompt) <= sv.PROMPT_MAX
    assert prompt.endswith("=== END OF DATA ===\n")
    assert "round 40" in prompt and "test_x.py::test_alpha" in prompt and "add alpha" in prompt


def test_build_prompt_without_a_verdict(branch):
    repo, base, tip = branch
    prompt = sv.build_prompt(ticket(), repo, base, tip, [])
    assert "its Codex review is not run" in " ".join(prompt.split())


def test_timeout_kills_the_process_group(model, env, monkeypatch):
    model.mode("hang")
    monkeypatch.setattr(sv, "MODEL_TIMEOUT", 1)
    with pytest.raises(sv.SupervisorError, match="longer than"):
        sv.run_model("p", env)
    pid = int(model.pid.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the model process is still running")


def test_nonzero_exit_and_missing_binary(model, env, monkeypatch):
    model.mode("fail")
    with pytest.raises(sv.SupervisorError, match="exited 3"):
        sv.run_model("p", env)
    monkeypatch.setenv("FLEET_CLAUDE", str(env / "nope"))
    with pytest.raises(sv.SupervisorError, match="could not be run"):
        sv.run_model("p", env)


def test_parse_reply_classes():
    assert sv.parse_reply(PROGRESS)["classification"] == "PROGRESS"
    r = sv.parse_reply("**CLASSIFICATION:** squirrels\n**REASON:** only nits\n")
    assert r["classification"] == "SQUIRRELS" and r["reason"] == "only nits" and r["design"] == ""


def test_parse_reply_keeps_a_design_line_starting_with_reason():
    text = LOOP + "Reason: callers each retry on their own, so the flaw returns.\nCLASSIFICATION: see above\n"
    r = sv.parse_reply(text)
    assert r["classification"] == "LOOP"
    assert "Reason: callers each retry" in r["design"] and "CLASSIFICATION: see above" in r["design"]
    assert r["reason"] == "same locking flaw"


@pytest.mark.parametrize("text", [
    "", "no format at all", "CLASSIFICATION: MAYBE\nREASON: x\n", "CLASSIFICATION: PROGRESS\n",
    "CLASSIFICATION: PROGRESS\nREASON: a\nCLASSIFICATION: LOOP\n",
    "CLASSIFICATION: PROGRESS\nREASON: a\nREASON: b\n",
    "CLASSIFICATION: LOOP\nREASON: x\n", "CLASSIFICATION: LOOP\nREASON: x\nDESIGN CHANGE: short\n",
])
def test_parse_reply_rejects(text):
    with pytest.raises(sv.SupervisorError):
        sv.parse_reply(text)


def test_classify_writes_a_note_logs_and_does_nothing_else(model, branch, env):
    repo, base, tip = branch
    t = ticket()
    before = tk.load_ticket(t["id"])
    model.say(LOOP)
    reply = sv.classify(t["id"], repo, base, tip, env / "no-task-folder", cwd=repo)
    assert reply["classification"] == "LOOP"
    after = tk.load_ticket(t["id"])
    assert len(after["notes"]) == len(before.get("notes", [])) + 1
    note = after["notes"][-1]["text"]
    assert note.startswith("SUPERVISOR LOOP (commit ") and "DESIGN CHANGE:" in note
    assert {k: v for k, v in after.items() if k not in ("notes", "updated_at")} == \
           {k: v for k, v in before.items() if k not in ("notes", "updated_at")}
    line = [x for x in dp.log_path().read_text().splitlines() if " SUPERVISOR " in x]
    assert len(line) == 1 and f"{t['id']} {tip[:8]} LOOP tokens_in=1200 tokens_out=80" in line[0]
    assert "locking" not in line[0]
    (call,) = model.calls()
    assert t["id"] in call["stdin"] and "test_alpha" in call["stdin"]


def test_classify_bad_reply_saves_nothing(model, branch, env):
    repo, base, tip = branch
    t = ticket()
    model.say("I think it is going fine")
    with pytest.raises(sv.SupervisorError):
        sv.classify(t["id"], repo, base, tip, env / "x", cwd=repo)
    assert not tk.load_ticket(t["id"]).get("notes")
