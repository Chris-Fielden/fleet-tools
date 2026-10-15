import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet import digest
from fleet import ticket as tk

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def stamp(delta):
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_log(*lines):
    home = Path(tk.fleet_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "dispatch.log").write_text("\n".join(lines) + "\n")


def test_empty_home_says_nothing_happened():
    assert digest.render(NOW) == "Nothing informational in the last 24 hours."


def test_window_and_event_filter():
    write_log(
        f"{stamp(-timedelta(hours=1))} PING_SENT shipped BD-1",
        f"{stamp(-timedelta(hours=30))} PING_SENT shipped BD-2",      # too old
        f"{stamp(-timedelta(hours=2))} PING_SENT ready BD-3",          # not informational
        f"{stamp(-timedelta(hours=3))} PING_FAILED oncall BD-4: notifications are off (no notify.json)",
        f"{stamp(-timedelta(hours=4))} PING_SENT usage_pause fleet",
        "garbage line",
    )
    out = digest.render(NOW)
    assert "BD-1" in out and "BD-4" in out and "fleet" in out
    assert "BD-2" not in out and "BD-3" not in out
    assert "[not sent]" in out


def test_digest_is_read_only_and_wired_into_fleet(env):
    before = sorted(p.name for p in Path(tk.fleet_home()).glob("*")) if Path(tk.fleet_home()).exists() else []
    from fleet import usage_cli
    assert usage_cli.main(["digest"]) == 0
    after = sorted(p.name for p in Path(tk.fleet_home()).glob("*")) if Path(tk.fleet_home()).exists() else []
    assert before == after


def _make(ttype, **over):
    fields = {"type": ttype, "repo": "fleet-tools", "branch": "task/demo",
              "title": "Do a thing", "body": "Details."}
    fields.update(over)
    t = tk.create_ticket(fields)
    return tk.approve_ticket(t["id"]) if t["status"] == "proposed" else t


def _kinds(now=None, skip=("proposed",)):
    """(kind, id) of every event in the window, without the proposal each ticket began with."""
    now = now or datetime.now(timezone.utc) + timedelta(minutes=1)
    return sorted((e[1], e[2]) for e in digest.collect(now) if e[1] not in skip)


def test_closed_build_and_fix_are_shipped():
    b = _make("build")
    f = _make("fix", parent=b["id"], branch="task/fixit")
    tk.close_ticket(b["id"], "done")
    tk.close_ticket(f["id"], "done")
    assert _kinds() == sorted([("shipped", b["id"]), ("shipped", f["id"])])
    rec = [r for r in tk.read_transitions() if r["ticket"] == b["id"]]
    assert len(rec) == 1 and rec[0]["kind"] == "shipped" and "at" in rec[0] and "commit" in rec[0]


def test_note_after_completion_keeps_the_event():
    t = _make("build")
    tk.close_ticket(t["id"], "done")
    tk.add_note(t["id"], "a remark after it shipped")
    assert _kinds() == [("shipped", t["id"])]


def test_closed_question_and_review_report_nothing():
    b = _make("build")
    q = _make("question")
    r = _make("review", branch="task/other", parent=b["id"], commit="a" * 40)
    tk.close_ticket(q["id"], "done")
    tk.close_ticket(r["id"], "done")
    assert _kinds() == []
    assert "shipped" not in digest.render(datetime.now(timezone.utc) + timedelta(minutes=1))


def test_investigation_is_investigated_not_shipped():
    t = _make("investigate")
    tk.set_status(t["id"], "claimed", allowed_from=("open",))
    tk.add_note(t["id"], "findings")
    tk.finish_investigation(t["id"])
    assert _kinds() == [("investigated", t["id"])]
    assert "shipped" not in digest.render(datetime.now(timezone.utc) + timedelta(minutes=1))


def test_failed_and_proposed_are_recorded_and_cancelled_is_not():
    p = tk.create_ticket({"type": "build", "repo": "fleet-tools", "branch": "task/p",
                          "title": "P", "body": "x"}, needs_approval=True)
    f, c = _make("build", branch="task/f"), _make("build", branch="task/c")
    tk.close_ticket(f["id"], "failed")
    tk.close_ticket(c["id"], "cancelled")
    assert _kinds(skip=()) == sorted([("proposed", p["id"]), ("failed", f["id"])])


def test_old_record_and_updated_at_are_ignored():
    t = _make("build")   # open, never shipped; updated_at is now
    write_transitions({"ticket": "BD-9", "kind": "shipped", "at": stamp(-timedelta(hours=30))},
                      {"ticket": "BD-8", "kind": "shipped", "at": stamp(-timedelta(hours=2))})
    got = _kinds(NOW)
    assert got == [("shipped", "BD-8")] and t["id"] not in str(got)


def test_damaged_transition_line_is_skipped():
    home = Path(tk.fleet_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "transitions.jsonl").write_text(
        "not json\n" + json.dumps({"ticket": "BD-1", "kind": "shipped", "at": stamp(-timedelta(hours=1))}) + "\n")
    assert _kinds(NOW) == [("shipped", "BD-1")]


def test_ping_line_and_transition_for_same_event_count_once():
    recent = stamp(-timedelta(hours=1))
    write_log(f"{recent} PING_SENT shipped BD-5")
    write_transitions({"ticket": "BD-5", "kind": "shipped", "at": recent})
    assert _kinds(NOW) == [("shipped", "BD-5")]


def write_transitions(*recs):
    home = Path(tk.fleet_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "transitions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_repeated_set_status_records_one_event():
    t = _make("build")
    tk.set_status(t["id"], "claimed", allowed_from=("open",))
    tk.set_status(t["id"], "done", allowed_from=("claimed", "done"))
    tk.set_status(t["id"], "done", allowed_from=("claimed", "done"))
    assert [r["kind"] for r in tk.read_transitions() if r["ticket"] == t["id"]
            and r["kind"] == "shipped"] == ["shipped"]


def test_log_write_failure_does_not_fail_the_status_change(monkeypatch, capsys):
    t = _make("build")
    real_open = open

    def failing_open(path, *a, **kw):
        if str(path).endswith("transitions.jsonl"):
            raise OSError("disk full")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", failing_open)
    closed = tk.close_ticket(t["id"], "done")
    assert closed["status"] == "done"
    assert "could not record" in capsys.readouterr().err
