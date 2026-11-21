const state = {
  dashboard: null,
  selectedRound: "",
  busy: false,
};

const $ = (id) => document.getElementById(id);
const fmt = (value) => Number(value ?? 0).toLocaleString("zh-CN");

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.add("show");
  clearTimeout(element._timer);
  element._timer = setTimeout(() => element.classList.remove("show"), 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`HTTP ${response.status}`);
  }
  if (!response.ok || payload.ok === false) {
    const detail = payload?.error;
    const err = new Error(detail?.message || `HTTP ${response.status}`);
    err.business = detail;
    throw err;
  }
  return payload;
}

function setBusy(value) {
  state.busy = value;
  for (const id of ["stepBtn", "runBtn", "resetBtn", "recoverBtn"]) {
    $(id).disabled = value;
  }
}

function summaryOf(snapshot) {
  return snapshot?.summary || snapshot?.state?.last_round || null;
}

function labelClass(label) {
  return ["gold", "silver", "bronze"].includes(label) ? label : "";
}

function renderMetrics(data) {
  const snapshot = data.snapshot;
  const stateData = data.state;
  const summary = summaryOf(snapshot) || {};
  const config = stateData.config;
  const activeLeases = Object.values(stateData.resources).filter((resource) => resource.active).length;
  const totalCap = Object.values(stateData.holders).reduce((sum, holder) => sum + holder.max_concurrent_grants, 0);
  const totalOccupied = Object.values(stateData.holders).reduce((sum, holder) => sum + holder.occupied_count, 0);
  const used = summary.used_budget_ms ?? 0;
  const budget = summary.budget_ms ?? config.round_budget_ms;
  const fairness = summary.fairness || {};
  const granted = summary.granted_count ?? (summary.grants || []).length;

  $("metricRound").textContent = fmt(snapshot.round_no);
  $("metricRoundFoot").textContent = stateData.last_round ? "已提交快照" : "初始快照";
  $("metricClock").textContent = fmt(snapshot.logical_clock_ms);
  $("metricStep").textContent = `+${fmt(config.clock_step_ms)} ms`;
  $("metricBudget").textContent = fmt(used);
  $("metricBudget").parentElement.querySelector("em").textContent = `/ ${fmt(budget)} ms`;
  $("budgetBar").style.width = `${Math.min(100, budget ? (used / budget) * 100 : 0)}%`;
  $("metricActive").textContent = fmt(activeLeases);
  $("metricHolders").textContent = `${totalOccupied} / ${totalCap}`;
  $("metricQuota").textContent = summary.fairness ? `${granted}` : "—";
  $("metricQuotaUnit").textContent = summary.fairness ? "awarded" : "awarded";
  $("metricQuotaFoot").textContent = `min ${config.fair_quota_min_grants}`;
  $("quotaBadge").textContent = !summary.fairness
    ? "等待轮次"
    : fairness.satisfied ? "配额满足" : "配额不足";
  $("quotaBadge").className = `trend ${summary.fairness && !fairness.satisfied ? "danger" : ""}`;
  $("metricYield").textContent = fmt((summary.forced_yields || []).length);
  $("metricYieldFoot").textContent = `上限 ${config.max_forced_yields_per_application}/申请`;
  $("metricErrorCode").textContent = `${fmt((summary.errors || []).length)} errors`;
  $("topVersion").textContent = `v${snapshot.version}`;
  $("commitStatus").textContent = summary.errors?.length ? "COMMITTED + ERRORS" : "COMMITTED";
  $("commitStatus").classList.toggle("is-error", Boolean(summary.errors?.length));
  $("statusText").textContent = summary.errors?.length
    ? `快照已原子提交，隔离记录 ${summary.errors.length} 个业务错误`
    : `轮次 ${snapshot.round_no} 已完整提交，读取方不可见中间态`;
}

