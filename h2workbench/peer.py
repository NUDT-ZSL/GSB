"""Minimal loopback HTTP/2 peer (server side).

Speaks HTTP/2 over a local TCP loopback socket using h2 for framing and
HPACK. It never contacts any real site. WINDOW_UPDATE frames are emitted
by the deterministic peer rule documented in planner.py, so the client
planner can predict every increment byte-exactly.
"""

import socket
import threading

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h2.settings

from .errors import E_PROTO
from .manifest import INITIAL_CONNECTION_WINDOW

_SOCKET_TIMEOUT_S = 15.0

_FRAME_TYPE_NAMES = {
    0x0: "DATA", 0x1: "HEADERS", 0x2: "PRIORITY", 0x3: "RST_STREAM",
    0x4: "SETTINGS", 0x5: "PUSH_PROMISE", 0x6: "PING", 0x7: "GOAWAY",
    0x8: "WINDOW_UPDATE", 0x9: "CONTINUATION",
}


class _FrameSniffer:
    """Incremental HTTP/2 frame-header parser used only to attach precise
    frame type / stream id context to E_PROTO diagnostics."""

    PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

    def __init__(self):
        self._buffer = bytearray()
        self._preface_stripped = False
        self.last_type = None
        self.last_stream_id = None

    def feed(self, data):
        self._buffer.extend(data)
        if not self._preface_stripped:
            if len(self._buffer) < len(self.PREFACE):
                return
            if bytes(self._buffer[:len(self.PREFACE)]) == self.PREFACE:
                del self._buffer[:len(self.PREFACE)]
            self._preface_stripped = True
        while len(self._buffer) >= 9:
            length = int.from_bytes(self._buffer[0:3], "big")
            if len(self._buffer) < 9 + length:
                break
            frame_type = self._buffer[3]
            stream_id = (int.from_bytes(self._buffer[5:9], "big")
                         & 0x7FFFFFFF)
            self.last_type = _FRAME_TYPE_NAMES.get(frame_type,
                                                   f"UNKNOWN({frame_type})")
            self.last_stream_id = stream_id
            del self._buffer[:9 + length]


class PeerResult:
    def __init__(self):
        self.grants = []          # ordered (decl_stream_id, increment)
        self.proto_errors = []    # E_PROTO diagnostic dicts
        self.frames_received = 0
        self.responses_sent = 0
        self.port = None


