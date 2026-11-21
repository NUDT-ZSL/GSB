"""End-to-end pipeline session: generate -> parse -> merge -> evaluate
-> index -> snapshot commit -> query engine.

The session is the single place that wires the batch pipeline and the
interactive query path together, and it records the most recent business
error codes for the workbench display.
"""
from __future__ import annotations

from .config import WorkbenchConfig
from .consistency import verify_query_vs_batch
from .errors import PanoError
from .formats import encode_v2, read_mask
from .generator import Sample, generate_sample
from .index import QueryIndex
from .merge import PanopticMask, merge_masks
from .metrics import MetricsReport, evaluate
from .query import QueryEngine
from .selfcheck import run_self_check
from .snapshot import Snapshot, SnapshotManager, SnapshotStore


class Session:
    def __init__(self, cfg: WorkbenchConfig):
        self.cfg = cfg
        self.store = SnapshotStore(cfg.snapshot_dir)
        self.manager = SnapshotManager()
        self.recent_errors: list[dict] = []
        self.sample: Sample | None = None
        self.mask: PanopticMask | None = None
        self.gt_mask: PanopticMask | None = None
        self.report: MetricsReport | None = None
        self.index: QueryIndex | None = None
        self.engine: QueryEngine | None = None
        self.snapshot: Snapshot | None = None

    # ------------------------------------------------------------ pipeline
    def run_pipeline(self) -> None:
        cfg = self.cfg
        self.sample = generate_sample(cfg)

        parsed = [
            read_mask(self.sample.pred_v1, cfg),
            read_mask(self.sample.pred_v2, cfg),
            read_mask(self.sample.pred_rle, cfg),
        ]
        self.mask = merge_masks(parsed, cfg)

        gt_parsed = read_mask(
            encode_v2(self.sample.gt_class, self.sample.gt_instance,
                      self.sample.gt_conf, cfg.mapping_version),
            cfg,
        )
        self.gt_mask = merge_masks([gt_parsed], cfg)

        self.report = evaluate(self.mask, self.gt_mask, cfg)
        self._commit_snapshot()

    def _commit_snapshot(self) -> None:
        cfg = self.cfg
        next_version = self.store.latest_version() + 1
        self.index = QueryIndex(self.mask, self.report, cfg,
                                snapshot_version=next_version)
        snap = self.store.commit(
            self.index.canonical_bytes(),
            self.report.canonical_bytes(),
            {"config_fingerprint": cfg.fingerprint(),
             "mask_checksum": self.mask.checksum()},
        )
        self.snapshot = self.manager.publish(snap)
        self.engine = QueryEngine(self.index, cfg)

    # ------------------------------------------------------------- guards
    def _require_engine(self) -> QueryEngine:
        if self.engine is None:
            raise PanoError("E_NOT_READY", "pipeline has not run yet")
        return self.engine

    def record_error(self, err: PanoError) -> None:
        self.recent_errors.append(err.to_dict())
        del self.recent_errors[:-8]           # keep the most recent few

    # -------------------------------------------------------------- facade
    def query_pixel(self, x: int, y: int):
        try:
            return self._require_engine().query_pixel(x, y)
        except PanoError as err:
            self.record_error(err)
            raise

    def query_instance(self, instance_id: int):
        try:
            return self._require_engine().query_instance(instance_id)
        except PanoError as err:
            self.record_error(err)
            raise

    def verify_consistency(self) -> dict:
        engine = self._require_engine()
        return verify_query_vs_batch(engine, self.index, self.mask,
                                     self.report, self.cfg)

    def self_check(self) -> dict:
        self._require_engine()
        return run_self_check(self.index, self.mask, self.report,
                              self.manager.current(), self.cfg)
