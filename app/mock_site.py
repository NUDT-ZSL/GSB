"""本地模拟工单系统：静态 HTML + JSON 接口，含分页、延迟渲染、503 与会话过期开关。

数据为内置确定性样本；所有异常注入均通过 /__config 配置完成，无需修改源码。
"""
from __future__ import annotations

import argparse
import json
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

PAGE_SIZE = 5
TOTAL_TICKETS = 24


def build_dataset(n: int = TOTAL_TICKETS) -> list[dict]:
    tickets = []
    for i in range(n):
        tid = f"TK-{1001 + i}"
        tickets.append(
            {
                "ticket_id": tid,
                "version": ["v1", "v2", "v9"][i % 3],
                "title": f"工单 {tid} 网络巡检",
                "priority": ["P1", "P2", "P3"][i % 3],
                "created_at": f"2026-11-{(i % 28) + 1:02d}T09:00:00",
                "requester": f"user{(i % 5) + 1}",
                "assignee": f"ops{(i % 4) + 1}",
                "department": ["net", "sys", "app"][i % 3],
                "category": ["incident", "change", "request"][i % 3],
                "due_date": f"2026-12-{(i % 28) + 1:02d}",
                "description": f"确定性样本描述 {tid}",
            }
        )
    return tickets


TICKETS = build_dataset()
TICKET_BY_ID = {t["ticket_id"]: t for t in TICKETS}

DEFAULT_TOGGLES = {
    "render_delay_ms": 250,
    "flaky_503": {},          # {"detail:TK-1003": 2, "list:p2": 1} -> 前 N 次返回 503
    "session_ttl_requests": 0,  # 0 表示永不过期；>0 表示会话有效请求数
    "session_fail_always": False,  # True 时新会话立即过期，用于触发 E_SESSION
}


def _detail_html(t: dict) -> str:
    if t["version"] == "v1":
        body = (
            '<dl class="schema-v1">'
            f"<dt>ticket_id</dt><dd>{t['ticket_id']}</dd>"
            f"<dt>title</dt><dd>{t['title']}</dd>"
            f"<dt>requester</dt><dd>{t['requester']}</dd>"
            f"<dt>priority</dt><dd>{t['priority']}</dd>"
            f"<dt>created_at</dt><dd>{t['created_at']}</dd>"
            f"<dt>description</dt><dd>{t['description']}</dd>"
            "</dl>"
        )
    elif t["version"] == "v2":
        fields = ["ticket_id", "title", "assignee", "department", "category",
                  "priority", "created_at", "due_date", "description"]
        body = '<div class="schema-v2">' + "".join(
            f'<span data-field="{f}">{t[f]}</span>' for f in fields
        ) + "</div>"
    else:  # v9：未知结构，采集端应降级为原始字段保留
        body = (
            '<article class="schema-v9">'
            f'<section data-k="ticket_id">{t["ticket_id"]}</section>'
            f'<section data-k="title">{t["title"]}</section>'
            f'<section data-k="priority">{t["priority"]}</section>'
            f'<section data-k="created_at">{t["created_at"]}</section>'
            f'<section data-k="description">{t["description"]}</section>'
            "</article>"
        )
    return (
        f'<html><head><meta name="schema-version" content="{t["version"]}">'
        f"<title>{t['ticket_id']}</title></head>"
        f"<body><h1>{t['ticket_id']}</h1>{body}</body></html>"
    )


