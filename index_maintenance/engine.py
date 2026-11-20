"""Secondary-index maintenance, rebuild adjudication, and invalidation closure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .btree import BTree, key_token
from .canonical import canonical_json, fixed_units, sha256_json
from .errors import (
    BusinessError,
    E_BAD_OP,
    E_DUP_PK,
    E_INDEX_DIGEST_MISMATCH,
    E_UNIQUE_VIOLATION,
)
from .sample import (
    IndexDeclaration,
    Manifest,
    Operation,
    all_changed_columns,
)


@dataclass
class IndexState:
    name: str
    kind: str
    columns: tuple[str, ...]
    unique: bool
    covering: tuple[str, ...]
    tree: BTree
    decision: dict[str, Any]
    last_change_seq: int
    last_change_clock_ms: int

    def snapshot(self, scale: int, rounding: str) -> dict[str, Any]:
        tree_snapshot = self.tree.snapshot(scale=scale, rounding=rounding)
        core = {
            "name": self.name,
            "kind": self.kind,
            "columns": list(self.columns),
            "unique": self.unique,
            "covering": list(self.covering),
            "tree": tree_snapshot,
            "decision": self.decision,
            "last_change_seq": self.last_change_seq,
            "last_change_clock_ms": self.last_change_clock_ms,
        }
        core["digest"] = sha256_json(core)
        return core


@dataclass
class OperationOutcome:
    seq: int
    affected_indexes: tuple[str, ...]
    changed_snapshots: tuple[str, ...]
    split_events: tuple[dict[str, Any], ...]
    invalidations: tuple[dict[str, Any], ...]
    decisions: dict[str, dict[str, Any]]
    primary_changed: bool


def _row_pk(row: dict[str, Any], manifest: Manifest) -> dict[str, Any]:
    return {column: row[column] for column in manifest.schema.primary_key}


def _row_key(row: dict[str, Any], manifest: Manifest) -> str:
    return canonical_json(_row_pk(row, manifest))


def _pk_list(row: dict[str, Any], manifest: Manifest) -> list[Any]:
    return [row[column] for column in manifest.schema.primary_key]


def _column_types(
    manifest: Manifest,
    columns: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(manifest.schema.column_map[column].type for column in columns)


class MaintenanceEngine:
    """Mutable in-memory engine for one table and all of its indexes."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        snapshots: dict[str, dict[str, Any]],
        rows: dict[str, dict[str, Any]],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        verify_integrity: bool = True,
    ) -> None:
        self.manifest = manifest
        self.config = manifest.config
        self.event_callback = event_callback
        self.progress_callback = progress_callback
        self.rows = dict(rows)
        self.split_events: list[dict[str, Any]] = []
        self.invalidations: list[dict[str, Any]] = []
        self.current_seq: int | None = None
        self.primary = self._load_state(
            "pk",
            snapshots["pk"],
            verify_integrity=verify_integrity,
        )
        self.secondary: dict[str, IndexState] = {}
        for declaration in sorted(manifest.indexes, key=lambda item: item.name):
            self.secondary[declaration.name] = self._load_state(
                declaration.name,
                snapshots[declaration.name],
                verify_integrity=verify_integrity,
            )

    def _new_tree(
        self,
        name: str,
        *,
        columns: tuple[str, ...],
        chain_version: int = 0,
        split_count: int = 0,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> BTree:
        return BTree(
            name,
            capacity=self.config.page_capacity,
            split_factor=self.config.split_factor,
            max_height=self.config.max_tree_height,
            key_types=_column_types(self.manifest, columns),
            pk_types=_column_types(self.manifest, self.manifest.schema.primary_key),
            chain_version=chain_version,
            split_count=split_count,
            event_callback=event_callback,
        )

    def _load_state(
        self,
        name: str,
        snapshot: dict[str, Any],
        *,
        verify_integrity: bool,
    ) -> IndexState:
        core = dict(snapshot)
        stored_digest = core.pop("digest", None)
        actual_digest = sha256_json(core)
        if stored_digest != actual_digest:
            raise BusinessError(
                E_INDEX_DIGEST_MISMATCH,
                "index snapshot digest does not match its content",
                {
                    "index": name,
                    "stored": stored_digest,
                    "actual": actual_digest,
                },
            )
        tree_snapshot = snapshot["tree"]
        tree_core = dict(tree_snapshot)
        tree_digest = tree_core.pop("digest", None)
        if tree_digest != sha256_json(tree_core):
            raise BusinessError(
                E_INDEX_DIGEST_MISMATCH,
                "tree snapshot digest does not match its content",
                {"index": name},
            )
        for page in tree_snapshot.get("pages", []):
            page_digest = tree_snapshot.get("page_digests", {}).get(page.get("page_id"))
            if page_digest != sha256_json(page):
                raise BusinessError(
                    E_INDEX_DIGEST_MISMATCH,
                    "page digest does not match page content",
                    {"index": name, "page_id": page.get("page_id")},
                )
        tree = BTree.from_snapshot(
            tree_snapshot,
            capacity=self.config.page_capacity,
            split_factor=self.config.split_factor,
            max_height=self.config.max_tree_height,
            key_types=_column_types(self.manifest, tuple(snapshot["columns"])),
            pk_types=_column_types(
                self.manifest,
                self.manifest.schema.primary_key,
            ),
            event_callback=self.event_callback,
        )
        if verify_integrity:
            tree.integrity_check()
        return IndexState(
            name=name,
            kind=snapshot["kind"],
            columns=tuple(snapshot["columns"]),
            unique=bool(snapshot["unique"]),
            covering=tuple(snapshot["covering"]),
            tree=tree,
            decision=dict(snapshot["decision"]),
            last_change_seq=int(snapshot["last_change_seq"]),
            last_change_clock_ms=int(snapshot["last_change_clock_ms"]),
        )

    @classmethod
    def initial(
        cls,
        manifest: Manifest,
        *,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> "MaintenanceEngine":
        snapshots: dict[str, dict[str, Any]] = {}
        primary_tree = BTree(
            "pk",
            capacity=manifest.config.page_capacity,
            split_factor=manifest.config.split_factor,
            max_height=manifest.config.max_tree_height,
            key_types=_column_types(manifest, manifest.schema.primary_key),
            pk_types=_column_types(manifest, manifest.schema.primary_key),
            event_callback=event_callback,
        )
        primary_tree.ensure_root()
        primary = IndexState(
            name="pk",
            kind="primary",
            columns=manifest.schema.primary_key,
            unique=True,
            covering=(),
            tree=primary_tree,
            decision={
                "mode": "initial",
                "seq": 0,
                "clock_ms": manifest.config.logical_start_ms,
            },
            last_change_seq=0,
            last_change_clock_ms=manifest.config.logical_start_ms,
        )
        snapshots["pk"] = primary.snapshot(
            manifest.config.fixed_scale,
            manifest.config.rounding,
        )
        for declaration in sorted(manifest.indexes, key=lambda item: item.name):
            tree = BTree(
                declaration.name,
                capacity=manifest.config.page_capacity,
                split_factor=manifest.config.split_factor,
                max_height=manifest.config.max_tree_height,
                key_types=_column_types(manifest, declaration.columns),
                pk_types=_column_types(manifest, manifest.schema.primary_key),
                event_callback=event_callback,
            )
            tree.ensure_root()
            state = IndexState(
                name=declaration.name,
                kind="secondary",
                columns=declaration.columns,
                unique=declaration.unique,
                covering=declaration.covering,
                tree=tree,
                decision={
                    "mode": "initial",
                    "seq": 0,
                    "clock_ms": manifest.config.logical_start_ms,
                },
                last_change_seq=0,
                last_change_clock_ms=manifest.config.logical_start_ms,
            )
            snapshots[declaration.name] = state.snapshot(
                manifest.config.fixed_scale,
                manifest.config.rounding,
            )
        return cls(
            manifest,
            snapshots=snapshots,
            rows={},
            event_callback=event_callback,
        )

    def all_snapshots(self) -> dict[str, dict[str, Any]]:
        result = {
            "pk": self.primary.snapshot(
                self.config.fixed_scale,
                self.config.rounding,
            )
        }
        for name, state in self.secondary.items():
            result[name] = state.snapshot(
                self.config.fixed_scale,
                self.config.rounding,
            )
        return result

    def snapshot_for(self, name: str) -> dict[str, Any]:
        if name == "pk":
            return self.primary.snapshot(self.config.fixed_scale, self.config.rounding)
        return self.secondary[name].snapshot(
            self.config.fixed_scale,
            self.config.rounding,
        )

    def _thresholds(self, index_name: str) -> tuple[int, int, str]:
        override = self.config.per_index_thresholds.get(index_name, {})
        return (
            override.get("max_pending_items", self.config.max_pending_items),
            override.get("max_touched_pages", self.config.max_touched_pages),
            "per_index" if override else "global",
        )

    def _index_item(
        self,
        declaration: IndexDeclaration,
        row: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        key = [row[column] for column in declaration.columns]
        item = {
            "pk": _pk_list(row, self.manifest),
            "cover": [row[column] for column in declaration.covering],
        }
        return key, item

    def _primary_item(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"pk": _pk_list(row, self.manifest), "cover": []}

    def _check_table_unique(
        self,
        candidate: dict[str, Any],
        *,
        seq: int,
        exclude_key: str | None,
    ) -> None:
        for constraint in self.manifest.schema.unique_constraints:
            signature = tuple(candidate[column] for column in constraint)
            for row_key, row in self.rows.items():
                if row_key == exclude_key:
                    continue
                if tuple(row[column] for column in constraint) == signature:
                    raise BusinessError(
                        E_UNIQUE_VIOLATION,
                        "table unique constraint would be violated",
                        {
                            "seq": seq,
                            "constraint": list(constraint),
                            "value": list(signature),
                            "conflicting_pk": _row_pk(row, self.manifest),
                        },
                    )

    def _check_secondary_unique(
        self,
        declaration: IndexDeclaration,
        row: dict[str, Any],
        *,
        seq: int,
    ) -> None:
        if not declaration.unique:
            return
        key, item = self._index_item(declaration, row)
        if any(value is None for value in key):
            return
        group = self.secondary[declaration.name].tree.get_group(key)
        if group is None:
            return
        for existing in group["items"]:
            if existing["pk"] == item["pk"]:
                continue
            raise BusinessError(
                E_UNIQUE_VIOLATION,
                "secondary unique index would be violated",
                {
                    "seq": seq,
                    "index": declaration.name,
                    "key": key,
                    "conflicting_pk": existing["pk"],
                },
            )

    def _clone_tree(self, tree: BTree) -> BTree:
        snapshot = tree.snapshot(
            scale=self.config.fixed_scale,
            rounding=self.config.rounding,
        )
        clone = BTree.from_snapshot(
            snapshot,
            capacity=self.config.page_capacity,
            split_factor=self.config.split_factor,
            max_height=self.config.max_tree_height,
            key_types=tree.key_types,
            pk_types=tree.pk_types,
        )
        clone.current_seq = self.current_seq
        clone.chain_version = tree.chain_version + 1
        return clone

    def _simulate_item_change(
        self,
        tree: BTree,
        *,
        old_key: list[Any] | None,
        old_item: dict[str, Any] | None,
        new_key: list[Any] | None,
        new_item: dict[str, Any] | None,
    ) -> int:
        clone = self._clone_tree(tree)
        before_splits = clone.split_count
        if old_key is not None and old_item is not None:
            if new_key is not None and new_key == old_key:
                clone.replace_item(old_key, new_item)
            else:
                clone.remove_item(old_key, old_item["pk"])
                if new_key is not None and new_item is not None:
                    clone.insert_item(new_key, new_item)
        elif new_key is not None and new_item is not None:
            clone.insert_item(new_key, new_item)
        return clone.split_count - before_splits

    def _precision_record(
        self,
        *,
        pending_items: int,
        touched_pages: int,
        max_pending_items: int,
        max_touched_pages: int,
        before_groups: int,
        after_groups: int,
        dependence: list[str],
    ) -> dict[str, Any]:
        scale = self.config.fixed_scale
        rounding = self.config.rounding
        before_units = fixed_units(before_groups, scale, rounding)
        after_units = fixed_units(after_groups, scale, rounding)
        delta_units = abs(after_units - before_units)
        pending_delta = pending_items - max_pending_items
        touched_delta = touched_pages - max_touched_pages
        sensitive = (
            dependence
            and delta_units < self.config.tolerance_units
            and self.config.tolerance_units > 0
        )
        return {
            "precision_sensitive": bool(sensitive),
            "decision_depends_on": dependence,
            "group_count_before_units": before_units,
            "group_count_after_units": after_units,
            "group_count_delta_units": delta_units,
            "tolerance_units": self.config.tolerance_units,
            "fixed_scale": scale,
            "rounding": rounding,
            "pending_items_delta_units": fixed_units(
                pending_delta, scale, rounding
            ),
            "touched_pages_delta_units": fixed_units(
                touched_delta, scale, rounding
            ),
            "error_source_chain": [
                "column_values",
                "grouped_index_items",
                f"fixed_scale:{scale}",
                f"rounding:{rounding}",
                f"tolerance_units:{self.config.tolerance_units}",
            ],
        }

    def _bulk_rebuild(
        self,
        state: IndexState,
    ) -> tuple[BTree, int, list[dict[str, Any]]]:
        tree = self._new_tree(
            state.name,
            columns=state.columns,
            chain_version=state.tree.chain_version + 1,
            split_count=state.tree.split_count,
            event_callback=self.event_callback,
        )
        tree.current_seq = self.current_seq
        rows = list(self.rows.values())
        if state.kind == "primary":
            keyed_rows = [
                (
                    _pk_list(row, self.manifest),
                    self._primary_item(row),
                )
                for row in rows
            ]
        else:
            declaration = self.manifest.index_map[state.name]
            keyed_rows = [
                self._index_item(declaration, row)
                for row in rows
            ]
        keyed_rows.sort(
            key=lambda item: (
                key_token(item[0], tree.key_types),
                key_token(item[1]["pk"], tree.pk_types),
            )
        )
        for key, item in keyed_rows:
            tree.insert_item(key, item)
        tree.ensure_root()
        new_splits = tree.split_count - state.tree.split_count
        events = list(tree.events)
        tree.events = []
        return tree, new_splits, events

    def _invalidation(
        self,
        *,
        seq: int,
        index_name: str,
        kind: str,
        action: str,
        path: list[Any],
        reason: str,
        ref: Any,
        safe_reuse: bool,
    ) -> dict[str, Any]:
        return {
            "seq": seq,
            "index": index_name,
            "kind": kind,
            "action": action,
            "path": path,
            "reason": reason,
            "ref": ref,
            "safe_reuse": safe_reuse,
            "resolved": True,
        }

    def apply_operation(self, operation: Operation) -> OperationOutcome:
        self.current_seq = operation.seq
        self.split_events = []
        self.invalidations = []
        seq = operation.seq
        row_key = canonical_json(operation.pk)
        if operation.op_type == "insert":
            if row_key in self.rows:
                raise BusinessError(
                    E_DUP_PK,
                    "insert operation reuses an existing primary key",
                    {"seq": seq, "pk": operation.pk},
                )
            assert operation.after is not None
            self._check_table_unique(
                operation.after,
                seq=seq,
                exclude_key=None,
            )
            for declaration in self.manifest.indexes:
                self._check_secondary_unique(
                    declaration,
                    operation.after,
                    seq=seq,
                )
        elif operation.op_type == "delete":
            if row_key not in self.rows:
                raise BusinessError(
                    E_BAD_OP,
                    "delete operation references a missing primary key",
                    {"seq": seq, "pk": operation.pk},
                )
        else:
            if row_key not in self.rows:
                raise BusinessError(
                    E_BAD_OP,
                    "update operation references a missing primary key",
                    {"seq": seq, "pk": operation.pk},
                )
            assert operation.after is not None
            self._check_table_unique(
                operation.after,
                seq=seq,
                exclude_key=row_key,
            )
            for declaration in self.manifest.indexes:
                self._check_secondary_unique(
                    declaration,
                    operation.after,
                    seq=seq,
                )

        changed_columns = all_changed_columns(operation.before, operation.after)
        primary_changed = operation.op_type in {"insert", "delete"}
        if primary_changed:
            self.primary.tree.chain_version += 1
            self.primary.tree.current_seq = seq
            if operation.op_type == "insert":
                assert operation.after is not None
                self.primary.tree.insert_item(
                    _pk_list(operation.after, self.manifest),
                    self._primary_item(operation.after),
                )
            else:
                assert operation.before is not None
                pk = _pk_list(operation.before, self.manifest)
                self.primary.tree.remove_item(pk, pk)
            self.primary.last_change_seq = seq
            self.primary.last_change_clock_ms = operation.clock_ms
            self.primary.decision = {
                "mode": "primary_update",
                "seq": seq,
                "clock_ms": operation.clock_ms,
                "operation": operation.op_type,
            }
            self.split_events.extend(self.primary.tree.events)
            self.primary.tree.events = []

        if operation.op_type == "insert":
            assert operation.after is not None
            self.rows[row_key] = dict(operation.after)
        elif operation.op_type == "delete":
            assert operation.before is not None
            self.rows.pop(row_key, None)
        else:
            assert operation.after is not None
            self.rows[row_key] = dict(operation.after)

        now_ms = operation.clock_ms
        affected: list[str] = []
        changed_snapshots: list[str] = ["pk"] if primary_changed else []
        decisions: dict[str, dict[str, Any]] = {}
        query_by_index: dict[str, list[str]] = {}
        for query in self.manifest.query_templates:
            query_by_index.setdefault(query.index, []).append(query.name)
        for names in query_by_index.values():
            names.sort()

        for declaration in self.manifest.indexes:
            state = self.secondary[declaration.name]
            relevant_columns = set(declaration.columns) | set(declaration.covering)
            relevant_changed = sorted(relevant_columns & set(changed_columns))
            is_affected = bool(relevant_changed)
            if operation.op_type == "insert":
                is_affected = True
            elif operation.op_type == "delete":
                is_affected = True
            if not is_affected:
                reason = "no_referenced_key_or_cover_column_changed"
                path = [
                    f"op:{seq}",
                    "changed_columns:" + ",".join(changed_columns),
                    f"index:{declaration.name}",
                    "decision:reuse",
                ]
                self.invalidations.append(
                    self._invalidation(
                        seq=seq,
                        index_name=declaration.name,
                        kind="index_reuse",
                        action="reuse",
                        path=path,
                        reason=reason,
                        ref={"key_columns": list(declaration.columns)},
                        safe_reuse=True,
                    )
                )
                self.invalidations.append(
                    self._invalidation(
                        seq=seq,
                        index_name=declaration.name,
                        kind="page_digest",
                        action="reuse",
                        path=path + [f"digest:{state.tree.snapshot(scale=self.config.fixed_scale, rounding=self.config.rounding)['digest']}"],
                        reason="page_structure_untouched",
                        ref={"digest": state.tree.snapshot(scale=self.config.fixed_scale, rounding=self.config.rounding)["digest"]},
                        safe_reuse=True,
                    )
                )
                for query_name in query_by_index.get(declaration.name, []):
                    self.invalidations.append(
                        self._invalidation(
                            seq=seq,
                            index_name=declaration.name,
                            kind="query_availability",
                            action="reuse",
                            path=path + [f"query:{query_name}"],
                            reason="index_structure_and_items_unchanged",
                            ref={"query": query_name},
                            safe_reuse=True,
                        )
                    )
                continue

            affected.append(declaration.name)
            changed_snapshots.append(declaration.name)
            old_key = (
                [operation.before[column] for column in declaration.columns]
                if operation.before is not None
                else None
            )
            old_item = (
                self._index_item(declaration, operation.before)[1]
                if operation.before is not None
                else None
            )
            new_key = (
                [operation.after[column] for column in declaration.columns]
                if operation.after is not None
                else None
            )
            new_item = (
                self._index_item(declaration, operation.after)[1]
                if operation.after is not None
                else None
            )
            same_key = old_key is not None and new_key is not None and old_key == new_key
            pending_items = (1 if old_item is not None else 0) + (
                1 if new_item is not None else 0
            )
            touched_page_ids: set[str] = set()
            if old_key is not None:
                touched_page_ids.add(state.tree.locate_key_leaf(old_key))
            if new_key is not None:
                touched_page_ids.add(state.tree.locate_key_leaf(new_key))
            touched_pages = len(touched_page_ids)
            max_pending, max_touched, threshold_scope = self._thresholds(
                declaration.name
            )
            trigger: dict[str, Any] | None = None
            if pending_items > max_pending:
                trigger = {
                    "condition": "pending_items",
                    "value": pending_items,
                    "threshold": max_pending,
                    "scope": threshold_scope,
                }
            elif touched_pages > max_touched:
                trigger = {
                    "condition": "touched_pages",
                    "value": touched_pages,
                    "threshold": max_touched,
                    "scope": threshold_scope,
                }
            estimated_incremental_splits = self._simulate_item_change(
                state.tree,
                old_key=old_key,
                old_item=old_item,
                new_key=new_key,
                new_item=new_item,
            )
            simulated = self._clone_tree(state.tree)
            if old_key is not None and old_item is not None:
                if same_key:
                    simulated.replace_item(old_key, new_item)
                else:
                    simulated.remove_item(old_key, old_item["pk"])
                    if new_key is not None and new_item is not None:
                        simulated.insert_item(new_key, new_item)
            elif new_key is not None and new_item is not None:
                simulated.insert_item(new_key, new_item)
            dependence = []
            if pending_items == max_pending:
                dependence.append("pending_items_at_threshold")
            if touched_pages == max_touched:
                dependence.append("touched_pages_at_threshold")
            precision = self._precision_record(
                pending_items=pending_items,
                touched_pages=touched_pages,
                max_pending_items=max_pending,
                max_touched_pages=max_touched,
                before_groups=state.tree.group_count(),
                after_groups=simulated.group_count(),
                dependence=dependence,
            )
            old_digest = state.tree.snapshot(
                scale=self.config.fixed_scale,
                rounding=self.config.rounding,
            )["digest"]
            item_ref = {
                "old": (
                    {"key": old_key, "item": old_item}
                    if old_item is not None
                    else None
                ),
                "new": (
                    {"key": new_key, "item": new_item}
                    if new_item is not None
                    else None
                ),
            }
            item_path = [
                f"op:{seq}",
                "columns:" + ",".join(relevant_changed),
                f"index:{declaration.name}",
            ]
            if old_key is not None:
                item_path.append(f"old_page:{state.tree.locate_key_leaf(old_key)}")
            if new_key is not None:
                item_path.append(f"new_page:{state.tree.locate_key_leaf(new_key)}")

            if trigger is not None:
                rebuilt_tree, actual_rebuild_splits, rebuild_events = (
                    self._bulk_rebuild(state)
                )
                rebuilt_tree.current_seq = seq
                state.tree = rebuilt_tree
                state.last_change_seq = seq
                state.last_change_clock_ms = now_ms
                split_delta = actual_rebuild_splits
                decision = {
                    "mode": "rebuild",
                    "seq": seq,
                    "clock_ms": now_ms,
                    "started_clock_ms": now_ms,
                    "window_end_ms": now_ms + self.config.rebuild_window_ms,
                    "first_trigger": trigger,
                    "rejected_incremental_path": {
                        "strategy": "delete_old_insert_new",
                        "pending_items": pending_items,
                        "touched_pages": touched_pages,
                        "estimated_splits": estimated_incremental_splits,
                    },
                    "estimated_incremental_splits": estimated_incremental_splits,
                    "actual_rebuild_splits": split_delta,
                    "precision": precision,
                    "fixed_point": {
                        "scale": self.config.fixed_scale,
                        "rounding": self.config.rounding,
                    },
                }
                action = "rebuild"
                self.split_events.extend(rebuild_events)
            else:
                state.tree.chain_version += 1
                state.tree.current_seq = seq
                before_splits = state.tree.split_count
                if old_key is not None and old_item is not None:
                    if same_key:
                        state.tree.replace_item(old_key, new_item)
                    else:
                        state.tree.remove_item(old_key, old_item["pk"])
                        if new_key is not None and new_item is not None:
                            state.tree.insert_item(new_key, new_item)
                elif new_key is not None and new_item is not None:
                    state.tree.insert_item(new_key, new_item)
                split_delta = state.tree.split_count - before_splits
                state.last_change_seq = seq
                state.last_change_clock_ms = now_ms
                decision = {
                    "mode": "incremental",
                    "seq": seq,
                    "clock_ms": now_ms,
                    "pending_items": pending_items,
                    "touched_pages": touched_pages,
                    "thresholds": {
                        "max_pending_items": max_pending,
                        "max_touched_pages": max_touched,
                        "scope": threshold_scope,
                    },
                    "actual_splits": split_delta,
                    "precision": precision,
                    "fixed_point": {
                        "scale": self.config.fixed_scale,
                        "rounding": self.config.rounding,
                    },
                }
                action = "recompute"
                self.split_events.extend(state.tree.events)
                state.tree.events = []
            state.decision = decision
            decisions[declaration.name] = decision
            new_digest = state.tree.snapshot(
                scale=self.config.fixed_scale,
                rounding=self.config.rounding,
            )["digest"]
            self.invalidations.append(
                self._invalidation(
                    seq=seq,
                    index_name=declaration.name,
                    kind="index_items",
                    action=action,
                    path=item_path,
                    reason=(
                        "rebuild_threshold_exceeded"
                        if trigger is not None
                        else "key_or_cover_values_changed"
                    ),
                    ref=item_ref,
                    safe_reuse=False,
                )
            )
            self.invalidations.append(
                self._invalidation(
                    seq=seq,
                    index_name=declaration.name,
                    kind="page_digest",
                    action=action,
                    path=item_path + [f"old_digest:{old_digest}", f"new_digest:{new_digest}"],
                    reason=(
                        "full_rebuild_required"
                        if trigger is not None
                        else "incremental_page_change"
                    ),
                    ref={"old": old_digest, "new": new_digest},
                    safe_reuse=False,
                )
            )
            for query_name in query_by_index.get(declaration.name, []):
                self.invalidations.append(
                    self._invalidation(
                        seq=seq,
                        index_name=declaration.name,
                        kind="query_availability",
                        action=action,
                        path=item_path + [f"query:{query_name}"],
                        reason="dependent_query_recomputed",
                        ref={"query": query_name, "availability": "available_after_commit"},
                        safe_reuse=False,
                    )
                )
            if self.progress_callback is not None:
                self.progress_callback(
                    {
                        "phase": "secondary",
                        "seq": seq,
                        "index": declaration.name,
                        "action": action,
                    }
                )

        for position, record in enumerate(self.invalidations, start=1):
            record["record_order"] = position
        return OperationOutcome(
            seq=seq,
            affected_indexes=tuple(sorted(affected)),
            changed_snapshots=tuple(sorted(changed_snapshots)),
            split_events=tuple(self.split_events),
            invalidations=tuple(self.invalidations),
            decisions=decisions,
            primary_changed=primary_changed,
        )

    def force_rebuild(
        self,
        index_name: str,
        *,
        clock_ms: int,
        reason: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if index_name not in self.secondary and index_name != "pk":
            raise BusinessError(
                E_BAD_OP,
                "rebuild target does not exist",
                {"index": index_name},
            )
        state = self.primary if index_name == "pk" else self.secondary[index_name]
        self.current_seq = None
        self.split_events = []
        self.invalidations = []
        old_digest = state.tree.snapshot(
            scale=self.config.fixed_scale,
            rounding=self.config.rounding,
        )["digest"]
        rebuilt_tree, actual_splits, rebuild_events = self._bulk_rebuild(state)
        rebuilt_tree.current_seq = None
        state.tree = rebuilt_tree
        state.last_change_seq = state.last_change_seq
        state.last_change_clock_ms = clock_ms
        decision = {
            "mode": "manual_rebuild",
            "seq": None,
            "clock_ms": clock_ms,
            "started_clock_ms": clock_ms,
            "window_end_ms": clock_ms + self.config.rebuild_window_ms,
            "reason": reason,
            "actual_rebuild_splits": actual_splits,
            "fixed_point": {
                "scale": self.config.fixed_scale,
                "rounding": self.config.rounding,
            },
        }
        state.decision = decision
        self.split_events.extend(rebuild_events)
        new_digest = state.tree.snapshot(
            scale=self.config.fixed_scale,
            rounding=self.config.rounding,
        )["digest"]
        record = self._invalidation(
            seq=0,
            index_name=index_name,
            kind="manual_rebuild",
            action="rebuild",
            path=[f"index:{index_name}", "manual:rebuild"],
            reason=reason,
            ref={"old": old_digest, "new": new_digest},
            safe_reuse=False,
        )
        self.invalidations.append(record)
        for position, invalidation in enumerate(self.invalidations, start=1):
            invalidation["record_order"] = position
        return self.snapshot_for(index_name), list(self.invalidations)
