const $ = (selector) => document.querySelector(selector);
let latest = null;
let toastTimer = null;

const fmt = (value) => new Intl.NumberFormat("zh-CN").format(value ?? 0);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[ch]));

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const err = payload.error || { message: "请求失败" };
    throw new Error(`${err.code || "ERROR"}: ${err.message || ""}`);
  }
  return payload;
}

function bar(value, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, (value / total) * 100));
}

function renderTenants(status) {
  $("#metric-tenants").textContent = status.tenants.length;
  $("#scale-label").textContent = `scale ${status.fixed_scale}`;
  $("#tenants").innerHTML = status.tenants.map((tenant) => {
    const baseTotal = tenant.quota.millicores;
    const baseUsed = tenant.base_used.millicores;
    const borrowTotal = tenant.borrow_limit.millicores;
    const borrowUsed = tenant.borrowed.millicores;
    const flags = [
      tenant.allow_overdraft ? '<span class="flag">OVERDRAFT ON</span>' : '<span class="flag">NO OVERDRAFT</span>',
      tenant.precision_sensitive ? '<span class="flag warn">PRECISION SENSITIVE</span>' : "",
      tenant.waiting ? `<span class="flag bad">${tenant.waiting} WAITING</span>` : "",
    ].join("");
    return `
      <article class="tenant-card">
        <div class="tenant-head">
          <div>
            <div class="tenant-name">${esc(tenant.name)}</div>
            <div class="tenant-meta">${esc(tenant.resource_pool)} · used ${fmt(tenant.used.millicores)} mc / ${fmt(tenant.used.mib)} MiB</div>
          </div>
          <span class="version-chip">v${tenant.declaration_version}</span>
        </div>
        <div class="bar-row">
          <div class="bar-label"><span>基础配额</span><span>${fmt(baseUsed)} / ${fmt(baseTotal)} mc</span></div>
          <div class="bar"><i style="width:${bar(baseUsed, baseTotal)}%"></i></div>
        </div>
        <div class="bar-row">
          <div class="bar-label"><span>借调余额</span><span>${fmt(borrowUsed)} / ${fmt(borrowTotal)} mc</span></div>
          <div class="bar borrow"><i style="width:${bar(borrowUsed, borrowTotal)}%"></i></div>
        </div>
        <div class="tenant-stats">
          <div class="stat"><span>剩余基础</span><strong>${fmt(tenant.remaining.millicores)} mc</strong></div>
          <div class="stat"><span>剩余借调</span><strong>${fmt(tenant.borrow_remaining.millicores)} mc</strong></div>
          <div class="stat"><span>MI 基础余量</span><strong>${fmt(tenant.remaining.mib)} MiB</strong></div>
          <div class="stat"><span>累计误差上界</span><strong>${fmt(tenant.cumulative_error_micro)} μ</strong></div>
        </div>
        <div class="flags">${flags}</div>
      </article>`;
  }).join("") || '<div class="empty">尚无租户声明</div>';
}

function renderPools(status) {
  $("#pools").innerHTML = status.resource_pools.map((pool) => `
    <div class="pool">
      <div class="pool-title"><span>${esc(pool.name)}</span><span class="mono">${fmt(pool.borrowed.millicores)} mc borrowed</span></div>
      <div class="pool-grid">
        <span>容量 ${fmt(pool.capacity.millicores)} mc</span>
        <span>容量 ${fmt(pool.capacity.mib)} MiB</span>
        <span>可借 ${fmt(pool.available_to_lend.millicores)} mc</span>
        <span>可借 ${fmt(pool.available_to_lend.mib)} MiB</span>
      </div>
    </div>`).join("") || '<div class="empty">尚无资源池</div>';
}

