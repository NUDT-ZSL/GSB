from __future__ import annotations

import asyncio
import html
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

router = APIRouter(prefix="/archive", tags=["deterministic-archive"])

SESSION_COOKIE = "archive_session"


@dataclass
class SiteSession:
    id: str
    request_count: int = 0
    expired: bool = False


@dataclass
class SiteState:
    request_seq: int = 0
    session_seq: int = 0
    sessions: dict[str, SiteSession] = field(default_factory=dict)
    fault_counts: dict[str, int] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def begin_request(self, delay_profile: str) -> tuple[int, int]:
        async with self.lock:
            self.request_seq += 1
            seq = self.request_seq
        delay_ms = delay_for(seq, delay_profile)
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000.0)
        return seq, delay_ms

    async def count_fault(self, key: str) -> int:
        async with self.lock:
            self.fault_counts[key] = self.fault_counts.get(key, 0) + 1
            return self.fault_counts[key]

    async def new_session(self) -> SiteSession:
        async with self.lock:
            self.session_seq += 1
            session = SiteSession(id=f"s{self.session_seq}")
            self.sessions[session.id] = session
            return session


def delay_for(seq: int, profile: str) -> int:
    if profile == "none":
        return 0
    if profile == "slow":
        return 12 + (seq % 4) * 18
    if profile == "bursty":
        return [16, 72, 5, 44, 25][seq % 5]
    if profile == "spike":
        return 380 if seq % 5 == 0 else 5
    return 0


def item(
    business_id: str,
    revision: int,
    schema_version: str | None,
    kind: str,
    fields: dict[str, Any],
    *,
    variant: str | None = None,
) -> dict[str, Any]:
    return {
        "business_id": business_id,
        "revision": revision,
        "schema_version": schema_version,
        "kind": kind,
        "fields": fields,
        "variant": variant,
    }


BASE_ITEMS = [
    item(
        "AUD-2026-001",
        1,
        "1.0",
        "v1",
        {
            "title": "Q1 procurement audit",
            "owner": "Li Wei",
            "amount": "128000.00",
            "status": "draft",
            "notes": "baseline full record",
        },
    ),
    item(
        "AUD-2026-002",
        1,
        "2.0",
        "v2",
        {
            "title": "Vendor onboarding review",
            "owner": "Chen Yu",
            "amount": "56000",
            "status": "approved",
            "region": "east",
        },
    ),
    item(
        "AUD-2026-003",
        1,
        "2.0",
        "v2",
        {
            "title": "Travel reimbursement sample",
            "owner": "Zhao Min",
            "amount": "9800",
            "status": "review",
            "category": "travel",
        },
    ),
    item(
        "AUD-2026-004",
        1,
        "1.0",
        "v1",
        {
            "title": "Inventory count audit",
            "owner": "Sun Qi",
            "amount": "43000",
            "status": "closed",
            "audit_date": "2026-01-15",
        },
    ),
    item(
        "AUD-2026-005",
        1,
        "9.9",
        "unknown",
        {
            "legacy_code": "LG-77",
            "legacy_owner": "Zhou Lan",
            "legacy_note": "schema not recognised; keep every raw field",
        },
    ),
    item(
        "AUD-2026-006",
        1,
        None,
        "missing",
        {
            "unlabelled_title": "Archive with no schema declaration",
            "unlabelled_owner": "He Ning",
            "unlabelled_amount": "7000",
        },
    ),
]

INCREMENTAL_ITEMS = [
    item(
        "AUD-2026-001",
        2,
        "1.0",
        "v1",
        {"status": "pending-review", "riskLevel": "high"},
    ),
    item(
        "AUD-2026-002",
        2,
        "2.0",
        "v2",
        {"status": "approved-final", "reviewer": "Wang Bo"},
    ),
    item(
        "AUD-2026-003",
        2,
        "2.0",
        "v2",
        {"amount": "10200", "status": "escalated"},
    ),
    item(
        "AUD-2026-007",
        1,
        "1.0",
        "v1",
        {
            "title": "New vendor audit",
            "owner": "Qian Rui",
            "amount": "23000",
            "status": "open",
        },
    ),
]

