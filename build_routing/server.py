from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .normalize import load_project
from .store import WorkbenchStore


def _parse_at(values: Mapping[str, list[str]]) -> int | None:
    if "at" not in values or not values["at"] or values["at"][0] == "":
        return None
    value = int(values["at"][0])
    if value < 0:
        raise ValueError("at must be non-negative")
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _summary_snapshot(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "prefix_id": item["prefix_id"],
            "cidr": item.get("cidr"),
            "repository": item.get("repository"),
            "node_id": item.get("node_id"),
            "node_name": item.get("node_name"),
            "region_id": item.get("region_id"),
            "region_name": item.get("region_name"),
            "status": item.get("status"),
            "best": item.get("best"),
            "backup": item.get("backup"),
            "converged_at": item.get("converged_at"),
            "last_change_at": item.get("last_change_at"),
            "observed_at": item.get("observed_at"),
            "event_seq": item.get("event_seq"),
            "snapshot_source": item.get("snapshot_source"),
        }
        for item in items
    ]


def make_handler(store: WorkbenchStore) -> type[BaseHTTPRequestHandler]:
    class WorkbenchHandler(BaseHTTPRequestHandler):
        server_version = "BuildRoutingWorkbench/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[http] {self.address_string()} - {format % args}")

        def _send_json(self, status: int, value: Any) -> None:
            payload = _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, value: str) -> None:
            payload = value.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, message: str) -> None:
            self._send_json(
                status,
                {"error": {"status": status, "message": message}},
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/":
                    self._send_html(INDEX_HTML)
                    return
                if path == "/api/health":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "inventory": store.integrity_check(),
                            "database": store.db_path,
                        },
                    )
                    return
                if path == "/api/meta":
                    self._send_json(HTTPStatus.OK, store.metadata())
                    return
                if path == "/api/events":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "events": store.events(),
                            "last_committed": store.last_committed_event(),
                        },
                    )
                    return
                if path == "/api/nodes":
                    self._send_json(HTTPStatus.OK, {"nodes": store.nodes()})
                    return
                if path == "/api/prefixes":
                    at = _parse_at(params)
                    node_id = params.get("node", [None])[0] or None
                    prefix_id = params.get("prefix", [None])[0] or None
                    snapshots = store.snapshots(
                        at=at, node_id=node_id, prefix_id=prefix_id
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "at": at,
                            "current": at is None,
                            "source": (
                                "sqlite.prefix_current"
                                if at is None
                                else "sqlite.prefix_snapshots"
                            ),
                            "recomputed": False,
                            "converged_at": max(
                                (
                                    item["converged_at"]
                                    for item in snapshots
                                ),
                                default=0,
                            ),
                            "prefixes": _summary_snapshot(snapshots),
                        },
                    )
                    return
                if path == "/api/snapshot":
                    at = _parse_at(params)
                    if at is None:
                        at = 0
                    node_id = params.get("node", [None])[0] or None
                    prefix_id = params.get("prefix", [None])[0] or None
                    self._send_json(
                        HTTPStatus.OK,
                        store.snapshot_at(
                            at, node_id=node_id, prefix_id=prefix_id
                        ),
                    )
                    return
                if path == "/api/stats":
                    at = _parse_at(params)
                    self._send_json(HTTPStatus.OK, store.stats_at(at))
                    return
                if path.startswith("/api/prefix/"):
                    prefix_id = unquote(path[len("/api/prefix/") :])
                    at = _parse_at(params)
                    self._send_json(
                        HTTPStatus.OK,
                        store.prefix_detail(prefix_id, at=at),
                    )
                    return
                if path.startswith("/api/history/"):
                    prefix_id = unquote(path[len("/api/history/") :])
                    at = _parse_at(params)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "prefix_id": prefix_id,
                            "at": at,
                            "source": "sqlite.switch_events",
                            "history": store.switch_history(prefix_id, at=at),
                        },
                    )
                    return
                self._error(HTTPStatus.NOT_FOUND, f"unknown route {path}")
            except KeyError as exc:
                self._error(HTTPStatus.NOT_FOUND, f"not found: {exc}")
            except (ValueError, TypeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    return WorkbenchHandler


def make_server(
    store: WorkbenchStore,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    class WorkbenchHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return WorkbenchHTTPServer((host, port), make_handler(store))


def run_server(
    *,
    db_path: str | Path,
    project_path: str | Path | None = None,
    events_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    reset: bool = False,
) -> int:
    store = WorkbenchStore(db_path)
    if project_path is not None:
        if events_path is None:
            raise ValueError("--events is required when --project is supplied")
        project = load_project(project_path)
        with Path(events_path).open("r", encoding="utf-8") as handle:
            events = json.load(handle)
        store.bootstrap(project, events, reset=reset)
    else:
        if not Path(db_path).exists():
            raise ValueError(
                "database does not exist; initialize with replay or provide "
                "--project and --events"
            )
        store.load_project()

    server = make_server(store, host, port)
    actual_host, actual_port = server.server_address[:2]
    print(f"Build routing workbench: http://{actual_host}:{actual_port}/")
    print(f"SQLite: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>构建节点出口路由诊断工作台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7f9;
      --panel: #ffffff;
      --ink: #17212b;
      --muted: #647383;
      --line: #d8e0e7;
      --accent: #0b6e69;
      --accent-2: #e6f4f2;
      --warn: #9a4d00;
      --danger: #a32828;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: linear-gradient(150deg, #eef4f6, var(--bg) 45%, #f8f6ef);
      color: var(--ink);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    header {
      padding: 28px clamp(18px, 4vw, 48px) 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,.78);
      backdrop-filter: blur(8px);
    }
    h1 { margin: 0 0 6px; font-size: clamp(22px, 3vw, 32px); letter-spacing: -.02em; }
    .sub { color: var(--muted); max-width: 900px; }
    main { padding: 22px clamp(18px, 4vw, 48px) 60px; }
    .toolbar {
      display: flex; flex-wrap: wrap; gap: 12px; align-items: end;
      background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
      padding: 15px; box-shadow: 0 8px 26px rgba(28,50,62,.05);
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    select, input, button {
      min-height: 38px; border-radius: 9px; border: 1px solid var(--line);
      background: white; color: var(--ink); padding: 7px 10px; font: inherit;
    }
    input { width: 150px; }
    button {
      cursor: pointer; border-color: var(--accent); background: var(--accent);
      color: white; font-weight: 650; padding-inline: 17px;
    }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 16px 0; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 13px; padding: 14px; }
    .card b { display: block; font-size: 23px; margin-top: 2px; }
    .eyebrow { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    .layout { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(310px, .9fr); gap: 16px; }
    @media (max-width: 980px) { .layout { grid-template-columns: 1fr; } }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; box-shadow: 0 8px 26px rgba(28,50,62,.04); }
    .panel h2 { margin: 0; padding: 14px 16px; font-size: 15px; border-bottom: 1px solid var(--line); }
    .scroll { overflow: auto; max-height: 62vh; }
    table { width: 100%; border-collapse: collapse; min-width: 780px; }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #edf1f4; vertical-align: top; }
    th { position: sticky; top: 0; background: #f8fafb; color: var(--muted); font-size: 12px; z-index: 1; }
    tbody tr { cursor: pointer; }
    tbody tr:hover { background: var(--accent-2); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #edf2f4; font-size: 12px; white-space: nowrap; }
    .pill.ready { background: #e3f4e8; color: #18633c; }
    .pill.all_filtered { background: #fff0dc; color: var(--warn); }
    .pill.load_error { background: #fde7e7; color: var(--danger); }
    .pill.single_candidate { background: #eef0fb; color: #424a90; }
    .detail { padding: 15px 16px; min-height: 250px; }
    .detail h3 { margin: 0 0 10px; }
    .kv { display: grid; grid-template-columns: 110px 1fr; gap: 6px 10px; margin-bottom: 14px; }
    .kv span:nth-child(odd) { color: var(--muted); }
    .reason { background: #f6f8f9; border-left: 3px solid var(--accent); padding: 9px 11px; margin: 10px 0; }
    .history { display: grid; gap: 8px; margin-top: 8px; }
    .event { border: 1px solid var(--line); border-radius: 9px; padding: 9px 10px; }
    .muted { color: var(--muted); }
    .error { color: var(--danger); background: #fde7e7; border: 1px solid #f3c4c4; padding: 10px 12px; border-radius: 9px; margin: 12px 0; }
    code { background: #edf2f4; padding: 1px 5px; border-radius: 4px; }
  </style>
</head>
<body>
<header>
  <h1>构建节点出口路由诊断工作台</h1>
  <div class="sub">按节点、前缀和逻辑时刻复盘最优/备选路径。历史快照直接读取 SQLite 持久化序列，不重新计算选路。</div>
</header>
<main>
  <div class="toolbar">
    <label>构建节点<select id="node"><option value="">全部节点</option></select></label>
    <label>前缀<select id="prefix"><option value="">全部前缀</option></select></label>
    <label>逻辑时刻 at<input id="at" inputmode="numeric" placeholder="当前"></label>
    <button id="load">读取快照</button>
    <button id="current" style="background:#fff;color:var(--accent)">回到当前</button>
  </div>
  <div id="error"></div>
  <div class="cards" id="cards"></div>
  <div class="layout">
    <section class="panel">
      <h2>前缀选路</h2>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>节点 / 区域</th><th>前缀</th><th>最优路径</th><th>备选路径</th><th>状态</th><th>收敛时刻</th></tr>
          </thead>
          <tbody id="rows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
      </div>
    </section>
    <aside class="panel">
      <h2>前缀详情与切换历史</h2>
      <div class="detail" id="detail"><span class="muted">点击左侧前缀查看择优依据与历史。</span></div>
    </aside>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let state = { nodes: [], prefixes: [], selected: null };

function esc(value) {
  return String(value ?? "—");
}
function hop(path) {
  if (!path) return '<span class="muted">无</span>';
  return `<span class="mono">${esc(path.next_hop_id)}</span><br><span class="muted mono">${esc((path.as_path || []).join(" → "))}</span>`;
}
function pill(status) {
  return `<span class="pill ${esc(status)}">${esc(status)}</span>`;
}
function showError(message) {
  $("error").innerHTML = message ? `<div class="error">${esc(message)}</div>` : "";
}
async function getJson(url) {
  const response = await fetch(url, {cache: "no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || response.statusText);
  return data;
}
function queryParams() {
  const params = new URLSearchParams();
  if ($("node").value) params.set("node", $("node").value);
  if ($("prefix").value) params.set("prefix", $("prefix").value);
  if ($("at").value.trim()) params.set("at", $("at").value.trim());
  return params;
}
async function loadNodes() {
  const data = await getJson("/api/nodes");
  state.nodes = data.nodes;
  const select = $("node");
  for (const node of data.nodes) {
    const option = document.createElement("option");
    option.value = node.node_id;
    option.textContent = `${node.node_name} · ${node.region_name}`;
    select.appendChild(option);
  }
}
async function loadRows() {
  showError("");
  const params = queryParams();
  const at = params.has("at") ? params.get("at") : null;
  const data = await getJson(`/api/prefixes?${params.toString()}`);
  state.prefixes = data.prefixes;
  if (at === null) $("at").placeholder = "当前";
  $("cards").innerHTML = `
    <div class="card"><span class="eyebrow">读取来源</span><b style="font-size:15px">${esc(data.source)}</b></div>
    <div class="card"><span class="eyebrow">视图</span><b style="font-size:19px">${at === null ? "当前状态" : "历史 at=" + esc(at)}</b></div>
    <div class="card"><span class="eyebrow">前缀数</span><b>${data.prefixes.length}</b></div>
    <div class="card"><span class="eyebrow">全局收敛时刻</span><b>${esc(data.converged_at)}</b></div>`;
  const body = $("rows");
  if (!data.prefixes.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">该逻辑时刻没有持久化快照。</td></tr>';
    return;
  }
  body.innerHTML = data.prefixes.map((item) => `
    <tr data-prefix="${esc(item.prefix_id)}">
      <td><b>${esc(item.node_name)}</b><br><span class="muted">${esc(item.region_name)}</span></td>
      <td><span class="mono">${esc(item.prefix_id)}</span><br><span class="muted mono">${esc(item.cidr)}</span></td>
      <td>${hop(item.best)}</td>
      <td>${hop(item.backup)}</td>
      <td>${pill(item.status)}</td>
      <td class="mono">${esc(item.converged_at)}</td>
    </tr>`).join("");
  body.querySelectorAll("tr[data-prefix]").forEach((row) => {
    row.addEventListener("click", () => loadDetail(row.dataset.prefix));
  });
  const selected = $("prefix").value;
  if (selected) loadDetail(selected);
}
async function loadDetail(prefixId) {
  state.selected = prefixId;
  const params = new URLSearchParams();
  if ($("at").value.trim()) params.set("at", $("at").value.trim());
  const item = await getJson(`/api/prefix/${encodeURIComponent(prefixId)}?${params.toString()}`);
  const basis = item.basis || {};
  const history = item.history || [];
  $("detail").innerHTML = `
    <h3 class="mono">${esc(item.prefix_id)}</h3>
    <div class="kv">
      <span>节点</span><span>${esc(item.node_name)}</span>
      <span>区域</span><span>${esc(item.region_name)}</span>
      <span>仓库</span><span>${esc(item.repository)}</span>
      <span>CIDR</span><span class="mono">${esc(item.cidr)}</span>
      <span>状态</span><span>${pill(item.status)}</span>
      <span>收敛时刻</span><span class="mono">${esc(item.converged_at)}</span>
      <span>快照来源</span><span class="mono">${esc(item.snapshot_source)}</span>
    </div>
    ${basis.best_reason ? `<div class="reason"><b>最优路径依据</b><br>${esc(basis.best_reason)}</div>` : ""}
    ${basis.backup_reason ? `<div class="reason"><b>备选路径依据</b><br>${esc(basis.backup_reason)}</div>` : ""}
    <h3 style="font-size:14px;margin-top:18px">切换历史 (${history.length})</h3>
    <div class="history">${history.length ? history.map((event) => `
      <div class="event">
        <div><b>at ${esc(event.at)}</b> · ${esc(event.event_type)} · <span class="mono">${esc(event.change_kind)}</span></div>
        <div class="muted">最优：<span class="mono">${esc(event.old_best_next_hop_id)}</span> → <span class="mono">${esc(event.new_best_next_hop_id)}</span></div>
        <div class="muted">备选：<span class="mono">${esc(event.old_backup_next_hop_id)}</span> → <span class="mono">${esc(event.new_backup_next_hop_id)}</span></div>
      </div>`).join("") : '<span class="muted">尚无切换。</span>'}</div>`;
}
async function loadAll() {
  try {
    await loadRows();
    history.replaceState(null, "", `?${queryParams().toString()}`);
  } catch (error) {
    showError(error.message);
  }
}
$("load").addEventListener("click", loadAll);
$("current").addEventListener("click", () => { $("at").value = ""; loadAll(); });
$("prefix").addEventListener("change", () => { if ($("prefix").value) loadDetail($("prefix").value); });
(async function init() {
  try {
    const initial = new URLSearchParams(location.search);
    if (initial.has("at")) $("at").value = initial.get("at");
    await loadNodes();
    const all = await getJson("/api/prefixes");
    const select = $("prefix");
    for (const item of all.prefixes) {
      const option = document.createElement("option");
      option.value = item.prefix_id;
      option.textContent = `${item.prefix_id} · ${item.cidr}`;
      select.appendChild(option);
    }
    if (initial.has("node")) $("node").value = initial.get("node");
    if (initial.has("prefix")) $("prefix").value = initial.get("prefix");
    await loadAll();
  } catch (error) {
    showError(error.message);
  }
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the local build-routing HTTP workbench")
    parser.add_argument("--db", default="artifacts/workbench.db")
    parser.add_argument("--project")
    parser.add_argument("--events")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        run_server(
            db_path=args.db,
            project_path=args.project,
            events_path=args.events,
            host=args.host,
            port=args.port,
            reset=args.reset,
        )
    )
