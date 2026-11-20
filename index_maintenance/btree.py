"""Deterministic grouped-entry B+ tree with logical page split chains."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

import numpy as np

from .canonical import fixed_units, sha256_json
from .errors import (
    BusinessError,
    E_BROKEN_PARENT_POINTER,
    E_INCONSISTENT_STATE,
    E_SPLIT_HEIGHT_OVERFLOW,
)


def _sort_token(value: Any, column_type: str) -> tuple[int, Any]:
    if value is None:
        return (0, 0)
    if column_type == "BOOL":
        return (1, int(bool(value)))
    if column_type == "INT":
        return (1, int(value))
    if column_type == "DECIMAL":
        return (1, Decimal(str(value)))
    return (2, str(value))


def key_token(key: list[Any] | tuple[Any, ...], types: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(_sort_token(value, column_type) for value, column_type in zip(key, types))


def compare_key(
    left: list[Any] | tuple[Any, ...],
    right: list[Any] | tuple[Any, ...],
    types: tuple[str, ...],
) -> int:
    left_token = key_token(left, types)
    right_token = key_token(right, types)
    return (left_token > right_token) - (left_token < right_token)


def compare_item_pk(left: dict[str, Any], right: dict[str, Any], types: tuple[str, ...]) -> int:
    return compare_key(left["pk"], right["pk"], types)


def sorted_items(items: list[dict[str, Any]], pk_types: tuple[str, ...]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: key_token(item["pk"], pk_types))


def partition_entries(entries: list[Any], split: int) -> tuple[list[Any], list[Any]]:
    """Partition page entries through a deterministic NumPy index array."""
    positions = np.arange(len(entries), dtype=np.int64)
    left = [entries[int(position)] for position in positions[:split]]
    right = [entries[int(position)] for position in positions[split:]]
    return left, right


@dataclass
class Page:
    page_id: str
    leaf: bool
    level: int
    parent_id: str | None = None
    left_id: str | None = None
    right_id: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    keys: list[list[Any]] = field(default_factory=list)
    children: list[str] = field(default_factory=list)


class BTree:
    """A deterministic logical B+ tree.

    Leaf entries are grouped by index key. Each group owns one or more items,
    which makes non-unique secondary indexes and covering values explicit.
    """

    def __init__(
        self,
        name: str,
        *,
        capacity: int,
        split_factor: float,
        max_height: int,
        key_types: tuple[str, ...],
        pk_types: tuple[str, ...],
        chain_version: int = 0,
        split_count: int = 0,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.name = name
        self.capacity = capacity
        self.split_factor = split_factor
        self.max_height = max_height
        self.key_types = key_types
        self.pk_types = pk_types
        self.chain_version = chain_version
        self.split_count = split_count
        self.event_callback = event_callback
        self.pages: dict[str, Page] = {}
        self.root_id: str | None = None
        self.next_page_seq = 1
        self.retired_pages: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.current_seq: int | None = None
        self.min_entries = max(
            1,
            min(
                math.ceil(capacity * split_factor),
                max(1, (capacity + 1) // 2),
            ),
        )
        self.min_children = max(
            2,
            min(
                math.ceil(capacity * split_factor),
                max(2, (capacity + 1) // 2),
            ),
        )

    @property
    def height(self) -> int:
        if self.root_id is None:
            return 0
        return self.pages[self.root_id].level + 1

    def _new_page(self, *, leaf: bool, level: int) -> Page:
        page_id = f"{self.name}-L{level}-{self.next_page_seq:06d}"
        self.next_page_seq += 1
        page = Page(page_id=page_id, leaf=leaf, level=level)
        self.pages[page_id] = page
        return page

    def _check_height(self, level: int) -> None:
        if level + 1 > self.max_height:
            raise BusinessError(
                E_SPLIT_HEIGHT_OVERFLOW,
                "page split would exceed max_tree_height",
                {
                    "index": self.name,
                    "new_level": level,
                    "max_tree_height": self.max_height,
                    "seq": self.current_seq,
                },
            )

    def _cmp_keys(self, left: list[Any], right: list[Any]) -> int:
        return compare_key(left, right, self.key_types)

    def _upper_bound(self, keys: list[list[Any]], target: list[Any]) -> int:
        low = 0
        high = len(keys)
        while low < high:
            middle = (low + high) // 2
            if self._cmp_keys(keys[middle], target) <= 0:
                low = middle + 1
            else:
                high = middle
        return low

    def _lower_group_index(self, entries: list[dict[str, Any]], key: list[Any]) -> int:
        low = 0
        high = len(entries)
        while low < high:
            middle = (low + high) // 2
            if self._cmp_keys(entries[middle]["key"], key) < 0:
                low = middle + 1
            else:
                high = middle
        return low

    def _find_leaf(self, key: list[Any]) -> Page:
        if self.root_id is None:
            root = self._new_page(leaf=True, level=0)
            root_id = root.page_id
            self.root_id = root_id
        page = self.pages[self.root_id]
        while not page.leaf:
            child_index = self._upper_bound(page.keys, key)
            page = self.pages[page.children[child_index]]
        return page

    def _group_in_leaf(
        self,
        leaf: Page,
        key: list[Any],
    ) -> tuple[int, dict[str, Any] | None]:
        position = self._lower_group_index(leaf.entries, key)
        if position < len(leaf.entries) and self._cmp_keys(leaf.entries[position]["key"], key) == 0:
            return position, leaf.entries[position]
        return position, None

    def get_group(self, key: list[Any]) -> dict[str, Any] | None:
        leaf = self._find_leaf(key)
        _, group = self._group_in_leaf(leaf, key)
        return group

    def locate_group_page(self, key: list[Any]) -> str | None:
        leaf = self._find_leaf(key)
        _, group = self._group_in_leaf(leaf, key)
        return leaf.page_id if group is not None else None

    def locate_key_leaf(self, key: list[Any]) -> str:
        return self._find_leaf(key).page_id

    def ensure_root(self) -> None:
        if self.root_id is None:
            root = self._new_page(leaf=True, level=0)
            self.root_id = root.page_id

    def item_count(self) -> int:
        return sum(
            len(entry["items"])
            for page in self.pages.values()
            if page.leaf
            for entry in page.entries
        )

    def group_count(self) -> int:
        return sum(
            len(page.entries) for page in self.pages.values() if page.leaf
        )

    def insert_item(self, key: list[Any], item: dict[str, Any]) -> None:
        leaf = self._find_leaf(key)
        position, group = self._group_in_leaf(leaf, key)
        if group is None:
            group = {"key": list(key), "items": []}
            leaf.entries.insert(position, group)
        else:
            for existing in group["items"]:
                if compare_key(existing["pk"], item["pk"], self.pk_types) == 0:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "duplicate item in index group",
                        {"index": self.name, "key": key, "pk": item["pk"]},
                    )
        group["items"].append(item)
        group["items"] = sorted_items(group["items"], self.pk_types)
        if len(leaf.entries) > self.capacity:
            self._split_leaf(leaf)

    def replace_item(self, key: list[Any], item: dict[str, Any]) -> None:
        leaf = self._find_leaf(key)
        _, group = self._group_in_leaf(leaf, key)
        if group is None:
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "cannot replace an item in a missing index group",
                {"index": self.name, "key": key, "pk": item["pk"]},
            )
        for position, existing in enumerate(group["items"]):
            if compare_key(existing["pk"], item["pk"], self.pk_types) == 0:
                group["items"][position] = item
                return
        raise BusinessError(
            E_INCONSISTENT_STATE,
            "cannot replace a missing index item",
            {"index": self.name, "key": key, "pk": item["pk"]},
        )

    def remove_item(self, key: list[Any], pk: list[Any]) -> None:
        leaf = self._find_leaf(key)
        position, group = self._group_in_leaf(leaf, key)
        if group is None:
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "index group is missing during delete",
                {"index": self.name, "key": key, "pk": pk},
            )
        item_position = next(
            (
                index
                for index, item in enumerate(group["items"])
                if compare_key(item["pk"], pk, self.pk_types) == 0
            ),
            None,
        )
        if item_position is None:
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "index item is missing during delete",
                {"index": self.name, "key": key, "pk": pk},
            )
        del group["items"][item_position]
        if not group["items"]:
            del leaf.entries[position]
        self._rebalance_leaf(leaf)

    def _split_point(self, count: int, minimum: int) -> int:
        desired = max(1, int(count * self.split_factor))
        low = minimum
        high = count - minimum
        if low > high:
            return max(1, min(count - 1, count // 2))
        return max(low, min(desired, high))

    def _link_new_right(self, left: Page, right: Page) -> None:
        old_right = left.right_id
        left.right_id = right.page_id
        right.left_id = left.page_id
        right.right_id = old_right
        if old_right is not None:
            self.pages[old_right].left_id = right.page_id

    def _record_split(self, left: Page, right: Page, separator: list[Any]) -> None:
        parent_id = left.parent_id
        event = {
            "tree": self.name,
            "seq": self.current_seq,
            "chain_version": self.chain_version,
            "event_order": len(self.events) + 1,
            "page_id": right.page_id,
            "source_page_id": left.page_id,
            "parent_page_id": parent_id,
            "left_sibling_page_id": left.page_id,
            "right_sibling_page_id": right.right_id,
            "level": right.level,
            "separator": list(separator),
        }
        self.events.append(event)
        if self.event_callback is not None:
            self.event_callback(event)

    def _new_root_above(
        self,
        left: Page,
        right: Page,
        separator: list[Any],
    ) -> Page:
        self._check_height(left.level + 1)
        root = self._new_page(leaf=False, level=left.level + 1)
        root.keys = [list(separator)]
        root.children = [left.page_id, right.page_id]
        left.parent_id = root.page_id
        right.parent_id = root.page_id
        self.root_id = root.page_id
        return root

    def _attach_to_parent(
        self,
        left: Page,
        right: Page,
        separator: list[Any],
    ) -> None:
        if left.parent_id is None:
            self._new_root_above(left, right, separator)
            return
        parent = self.pages[left.parent_id]
        child_index = parent.children.index(left.page_id)
        parent.children.insert(child_index + 1, right.page_id)
        parent.keys.insert(child_index, list(separator))
        right.parent_id = parent.page_id
        if len(parent.children) > self.capacity:
            self._split_internal(parent)

    def _split_leaf(self, left: Page) -> None:
        split = self._split_point(len(left.entries), self.min_entries)
        left.entries, right_entries = partition_entries(left.entries, split)
        right = self._new_page(leaf=True, level=left.level)
        right.entries = right_entries
        self._link_new_right(left, right)
        separator = list(right.entries[0]["key"])
        self._attach_to_parent(left, right, separator)
        self.split_count += 1
        self._record_split(left, right, separator)

    def _split_internal(self, left: Page) -> None:
        split = self._split_point(len(left.children), self.min_children)
        separator = list(left.keys[split - 1])
        right = self._new_page(leaf=False, level=left.level)
        left_children, right.children = partition_entries(left.children, split)
        left_keys, right.keys = partition_entries(left.keys, split)
        left.children = left_children
        left.keys = left_keys[: split - 1]
        for child_id in right.children:
            self.pages[child_id].parent_id = right.page_id
        self._link_new_right(left, right)
        self._attach_to_parent(left, right, separator)
        self.split_count += 1
        self._record_split(left, right, separator)

    def _sibling(self, page: Page, direction: int) -> Page | None:
        sibling_id = page.left_id if direction < 0 else page.right_id
        return self.pages.get(sibling_id) if sibling_id is not None else None

    def _replace_child(self, parent: Page, old_child_id: str, new_child_id: str) -> None:
        position = parent.children.index(old_child_id)
        parent.children[position] = new_child_id

    def _remove_child_and_separator(self, parent: Page, child_index: int) -> None:
        del parent.children[child_index]
        key_index = child_index - 1
        if key_index < 0:
            key_index = 0
        if parent.keys:
            del parent.keys[min(key_index, len(parent.keys) - 1)]

    def _retire(self, page: Page, reason: str) -> None:
        if page.page_id == self.root_id:
            return
        self.pages.pop(page.page_id, None)
        self.retired_pages.append(
            {
                "page_id": page.page_id,
                "level": page.level,
                "reason": reason,
                "chain_version": self.chain_version,
                "seq": self.current_seq,
            }
        )

    def _unlink_retired(self, page: Page) -> None:
        if page.left_id is not None and page.left_id in self.pages:
            self.pages[page.left_id].right_id = page.right_id
        if page.right_id is not None and page.right_id in self.pages:
            self.pages[page.right_id].left_id = page.left_id

    def _rebalance_leaf(self, page: Page) -> None:
        if page.parent_id is None or len(page.entries) >= self.min_entries:
            if page.parent_id is not None:
                self._refresh_separator_for(page)
            return
        parent = self.pages[page.parent_id]
        child_index = parent.children.index(page.page_id)
        left = self._sibling(page, -1)
        right = self._sibling(page, 1)
        if (
            left is not None
            and left.leaf
            and left.parent_id == page.parent_id
            and len(left.entries) > self.min_entries
        ):
            page.entries.insert(0, left.entries.pop())
            parent.keys[child_index - 1] = list(page.entries[0]["key"])
            self._refresh_separator_for(right)
            return
        if (
            right is not None
            and right.leaf
            and right.parent_id == page.parent_id
            and len(right.entries) > self.min_entries
        ):
            page.entries.append(right.entries.pop(0))
            parent.keys[child_index] = list(right.entries[0]["key"])
            return
        if left is not None and left.leaf and left.parent_id == page.parent_id:
            left.entries.extend(page.entries)
            self._unlink_retired(page)
            self._remove_child_and_separator(parent, child_index)
            self._retire(page, "leaf_merge_into_left")
            self._handle_parent_after_removal(parent)
            return
        if right is not None and right.leaf and right.parent_id == page.parent_id:
            page.entries.extend(right.entries)
            self._unlink_retired(right)
            self._remove_child_and_separator(parent, child_index + 1)
            self._retire(right, "leaf_merge_into_left")
            self._handle_parent_after_removal(parent)
            return

    def _refresh_separator_for(self, page: Page | None) -> None:
        current = page
        while current is not None and current.parent_id is not None:
            parent = self.pages[current.parent_id]
            child_index = parent.children.index(current.page_id)
            if child_index > 0:
                parent.keys[child_index - 1] = list(self._subtree_min_key(current))
                return
            current = parent

    def _handle_parent_after_removal(self, parent: Page) -> None:
        if parent.parent_id is not None:
            self._refresh_separator_for(parent)
        if parent.page_id == self.root_id:
            self._collapse_root()
            return
        if len(parent.children) >= self.min_children:
            return
        self._rebalance_internal(parent)

    def _collapse_root(self) -> None:
        while self.root_id is not None:
            root = self.pages[self.root_id]
            if root.leaf or len(root.children) >= 2:
                break
            if not root.children:
                replacement = self._new_page(leaf=True, level=0)
                self.root_id = replacement.page_id
                self._retire(root, "empty_internal_root")
                break
            child = self.pages[root.children[0]]
            child.parent_id = None
            self.root_id = child.page_id
            self._retire(root, "root_collapse")

    def _rebalance_internal(self, page: Page) -> None:
        if page.parent_id is None or len(page.children) >= self.min_children:
            return
        parent = self.pages[page.parent_id]
        child_index = parent.children.index(page.page_id)
        left = self._sibling(page, -1)
        right = self._sibling(page, 1)
        if (
            left is not None
            and not left.leaf
            and left.parent_id == page.parent_id
            and len(left.children) > self.min_children
        ):
            moved_child = left.children.pop()
            moved_separator = parent.keys[child_index - 1]
            page.children.insert(0, moved_child)
            page.keys.insert(0, moved_separator)
            parent.keys[child_index - 1] = left.keys.pop()
            self.pages[moved_child].parent_id = page.page_id
            return
        if (
            right is not None
            and not right.leaf
            and right.parent_id == page.parent_id
            and len(right.children) > self.min_children
        ):
            moved_child = right.children.pop(0)
            moved_separator = parent.keys[child_index]
            page.children.append(moved_child)
            page.keys.append(moved_separator)
            parent.keys[child_index] = right.keys.pop(0)
            self.pages[moved_child].parent_id = page.page_id
            return
        if left is not None and not left.leaf and left.parent_id == page.parent_id:
            separator = parent.keys[child_index - 1]
            left.keys.append(separator)
            left.keys.extend(page.keys)
            left.children.extend(page.children)
            for child_id in page.children:
                self.pages[child_id].parent_id = left.page_id
            self._unlink_retired(page)
            self._remove_child_and_separator(parent, child_index)
            self._retire(page, "internal_merge_into_left")
            self._handle_parent_after_removal(parent)
            return
        if right is not None and not right.leaf and right.parent_id == page.parent_id:
            separator = parent.keys[child_index]
            page.keys.append(separator)
            page.keys.extend(right.keys)
            page.children.extend(right.children)
            for child_id in right.children:
                self.pages[child_id].parent_id = page.page_id
            self._unlink_retired(right)
            self._remove_child_and_separator(parent, child_index + 1)
            self._retire(right, "internal_merge_into_left")
            self._handle_parent_after_removal(parent)
            return

    def _subtree_min_key(self, page: Page) -> list[Any]:
        current = page
        while not current.leaf:
            current = self.pages[current.children[0]]
        return list(current.entries[0]["key"])

    def integrity_check(self) -> dict[str, Any]:
        if self.root_id is None or self.root_id not in self.pages:
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "tree root is missing",
                {"index": self.name},
            )
        root = self.pages[self.root_id]
        if root.parent_id is not None:
            raise BusinessError(
                E_BROKEN_PARENT_POINTER,
                "root page must not have a parent",
                {"index": self.name, "page_id": root.page_id, "parent": root.parent_id},
            )
        if root.level + 1 > self.max_height:
            raise BusinessError(
                E_SPLIT_HEIGHT_OVERFLOW,
                "tree height exceeds configured maximum",
                {
                    "index": self.name,
                    "height": root.level + 1,
                    "max_tree_height": self.max_height,
                },
            )
        reachable: set[str] = set()
        stack = [root.page_id]
        while stack:
            page_id = stack.pop()
            if page_id in reachable:
                raise BusinessError(
                    E_BROKEN_PARENT_POINTER,
                    "page is referenced more than once in the child chain",
                    {"index": self.name, "page_id": page_id},
                )
            reachable.add(page_id)
            page = self.pages[page_id]
            if page.left_id is not None:
                left = self.pages.get(page.left_id)
                if left is None or left.right_id != page.page_id:
                    raise BusinessError(
                        E_BROKEN_PARENT_POINTER,
                        "left sibling pointer is not reciprocal",
                        {"index": self.name, "page_id": page.page_id, "left": page.left_id},
                    )
            if page.right_id is not None:
                right = self.pages.get(page.right_id)
                if right is None or right.left_id != page.page_id:
                    raise BusinessError(
                        E_BROKEN_PARENT_POINTER,
                        "right sibling pointer is not reciprocal",
                        {"index": self.name, "page_id": page.page_id, "right": page.right_id},
                    )
            if page.leaf:
                if page.level != 0:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "leaf page has a non-zero level",
                        {"index": self.name, "page_id": page.page_id},
                    )
                if page.children or page.keys:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "leaf page contains internal fields",
                        {"index": self.name, "page_id": page.page_id},
                    )
                if page.page_id != self.root_id:
                    if len(page.entries) < self.min_entries:
                        raise BusinessError(
                            E_INCONSISTENT_STATE,
                            "leaf page is below its fill lower bound",
                            {
                                "index": self.name,
                                "page_id": page.page_id,
                                "entries": len(page.entries),
                                "min_entries": self.min_entries,
                            },
                        )
                previous_key: list[Any] | None = None
                seen_pks: set[tuple[Any, ...]] = set()
                for entry in page.entries:
                    if previous_key is not None and self._cmp_keys(previous_key, entry["key"]) >= 0:
                        raise BusinessError(
                            E_INCONSISTENT_STATE,
                            "leaf entries are not strictly sorted",
                            {"index": self.name, "page_id": page.page_id},
                        )
                    previous_key = entry["key"]
                    if not entry["items"]:
                        raise BusinessError(
                            E_INCONSISTENT_STATE,
                            "leaf group has no items",
                            {"index": self.name, "page_id": page.page_id},
                        )
                    item_keys = [
                        key_token(item["pk"], self.pk_types) for item in entry["items"]
                    ]
                    if item_keys != sorted(item_keys):
                        raise BusinessError(
                            E_INCONSISTENT_STATE,
                            "group items are not sorted by primary key",
                            {"index": self.name, "page_id": page.page_id},
                        )
                    for item in entry["items"]:
                        item_key = key_token(item["pk"], self.pk_types)
                        if item_key in seen_pks:
                            raise BusinessError(
                                E_INCONSISTENT_STATE,
                                "duplicate primary key item in leaf",
                                {"index": self.name, "page_id": page.page_id, "pk": item["pk"]},
                            )
                        seen_pks.add(item_key)
            else:
                if page.entries:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "internal page contains leaf fields",
                        {"index": self.name, "page_id": page.page_id},
                    )
                if len(page.children) != len(page.keys) + 1:
                    raise BusinessError(
                        E_BROKEN_PARENT_POINTER,
                        "internal page key/child arity is broken",
                        {
                            "index": self.name,
                            "page_id": page.page_id,
                            "keys": len(page.keys),
                            "children": len(page.children),
                        },
                    )
                if page.page_id != self.root_id and len(page.children) < self.min_children:
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "internal page is below its fill lower bound",
                        {
                            "index": self.name,
                            "page_id": page.page_id,
                            "children": len(page.children),
                            "min_children": self.min_children,
                        },
                    )
                if page.keys != sorted(
                    page.keys,
                    key=lambda key: key_token(key, self.key_types),
                ):
                    raise BusinessError(
                        E_INCONSISTENT_STATE,
                        "internal keys are not sorted",
                        {"index": self.name, "page_id": page.page_id},
                    )
                for child_id in page.children:
                    child = self.pages.get(child_id)
                    if child is None:
                        raise BusinessError(
                            E_BROKEN_PARENT_POINTER,
                            "internal page points to a missing child",
                            {"index": self.name, "page_id": page.page_id, "child": child_id},
                        )
                    if child.parent_id != page.page_id:
                        raise BusinessError(
                            E_BROKEN_PARENT_POINTER,
                            "child page parent pointer is broken",
                            {
                                "index": self.name,
                                "page_id": page.page_id,
                                "child": child_id,
                                "child_parent": child.parent_id,
                            },
                        )
                    if child.level + 1 != page.level:
                        raise BusinessError(
                            E_INCONSISTENT_STATE,
                            "child and parent levels are inconsistent",
                            {"index": self.name, "page_id": page.page_id, "child": child_id},
                        )
                    stack.append(child_id)
                for index, separator in enumerate(page.keys, start=1):
                    child = self.pages[page.children[index]]
                    actual = self._subtree_min_key(child)
                    if self._cmp_keys(actual, separator) != 0:
                        raise BusinessError(
                            E_INCONSISTENT_STATE,
                            "internal separator does not equal child subtree minimum",
                            {
                                "index": self.name,
                                "page_id": page.page_id,
                                "separator": separator,
                                "actual": actual,
                            },
                        )
        if reachable != set(self.pages):
            orphaned = sorted(set(self.pages) - reachable)
            raise BusinessError(
                E_BROKEN_PARENT_POINTER,
                "active pages are not reachable from the root",
                {"index": self.name, "orphaned_page_ids": orphaned},
            )
        return {
            "index": self.name,
            "page_count": len(self.pages),
            "height": self.height,
            "split_count": self.split_count,
            "root_page_id": self.root_id,
        }

    def _page_dict(self, page: Page, scale: int, rounding: str) -> dict[str, Any]:
        entry_count = len(page.entries) if page.leaf else len(page.children)
        item_count = (
            sum(len(entry["items"]) for entry in page.entries)
            if page.leaf
            else len(page.children)
        )
        result: dict[str, Any] = {
            "page_id": page.page_id,
            "leaf": page.leaf,
            "level": page.level,
            "parent_page_id": page.parent_id,
            "left_sibling_page_id": page.left_id,
            "right_sibling_page_id": page.right_id,
            "metrics": {
                "entry_count_units": fixed_units(entry_count, scale, rounding),
                "item_count_units": fixed_units(item_count, scale, rounding),
                "scale": scale,
                "rounding": rounding,
            },
        }
        if page.leaf:
            result["entries"] = [
                {
                    "key": list(entry["key"]),
                    "items": [
                        {"pk": list(item["pk"]), "cover": list(item["cover"])}
                        for item in entry["items"]
                    ],
                }
                for entry in page.entries
            ]
        else:
            result["keys"] = [list(key) for key in page.keys]
            result["children"] = list(page.children)
        return result

    def snapshot(self, *, scale: int, rounding: str) -> dict[str, Any]:
        pages = [
            self._page_dict(page, scale, rounding)
            for page in sorted(self.pages.values(), key=lambda item: item.page_id)
        ]
        page_digests = {
            page["page_id"]: sha256_json(page)
            for page in pages
        }
        core = {
            "name": self.name,
            "chain_version": self.chain_version,
            "split_count": self.split_count,
            "root_page_id": self.root_id,
            "height": self.height,
            "page_count": len(self.pages),
            "next_page_seq": self.next_page_seq,
            "min_entries": self.min_entries,
            "min_children": self.min_children,
            "retired_pages": sorted(
                self.retired_pages,
                key=lambda item: (item["chain_version"], item["page_id"]),
            ),
            "pages": pages,
            "page_digests": page_digests,
        }
        core["digest"] = sha256_json(core)
        return core

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        capacity: int,
        split_factor: float,
        max_height: int,
        key_types: tuple[str, ...],
        pk_types: tuple[str, ...],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> "BTree":
        try:
            tree = cls(
                snapshot["name"],
                capacity=capacity,
                split_factor=split_factor,
                max_height=max_height,
                key_types=key_types,
                pk_types=pk_types,
                chain_version=snapshot["chain_version"],
                split_count=snapshot["split_count"],
                event_callback=event_callback,
            )
            tree.root_id = snapshot["root_page_id"]
            tree.next_page_seq = snapshot["next_page_seq"]
            tree.retired_pages = list(snapshot.get("retired_pages", []))
            for raw_page in snapshot["pages"]:
                page = Page(
                    page_id=raw_page["page_id"],
                    leaf=raw_page["leaf"],
                    level=raw_page["level"],
                    parent_id=raw_page.get("parent_page_id"),
                    left_id=raw_page.get("left_sibling_page_id"),
                    right_id=raw_page.get("right_sibling_page_id"),
                )
                if page.leaf:
                    page.entries = [
                        {
                            "key": list(entry["key"]),
                            "items": [
                                {
                                    "pk": list(item["pk"]),
                                    "cover": list(item["cover"]),
                                }
                                for item in entry["items"]
                            ],
                        }
                        for entry in raw_page["entries"]
                    ]
                else:
                    page.keys = [list(key) for key in raw_page["keys"]]
                    page.children = list(raw_page["children"])
                tree.pages[page.page_id] = page
            return tree
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                E_INCONSISTENT_STATE,
                "stored tree snapshot is malformed",
                {"reason": str(exc)},
            ) from exc
