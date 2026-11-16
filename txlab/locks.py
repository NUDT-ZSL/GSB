"""Application-level row lock manager with stable FIFO wait queues."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


S_LOCK = "S"
X_LOCK = "X"


@dataclass
class LockTransaction:
    tx_id: str
    session_id: str
    start_seq: int
    isolation: str


@dataclass
class LockWaiter:
    tx_id: str
    session_id: str
    row_key: str
    mode: str
    op_index: int
    requested_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "session_id": self.session_id,
            "row_key": self.row_key,
            "mode": self.mode,
            "op_index": self.op_index,
            "requested_at_ms": self.requested_at_ms,
        }


@dataclass
class RowLockState:
    holders: dict[str, str] = field(default_factory=dict)
    waiters: list[LockWaiter] = field(default_factory=list)


@dataclass
class AcquireResult:
    granted: bool
    newly_acquired: bool = False
    already_held_mode: str | None = None
    queue_position: int | None = None
    blockers: list[str] = field(default_factory=list)
    waiters: list[LockWaiter] = field(default_factory=list)


class LockManager:
    def __init__(self) -> None:
        self.transactions: dict[str, LockTransaction] = {}
        self.rows: dict[str, RowLockState] = {}

    def begin_transaction(
        self,
        tx_id: str,
        session_id: str,
        start_seq: int,
        isolation: str,
    ) -> None:
        if tx_id in self.transactions:
            raise ValueError(f"duplicate transaction id {tx_id}")
        self.transactions[tx_id] = LockTransaction(
            tx_id=tx_id,
            session_id=session_id,
            start_seq=start_seq,
            isolation=isolation,
        )

    def end_transaction(self, tx_id: str) -> list[dict[str, Any]]:
        """Release every holder/waiter owned by a transaction.

        Releasing does not grant waiters by itself; the scheduler observes and
        retries them in deterministic queue order.
        """

        released: list[dict[str, Any]] = []
        for row_key, row in self.rows.items():
            if tx_id in row.holders:
                mode = row.holders.pop(tx_id)
                released.append({"row_key": row_key, "mode": mode, "scope": "transaction"})
            row.waiters = [waiter for waiter in row.waiters if waiter.tx_id != tx_id]
        self.transactions.pop(tx_id, None)
        self.rows = {
            key: row
            for key, row in self.rows.items()
            if row.holders or row.waiters
        }
        return released

    def holder_mode(self, tx_id: str, row_key: str) -> str | None:
        row = self.rows.get(row_key)
        return row.holders.get(tx_id) if row is not None else None

    def request(
        self,
        tx_id: str,
        row_key: str,
        mode: str,
        op_index: int,
        now_ms: int,
    ) -> AcquireResult:
        if tx_id not in self.transactions:
            raise ValueError(f"unknown transaction {tx_id}")
        if mode not in (S_LOCK, X_LOCK):
            raise ValueError(f"unknown lock mode {mode}")

        row = self.rows.setdefault(row_key, RowLockState())
        held_mode = row.holders.get(tx_id)
        if held_mode == X_LOCK or (mode == S_LOCK and held_mode == S_LOCK):
            return AcquireResult(
                granted=True,
                already_held_mode=held_mode,
                waiters=list(row.waiters),
            )

        existing = next(
            (
                waiter
                for waiter in row.waiters
                if waiter.tx_id == tx_id and waiter.row_key == row_key
            ),
            None,
        )
        if existing is not None:
            return self.retry(tx_id)

        if not row.waiters and self._holders_compatible(tx_id, row, mode):
            row.holders[tx_id] = mode
            return AcquireResult(
                granted=True,
                newly_acquired=True,
                waiters=list(row.waiters),
            )

        waiter = LockWaiter(
            tx_id=tx_id,
            session_id=self.transactions[tx_id].session_id,
            row_key=row_key,
            mode=mode,
            op_index=op_index,
            requested_at_ms=now_ms,
        )
        row.waiters.append(waiter)
        return AcquireResult(
            granted=False,
            queue_position=len(row.waiters),
            blockers=self._blockers_for(row, len(row.waiters) - 1),
            waiters=list(row.waiters),
        )

    def retry(self, tx_id: str) -> AcquireResult:
        for row_key, row in self.rows.items():
            position = next(
                (
                    index
                    for index, waiter in enumerate(row.waiters)
                    if waiter.tx_id == tx_id
                ),
                None,
            )
            if position is None:
                continue
            waiter = row.waiters[position]
            if position == 0 and self._holders_compatible(tx_id, row, waiter.mode):
                row.waiters.pop(0)
                row.holders[tx_id] = waiter.mode
                return AcquireResult(
                    granted=True,
                    newly_acquired=True,
                    waiters=list(row.waiters),
                )
            return AcquireResult(
                granted=False,
                queue_position=position + 1,
                blockers=self._blockers_for(row, position),
                waiters=list(row.waiters),
            )
        return AcquireResult(granted=False)

    def release_statement_lock(self, tx_id: str, row_key: str) -> dict[str, Any] | None:
        row = self.rows.get(row_key)
        if row is None or row.holders.get(tx_id) != S_LOCK:
            return None
        row.holders.pop(tx_id, None)
        result = {"row_key": row_key, "mode": S_LOCK, "scope": "statement"}
        if not row.holders and not row.waiters:
            self.rows.pop(row_key, None)
        return result

    def queue_entry(self, tx_id: str) -> LockWaiter | None:
        for row in self.rows.values():
            for waiter in row.waiters:
                if waiter.tx_id == tx_id:
                    return waiter
        return None

    def queue_position(self, tx_id: str) -> int | None:
        for row in self.rows.values():
            for index, waiter in enumerate(row.waiters):
                if waiter.tx_id == tx_id:
                    return index + 1
        return None

    def held_locks(self, tx_id: str) -> list[dict[str, str]]:
        locks: list[dict[str, str]] = []
        for row_key in sorted(self.rows):
            mode = self.rows[row_key].holders.get(tx_id)
            if mode is not None:
                locks.append({"row_key": row_key, "mode": mode})
        return locks

    def rows_snapshot(self) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for row_key in sorted(self.rows):
            row = self.rows[row_key]
            holders = []
            for tx_id, mode in row.holders.items():
                info = self.transactions.get(tx_id)
                holders.append(
                    {
                        "tx_id": tx_id,
                        "session_id": info.session_id if info else None,
                        "mode": mode,
                    }
                )
            holders.sort(key=lambda item: (item["session_id"] or "", item["tx_id"]))
            snapshot.append(
                {
                    "row_key": row_key,
                    "holders": holders,
                    "waiters": [
                        {"position": index + 1, **waiter.to_dict()}
                        for index, waiter in enumerate(row.waiters)
                    ],
                }
            )
        return snapshot

    def wait_graph(self) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for row_key in sorted(self.rows):
            row = self.rows[row_key]
            for index, waiter in enumerate(row.waiters):
                blockers = self._blockers_for(row, index)
                blocker_rows = []
                for blocker_tx in blockers:
                    info = self.transactions.get(blocker_tx)
                    blocker_rows.append(
                        {
                            "tx_id": blocker_tx,
                            "session_id": info.session_id if info else None,
                        }
                    )
                blocker_rows.sort(
                    key=lambda item: (item["session_id"] or "", item["tx_id"])
                )
                edges.append(
                    {
                        "row_key": row_key,
                        "waiter_tx_id": waiter.tx_id,
                        "waiter_session_id": waiter.session_id,
                        "blocker_txs": blocker_rows,
                    }
                )
        edges.sort(
            key=lambda item: (
                item["waiter_session_id"] or "",
                item["waiter_tx_id"],
                item["row_key"],
            )
        )
        return edges

    def find_deadlock(self) -> dict[str, Any] | None:
        adjacency: dict[str, set[str]] = {
            tx_id: set() for tx_id in self.transactions
        }
        for edge in self.wait_graph():
            for blocker in edge["blocker_txs"]:
                if blocker["tx_id"] in adjacency:
                    adjacency[edge["waiter_tx_id"]].add(blocker["tx_id"])

        components = self._strongly_connected_components(adjacency)
        cycles: list[list[str]] = []
        for component in components:
            if len(component) > 1:
                cycles.append(component)
            elif component and component[0] in adjacency.get(component[0], set()):
                cycles.append(component)
        if not cycles:
            return None

        cycles.sort(
            key=lambda group: tuple(
                sorted(
                    (
                        self.transactions[tx_id].session_id
                        for tx_id in group
                        if tx_id in self.transactions
                    )
                )
            )
        )
        cycle_tx_ids = cycles[0]
        member_rows = []
        for tx_id in cycle_tx_ids:
            info = self.transactions.get(tx_id)
            if info is not None:
                member_rows.append(
                    {
                        "tx_id": tx_id,
                        "session_id": info.session_id,
                        "start_seq": info.start_seq,
                    }
                )
        member_rows.sort(key=lambda item: (item["session_id"], item["start_seq"]))
        victim = min(member_rows, key=lambda item: (item["session_id"], item["tx_id"]))
        cycle_session_ids = sorted(item["session_id"] for item in member_rows)
        return {
            "rule": "lowest_session_id",
            "cycle_members": cycle_session_ids,
            "cycle_transactions": member_rows,
            "victim_session_id": victim["session_id"],
            "victim_tx_id": victim["tx_id"],
            "wait_graph": self.wait_graph(),
        }

    def _holders_compatible(self, tx_id: str, row: RowLockState, mode: str) -> bool:
        for holder_tx, holder_mode in row.holders.items():
            if holder_tx == tx_id:
                continue
            if mode == X_LOCK:
                return False
            if holder_mode == X_LOCK:
                return False
        return True

    def _blockers_for(self, row: RowLockState, position: int) -> list[str]:
        waiter = row.waiters[position]
        blockers: set[str] = set()
        for holder_tx, holder_mode in row.holders.items():
            if holder_tx == waiter.tx_id:
                continue
            if waiter.mode == X_LOCK or holder_mode == X_LOCK:
                blockers.add(holder_tx)
        if position > 0:
            blockers.add(row.waiters[position - 1].tx_id)
        return sorted(
            blockers,
            key=lambda tx_id: (
                self.transactions[tx_id].session_id
                if tx_id in self.transactions
                else "",
                tx_id,
            ),
        )

    @staticmethod
    def _strongly_connected_components(
        adjacency: dict[str, set[str]]
    ) -> list[list[str]]:
        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        components: list[list[str]] = []

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for neighbor in sorted(adjacency.get(node, ())):
                if neighbor not in adjacency:
                    continue
                if neighbor not in indices:
                    visit(neighbor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
                elif neighbor in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[neighbor])

            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while stack:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

        for node in sorted(adjacency):
            if node not in indices:
                visit(node)
        return components