class LoopbackPeer:
    """Deterministic HTTP/2 peer running in a background thread."""

    def __init__(self, manifest):
        self.manifest = manifest
        self.result = PeerResult()
        self.ready = threading.Event()
        self.finished = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="h2workbench-peer")

    def start(self):
        self._thread.start()

    def wait_ready(self, timeout=10.0):
        if not self.ready.wait(timeout):
            raise RuntimeError("peer failed to bind loopback port")
        return self.result.port

    def join(self, timeout=30.0):
        self.finished.wait(timeout)

    # ------------------------------------------------------------------
    def _serve(self):
        manifest = self.manifest
        conn_cfg = manifest["connection"]
        peer_cfg = manifest["peer"]
        grant_increment = peer_cfg["grant_increment"]
        response_bytes = peer_cfg["response_bytes"]
        expected_streams = len(manifest["streams"])

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(_SOCKET_TIMEOUT_S)
            self.result.port = listener.getsockname()[1]
            self.ready.set()
            sock, _ = listener.accept()
        except OSError as exc:
            self.result.proto_errors.append({
                "code": E_PROTO, "frame_type": "LISTEN",
                "stream_id": None, "detail": str(exc),
            })
            self.ready.set()
            self.finished.set()
            return
        finally:
            listener.close()

        with sock:
            sock.settimeout(_SOCKET_TIMEOUT_S)
            conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False))
            conn.initiate_connection()
            conn.update_settings({
                h2.settings.SettingCodes.INITIAL_WINDOW_SIZE:
                    conn_cfg["client_send"]["stream_window"],
                h2.settings.SettingCodes.MAX_FRAME_SIZE:
                    conn_cfg["max_frame_size"],
                h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS:
                    conn_cfg["max_concurrent_streams"],
            })
            setup_increment = (conn_cfg["client_send"]["connection_window"]
                               - INITIAL_CONNECTION_WINDOW)
            if setup_increment > 0:
                conn.increment_flow_control_window(setup_increment)
                self.result.grants.append((0, setup_increment))
            sock.sendall(conn.data_to_send())

            # Deterministic peer rule state.
            consumed_since_grant = {}
            conn_consumed = 0
            stream_window = {}      # mirrored client send window per stream
            conn_window = conn_cfg["client_send"]["connection_window"]
            open_order = []         # real stream ids in arrival order
            ended_streams = set()
            responses_pending = []
            sniffer = _FrameSniffer()

            def decl_id(real_id):
                try:
                    index = open_order.index(real_id)
                except ValueError:
                    return None
                return self.manifest["streams"][index]["id"]

            def emit_grant(real_id, amount):
                nonlocal conn_window
                if real_id == 0:
                    conn_window += amount
                    conn.increment_flow_control_window(amount)
                    self.result.grants.append((0, amount))
                else:
                    stream_window[real_id] += amount
                    conn.increment_flow_control_window(amount, real_id)
                    self.result.grants.append((decl_id(real_id), amount))

            response_state = {"sent": 0, "headers_sent": False}

            def send_responses():
                while responses_pending:
                    real_id = responses_pending[0]
                    try:
                        # Already the minimum of stream and connection
                        # outbound windows.
                        window = conn.local_flow_control_window(real_id)
                    except h2.exceptions.StreamClosedError:
                        responses_pending.pop(0)
                        response_state["sent"] = 0
                        response_state["headers_sent"] = False
                        continue
                    if not response_state["headers_sent"]:
                        conn.send_headers(real_id, [
                            (":status", "200"),
                            ("content-type", "application/octet-stream"),
                        ], end_stream=(response_bytes == 0))
                        response_state["headers_sent"] = True
                        if response_bytes == 0:
                            self.result.responses_sent += 1
                            responses_pending.pop(0)
                            response_state["sent"] = 0
                            response_state["headers_sent"] = False
                            continue
                    if window <= 0:
                        break  # wait for client WINDOW_UPDATE
                    remaining = response_bytes - response_state["sent"]
                    chunk = min(remaining, conn.max_outbound_frame_size,
                                window)
                    if chunk <= 0:
                        break
                    response_state["sent"] += chunk
                    end = response_state["sent"] >= response_bytes
                    conn.send_data(real_id, b"\x00" * chunk, end_stream=end)
                    if end:
                        self.result.responses_sent += 1
                        responses_pending.pop(0)
                        response_state["sent"] = 0
                        response_state["headers_sent"] = False
                sock.sendall(conn.data_to_send())

            try:
                while (len(ended_streams) < expected_streams
                        or responses_pending):
                    data = sock.recv(65535)
                    if not data:
                        break
                    sniffer.feed(data)
                    events = conn.receive_data(data)
                    for event in events:
                        if isinstance(event, h2.events.RequestReceived):
                            self.result.frames_received += 1
                            open_order.append(event.stream_id)
                            stream_window[event.stream_id] = (
                                conn_cfg["client_send"]["stream_window"])
                            consumed_since_grant[event.stream_id] = 0
                        elif isinstance(event, h2.events.DataReceived):
                            self.result.frames_received += 1
                            size = event.flow_controlled_length
                            rid = event.stream_id
                            stream_window[rid] -= size
                            conn_window -= size
                            consumed_since_grant[rid] += size
                            conn_consumed += size
                            if (consumed_since_grant[rid] >= grant_increment
                                    or stream_window[rid] == 0):
                                amount = consumed_since_grant[rid]
                                consumed_since_grant[rid] = 0
                                emit_grant(rid, amount)
                            if (conn_consumed >= grant_increment
                                    or conn_window == 0):
                                amount = conn_consumed
                                conn_consumed = 0
                                emit_grant(0, amount)
                        elif isinstance(event, h2.events.StreamEnded):
                            rid = event.stream_id
                            ended_streams.add(rid)
                            if consumed_since_grant.get(rid, 0) > 0:
                                amount = consumed_since_grant[rid]
                                consumed_since_grant[rid] = 0
                                emit_grant(rid, amount)
                            responses_pending.append(rid)
                        elif isinstance(event, h2.events.WindowUpdated):
                            send_responses()
                        elif isinstance(event, h2.events.StreamReset):
                            self.result.proto_errors.append({
                                "code": E_PROTO, "frame_type": "RST_STREAM",
                                "stream_id": decl_id(event.stream_id),
                                "detail": "peer received unexpected "
                                          "RST_STREAM",
                            })
                    send_responses()
                    sock.sendall(conn.data_to_send())
            except h2.exceptions.H2Error as exc:
                raw_stream = sniffer.last_stream_id
                self.result.proto_errors.append({
                    "code": E_PROTO,
                    "frame_type": sniffer.last_type,
                    "stream_id": decl_id(raw_stream) if raw_stream else None,
                    "detail": f"{type(exc).__name__}: {exc}",
                })
            except (OSError, socket.timeout) as exc:
                self.result.proto_errors.append({
                    "code": E_PROTO, "frame_type": "SOCKET",
                    "stream_id": None, "detail": str(exc),
                })
            finally:
                self.finished.set()
