"""Stage snapshot store: manifest, atomic commits, single-committer lock.

Layout under the state directory:
    manifest.json                     committed stage -> {version, checksum}
    stages/<stage>/v<NNN>.jsonl       committed snapshot payload (canonical)
    stages/<stage>/v<NNN>.meta.json   snapshot metadata (deps, degradations)
    working/                          uncommitted in-progress data (wiped on boot)
    commit.lock                       single-committer lock (non-blocking)

A stage snapshot becomes visible only through an atomic manifest rename,
so partially finished stages are never committed.
"""

import json
import os
import shutil
from contextlib import contextmanager

from .errors import E_STAGE_BUSY, E_SNAPSHOT_ROLLBACK, PipelineError
from .util import atomic_write, canonical_bytes, sha256_hex


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class StageStore:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.stages_dir = os.path.join(state_dir, "stages")
        self.working_dir = os.path.join(state_dir, "working")
        self.manifest_path = os.path.join(state_dir, "manifest.json")
        self.lock_path = os.path.join(state_dir, "commit.lock")
        os.makedirs(self.stages_dir, exist_ok=True)
        # Recovery rule: any uncommitted in-progress work is discarded.
        shutil.rmtree(self.working_dir, ignore_errors=True)
        os.makedirs(self.working_dir, exist_ok=True)
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, "r", encoding="utf-8") as fh:
                self.manifest = json.load(fh)
        else:
            self.manifest = {"format": 1, "stages": {}}

    # ---------------- committed state ----------------

    def committed(self, stage):
        return self.manifest["stages"].get(stage)

    def snapshot_path(self, stage, version):
        return os.path.join(self.stages_dir, stage, f"v{version:03d}.jsonl")

    def meta_path(self, stage, version):
        return os.path.join(self.stages_dir, stage, f"v{version:03d}.meta.json")

    def load_snapshot_bytes(self, stage, version):
        with open(self.snapshot_path(stage, version), "rb") as fh:
            return fh.read()

    def load_meta(self, stage, version):
        with open(self.meta_path(stage, version), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def iter_snapshot(self, stage, version):
        path = self.snapshot_path(stage, version)
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # ---------------- commit protocol ----------------

    @contextmanager
    def commit_lock(self, stage=None):
        """Non-blocking single-committer lock; loser gets E_STAGE_BUSY at once."""
        fd = None
        for attempt in (1, 2):
            try:
                fd = os.open(self.lock_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                stale = False
                try:
                    with open(self.lock_path, "r", encoding="utf-8") as fh:
                        holder = int(fh.read().strip() or "0")
                    stale = not _pid_alive(holder)
                except (OSError, ValueError):
                    stale = True
                if stale and attempt == 1:
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                    continue
                raise PipelineError(
                    E_STAGE_BUSY,
                    "another committer holds the stage commit lock",
                    stage=stage)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
            yield
        finally:
            os.close(fd)
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass

    def commit(self, stage, version, data, meta):
        """Atomically publish a fully processed stage snapshot.

        Raises E_STAGE_BUSY on concurrent commit, E_SNAPSHOT_ROLLBACK when
        the proposed version would not move the stage forward.
        """
        with self.commit_lock(stage=stage):
            current = self.manifest["stages"].get(stage)
            if current and version <= current["version"]:
                raise PipelineError(
                    E_SNAPSHOT_ROLLBACK,
                    f"stage {stage} already at v{current['version']}, "
                    f"refusing v{version}",
                    stage=stage, version=current["version"])
            checksum = sha256_hex(data)
            meta = dict(meta)
            meta.update({
                "stage": stage,
                "version": version,
                "checksum": checksum,
            })
            stage_dir = os.path.join(self.stages_dir, stage)
            os.makedirs(stage_dir, exist_ok=True)
            atomic_write(self.snapshot_path(stage, version), data, stage=stage)
            atomic_write(self.meta_path(stage, version),
                         canonical_bytes(meta), stage=stage)
            # Wholesale replacement: drop superseded versions of this stage.
            for name in os.listdir(stage_dir):
                if name.startswith("v") and not name.startswith(
                        f"v{version:03d}."):
                    try:
                        os.unlink(os.path.join(stage_dir, name))
                    except OSError:
                        pass
            # Manifest flip is the single atomic visibility point.
            self.manifest["stages"][stage] = {
                "version": version,
                "checksum": checksum,
            }
            atomic_write(self.manifest_path, canonical_bytes(self.manifest),
                         stage=stage)
        return meta
