"""dispatch --watch reloads settings every cycle; daily caps reset at local midnight
(temporary FLEET_HOME, fake clock)."""
import json
from datetime import datetime, timezone

import pytest

from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import notify
from fleet import ticket as tk

from test_dispatch import herdr_log, mk, projects, real_repo  # noqa: F401
from test_watch import FakeClock


def write_settings(env, projects, **over):
    (env / "fleet" / "settings.json").write_text(json.dumps({"projects_dir": str(projects), **over}))


def run_watch(clock, reload=None):
    settings = dp.load_settings()
    return settings, dispatch_cli._cmd_watch(settings, clock, reload=reload or dp.load_settings)


def log_text():
    return dp.log_path().read_text(encoding="utf-8") if dp.log_path().exists() else ""


def test_a_changed_max_running_takes_effect_next_cycle_without_a_restart(projects, herdr_log, env):
    real_repo(projects)
    first = mk(branch="task/one")
    second = mk(branch="task/two")
    write_settings(env, projects, max_running=1)

    def raise_limit(clock):
        write_settings(env, projects, max_running=2)
    settings, code = run_watch(FakeClock(3, at={1: raise_limit}))
    assert code == 0
    assert tk.load_ticket(first["id"])["status"] == "claimed"
    assert tk.load_ticket(second["id"])["status"] == "claimed"
    assert settings["max_running"] == 2
    assert "SETTINGS_RELOADED changed: max_running" in log_text()


def test_an_unchanged_file_logs_nothing(projects, herdr_log, env):
    write_settings(env, projects)
    run_watch(FakeClock(3))
    assert "SETTINGS_RELOADED" not in log_text()


def test_an_invalid_file_keeps_the_old_values_and_pings_once(projects, herdr_log, env, monkeypatch):
    pings = []
    monkeypatch.setattr(notify, "send_alert", lambda *a: pings.append(a) or True)
    write_settings(env, projects, max_running=3)

    def break_it(clock):
        (env / "fleet" / "settings.json").write_text("{not json")
    settings = dp.load_settings()
    assert settings["max_running"] == 3
    code = dispatch_cli._cmd_watch(settings, FakeClock(4, at={1: break_it}), reload=dp.load_settings)
    assert code == 0
    assert settings["max_running"] == 3                  # the last good value
    assert log_text().count("SETTINGS_INVALID") == 1     # three bad cycles, one log line
    assert [p[0] for p in pings] == ["settings_invalid"]


def test_a_bad_value_is_invalid_too_and_a_fixed_file_is_picked_up_again(projects, herdr_log, env, monkeypatch):
    pings = []
    monkeypatch.setattr(notify, "send_alert", lambda *a: pings.append(a) or True)
    write_settings(env, projects, max_running=2)

    def bad(clock):
        write_settings(env, projects, max_running=0)

    def fixed(clock):
        write_settings(env, projects, max_running=4)
    settings = dp.load_settings()
    dispatch_cli._cmd_watch(settings, FakeClock(5, at={1: bad, 3: fixed}), reload=dp.load_settings)
    assert settings["max_running"] == 4
    assert log_text().count("SETTINGS_INVALID") == 1 and "SETTINGS_RELOADED changed: max_running" in log_text()
    assert len(pings) == 1


def test_the_ping_priority_is_high(env, monkeypatch):
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "notify.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "topic": "t"}))
    (env / "fleet" / "notify.json").chmod(0o600)
    got = {}
    monkeypatch.setenv("FLEET_HOME", str(env / "fleet"))
    monkeypatch.setattr(notify, "_post", lambda c, m, p: got.update(p=p) or True)
    assert notify.send_alert("settings_invalid", "t", "x") and got["p"] == "high"


def test_a_missing_ping_config_does_not_stop_the_watch(projects, herdr_log, env):
    write_settings(env, projects)

    def break_it(clock):
        (env / "fleet" / "settings.json").write_text("[]")
    settings = dp.load_settings()
    assert dispatch_cli._cmd_watch(settings, FakeClock(3, at={1: break_it}), reload=dp.load_settings) == 0
    assert "SETTINGS_INVALID" in log_text()


def test_timezone_setting_is_validated(projects, env):
    write_settings(env, projects, timezone="Mars/Olympus")
    with pytest.raises(dp.SettingsError, match="timezone"):
        dp.load_settings()
    write_settings(env, projects)
    assert dp.load_settings()["timezone"] == "America/Los_Angeles"


# --- local midnight, across a daylight-saving change ---------------------------

