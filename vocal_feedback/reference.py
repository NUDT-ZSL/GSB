from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pitch import midi_to_note_name, note_name_to_midi


@dataclass
class ReferenceNote:
    index: int
    start_s: float
    end_s: float
    midi: float
    note_name: str


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _parse_entry(index: int, entry: dict[str, Any]) -> ReferenceNote:
    start = _first(entry, ("start_s", "start", "begin", "from"))
    end = _first(entry, ("end_s", "end", "stop", "to"))
    interval = _first(entry, ("interval", "time", "time_interval"))
    if interval is not None and isinstance(interval, (list, tuple)) and len(interval) >= 2:
        start, end = interval[0], interval[1]
    pitch = _first(entry, ("pitch", "note", "note_name", "name", "midi", "midi_note"))
    if start is None or end is None or pitch is None:
        raise ValueError(f"reference note {index} needs start, end and pitch")
    start_s = float(start)
    end_s = float(end)
    if end_s <= start_s:
        raise ValueError(f"reference note {index} has non-positive duration")
    midi = note_name_to_midi(pitch)
    return ReferenceNote(
        index=index,
        start_s=start_s,
        end_s=end_s,
        midi=midi,
        note_name=str(pitch),
    )


def _load_json(path: Path) -> list[ReferenceNote]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        for key in ("notes", "reference_notes", "reference_pitch", "pitch"):
            if key in payload:
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError(f"{path}: reference JSON must contain a note list")
    notes = [_parse_entry(index, entry) for index, entry in enumerate(payload)]
    return notes


def _load_csv(path: Path) -> list[ReferenceNote]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        delimiter = "\t" if "\t" in sample and "," not in sample.splitlines()[0] else ","
        rows = list(csv.DictReader(handle, delimiter=delimiter))
    notes: list[ReferenceNote] = []
    for index, row in enumerate(rows):
        normalized = {str(key).strip().lower(): value for key, value in row.items() if key}
        notes.append(_parse_entry(index, normalized))
    return notes


def load_reference(path: str | Path) -> list[ReferenceNote]:
    path = Path(path)
    lower = path.name.lower()
    if lower.endswith(".json"):
        notes = _load_json(path)
    elif lower.endswith((".csv", ".tsv", ".txt")):
        notes = _load_csv(path)
    else:
        raise ValueError(f"{path}: unsupported reference pitch format")
    notes.sort(key=lambda note: (note.start_s, note.end_s))
    for index, note in enumerate(notes):
        note.index = index
    if not notes:
        raise ValueError(f"{path}: reference contains no notes")
    return notes


def serialize_reference(notes: list[ReferenceNote]) -> list[dict[str, Any]]:
    return [
        {
            "index": note.index,
            "start_s": round(note.start_s, 6),
            "end_s": round(note.end_s, 6),
            "pitch": midi_to_note_name(note.midi, cents=True),
            "midi": round(note.midi, 4),
        }
        for note in notes
    ]

