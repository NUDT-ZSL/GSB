from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Segment:
    start: int
    end: int
    sent_at_ms: int
    retransmits: int = 0

    def clone(self) -> "Segment":
        return Segment(
            start=self.start,
            end=self.end,
            sent_at_ms=self.sent_at_ms,
            retransmits=self.retransmits,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "sent_at_ms": self.sent_at_ms,
            "retransmits": self.retransmits,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Segment":
        return cls(
            start=int(value["start"]),
            end=int(value["end"]),
            sent_at_ms=int(value["sent_at_ms"]),
            retransmits=int(value.get("retransmits", 0)),
        )


@dataclass
class TcpState:
    tcp_state: str
    cwnd_fixed: int
    ssthresh_fixed: int
    snd_una: int = 0
    snd_nxt: int = 0
    app_end: int = 0
    peer_window: int = 0
    retry_count: int = 0
    rto_ms: int = 0
    deadline_ms: int | None = None
    dup_ack_count: int = 0
    in_recovery: bool = False
    recover_seq: int = 0
    segments: list[Segment] = field(default_factory=list)
    peer_data_received: int = 0
    peer_data_read: int = 0
    failed_code: str | None = None
    last_ts_ms: int = 0

    def clone(self) -> "TcpState":
        return TcpState(
            tcp_state=self.tcp_state,
            cwnd_fixed=self.cwnd_fixed,
            ssthresh_fixed=self.ssthresh_fixed,
            snd_una=self.snd_una,
            snd_nxt=self.snd_nxt,
            app_end=self.app_end,
            peer_window=self.peer_window,
            retry_count=self.retry_count,
            rto_ms=self.rto_ms,
            deadline_ms=self.deadline_ms,
            dup_ack_count=self.dup_ack_count,
            in_recovery=self.in_recovery,
            recover_seq=self.recover_seq,
            segments=[segment.clone() for segment in self.segments],
            peer_data_received=self.peer_data_received,
            peer_data_read=self.peer_data_read,
            failed_code=self.failed_code,
            last_ts_ms=self.last_ts_ms,
        )

    @property
    def in_flight(self) -> int:
        return max(0, self.snd_nxt - self.snd_una)

    @property
    def unsent(self) -> int:
        return max(0, self.app_end - self.snd_nxt)

    def trim_acked(self, ack_no: int) -> None:
        kept: list[Segment] = []
        for segment in self.segments:
            if segment.end <= ack_no:
                continue
            clone = segment.clone()
            if clone.start < ack_no:
                clone.start = ack_no
            kept.append(clone)
        self.segments = kept

    def to_dict(self) -> dict[str, Any]:
        return {
            "tcp_state": self.tcp_state,
            "cwnd_fixed": self.cwnd_fixed,
            "ssthresh_fixed": self.ssthresh_fixed,
            "snd_una": self.snd_una,
            "snd_nxt": self.snd_nxt,
            "app_end": self.app_end,
            "peer_window": self.peer_window,
            "retry_count": self.retry_count,
            "rto_ms": self.rto_ms,
            "deadline_ms": self.deadline_ms,
            "dup_ack_count": self.dup_ack_count,
            "in_recovery": self.in_recovery,
            "recover_seq": self.recover_seq,
            "segments": [segment.to_dict() for segment in self.segments],
            "peer_data_received": self.peer_data_received,
            "peer_data_read": self.peer_data_read,
            "failed_code": self.failed_code,
            "last_ts_ms": self.last_ts_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TcpState":
        return cls(
            tcp_state=str(value["tcp_state"]),
            cwnd_fixed=int(value["cwnd_fixed"]),
            ssthresh_fixed=int(value["ssthresh_fixed"]),
            snd_una=int(value.get("snd_una", 0)),
            snd_nxt=int(value.get("snd_nxt", 0)),
            app_end=int(value.get("app_end", 0)),
            peer_window=int(value.get("peer_window", 0)),
            retry_count=int(value.get("retry_count", 0)),
            rto_ms=int(value.get("rto_ms", 0)),
            deadline_ms=(
                None
                if value.get("deadline_ms") is None
                else int(value["deadline_ms"])
            ),
            dup_ack_count=int(value.get("dup_ack_count", 0)),
            in_recovery=bool(value.get("in_recovery", False)),
            recover_seq=int(value.get("recover_seq", 0)),
            segments=[
                Segment.from_dict(segment) for segment in value.get("segments", [])
            ],
            peer_data_received=int(value.get("peer_data_received", 0)),
            peer_data_read=int(value.get("peer_data_read", 0)),
            failed_code=value.get("failed_code"),
            last_ts_ms=int(value.get("last_ts_ms", 0)),
        )

