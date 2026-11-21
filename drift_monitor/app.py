"""Local interactive menu application (no third-party UI dependency).

Run with ``python -m drift_monitor.app``.  The application keeps a small
in-memory state and delegates every computation to the same deterministic
engine used by the CLI, so the interactive demo and the JSON reports are
guaranteed to agree.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any, Dict, List, Optional

from .cache import ContentStore
from .config_reader import read_config
from .data import DriftInjection, generate_batch, read_batches
from .engine import MonitoringEngine
from .errors import DriftError
from .resources import ResourceGuard, ResourceLimits
from .snapshot import AlertStore, SnapshotStore


class InteractiveApp:
    def __init__(self, runtime_dir: str, config_path: str, stream_path: str):
        self.runtime_dir = runtime_dir
        self.config_path = config_path
        self.stream_path = stream_path
        self.paths = self._ensure_dirs(runtime_dir)
        with open(config_path, "r", encoding="utf-8") as handle:
            self.base_config_text = handle.read()
        self.config = read_config(self.base_config_text)
        self.stream = read_batches(stream_path)
        self.cursor = 0
        self.engine = self._new_engine(self.config)
        self.last_error: Optional[Dict[str, Any]] = None
        self.last_propagation: Optional[Dict[str, Any]] = None

    @staticmethod
    def _ensure_dirs(runtime_dir: str) -> Dict[str, str]:
        paths = {}
        for name in ("cache", "alerts", "snapshots", "reports"):
            target = os.path.join(runtime_dir, name)
            os.makedirs(target, exist_ok=True)
            paths[name] = target
        return paths

    def _new_engine(self, config: Any) -> MonitoringEngine:
        limits = ResourceLimits()
        return MonitoringEngine(
            config=config,
            cache=ContentStore(self.paths["cache"], max_entries=limits.max_cache_entries),
            snapshots=SnapshotStore(self.paths["snapshots"]),
            alerts=AlertStore(self.paths["alerts"]),
            guard=ResourceGuard(limits=limits),
        )

    # ------------------------------------------------------------- menu actions
    def next_normal_batch(self) -> Dict[str, Any]:
        if self.cursor >= len(self.stream):
            raise DriftError(
                "E_RESOURCE_LIMIT",
                "the pre-generated stream has been fully consumed",
                {"field": "stream_cursor"},
            )
        batch = self.stream[self.cursor]
        self.cursor += 1
        return self._safe_evaluate(batch)

    def inject_drift_batch(self) -> Dict[str, Any]:
        batch = generate_batch(
            batch_id=10_000 + self.cursor,
            sample_count=200,
            master_seed=20260101,
            injection=DriftInjection(
                batch_id=10_000 + self.cursor,
                mean_shift=18.0,
                variance_multiplier=2.5,
                category_shift=True,
                missing_rate=0.1,
            ),
        )
        self.cursor += 1
        return self._safe_evaluate(batch)

    def slide_again(self) -> Dict[str, Any]:
        # A normal follow-up batch moves the reference window one more step,
        # which evicts the oldest contribution and invalidates prior results.
        batch = generate_batch(
            batch_id=20_000 + self.cursor,
            sample_count=200,
            master_seed=20260101,
        )
        self.cursor += 1
        return self._safe_evaluate(batch)

    def toggle_feature(self, feature: str) -> Dict[str, Any]:
        new_features = dict(self.config.features)
        spec = new_features[feature]
        new_features[feature] = replace(spec, enabled=not spec.enabled)
        new_config = replace(self.config, features=new_features)
        propagation = self.engine.change_config(new_config)
        self.config = new_config
        self.last_propagation = propagation
        return propagation

    def retype_feature(self, feature: str, new_type: str) -> Dict[str, Any]:
        new_features = dict(self.config.features)
        spec = new_features[feature]
        new_features[feature] = replace(
            spec,
            column_type=new_type,
            methods=["proportion_dist"] if new_type == "categorical" else ["mean_shift"],
            quantiles=[] if new_type == "categorical" else spec.quantiles,
        )
        new_config = replace(self.config, features=new_features)
        propagation = self.engine.change_config(new_config)
        self.config = new_config
        self.last_propagation = propagation
        return propagation

    def _safe_evaluate(self, batch: Any) -> Dict[str, Any]:
        try:
            report = self.engine.evaluate_batch(batch)
            self.last_error = None
            return report.to_dict()
        except DriftError as exc:
            self.last_error = exc.to_dict()
            return {"error": exc.to_dict()}

    # ---------------------------------------------------------------- rendering
    def render_status(self) -> str:
        report = self.engine.last_report
        report = report.to_dict() if report is not None else None
        lines: List[str] = []
        lines.append("=" * 72)
        lines.append("FEATURE DRIFT MONITOR (local deterministic console)")
        lines.append("=" * 72)
        lines.append(f"config format={self.config.source_format} digest={self.config.definition_digest()[:12]}")
        lines.append(f"stream cursor={self.cursor}/{len(self.stream)}")
        if report is None:
            lines.append("no batch evaluated yet")
        else:
            lines.append(
                f"reference window={report['window_range']} batches={report['window_batch_ids']}"
            )
            lines.append(
                f"removed batches={report['removed_batches']} "
                f"recompute={report['recomputed_features']} reused={report['reused_features']}"
            )
            lines.append("-" * 72)
            lines.append(f"{'feature':<18}{'type':<12}{'method':<18}{'amount':>10}{'thr':>8}  severity")
            for item in report["feature_results"]:
                lines.append(
                    f"{item['feature']:<18}{item['column_type']:<12}{item['method']:<18}"
                    f"{item['drift_amount']:>10.4f}{item['threshold']:>8.3f}  {item['severity']}"
                )
            lines.append("-" * 72)
            lines.append(f"alerts in this step: {len(report['alerts'])}")
            for alert in report["alerts"]:
                lines.append(
                    f"  {alert['alert_id']} feature={alert['feature']} "
                    f"severity={alert['severity']} cache={alert['cache_key'][:10]}"
                )
            if report["degradation"]:
                lines.append(f"degradation marks: {json.dumps(report['degradation'], sort_keys=True)}")
        if self.last_propagation is not None:
            lines.append(f"last invalidation: {json.dumps(self.last_propagation, sort_keys=True)}")
        lines.append(
            "last business error: "
            + (json.dumps(self.last_error, sort_keys=True) if self.last_error else "none")
        )
        lines.append("=" * 72)
        return "\n".join(lines)

    def run(self) -> None:
        menu = (
            "\nActions:\n"
            "  1) evaluate next normal batch\n"
            "  2) inject a drift batch and inspect alerts\n"
            "  3) slide the reference window once more and observe invalidation\n"
            "  4) list all stored alerts\n"
            "  5) toggle a feature (enable/disable invalidation)\n"
            "  6) show cache hit/miss counters\n"
            "  q) quit\n"
        )
        print(self.render_status())
        while True:
            print(menu)
            choice = input("choose> ").strip().lower()
            if choice == "1":
                self.next_normal_batch()
            elif choice == "2":
                self.inject_drift_batch()
            elif choice == "3":
                self.slide_again()
            elif choice == "4":
                print(json.dumps(self.engine.alerts.all_alerts(), sort_keys=True, indent=2))
                continue
            elif choice == "5":
                feature = input("feature name> ").strip()
                if feature not in self.config.features:
                    print("unknown feature")
                    continue
                print(json.dumps(self.toggle_feature(feature), sort_keys=True, indent=2))
                continue
            elif choice == "6":
                print(
                    json.dumps(
                        {
                            "hits": self.engine.cache.hits,
                            "misses": self.engine.cache.misses,
                            "entries": self.engine.cache.entry_count(),
                        },
                        sort_keys=True,
                    )
                )
                continue
            elif choice in ("q", "quit", "exit"):
                return
            else:
                print("unknown choice")
                continue
            print(self.render_status())


def main() -> None:
    base = os.getcwd()
    app = InteractiveApp(
        runtime_dir=os.path.join(base, "runtime_app"),
        config_path=os.path.join(base, "samples", "monitor_v3.json"),
        stream_path=os.path.join(base, "samples", "data", "batch_stream.json"),
    )
    app.run()


if __name__ == "__main__":
    main()
