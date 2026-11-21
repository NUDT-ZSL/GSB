const STAGE_NAMES = {
  validate: "校验",
  segment: "分词",
  tag: "标注",
  summarize: "汇总",
  not_started: "未开始",
};

const STAGE_EN = {
  validate: "VALIDATE",
  segment: "SEGMENT",
  tag: "TAG",
  summarize: "SUMMARIZE",
};

const ACTION_LABELS = {
  run_full: "完整运行",
  crash: "强制终止",
  resume: "快照恢复",
  upgrade: "词典与标签升级",
  low_budget: "低预算降级",
  strict: "严格资源上限",
  errors: "异常链路",
  demo: "一键对比演示",
  selfcheck: "跨模块自检",
  reset: "重置工作区",
};

let currentState = null;
let lastErrorChain = null;
let lastDemo = null;
let toastTimer = null;

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

function shortHash(value) {
  return value ? String(value).slice(0, 12) : "—";
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", Boolean(isError));
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 5000);
}

function setBusy(busy, title = "正在执行") {
  $("#overlay").hidden = !busy;
  $("#overlay-title").textContent = title;
  document.querySelectorAll("button[data-action]").forEach((button) => {
    button.disabled = busy;
  });
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok && !data.code) {
    throw new Error(`HTTP ${response.status}`);
  }
  return data;
}

async function loadState() {
  try {
    const state = await fetchJSON("/api/state");
    currentState = state;
    renderState(state);
    await loadResults();
    setPill(state.ok !== false, state.ok ? "工作台就绪" : state.code);
  } catch (error) {
    setPill(false, "连接失败");
    showToast(`状态读取失败：${error.message}`, true);
  }
}

async function loadResults() {
  try {
    const results = await fetchJSON("/api/results");
    renderResults(results);
  } catch (error) {
    renderResults({ annotations: [], summary: null });
  }
}

function setPill(ok, text) {
  const pill = $("#status-pill");
  pill.innerHTML = `<span class="pulse" style="${ok ? "" : "background:#e07b6b"}"></span><span>${esc(text)}</span>`;
}

function renderState(state) {
  const stageCards = state.stage_cards || [];
  const committed = stageCards.filter((item) => item.committed);
  const currentStage = state.current_stage || "not_started";
  const latest = committed.length ? committed[committed.length - 1] : null;
  const active = state.active_generation || null;

  $("#metric-stage").textContent = STAGE_NAMES[currentStage] || currentStage;
  $("#metric-complete").textContent = state.complete ? "四阶段全部提交" : (committed.length ? "等待后缀阶段" : "等待运行");
  $("#metric-generation").textContent = active ? `G${active.generation}` : "—";
  $("#metric-generation-foot").textContent = active ? `词典 v${active.dict_version} · 标签 v${active.labels_version}` : "尚无快照";
  $("#metric-version").textContent = latest ? `v${latest.snapshot_version}` : "—";
  $("#metric-version-foot").textContent = latest ? `校验和 ${shortHash(latest.payload_checksum)}` : "单调递增";
  const rowCount = committed.length ? Math.max(...committed.map((item) => item.row_count || 0)) : 0;
  $("#metric-rows").textContent = rowCount;
  $("#metric-rows-foot").textContent = state.complete ? "完整提交已汇总" : "完整提交后才计入";
  $("#snapshot-count").textContent = `${committed.length} 个已提交快照`;

  renderStageTrack(stageCards, currentStage);
  renderRecovery(state);
  renderReuse(state);
  renderDegradations(state);
  renderSelfcheck(state);
  renderErrors(state);
  renderHistory(state);
  renderDemo(state.demo_report || lastDemo);
  if (lastErrorChain) renderErrorChain(lastErrorChain);
}

function renderStageTrack(cards, currentStage) {
  const track = $("#stage-track");
  if (!cards.length) {
    track.innerHTML = ["validate", "segment", "tag", "summarize"].map((stage) => `
      <div class="stage-node">
        <div class="stage-dot"></div>
        <div class="stage-name">${STAGE_NAMES[stage]}</div>
        <div class="stage-en">${STAGE_EN[stage]}</div>
        <div class="stage-meta"><span>未提交</span><span>—</span></div>
      </div>`).join("");
    $("#stage-detail").innerHTML = '<div class="empty-state">完整运行后展示每个阶段的快照版本、行数与校验和。</div>';
    return;
  }
  track.innerHTML = cards.map((card) => `
    <div class="stage-node ${card.committed ? "committed" : ""} ${currentStage === card.stage ? "active" : ""}">
      <div class="stage-dot"></div>
      <div class="stage-name">${STAGE_NAMES[card.stage]}</div>
      <div class="stage-en">${STAGE_EN[card.stage]}</div>
      <div class="stage-meta">
        <span>${card.committed ? `v${card.snapshot_version}` : "未提交"}</span>
        <span>${card.committed ? `${card.row_count} 行` : "—"}</span>
      </div>
    </div>`).join("");
  $("#stage-detail").innerHTML = cards.map((card) => `
    <div class="detail-tile">
      ${STAGE_NAMES[card.stage]}快照
      <b>${card.committed ? `v${card.snapshot_version} · ${shortHash(card.payload_checksum)}` : "未生成"}</b>
      <div>${card.committed ? `${card.completed_doc_count} doc · ${card.degradation_count || 0} 降级` : "等待提交"}</div>
    </div>`).join("");
}