def _list_html(page: int, size: int, delay_ms: int) -> str:
    total = len(TICKETS)
    pages = (total + size - 1) // size
    meta = {"page": page, "pages": pages, "total": total, "size": size}
    return f"""<html><head><title>工单列表</title></head><body>
<h1>工单列表 第 {page} 页</h1>
<div id="ticket-list" data-render-done="false"></div>
<span id="render-count">0</span>
<script id="page-meta" type="application/json">{json.dumps(meta)}</script>
<script>
async function renderList() {{
  const container = document.getElementById('ticket-list');
  try {{
    const r = await fetch('/api/tickets?page={page}&size={size}');
    if (!r.ok) {{ throw new Error('http ' + r.status); }}
    const data = await r.json();
    for (const t of data.items) {{
      const d = document.createElement('div');
      d.className = 'ticket-item';
      d.dataset.ticketId = t.ticket_id;
      d.dataset.version = t.version;
      d.textContent = t.ticket_id + ' ' + t.title;
      container.appendChild(d);
      await new Promise(res => setTimeout(res, 20));
    }}
    document.getElementById('render-count').textContent = String(data.items.length);
    container.dataset.renderDone = 'true';
  }} catch (e) {{
    container.dataset.renderDone = 'error';
  }}
}}
setTimeout(renderList, {delay_ms});
</script>
</body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="mock-ticket-site")
    toggles = dict(DEFAULT_TOGGLES)
    flaky: dict[str, int] = {}
    sessions: dict[str, int] = {}

    def _check_flaky(key: str) -> bool:
        if flaky.get(key, 0) > 0:
            flaky[key] -= 1
            return True
        return False

    def _check_session(request: Request) -> JSONResponse | None:
        sid = request.cookies.get("sid")
        if not sid or sid not in sessions or sessions[sid] <= 0:
            return JSONResponse({"code": "E_SESSION", "message": "session expired"}, status_code=401)
        sessions[sid] -= 1
        return None

    @app.get("/session")
    async def get_session():
        sid = uuid.uuid4().hex
        ttl = 0 if toggles["session_fail_always"] else toggles["session_ttl_requests"]
        sessions[sid] = ttl if ttl > 0 else (0 if toggles["session_fail_always"] else 1 << 30)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("sid", sid)
        return resp

    @app.get("/api/tickets")
    async def api_tickets(request: Request, page: int = 1, size: int = PAGE_SIZE):
        err = _check_session(request)
        if err is not None:
            return err
        if _check_flaky(f"list:p{page}"):
            return JSONResponse({"code": "E_HTTP_503", "message": "injected 503"}, status_code=503)
        start = (page - 1) * size
        items = TICKETS[start:start + size]
        return {"items": items, "page": page,
                "pages": (len(TICKETS) + size - 1) // size, "total": len(TICKETS)}

    @app.get("/api/tickets/{tid}")
    async def api_ticket(request: Request, tid: str):
        err = _check_session(request)
        if err is not None:
            return err
        if _check_flaky(f"detail:{tid}"):
            return JSONResponse({"code": "E_HTTP_503", "message": "injected 503"}, status_code=503)
        t = TICKET_BY_ID.get(tid)
        if t is None:
            return JSONResponse({"code": "E_NOT_FOUND"}, status_code=404)
        return t

    @app.get("/tickets", response_class=HTMLResponse)
    async def list_page(page: int = 1, size: int = PAGE_SIZE):
        return _list_html(page, size, toggles["render_delay_ms"])

    @app.get("/tickets/{tid}", response_class=HTMLResponse)
    async def detail_page(request: Request, tid: str):
        err = _check_session(request)
        if err is not None:
            return err
        if _check_flaky(f"detail:{tid}"):
            return HTMLResponse("injected 503", status_code=503)
        t = TICKET_BY_ID.get(tid)
        if t is None:
            return HTMLResponse("not found", status_code=404)
        return _detail_html(t)

    @app.get("/__config")
    async def get_config():
        return {"toggles": toggles, "flaky_remaining": dict(flaky)}

    @app.post("/__config")
    async def set_config(request: Request):
        body = await request.json()
        toggles.update(body.get("toggles", {}))
        flaky.clear()
        flaky.update(body.get("flaky_503", toggles.get("flaky_503", {})))
        sessions.clear()
        return {"ok": True, "toggles": toggles}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
