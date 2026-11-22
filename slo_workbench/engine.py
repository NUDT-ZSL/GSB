"""Clock-driven aggregation engine with recovery and idempotent commits.

Design properties required by the workbench contract:

* Every boundary decision comes from an *injected* logical clock (millisecond
  ticks); ``time.monotonic`` / wall clocks are never consulted.
* One tick per transaction, so a ``SIGKILL`` at any point leaves either the
  previous fully committed state or the next one — never a half-written window.
* Window commits are digest-idempotent; re-running over the same inputs never
  double counts budget because cumulative figures are derived from committed
  window rows rather than incrementally mutated totals.
* Late events only get a diagnostic row; closed window payloads never change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import aggregation as agg
from .errors import BusinessError, E_CLOCK_ROLLBACK
from .models import ClockConfig, Event, SLO
from .repository import Repository
from .storage import FileLock, Workspace, transaction

# Crash injection points (used by tests to simulate SIGKILL deterministically).
CRASH_AFTER_EVENT_INSERT = "after_event_insert"
CRASH_AFTER_WINDOW_INSERT = "after_window_insert"


@dataclass
class TickResult:
    tick_ms: int
    closed_windows: Dict[str, List[int]]
    late_events: Dict[str, int]
    errors: List[Dict[str, Any]]


class Engine:
    """High-level orchestration bound to one :class:`Workspace`."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    # ------------------------------------------------------------- processing
    def run(
        self,
        clock_config: ClockConfig,
        *,
        crash_after_tick: Optional[int] = None,
        crash_point: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Advance through every configured tick, returning a summary."""
        summary = {
            "ticks_processed": 0,
            "closed_windows": {},
            "late_events": {},
            "errors": [],
            "rollbacks": [],
        }
        for index, tick_ms in enumerate(clock_config.ticks_ms):
            result = self.advance(
                tick_ms,
                clock_config.rollback_tolerance_ms,
                crash=(
                    crash_after_tick == index
                    and (crash_point or CRASH_AFTER_WINDOW_INSERT)
                ),
            )
            summary["ticks_processed"] += 1
            for name, windows in result.closed_windows.items():
                summary["closed_windows"].setdefault(name, []).extend(windows)
            for name, count in result.late_events.items():
                summary["late_events"][name] = summary["late_events"].get(name, 0) + count
            summary["errors"].extend(result.errors)
        return summary

    def advance(
        self,
        tick_ms: int,
        rollback_tolerance_ms: int = 0,
        *,
        crash: Optional[str] = None,
    ) -> TickResult:
        """Process one injected clock tick under the cross-process file lock."""
        with FileLock(self.workspace):
            conn = self.workspace.connect()
            try:
                with transaction(conn):
                    return self._advance_locked(
                        Repository(conn), tick_ms, rollback_tolerance_ms, crash
                    )
            finally:
                conn.close()

    def _advance_locked(
        self,
        repo: Repository,
        tick_ms: int,
        rollback_tolerance_ms: int,
        crash: Optional[str],
    ) -> TickResult:
        slos = repo.current_slos()
        all_events = repo.stream_events()
        result = TickResult(tick_ms=tick_ms, closed_windows={}, late_events={}, errors=[])

        for slo in slos:
            watermark = repo.get_watermark(slo.name)
            last_clock = watermark["committed_clock"] if watermark else -1
            if tick_ms + rollback_tolerance_ms < last_clock:
                error = BusinessError(
                    E_CLOCK_ROLLBACK,
                    "injected clock moved backwards beyond tolerance",
                    fields={
                        "slo_name": slo.name,
                        "tick_ms": tick_ms,
                        "last_committed_clock": last_clock,
                        "tolerance_ms": rollback_tolerance_ms,
                    },
                )
                repo.log_error(error.code, error.message, error.fields)
                result.errors.append(error.to_dict())
                # One SLO's clock problem must not stop the other SLOs.
                continue

            committed_seq = watermark["committed_seq"] if watermark else 0
            next_window_no = watermark["next_window_no"] if watermark else 0
            already_ingested = repo.ingested_sequences(slo.name, slo.version)
            # Arrival model (deterministic watermark): at tick t, walk the
            # stream in sequence order and consume events while their logical
            # clock is <= t.  The first future event stops consumption, so an
            # old-clock event appended near the end of the stream only arrives
            # at a late tick and is correctly diagnosed as late.
            service_events: List[Event] = []
            for event in sorted(
                (e for e in all_events if e.service == slo.service),
                key=lambda item: item.seq,
            ):
                if event.seq in already_ingested:
                    continue
                if event.clock_ms > tick_ms:
                    # Arrival follows sequence order: the first event whose
                    # logical clock is still in the future blocks everything
                    # behind it, including old-clock tail events.
                    break
                service_events.append(event)

            max_seq = committed_seq
            for event in service_events:
                window_no = agg.home_window_no(event.clock_ms, slo.window_step_ms)
                if agg.is_late(event, window_no, slo, tick_ms):
                    window_end = agg.window_accepts_until(window_no, slo)
                    repo.record_late_event(
                        slo,
                        event,
                        window_no,
                        window_end,
                        reason=(
                            f"event clock {event.clock_ms}ms belongs to window "
                            f"{window_no} which closed at {window_end}ms "
                            f"(current tick {tick_ms}ms)"
                        ),
                    )
                    result.late_events[slo.name] = result.late_events.get(slo.name, 0) + 1
                    repo.mark_ingested(
                        slo.name, slo.version, event.seq, window_no, accepted=False
                    )
                    continue
                repo.mark_ingested(
                    slo.name, slo.version, event.seq, window_no, accepted=True
                )
                max_seq = max(max_seq, event.seq)

            closed: List[int] = []
            # Close every window whose acceptance horizon has been reached.
            while agg.window_accepts_until(next_window_no, slo) <= tick_ms:
                window_no = next_window_no
                events_for_window = repo.accepted_events(
                    slo.name, slo.version, window_no
                )
                self._close_window(repo, slo, window_no, events_for_window, result)
                closed.append(window_no)
                next_window_no += 1

            if closed:
                result.closed_windows[slo.name] = closed

            if crash == CRASH_AFTER_EVENT_INSERT:
                # Simulate SIGKILL: abort everything except simulate hard kill.
                raise _SimulatedCrash("simulated crash after event processing")

            repo.upsert_watermark(
                slo.name,
                slo.version,
                max_seq,
                max(last_clock, tick_ms),
                next_window_no,
            )

            if crash == CRASH_AFTER_WINDOW_INSERT:
                raise _SimulatedCrash("simulated crash after window insert, before commit")
        return result

    def _close_window(
        self,
        repo: Repository,
        slo: SLO,
        window_no: int,
        events: Sequence[Event],
        result: TickResult,
    ) -> None:
        start_ms, end_ms = agg.window_bounds(window_no, slo)
        try:
            payload = agg.compute_window(slo, window_no, events)
        except BusinessError as error:
            if error.code == "E_EMPTY_WINDOW":
                repo.commit_empty_window(
                    slo, window_no, start_ms, end_ms, error.fields.get("event_count", 0)
                )
                repo.log_error(error.code, error.message, error.fields)
                result.errors.append(error.to_dict())
                # Empty windows do not participate in alert streak evaluation.
                return
            raise

        inserted = repo.commit_window(slo, window_no, start_ms, end_ms, payload)
        if not inserted:
            # Identical duplicate conclusion: alert state was already advanced.
            return

        previous = repo.get_alert_state(slo.name)
        verdict = agg.adjudicate_alert(
            slo, window_no, payload["burn_exceeded"], previous
        )
        repo.save_alert_state(verdict)

    # ------------------------------------------------- explicit window ranges
    def process_window_range(
        self, slo_name: str, from_window: int, to_window: int
    ) -> Dict[str, Any]:
        """Aggregate an explicit window range (used by parallel workers).

        Idempotency is guaranteed by digest checks in ``commit_window`` and the
        advisory file lock; two workers racing on the same window produce one
        committed row and no duplicated budget.
        """
        with FileLock(self.workspace):
            conn = self.workspace.connect()
            try:
                with transaction(conn):
                    repo = Repository(conn)
                    slo = repo.get_slo(slo_name)
                    closed = []
                    for window_no in range(from_window, to_window + 1):
                        if repo.window_exists(slo.name, slo.version, window_no):
                            closed.append(window_no)
                            continue
                        members = repo.accepted_events(slo.name, slo.version, window_no)
                        placeholder = TickResult(
                            tick_ms=agg.window_accepts_until(window_no, slo),
                            closed_windows={},
                            late_events={},
                            errors=[],
                        )
                        self._close_window(repo, slo, window_no, members, placeholder)
                        closed.append(window_no)
                    return {"slo_name": slo_name, "windows": closed}
            finally:
                conn.close()

    # ------------------------------------------------------------- resume
    def resume(self, clock_config: Optional[ClockConfig] = None) -> Dict[str, Any]:
        """Reconcile persisted state after a hard kill.

        Replays the injected clock ticks against the durable event/ingestion
        tables.  Already committed windows are skipped by digest idempotency and
        cumulative figures are derived from window rows, so budget never double
        accumulates.  When no clock config is supplied, a synthetic monotonic
        tick series is derived from the max event clock instead.
        """
        report: Dict[str, Any] = {"recovered": {}, "errors": []}
        if clock_config is None:
            clock_config = self._synthetic_clock()

        before = self._window_counts()
        max_committed_clock = self._max_committed_clock()
        for tick_ms in clock_config.ticks_ms:
            # Only ticks beyond the durable high-water mark can close missing
            # windows; replaying earlier ticks would look like a rollback.
            if tick_ms <= max_committed_clock:
                continue
            tick = self.advance(tick_ms, clock_config.rollback_tolerance_ms)
            report["errors"].extend(tick.errors)
        after = self._window_counts()
        for slo_name, total in after.items():
            report["recovered"][slo_name] = {
                "recovered_windows": total - before.get(slo_name, 0),
                "committed_windows": total,
            }
        return report

    def _window_counts(self) -> Dict[str, int]:
        conn = self.workspace.connect()
        try:
            rows = conn.execute(
                "SELECT slo_name, COUNT(*) c FROM window GROUP BY slo_name"
            ).fetchall()
            return {row["slo_name"]: row["c"] for row in rows}
        finally:
            conn.close()

    def _max_committed_clock(self) -> int:
        conn = self.workspace.connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(committed_clock), -1) AS m FROM watermark"
            ).fetchone()
            return row["m"]
        finally:
            conn.close()

    def _synthetic_clock(self) -> ClockConfig:
        """Derive a monotonic tick series from persisted event clocks."""
        conn = self.workspace.connect()
        try:
            max_clock = conn.execute(
                "SELECT COALESCE(MAX(clock_ms), 0) AS m FROM event"
            ).fetchone()["m"]
            tolerance = int(self._meta_default(conn, "rollback_tolerance_ms", "0"))
        finally:
            conn.close()
        # Step every 1000ms up to the largest observed logical clock.
        ticks = list(range(1000, max(max_clock + 1, 1000), 1000))
        if not ticks or ticks[-1] < max_clock:
            ticks.append(max_clock)
        return ClockConfig(ticks_ms=ticks, rollback_tolerance_ms=tolerance)

    @staticmethod
    def _meta_default(conn, key: str, default: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


class _SimulatedCrash(RuntimeError):
    """Raised inside a transaction to force rollback (proxy for SIGKILL)."""
