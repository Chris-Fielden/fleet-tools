"""EM-21: a blocked agent's dialog. Read-only; pane text is untrusted data.
Covers fleet/dialog.py, the watch pass (fake herdr pane, fake ntfy) and the doctor row."""
import json

import pytest

from fleet import dialog as dg
from fleet import dispatch as dp
from fleet import doctor
from fleet import notify
from fleet import review_watch as rw
from test_dispatch import herdr_log, projects  # noqa: F401
from test_review_watch import Rig, ntfy, rig, sent  # noqa: F401


def test_sanitize_strips_controls_keeps_last_lines_and_caps():
    out = dg.sanitize("\x1b[1mA\x1b[0m\x00\r\n\n\n" + "\n".join(f"l{i}" for i in range(20)) + "‮")
    assert out.split("\n") == [f"l{i}" for i in range(12, 20)]
    long = dg.sanitize("x" * 5000)
    assert len(long) == dg.KEEP_CHARS and long.startswith("…")


def test_classify_labels():
    assert dg.classify("") == "unreadable"
    assert dg.classify("TEACH AUTO MODE about  your environment?") == "auto_mode_setup"
    assert dg.classify("Allow this read outside the working directories?") == "read_outside_dirs"
    assert dg.classify("❯ 1. Yes\n  2) No") == "generic_menu"
    assert dg.classify("1. only one option") == "unknown"


def test_read_pane_refuses_odd_ids_and_survives_a_missing_herdr(monkeypatch):
    monkeypatch.setenv("FLEET_HERDR", "/nonexistent/herdr")
    assert dg.read_pane("w1:p1") == "" and dg.read_pane("--flag") == "" and dg.read_pane(None) == ""


def test_dialog_event_is_known_low_and_not_deprecated():
    assert "dialog" in notify.EVENTS and notify.PRIORITIES["dialog"] == "low"
    assert "dialog" not in notify.DEPRECATED_EVENTS


