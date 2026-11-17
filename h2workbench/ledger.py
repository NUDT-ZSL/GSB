"""Flow-control window ledger.

Tracks, per stream and at connection level: bytes sent, window remaining
and WINDOW_UPDATE increments. Window remaining must never go negative;
a violation is a hard ledger error (E_WINDOW).
"""

from .errors import DiagError, E_WINDOW

CONNECTION_STREAM_ID = 0


class StreamLedger:
    __slots__ = ("stream_id", "sent", "window", "increments",
                 "window_curve")

    def __init__(self, stream_id, initial_window):
        self.stream_id = stream_id
        self.sent = 0
        self.window = initial_window
        self.increments = []  # ordered WINDOW_UPDATE increments observed
        self.window_curve = [initial_window]  # window after each event

    def to_dict(self):
        return {"stream_id": self.stream_id, "sent": self.sent,
                "window": self.window, "increments": list(self.increments),
                "window_curve": list(self.window_curve)}

    @classmethod
    def from_dict(cls, data):
        ledger = cls(data["stream_id"], data["window"])
        ledger.sent = data["sent"]
        ledger.increments = list(data["increments"])
        ledger.window_curve = list(data.get("window_curve",
                                            [data["window"]]))
        return ledger


class WindowLedger:
    """Connection-level plus per-stream send window accounting."""

    def __init__(self, stream_ids, stream_window, connection_window):
        self.connection = StreamLedger(CONNECTION_STREAM_ID,
                                       connection_window)
        self.streams = {sid: StreamLedger(sid, stream_window)
                        for sid in stream_ids}

    def send(self, stream_id, nbytes):
        for ledger in (self.streams[stream_id], self.connection):
            ledger.window -= nbytes
            ledger.sent += nbytes
            if ledger.window < 0:
                raise DiagError(
                    E_WINDOW,
                    f"window underflow on stream {ledger.stream_id}: "
                    f"{ledger.window} after sending {nbytes} bytes",
                    details={"stream_id": ledger.stream_id,
                             "window": ledger.window})
            ledger.window_curve.append(ledger.window)

    def grant(self, stream_id, increment):
        ledger = self.connection if stream_id == CONNECTION_STREAM_ID \
            else self.streams[stream_id]
        ledger.window += increment
        ledger.increments.append(increment)
        ledger.window_curve.append(ledger.window)

    def window(self, stream_id):
        return self.streams[stream_id].window

    def to_dict(self):
        return {
            "connection": self.connection.to_dict(),
            "streams": [self.streams[sid].to_dict()
                        for sid in sorted(self.streams)],
        }

    @classmethod
    def from_dict(cls, data):
        ledger = cls([], 0, 0)
        ledger.connection = StreamLedger.from_dict(data["connection"])
        ledger.streams = {entry["stream_id"]: StreamLedger.from_dict(entry)
                          for entry in data["streams"]}
        return ledger


def reconcile_grants(expected, observed):
    """Item-by-item reconciliation of WINDOW_UPDATE increments.

    ``expected``/``observed`` are ordered lists of (stream_id, increment).
    Returns None when identical, otherwise a dict describing the first
    deviation (used to raise E_WINDOW).
    """
    for index, (exp, obs) in enumerate(zip(expected, observed)):
        if exp != obs:
            return {
                "index": index,
                "stream_id": exp[0],
                "expected_increment": exp[1],
                "observed_stream_id": obs[0],
                "observed_increment": obs[1],
                "deviation_bytes": obs[1] - exp[1],
            }
    if len(expected) != len(observed):
        shorter = expected if len(expected) < len(observed) else observed
        longer = observed if len(expected) < len(observed) else expected
        extra = longer[len(shorter)]
        return {
            "index": len(shorter),
            "stream_id": extra[0],
            "expected_increment": None,
            "observed_stream_id": extra[0],
            "observed_increment": extra[1],
            "deviation_bytes": extra[1],
        }
    return None
