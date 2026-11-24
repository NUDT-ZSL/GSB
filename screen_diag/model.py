from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Screen:
    screen_id: str
    canonical_id: str
    role: str
    width_px: int
    height_px: int
    scale: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Session:
    session_id: str
    multicast_group: str
    protocol: str
    sender_id: str
    start_logic_ms: int
    screens: List[Screen]

    @property
    def screen_map(self) -> Dict[str, Screen]:
        return {screen.screen_id: screen for screen in self.screens}

    @property
    def canonical_map(self) -> Dict[str, Screen]:
        return {screen.canonical_id: screen for screen in self.screens}


@dataclass(frozen=True)
class Manifest:
    seed: int
    min_logic_ms: int
    max_logic_ms: int
    late_tolerance_ms: int
    chunk_interval_ms: int
    sessions: List[Session]
    raw: Dict[str, Any] = field(hash=False, compare=False, default_factory=dict)

    @property
    def session_map(self) -> Dict[str, Session]:
        return {session.session_id: session for session in self.sessions}

    @property
    def screen_index(self) -> Dict[str, tuple[Session, Screen]]:
        index: Dict[str, tuple[Session, Screen]] = {}
        for session in self.sessions:
            for screen in session.screens:
                index[screen.screen_id] = (session, screen)
        return index

    @property
    def canonical_index(self) -> Dict[str, tuple[Session, Screen]]:
        index: Dict[str, tuple[Session, Screen]] = {}
        for session in self.sessions:
            for screen in session.screens:
                index[screen.canonical_id] = (session, screen)
        return index


@dataclass(frozen=True)
class FrameEvent:
    event_seq: int
    session_id: str
    screen_id: str
    frame_seq: int
    sampled_at_ms: int
    is_keyframe: bool
    payload_digest: str
    generation: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScreenState:
    screen_id: str
    canonical_id: str
    session_id: str
    generation: int = 1
    keyframe_seq: Optional[int] = None
    decoded_frame_seq: Optional[int] = None
    play_position_ms: Optional[int] = None
    clock_ms: Optional[int] = None
    status: str = "idle"

    def copy(self) -> "ScreenState":
        return ScreenState(**asdict(self))

    def comparable(self) -> Dict[str, Any]:
        return {
            "generation": self.generation,
            "keyframe_seq": self.keyframe_seq,
            "play_position_ms": self.play_position_ms,
        }


@dataclass
class Decision:
    event_seq: int
    screen_id: str
    canonical_id: str
    logic_ms: int
    verdict: str
    reason: str
    generation_before: int
    generation_after: int
    late_by_ms: int = 0
    rebuild_keyframe_seq: Optional[int] = None
    replayed_frame_count: int = 0
    state_after: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayResult:
    states: Dict[str, List[Dict[str, Any]]]
    decisions: List[Decision]
    counts_by_screen: Dict[str, Dict[str, int]]
    checkpoints: Dict[str, List[Dict[str, Any]]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "states": self.states,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "counts_by_screen": self.counts_by_screen,
            "checkpoints": self.checkpoints,
        }


@dataclass(frozen=True)
class SnapshotRecord:
    canonical_screen_id: str
    logic_ms: int
    generation: int
    keyframe_seq: Optional[int]
    play_position_ms: Optional[int]
    source_kind: str
    source_locator: str
    screen_id: Optional[str] = None
    decoded_frame_seq: Optional[int] = None
    tombstone: bool = False
    recorded_at_ms: Optional[int] = None
    shard_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