function renderQueue(status) {
  $("#metric-queue").textContent = status.wait_queue.length;
  $("#queue-count").textContent = `${status.wait_queue.length} waiting`;
  $("#queue-body").innerHTML = status.wait_queue.map((row) => `
    <tr>
      <td>${esc(row.tenant)}</td>
      <td class="mono">#${row.sequence} · ${esc(row.application_id)}</td>
      <td class="mono">${fmt(row.requested_at_ms)} ms</td>
      <td><span class="status WAITING">${esc(row.reason_code)}</span> <span class="mono">${esc(row.reason_text)}</span></td>
      <td class="mono">${esc(row.last_release_id || "—")}</td>
    </tr>`).join("") || '<tr><td colspan="5" class="empty">等待队列为空</td></tr>';
}

function renderApps(status) {
  const active = status.applications.filter((app) => app.status === "ADMITTED").length;
  $("#metric-active").textContent = active;
  $("#metric-borrow").textContent = status.tenants.reduce((sum, tenant) => sum + tenant.borrowed.millicores, 0);
  $("#app-count").textContent = `${status.applications.length} applications`;
  $("#apps-body").innerHTML = status.applications.map((app) => `
    <tr>
      <td class="mono">${app.sequence}</td>
      <td class="app-id">${esc(app.application_id)}</td>
      <td>${esc(app.tenant)}<br><span class="mono">${esc(app.service)}</span></td>
      <td class="mono">${fmt(app.request_mc)} mc · ${fmt(app.request_mib)} MiB</td>
      <td class="mono">${fmt(app.requested_at_ms)} → ${fmt(app.expires_at_ms)} ms</td>
      <td><span class="status ${esc(app.status)}">${esc(app.status)}</span></td>
      <td class="mono">${app.admitted_version ? `v${app.admitted_version}` : "—"}</td>
      <td class="mono">${esc(app.reason_code || "—")}</td>
    </tr>`).join("") || '<tr><td colspan="8" class="empty">尚无申请轨迹</td></tr>';

  const staged = status.applications.filter((app) => app.status === "STAGED");
  const activeApps = status.applications.filter((app) => app.status === "ADMITTED");
  const submitSelect = $("#submit-app");
  const previousSubmit = submitSelect.value;
  submitSelect.innerHTML = staged.map((app) =>
    `<option value="${esc(app.application_id)}">#${app.sequence} · ${esc(app.application_id)} · ${esc(app.tenant)} · ${app.request_mc}mc/${app.request_mib}MiB</option>`
  ).join("") || '<option value="">没有暂存申请</option>';
  if (staged.some((app) => app.application_id === previousSubmit)) submitSelect.value = previousSubmit;

  const releaseSelect = $("#release-app");
  const previousRelease = releaseSelect.value;
  releaseSelect.innerHTML = activeApps.map((app) =>
    `<option value="${esc(app.application_id)}">#${app.sequence} · ${esc(app.application_id)} · ${esc(app.tenant)}</option>`
  ).join("") || '<option value="">没有活动预留</option>';
  if (activeApps.some((app) => app.application_id === previousRelease)) releaseSelect.value = previousRelease;
}

function renderPrecision(status) {
  $("#precision").innerHTML = status.precision_events.map((event) => {
    const chain = event.chain || {};
    const steps = (chain.steps || []).slice(0, 5).map((step) =>
      `${step.field}:${step.raw_numerator}/${step.raw_denominator}→${step.converted}@${step.scale}`
    ).join(" · ");
    return `
      <div class="precision-item">
        <div class="precision-head">
          <div class="precision-title">${esc(event.tenant)} / ${esc(event.application_id)} · ${esc(event.decision)}</div>
          <span class="status ${event.sensitive ? "WAITING" : "RELEASED"}">${event.sensitive ? "SENSITIVE" : "EXACT"}</span>
        </div>
        <div class="precision-chain">gap=${fmt(event.gap_micro)}μ / tol=${fmt(event.tolerance_micro)}μ · ${esc(steps || "无换算步骤")}</div>
      </div>`;
  }).join("") || '<div class="empty">尚无定点换算记录</div>';
}

