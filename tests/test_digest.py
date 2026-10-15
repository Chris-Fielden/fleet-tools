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


def test_ticket_store_fills_in_and_is_deduplicated(monkeypatch):
    recent = stamp(-timedelta(hours=1))
    old = stamp(-timedelta(hours=50))
    tickets = [
        {"id": "BD-5", "type": "build", "status": "done", "updated_at": recent, "created_at": old},
        {"id": "BD-6", "type": "investigate", "status": "done", "updated_at": recent, "created_at": old},
        {"id": "BD-7", "type": "build", "status": "failed", "updated_at": recent, "created_at": old},
        {"id": "BD-8", "type": "fix", "status": "proposed", "updated_at": recent, "created_at": recent},
        {"id": "BD-9", "type": "build", "status": "done", "updated_at": old, "created_at": old},
        {"id": "BD-10", "type": "build", "status": "done", "updated_at": recent, "created_at": old,
         "notes": [{"at": recent, "by": "claude", "text": "late note on an old ticket"}]},
    ]
    monkeypatch.setattr(tk, "list_tickets", lambda: tickets)
    write_log(f"{recent} PING_SENT shipped BD-5")
    out = digest.render(NOW)
    assert out.count("BD-5") == 1 and "[ticket]" not in out.split("BD-5")[1].splitlines()[0]
    for ident in ("BD-6", "BD-7", "BD-8"):
        assert ident in out
    assert "BD-9" not in out and "BD-10" not in out
    assert "shipped (1)" in out and out.count("BD-6") == 1   # an investigation is not shipped
    assert "investigated (1)" in out and "failed (1)" in out and "proposed (1)" in out


def test_digest_is_read_only_and_wired_into_fleet(env):
    before = sorted(p.name for p in Path(tk.fleet_home()).glob("*")) if Path(tk.fleet_home()).exists() else []
    from fleet import usage_cli
    assert usage_cli.main(["digest"]) == 0
    after = sorted(p.name for p in Path(tk.fleet_home()).glob("*")) if Path(tk.fleet_home()).exists() else []
    assert before == after


def test_unreadable_ticket_store_keeps_log_events(monkeypatch):
    def broken():
        raise tk.TicketError("bad ticket file")
    monkeypatch.setattr(tk, "list_tickets", broken)
    write_log(f"{stamp(-timedelta(hours=1))} PING_SENT shipped BD-1")
    assert "BD-1" in digest.render(NOW)
