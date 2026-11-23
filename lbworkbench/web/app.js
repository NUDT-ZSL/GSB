const state = {
  status: null,
};

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showError(error) {
  const banner = el("error-banner");
  banner.textContent = typeof error === "string" ? error : JSON.stringify(error, null, 2);
  banner.classList.remove("hidden");
}

function clearError() {
  el("error-banner").classList.add("hidden");
  el("error-banner").textContent = "";
}

function sourceTags(instance) {
  const versions = instance.source_versions || {};
  return Object.keys(versions)
    .sort()
    .map((source) => `<span class="tag ${source}">${source}</span>`)
    .join("");
}

function detailRow(instance) {
  const row = document.createElement("tr");
  row.className = "detail-row";
  row.hidden = true;
  const cell = document.createElement("td");
  cell.colSpan = 7;
  const template = el("detail-template").content.cloneNode(true);
  template.querySelector('[data-field="decision"]').textContent = JSON.stringify(
    instance.decision,
    null,
    2
  );
  template.querySelector('[data-field="sources"]').textContent = JSON.stringify(
    instance.sources,
    null,
    2
  );
  template.querySelector('[data-field="drift"]').textContent = instance.drift
    ? JSON.stringify(instance.drift, null, 2)
    : "无漂移证据";
  cell.appendChild(template);
  row.appendChild(cell);
  return row;
}

function render(status) {
  state.status = status;
  const snapshot = status.current_snapshot;
  el("snapshot-version").textContent = snapshot ? snapshot.version : "未载入";
  el("clock-waterline").textContent = status.clock_waterline_ms ?? 0;
  el("round-count").textContent = status.committed_rounds?.count ?? 0;
  const driftCount = (status.instances || []).filter((item) => item.drift).length;
  el("drift-count").textContent = driftCount;
  el("snapshot-meta").textContent = snapshot
    ? `package ${snapshot.version} · load=${snapshot.load_kind} · clocks ${snapshot.v1_snapshot_id} / ${snapshot.v2_snapshot_id}`
    : "等待通过 load 载入两代快照";

  const body = el("instance-body");
  body.replaceChildren();
  if (!status.instances || status.instances.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "empty";
    cell.textContent = "加载快照后显示实例状态。";
    row.appendChild(cell);
    body.appendChild(row);
  } else {
    for (const instance of status.instances) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><div class="instance-id">${escapeHtml(instance.instance_id)}</div></td>
        <td>${sourceTags(instance)}<div class="muted">${instance.selected_source || "manual"}</div></td>
        <td><span class="status ${instance.health_status}">${instance.health_status}</span></td>
        <td class="mono">${instance.consecutive_failures} / ${instance.consecutive_successes}</td>
        <td class="mono">${instance.effective_weight}</td>
        <td>${instance.drift ? `<span class="drift">${escapeHtml(instance.drift.cause)}</span>` : '<span class="muted">—</span>'}</td>
        <td><details><summary>查看</summary></details></td>
      `;
      const details = row.querySelector("details");
      const detail = detailRow(instance);
      details.addEventListener("toggle", () => {
        detail.hidden = !details.open;
      });
      body.appendChild(row);
      body.appendChild(detail);
    }
  }

  const probeBody = el("probe-error-body");
  probeBody.replaceChildren();
  if (!status.probe_errors || status.probe_errors.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty";
    cell.textContent = "当前没有已提交的探测错误。";
    row.appendChild(cell);
    probeBody.appendChild(row);
  } else {
    for (const error of status.probe_errors) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td class="mono">#${error.round_index}</td>
        <td class="instance-id">${escapeHtml(error.instance_id)}</td>
        <td><span class="drift">${escapeHtml(error.error_code)}</span></td>
        <td class="mono">${escapeHtml(error.target)} / ${error.request_budget_ms} ms</td>
        <td class="mono">${error.http_status ?? "—"}</td>
        <td>${escapeHtml(error.detail)}</td>
      `;
      probeBody.appendChild(row);
    }
  }
}

async function loadStatus() {
  clearError();
  const response = await fetch("/api/status", { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`status HTTP ${response.status}`);
  }
  render(await response.json());
}

async function verify() {
  clearError();
  const pill = el("verify-result");
  pill.className = "verify-pill idle";
  pill.textContent = "校验中…";
  try {
    const response = await fetch("/api/verify", { headers: { Accept: "application/json" } });
    const result = await response.json();
    pill.className = `verify-pill ${result.ok ? "ok" : "bad"}`;
    pill.textContent = result.ok
      ? "verify 通过"
      : `发现 ${result.differences?.length || 0} 项差异`;
    if (!result.ok) {
      showError(result);
    }
  } catch (error) {
    pill.className = "verify-pill bad";
    pill.textContent = "verify 失败";
    showError(error);
  }
}

el("refresh").addEventListener("click", () => {
  loadStatus().catch(showError);
});
el("verify").addEventListener("click", verify);

loadStatus().catch(showError);
setInterval(() => loadStatus().catch(() => {}), 15000);
