from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
from pathlib import Path

import pytest

from certpath.engine import CertPathEngine
from certpath.errors import CertPathError
from tests.helpers import chain_map, load_json, update_v2_anchor, write_json


def test_snapshot_and_chain_arrival_order_is_irrelevant(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    first = CertPathEngine(tmp_path / "first").adjudicate(
        sample_dir, arrival_order=("v1", "v2")
    )
    second = CertPathEngine(tmp_path / "second").adjudicate(
        sample_dir, arrival_order=("v2", "v1")
    )
    assert first == second
    assert first["snapshot"]["digest"] == second["snapshot"]["digest"]
    assert first["summary"]["trusted"] == 4
    assert first["summary"]["partial"] == 1
    assert first["summary"]["untrusted"] == 8


def test_ledger_uses_chain_key_and_is_fully_rebuilt(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    engine = CertPathEngine(tmp_path / "state")
    first = engine.adjudicate(sample_copy)
    connection = sqlite3.connect(engine.store.db_path)
    try:
        rows = connection.execute(
            "SELECT chain_key, chain_id, conclusion, snapshot_watermark "
            "FROM verdicts ORDER BY chain_id"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 13
    assert len({row[0] for row in rows}) == 13
    assert all(row[0].startswith("chain:") for row in rows)
    assert all(row[3] == first["snapshot"]["digest"] for row in rows)

    update_v2_anchor(
        sample_copy,
        "v2-anchor-beta",
        trusted=False,
        tombstone=True,
        effective_ms=1_700_000_009_000,
    )
    second = engine.adjudicate(sample_copy)
    assert second["snapshot"]["digest"] != first["snapshot"]["digest"]
    assert chain_map(second)["chain-direct-beta"]["conclusion"] == "untrusted"
    connection = sqlite3.connect(engine.store.db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
        direct = connection.execute(
            "SELECT conclusion FROM verdicts WHERE chain_id='chain-direct-beta'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 13
    assert direct == "untrusted"


def test_repeated_adjudication_is_reproducible(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = CertPathEngine(tmp_path / "state")
    first = engine.adjudicate(sample_dir)
    second = engine.adjudicate(sample_dir)
    assert first == second
    assert first["round_key"] == second["round_key"]


def test_chain_and_v1_list_order_do_not_change_decisions(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    first_sample = tmp_path / "first-sample"
    second_sample = tmp_path / "second-sample"
    shutil.copytree(sample_dir, first_sample)
    shutil.copytree(sample_dir, second_sample)

    first_chains = load_json(first_sample / "chains.json")
    first_chains["chains"] = list(reversed(first_chains["chains"]))
    write_json(first_sample / "chains.json", first_chains)
    second_v1 = load_json(second_sample / "snapshots/v1/trust_v1.json")
    second_v1["trusted_root_ids"] = list(reversed(second_v1["trusted_root_ids"]))
    write_json(second_sample / "snapshots/v1/trust_v1.json", second_v1)

    first = CertPathEngine(tmp_path / "first-state").adjudicate(first_sample)
    second = CertPathEngine(tmp_path / "second-state").adjudicate(second_sample)
    assert first["snapshot"]["digest"] == second["snapshot"]["digest"]
    assert first["round_key"] == second["round_key"]
    assert first["chains"] == second["chains"]
    assert first["summary"] == second["summary"]


def test_prevalidation_failure_does_not_change_ledger(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    engine = CertPathEngine(tmp_path / "state")
    engine.adjudicate(sample_copy)
    before = engine.store.current_ledger_digest()
    before_db = hashlib.sha256(engine.store.db_path.read_bytes()).hexdigest()
    chains = load_json(sample_copy / "chains.json")
    for record in chains["chains"]:
        record["enumeration_limit"] = 0
    write_json(sample_copy / "chains.json", chains)
    with pytest.raises(CertPathError) as captured:
        engine.adjudicate(sample_copy)
    assert captured.value.code == "E_SCHEMA"
    assert engine.store.current_ledger_digest() == before
    after_db = hashlib.sha256(engine.store.db_path.read_bytes()).hexdigest()
    assert after_db == before_db


def test_query_returns_selected_path_and_generation(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = CertPathEngine(tmp_path / "state")
    engine.adjudicate(sample_dir)
    result = engine.query("chain-cross")
    assert result["verdicts"][0]["conclusion"] == "trusted"
    assert result["verdicts"][0]["trust_generation"] == 2
    assert result["verdicts"][0]["selected_certificate_ids"][-1] == "cert-root-beta"
    with pytest.raises(CertPathError) as captured:
        engine.query("does-not-exist")
    assert captured.value.code == "E_NOT_FOUND"