function renderRecovery(state) {
  const report = state.recovery_report;
  const mode = report?.mode || (state.mode || "—");
  const modeTag = $("#recovery-mode");
  modeTag.textContent = mode;
  modeTag.className = `tag ${mode === "upgrade" ? "tag-warm" : ""}`;
  if (!report) {
    $("#recovery-body").innerHTML = '<div class="empty-state">尚无恢复记录。</div>';
    return;
  }
  $("#recovery-body").innerHTML = `
    <div class="kv"><span>最后已提交阶段</span><b>${esc(report.last_committed_stage_at_start || "无")}</b></div>
    <div class="kv"><span>恢复起点</span><b>${esc(report.resume_from_stage || "已完成")}</b></div>
    <div class="kv"><span>目标代际</span><b>G${esc(report.target_generation)}</b></div>
    <div class="kv"><span>基线代际</span><b>${report.base_generation ? `G${esc(report.base_generation)}` : "—"}</b></div>
    <div class="kv"><span>本次跳过已提交阶段</span><b>${esc((report.skipped_committed_stages || []).join(" · ") || "无")}</b></div>
    <div class="kv"><span>本次执行阶段</span><b>${esc((report.executed_stages_this_run || []).join(" · ") || "无")}</b></div>
    <div class="kv"><span>未提交中间态</span><b>丢弃并重建</b></div>
    <div class="chips">${(report.reasons || []).map((reason) => `<span class="chip">${esc(reason)}</span>`).join("") || '<span class="chip">initial</span>'}</div>
  `;
}

function renderReuse(state) {
  const affected = state.affected || {};
  const recomputed = state.recomputed || {};
  const reused = state.reused || {};
  const stages = ["validate", "segment", "tag", "summarize"];
  const hasData = stages.some((stage) => (recomputed[stage] || []).length || (reused[stage] || []).length);
  $("#upgrade-state").textContent = hasData && (state.generations || []).length > 1 ? "已发生版本替换" : "当前代际";
  $("#upgrade-state").className = `tag ${(state.generations || []).length > 1 ? "tag-warm" : ""}`;
  if (!hasData) {
    $("#reuse-body").innerHTML = '<div class="empty-state">词典版本变化后展示受影响集合与复用比例。</div>';
    return;
  }
  $("#reuse-body").innerHTML = stages.map((stage) => {
    const recomputeCount = (recomputed[stage] || []).length;
    const reuseCount = (reused[stage] || []).length;
    const affectedCount = (affected[stage] || []).length;
    const total = Math.max(1, recomputeCount + reuseCount);
    return `
      <div>
        <div class="kv"><span>${STAGE_NAMES[stage]} · 受影响 ${affectedCount}</span><b>重算 ${recomputeCount} / 复用 ${reuseCount}</b></div>
        <div class="metric-bar">
          <span class="used" style="width:${(recomputeCount / total) * 100}%"></span>
          <span class="reuse" style="width:${(reuseCount / total) * 100}%"></span>
        </div>
      </div>`;
  }).join("") + `
    <div class="chips">
      ${(affected.segment || []).slice(0, 20).map((docId) => `<span class="chip">${esc(docId)}</span>`).join("")}
    </div>`;
}

function renderDegradations(state) {
  const items = state.degradations || [];
  $("#degradation-count").textContent = String(items.length);
  $("#degradation-count").className = `tag ${items.length ? "tag-warn" : ""}`;
  if (!items.length) {
    $("#degradation-body").innerHTML = '<div class="empty-state">当前没有降级记录。</div>';
    return;
  }
  const grouped = {};
  for (const item of items) grouped[item.category] = (grouped[item.category] || 0) + 1;
  $("#degradation-body").innerHTML = `
    <div class="callout warn">降级只限制候选或中间缓存，不改变 Unicode 码点偏移口径。</div>
    ${Object.entries(grouped).map(([category, count]) => `
      <div class="kv"><span>${esc(category)}</span><b>${count} 条</b></div>`).join("")}
    <div class="chips">${items.slice(0, 18).map((item) => `<span class="chip">${esc(item.doc_id || "stage")} · ${esc(item.category)}</span>`).join("")}</div>
  `;
}

