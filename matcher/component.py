"""Integrable pipeline orchestrator: ingest -> match -> dual ledger ->
snapshot -> replay -> verify, plus incremental invalidation updates."""
import json
import os
import shutil

from .cache import affected_images, check_revision_regress, compute_keys
from .config import Config
from .errors import BusinessError, E_RESOURCE_LIMIT
from .ingest import build_intermediate, load_candidates, load_ground_truth
from .ledger import read_ledger, read_v1, read_v2, write_v1, write_v2
from .match import match_image
from .migrate import migrate_v1_to_v2, migrate_v2_to_v1
from .models import ImageMatch, canonical_json, content_hash, write_atomic
from .replay import replay_verify
from .selfcheck import selfcheck
from .snapshot import BatchCommitter, SnapshotStore, StageLock


class SimulatedCrash(Exception):
    """Raised by tests/examples to emulate a mid-run interruption."""


def _cfg_snapshot(cfg):
    return {
        "iou_threshold": cfg.iou_threshold,
        "classes": list(cfg.classes),
        "n_shards": cfg.n_shards,
        "max_open_shards": cfg.max_open_shards,
        "max_resident_candidates": cfg.max_resident_candidates,
        "max_recompute_images": cfg.max_recompute_images,
        "batch_size": cfg.batch_size,
        "clock_base_ms": cfg.clock_base_ms,
        "clock_step_ms": cfg.clock_step_ms,
    }


