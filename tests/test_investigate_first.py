"""Investigate before building in security-sensitive areas: a build or fix ticket
whose body names high-risk paths waits for a done investigation with a design."""

import pytest

from fleet import dispatch as dp
from fleet import risk as rk
from fleet import ticket as tk
from fleet.dispatch_cli import main as dispatch_main
from test_dispatch import calls, herdr_log, mk, projects, real_repo  # noqa: F401  (fixtures)

RISKY = "Stop guard_bash.py letting redirects through; edit .claude/hooks/guard_bash.py."


def only(kind):
    return [t for t in tk.list_tickets() if t["type"] == kind]


def finish(inv_id, *notes):
    """The investigation's agent writes notes and stops; the dispatcher marks it done or failed."""
    for n in notes:
        tk.add_note(inv_id, n)
    tk.finish_investigation(inv_id)


# --- risk.high_paths -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("edit .claude/hooks/guard_bash.py now", [".claude/hooks/guard_bash.py"]),
    ("see `scripts/review.sh`.", ["scripts/review.sh"]),
    ("/home/me/repo/.claude/settings.json", ["home/me/repo/.claude/settings.json"]),
    ("update CLAUDE.md, then k8s/app.yaml", ["CLAUDE.md", "k8s/app.yaml"]),
    ("the glob .claude/** and k8s/**", [".claude/**", "k8s/**"]),
    ("an author wrote authentic prose", []),          # plain words are not paths
    ("fix README.md and docs/x.md", []),
    ("", []),
])
def test_high_paths(text, expected):
    assert rk.high_paths(text, rk.load_rules()) == expected


# --- the gate --------------------------------------------------------------------

def test_risky_build_files_an_investigation_and_waits(projects, herdr_log, capsys):
    real_repo(projects)
    build = mk(branch="task/guard", title="Guard fix", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(build["id"])
    assert got["status"] == "open" and got["assignee"] is None
    (inv,) = only("investigate")
    assert got["parent"] == inv["id"]
    assert inv["status"] == "claimed" and inv["created_by"] == "human"   # filed open: launched, no approval
    assert RISKY in inv["body"] and "DESIGN" in inv["body"] and "FAILURE MODES" in inv["body"]
    assert inv["repo"] == build["repo"] and "Guard fix" in inv["title"]
    assert "filed investigation" in capsys.readouterr().out
    assert f"INVESTIGATION_FILED {inv['id']} for {build['id']}" in dp.log_path().read_text()
    assert not any(c[:2] == ["tab", "create"] and "task" in " ".join(c) for c in calls(herdr_log)
                   if build["id"] in " ".join(c))


def test_second_run_files_nothing_more(projects, herdr_log):
    real_repo(projects)
    mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    assert dispatch_main(["--once"]) == 0
    assert len(only("investigate")) == 1


def test_build_launches_after_a_done_investigation_with_a_design(projects, herdr_log):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    finish(inv["id"], "DESIGN: parse tokens; failure modes: quoting")
    assert tk.load_ticket(inv["id"])["status"] == "done"
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(build["id"])
    assert got["status"] == "claimed" and got["assignee"] == "claude"
    (note,) = got["notes"]
    assert note["text"].startswith(f"Findings from investigation {inv['id']}:")
    assert "DESIGN: parse tokens" in note["text"]


def test_findings_note_is_added_once(projects, herdr_log):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    finish(inv["id"], "the design is X")
    tickets = tk.list_tickets()
    assert len(dp.ensure_investigations(tickets)[0]) == 1
    assert dp.ensure_investigations(tk.list_tickets()) == ([], set())
    assert len(tk.load_ticket(build["id"])["notes"]) == 1


def test_done_investigation_without_a_design_blocks(projects, herdr_log, capsys):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    finish(inv["id"], "it is hard")
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "open"
    assert "has no findings note with a design" in capsys.readouterr().out
    assert len(only("investigate")) == 1


def test_failed_investigation_blocks_and_is_not_refiled(projects, herdr_log, capsys):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    finish(inv["id"])                                   # stops with no notes: failed
    assert tk.load_ticket(inv["id"])["status"] == "failed"
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "open"
    assert len(only("investigate")) == 1
    assert "a human must decide" in capsys.readouterr().out


def test_low_risk_build_launches_as_before(projects, herdr_log):
    real_repo(projects)
    build = mk(branch="task/docs", body="Fix the typo in docs/guide.md and README.md")
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "claimed"
    assert only("investigate") == []


def test_human_filed_investigation_with_a_design_satisfies_the_gate(projects, herdr_log):
    real_repo(projects)
    inv = mk(type="investigate", repo="ideas", branch="task/idea-1", title="idea", body="?")
    tk.approve_ticket(inv["id"])
    tk.add_note(inv["id"], "Design: do it carefully")
    tk.set_status(inv["id"], "claimed", allowed_from=("open",), assignee="claude")
    tk.finish_investigation(inv["id"])
    build = mk(branch="task/guard", body=RISKY, parent=inv["id"])
    tk.approve_ticket(build["id"])
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "claimed"
    assert len(only("investigate")) == 1


def test_fix_ticket_gets_its_investigation_in_a_field(projects, herdr_log):
    real_repo(projects)
    parent = mk(branch="task/orig")
    tk.set_status(parent["id"], "claimed", allowed_from=("open",), assignee="codex")
    tk.set_status(parent["id"], "done", allowed_from=("claimed",))   # frees the capacity
    fix = mk(type="fix", branch="task/fixit", parent=parent["id"], body=RISKY)
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(fix["id"])
    (inv,) = only("investigate")
    assert got["investigation"] == inv["id"] and got["parent"] == parent["id"]
    assert got["status"] == "open"
    finish(inv["id"], "design: small steps")
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(fix["id"])
    assert got["status"] == "claimed" and got["assignee"] == "codex"


def test_dry_run_says_why_and_files_nothing(projects, herdr_log, capsys):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--dry-run"]) == 0
    assert "an investigation comes first" in capsys.readouterr().out
    assert only("investigate") == []
    assert tk.load_ticket(build["id"])["parent"] is None


def test_unreadable_rules_fail_closed(projects, herdr_log, monkeypatch, env):
    real_repo(projects)
    build = mk(branch="task/docs", body="plain")
    monkeypatch.setenv("FLEET_RISK_RULES", str(env / "missing.json"))
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "open"


def test_long_spec_is_cut_to_fit_and_points_back():
    body = RISKY + " " + "x" * (tk.BODY_MAX - len(RISKY) - 1)
    t = {"id": "BD-9", "type": "build", "repo": "r", "body": body}
    out = dp._investigation_body(t)
    assert len(out) <= tk.BODY_MAX and "see BD-9" in out


def test_low_risk_build_with_an_unfinished_parent_investigation_does_not_crash(projects, herdr_log):
    real_repo(projects)
    inv = mk(type="investigate", repo="ideas", branch="task/idea-1", title="idea", body="?")
    build = mk(branch="task/plain", body="plain", parent=inv["id"])
    tk.approve_ticket(build["id"])
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(build["id"])
    assert got["status"] == "claimed" and "notes" not in got


def test_failed_findings_note_holds_the_build_back(projects, herdr_log, monkeypatch):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    finish(inv["id"], "design: steps")

    def boom(*a, **k):
        raise tk.TicketError("disk full")
    real = tk.add_note
    monkeypatch.setattr(tk, "add_note", boom)
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "open"
    monkeypatch.setattr(tk, "add_note", real)
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(build["id"])["status"] == "claimed"


def test_an_unattached_investigation_is_reused_not_refiled(projects, herdr_log, monkeypatch):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)

    def boom(*a, **k):
        raise tk.TicketError("lock lost")
    real = tk.attach_investigation
    monkeypatch.setattr(tk, "attach_investigation", boom)
    assert dispatch_main(["--once"]) == 0
    assert len(only("investigate")) == 1 and tk.load_ticket(build["id"])["parent"] is None
    monkeypatch.setattr(tk, "attach_investigation", real)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    assert tk.load_ticket(build["id"])["parent"] == inv["id"]


