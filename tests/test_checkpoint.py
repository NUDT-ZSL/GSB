from __future__ import annotations

import json
from pathlib import Path

import pytest

from sso_ticket_guard.checkpoint import CheckpointManager
from sso_ticket_guard.constants import SCHEMA_VERSION
from sso_ticket_guard.engine import RenewalEngine, RenewalState
from sso_ticket_guard.errors import BusinessError


def _manager(tmp_path: Path, dataset, quota_version: str | None = None):
    return CheckpointManager(
        tmp_path / "state",
        schema_version=SCHEMA_VERSION,
        quota_version=quota_version or dataset.quota_profile_version,
    )


def _save_batches(manager: CheckpointManager, dataset, batch_limit: int) -> None:
    engine = RenewalEngine(dataset)
    requests = dataset.renewal_order()
    batches: dict[int, list] = {}
    for request in requests:
        batches.setdefault(request.batch_id, []).append(request)
    state = None
    events: list[dict] = []
    with manager.session() as session:
        view = session.load()
        state = view.state
        events = list(view.events)
        for batch_id in sorted(batches)[:batch_limit]:
            _, batch_events, state = engine.adjudicate_all(batches[batch_id], state)
            events.extend(batch_events)
            session.save(watermark=batch_id, events=events, state=state)


def test_checkpoint_round_trip_and_replay(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 2)
    view = manager.load_for_resume()
    assert view.status == "resumed"
    assert view.watermark == 2
    assert view.diagnostics["replay_verified"] is True
    assert view.summary["watermark"] == 2
    assert view.summary["processed_request_ids"]


def test_orphan_snapshot_is_ignored_as_half_write(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 2)
    snapshot = next(manager.snapshot_dir.glob("ckpt-*.json"))
    wrapper = json.loads(snapshot.read_text(encoding="utf-8"))
    orphan = manager.snapshot_dir / "ckpt-99999999999999999999-orphan.json"
    orphan.write_text(
        json.dumps({"checksum": "orphan", "payload": wrapper["payload"]}),
        encoding="utf-8",
    )
    view = manager.load_for_resume()
    assert view.status == "resumed"
    assert view.watermark == 2
    assert view.diagnostics["orphan_snapshots"] == [
        {"checkpoint_id": "orphan", "location": str(orphan)}
    ]


def test_missing_latest_snapshot_falls_back_to_last_complete(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 3)
    with manager.session() as session:
        rows = session.connection.execute(
            "SELECT sequence, checkpoint_id FROM checkpoints ORDER BY sequence"
        ).fetchall()
    assert len(rows) == 3
    latest = rows[-1]
    latest_snapshot = manager._snapshot_path(int(latest[0]), str(latest[1]))
    latest_snapshot.unlink()

    view = manager.load_for_resume()
    assert view.status == "resumed"
    assert view.sequence == 2
    assert view.watermark == 2
    assert view.diagnostics["damaged_checkpoints"][0]["reason"] == "missing_snapshot"


def test_version_change_rebuilds_everything(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 2)
    changed = _manager(tmp_path, dataset, quota_version="new-quota-profile/v2")
    view = changed.load_for_resume()
    assert view.status == "rebuilt"
    assert view.watermark == 0
    assert "version changed" in view.diagnostics["rebuild_reason"]
    with changed.session() as session:
        count = session.connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    assert count == 0
    assert list(changed.snapshot_dir.glob("ckpt-*.json")) == []


def test_lock_conflict_returns_e_lock_without_damage(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 1)
    contender = _manager(tmp_path, dataset)
    with manager.session():
        with pytest.raises(BusinessError) as error:
            contender.load_for_resume()
    assert error.value.code == "E_LOCK"
    assert manager.load_for_resume().watermark == 1


def test_corrupt_database_payload_falls_back(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 2)
    with manager.session() as session:
        latest = session.connection.execute(
            "SELECT sequence FROM checkpoints ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        session.connection.execute(
            "UPDATE checkpoints SET payload_json='{}' WHERE sequence=?",
            (latest,),
        )
        session.connection.commit()
    view = manager.load_for_resume()
    assert view.sequence == 1
    assert view.watermark == 1
    assert view.diagnostics["damaged_checkpoints"][0]["reason"] == (
        "snapshot_database_payload_mismatch"
    )


def test_direct_save_after_version_change_rebuilds_old_state(tmp_path: Path, dataset):
    manager = _manager(tmp_path, dataset)
    _save_batches(manager, dataset, 2)
    changed = _manager(tmp_path, dataset, quota_version="new-quota-profile/v2")
    changed.save(watermark=0, events=[], state=RenewalState())
    view = changed.load_for_resume()
    assert view.status == "resumed"
    assert view.watermark == 0
    assert view.checkpoint_id is not None

