"""Read-model queries with pre-computed cache and exact-recompute verification."""

from __future__ import annotations
from typing import Any, Dict, List, Optional

from . import aggregation as agg
from .errors import BusinessError, E_CACHE_MISMATCH, E_NOT_FOUND
from .repository import Repository, payload_digest


def _cache_key(kind: str, slo_name: str, version: int, watermark: int, window_no: Optional[int]) -> str:
    window_part = "all" if window_no is None else str(window_no)
    return f"{kind}:{slo_name}:v{version}:w{watermark}:win{window_part}"


def _diff_fields(expected: Any, actual: Any, path: str = "") -> Optional[Dict[str, Any]]:
    """Depth-first comparison returning the first differing field path."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = sorted(set(expected) | set(actual))
        for key in keys:
            if key not in expected or key not in actual:
                return {
                    "field": f"{path}.{key}" if path else key,
                    "cached": actual.get(key),
                    "recomputed": expected.get(key),
                }
            mismatch = _diff_fields(expected[key], actual[key], f"{path}.{key}" if path else key)
            if mismatch:
                return mismatch
        return None
    if expected != actual:
        return {"field": path or "$", "cached": actual, "recomputed": expected}
    return None


class QueryService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # ----------------------------------------------------------- recomputation
    def recompute_window(self, slo_name: str, window_no: int) -> Dict[str, Any]:
        """Recompute one window report from the durable event table."""
        slo = self.repo.get_slo(slo_name)
        # Only accepted (non-late) events may feed a window conclusion; the
        # durable ingested_event table is the source of that decision.
        events = self.repo.accepted_events(slo_name, slo.version, window_no)
        return agg.compute_window(slo, window_no, events)

    def recompute_compliance(self, slo_name: str, at_window: Optional[int] = None) -> Dict[str, Any]:
        slo = self.repo.get_slo(slo_name)
        windows = self.repo.list_windows(slo.name, slo.version)
        windows = [w for w in windows if w.get("status") != "empty"]
        if at_window is not None:
            period_numbers = set(agg.compliance_window_numbers(at_window, slo))
            windows = [w for w in windows if w["window_no"] in period_numbers]
        else:
            period_numbers = set(
                agg.compliance_window_numbers(
                    max((w["window_no"] for w in windows), default=0), slo
                )
            )
            windows = [w for w in windows if w["window_no"] in period_numbers]
        return agg.aggregate_compliance(slo, windows)

    # ------------------------------------------------------------- window query
    def query_window(
        self, slo_name: str, window_no: int, *, verify: bool = False
    ) -> Dict[str, Any]:
        slo = self.repo.get_slo(slo_name)
        watermark = self._watermark(slo_name)
        key = _cache_key("window", slo_name, slo.version, watermark, window_no)

        cached = self.repo.cache_get(key)
        if cached is None:
            cached = self._load_committed_window(slo_name, slo.version, window_no)
            self.repo.cache_put(
                key, slo_name, slo.version, window_no, watermark, cached,
                self._current_tick(slo_name),
            )

        response = {"cache_hit": True, "source": "cache", "report": cached}
        if verify:
            mismatch = self._verify_window(slo_name, window_no, cached)
            response["verification"] = mismatch or {"status": "match"}
        return response

    def _load_committed_window(self, slo_name: str, version: int, window_no: int) -> Dict[str, Any]:
        payload = self.repo.get_window_payload(slo_name, version, window_no)
        if payload is None:
            raise BusinessError(
                E_NOT_FOUND,
                "window has not been committed yet",
                fields={"slo_name": slo_name, "window_no": window_no},
            )
        return payload

    def _verify_window(
        self, slo_name: str, window_no: int, cached: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        committed = self.repo.get_window_payload(
            slo_name, self.repo.get_slo(slo_name).version, window_no
        )
        try:
            recomputed = self.recompute_window(slo_name, window_no)
        except BusinessError:
            recomputed = committed
        # Compare both the committed row and the fresh recomputation.
        mismatch = _diff_fields(recomputed, cached)
        if mismatch is None and committed is not None:
            mismatch = _diff_fields(committed, cached)
        if mismatch is not None:
            error = BusinessError(
                E_CACHE_MISMATCH,
                "cached window report differs from exact recomputation",
                fields={
                    "slo_name": slo_name,
                    "window_no": window_no,
                    "first_diff_field": mismatch["field"],
                    "cached_value": mismatch["cached"],
                    "recomputed_value": mismatch["recomputed"],
                    "cached_digest": payload_digest(cached),
                    "recomputed_digest": payload_digest(recomputed),
                },
            )
            self.repo.log_error(error.code, error.message, error.fields)
            raise error
        return None

    # -------------------------------------------------------- compliance query
    def query_burn(
        self, slo_name: str, *, at_window: Optional[int] = None, verify: bool = False
    ) -> Dict[str, Any]:
        slo = self.repo.get_slo(slo_name)
        watermark = self._watermark(slo_name)
        key = _cache_key("burn", slo_name, slo.version, watermark, at_window)

        cached = self.repo.cache_get(key)
        if cached is None:
            cached = self.recompute_compliance(slo_name, at_window)
            self.repo.cache_put(
                key, slo_name, slo.version, at_window, watermark, cached,
                self._current_tick(slo_name),
            )

        response = {"cache_hit": True, "source": "cache", "conclusion": cached}
        if verify:
            recomputed = self.recompute_compliance(slo_name, at_window)
            mismatch = _diff_fields(recomputed, cached)
            if mismatch is not None:
                error = BusinessError(
                    E_CACHE_MISMATCH,
                    "cached burn conclusion differs from exact recomputation",
                    fields={
                        "slo_name": slo_name,
                        "first_diff_field": mismatch["field"],
                        "cached_value": mismatch["cached"],
                        "recomputed_value": mismatch["recomputed"],
                    },
                )
                self.repo.log_error(error.code, error.message, error.fields)
                raise error
            response["verification"] = {"status": "match"}
        return response

    # ------------------------------------------------------------- status view
    def status(self) -> Dict[str, Any]:
        slos = self.repo.current_slos()
        report: Dict[str, Any] = {"slos": [], "errors": self.repo.list_errors()}
        for slo in sorted(slos, key=lambda item: item.name):
            watermark = self.repo.get_watermark(slo.name)
            windows = self.repo.list_windows(slo.name, slo.version)
            alert = self.repo.get_alert_state(slo.name)
            report["slos"].append(
                {
                    "slo_name": slo.name,
                    "service": slo.service,
                    "version": slo.version,
                    "window_count": len(windows),
                    "watermark": watermark,
                    "alert_state": (
                        {
                            "state": alert["state"],
                            "fire_streak": alert["fire_streak"],
                            "release_streak": alert["release_streak"],
                            "since_window": alert["since_window"],
                            "updated_window": alert["updated_window"],
                        }
                        if alert
                        else {"state": "ok", "fire_streak": 0, "release_streak": 0}
                    ),
                    "late_events": self.repo.list_late_events(slo.name, slo.version),
                    "alert_events": self.repo.list_alert_events(slo.name, slo.version),
                }
            )
        return report

    def verify_all(self) -> Dict[str, Any]:
        """Verify every cached entry against exact recomputation (stable order)."""
        results: List[Dict[str, Any]] = []
        for slo in sorted(self.repo.current_slos(), key=lambda item: item.name):
            windows = self.repo.list_windows(slo.name, slo.version)
            for payload in sorted(windows, key=lambda item: item["window_no"]):
                if payload.get("status") == "empty":
                    results.append(
                        {
                            "slo_name": slo.name,
                            "window_no": payload["window_no"],
                            "status": "skipped_empty",
                        }
                    )
                    continue
                window_no = payload["window_no"]
                watermark = self._watermark(slo.name)
                key = _cache_key("window", slo.name, slo.version, watermark, window_no)
                cached = self.repo.cache_get(key) or payload
                try:
                    self._verify_window(slo.name, window_no, cached)
                    results.append(
                        {"slo_name": slo.name, "window_no": window_no, "status": "match"}
                    )
                except BusinessError as error:
                    results.append(
                        {
                            "slo_name": slo.name,
                            "window_no": window_no,
                            "status": "mismatch",
                            "error": error.to_dict(),
                        }
                    )
        return {"results": sorted(results, key=lambda item: (item["slo_name"], item["window_no"]))}

    def _watermark(self, slo_name: str) -> int:
        watermark = self.repo.get_watermark(slo_name)
        if watermark is None:
            raise BusinessError(
                E_NOT_FOUND, "SLO has not been aggregated yet", fields={"slo_name": slo_name}
            )
        return watermark["committed_seq"]

    def _current_tick(self, slo_name: str) -> int:
        watermark = self.repo.get_watermark(slo_name)
        return watermark["committed_clock"] if watermark else 0
