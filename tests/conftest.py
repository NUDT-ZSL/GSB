import json
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from orchestrator.dag_loader import parse_dag  # noqa: E402


def _stage(stage_id, deps, units, retries=0, timeout_ms=10000, concurrency=None):
    stage = {
        "stage_id": stage_id,
        "script_path": f"../stages/stage_{stage_id}.py",
        "dependencies": deps,
        "resource_units": units,
        "estimated_ms": 10,
        "retries": retries,
        "output_path": f"outputs/{stage_id}.jsonl",
        "timeout_ms": timeout_ms,
    }
    if concurrency is not None:
        stage["max_concurrency"] = concurrency
    return stage


def make_dag(stages, budget=4, version="t-1.0", concurrency=2):
    return {
        "dag_id": "test-dag",
        "dag_version": version,
        "global_resource_budget": budget,
        "default_max_concurrency": concurrency,
        "default_timeout_ms": 10000,
        "stages": stages,
    }


@pytest.fixture
def tmp_workspace(tmp_path):
    base = tmp_path / "dags"
    stages_dir = tmp_path / "stages"
    work = tmp_path / "work"
    sessions = tmp_path / "sessions"
    base.mkdir()
    stages_dir.mkdir()
    work.mkdir()
    sessions.mkdir()
    return {
        "root": tmp_path,
        "dag_dir": base,
        "stages_dir": stages_dir,
        "work": work,
        "sessions": sessions,
    }


def write_script(stages_dir, name, body):
    path = stages_dir / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


SIMPLE_SCRIPT = (
    "import json, os\n"
    "out = os.environ['PIPE_OUTPUT_PATH']\n"
    "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
    "with open(out, 'w', encoding='utf-8', newline='\\n') as fh:\n"
    "    fh.write(json.dumps({'stage': os.environ['PIPE_STAGE_ID']}, sort_keys=True) + '\\n')\n"
)


FAIL_FIRST_TEMPLATE = (
    "import json, os, sys\n"
    "attempt = int(os.environ.get('PIPE_ATTEMPT_NO', '1'))\n"
    "cfg = json.loads(os.environ.get('PIPE_FAIL_FIRST_N', '{}') or '{}')\n"
    "sid = os.environ['PIPE_STAGE_ID']\n"
    "if attempt <= int(cfg.get(sid, 0)):\n"
    "    sys.stderr.write('boom ' + sid + '\\n')\n"
    "    sys.exit(7)\n"
    "out = os.environ['PIPE_OUTPUT_PATH']\n"
    "os.makedirs(os.path.dirname(out), exist_ok=True)\n"
    "with open(out, 'w', encoding='utf-8', newline='\\n') as fh:\n"
    "    fh.write(json.dumps({'stage': sid, 'attempt': attempt}, sort_keys=True) + '\\n')\n"
)


@pytest.fixture
def simple_dag_doc():
    return make_dag(
        [
            _stage("a", [], 2),
            _stage("b", [{"stage_id": "a", "kind": "strong"}], 2),
            _stage(
                "c",
                [
                    {"stage_id": "a", "kind": "strong"},
                    {"stage_id": "b", "kind": "weak"},
                ],
                1,
            ),
        ],
        budget=4,
    )


@pytest.fixture
def simple_env(tmp_workspace, simple_dag_doc):
    for name in ("stage_a.py", "stage_b.py", "stage_c.py"):
        write_script(tmp_workspace["stages_dir"], name, SIMPLE_SCRIPT)
    return tmp_workspace, parse_dag(simple_dag_doc)