function renderScenario(data) {
  const config = data.state.config;
  $("scenarioName").textContent = data.bundle_path.split(/[\\/]/).pop();
  $("scenarioMeta").innerHTML = [
    `${Object.keys(data.state.resources).length} 锁键 · ${Object.keys(data.state.holders).length} 持有者`,
    `预算 ${fmt(config.round_budget_ms)} ms · 配额 ${config.fair_quota_min_grants}`,
    `饥饿阈值 ${config.starvation_rounds} 轮 · 步进 ${fmt(config.clock_step_ms)} ms`,
  ].map((line) => `<div>${esc(line)}</div>`).join("");
}

function renderResources(data) {
  const stateData = data.state;
  const summary = summaryOf(data.snapshot) || {};
  const forcedSeqs = new Set((summary.forced_yields || []).map((item) => item.seq));
  const resources = Object.entries(stateData.resources);
  const totalQueued = resources.reduce((sum, [, resource]) => sum + (stateData.queues[resource.lock_key]?.queued_count || 0), 0);
  $("queueCount").textContent = `${resources.length} queues`;
  $("pendingCount").textContent = `${totalQueued} pending`;
  $("resourceGrid").innerHTML = resources.map(([key, resource]) => {
    const queue = stateData.queues[key] || { groups: {}, ordered_seqs: [] };
    const active = resource.active;
    const activeApp = active ? stateData.applications[String(active.seq)] : null;
    const groups = stateData.config.fairness_labels.map((label) => {
      const group = queue.groups[label] || { seqs: [], count: 0 };
      const chips = (group.seqs || []).slice(0, 8).map((seq) => {
        const app = stateData.applications[String(seq)];
        const forced = forcedSeqs.has(seq) || (
          app.wait_rounds >= stateData.config.starvation_rounds &&
          app.forced_yields < stateData.config.max_forced_yields_per_application
        );
        return `<span class="queue-chip ${app.wait_rounds >= stateData.config.starvation_rounds ? "waiting" : ""} ${forced ? "forced" : ""}" title="位置 ${app.queue_position} · 优先级 ${app.priority}">#${app.seq}<span class="wait">w${app.wait_rounds}</span></span>`;
      }).join("");
      return `<div class="group-row">
        <div class="group-label ${labelClass(label)}">${esc(label)}</div>
        <div class="group-count">${group.count || 0}</div>
        <div class="queue-chips">${chips || '<span class="queue-empty">空</span>'}</div>
      </div>`;
    }).join("");
    return `<article class="resource-card">
      <div class="resource-head">
        <div>
          <div class="resource-name"><span class="status-dot ${active ? "active" : ""}"></span>${esc(key)}</div>
          <div class="resource-sub">lease ${fmt(resource.lease_duration_ms)} ms · max wait ${resource.max_wait_rounds} · gen ${resource.lease_generation}</div>
        </div>
        <span class="priority-chip">P${resource.priority}</span>
      </div>
      ${active ? `<div class="active-lease">
        <div><strong>#${active.seq} · ${esc(active.holder)}</strong><small>grant @ ${fmt(active.start_clock_ms)} ms → expire @ ${fmt(active.expires_at_ms)} ms</small></div>
        <div class="gen">GEN<br>${active.lease_generation}</div>
      </div>` : ""}
      <div class="groups">${groups}</div>
    </article>`;
  }).join("");
}

