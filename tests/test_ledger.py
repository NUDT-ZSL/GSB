from __future__ import annotations

import sqlite3

import pytest

from bsl_workbench.codes import LEDGER_LAYOUT_VERSION
from bsl_workbench.ledger import Ledger, LedgerPrecondition
from bsl_workbench.parser import parse_file


def test_segment_pk_generation_and_idempotent_state_generation(tmp_path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    sample = tmp_path / "sample.bsl"

    from bsl_workbench.generator import generate_tree

    root = tmp_path / "samples"
    generate_tree(root)
    report = parse_file(root / "valid/valid_basic.bsl", file_id="fixed-id")

    with Ledger(db_path) as ledger:
        first = ledger.commit_file_report(
            report, source_path=str(sample), completed_files={}
        )
        assert first["sequence"] == 1
        rows = ledger.query_segments("fixed-id")
        assert {row["canonical_key"] for row in rows} == {
            "/alpha",
            "/beta",
            "/gamma",
        }
        assert all(row["generation"] == 1 for row in rows)
        assert all(row["state_generation"] == 1 for row in rows)

        second = ledger.commit_file_report(
            report,
            source_path=str(sample),
            completed_files=first["completed_files"],
        )
        assert second["sequence"] == 2
        rows = ledger.query_segments("fixed-id")
        assert all(row["generation"] == 2 for row in rows)
        assert all(row["state_generation"] == 1 for row in rows)


def test_preflight_failure_does_not_write_ledger(tmp_path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    with Ledger(db_path) as ledger:
        before_segments = ledger.conn.execute(
            "SELECT COUNT(*) AS count FROM segments"
        ).fetchone()["count"]
        before_checkpoints = ledger.conn.execute(
            "SELECT COUNT(*) AS count FROM checkpoints"
        ).fetchone()["count"]

    from bsl_workbench.generator import generate_tree

    root = tmp_path / "samples"
    generate_tree(root)
    report = parse_file(
        root / "invalid/malformed_bad_magic.bsl", file_id="bad-header"
    )
    assert report["preflight_failed"] is True

    with Ledger(db_path) as ledger:
        with pytest.raises(LedgerPrecondition):
            ledger.commit_file_report(
                report, source_path="bad.bsl", completed_files={}
            )
        after_segments = ledger.conn.execute(
            "SELECT COUNT(*) AS count FROM segments"
        ).fetchone()["count"]
        after_checkpoints = ledger.conn.execute(
            "SELECT COUNT(*) AS count FROM checkpoints"
        ).fetchone()["count"]

    assert after_segments == before_segments == 0
    assert after_checkpoints == before_checkpoints == 0


def test_layout_version_mismatch_rebuilds(tmp_path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    with Ledger(db_path) as ledger:
        ledger.conn.execute(
            "INSERT INTO ledger_meta(key, value) VALUES ('x', 'y')"
        )

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE ledger_meta SET value = '1' WHERE key = 'layout_version'"
    )
    connection.commit()
    connection.close()

    with Ledger(db_path) as ledger:
        version = ledger.conn.execute(
            "SELECT value FROM ledger_meta WHERE key = 'layout_version'"
        ).fetchone()["value"]
        assert int(version) == LEDGER_LAYOUT_VERSION
        count = ledger.conn.execute("SELECT COUNT(*) AS count FROM segments").fetchone()[
            "count"
        ]
        assert count == 0