class Pipeline:
    """Facade wiring all stages together against a local workspace root."""

    def __init__(self, root=".", config=None):
        self.root = root
        self.cfg = (config or Config()).validate()
        self.dir_samples = os.path.join(root, "samples")
        self.dir_ledgers = os.path.join(root, "ledgers")
        self.dir_ckpt = os.path.join(root, "checkpoints")
        for path in (self.dir_samples, self.dir_ledgers, self.dir_ckpt):
            os.makedirs(path, exist_ok=True)
        self.v1_path = os.path.join(self.dir_ledgers, "ledger_v1.json")
        self.v2_dir = os.path.join(self.dir_ledgers, "v2")
        self.downgrade_log = os.path.join(self.dir_ledgers, "downgrade_log.json")
        self.state_path = os.path.join(self.dir_ckpt, "state.json")
        self.matches_path = os.path.join(self.dir_ckpt, "matches.json")
        self.gt_manifest_path = os.path.join(self.dir_ckpt, "gt_manifest.jsonl")
        self.cfg_snapshot_path = os.path.join(self.dir_ckpt, "config_snapshot.json")
        self.snapshots = SnapshotStore(os.path.join(self.dir_ckpt, "snapshots.json"))
        self.committer = BatchCommitter(self.dir_ckpt)
        self.lock = StageLock(os.path.join(self.dir_ledgers, "commit.lock"))
        self.state = self._load_state()

    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {
            "ledger_version": 0,
            "gt_version": 0,
            "config_version": 0,
            "generations": {},
            "keys": {},
            "candidates_path": None,
            "iou_threshold": self.cfg.iou_threshold,
        }

    def _save_state(self):
        write_atomic(self.state_path, canonical_json(self.state))

    def _load_committed_matches(self):
        matches = {}
        done = self.committer.committed_batch()
        for batch_id in range(done + 1):
            payload = self.committer.load_batch(batch_id)
            for image_id, doc in payload["matches"].items():
                matches[image_id] = ImageMatch.from_dict(doc)
        return matches

    def _persist_matches(self, matches):
        write_atomic(
            self.matches_path,
            canonical_json({k: matches[k].to_dict() for k in sorted(matches)}),
        )

    def _load_matches_file(self):
        with open(self.matches_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: ImageMatch.from_dict(v) for k, v in raw.items()}

    def _write_ledgers(self, matches):
        self.state["ledger_version"] += 1
        version = self.state["ledger_version"]
        ordered = [matches[k] for k in sorted(matches)]
        doc_v1 = write_v1(self.v1_path, ordered, self.cfg, version)
        result_v2 = write_v2(
            self.v2_dir, ordered, self.state["generations"], self.cfg, version
        )
        self.snapshots.update("ledger_v1", version, self.v1_path)
        self.snapshots.update(
            "ledger_v2", version, os.path.join(self.v2_dir, "meta.sqlite3")
        )
        return doc_v1, result_v2

    def _snapshot_inputs(self, gts):
        self.state["gt_version"] += 1
        lines = []
        for g in sorted(gts, key=lambda g: (g.image_id, g.box_idx)):
            lines.append(
                canonical_json(
                    dict({"image_id": g.image_id, "box_idx": g.box_idx}, **g.content())
                )
            )
        write_atomic(self.gt_manifest_path, "\n".join(lines) + "\n")
        self.snapshots.update(
            "gt_manifest", self.state["gt_version"], self.gt_manifest_path
        )
        self.state["config_version"] += 1
        write_atomic(self.cfg_snapshot_path, canonical_json(_cfg_snapshot(self.cfg)))
        self.snapshots.update(
            "config", self.state["config_version"], self.cfg_snapshot_path
        )

    def _verify_all(self, images):
        doc_v1 = read_v1(self.v1_path)
        data_v2 = read_v2(self.v2_dir)
        report = replay_verify(
            doc_v1["rows"], data_v2["rows"], images, self.cfg.iou_threshold
        )
        check = selfcheck(doc_v1["rows"], data_v2["rows"], report)
        return {"replay": report, "selfcheck": check}

    def run(self, cand_path, gt_path, crash_after_batch=None):
        """Full pipeline with batch-boundary commits and crash recovery."""
        self.cfg.validate()
        cands = load_candidates(cand_path, self.cfg.classes)
        gts = load_ground_truth(gt_path, self.cfg.classes)
        images, ir_text = build_intermediate(cands, gts, self.cfg.classes)
        degraded = []
        if len(cands) > self.cfg.max_resident_candidates:
            if not self.cfg.allow_degrade:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "resident candidates %d exceed limit %d"
                    % (len(cands), self.cfg.max_resident_candidates),
                    field="max_resident_candidates",
                )
            degraded.append("resident_candidates")
        image_ids = sorted(images)
        new_keys = compute_keys(images, self.cfg.iou_threshold, self.cfg.classes)
        # committed batches are only reused for the identical input fingerprint
        fingerprint = content_hash(new_keys)
        if self.committer.job_key() != fingerprint:
            self.committer.reset()
            self.committer.set_job_key(fingerprint)
        batches = [
            image_ids[i : i + self.cfg.batch_size]
            for i in range(0, len(image_ids), self.cfg.batch_size)
        ]
        done = self.committer.committed_batch()
        matches = self._load_committed_matches()
        resumed_from = done
        recomputed = 0
        with self.lock:
            for batch_id in range(done + 1, len(batches)):
                batch_matches = {}
                for image_id in batches[batch_id]:
                    bundle = images[image_id]
                    im = match_image(
                        image_id,
                        bundle["candidates"],
                        bundle["ground_truth"],
                        self.cfg.iou_threshold,
                    )
                    matches[image_id] = im
                    batch_matches[image_id] = im.to_dict()
                    recomputed += 1
                self.committer.commit(batch_id, {"matches": batch_matches})
                if crash_after_batch is not None and batch_id == crash_after_batch:
                    raise SimulatedCrash("interrupted after batch %d" % batch_id)
            doc_v1, result_v2 = self._write_ledgers(matches)
            self._snapshot_inputs(gts)
        self.state["candidates_path"] = cand_path
        self.state["keys"] = new_keys
        self.state["iou_threshold"] = self.cfg.iou_threshold
        for image_id in image_ids:
            self.state["generations"].setdefault(image_id, 0)
        self._save_state()
        self._persist_matches(matches)
        verification = self._verify_all(images)
        return {
            "status": "OK",
            "images": len(image_ids),
            "rows_v1": len(doc_v1["rows"]),
            "rows_v2": len(result_v2["rows"]),
            "aggregate": doc_v1["aggregate"],
            "batches_total": len(batches),
            "resumed_from_batch": resumed_from,
            "images_recomputed": recomputed,
            "degraded": degraded + result_v2["degraded"],
            "ir_bytes": len(ir_text.encode("utf-8")),
            "verification": verification,
        }

    def apply_update(self, gt_path=None, iou_threshold=None, migrate_to=None):
        """Directed invalidation: GT revision bumps and/or threshold change.

        A ledger format change (v1<->v2) migrates the whole ledger first;
        only content-affected images are recomputed afterwards.
        """
        migration = None
        if migrate_to == "v2":
            cand_store = self._candidate_store()
            migration = {"direction": "v1->v2"}
            migration.update(
                migrate_v1_to_v2(
                    self.v1_path, self.v2_dir, self.cfg,
                    self.state["ledger_version"], candidate_store=cand_store,
                )
            )
        elif migrate_to == "v1":
            migration = {
                "direction": "v2->v1",
                "record": migrate_v2_to_v1(
                    self.v2_dir, self.v1_path, self.downgrade_log,
                    self.cfg, self.state["ledger_version"],
                ),
            }
        if iou_threshold is not None:
            self.cfg.iou_threshold = float(iou_threshold)
            self.cfg.validate()
        cand_path = self.state["candidates_path"]
        cands = load_candidates(cand_path, self.cfg.classes)
        old_gts = []
        if os.path.exists(self.gt_manifest_path):
            old_gts = self._manifest_gts()
        if gt_path is not None:
            new_gts = load_ground_truth(gt_path, self.cfg.classes)
            check_revision_regress(old_gts, new_gts)
        else:
            new_gts = old_gts
        images, _ = build_intermediate(cands, new_gts, self.cfg.classes)
        new_keys = compute_keys(images, self.cfg.iou_threshold, self.cfg.classes)
        affected = affected_images(self.state["keys"], new_keys)
        if len(affected) > self.cfg.max_recompute_images:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "recompute images %d exceed limit %d"
                % (len(affected), self.cfg.max_recompute_images),
                field="max_recompute_images",
            )
        matches = self._load_matches_file()
        with self.lock:
            for image_id in affected:
                bundle = images[image_id]
                matches[image_id] = match_image(
                    image_id,
                    bundle["candidates"],
                    bundle["ground_truth"],
                    self.cfg.iou_threshold,
                )
                self.state["generations"][image_id] = (
                    self.state["generations"].get(image_id, 0) + 1
                )
            for image_id in list(matches):
                if image_id not in images:
                    del matches[image_id]
            doc_v1, result_v2 = self._write_ledgers(matches)
            self._snapshot_inputs(new_gts)
        self.state["keys"] = new_keys
        self.state["iou_threshold"] = self.cfg.iou_threshold
        self._save_state()
        self._persist_matches(matches)
        verification = self._verify_all(images)
        return {
            "status": "OK",
            "migration": migration,
            "affected_images": affected,
            "images_reused": len(matches) - len(affected),
            "rows_v1": len(doc_v1["rows"]),
            "rows_v2": len(result_v2["rows"]),
            "verification": verification,
        }

    def _candidate_store(self):
        cand_path = self.state.get("candidates_path")
        if not cand_path:
            return {}
        store = {}
        for cand in load_candidates(cand_path, self.cfg.classes):
            store[(cand.image_id, cand.box_idx)] = cand
        return store

    def _manifest_gts(self):
        from .models import GTBox

        gts = []
        with open(self.gt_manifest_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                gts.append(
                    GTBox(
                        image_id=rec["image_id"],
                        box_idx=rec["box_idx"],
                        x1=rec["x1"],
                        y1=rec["y1"],
                        x2=rec["x2"],
                        y2=rec["y2"],
                        label=rec["label"],
                        difficulty=rec["difficulty"],
                        occluded=rec["occluded"],
                        revision=rec["revision"],
                    )
                )
        return gts

    def verify_snapshots(self):
        """Checksum-verify every recorded snapshot artifact."""
        verified = []
        for name in ("ledger_v1", "ledger_v2", "gt_manifest", "config"):
            entry = self.snapshots.verify(name)
            if entry:
                verified.append(name)
        return verified

    def read_back(self):
        """Read both ledgers through the version-dispatching reader."""
        v1_version, doc_v1 = read_ledger(self.v1_path)
        v2_version, data_v2 = read_ledger(self.v2_dir)
        return {
            "v1": {"version": v1_version, "rows": len(doc_v1["rows"])},
            "v2": {"version": v2_version, "rows": len(data_v2["rows"])},
        }

    def reset(self):
        """Remove all derived state (keeps source samples)."""
        for path in (self.dir_ledgers, self.dir_ckpt):
            if os.path.exists(path):
                shutil.rmtree(path)
        os.makedirs(self.dir_ledgers, exist_ok=True)
        os.makedirs(self.dir_ckpt, exist_ok=True)