function renderSelfcheck(state) {
  const selfcheck = state.selfcheck;
  const code = selfcheck?.code || "—";
  const badge = $("#selfcheck-code");
  badge.textContent = code;
  badge.className = `tag ${code === "OK" ? "" : code === "—" ? "" : "tag-bad"}`;
  if (!selfcheck) {
    $("#selfcheck-body").innerHTML = '<div class="empty-state">运行后校验偏移覆盖、受控标签和集合关系。</div>';
    return;
  }
  if (selfcheck.ok) {
    $("#selfcheck-body").innerHTML = `
      <div class="callout good">偏移首尾相接并覆盖全行；标签全部来自受控集合；重算与复用集合无交集且覆盖完整。</div>
      <div class="kv"><span>检查项</span><b>OFFSET · LABEL · SET COVERAGE</b></div>
      <div class="kv"><span>问题数</span><b>0</b></div>`;
    return;
  }
  $("#selfcheck-body").innerHTML = `
    <div class="callout bad">一致性自检失败，返回 E_INCONSISTENT。</div>
    ${(selfcheck.issues || []).slice(0, 8).map((issue) => `
      <div class="error-row">
        <div class="error-code">${esc(issue.category)}</div>
        <div class="error-msg">${esc(issue.message)} ${issue.doc_id ? `· ${esc(issue.doc_id)}` : ""}</div>
      </div>`).join("")}`;
}

function renderErrors(state) {
  const errors = state.recent_errors || [];
  $("#error-count").textContent = String(errors.length);
  if (!errors.length) {
    $("#error-body").innerHTML = '<div class="empty-state">业务异常会显示错误码、doc_id 与行号。</div>';
    return;
  }
  $("#error-body").innerHTML = errors.slice(0, 10).map((error) => `
    <div class="error-row">
      <div class="error-code">${esc(error.code)}</div>
      <div class="error-msg">${esc(error.message)}${error.doc_id ? ` · doc=${esc(error.doc_id)}` : ""}${error.line_no ? ` · line=${esc(error.line_no)}` : ""}</div>
    </div>`).join("");
}

