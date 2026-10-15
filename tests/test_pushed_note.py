"""BD-88: the merge script only claims ArgoCD will pick up a push when the repo has k8s/."""
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "finish-task.sh"


def say(tmp_path):
    text = SCRIPT.read_text()
    fn = re.search(r"^pushed_note\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
    out = subprocess.run(["bash", "-c", fn + "pushed_note main"], cwd=tmp_path,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_plain_pushed_without_k8s(tmp_path):
    assert say(tmp_path) == "Pushed."


def test_argocd_message_with_k8s(tmp_path):
    (tmp_path / "k8s").mkdir()
    assert say(tmp_path) == "Pushed. ArgoCD will pick up the change from main."


def test_script_uses_the_conditional_note():
    text = SCRIPT.read_text()
    assert 'pushed_note "$base"' in text
    assert text.count("ArgoCD will pick up") == 1
