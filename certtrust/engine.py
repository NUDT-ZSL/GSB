"""Engine: state evolution, out-of-order consistency, checkpointing.

The engine keeps every accepted input inside the checkpoint payload, so
recovery only ever continues from the last complete checkpoint. When a
newly arrived snapshot supersedes the current effective one, all chain
conclusions are recomputed against the new anchor; changed conclusions
overwrite the old ones and the overwrite is counted, unchanged ones are
counted as consistent observations. A structure- or policy-version
mismatch forces a full rebuild of conclusions from raw inputs, never an
incremental mix of old-policy conclusions with new revocations.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from . import STRUCTURE_VERSION, errors
from .anchors import SnapshotStore
from .checkpoint import FileLock, read_checkpoint, write_checkpoint
from .models import CertDesc, ChainClaim, Conclusion, Snapshot
from .validation import validate_claim

STATE_DIR = "state"
CHECKPOINT_NAME = "checkpoint.json"
LOCK_NAME = "state.lock"


def _blank_state():
    return {
        "structure_version": STRUCTURE_VERSION,
        "policy_version": None,
        "applied": None,
        "certs": {},
        "claims": {},
        "snapshots": {},
        "conclusions": {},
        "rejects": [],
        "counters": {
            "consistent": 0,
            "overwritten": 0,
            "recomputed": 0,
            "rebuilds": 0,
        },
    }


class Engine:
    def __init__(self, workdir):
        self.workdir = os.path.abspath(workdir)
        self.state_dir = os.path.join(self.workdir, STATE_DIR)
        self.checkpoint_path = os.path.join(self.state_dir, CHECKPOINT_NAME)
        self.lock_path = os.path.join(self.state_dir, LOCK_NAME)
        self.state = _blank_state()
        self.recovered_from_checkpoint = False
        self.checkpoint_corrupt = False

    def lock(self):
        os.makedirs(self.state_dir, exist_ok=True)
        return FileLock(self.lock_path)

    def load(self):
        """Restore from the last complete checkpoint, if any."""
        payload = read_checkpoint(self.checkpoint_path)
        if payload is None:
            self.checkpoint_corrupt = os.path.exists(self.checkpoint_path)
            return False
        self.state = payload
        self.recovered_from_checkpoint = True
        return True

    def save(self):
        write_checkpoint(self.checkpoint_path, self.state)

    def certs(self):
        return {cid: CertDesc.from_dict(d)
                for cid, d in self.state["certs"].items()}

    def claims(self):
        return {cid: ChainClaim.from_dict(d)
                for cid, d in self.state["claims"].items()}

    def store(self):
        return SnapshotStore(
            [Snapshot.from_dict(d) for d in self.state["snapshots"].values()])

    def conclusions(self):
        return {cid: Conclusion.from_dict(d)
                for cid, d in self.state["conclusions"].items()}

    def import_certs(self, certs):
        for cid, cert in certs.items():
            self.state["certs"][cid] = cert.to_dict()

    def import_claims(self, claims):
        for cid, claim in claims.items():
            self.state["claims"][cid] = claim.to_dict()

    def import_snapshots(self, snapshots):
        for snap in snapshots:
            self.state["snapshots"][snap.stable_key] = snap.to_dict()

    def add_rejects(self, rejects):
        self.state["rejects"].extend(rejects)

    def _effective_snapshot(self):
        return self.store().effective

    def _needs_rebuild(self, effective):
        if self.state["structure_version"] != STRUCTURE_VERSION:
            return True
        if effective is None:
            return False
        applied = self.state["applied"]
        if applied is None:
            return False
        return self.state["policy_version"] != effective.policy_version

    def recompute(self, jobs=1):
        """Recompute every conclusion against the effective snapshot.

        Conclusions are always recomputed from raw inputs; the previous
        conclusions only feed the consistent/overwritten counters.
        """
        effective = self._effective_snapshot()
        if effective is None:
            raise errors.CertTrustError(errors.E_NO_SNAPSHOT,
                                        "no effective trust-anchor snapshot")
        if self._needs_rebuild(effective):
            # Full rebuild: old-policy conclusions and their observation
            # counters are discarded, never mixed with the new policy.
            self.state["conclusions"] = {}
            counters = self.state["counters"]
            counters["consistent"] = 0
            counters["overwritten"] = 0
            counters["recomputed"] = 0
            counters["rebuilds"] += 1
        certs = self.certs()
        claims = self.claims()
        previous = self.conclusions()

        def _one(claim):
            return validate_claim(claim, certs, effective)

        ordered = [claims[cid] for cid in sorted(claims)]
        if jobs and jobs > 1 and len(ordered) > 1:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                results = list(pool.map(_one, ordered))
        else:
            results = [_one(c) for c in ordered]

        counters = self.state["counters"]
        new_conclusions = {}
        for concl in results:
            old = previous.get(concl.claim_id)
            if old is not None:
                counters["recomputed"] += 1
                if old.to_dict() == concl.to_dict():
                    counters["consistent"] += 1
                else:
                    counters["overwritten"] += 1
            new_conclusions[concl.claim_id] = concl.to_dict()
        self.state["conclusions"] = new_conclusions
        self.state["policy_version"] = effective.policy_version
        self.state["applied"] = {
            "snapshot_version": effective.snapshot_version,
            "stable_key": effective.stable_key,
        }
        return results

    def checkpoint_status(self):
        """Inspect the on-disk checkpoint without mutating state."""
        if not os.path.exists(self.checkpoint_path):
            return {"ok": False, "code": errors.E_NO_CHECKPOINT,
                    "message": "no checkpoint exists"}
        payload = read_checkpoint(self.checkpoint_path)
        if payload is None:
            return {"ok": False, "code": errors.E_CHECKPOINT_CORRUPT,
                    "message": "checkpoint is missing, half-written or corrupt"}
        if payload.get("structure_version") != STRUCTURE_VERSION:
            return {"ok": False, "code": errors.E_CHECKPOINT_VERSION,
                    "message": "checkpoint structure version mismatch; "
                               "a full rebuild is required"}
        store = SnapshotStore(
            [Snapshot.from_dict(d) for d in payload["snapshots"].values()])
        effective = store.effective
        if effective is not None and payload.get("policy_version") not in (
                None, effective.policy_version):
            return {"ok": False, "code": errors.E_CHECKPOINT_VERSION,
                    "message": "checkpoint policy version differs from the "
                               "effective snapshot; a full rebuild is required"}
        return {"ok": True, "code": None,
                "applied": payload.get("applied"),
                "conclusions": len(payload.get("conclusions", {})),
                "counters": payload.get("counters", {})}
