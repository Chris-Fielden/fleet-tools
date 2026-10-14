"""The GitHub workflow that runs scripts/check.sh must parse and stay safe."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_parses_and_triggers():
    on = load().get("on", load().get(True))  # YAML 1.1 reads `on` as True
    assert on["push"]["branches"] == ["main"]
    assert "pull_request" in on


def test_runs_check_script():
    steps = load()["jobs"]["check"]["steps"]
    assert any("scripts/check.sh" in s.get("run", "") for s in steps)


def test_actions_pinned_to_full_sha():
    for step in load()["jobs"]["check"]["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_no_secrets_and_read_only():
    assert "secrets." not in WORKFLOW.read_text()
    assert load()["permissions"] == {"contents": "read"}
