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
    assert set(on) == {"push"}  # no pull_request / pull_request_target


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


def test_timeout_and_full_history():
    job = load()["jobs"]["check"]
    assert job["timeout-minutes"] >= 7
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False


def test_no_debug_or_env_dump():
    text = WORKFLOW.read_text()
    assert "set -x" not in text and "ACTIONS_STEP_DEBUG" not in text
    assert not re.search(r"^\s*(env|printenv)\s*$", text, re.M)


def test_main_runs_are_not_cancelled():
    assert load()["concurrency"]["cancel-in-progress"] is False


def test_k8s_yaml_requires_kubeconform_install():
    # check.sh fails (not skips) without kubeconform when k8s/ has manifests.
    if list((ROOT / "k8s").glob("**/*.y*ml")):
        assert "kubeconform" in WORKFLOW.read_text()