function render(status) {
  latest = status;
  $("#clock").textContent = fmt(status.logical_clock_ms);
  $("#watermark").textContent = `提交水位 ${status.submitted_watermark}`;
  renderTenants(status);
  renderPools(status);
  renderQueue(status);
  renderApps(status);
  renderPrecision(status);
  if (!$("#resume-now").dataset.touched) $("#resume-now").value = status.logical_clock_ms;
  if (!$("#submit-now").dataset.touched) $("#submit-now").value = status.logical_clock_ms;
  if (!$("#release-now").dataset.touched) $("#release-now").value = status.logical_clock_ms;
}

async function refresh() {
  const status = await api("/api/status");
  render(status);
}

function withBusy(button, action) {
  button.disabled = true;
  action()
    .then(() => refresh())
    .catch((err) => toast(err.message, true))
    .finally(() => { button.disabled = false; });
}

$("#verify-btn").addEventListener("click", () => withBusy($("#verify-btn"), async () => {
  const result = await api("/api/verify");
  const pill = $("#verify-pill");
  pill.textContent = `校验通过 · ${result.checks} 项检查`;
  pill.className = "verify-pill ok";
  toast("账本、队列、借调与快照摘要全部通过");
}));

$("#resume-btn").addEventListener("click", () => withBusy($("#resume-btn"), async () => {
  const now = Number($("#resume-now").value);
  const result = await api("/api/resume", { method: "POST", body: JSON.stringify({ now_ms: now }) });
  toast(`逻辑时钟推进至 ${result.logical_clock_ms}ms，恢复 ${result.recovered_operations.length} 个操作，释放 ${result.due_releases.length} 个到期预留`);
}));

$("#submit-btn").addEventListener("click", () => withBusy($("#submit-btn"), async () => {
  const application_id = $("#submit-app").value;
  if (!application_id) throw new Error("没有可提交的暂存申请");
  const now_ms = Number($("#submit-now").value);
  const result = await api("/api/submit", { method: "POST", body: JSON.stringify({ application_id, now_ms }) });
  toast(`${application_id} → ${result.application.status}${result.application.reason_code ? " / " + result.application.reason_code : ""}`);
}));

$("#release-btn").addEventListener("click", () => withBusy($("#release-btn"), async () => {
  const application_id = $("#release-app").value;
  if (!application_id) throw new Error("没有可释放的活动预留");
  const now_ms = Number($("#release-now").value);
  const trigger_type = $("#release-trigger").value;
  const result = await api("/api/release", { method: "POST", body: JSON.stringify({ application_id, now_ms, trigger_type }) });
  toast(`${application_id} 已释放，追平 ${result.wake_attempts?.length || 0} 个等待申请`);
}));

$("#run-demo").addEventListener("click", () => withBusy($("#run-demo"), async () => {
  const result = await api("/api/demo/run", { method: "POST", body: "{}" });
  const pill = $("#verify-pill");
  pill.textContent = result.ok ? `演示完成 · ${result.verify.checks} 项校验通过` : "演示完成但校验失败";
  pill.className = `verify-pill ${result.ok ? "ok" : "bad"}`;
  toast("已验证并行准入、借调、到期追平与原子快照");
}));

$("#reset-demo").addEventListener("click", () => withBusy($("#reset-demo"), async () => {
  await api("/api/demo/reset", { method: "POST", body: "{}" });
  $("#verify-pill").textContent = "样本已重置";
  $("#verify-pill").className = "verify-pill";
  toast("确定性样本已清空并重新装载");
}));

["resume-now", "submit-now", "release-now"].forEach((id) => {
  $(`#${id}`).addEventListener("input", (event) => { event.target.dataset.touched = "1"; });
});

refresh().catch((err) => toast(err.message, true));
setInterval(() => {
  if (document.visibilityState === "visible") refresh().catch(() => {});
}, 8000);
