"""Deterministic local score table (logits per context fingerprint and step)."""

from __future__ import annotations

import hashlib
import json

from .errors import E_SCORE_EMPTY, E_SCORE_MISSING, WorkbenchError


class ScoreTable:
    def __init__(self, entries):
        # entries: {context_fingerprint: [[logits per step], ...]}
        self._entries = dict(entries)
        blob = json.dumps(self._entries, sort_keys=True, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path):
        entries = {}
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                entries[rec["fingerprint"]] = rec["steps"]
        return cls(entries)

    def fingerprints(self):
        return sorted(self._entries)

    def num_steps(self, context_fp):
        if context_fp not in self._entries:
            raise WorkbenchError(
                E_SCORE_MISSING, f"no score entry for context {context_fp!r}"
            )
        steps = self._entries[context_fp]
        if not steps:
            raise WorkbenchError(
                E_SCORE_EMPTY, f"empty score entry for context {context_fp!r}"
            )
        return len(steps)

    def logits(self, context_fp, step):
        n = self.num_steps(context_fp)
        if step >= n:
            raise WorkbenchError(
                E_SCORE_MISSING,
                f"score entry for {context_fp!r} has {n} steps, step {step} missing",
            )
        return self._entries[context_fp][step]
