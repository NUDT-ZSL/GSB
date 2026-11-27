"""Top-level orchestration for query reuse and incremental maintenance."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import GENERATOR_VERSION, SCHEMA_VERSION, TABLE_COLUMNS
from .engine import (
    Executor,
    Relation,
    evaluate_reference,
    lineage_json,
    relation_blob,
    relation_digest,
    relation_from_stored,
)
from .estimate import (
    CandidateDecision,
    Estimator,
    decide_candidates,
)
from .generator import SEED_DEFAULT, generate_problem
from .model import GeneratedProblem
from .normalize import normalize_record
from .plan import (
    CseCandidate,
    build_plan,
    collect_scans,
    event_source_versions,
    extract_cse_candidates,
    plan_fingerprint,
    plan_to_dict,
    query_ids_from_refs,
    used_tables,
)
from .storage import Store
from .util import canonical_json


class InjectedInterruption(RuntimeError):
    """Raised after a batch has committed to simulate process interruption."""


@dataclass
class ReplanResult:
    decisions: list[CandidateDecision]
    affected_fingerprints: set[str]
    retired_fingerprints: set[str]
    materialized: dict[str, Relation]
    actual_cache_hits: Counter[str]

    @property
    def changed_fingerprints(self) -> set[str]:
        return set(self.affected_fingerprints) | set(self.retired_fingerprints)


def _plan_depth(node: Any) -> int:
    from .plan import Aggregate, Filter, Join, Project

    if isinstance(node, Filter):
        return 1 + _plan_depth(node.child)
    if isinstance(node, Join):
        return 1 + max(_plan_depth(node.left), _plan_depth(node.right))
    if isinstance(node, (Aggregate, Project)):
        return 1 + _plan_depth(node.child)
    return 0


class IncidentReuseKernel:
    """Durable query-reuse kernel.

    Opening an existing database resumes from its persisted version vectors.
    Setting ``reset=True`` discards all tables and starts from the seed again.
    """

    def __init__(
        self,
        db_path: str | Path = "dispatch.db",
        seed: int = SEED_DEFAULT,
        reset: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.seed = int(seed)
        self.store = Store(self.db_path)
        if reset:
            self.store.reset()
        stored_seed = self.store.get_meta("generator_seed")
        if stored_seed is None:
            self._initialize()
        elif int(stored_seed) != self.seed:
            self.store.close()
            raise ValueError(
                f"database seed is {stored_seed}, requested {self.seed}; "
                "use reset=True to replace it"
            )
        else:
            self._load_query_state()
        self._candidate_report_cache: list[dict[str, Any]] | None = None

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "IncidentReuseKernel":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Initialization

    def _initialize(self) -> None:
        problem = generate_problem(self.seed)
        with self.store.transaction():
            self.store.set_meta("schema_version", SCHEMA_VERSION)
            self.store.set_meta("generator_version", GENERATOR_VERSION)
            self.store.set_meta("generator_seed", self.seed)
            self.store.set_meta(
                "data_versions",
                {
                    "event.v1": 1,
                    "event.v2": 1,
                    "event.v3": 1,
                    "aux": 1,
                },
            )
            self.store.set_meta("last_update_seq", 0)
            self._insert_base_problem(problem)
            self._load_query_state()
            replan = self._replan(update_seq=0)
            cache_hits = self._rebuild_queries(replan, update_seq=0, force_all=True)
            self._write_ledger(replan, cache_hits, update_seq=0)
            report = self._build_consistency_report(update_seq=0)
            self.store.save_consistency_report(0, report)
            if not report["consistent"]:
                raise RuntimeError("initial incremental results failed consistency check")

    def _insert_base_problem(self, problem: GeneratedProblem) -> None:
        for version in (1, 2, 3):
            for raw in problem.base_records[version]:
                self.store.insert_raw_event(version, raw)
                self.store.insert_normalized(normalize_record(version, raw))
        self.store.replace_vehicles(problem.vehicles)
        self.store.replace_water_sources(problem.water_sources)
        for spec in problem.queries:
            self.store.upsert_query(spec, version=1, plan=build_plan(spec))
        self.store.replace_update_batches(problem.updates)

    # ------------------------------------------------------------------
    # State loading and planning

    def _load_query_state(self) -> None:
        rows = self.store.load_queries()
        self._query_rows: dict[str, dict[str, Any]] = {
            row["query_id"]: row for row in rows
        }
        self._specs: dict[str, dict[str, Any]] = {}
        self._plans: dict[str, Any] = {}
        self._query_versions: dict[str, int] = {}
        for query_id, row in self._query_rows.items():
            spec = json.loads(row["spec_json"])
            plan = build_plan(spec)
            stored_fingerprint = row["plan_fingerprint"]
            if stored_fingerprint != plan_fingerprint(plan):
                raise RuntimeError(
                    f"persisted plan fingerprint mismatch for {query_id}"
                )
            self._specs[query_id] = spec
            self._plans[query_id] = plan
            self._query_versions[query_id] = int(row["version"])
        self._candidate_report_cache = None

    def normalized_data_versions(self) -> dict[str, int]:
        value = self.store.get_meta("data_versions")
        if not isinstance(value, dict):
            raise RuntimeError("missing data_versions metadata")
        return {str(key): int(version) for key, version in value.items()}

    def _field_cardinality(self) -> dict[str, int]:
        base = self.store.table_field_cardinalities()
        result: dict[str, int] = {}
        for plan in self._plans.values():
            for alias, table in collect_scans(plan).items():
                for column in TABLE_COLUMNS[table]:
                    result[f"{alias}.{column}"] = base[f"{table}.{column}"]
        return result

    def _estimator(self) -> Estimator:
        return Estimator(
            table_rows=self.store.table_row_counts(),
            field_cardinality=self._field_cardinality(),
        )

    def _plan_data_versions(self, plan: Any) -> dict[str, int]:
        versions = self.normalized_data_versions()
        tables = used_tables(plan)
        keys: set[str] = set()
        if "incidents" in tables:
            for source_version in event_source_versions(plan):
                keys.add(f"event.v{source_version}")
        if "vehicles" in tables or "water_sources" in tables:
            keys.add("aux")
        return {key: versions[key] for key in sorted(keys) if key in versions}

    def _candidate_version_vector(
        self,
        candidate: CseCandidate,
    ) -> dict[str, Any]:
        data = self._plan_data_versions(candidate.node)
        query_ids = query_ids_from_refs(candidate.refs)
        queries = {
            query_id: self._query_versions[query_id]
            for query_id in query_ids
            if query_id in self._query_versions
        }
        return {"data": data, "queries": queries}

    def _query_version_vector(self, query_id: str) -> dict[str, Any]:
        return {
            "data": self._plan_data_versions(self._plans[query_id]),
            "query_version": self._query_versions[query_id],
        }

    # ------------------------------------------------------------------
    # CSE planning and materialization

    def _replan(self, update_seq: int) -> ReplanResult:
        old_candidates = {
            row["fingerprint"]: row for row in self.store.load_candidates()
        }
        old_materialized = self.store.load_materialized()
        candidates = extract_cse_candidates(self._plans)
        decisions = decide_candidates(candidates, self._estimator())
        self.store.replace_candidates(decisions)
        self._candidate_report_cache = None

        selected = [item for item in decisions if item.selected]
        selected_by_fp = {item.candidate.fingerprint: item for item in selected}
        selected_fps = set(selected_by_fp)

        retired = set(old_materialized) - selected_fps
        for fingerprint in retired:
            self.store.delete_materialized(fingerprint)

        materialized: dict[str, Relation] = {}
        affected: set[str] = set()
        for fingerprint, item in selected_by_fp.items():
            old_row = old_materialized.get(fingerprint)
            vector = self._candidate_version_vector(item.candidate)
            old_vector = (
                json.loads(old_row["version_vector_json"])
                if old_row is not None
                else None
            )
            if old_row is not None and old_vector == vector:
                materialized[fingerprint] = relation_from_stored(
                    old_row["blob"],
                    old_row["lineage_json"],
                )
            else:
                affected.add(fingerprint)

        executor = Executor(
            self.store,
            materialized=materialized,
            use_cache=True,
        )
        ordered_affected = sorted(
            (selected_by_fp[fingerprint] for fingerprint in affected),
            key=lambda item: (
                _plan_depth(item.candidate.node),
                item.candidate.fingerprint,
            ),
        )
        for item in ordered_affected:
            candidate = item.candidate
            relation = executor.evaluate(
                candidate.node,
                skip_cache_fp=candidate.fingerprint,
            )
            materialized[candidate.fingerprint] = relation
            self.store.save_materialized(
                candidate.fingerprint,
                relation_blob(relation),
                list(relation.columns),
                len(relation.rows),
                json.loads(lineage_json(relation)),
                self._candidate_version_vector(candidate),
                item.cost,
                update_seq,
            )

        return ReplanResult(
            decisions=decisions,
            affected_fingerprints=affected,
            retired_fingerprints=retired,
            materialized=materialized,
            actual_cache_hits=Counter(executor.cache_hits),
        )

    # ------------------------------------------------------------------
    # Query result maintenance

    def _rebuild_queries(
        self,
        replan: ReplanResult,
        update_seq: int,
        force_all: bool = False,
    ) -> Counter[str]:
        stored_results = self.store.load_query_results()
        current_refs: dict[str, set[str]] = defaultdict(set)
        for item in replan.decisions:
            for ref in item.candidate.refs:
                current_refs[ref["query_id"]].add(item.candidate.fingerprint)

        changed = replan.changed_fingerprints
        executor = Executor(
            self.store,
            materialized=replan.materialized,
            use_cache=True,
        )

        for query_id in sorted(self._plans):
            plan = self._plans[query_id]
            vector = self._query_version_vector(query_id)
            stored = stored_results.get(query_id)
            impacted = force_all or stored is None
            if stored is not None:
                impacted = impacted or (
                    int(stored["query_version"]) != vector["query_version"]
                )
                impacted = impacted or (
                    json.loads(stored["data_versions_json"]) != vector["data"]
                )
            if changed & current_refs.get(query_id, set()):
                impacted = True

            if not impacted:
                continue
            relation = executor.evaluate(plan)
            self.store.save_query_result(
                query_id,
                relation_blob(relation),
                list(relation.columns),
                len(relation.rows),
                json.loads(lineage_json(relation)),
                vector["query_version"],
                vector["data"],
                update_seq,
            )
        return Counter(executor.cache_hits)

    # ------------------------------------------------------------------
    # Accounting

    def _write_ledger(
        self,
        replan: ReplanResult,
        query_cache_hits: Counter[str],
        update_seq: int,
    ) -> None:
        actual_hits = Counter(replan.actual_cache_hits)
        actual_hits.update(query_cache_hits)
        by_fp = {
            item.candidate.fingerprint: item for item in replan.decisions
        }
        entries: list[dict[str, Any]] = []
        incident_index: list[tuple[str, str, int, str]] = []
        for fingerprint in sorted(by_fp):
            item = by_fp[fingerprint]
            candidate = item.candidate
            selected = bool(item.selected)
            reuse_count = max(0, candidate.reference_count - 1) if selected else 0
            saved_cost = reuse_count * item.cost if selected else 0
            relation = replan.materialized.get(fingerprint)
            materialized_rows = len(relation.rows) if relation is not None else 0
            entries.append(
                {
                    "fingerprint": fingerprint,
                    "candidate_id": candidate.candidate_id,
                    "selected": selected,
                    "reference_count": candidate.reference_count,
                    "reuse_count": reuse_count,
                    "materialized_rows": materialized_rows,
                    "cost": item.cost,
                    "saved_cost": saved_cost,
                    "actual_cache_hits": int(actual_hits.get(fingerprint, 0)),
                    "query_refs": list(candidate.refs),
                    "decision_reason": item.reason,
                    "update_seq": update_seq,
                }
            )
            if relation is None:
                continue
            scope = relation.scope()
            query_ids = sorted({ref["query_id"] for ref in candidate.refs})
            for query_id in query_ids:
                for incident_id, occurred_at_ms in scope:
                    incident_index.append(
                        (
                            candidate.candidate_id,
                            incident_id,
                            occurred_at_ms,
                            query_id,
                        )
                    )
        self.store.replace_ledger(entries, incident_index)

    # ------------------------------------------------------------------
    # Consistency

    def _build_consistency_report(self, update_seq: int) -> dict[str, Any]:
        stored_results = self.store.load_query_results()
        candidate_ids: dict[str, list[str]] = {}
        for row in self.store.load_candidates():
            for ref in json.loads(row["refs_json"]):
                candidate_ids.setdefault(ref["query_id"], []).append(
                    row["candidate_id"]
                )
        query_reports: list[dict[str, Any]] = []
        differences: list[dict[str, Any]] = []
        for query_id in sorted(self._plans):
            stored = stored_results.get(query_id)
            involved = sorted(candidate_ids.get(query_id, []))
            if stored is None:
                difference = {
                    "query_id": query_id,
                    "issue": "incremental result missing",
                    "involved_cse": involved,
                }
                differences.append(difference)
                query_reports.append(
                    {
                        "query_id": query_id,
                        "consistent": False,
                        "involved_cse": involved,
                    }
                )
                continue
            incremental = relation_from_stored(
                stored["blob"],
                stored["lineage_json"],
            )
            reference = evaluate_reference(self.store, self._plans[query_id])
            incremental_digest = relation_digest(incremental)
            reference_digest = relation_digest(reference)
            consistent = incremental_digest == reference_digest
            query_report = {
                "query_id": query_id,
                "consistent": consistent,
                "incremental_digest": incremental_digest,
                "reference_digest": reference_digest,
                "incremental_rows": len(incremental.rows),
                "reference_rows": len(reference.rows),
                "involved_cse": involved,
            }
            query_reports.append(query_report)
            if not consistent:
                difference = {
                    "query_id": query_id,
                    "issue": "result digest mismatch",
                    "involved_cse": involved,
                    "incremental_digest": incremental_digest,
                    "reference_digest": reference_digest,
                }
                difference.update(
                    self._row_difference(incremental, reference)
                )
                differences.append(difference)
        return {
            "report_version": 1,
            "update_seq": update_seq,
            "consistent": not differences,
            "queries": query_reports,
            "differences": differences,
        }

    @staticmethod
    def _row_difference(
        incremental: Relation,
        reference: Relation,
    ) -> dict[str, Any]:
        def keyed(relation: Relation) -> Counter[str]:
            return Counter(canonical_json(row) for row in relation.rows)

        incremental_rows = keyed(incremental)
        reference_rows = keyed(reference)
        missing = reference_rows - incremental_rows
        extra = incremental_rows - reference_rows
        return {
            "incremental_columns": list(incremental.columns),
            "reference_columns": list(reference.columns),
            "missing_row_count": sum(missing.values()),
            "extra_row_count": sum(extra.values()),
            "missing_rows_sample": list(sorted(missing))[:20],
            "extra_rows_sample": list(sorted(extra))[:20],
        }

    # ------------------------------------------------------------------
    # Updates

    def apply_next_update(
        self,
        interrupt_after_commit: bool = False,
    ) -> dict[str, Any] | None:
        pending = self.store.next_pending_update()
        if pending is None:
            return None

        try:
            with self.store.transaction():
                old_query_versions = dict(self._query_versions)
                old_data_versions = self.normalized_data_versions()
                last_update_seq = int(self.store.get_meta("last_update_seq", 0)) + 1
                self.store.set_meta("last_update_seq", last_update_seq)

                if pending["kind"] == "append_events":
                    payload = pending["payload"]
                    version = int(payload["source_version"])
                    for raw in payload["events"]:
                        self.store.insert_raw_event(version, raw)
                        self.store.insert_normalized(normalize_record(version, raw))
                    key = f"event.v{version}"
                    old_data_versions[key] = old_data_versions.get(key, 0) + 1
                    self.store.set_meta("data_versions", old_data_versions)
                elif pending["kind"] == "edit_query":
                    spec = pending["payload"]["query"]
                    query_id = str(spec["query_id"])
                    row = self.store.get_query(query_id)
                    if row is None:
                        raise KeyError(f"query edit targets unknown query: {query_id}")
                    new_version = int(row["version"]) + 1
                    self.store.upsert_query(
                        spec,
                        version=new_version,
                        plan=build_plan(spec),
                    )
                else:
                    raise ValueError(f"unknown update kind: {pending['kind']}")

                self._load_query_state()
                replan = self._replan(update_seq=last_update_seq)
                cache_hits = self._rebuild_queries(
                    replan,
                    update_seq=last_update_seq,
                )
                self._write_ledger(replan, cache_hits, last_update_seq)
                report = self._build_consistency_report(last_update_seq)
                self.store.save_consistency_report(last_update_seq, report)
                self.store.mark_update_applied(pending["seq"], last_update_seq)

                summary = {
                    "seq": pending["seq"],
                    "update_seq": last_update_seq,
                    "kind": pending["kind"],
                    "query_versions_before": old_query_versions,
                    "query_versions_after": dict(self._query_versions),
                    "affected_cse": sorted(
                        self._candidate_id_for_fingerprint(fingerprint)
                        for fingerprint in replan.affected_fingerprints
                    ),
                    "retired_cse": sorted(
                        self._candidate_id_for_fingerprint(fingerprint)
                        for fingerprint in replan.retired_fingerprints
                    ),
                    "consistent": report["consistent"],
                }
        except BaseException:
            # The transaction rolled back; discard the tentative in-memory
            # query state so the same kernel instance remains usable.
            self._load_query_state()
            raise

        if interrupt_after_commit:
            raise InjectedInterruption(
                f"committed batch {summary['seq']} and stopped at batch boundary"
            )
        return summary

    def apply_updates(
        self,
        max_batches: int | None = None,
        interrupt_after: int | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        while max_batches is None or len(results) < max_batches:
            should_interrupt = (
                interrupt_after is not None
                and len(results) + 1 == interrupt_after
            )
            result = self.apply_next_update(
                interrupt_after_commit=should_interrupt
            )
            if result is None:
                break
            results.append(result)
        return results

    def resume_updates(
        self,
        max_batches: int | None = None,
        interrupt_after: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.apply_updates(
            max_batches=max_batches,
            interrupt_after=interrupt_after,
        )

    def _candidate_id_for_fingerprint(self, fingerprint: str) -> str:
        from .util import short_digest

        return f"cse_{short_digest(fingerprint)}"

    # ------------------------------------------------------------------
    # Public reports

    def status(self) -> dict[str, Any]:
        candidates = self.store.load_candidates()
        updates = self.store.list_updates()
        return {
            "database": str(self.db_path),
            "seed": self.seed,
            "schema_version": SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "data_versions": self.normalized_data_versions(),
            "query_versions": dict(sorted(self._query_versions.items())),
            "raw_event_counts": self.store.raw_counts(),
            "normalized_event_count": self.store.table_row_counts()["incidents"],
            "candidate_count": len(candidates),
            "materialized_candidate_count": sum(
                1 for row in candidates if row["selected"]
            ),
            "pending_update_count": sum(
                1 for row in updates if row["status"] == "pending"
            ),
            "last_update_seq": int(self.store.get_meta("last_update_seq", 0)),
            "updates": [
                {
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "applied_in_update": row["applied_in_update"],
                }
                for row in updates
            ],
        }

    def candidate_report(self) -> list[dict[str, Any]]:
        if self._candidate_report_cache is not None:
            return self._candidate_report_cache
        materialized = self.store.load_materialized()
        result: list[dict[str, Any]] = []
        for row in self.store.load_candidates():
            materialized_row = materialized.get(row["fingerprint"])
            result.append(
                {
                    "candidate_id": row["candidate_id"],
                    "fingerprint": row["fingerprint"],
                    "refs": json.loads(row["refs_json"]),
                    "reference_count": int(row["reference_count"]),
                    "estimated_rows": int(row["estimated_rows"]),
                    "cost": int(row["cost"]),
                    "weight": int(row["weight"]),
                    "rank": int(row["rank"]),
                    "selected": bool(row["selected"]),
                    "reason": row["reason"],
                    "plan": json.loads(row["plan_json"]),
                    "version_vector": (
                        json.loads(materialized_row["version_vector_json"])
                        if materialized_row is not None
                        else None
                    ),
                }
            )
        self._candidate_report_cache = result
        return result

    def plan_report(self) -> list[dict[str, Any]]:
        return [
            {
                "query_id": query_id,
                "version": self._query_versions[query_id],
                "plan_fingerprint": plan_fingerprint(self._plans[query_id]),
                "plan": plan_to_dict(self._plans[query_id]),
            }
            for query_id in sorted(self._plans)
        ]

    def query_results(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for query_id, row in sorted(self.store.load_query_results().items()):
            payload = json.loads(bytes(row["blob"]).decode("utf-8"))
            result[query_id] = {
                "query_id": query_id,
                "query_version": int(row["query_version"]),
                "data_versions": json.loads(row["data_versions_json"]),
                "columns": payload["columns"],
                "rows": payload["rows"],
                "row_count": int(row["row_count"]),
                "blob_sha256": hashlib.sha256(
                    bytes(row["blob"])
                ).hexdigest(),
            }
        return result

    def version_vectors(self) -> dict[str, Any]:
        candidate_ids = {
            row["fingerprint"]: row["candidate_id"]
            for row in self.store.load_candidates()
        }
        materialized = self.store.load_materialized()
        candidates = {
            candidate_ids[fingerprint]: {
                "fingerprint": fingerprint,
                "vector": json.loads(row["version_vector_json"]),
            }
            for fingerprint, row in sorted(materialized.items())
        }
        queries = {
            query_id: self._query_version_vector(query_id)
            for query_id in sorted(self._plans)
        }
        return {
            "data_versions": self.normalized_data_versions(),
            "candidates": candidates,
            "queries": queries,
        }

    def ledger(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self.store.load_ledger():
            item = dict(row)
            item["selected"] = bool(item["selected"])
            item["query_refs"] = json.loads(item.pop("query_refs_json"))
            item["net_saved_cost"] = (
                int(item["saved_cost"]) - int(item["materialized_rows"])
                if item["selected"]
                else 0
            )
            result.append(item)
        return result

    def ledger_lookup(
        self,
        incident_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.lookup_ledger(
            incident_id=incident_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def normalization_report(
        self,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        if incident_id is None:
            rows = self.store.conn.execute(
                "SELECT incident_id FROM incidents ORDER BY incident_id"
            ).fetchall()
            events = [
                self.normalization_report(row["incident_id"])["events"][0]
                for row in rows
            ]
            return {
                "field_count": len(events[0]["fields"]) if events else 0,
                "events": events,
            }
        event = self.store.normalized_event(incident_id)
        if event is None:
            raise KeyError(f"unknown incident: {incident_id}")
        provenance = self.store.provenance(incident_id)
        fields = {
            (
                "persons"
                if row["field_name"] == "persons_json"
                else row["field_name"]
            ): {
                "value": (
                    event["persons"]
                    if row["field_name"] == "persons_json"
                    else event.get(row["field_name"])
                ),
                "source_version": int(row["source_version"]),
                "source_path": row["source_path"],
                "transform": row["transform"],
            }
            for row in provenance
        }
        fields["source_version"] = {
            "value": int(event["source_version"]),
            "source_version": int(event["source_version"]),
            "source_path": "$schema_version",
            "transform": "schema_version_tag",
        }
        return {"events": [{"incident_id": incident_id, "fields": fields}]}

    def verify(self) -> dict[str, Any]:
        update_seq = int(self.store.get_meta("last_update_seq", 0))
        report = self._build_consistency_report(update_seq)
        self.store.save_consistency_report(update_seq, report)
        return report

    def latest_consistency_report(self) -> dict[str, Any] | None:
        update_seq = int(self.store.get_meta("last_update_seq", 0))
        return self.store.load_consistency_report(update_seq)

    def materialized_bytes(self) -> dict[str, bytes]:
        return {
            fingerprint: bytes(row["blob"])
            for fingerprint, row in self.store.load_materialized().items()
        }
