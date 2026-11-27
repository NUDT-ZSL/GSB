"""Observable B+Tree with deterministic page allocation and leaf links."""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .indexes import INF_SEQUENCE, IndexDefinition
from .models import PortRecord


SCHEMA_VERSION = 1


@dataclass(slots=True)
class Page:
    page_id: int
    level: int
    parent_id: int | None
    keys: list[tuple[Any, ...]] = field(default_factory=list)
    values: list[int] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    prev_leaf: int | None = None
    next_leaf: int | None = None

    @property
    def is_leaf(self) -> bool:
        return self.level == 0


class BPlusTree:
    """A B+Tree whose leaves are linked in ascending key order.

    ``capacity`` is the maximum number of keys in a leaf or separator keys in
    an internal page. A split uses ``fill_factor`` to choose the left page's
    key count; the first key of the right leaf (or the separator key itself for
    an internal page) is promoted.
    """

    def __init__(
        self,
        index_definition: IndexDefinition,
        *,
        capacity: int = 12,
        fill_factor: float = 0.5,
        seed: int = 0,
    ) -> None:
        if capacity < 3:
            raise ValueError("page capacity must be >= 3")
        if not 0.05 <= fill_factor <= 0.95:
            raise ValueError("fill factor must be between 0.05 and 0.95")
        self.index_definition = index_definition
        self.capacity = capacity
        self.fill_factor = fill_factor
        self.seed = seed
        self.pages: dict[int, Page] = {}
        self.records: dict[int, PortRecord] = {}
        self.split_history: list[dict[str, Any]] = []
        self.next_page_id = 0
        self.root_page_id = self._new_page(level=0, parent_id=None)
        self.last_completed_batch = 0

    @property
    def split_count(self) -> int:
        return len(self.split_history)

    @property
    def record_count(self) -> int:
        return len(self.records)

    def _new_page(self, level: int, parent_id: int | None) -> int:
        page_id = self.next_page_id
        self.next_page_id += 1
        self.pages[page_id] = Page(
            page_id=page_id,
            level=level,
            parent_id=parent_id,
        )
        return page_id

    def insert_many(self, records: Iterable[PortRecord]) -> None:
        for record in records:
            self.insert_record(record)

    def insert_record(self, record: PortRecord) -> None:
        if record.write_seq in self.records:
            raise ValueError(f"duplicate write_seq: {record.write_seq}")

        key = self.index_definition.key_for_record(record)
        leaf = self._find_leaf(key)
        position = bisect.bisect_left(leaf.keys, key)
        if position < len(leaf.keys) and leaf.keys[position] == key:
            raise ValueError(f"duplicate index key: {key!r}")

        leaf.keys.insert(position, key)
        leaf.values.insert(position, record.write_seq)
        self.records[record.write_seq] = record

        if len(leaf.keys) <= self.capacity:
            return

        current = leaf
        while True:
            right, promoted = self._split_page(current)
            event = self._split_event(current, right, promoted)

            if current.parent_id is None:
                root = self.pages[
                    self._new_page(level=current.level + 1, parent_id=None)
                ]
                root.keys = [promoted]
                root.children = [current.page_id, right.page_id]
                current.parent_id = root.page_id
                right.parent_id = root.page_id
                self.root_page_id = root.page_id
                event["root_created"] = True
                event["parent_page_id"] = None
                event["new_root_page_id"] = root.page_id
                self.split_history.append(event)
                return

            parent = self.pages[current.parent_id]
            child_index = parent.children.index(current.page_id)
            parent.keys.insert(child_index, promoted)
            parent.children.insert(child_index + 1, right.page_id)
            right.parent_id = parent.page_id
            event["root_created"] = False
            event["parent_page_id"] = parent.page_id
            event["parent_insert_index"] = child_index
            self.split_history.append(event)

            if len(parent.keys) <= self.capacity:
                return
            current = parent

    def _split_page(self, page: Page) -> tuple[Page, tuple[Any, ...]]:
        total = len(page.keys)
        left_count = self._left_key_count(total)
        old_keys = page.keys
        promoted = old_keys[left_count]
        right = self.pages[
            self._new_page(level=page.level, parent_id=page.parent_id)
        ]

        if page.is_leaf:
            old_values = page.values
            page.keys = old_keys[:left_count]
            page.values = old_values[:left_count]
            right.keys = old_keys[left_count:]
            right.values = old_values[left_count:]
            right.prev_leaf = page.page_id
            right.next_leaf = page.next_leaf
            if page.next_leaf is not None:
                self.pages[page.next_leaf].prev_leaf = right.page_id
            page.next_leaf = right.page_id
        else:
            old_children = page.children
            page.keys = old_keys[:left_count]
            page.children = old_children[: left_count + 1]
            right.keys = old_keys[left_count + 1 :]
            right.children = old_children[left_count + 1 :]
            for child_id in right.children:
                self.pages[child_id].parent_id = right.page_id

        return right, promoted

    def _left_key_count(self, total: int) -> int:
        target = int(total * self.fill_factor + 0.5)
        return max(1, min(total - 1, target))

    def _split_event(
        self,
        left: Page,
        right: Page,
        promoted: tuple[Any, ...],
    ) -> dict[str, Any]:
        return {
            "split_event": len(self.split_history) + 1,
            "page_id": left.page_id,
            "new_page_id": right.page_id,
            "level": left.level,
            "page_type": "leaf" if left.is_leaf else "internal",
            "promoted_key": self.index_definition.key_to_json(promoted),
            "before": self._page_occupancy(left.page_id, len_before=self.capacity + 1),
            "after": {
                "left": self._page_occupancy(left.page_id),
                "right": self._page_occupancy(right.page_id),
            },
            "split_fill_factor": self.fill_factor,
        }

    def _find_leaf(self, key: tuple[Any, ...] | None = None) -> Page:
        page = self.pages[self.root_page_id]
        while not page.is_leaf:
            if len(page.children) != len(page.keys) + 1:
                raise RuntimeError(
                    f"internal page {page.page_id} has inconsistent child count"
                )
            child_index = 0 if key is None else bisect.bisect_right(page.keys, key)
            page = self.pages[page.children[child_index]]
        return page

    def leftmost_leaf(self) -> Page:
        return self._find_leaf(None)

    def rightmost_leaf(self) -> Page:
        page = self.pages[self.root_page_id]
        while not page.is_leaf:
            page = self.pages[page.children[-1]]
        return page

    def scan_range(
        self,
        lower_key: tuple[Any, ...] | None,
        upper_key: tuple[Any, ...] | None,
        predicate: Callable[[PortRecord], bool] | None = None,
    ) -> dict[str, Any]:
        if predicate is None:
            predicate = lambda _record: True

        start_leaf = self.leftmost_leaf() if lower_key is None else self._find_leaf(lower_key)
        root_end_leaf = (
            self.rightmost_leaf() if upper_key is None else self._find_leaf(upper_key)
        )
        logical_leaves = self._logical_leaves()
        expected_start = (
            logical_leaves[0]
            if lower_key is None and logical_leaves
            else self._expected_landing_leaf(logical_leaves, lower_key)
        )
        expected_end = self._expected_landing_leaf(logical_leaves, upper_key)

        hits: list[PortRecord] = []
        hit_keys: list[tuple[Any, ...]] = []
        visited: list[int] = []
        seen: set[int] = set()
        current: Page | None = start_leaf
        stop_reason = "leaf_chain_end"

        while current is not None:
            if current.page_id in seen:
                stop_reason = f"duplicate_leaf_link:{current.page_id}"
                break
            if current.page_id not in self.pages:
                stop_reason = f"missing_leaf_page:{current.page_id}"
                break
            if not current.is_leaf:
                stop_reason = f"non_leaf_link:{current.page_id}"
                break

            seen.add(current.page_id)
            visited.append(current.page_id)
            crossed_upper = False

            for key, record_id in zip(current.keys, current.values):
                if lower_key is not None and key < lower_key:
                    continue
                if upper_key is not None and key > upper_key:
                    crossed_upper = True
                    stop_reason = "upper_bound_exceeded"
                    break
                record = self.records[record_id]
                if predicate(record):
                    hits.append(record)
                    hit_keys.append(key)

            if crossed_upper:
                break
            if upper_key is not None and current.keys and current.keys[-1] >= upper_key:
                stop_reason = "upper_bound_reached"
                break

            next_id = current.next_leaf
            if next_id is None:
                stop_reason = "leaf_chain_end"
                break
            next_page = self.pages.get(next_id)
            if next_page is None:
                stop_reason = f"missing_leaf_page:{next_id}"
                break
            if not next_page.is_leaf:
                stop_reason = f"non_leaf_link:{next_id}"
                break
            if (
                upper_key is not None
                and next_page.keys
                and next_page.keys[0] > upper_key
            ):
                stop_reason = "next_leaf_beyond_upper_bound"
                break
            if next_page.prev_leaf != current.page_id:
                stop_reason = f"broken_back_link:{next_id}"
                break
            current = next_page

        visited_sorted = all(
            hit_keys[index - 1] < hit_keys[index]
            for index in range(1, len(hit_keys))
        )
        logical_index = {
            page.page_id: index for index, page in enumerate(logical_leaves)
        }
        visited_positions = [logical_index.get(page_id) for page_id in visited]
        contiguous = (
            None not in visited_positions
            and visited_positions == list(
                range(visited_positions[0], visited_positions[0] + len(visited_positions))
            )
            if visited_positions
            else False
        )
        start_correct = start_leaf.page_id == expected_start.page_id
        end_correct = bool(visited) and visited[-1] == expected_end.page_id
        landing_correct = bool(start_correct and end_correct and contiguous)

        return {
            "start_leaf_id": start_leaf.page_id,
            "termination_leaf_id": visited[-1] if visited else None,
            "root_lookup_end_leaf_id": root_end_leaf.page_id,
            "expected_start_leaf_id": expected_start.page_id,
            "expected_end_leaf_id": expected_end.page_id,
            "visited_leaf_ids": visited,
            "visited_page_count": len(visited),
            "stop_reason": stop_reason,
            "landing_correct": landing_correct,
            "landing_details": {
                "start_correct": start_correct,
                "end_correct": end_correct,
                "visited_leaves_contiguous": contiguous,
            },
            "hits_sorted_by_index_key": visited_sorted,
            "hits": [record.to_dict() for record in hits],
            "hit_record_ids": [record.write_seq for record in hits],
            "hit_index_keys": [
                self.index_definition.key_to_json(key) for key in hit_keys
            ],
        }

    def _logical_leaves(self) -> list[Page]:
        leaves = [
            page for page in self.pages.values() if page.is_leaf and page.keys
        ]
        leaves.sort(key=lambda page: page.keys[0])
        return leaves

    def _expected_landing_leaf(
        self,
        logical_leaves: list[Page],
        key: tuple[Any, ...] | None,
    ) -> Page:
        if not logical_leaves:
            return self.leftmost_leaf()
        if key is None:
            return logical_leaves[-1]
        minima = [page.keys[0] for page in logical_leaves]
        index = bisect.bisect_right(minima, key) - 1
        return logical_leaves[max(0, index)]

    def validate_structure(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        pages_by_level: dict[int, int] = {}
        leaf_count = 0

        if self.root_page_id not in self.pages:
            issues.append(
                {
                    "type": "missing_root",
                    "page_id": self.root_page_id,
                }
            )
        root = self.pages.get(self.root_page_id)
        depth = root.level + 1 if root else 0

        for page in sorted(self.pages.values(), key=lambda item: item.page_id):
            pages_by_level[page.level] = pages_by_level.get(page.level, 0) + 1
            if len(page.keys) > self.capacity:
                issues.append(
                    {
                        "type": "capacity_exceeded",
                        "page_id": page.page_id,
                        "key_count": len(page.keys),
                        "capacity": self.capacity,
                    }
                )
            if page.keys != sorted(page.keys):
                issues.append(
                    {
                        "type": "keys_not_sorted",
                        "page_id": page.page_id,
                    }
                )
            if len(page.keys) != len(set(page.keys)):
                issues.append(
                    {
                        "type": "duplicate_keys_in_page",
                        "page_id": page.page_id,
                    }
                )
            if page.parent_id is not None and page.parent_id not in self.pages:
                issues.append(
                    {
                        "type": "missing_parent",
                        "page_id": page.page_id,
                        "parent_page_id": page.parent_id,
                    }
                )

            if page.is_leaf:
                leaf_count += 1
                if len(page.keys) != len(page.values):
                    issues.append(
                        {
                            "type": "leaf_key_value_mismatch",
                            "page_id": page.page_id,
                            "key_count": len(page.keys),
                            "value_count": len(page.values),
                        }
                    )
                if page.children:
                    issues.append(
                        {
                            "type": "leaf_has_children",
                            "page_id": page.page_id,
                        }
                    )
                for direction, target in (
                    ("prev_leaf", page.prev_leaf),
                    ("next_leaf", page.next_leaf),
                ):
                    if target is not None:
                        target_page = self.pages.get(target)
                        if target_page is None:
                            issues.append(
                                {
                                    "type": "missing_leaf_link",
                                    "page_id": page.page_id,
                                    "direction": direction,
                                    "target_page_id": target,
                                }
                            )
                        elif not target_page.is_leaf:
                            issues.append(
                                {
                                    "type": "non_leaf_link",
                                    "page_id": page.page_id,
                                    "direction": direction,
                                    "target_page_id": target,
                                }
                            )
            else:
                if len(page.children) != len(page.keys) + 1:
                    issues.append(
                        {
                            "type": "internal_child_count",
                            "page_id": page.page_id,
                            "key_count": len(page.keys),
                            "child_count": len(page.children),
                        }
                    )
                for child_id in page.children:
                    child = self.pages.get(child_id)
                    if child is None:
                        issues.append(
                            {
                                "type": "missing_child",
                                "page_id": page.page_id,
                                "child_page_id": child_id,
                            }
                        )
                        continue
                    if child.parent_id != page.page_id:
                        issues.append(
                            {
                                "type": "wrong_parent_pointer",
                                "page_id": child.page_id,
                                "expected_parent_page_id": page.page_id,
                                "actual_parent_page_id": child.parent_id,
                            }
                        )
                    if child.level != page.level - 1:
                        issues.append(
                            {
                                "type": "wrong_child_level",
                                "page_id": page.page_id,
                                "child_page_id": child_id,
                                "expected_level": page.level - 1,
                                "actual_level": child.level,
                            }
                        )

        connected_ids: list[int] = []
        chain_issues = self._validate_leaf_chain()
        issues.extend(chain_issues["issues"])
        connected_ids = chain_issues["leaf_ids"]

        if self.record_count != sum(len(page.values) for page in self.pages.values() if page.is_leaf):
            issues.append(
                {
                    "type": "record_count_mismatch",
                    "record_table_count": self.record_count,
                    "leaf_value_count": sum(
                        len(page.values)
                        for page in self.pages.values()
                        if page.is_leaf
                    ),
                }
            )

        return {
            "valid": not issues,
            "issues": issues,
            "depth": depth,
            "root_page_id": self.root_page_id,
            "total_pages": len(self.pages),
            "pages_by_level": {
                str(level): pages_by_level[level]
                for level in sorted(pages_by_level)
            },
            "leaf_count": leaf_count,
            "leaf_chain_connected": chain_issues["connected"],
            "leaf_chain": {
                "connected": chain_issues["connected"],
                "leaf_ids": connected_ids,
                "issues": chain_issues["issues"],
                "expected_leaf_count": leaf_count,
                "reachable_leaf_count": len(connected_ids),
            },
        }

    def _validate_leaf_chain(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        leaf_pages = {
            page.page_id: page for page in self.pages.values() if page.is_leaf
        }
        leaf_by_key = sorted(leaf_pages.values(), key=lambda page: page.keys[0] if page.keys else ())
        expected_order = [page.page_id for page in leaf_by_key]

        leftmost = self.leftmost_leaf()
        current: Page | None = leftmost
        visited: list[int] = []
        seen: set[int] = set()
        while current is not None:
            if current.page_id in seen:
                issues.append(
                    {
                        "type": "duplicate_leaf_link",
                        "page_id": current.page_id,
                        "previous_page_id": visited[-1] if visited else None,
                    }
                )
                break
            seen.add(current.page_id)
            visited.append(current.page_id)

            if current.prev_leaf is not None:
                previous = leaf_pages.get(current.prev_leaf)
                if previous is None:
                    issues.append(
                        {
                            "type": "back_link_to_missing_page",
                            "page_id": current.page_id,
                            "target_page_id": current.prev_leaf,
                        }
                    )
                elif previous.next_leaf != current.page_id:
                    issues.append(
                        {
                            "type": "back_link_mismatch",
                            "page_id": current.page_id,
                            "target_page_id": current.prev_leaf,
                            "target_next_page_id": previous.next_leaf,
                        }
                    )

            next_id = current.next_leaf
            if next_id is None:
                break
            next_page = leaf_pages.get(next_id)
            if next_page is None:
                issues.append(
                    {
                        "type": "forward_link_to_missing_page",
                        "page_id": current.page_id,
                        "target_page_id": next_id,
                    }
                )
                break
            if next_page.prev_leaf != current.page_id:
                issues.append(
                    {
                        "type": "forward_link_mismatch",
                        "page_id": current.page_id,
                        "target_page_id": next_id,
                        "target_prev_page_id": next_page.prev_leaf,
                    }
                )
            if current.keys and next_page.keys and current.keys[-1] >= next_page.keys[0]:
                issues.append(
                    {
                        "type": "leaf_order_violation",
                        "page_id": current.page_id,
                        "next_page_id": next_id,
                    }
                )
            current = next_page

        missing = [page_id for page_id in expected_order if page_id not in seen]
        if missing:
            issues.append(
                {
                    "type": "leaf_chain_broken",
                    "unreachable_page_ids": missing,
                    "last_reachable_page_id": visited[-1] if visited else None,
                }
            )
        if len(visited) != len(leaf_pages):
            issues.append(
                {
                    "type": "leaf_count_mismatch",
                    "expected_leaf_count": len(leaf_pages),
                    "reachable_leaf_count": len(visited),
                }
            )

        return {
            "connected": not issues and len(visited) == len(leaf_pages),
            "leaf_ids": visited,
            "issues": issues,
        }

    def _page_occupancy(
        self,
        page_id: int,
        *,
        len_before: int | None = None,
    ) -> dict[str, Any]:
        key_count = (
            len_before if len_before is not None else len(self.pages[page_id].keys)
        )
        return {
            "page_id": page_id,
            "key_count": key_count,
            "capacity": self.capacity,
            "fill_ratio": round(key_count / self.capacity, 6),
        }

    def leaf_fill_statistics(self) -> dict[str, Any]:
        leaves = [page for page in self.pages.values() if page.is_leaf]
        key_count = sum(len(page.keys) for page in leaves)
        total_capacity = self.capacity * len(leaves)
        ratios = [
            len(page.keys) / self.capacity for page in leaves
        ]
        return {
            "leaf_count": len(leaves),
            "leaf_key_count": key_count,
            "total_leaf_capacity": total_capacity,
            "average_fill_ratio": round(
                key_count / total_capacity if total_capacity else 0.0,
                6,
            ),
            "minimum_fill_ratio": round(min(ratios), 6) if ratios else 0.0,
            "maximum_fill_ratio": round(max(ratios), 6) if ratios else 0.0,
        }

    def page_report(self) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for page in sorted(self.pages.values(), key=lambda item: item.page_id):
            item: dict[str, Any] = {
                "page_id": page.page_id,
                "level": page.level,
                "page_type": "leaf" if page.is_leaf else "internal",
                "parent_id": page.parent_id,
                "key_count": len(page.keys),
                "capacity": self.capacity,
                "fill_ratio": round(len(page.keys) / self.capacity, 6),
                "keys": [
                    self.index_definition.key_to_json(key) for key in page.keys
                ],
            }
            if page.is_leaf:
                item["record_ids"] = list(page.values)
                item["prev_leaf"] = page.prev_leaf
                item["next_leaf"] = page.next_leaf
            else:
                item["children"] = list(page.children)
            report.append(item)
        return report

    def snapshot(
        self,
        *,
        completed_batch: int,
        batch_split_start: int = 0,
    ) -> dict[str, Any]:
        self.last_completed_batch = completed_batch
        structure = self.validate_structure()
        state = self._canonical_state()
        fingerprint = hashlib.sha256(
            json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": SCHEMA_VERSION,
            "index": self.index_definition.describe(),
            "configuration": {
                "seed": self.seed,
                "page_capacity": self.capacity,
                "fill_factor": self.fill_factor,
            },
            "last_completed_batch": completed_batch,
            "record_count": self.record_count,
            "split_count": self.split_count,
            "batch_split_events": self.split_history[batch_split_start:],
            "split_history": self.split_history,
            "structure": structure,
            "leaf_fill_statistics": self.leaf_fill_statistics(),
            "root_page_id": self.root_page_id,
            "next_page_id": self.next_page_id,
            "pages": self.page_report(),
            "records": [
                self.records[record_id].to_dict()
                for record_id in sorted(self.records)
            ],
            "state_fingerprint": fingerprint,
        }

    def _canonical_state(self) -> dict[str, Any]:
        return {
            "index_name": self.index_definition.name,
            "configuration": {
                "seed": self.seed,
                "page_capacity": self.capacity,
                "fill_factor": self.fill_factor,
            },
            "last_completed_batch": self.last_completed_batch,
            "root_page_id": self.root_page_id,
            "next_page_id": self.next_page_id,
            "pages": [
                {
                    "page_id": page.page_id,
                    "level": page.level,
                    "parent_id": page.parent_id,
                    "keys": [
                        self.index_definition.key_to_json(key)
                        for key in page.keys
                    ],
                    "values": list(page.values),
                    "children": list(page.children),
                    "prev_leaf": page.prev_leaf,
                    "next_leaf": page.next_leaf,
                }
                for page in sorted(
                    self.pages.values(), key=lambda item: item.page_id
                )
            ],
            "records": [
                self.records[record_id].to_dict()
                for record_id in sorted(self.records)
            ],
            "split_history": self.split_history,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self._canonical_state(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        index_definition: IndexDefinition,
    ) -> "BPlusTree":
        if snapshot.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported snapshot schema version")
        if snapshot["index"]["name"] != index_definition.name:
            raise ValueError(
                "snapshot index definition does not match requested index"
            )
        configuration = snapshot["configuration"]
        tree = cls(
            index_definition,
            capacity=int(configuration["page_capacity"]),
            fill_factor=float(configuration["fill_factor"]),
            seed=int(configuration["seed"]),
        )
        tree.pages = {}
        tree.records = {}
        tree.split_history = list(snapshot.get("split_history", []))
        tree.root_page_id = int(snapshot["root_page_id"])
        tree.next_page_id = int(snapshot["next_page_id"])
        tree.last_completed_batch = int(snapshot["last_completed_batch"])

        for page_value in snapshot["pages"]:
            page = Page(
                page_id=int(page_value["page_id"]),
                level=int(page_value["level"]),
                parent_id=(
                    int(page_value["parent_id"])
                    if page_value["parent_id"] is not None
                    else None
                ),
                keys=[tuple(key) for key in page_value.get("keys", [])],
                values=[int(value) for value in page_value.get("record_ids", [])],
                children=[
                    int(value) for value in page_value.get("children", [])
                ],
                prev_leaf=(
                    int(page_value["prev_leaf"])
                    if page_value.get("prev_leaf") is not None
                    else None
                ),
                next_leaf=(
                    int(page_value["next_leaf"])
                    if page_value.get("next_leaf") is not None
                    else None
                ),
            )
            if page.page_id in tree.pages:
                raise ValueError(f"duplicate page in snapshot: {page.page_id}")
            tree.pages[page.page_id] = page

        for record_value in snapshot["records"]:
            record = PortRecord.from_dict(record_value)
            tree.records[record.write_seq] = record

        expected_fingerprint = snapshot.get("state_fingerprint")
        actual_fingerprint = tree.fingerprint()
        if expected_fingerprint and expected_fingerprint != actual_fingerprint:
            raise ValueError(
                "snapshot state fingerprint mismatch after restore"
            )
        structure = tree.validate_structure()
        if not structure["valid"]:
            raise ValueError(
                f"restored tree failed structural validation: {structure['issues']}"
            )
        return tree

    def range_plan_for(self, query: Any) -> Any:
        return self.index_definition.scan_bounds(query)

    def scan_query(self, query: Any) -> dict[str, Any]:
        bounds = self.index_definition.scan_bounds(query)
        scan = self.scan_range(
            bounds.lower_key,
            bounds.upper_key,
            query.matches,
        )
        scan["query_id"] = query.query_id
        scan["query_kind"] = query.kind
        scan["access_description"] = bounds.access_description
        scan["full_scan_required_by_index"] = bounds.full_scan_required
        scan["lower_key"] = (
            self.index_definition.key_to_json(bounds.lower_key)
            if bounds.lower_key is not None
            else None
        )
        scan["upper_key"] = (
            self.index_definition.key_to_json(bounds.upper_key)
            if bounds.upper_key is not None
            else None
        )
        return scan
