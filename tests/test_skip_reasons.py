"""BD-83: a DECISION line never says `skip: None`; every skip has a real reason."""
from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import ticket as tk
from test_dispatch import herdr_log, make_repo, mk, projects  # noqa: F401  (fixtures)
from test_investigate import idea as investigation


def log_text():
    path = dp.log_path()
    return path.read_text() if path.exists() else ""


def decision_lines():
    return [x for x in log_text().splitlines() if "DECISION" in x]


def run(reserve=None):
    lines = []
    code = dispatch_cli._cycle(dp.load_settings(), lines.append, lambda m: None, reserve=reserve)
    return code, lines


def test_refused_investigation_logs_the_limit_not_none(projects, herdr_log):
    t = investigation()
    _, lines = run(reserve=lambda: False)
    ours = [x for x in decision_lines() if t["id"] in x]
    assert len(ours) == 1 and "daily launch limit reached" in ours[0]
    assert not any("None" in x for x in decision_lines())
    assert not any("would launch" in x for x in lines)
    assert tk.load_ticket(t["id"])["status"] == "proposed"


def test_refused_build_logs_the_limit_not_none(projects, herdr_log):
    make_repo(projects, "app")
    t = mk()
    _, lines = run(reserve=lambda: False)
    ours = [x for x in decision_lines() if t["id"] in x]
    assert len(ours) == 1 and "daily launch limit reached" in ours[0]
    assert any("daily launch limit reached" in x for x in lines)
    assert not any("None" in x for x in decision_lines())


def test_mixed_queue_never_logs_none(projects, herdr_log):
    first = investigation()
    make_repo(projects, "app")
    build = mk(branch="task/other")
    run(reserve=lambda: False)
    for t in (first, build):   # each refused ticket gets exactly one line
        assert len([x for x in decision_lines() if t["id"] in x]) == 1
    assert not any("None" in x for x in decision_lines())


def test_launched_ticket_logs_no_skip(projects, herdr_log):
    t = investigation()
    run(reserve=lambda: True)
    assert not any(t["id"] in x for x in decision_lines())


def test_log_skip_replaces_a_missing_or_empty_reason(projects):
    for empty in (None, ""):
        d = dp.Decision(ticket={"id": "X-1"}, skip=empty)
        assert d.eligible
        assert d.log_skip("because") == "because" and not d.eligible
    assert "X-1 skip: because" in log_text()