CONFLICT_ITEMS = [
    item(
        "AUD-2026-101",
        1,
        "1.0",
        "v1",
        {"title": "Conflict sample", "owner": "Lin Tao", "amount": "100.00", "status": "draft"},
        variant="a",
    ),
    item(
        "AUD-2026-101",
        1,
        "1.0",
        "v1",
        {"title": "Conflict sample", "owner": "Lin Tao", "amount": "100.00", "status": "approved"},
        variant="b",
    ),
]

WAVES: dict[str, list[list[dict[str, Any]]]] = {
    "baseline": [BASE_ITEMS[0:2], BASE_ITEMS[2:4], BASE_ITEMS[4:6]],
    "incremental": [INCREMENTAL_ITEMS[0:2], INCREMENTAL_ITEMS[2:4]],
    "conflict": [CONFLICT_ITEMS],
}


def _state(request: Request) -> SiteState:
    return request.app.state.archive_site


def _query_common(request: Request) -> dict[str, str]:
    return {
        "wave": request.query_params.get("wave", "baseline"),
        "delay_profile": request.query_params.get("delay_profile", "none"),
        "fault_profile": request.query_params.get("fault_profile", "none"),
    }


def _with_query(path: str, params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return f"{path}?{urlencode(clean)}"


def _item_url(request: Request, entry: dict[str, Any]) -> str:
    common = _query_common(request)
    return _with_query(
        "/archive/detail",
        {
            "id": entry["business_id"],
            "wave": common["wave"],
            "variant": entry.get("variant"),
            "delay_profile": common["delay_profile"],
            "fault_profile": common["fault_profile"],
        },
    )


def _find_entry(wave: str, business_id: str, variant: str | None) -> dict[str, Any] | None:
    pages = WAVES.get(wave)
    if pages is None:
        return None
    for page in pages:
        for entry in page:
            if entry["business_id"] == business_id and (entry.get("variant") or None) == (variant or None):
                return entry
    return None


def _response(
    content: str,
    *,
    status_code: int = 200,
    media_type: str = "text/html; charset=utf-8",
    seq: int = 0,
    delay_ms: int = 0,
    session: SiteSession | None = None,
) -> Response:
    headers = {
        "X-Archive-Seq": str(seq),
        "X-Interaction-Delay-Ms": str(delay_ms),
        "Cache-Control": "no-store",
    }
    if session is not None:
        headers["X-Archive-Session"] = session.id
    return Response(
        content=content,
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


def _expired_response(seq: int, delay_ms: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": "E_SESSION_EXPIRED", "message": "archive session expired"},
        status_code=401,
        headers={
            "X-Archive-Seq": str(seq),
            "X-Interaction-Delay-Ms": str(delay_ms),
            "Cache-Control": "no-store",
        },
    )


def _authenticate(
    request: Request, profile: str, seq: int, delay_ms: int
) -> tuple[SiteSession | None, Response | None]:
    state = _state(request)
    token = request.cookies.get(SESSION_COOKIE)
    session = state.sessions.get(token or "")
    if session is None or session.expired:
        return None, _expired_response(seq, delay_ms)
    session.request_count += 1
    if profile in {"session_expiry", "mixed"} and session.request_count == 6:
        session.expired = True
        return None, _expired_response(seq, delay_ms)
    return session, None


async def _maybe_503(
    request: Request, profile: str, seq: int, delay_ms: int, fault_key: str
) -> Response | None:
    state = _state(request)
    if profile == "always_503" and "detail" in fault_key:
        return _upstream_503(seq, delay_ms)
    if profile == "mixed":
        count = await state.count_fault(fault_key)
        if count == 1 and (
            "/detail" in fault_key or "list-fragment" in fault_key
        ):
            return _upstream_503(seq, delay_ms)
    return None


def _upstream_503(seq: int, delay_ms: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": "E_UPSTREAM_503", "message": "injected upstream failure"},
        status_code=503,
        headers={
            "X-Archive-Seq": str(seq),
            "X-Interaction-Delay-Ms": str(delay_ms),
        },
    )


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {
        "ok": True,
        "request_seq": state.request_seq,
        "sessions": len(state.sessions),
        "waves": sorted(WAVES),
    }


@router.get("/session")
async def new_session(request: Request) -> Response:
    common = _query_common(request)
    seq, delay_ms = await _state(request).begin_request(common["delay_profile"])
    session = await _state(request).new_session()
    payload = {
        "ok": True,
        "session_id": session.id,
        "wave": common["wave"],
        "request_seq": seq,
        "interaction_delay_ms": delay_ms,
    }
    response = JSONResponse(
        payload,
        headers={
            "X-Archive-Seq": str(seq),
            "X-Interaction-Delay-Ms": str(delay_ms),
            "X-Archive-Session": session.id,
            "Cache-Control": "no-store",
        },
    )
    response.set_cookie(
        SESSION_COOKIE,
        session.id,
        httponly=True,
        samesite="lax",
        path="/archive",
    )
    return response


@router.get("/list", response_class=HTMLResponse)
async def list_page(request: Request) -> Response:
    common = _query_common(request)
    seq, delay_ms = await _state(request).begin_request(common["delay_profile"])
    session, error = _authenticate(request, common["fault_profile"], seq, delay_ms)
    if error:
        return error
    page = int(request.query_params.get("page", "1"))
    fragment_url = _with_query(
        "/archive/list-fragment",
        {
            "page": page,
            "wave": common["wave"],
            "delay_profile": common["delay_profile"],
            "fault_profile": common["fault_profile"],
        },
    )
    session_url = _with_query(
        "/archive/session",
        {
            "wave": common["wave"],
            "delay_profile": common["delay_profile"],
            "fault_profile": common["fault_profile"],
        },
    )
    contract = {
        "render_api": fragment_url,
        "session_api": session_url,
        "container": "#entry-container",
        "item_selector": "[data-entry]",
        "stable_observations": 2,
        "declaration": "container-present-and-count-stable-twice",
    }
    body = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Audit archive list {page}</title></head>
<body>
<main>
  <h1>Internal audit archive</h1>
  <p id="render-status">loading secondary render...</p>
  <div id="archive-app" data-render-api="{html.escape(fragment_url, quote=True)}"></div>
  <script type="application/json" id="render-contract">{json.dumps(contract, ensure_ascii=False)}</script>
  <script>
  (async () => {{
    const contract = JSON.parse(document.getElementById('render-contract').textContent);
    const app = document.getElementById('archive-app');
    let previousCount = -1;
    let stableCount = 0;
    while (true) {{
      const response = await fetch(contract.render_api, {{credentials: 'same-origin'}});
      if (response.status === 401) {{
        await fetch(contract.session_api, {{credentials: 'same-origin'}});
        continue;
      }}
      if (!response.ok) {{
        await new Promise(requestAnimationFrame);
        continue;
      }}
      const fragment = await response.text();
      const template = document.createElement('template');
      template.innerHTML = fragment;
      const container = template.content.querySelector(contract.container);
      const count = container ? container.querySelectorAll(contract.item_selector).length : 0;
      if (container && count === previousCount) stableCount += 1;
      else stableCount = container ? 1 : 0;
      previousCount = count;
      if (container && stableCount >= contract.stable_observations) {{
        app.replaceChildren(container);
        document.getElementById('render-status').textContent = `rendered ${{count}} entries`;
        break;
      }}
      await new Promise(requestAnimationFrame);
    }}
  }})().catch(error => {{
    document.getElementById('render-status').textContent = String(error);
  }});
  </script>
</main>
</body>
</html>"""
    return _response(body, seq=seq, delay_ms=delay_ms, session=session)


def _render_entry(request: Request, entry: dict[str, Any]) -> str:
    schema = entry["schema_version"] or ""
    return f"""<article class="entry" data-entry data-business-id="{html.escape(entry['business_id'])}"
  data-revision="{entry['revision']}" data-schema-version="{html.escape(schema)}"
  data-detail-url="{html.escape(_item_url(request, entry), quote=True)}">
  <a href="{html.escape(_item_url(request, entry), quote=True)}">{html.escape(entry['business_id'])} · r{entry['revision']}</a>
</article>"""


@router.get("/list-fragment", response_class=HTMLResponse)
async def list_fragment(request: Request) -> Response:
    common = _query_common(request)
    state = _state(request)
    seq, delay_ms = await state.begin_request(common["delay_profile"])
    session, error = _authenticate(request, common["fault_profile"], seq, delay_ms)
    if error:
        return error
    page = int(request.query_params.get("page", "1"))
    fault = await _maybe_503(
        request, common["fault_profile"], seq, delay_ms, f"list-fragment:{common['wave']}:{page}"
    )
    if fault:
        return fault
    pages = WAVES.get(common["wave"])
    if pages is None or page < 1 or page > len(pages):
        return _response("page not found", status_code=404, seq=seq, delay_ms=delay_ms, session=session)
    if common["fault_profile"] == "render_timeout":
        body = '<div id="archive-loading">secondary render has not produced an entry container</div>'
        return _response(body, seq=seq, delay_ms=delay_ms, session=session)
    total_pages = len(pages)
    next_page = page + 1 if page < total_pages else None
    if common["fault_profile"] == "bad_cursor" and page == 1:
        next_page = page + 2
    entries = "\n".join(_render_entry(request, entry) for entry in pages[page - 1])
    next_attr = "" if next_page is None else str(next_page)
    body = f"""<div id="entry-container" data-page="{page}" data-total-pages="{total_pages}"
  data-next-page="{next_attr}" data-render-state="ready">
{entries}
</div>"""
    return _response(body, seq=seq, delay_ms=delay_ms, session=session)


def _v1_detail(entry: dict[str, Any]) -> str:
    fields = "\n".join(
        f'<div data-field="{html.escape(str(key), quote=True)}">{html.escape(str(value))}</div>'
        for key, value in entry["fields"].items()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="schemaVersion" content="1.0">
<meta name="auditRevision" content="{entry['revision']}">
<meta name="businessId" content="{html.escape(entry['business_id'], quote=True)}">
<title>{html.escape(entry['business_id'])}</title></head>
<body data-business-id="{html.escape(entry['business_id'], quote=True)}" data-revision="{entry['revision']}">
<main id="audit-detail"><h1>{html.escape(entry['business_id'])}</h1>{fields}</main>
</body></html>"""


def _v2_detail(entry: dict[str, Any]) -> str:
    payload = {
        "businessId": entry["business_id"],
        "schemaVersion": "2.0",
        "revision": entry["revision"],
        "record": {"attributes": entry["fields"]},
    }
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="schemaVersion" content="2.0">
<meta name="auditRevision" content="{entry['revision']}">
<meta name="businessId" content="{html.escape(entry['business_id'], quote=True)}">
<title>{html.escape(entry['business_id'])}</title></head>
<body data-business-id="{html.escape(entry['business_id'], quote=True)}">
<script id="audit-detail" type="application/json">{json.dumps(payload, ensure_ascii=False)}</script>
<main id="audit-detail-v2"><h1>{html.escape(entry['business_id'])}</h1></main>
</body></html>"""


def _raw_detail(entry: dict[str, Any]) -> str:
    declared = entry.get("schema_version")
    meta = (
        f'<meta name="schemaVersion" content="{html.escape(declared, quote=True)}">'
        if declared
        else ""
    )
    fields = "\n".join(
        f'<span data-key="{html.escape(str(key), quote=True)}" data-value="{html.escape(str(value), quote=True)}">'
        f"{html.escape(str(value))}</span>"
        for key, value in entry["fields"].items()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">{meta}
<meta name="auditRevision" content="{entry['revision']}">
<meta name="businessId" content="{html.escape(entry['business_id'], quote=True)}">
<title>{html.escape(entry['business_id'])}</title></head>
<body data-business-id="{html.escape(entry['business_id'], quote=True)}">
<main>{fields}</main></body></html>"""


@router.get("/detail", response_class=HTMLResponse)
async def detail(request: Request) -> Response:
    common = _query_common(request)
    seq, delay_ms = await _state(request).begin_request(common["delay_profile"])
    session, error = _authenticate(request, common["fault_profile"], seq, delay_ms)
    if error:
        return error
    business_id = request.query_params.get("id", "")
    variant = request.query_params.get("variant") or None
    fault = await _maybe_503(
        request,
        common["fault_profile"],
        seq,
        delay_ms,
        f"detail:{common['wave']}:{business_id}:{variant or '-'}",
    )
    if fault:
        return fault
    entry = _find_entry(common["wave"], business_id, variant)
    if entry is None:
        return _response("detail not found", status_code=404, seq=seq, delay_ms=delay_ms, session=session)
    if entry["kind"] == "v1":
        body = _v1_detail(entry)
    elif entry["kind"] == "v2":
        body = _v2_detail(entry)
    else:
        body = _raw_detail(entry)
    return _response(body, seq=seq, delay_ms=delay_ms, session=session)


@router.post("/debug/reset")
async def reset_debug(request: Request) -> dict[str, Any]:
    request.app.state.archive_site = SiteState()
    return {"ok": True}
