"""CI's change filter (.github/workflows/ci.yml, job `changes`): which jobs a
pull request runs. The step's own script is run here on a scratch git
repository, as CI runs it for a pull request (#595)."""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def paths_script():
    """The run: block of the step with id paths, dedented."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "- id: paths")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def clean_env(**extra):
    """No GIT_* from the caller (a hook's GIT_DIR would reach the real
    repository) and none of its git config (commit.gpgsign would fail it)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return dict(env, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", **extra)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class ChangeFilterTests(unittest.TestCase):
    def outputs(self, changed):
        """The jobs a pull request that changes only these files runs."""
        with tempfile.TemporaryDirectory() as repo:
            git = lambda *args: subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=clean_env())
            git("init", "-q")
            git("config", "user.email", "ci@example.com")
            git("config", "user.name", "CI")
            (Path(repo) / ".github" / "workflows").mkdir(parents=True)
            shutil.copy(WORKFLOW, Path(repo) / ".github" / "workflows" / "ci.yml")
            for name in changed:
                (Path(repo) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(repo) / name).write_text("before\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            for name in changed:
                (Path(repo) / name).write_text("after\n")
            git("commit", "-qam", "change")
            out = Path(repo) / "github-output"
            env = clean_env(EVENT="pull_request", BEFORE="", GITHUB_OUTPUT=str(out))
            subprocess.run(["bash", "-e", "-c", paths_script()], cwd=repo, env=env, check=True, capture_output=True)
            return dict(line.split("=", 1) for line in out.read_text().split())

    def test_a_change_to_the_web_app_runs_the_browser_checks(self):
        self.assertEqual("true", self.outputs(["web/js/core.js"])["ui"])

    def test_a_change_to_only_a_browser_check_runs_the_browser_checks(self):
        for check in ("tests/integration/unread-is-not-empty.mjs", "scripts/check_dialog_focus.mjs"):
            with self.subTest(check=check):
                self.assertEqual("true", self.outputs([check])["ui"])

    def test_a_change_to_docs_runs_no_browser_checks(self):
        self.assertEqual("false", self.outputs(["docs/design.md"])["ui"])


if __name__ == "__main__":
    unittest.main()