def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_local_day_across_the_daylight_saving_change():
    # Midnight before the change is 07:00 UTC, midnight after it is 08:00 UTC.
    assert dp.local_day(utc(2026, 10, 31, 6, 59)) == "2026-10-30"
    assert dp.local_day(utc(2026, 10, 31, 7, 0)) == "2026-10-31"
    assert dp.local_day(utc(2026, 11, 1, 6, 59)) == "2026-10-31"
    assert dp.local_day(utc(2026, 11, 1, 7, 0)) == "2026-11-01"   # still PDT: the day is 25 hours long
    assert dp.local_day(utc(2026, 11, 2, 7, 59)) == "2026-11-01"
    assert dp.local_day(utc(2026, 11, 2, 8, 0)) == "2026-11-02"   # PST: midnight is now 08:00 UTC
    assert dp.local_day(utc(2026, 11, 2, 0, 0), "UTC") == "2026-11-02"


def test_the_launch_cap_resets_at_local_midnight_not_utc_midnight(projects, herdr_log, env):
    real_repo(projects)
    mk(branch="task/one")
    mk(branch="task/two")
    write_settings(env, projects, max_launches_per_day=1, max_running=5)
    # 07:40 UTC on Nov 2 is still Nov 1 in Los Angeles (now PST: local midnight is 08:00 UTC).
    run_watch(FakeClock(2, start=utc(2026, 11, 2, 7, 40)))
    assert dp.launches_today("2026-11-01") == 1
    # 08:05 UTC: past UTC midnight long ago, but only now past local midnight.
    run_watch(FakeClock(2, start=utc(2026, 11, 2, 8, 5)))
    assert dp.launches_today("2026-11-02") == 1


def test_the_launch_cap_holds_between_utc_midnight_and_local_midnight(projects, herdr_log, env):
    real_repo(projects)
    one = mk(branch="task/one")
    two = mk(branch="task/two")
    write_settings(env, projects, max_launches_per_day=1, max_running=5)
    run_watch(FakeClock(1, start=utc(2026, 11, 1, 20, 0)))         # Nov 1, 13:00 PDT
    run_watch(FakeClock(2, start=utc(2026, 11, 2, 2, 0)))          # past UTC midnight: same local day
    assert tk.load_ticket(one["id"])["status"] == "claimed"
    assert tk.load_ticket(two["id"])["status"] == "open"


def test_the_timezone_setting_moves_the_reset(projects, herdr_log, env):
    real_repo(projects)
    mk(branch="task/one")
    two = mk(branch="task/two")
    write_settings(env, projects, max_launches_per_day=1, max_running=5, timezone="UTC")
    run_watch(FakeClock(1, start=utc(2026, 11, 1, 23, 0)))
    run_watch(FakeClock(2, start=utc(2026, 11, 2, 0, 30)))         # UTC midnight passed: a new day
    assert tk.load_ticket(two["id"])["status"] == "claimed"


def test_the_oncall_cap_counts_the_local_day():
    # Filed 07:30 UTC on Nov 2 = 23:30 on Nov 1 in Los Angeles (PST: midnight is 08:00 UTC).
    late = {"oncall": True, "created_at": "2026-11-02T07:30:00Z"}
    early = {"oncall": True, "created_at": "2026-11-02T08:30:00Z"}
    other = {"created_at": "2026-11-02T08:30:00Z"}
    tickets = [late, early, other]
    assert dp.oncall_today(tickets, utc(2026, 11, 2, 7, 45)) == 1    # Nov 1 locally: only `late`
    assert dp.oncall_today(tickets, utc(2026, 11, 2, 9, 0)) == 1     # Nov 2 locally: only `early`
    assert dp.oncall_today(tickets, utc(2026, 11, 2, 9, 0), "UTC") == 2
    # Before the change (PDT: midnight is 07:00 UTC).
    assert dp.oncall_today([{"oncall": True, "created_at": "2026-10-31T06:30:00Z"}], utc(2026, 10, 31, 5, 0)) == 1
    assert dp.oncall_today([{"oncall": True, "created_at": "2026-10-31T06:30:00Z"}], utc(2026, 10, 31, 8, 0)) == 0


def test_a_leftover_cursor_review_setting_is_ignored_with_one_warning(projects, env, capsys):
    write_settings(env, projects, cursor_review=False)
    settings = dp.load_settings()
    assert "cursor_review" not in settings
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "cursor_review" in err[0] and "delete" in err[0]
