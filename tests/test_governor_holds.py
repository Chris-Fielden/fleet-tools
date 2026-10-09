"""EM-20 review fixes: the governor's backstop, held work, and retries."""
import json
from datetime import timedelta

from fleet import governor
from fleet import launch as lc
from fleet import ticket as tk
from test_dispatch import calls, herdr_log, projects  # noqa: F401
from test_governor import cycle, usage
from test_review_watch import Rig, ntfy, sent  # noqa: F401


def test_the_backstop_uses_the_settings_in_force(projects, env):
    usage(92)
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "settings.json").write_text(json.dumps({
        "projects_dir": str(projects), "pause_weekly_pct": 0, "hard_stop_weekly_pct": 0}))
    assert governor.refusal() is None   # both limits off: nothing is refused at 92%


def test_held_work_is_left_alone_while_stopped(projects, herdr_log, ntfy, env, monkeypatch):
    rig = Rig(projects, env, monkeypatch)
    usage(96, resets_in=timedelta(hours=3))
    cycle(rig.settings)
    assert governor.held_ids() == {rig.id}
    tk.add_note(rig.id, "please also do X")
    cycle(rig.settings)
    sent_prompts = [c for c in calls(herdr_log) if c[:2] == ["agent", "prompt"]]
    assert sent_prompts == []   # a note is not pushed to the stopped agent
    state = governor.load_state()
    state["held_jobs"] = ["prep"]
    governor.save_state(state)
    assert governor.held_jobs() == {"prep"}


def test_a_failed_interrupt_is_retried_and_a_lost_ping_is_resent(projects, herdr_log, ntfy, env, monkeypatch):
    rig = Rig(projects, env, monkeypatch)
    usage(96, resets_in=timedelta(hours=3))
    real = lc.herdr_run
    failed = []

    def flaky(*args, **kw):
        if args[:2] == ("agent", "send-keys") and not failed:
            failed.append(1)
            raise lc.LaunchError("herdr hiccup")
        return real(*args, **kw)
    monkeypatch.setattr(lc, "herdr_run", flaky)
    cycle(rig.settings)
    assert not governor.load_state()["stopped"] and [p for _, _, p in sent(ntfy)] == ["low"]
    cycle(rig.settings)
    assert governor.load_state()["stopped"]
    assert [p for _, _, p in sent(ntfy)] == ["low", "high"]


def test_agents_that_are_not_the_fleets_are_left_alone(projects, herdr_log, ntfy, env, monkeypatch):
    rig = Rig(projects, env, monkeypatch)
    world = json.loads(rig.world.read_text())
    world["agents"].append({"name": "my-own-session", "agent_status": "working"})
    rig.world.write_text(json.dumps(world))
    usage(96, resets_in=timedelta(hours=3))
    cycle(rig.settings)
    keys = [c[2] for c in calls(herdr_log) if c[:2] == ["agent", "send-keys"]]
    assert keys == [rig.id.lower()]
    assert governor.load_state()["held_jobs"] == []