function renderHistory(state) {
  const generations = state.generations || [];
  const snapshots = state.snapshots || [];
  if (!generations.length) {
    $("#history-body").innerHTML = '<div class="empty-state">尚无提交历史。</div>';
    return;
  }
  $("#history-body").innerHTML = `
    <table>
      <thead><tr><th>代际</th><th>词典</th><th>标签集</th><th>基线</th><th>阶段快照版本</th></tr></thead>
      <tbody>
        ${generations.slice().reverse().map((generation) => {
          const generationSnapshots = snapshots.filter((item) => item.generation === generation.generation);
          return `<tr>
            <td><b>G${generation.generation}</b></td>
            <td>v${generation.dict_version}<br><span class="mono">${shortHash(generation.dict_checksum)}</span></td>
            <td>v${generation.labels_version}<br><span class="mono">${shortHash(generation.labels_checksum)}</span></td>
            <td>${generation.base_generation ? `G${generation.base_generation}` : "—"}</td>
            <td>${generationSnapshots.map((item) => `${item.stage}:v${item.snapshot_version}`).join("<br>") || "—"}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

function renderDemo(report) {
  const badge = $("#demo-code");
  if (!report) {
    badge.textContent = "未运行";
    badge.className = "tag";
    $("#demo-body").innerHTML = '<div class="empty-state">执行完整运行、硬终止、恢复、v2 升级与降级对比。</div>';
    return;
  }
  badge.textContent = report.ok ? "全部通过" : "存在失败";
  badge.className = `tag ${report.ok ? "" : "tag-bad"}`;
  const checks = Object.entries(report.checks || {});
  $("#demo-body").innerHTML = `
    <div class="callout ${report.ok ? "good" : "bad"}">
      恢复一致：${report.recovery?.byte_identical ? "逐字节一致" : "不一致"} ·
      v2 升级：${report.upgrade?.byte_identical_to_fresh_v2 ? "与全新运行一致" : "不一致"} ·
      硬终止返回码：${report.recovery?.crash_returncode}
    </div>
    <div class="kv"><span>重算集合</span><b>${JSON.stringify(report.upgrade?.recomputed_counts || {})}</b></div>
    <div class="kv"><span>复用集合</span><b>${JSON.stringify(report.upgrade?.reused_counts || {})}</b></div>
    <div class="kv"><span>降级类别</span><b>${(report.low_budget?.categories || []).join(" · ") || "—"}</b></div>
    <div class="chips">${checks.map(([name, passed]) => `<span class="chip" style="${passed ? "" : "background:#f5dddd;color:#8e3434"}">${esc(name)}</span>`).join("")}</div>`;
}

function renderErrorChain(report) {
  const body = $("#error-body");
  if (!report?.scenarios) return;
  body.innerHTML = report.scenarios.map((item) => `
    <div class="error-row">
      <div class="error-code">${esc(item.scenario)} → ${esc(item.actual_code)}</div>
      <div class="error-msg">${item.doc_id ? `doc=${esc(item.doc_id)}` : "资源级检查"}${item.line_no ? ` · line=${esc(item.line_no)}` : ""}</div>
    </div>`).join("");
  $("#error-count").textContent = `${report.scenarios.length} 类`;
}

function renderResults(results) {
  const annotations = results.annotations || [];
  const summary = results.summary;
  if (!annotations.length) {
    $("#result-summary").textContent = "等待结果";
    $("#results-body").innerHTML = '<div class="empty-state">完成一次级联后展示逐词标签与置信度。</div>';
    return;
  }
  $("#result-summary").textContent = `${summary?.doc_count || annotations.length} 行 · ${summary?.token_count || 0} 词 · 平均置信度 ${summary?.mean_confidence ?? "—"}`;
  $("#results-body").innerHTML = annotations.map((line) => `
    <div class="result-line">
      <div class="result-head"><span>${esc(line.doc_id)} · line ${esc(line.line_no)}</span><span>路径分 ${esc(line.path_score)} · 平均置信度 ${esc(line.mean_confidence)}</span></div>
      <div class="tokens">
        ${(line.tokens || []).map((token) => `
          <span class="token" title="offset ${token.start}-${token.end}">
            <span class="token-word">${esc(token.word)}</span>
            <span class="token-pos">${esc(token.label)}</span>
            <span class="token-conf">${(Number(token.confidence) * 100).toFixed(0)}%</span>
          </span>`).join("")}
      </div>
    </div>`).join("");
}

async function runAction(action) {
  const payload = { action };
  if (action === "crash") payload.stage = $("#crash-stage").value;
  const label = ACTION_LABELS[action] || action;
  setBusy(true, `${label}执行中`);
  showToast(`正在执行：${label}`);
  try {
    const result = await fetchJSON("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (result.error_chain) {
      lastErrorChain = result.error_chain;
      renderErrorChain(result.error_chain);
    }
    if (result.demo_report) {
      lastDemo = result.demo_report;
      renderDemo(result.demo_report);
    }
    if (result.state) {
      if (["run_full", "crash", "resume", "upgrade", "reset"].includes(action)) {
        currentState = result.state;
        renderState(result.state);
        await loadResults();
      } else {
        renderState(currentState || result.state);
      }
    }
    if (["low_budget", "strict"].includes(action)) {
      renderSpecialResult(label, result);
    }
    const suffix = result.code ? ` · ${result.code}` : "";
    showToast(`${label}完成${suffix}`, !result.ok);
    setPill(result.ok !== false, result.ok === false ? result.code : "工作台就绪");
  } catch (error) {
    showToast(`${label}失败：${error.message}`, true);
    setPill(false, "执行失败");
  } finally {
    setBusy(false);
  }
}

function renderSpecialResult(label, result) {
  const state = result.state || {};
  const degr = state.degradations || [];
  const errors = state.recent_errors || [];
  const recompute = state.recomputed || {};
  const reused = state.reused || {};
  $("#demo-body").innerHTML = `
    <div class="callout ${result.ok ? "good" : "warn"}">
      ${esc(label)} · 返回码 ${esc(result.process?.returncode ?? "—")} · ${esc(result.code || "—")}
    </div>
    <div class="kv"><span>当前阶段</span><b>${esc(STAGE_NAMES[state.current_stage] || state.current_stage || "—")}</b></div>
    <div class="kv"><span>降级记录</span><b>${degr.length}</b></div>
    <div class="kv"><span>阶段重算</span><b>${JSON.stringify(Object.fromEntries(Object.entries(recompute).map(([k,v]) => [k,v.length])))}</b></div>
    <div class="kv"><span>阶段复用</span><b>${JSON.stringify(Object.fromEntries(Object.entries(reused).map(([k,v]) => [k,v.length])))}</b></div>
    ${errors.slice(0,5).map((error) => `<div class="error-row"><div class="error-code">${esc(error.code)}</div><div class="error-msg">${esc(error.message)}</div></div>`).join("")}
  `;
  $("#demo-code").textContent = result.code || "完成";
  $("#demo-code").className = `tag ${result.ok ? "" : "tag-warn"}`;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  runAction(button.dataset.action);
});

$("#refresh-btn").addEventListener("click", () => loadState());
document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, select, textarea")) return;
  const key = event.key.toLowerCase();
  if (key === "f") runAction("run_full");
  if (key === "r") runAction("resume");
  if (key === "k") runAction("crash");
});

loadState();
