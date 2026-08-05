import os

import pytest

from omm_agent_tools import PythonSandbox, TaskWorkspace
from omm_agent_tools.registry import ToolCallContext


@pytest.fixture()
def sandbox(tmp_path):
    workspace = TaskWorkspace(root=tmp_path, run_id="run_s")
    return PythonSandbox(workspace, timeout_s=15.0)


def ctx(step="step_1"):
    return ToolCallContext(run_id="run_s", step_id=step, tool_name="python_run")


def test_happy_run_captures_stdout_and_artifacts(sandbox):
    code = (
        "from pathlib import Path\n"
        "Path('out.txt').write_text('42', encoding='utf-8')\n"
        "print('hello from sandbox')\n"
    )
    result = sandbox._handle({"code": code}, ctx())

    assert result.status == "succeeded"
    assert "hello from sandbox" in result.output["stdout"]
    assert result.output["exit_code"] == 0
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.size == 2
    assert artifact.producer_step == "step_1"


def test_environment_is_scrubbed(sandbox, monkeypatch):
    monkeypatch.setenv("OMM_SUPER_SECRET", "leak-me")
    code = "import os\nprint(repr(os.environ.get('OMM_SUPER_SECRET')))\n"

    result = sandbox._handle({"code": code}, ctx("step_env"))

    assert result.status == "succeeded"
    assert "None" in result.output["stdout"]
    assert "leak-me" not in result.output["stdout"]


def test_nonzero_exit_is_failure_with_stderr(sandbox):
    result = sandbox._handle(
        {"code": "import sys\nsys.exit('broken experiment')\n"}, ctx("step_fail")
    )

    assert result.status == "failed"
    assert "exited with code 1" in result.error
    assert "broken experiment" in result.output["stderr"]


def test_timeout_kills_the_process(sandbox):
    result = sandbox._handle(
        {"code": "import time\ntime.sleep(30)\n", "timeout_s": 1.5},
        ctx("step_slow"),
    )

    assert result.status == "timeout"
    assert "killed" in result.error


def test_output_is_truncated(sandbox):
    result = sandbox._handle(
        {"code": "print('x' * 100000)"}, ctx("step_big")
    )

    assert result.status == "succeeded"
    assert "truncated" in result.output["stdout"]
    assert len(result.output["stdout"]) < 40000


def test_empty_code_is_rejected(sandbox):
    result = sandbox._handle({"code": "   "}, ctx("step_empty"))
    assert result.status == "failed"
    assert "non-empty" in result.error


def test_spec_wires_registry_metadata(sandbox):
    spec = sandbox.spec()
    assert spec.name == "python_run"
    assert spec.risk == "high"
    assert spec.required_args == ("code",)
    assert spec.timeout_s > sandbox.timeout_s