function renderDecisions(data) {
  const summary = summaryOf(data.snapshot) || {};
  const config = data.state.config;
  const grants = summary.grants || [];
  const deferred = summary.deferred || [];
  const yields = summary.forced_yields || [];
  const budget = summary.budget_ms ?? config.round_budget_ms;
  const used = summary.used_budget_ms ?? 0;
  $("decisionRound").textContent = `R${data.snapshot.round_no}`;
  $("constraintBudget").textContent = `${fmt(used)} / ${fmt(budget)} ms`;
  $("constraintQuota").textContent = summary.fairness
    ? `${summary.fairness.awarded ?? grants.length} / min ${summary.fairness.minimum ?? config.fair_quota_min_grants}`
    : "未执行";
  const triggerCounts = deferred.reduce((acc, item) => {
    acc[item.first_trigger] = (acc[item.first_trigger] || 0) + 1;
    return acc;
  }, {});
  $("constraintTrigger").textContent = Object.entries(triggerCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => `${key} ×${count}`)
    .join(" · ") || "—";
  $("grantCount").textContent = grants.length;
  $("yieldCount").textContent = yields.length;
  $("deferCount").textContent = deferred.length;
  $("grantTrace").classList.toggle("empty-state", grants.length === 0);
  $("grantTrace").innerHTML = grants.length ? grants.map((grant) => `
    <div class="trace-item">
      <div><strong>#${grant.seq} ${esc(grant.lock_key)}</strong><div class="mono">${esc(grant.holder)} · gen ${grant.lease_generation}</div></div>
      <div class="mono">${fmt(grant.start_clock_ms)}→${fmt(grant.expires_at_ms)}<br>${grant.cost_ms} ms</div>
    </div>`).join("") : "尚未提交轮次";
  $("yieldTrace").classList.toggle("empty-state", yields.length === 0);
  $("yieldTrace").innerHTML = yields.length ? yields.map((item) => `
    <div class="trace-item yield">
      <div><strong>#${item.seq} 强制让渡</strong><div class="mono">wait ${item.wait_rounds} ≥ ${item.threshold}</div></div>
      <div class="mono">对比 #${item.compared_application_seq ?? "—"} P${item.compared_priority ?? "—"}<br>避免 ${item.estimated_avoided_delay_rounds ?? 0} 轮推迟</div>
    </div>`).join("") : "没有触发饥饿阈值";
  $("deferTrace").classList.toggle("empty-state", deferred.length === 0);
  $("deferTrace").innerHTML = deferred.length ? deferred.map((item) => `
    <div class="trace-item">
      <div><strong>#${item.seq} ${esc(item.fairness_label)}</strong><div class="mono">#${item.queue_position} · wait ${item.wait_rounds}</div></div>
      <div class="mono">${esc(item.first_trigger)}<br>delay ${item.deferred_rounds}</div>
    </div>`).join("") : "没有候选被推迟";
}

function flattenLeases(data) {
  const rows = [];
  for (const round of data.rounds) {
    const summary = round.summary || {};
    const expiredMap = new Map((summary.expired || []).map((item) => [item.seq, item]));
    for (const grant of summary.grants || []) {
      const activeApp = data.state.applications[String(grant.seq)];
      let status = grant.status || "active";
      if (activeApp?.status === "expired" || expiredMap.has(grant.seq)) status = "expired";
      if (activeApp?.status === "granted") status = "active";
      rows.push({ round: round.round_no, ...grant, displayStatus: status });
    }
  }
  return rows.slice(-40).reverse();
}