def _parts(sizes, design_last=True):
    notes = [{"text": ("line %d\n" % i) * (n // 8)} for i, n in enumerate(sizes)]
    if design_last:
        notes.append({"text": "the design: y"})
    return dp.findings_note({"id": "ID-1", "notes": notes})


def test_findings_note_1kb_is_one_part():
    parts = _parts([1000])
    assert len(parts) == 1 and parts[0].startswith("Findings from investigation ID-1: part 1/1\n")


def test_findings_note_3kb_and_9kb_split_without_loss():
    for size, want in ((3000, 1), (9000, 3)):
        parts = _parts([size, size // 3, size // 3])
        assert all(len(p) <= tk.NOTE_MAX for p in parts)
        assert len(parts) >= want
        n = len(parts)
        for i, p in enumerate(parts, 1):
            assert p.startswith(f"Findings from investigation ID-1: part {i}/{n}\n")
        assert "the design: y" in parts[0]
        body = "\n".join(parts)
        assert body.count("line 0") == size // 8 and body.count("line 1") == size // 24


def test_findings_note_splits_one_overlong_note_at_line_ends():
    parts = dp.findings_note({"id": "ID-1", "notes": [{"text": "\n".join(f"row {i:04d}" for i in range(1500))}]})
    assert len(parts) > 1 and all(len(p) <= tk.NOTE_MAX for p in parts)
    rows = [r for p in parts for r in p.split("\n")[1:] if r]
    assert rows == [f"row {i:04d}" for i in range(1500)]


def test_findings_parts_appended_once_and_midway_failure_completed(projects, herdr_log, monkeypatch):
    real_repo(projects)
    build = mk(branch="task/guard", body=RISKY)
    assert dispatch_main(["--once"]) == 0
    (inv,) = only("investigate")
    tk.add_note(inv["id"], "x" * 3500)
    tk.add_note(inv["id"], "y" * 3500)
    finish(inv["id"], "the design: steps")
    real = tk.add_note
    calls = []

    def flaky(tid, text, *a, **k):
        if tid == build["id"]:
            calls.append(1)
            if len(calls) == 2:
                raise tk.TicketError("disk full")
        return real(tid, text, *a, **k)
    monkeypatch.setattr(tk, "add_note", flaky)
    lines, held = dp.ensure_investigations(tk.list_tickets())
    assert held == {build["id"]}
    assert len(tk.load_ticket(build["id"])["notes"]) == 1
    monkeypatch.setattr(tk, "add_note", real)
    lines, held = dp.ensure_investigations(tk.list_tickets())
    got = tk.load_ticket(build["id"])["notes"]
    assert held == set() and len(got) >= 2
    assert [n["text"].split("\n")[0].rsplit(" ", 1)[1] for n in got] == [f"{i}/{len(got)}" for i in range(1, len(got) + 1)]
    assert dp.ensure_investigations(tk.list_tickets()) == ([], set())
    assert len(tk.load_ticket(build["id"])["notes"]) == len(got)