def test_doctor_row(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert doctor._claude_dialog_row()[0] == "warn"
    (tmp_path / "settings.json").write_text("{nope")
    assert doctor._claude_dialog_row()[0] == "warn"
    for body in ({}, {"skillOverrides": []}, {"skillOverrides": {"auto-mode-setup": "on"}}):
        (tmp_path / "settings.json").write_text(json.dumps(body))
        assert doctor._claude_dialog_row()[0] == "warn"
    (tmp_path / "settings.json").write_text(json.dumps({"skillOverrides": {"auto-mode-setup": "off"}}))
    assert doctor._claude_dialog_row()[0] == "ok"


# --- the watch pass ------------------------------------------------------------

def herdr_calls(herdr_log):
    return [json.loads(line) for line in herdr_log.read_text().splitlines()] if herdr_log.exists() else []


def assert_read_only(herdr_log, reads=1):
    calls = herdr_calls(herdr_log)
    assert sum(1 for c in calls if c[:2] == ["pane", "read"]) == reads
    banned = (["pane", "run"], ["pane", "send-keys"], ["pane", "send-text"],
              ["agent", "send-keys"], ["agent", "prompt"])
    assert not [c for c in calls if c[:2] in banned]


def dialog_lines():
    return [ln for ln in dp.log_path().read_text().splitlines() if " AGENT_DIALOG " in ln]


@pytest.mark.parametrize("pane,label", [
    ("Teach auto mode about your environment?\n 1. Yes\n 2. No", "auto_mode_setup"),
    ("Read outside the working directories\nAllow?", "read_outside_dirs"),
    ("  Allow this   read outside the working directories?\n ❯ 1. Yes, keep allowing\n 2. No", "read_outside_dirs"),
    ("Do you want to proceed?\n❯ 1. Yes\n  2) No", "generic_menu"),
    ("1. Yes\n2. No", "generic_menu"),
    ("just some words, no menu", "unknown"),
])
def test_blocked_agent_logs_the_dialog_once_and_pings_once(rig, ntfy, herdr_log, monkeypatch, pane, label):
    monkeypatch.setenv("FAKE_HERDR_PANE_TEXT", pane)
    rig.set_agent("blocked", pane_id="w1:p3", tab_id="w1:t3")
    rig.cycle(0)
    rig.cycle(5)
    rig.cycle(10)
    lines = dialog_lines()
    assert len(lines) == 1 and f"{rig.id} tab=w1:t3 {label}:" in lines[0]
    assert len(ntfy.requests) == 1
    title, message, header = sent(ntfy)[0]
    assert title == f"Agent dialog: {rig.id} ({label})" and header == "low"
    assert "w1:t3" in message and "\n" not in message
    assert rig.get()["status"] == "claimed"
    assert_read_only(herdr_log)


def test_ping_resets_when_unblocked_and_the_twenty_minute_rule_stays(rig, ntfy, herdr_log, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_PANE_TEXT", "Teach auto mode about your environment?")
    rig.set_agent("blocked", pane_id="w1:p3", tab_id="w1:t3")
    rig.cycle(0)
    rig.set_agent("working")
    rig.cycle(2)
    rig.set_agent("blocked", pane_id="w1:p3", tab_id="w1:t3")
    rig.cycle(3)
    assert len(dialog_lines()) == 2
    rig.cycle(24)
    assert rig.get()["status"] == "failed" and "blocked" in rig.get()["close_note"]


def test_unreadable_pane_still_logs_and_pings(rig, ntfy, herdr_log, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_PANE_FAIL", "1")
    rig.set_agent("blocked", pane_id="w1:p3", tab_id="w1:t3")
    rig.cycle(0)
    assert "unreadable" in dialog_lines()[0] and sent(ntfy)[0][0].endswith("(unreadable)")
    assert_read_only(herdr_log)


def test_agent_without_a_pane_id_reads_nothing(rig, ntfy, herdr_log):
    rig.set_agent("blocked")
    rig.cycle(0)
    assert "unreadable" in dialog_lines()[0] and len(ntfy.requests) == 1
    assert_read_only(herdr_log, reads=0)


def test_hostile_pane_text_is_stripped_capped_and_cannot_forge_a_log_line(rig, ntfy, herdr_log, monkeypatch):
    evil = ("\x1b[31mred\x1b[0m \x1b]0;title\x07bell\x07 bidi‮evil zw​x\r"
            "\n2099-01-01T00:00:00Z FAKE_KIND forged\n" + "A" * 5000 + "\nTeach auto mode about your environment?")
    monkeypatch.setenv("FAKE_HERDR_PANE_TEXT", evil)
    rig.set_agent("blocked", pane_id="w1:p3", tab_id="w1:t3")
    rig.cycle(0)
    log = dp.log_path().read_text()
    line = dialog_lines()[0]
    assert not any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or c in "‮​" for c in line)
    assert len(line) < 1000 and "auto_mode_setup" in line
    assert not [ln for ln in log.splitlines() if " FAKE_KIND " in ln.split("AGENT_DIALOG")[0]]
    assert len(ntfy.requests[0]["body"]["message"]) < 450


def test_a_secret_in_the_pane_never_reaches_the_phone(rig, ntfy, herdr_log, leaky_gitleaks, monkeypatch):
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    pane = f"aws_access_key_id = {key}\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n1. Yes\n2. No"
    monkeypatch.setenv("FAKE_HERDR_PANE_TEXT", pane)
    rig.set_agent("blocked", pane_id="w1:p3", tab_id="w1:t3")
    rig.cycle(0)
    assert len(ntfy.requests) == 1
    assert key not in json.dumps(ntfy.requests[0]["body"]) and ntfy.requests[0]["body"]["message"] == "tab w1:t3"


def test_old_state_files_without_dialog_pinged_still_load(rig):
    rw.state_path().write_text(json.dumps({"sent": [], "blocked_since": {}, "seen": {}}))
    assert rw._load_state(rig.warnings.append)["dialog_pinged"] == {} and rig.warnings == []
