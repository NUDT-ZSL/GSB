"""Controller: spawns worker processes, checks consistency and versions."""

import base64
import multiprocessing as mp
import os
import time
from dataclasses import dataclass, field

import numpy as np

from .config import SimConfig
from .errors import E_DIVERGE, E_VERSION, SimError
from .model import digest_array, init_params
from .snapshot import load_snapshot, save_snapshot
from .worker import run_worker


@dataclass
class StepResult:
    version: int
    grad_digest: str
    param_digest: str
    elapsed: float
    workers: list = field(default_factory=list)  # per-rank done records


class Controller:
    """Owns the authoritative training state for one session."""

    def __init__(self, cfg: SimConfig):
        cfg.validate()
        self.cfg = cfg
        self.params = init_params(cfg.seed, cfg.dim)
        self.version = 0
        self.steps_done = 0

    # ------------------------------------------------------------------ state
    def state_dict(self):
        return {"params": self.params, "version": self.version,
                "seed": self.cfg.seed, "dim": self.cfg.dim,
                "steps_done": self.steps_done, "lr": self.cfg.lr}

    def save_snapshot(self, path):
        return save_snapshot(path, self.state_dict())

    def restore(self, path):
        """Whole-state replace when the snapshot version differs."""
        snap = load_snapshot(path)
        replaced = snap["version"] != self.version or snap["steps_done"] != self.steps_done
        if snap["dim"] != self.cfg.dim or snap["seed"] != self.cfg.seed:
            # Session config follows the snapshot as a whole as well.
            self.cfg.seed = snap["seed"]
            self.cfg.dim = snap["dim"]
        self.params = snap["params"]
        self.version = snap["version"]
        self.steps_done = snap["steps_done"]
        self.cfg.lr = snap["lr"]
        return {"replaced": replaced, "version": self.version,
                "steps_done": self.steps_done}

    # ------------------------------------------------------------------- step
    def run_step(self, event_sink=None):
        """Run one full training step (one ring all-reduce + update)."""
        cfg = self.cfg
        os.makedirs(cfg.log_dir, exist_ok=True)
        for fname in os.listdir(cfg.log_dir):
            if fname.startswith("worker-") and fname.endswith(".jsonl"):
                os.remove(os.path.join(cfg.log_dir, fname))
        worker_cfg = {
            "workers": cfg.workers, "dim": cfg.dim, "seed": cfg.seed,
            "step": self.version, "lr": cfg.lr,
            "block_size": cfg.block_size, "chunk_sizes": cfg.chunk_sizes,
            "interleave": cfg.interleave, "base_port": cfg.base_port,
            "delays_ms": {str(k): float(v) for k, v in cfg.delays_ms.items()},
            "log_dir": cfg.log_dir, "recv_timeout_s": cfg.recv_timeout_s,
            "params_b64": base64.b64encode(self.params.tobytes()).decode("ascii"),
        }
        queue = mp.Queue()
        procs = [mp.Process(target=run_worker, args=(worker_cfg, r, queue),
                            name=f"worker-{r}", daemon=True)
                 for r in range(cfg.workers)]
        started = time.monotonic()
        for proc in procs:
            proc.start()

        done, errors = [], []
        remaining = cfg.workers
        while remaining > 0:
            record = queue.get()
            if event_sink is not None:
                event_sink(record)
            if record.get("kind") == "done":
                done.append(record)
                remaining -= 1
            elif record.get("kind") == "error":
                errors.append(record)
                remaining -= 1
            # 'status' records are forwarded to the sink only.

        for proc in procs:
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)

        if errors:
            first = errors[0]
            raise SimError(first["code"], first["message"], **first.get("context", {}))

        done.sort(key=lambda rec: rec["rank"])
        self._check_versions(done)
        self._check_consistency(done)

        global_grad = np.frombuffer(
            base64.b64decode(done[0]["global_grad_b64"]), dtype=np.float64).copy()
        self.params = self.params - cfg.lr * (global_grad / cfg.workers)
        self.version += 1
        self.steps_done += 1
        return StepResult(
            version=self.version,
            grad_digest=done[0]["grad_digest"],
            param_digest=digest_array(self.params),
            elapsed=time.monotonic() - started,
            workers=done,
        )

    # ----------------------------------------------------------------- checks
    def _check_versions(self, records):
        expected = self.version + 1
        bad = {rec["rank"]: rec["version"] for rec in records
               if rec["version"] != expected}
        if bad:
            raise SimError(E_VERSION, "parameter version skew detected",
                           expected=expected, workers=bad)

    @staticmethod
    def _check_consistency(records):
        digests = {rec["grad_digest"] for rec in records}
        if len(digests) == 1:
            return
        nchunks = len(records[0]["chunk_digests"])
        first_chunk = next(
            c for c in range(nchunks)
            if len({rec["chunk_digests"][c] for rec in records}) > 1)
        raise SimError(
            E_DIVERGE, "global gradient digests diverge across workers",
            first_divergent_chunk=first_chunk,
            accumulation_orders={rec["rank"]: rec["acc_orders"][first_chunk]
                                 for rec in records},
            digests={rec["rank"]: rec["grad_digest"] for rec in records})


def verify_run(log_dir):
    """Consistency check command: compare per-worker grad digests in logs."""
    import json

    records = []
    for fname in sorted(os.listdir(log_dir)):
        if not (fname.startswith("worker-") and fname.endswith(".jsonl")):
            continue
        rank = int(fname[len("worker-"):-len(".jsonl")])
        done = None
        with open(os.path.join(log_dir, fname), "r", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("event") == "done":
                    done = rec
                elif rec.get("event") == "error":
                    raise SimError(rec["code"], rec.get("message", "worker error"),
                                   rank=rank)
        if done is None:
            raise SimError(E_DIVERGE, "worker log missing done record", rank=rank)
        done["rank"] = rank
        records.append(done)
    if not records:
        raise SimError(E_DIVERGE, "no worker logs found", log_dir=log_dir)
    Controller._check_consistency(records)
    return {"workers": len(records), "grad_digest": records[0]["grad_digest"],
            "consistent": True}
