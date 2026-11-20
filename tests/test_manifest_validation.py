from __future__ import annotations

from pathlib import Path

import pytest

from conftest import payload, read_manifest, run_sample, write_manifest


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("duplicate", "E_DUPLICATE_TASK"),
        ("escape", "E_PATH_ESCAPE"),
        ("env", "E_ENV_INVALID"),
        ("timeout", "E_TIMEOUT_INVALID"),
        ("missing_input", "E_INPUT_MISSING"),
    ],
)
def test_manifest_errors_are_distinct_and_do_not_persist(
    sample_workspace: Path, mutation: str, expected: str
) -> None:
    data = read_manifest(sample_workspace)
    if mutation == "duplicate":
        data["tasks"].append(dict(data["tasks"][0]))
    elif mutation == "escape":
        data["tasks"][0]["reads"][0] = "../outside.json"
    elif mutation == "env":
        data["tasks"][0]["env_allowlist"] = ["BAD-NAME"]
    elif mutation == "timeout":
        data["tasks"][0]["timeout_ms"] = 0
    elif mutation == "missing_input":
        data["tasks"][0]["inputs"][0] = "samples/data/missing.json"
        data["tasks"][0]["reads"][0] = "samples/data/missing.json"
    write_manifest(sample_workspace, data)

    result = run_sample(
        sample_workspace,
        state="var/never-created/state.sqlite3",
        cache="var/never-created/cache",
    )
    assert result.returncode != 0
    error = payload(result)["error"]
    assert error["code"] == expected
    assert not (sample_workspace / "var/never-created").exists()
    assert not (sample_workspace / "artifacts").exists()


def test_symlink_escape_is_rejected(sample_workspace: Path) -> None:
    outside = sample_workspace.parent / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text('{"records":[]}', encoding="utf-8")
    link = sample_workspace / "samples" / "data" / "escape_link.json"
    link.symlink_to(outside / "secret.json")
    data = read_manifest(sample_workspace)
    data["tasks"][0]["inputs"][0] = "samples/data/escape_link.json"
    data["tasks"][0]["reads"][0] = "samples/data/escape_link.json"
    write_manifest(sample_workspace, data)
    result = run_sample(sample_workspace)
    assert result.returncode != 0
    assert payload(result)["error"]["code"] == "E_PATH_ESCAPE"
    assert not (sample_workspace / "var").exists()