function renderLeases(data) {
  const rows = flattenLeases(data);
  $("leaseLedgerCount").textContent = `${rows.length} entries`;
  $("leaseTable").innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td>R${row.round}</td>
      <td>#${row.seq}</td>
      <td>${esc(row.lock_key)}</td>
      <td>${esc(row.holder)}</td>
      <td>G${row.lease_generation}</td>
      <td>${fmt(row.start_clock_ms)} → ${fmt(row.expires_at_ms)}</td>
      <td>${fmt(row.cost_ms)} ms</td>
      <td><span class="status-tag ${row.forced_yield ? "forced" : row.displayStatus}">${row.forced_yield ? "forced" : row.displayStatus}</span></td>
    </tr>`).join("") : '<tr><td colspan="8" class="table-empty">等待首个轮次</td></tr>';
}

function renderErrors(data) {
  const errors = data.errors || [];
  $("errorCount").textContent = `${errors.length} errors`;
  $("errorList").classList.toggle("empty-state", errors.length === 0);
  $("errorList").innerHTML = errors.length ? errors.slice(0, 30).map((issue) => `
    <div class="error-item">
      <div class="error-head"><span class="error-code">${esc(issue.code)}</span><span class="error-round">R${issue.round_no} · ${esc(issue.constraint || "—")}</span></div>
      <div class="error-msg">${esc(issue.message)}</div>
      <div class="error-detail">lock=${esc(issue.lock_key ?? "—")} seq=${esc(issue.application_seq ?? "—")} ${esc(JSON.stringify(issue.details || {}))}</div>
    </div>`).join("") : "错误码链路为空";
}

function renderEvents(data) {
  const events = data.events || [];
  const timeline = $("eventTimeline");
  timeline.classList.toggle("empty-state", events.length === 0);
  timeline.innerHTML = events.length ? events.slice(-24).map((event) => `
    <div class="event-node ${esc(event.event_type)}">
      <div class="event-top"><span>R${event.round_no}</span><span>pos ${event.queue_position ?? "—"}</span></div>
      <div class="event-type">#${event.seq} · ${esc(event.event_type)}</div>
      <div class="event-detail">${esc(event.lock_key)} · ${esc(event.fairness_label)}<br>wait ${event.wait_rounds} · ${esc(event.details?.lease_generation ? `gen ${event.details.lease_generation}` : event.details?.reason || "")}</div>
    </div>`).join("") : "等待轮次提交";
}

function renderRoundOptions(data) {
  const select = $("roundSelect");
  const currentValue = state.selectedRound;
  const options = ['<option value="">最新</option>'];
  for (const round of [...data.rounds].sort((a, b) => b.round_no - a.round_no)) {
    options.push(`<option value="${round.round_no}">R${round.round_no} · v${round.snapshot_version}</option>`);
  }
  select.innerHTML = options.join("");
  select.value = currentValue;
}

function render(data) {
  state.dashboard = data;
  renderScenario(data);
  renderMetrics(data);
  renderResources(data);
  renderDecisions(data);
  renderLeases(data);
  renderErrors(data);
  renderEvents(data);
  renderRoundOptions(data);
  $("connectionState").textContent = "SQLite 快照读取正常";
}

async function load(round = state.selectedRound) {
  const query = round ? `?round=${encodeURIComponent(round)}` : "";
  const data = await api(`/api/state${query}`);
  render(data);
}

async function runAction(action, successMessage) {
  if (state.busy) return;
  setBusy(true);
  try {
    const result = await action();
    if (result?.dashboard) {
      render(result.dashboard);
    } else {
      await load();
    }
    toast(successMessage);
  } catch (error) {
    const code = error.business?.code;
    const detail = error.business
      ? `${error.business.code} · ${error.business.message}${error.business.constraint ? ` [${error.business.constraint}]` : ""}`
      : error.message;
    toast(detail, true);
    $("connectionState").textContent = code ? `业务拒绝: ${code}` : "请求失败";
  } finally {
    setBusy(false);
  }
}

$("stepBtn").addEventListener("click", () => {
  state.selectedRound = "";
  runAction(
    () => api("/api/step", { method: "POST", body: JSON.stringify({}) }),
    "轮次已原子提交",
  );
});
$("runBtn").addEventListener("click", () => {
  state.selectedRound = "";
  runAction(
    () => api("/api/run", { method: "POST", body: JSON.stringify({ count: 5 }) }),
    "连续 5 轮已提交",
  );
});
$("resetBtn").addEventListener("click", () => {
  if (!window.confirm("重置演示 SQLite 状态到初始样本？")) return;
  state.selectedRound = "";
  runAction(
    () => api("/api/reset", { method: "POST", body: JSON.stringify({}) }),
    "演示状态已重置",
  );
});
$("recoverBtn").addEventListener("click", () => {
  runAction(
    () => api("/api/recover", { method: "POST", body: JSON.stringify({}) }),
    "恢复裁决完成",
  );
});
$("roundSelect").addEventListener("change", async (event) => {
  state.selectedRound = event.target.value;
  try {
    await load();
  } catch (error) {
    toast(error.message, true);
  }
});
document.querySelectorAll("[data-scroll]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    $(button.dataset.scroll).scrollIntoView({ behavior: "smooth", block: "start" });
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.metaKey && !event.ctrlKey && !event.altKey) {
    const tag = document.activeElement?.tagName;
    if (tag !== "SELECT" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "BUTTON") {
      $("stepBtn").click();
    }
  }
});

load().catch((error) => {
  toast(error.message, true);
  $("connectionState").textContent = "快照读取失败";
});
