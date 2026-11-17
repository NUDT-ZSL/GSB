"""Runtime client engine: executes a planned frame sequence against the
loopback peer over a real HTTP/2 connection and records what actually
happened (frames sent, WINDOW_UPDATE increments observed, stall segments
with monotonic-clock milliseconds).
"""

import socket
import time

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h2.settings

from .errors import E_PROTO
from .ledger import WindowLedger
from .manifest import INITIAL_CONNECTION_WINDOW

_SOCKET_TIMEOUT_S = 15.0


def _monotonic_ms():
    return int(time.monotonic() * 1000)


class RunResult:
    def __init__(self):
        self.frames = []          # observed sent frames (decl stream ids)
        self.grants = []          # observed WINDOW_UPDATE increments
        self.stalls = []          # window-exhaustion stall segments
        self.proto_errors = []    # E_PROTO diagnostic dicts
        self.ledger = None
        self.actual_sent = {}     # decl stream id -> DATA bytes sent


class ClientEngine:
    """Executes ``plan`` against the peer on 127.0.0.1:``port``."""

    def __init__(self, manifest, plan, port):
        self.manifest = manifest
        self.plan = plan
        self.port = port
        self.result = RunResult()

    def run(self):
        manifest = self.manifest
        conn_cfg = manifest["connection"]
        result = self.result
        ledger = WindowLedger(
            [s["id"] for s in manifest["streams"]],
            conn_cfg["client_send"]["stream_window"],
            conn_cfg["client_send"]["connection_window"],
        )
        result.ledger = ledger
        result.actual_sent = {s["id"]: 0 for s in manifest["streams"]}

        sock = socket.create_connection(("127.0.0.1", self.port),
                                        timeout=_SOCKET_TIMEOUT_S)
        last_frame = {"type": None, "stream_id": None}
        with sock:
            conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True))
            conn.initiate_connection()
            conn.update_settings({
                h2.settings.SettingCodes.INITIAL_WINDOW_SIZE:
                    conn_cfg["server_send"]["stream_window"],
                h2.settings.SettingCodes.MAX_FRAME_SIZE:
                    conn_cfg["max_frame_size"],
                h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS:
                    conn_cfg["max_concurrent_streams"],
            })
            server_conn_delta = (conn_cfg["server_send"]["connection_window"]
                                 - INITIAL_CONNECTION_WINDOW)
            if server_conn_delta > 0:
                conn.increment_flow_control_window(server_conn_delta)
            sock.sendall(conn.data_to_send())

            decl_to_real = {}
            real_to_decl = {}
            closed_decl = set()
            remaining = {s["id"]: s["body_bytes"]
                         for s in manifest["streams"]}
            opened_real = set()       # real ids we opened
            local_ended = set()       # real ids where we sent END_STREAM
            remote_ended = set()      # real ids where the peer ended

            def h2_open_count():
                both = local_ended & remote_ended
                return len(opened_real - both)

            def record_proto(frame_type, stream_id, exc):
                result.proto_errors.append({
                    "code": E_PROTO, "frame_type": frame_type,
                    "stream_id": stream_id,
                    "detail": f"{type(exc).__name__}: {exc}",
                })

            def process_events(events):
                for event in events:
                    if isinstance(event, h2.events.WindowUpdated):
                        sid = event.stream_id
                        decl = 0 if sid == 0 else real_to_decl.get(sid)
                        if sid != 0 and decl is None:
                            continue
                        ledger.grant(decl, event.delta)
                        result.grants.append((decl, event.delta))
                    elif isinstance(event, h2.events.DataReceived):
                        size = event.flow_controlled_length
                        conn.acknowledge_received_data(size,
                                                       event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        remote_ended.add(event.stream_id)
                        decl = real_to_decl.get(event.stream_id)
                        if decl is not None:
                            closed_decl.add(decl)
                    elif isinstance(event, h2.events.StreamReset):
                        decl = real_to_decl.get(event.stream_id)
                        result.proto_errors.append({
                            "code": E_PROTO, "frame_type": "RST_STREAM",
                            "stream_id": decl,
                            "detail": "peer reset stream "
                                      f"{event.stream_id}",
                        })

            def recv_events():
                data = sock.recv(65535)
                if not data:
                    raise DiagEOF("peer closed connection unexpectedly")
                process_events(conn.receive_data(data))
                outbound = conn.data_to_send()
                if outbound:
                    sock.sendall(outbound)

            # Wait for the peer's SETTINGS (initial window + setup grant).
            expected_window = conn_cfg["client_send"]["stream_window"]
            while conn.remote_settings.initial_window_size != expected_window:
                recv_events()

            try:
                for frame in self.plan.frames:
                    ftype = frame["type"]
                    decl = frame["stream_id"]
                    last_frame = {"type": ftype, "stream_id": decl}
                    if ftype == "HEADERS":
                        max_open = conn_cfg["max_concurrent_streams"]
                        while h2_open_count() >= max_open:
                            recv_events()
                        real = conn.get_next_available_stream_id()
                        opened_real.add(real)
                        decl_to_real[decl] = real
                        real_to_decl[real] = decl
                        stream = next(
                            s for s in manifest["streams"]
                            if s["id"] == decl)
                        depends = stream["depends_on"]
                        kwargs = {}
                        if depends:
                            kwargs["priority_depends_on"] = (
                                decl_to_real.get(depends))
                        conn.send_headers(
                            real, [
                                (":method", "POST"),
                                (":scheme", "http"),
                                (":authority", "127.0.0.1"),
                                (":path", stream["path"]),
                            ],
                            end_stream=frame["end_stream"],
                            priority_weight=stream["weight"],
                            **kwargs)
                        sock.sendall(conn.data_to_send())
                        if frame["end_stream"]:
                            local_ended.add(real)
                        result.frames.append({
                            "type": "HEADERS", "stream_id": decl,
                            "bytes": 0,
                            "end_stream": frame["end_stream"],
                        })
                    else:  # DATA
                        need = frame["bytes"]
                        stall_start = None
                        while (ledger.window(decl) < need
                               or ledger.connection.window < need):
                            if stall_start is None:
                                stall_start = _monotonic_ms()
                            recv_events()
                        if stall_start is not None:
                            result.stalls.append({
                                "start_ms": stall_start,
                                "end_ms": _monotonic_ms(),
                                "streams": [decl],
                                "connection_blocked":
                                    ledger.connection.window < need,
                            })
                        ledger.send(decl, need)
                        conn.send_data(decl_to_real[decl], b"\x00" * need,
                                       end_stream=frame["end_stream"])
                        if frame["end_stream"]:
                            local_ended.add(decl_to_real[decl])
                        sock.sendall(conn.data_to_send())
                        remaining[decl] -= need
                        result.actual_sent[decl] += need
                        result.frames.append({
                            "type": "DATA", "stream_id": decl,
                            "bytes": need,
                            "end_stream": frame["end_stream"],
                        })
                # Drain until every stream has ended.
                deadline = time.monotonic() + _SOCKET_TIMEOUT_S
                while len(closed_decl) < len(manifest["streams"]):
                    if time.monotonic() > deadline:
                        raise DiagEOF("timed out waiting for stream ends")
                    recv_events()
            except h2.exceptions.H2Error as exc:
                record_proto(last_frame["type"], last_frame["stream_id"],
                             exc)
            except (OSError, socket.timeout, DiagEOF) as exc:
                result.proto_errors.append({
                    "code": E_PROTO, "frame_type": last_frame["type"],
                    "stream_id": last_frame["stream_id"],
                    "detail": str(exc),
                })
        return result


class DiagEOF(Exception):
    pass
