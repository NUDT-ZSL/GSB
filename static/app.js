const state = {
  data: null,
  selectedPage: null,
  editorLines: [],
  expandedLines: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function shortHash(value, length = 12) {
  if (!value) return "—";
  return String(value).slice(0, length);
}

function formatBox(line) {
  const box = line.truth_box || line.box || [];
  return `[${box.join(", ")}]`;
}

function pageResult(pageId) {
  const pages = state.data?.report?.pages || [];
  return pages.find((page) => page.page_id === pageId) || null;
}

function selectedRecord() {
  return state.data?.pages?.find((page) => page.page_id === state.selectedPage) || null;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({
    ok: false,
    error: { code: "E_INTERNAL", message: "invalid server response", stage: "api" },
  }));
  if (!response.ok || payload.ok === false) {
    const error = payload.error || {
      code: "E_INTERNAL",
      message: "request failed",
      stage: "api",
    };
    const exception = new Error(error.message || error.code);
    exception.business = error;
    throw exception;
  }
  return payload;
}

function showBusy(title) {
  $("#busyTitle").textContent = title;
  $("#busyOverlay").classList.remove("hidden");
}

function hideBusy() {
  $("#busyOverlay").classList.add("hidden");
}

function showToast(message, kind = "info") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.style.background = kind === "error" ? "#6f2420" : "#20242a";
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

function showError(error) {
  const business = error.business || {
    code: "E_INTERNAL",
    message: error.message,
    stage: "api",
  };
  $("#errorCode").textContent = business.code;
  const parts = [business.message, business.stage ? `stage=${business.stage}` : null, business.page_id || null]
    .filter(Boolean);
  $("#errorMessage").textContent = parts.join(" · ");
  $("#errorBanner").classList.remove("hidden");
}

async function refresh({ preserveSelection = true } = {}) {
  const payload = await api("/api/state");
  state.data = payload.state;
  if (!preserveSelection || !state.data.pages.some((page) => page.page_id === state.selectedPage)) {
    state.selectedPage = state.data.pages[0]?.page_id || null;
  }
  renderAll();
  if (!state.data.report?.pages) {
    state.editorLines = cloneLines(selectedRecord()?.lines || []);
    renderEditor();
  }
}

function cloneLines(lines) {
  return lines.map((line, index) => ({
    line_no: index,
    text: line.text,
    truth_box: [...line.truth_box],
  }));
}

function renderAll() {
  renderSidebar();
  renderStageStrip();
  renderCanvas();
  syncEditorFromSelected();
  renderResults();
  renderInjections();
  renderDegradations();
  renderSelfcheck();
  renderRecovery();
  renderFingerprints();
  renderLatestError();
}

function renderSidebar() {
  const pages = state.data?.pages || [];
  $("#datasetChecksum").textContent = shortHash(state.data?.workspace?.dataset_checksum);
  $("#templateVersion").textContent = state.data?.template_library?.version || "—";
  $("#configVersion").textContent = state.data?.config?.version || "—";
  const list = $("#pageList");
  if (!pages.length) {
    list.innerHTML = `<div class="empty-state">暂无页面</div>`;
    return;
  }
  list.innerHTML = pages.map((page) => {
    const result = pageResult(page.page_id);
    const status = result?.status || "neutral";
    return `
      <button class="page-item ${page.page_id === state.selectedPage ? "active" : ""}"
        data-page-id="${escapeHtml(page.page_id)}">
        <span class="page-dot ${escapeHtml(status)}"></span>
        <span>
          <span class="page-item-name">${escapeHtml(page.page_id)}</span>
          <span class="page-item-sub">${escapeHtml(page.policy)} · ${escapeHtml(page.charset_version)}</span>
        </span>
        <span class="page-item-lines">${page.line_count}</span>
      </button>
    `;
  }).join("");
  $$(".page-item").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedPage = button.dataset.pageId;
      state.expandedLines.clear();
      state.editorDirty = false;
      renderAll();
    });
  });
}

function renderStageStrip() {
  const names = ["像素预处理", "行检测排序", "模板匹配识别", "指标聚合"];
  const snapshotStages = state.data?.snapshots?.stages || {};
  const reportStages = state.data?.report?.stages || [];
  const strip = $("#stageStrip");
  strip.innerHTML = names.map((name, index) => {
    const stageNo = index + 1;
    const summary = reportStages.find((stage) => stage.stage === stageNo);
    const snapshot = snapshotStages[String(stageNo)];
    const status = summary?.status || (snapshot ? "ok" : "stale");
    const pageCount = summary?.page_count ?? snapshot?.page_count ?? 0;
    const stateLabel = status === "ok" ? "COMMITTED" : status === "degraded" ? "DEGRADED" : status.toUpperCase();
    return `
      <article class="stage-card ${escapeHtml(status)}">
        <div class="stage-top">
          <span class="stage-num">STAGE 0${stageNo}</span>
          <span class="stage-state ${escapeHtml(status)}">${escapeHtml(stateLabel)}</span>
        </div>
        <div class="stage-name">${escapeHtml(name)}</div>
        <div class="stage-sub">
          <span>${pageCount} pages</span>
          <span>${snapshot ? `v${snapshot.version}` : "no snapshot"}</span>
        </div>
      </article>
    `;
  }).join("");
}

function renderCanvas() {
  const page = selectedRecord();
  const image = $("#pageImage");
  const placeholder = $("#canvasPlaceholder");
  const overlay = $("#boxOverlay");
  if (!page) {
    image.hidden = true;
    overlay.hidden = true;
    placeholder.hidden = false;
    return;
  }
  $("#canvasTitle").textContent = page.page_id;
  $("#pageMeta").textContent =
    `${page.image_shape[1]}×${page.image_shape[0]} · seed ${page.image_seed} · ${page.line_count} 行`;
  const result = pageResult(page.page_id);
  if (image.dataset.pageId !== page.page_id) {
    image.dataset.pageId = page.page_id;
    image.src = `${page.image_url}?v=${encodeURIComponent(state.data?.workspace?.dataset_checksum || "")}`;
  }
  image.hidden = false;
  placeholder.hidden = true;
  overlay.hidden = false;
  const drawOverlay = () => {
    const imageRect = image.getBoundingClientRect();
    const frameRect = $("#canvasFrame").getBoundingClientRect();
    overlay.style.left = `${imageRect.left - frameRect.left}px`;
    overlay.style.top = `${imageRect.top - frameRect.top}px`;
    overlay.style.width = `${imageRect.width}px`;
    overlay.style.height = `${imageRect.height}px`;
    overlay.innerHTML = "";
    const lines = result?.lines?.length ? result.lines : page.lines;
    lines.forEach((line, index) => {
      const box = line.box || line.truth_box;
      if (!box) return;
      const [x0, y0, x1, y1] = box;
      const width = page.image_shape[1];
      const height = page.image_shape[0];
      const element = document.createElement("div");
      element.className = `box-outline ${line.fallback || result?.status === "degraded" ? "degraded" : "truth"}`;
      element.style.left = `${(x0 / width) * 100}%`;
      element.style.top = `${(y0 / height) * 100}%`;
      element.style.width = `${((x1 - x0) / width) * 100}%`;
      element.style.height = `${((y1 - y0) / height) * 100}%`;
      element.innerHTML = `<span class="box-label">${index + 1}</span>`;
      overlay.appendChild(element);
    });
  };
  if (image.complete) drawOverlay();
  image.onload = drawOverlay;
  window.addEventListener("resize", drawOverlay, { once: true });
}

function syncEditorFromSelected() {
  const page = selectedRecord();
  if (!page) {
    state.editorLines = [];
    renderEditor();
    return;
  }
  if (!state.editorDirty) {
    state.editorLines = cloneLines(page.lines);
  }
  renderEditor();
}

function renderEditor() {
  const body = $("#editorBody");
  if (!state.editorLines.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty-cell">当前页面没有真值行。</td></tr>`;
    return;
  }
  body.innerHTML = state.editorLines.map((line, index) => {
    const box = line.truth_box;
    return `
      <tr data-line-index="${index}">
        <td>${index + 1}</td>
        <td><input type="number" data-field="x0" value="${box[0]}" aria-label="x0"></td>
        <td><input type="number" data-field="y0" value="${box[1]}" aria-label="y0"></td>
        <td><input type="number" data-field="x1" value="${box[2]}" aria-label="x1"></td>
        <td><input type="number" data-field="y1" value="${box[3]}" aria-label="y1"></td>
        <td><input type="text" data-field="text" value="${escapeHtml(line.text)}" aria-label="真值字符串" spellcheck="false"></td>
        <td><button class="row-action" data-remove-line="${index}" title="删除行">×</button></td>
      </tr>
    `;
  }).join("");
  body.querySelectorAll("input").forEach((input) => {
    input.addEventListener("input", () => {
      state.editorDirty = true;
    });
  });
  body.querySelectorAll("[data-remove-line]").forEach((button) => {
    button.addEventListener("click", () => {
      collectEditor();
      state.editorLines.splice(Number(button.dataset.removeLine), 1);
      state.editorDirty = true;
      renderEditor();
    });
  });
}

function collectEditor() {
  const rows = $$("#editorBody tr[data-line-index]");
  if (!rows.length) return;
  state.editorLines = rows.map((row, index) => {
    const values = {};
    row.querySelectorAll("input").forEach((input) => {
      values[input.dataset.field] = input.value;
    });
    return {
      line_no: index,
      text: values.text || "",
      truth_box: [values.x0, values.y0, values.x1, values.y1].map((value) => Number(value)),
    };
  });
}

async function savePage() {
  collectEditor();
  if (!state.selectedPage) throw new Error("请先选择页面");
  if (state.editorLines.some((line) => !line.text)) {
    throw new Error("真值字符串不能为空");
  }
  const payload = await api(`/api/pages/${encodeURIComponent(state.selectedPage)}`, {
    method: "PUT",
    body: JSON.stringify({ lines: state.editorLines }),
  });
  state.editorDirty = false;
  showToast(`已保存 ${payload.page.page_id}，旧快照已整体失效。`);
  await refresh();
}

function renderResults() {
  const page = selectedRecord();
  const result = page ? pageResult(page.page_id) : null;
  const body = $("#resultBody");
  const summary = $("#resultSummary");
  const comparison = $("#comparisonStrip");
  if (!result) {
    body.innerHTML = `<tr><td colspan="7" class="empty-cell">该页面尚无识别结果，请运行四阶段流水。</td></tr>`;
    summary.textContent = state.data?.report ? "当前页面无结果" : "尚无运行结果";
    comparison.innerHTML = "";
    return;
  }
  const metrics = result.metrics || {};
  summary.innerHTML =
    `${metrics.line_count ?? 0} 行 · 平均置信度 <strong>${Number(metrics.mean_confidence || 0).toFixed(4)}</strong>` +
    ` · <span class="status-chip ${result.status === "ok" ? "ok" : "warn"}">${escapeHtml(result.status)}</span>`;
  const truthLines = page.lines;
  body.innerHTML = result.lines.map((line, index) => {
    const truth = truthLines[index]?.text || "—";
    const low = Number(line.confidence) < Number(metrics.threshold || 0.5);
    const expanded = state.expandedLines.has(index);
    const candidateRows = expanded ? `
      <tr class="candidate-row">
        <td colspan="7">
          <div class="candidate-grid">
            ${line.chars.map((char) => `
              <div class="char-evidence">
                <div class="char-evidence-label">#${char.index + 1} ${escapeHtml(char.char)} · ${Number(char.score).toFixed(4)}</div>
                <div class="candidate-list">
                  ${(char.candidates || []).map((candidate, candidateIndex) => `
                    <span class="candidate-pill ${candidateIndex === 0 ? "best" : ""}">
                      ${escapeHtml(candidate.char)} ${Number(candidate.score).toFixed(4)}
                      <span class="muted">${escapeHtml(candidate.template_level)}</span>
                    </span>
                  `).join("")}
                </div>
              </div>
            `).join("")}
          </div>
        </td>
      </tr>
    ` : "";
    return `
      <tr>
        <td>${index + 1}</td>
        <td class="box-code">${formatBox(line)}</td>
        <td class="truth">${escapeHtml(truth)}</td>
        <td class="recognized">${escapeHtml(line.text) || "<span class='muted'>∅</span>"}</td>
        <td>
          <div class="confidence ${low ? "low" : ""}">
            <div class="confidence-bar"><span style="width:${Math.max(2, Math.min(100, Number(line.confidence) * 100))}%"></span></div>
            <span class="confidence-value">${Number(line.confidence).toFixed(4)}</span>
          </div>
        </td>
        <td>
          ${line.fallback
            ? `<span class="code-chip">${escapeHtml(line.fallback)}</span>`
            : `<span class="small-chip">high</span>`}
        </td>
        <td><button class="expand-btn" data-expand-line="${index}" aria-label="展开候选">${expanded ? "−" : "+"}</button></td>
      </tr>
      ${candidateRows}
    `;
  }).join("");
  body.querySelectorAll("[data-expand-line]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.expandLine);
      if (state.expandedLines.has(index)) state.expandedLines.delete(index);
      else state.expandedLines.add(index);
      renderResults();
    });
  });
  const cmp = result.comparison || {};
  comparison.innerHTML = `
    <span>参考对照 <span class="status-chip ${cmp.ok ? "ok" : "fail"}">${cmp.ok ? "PASS" : "DIFF"}</span></span>
    <span>最大坐标偏差 <code>${Number(cmp.max_coordinate_deviation || 0).toFixed(4)}</code></span>
    <span>最大分数差 <code>${Number(cmp.max_score_delta || 0).toExponential(2)}</code></span>
    <span>检查行数 <code>${cmp.lines_checked || 0}</code></span>
    ${(cmp.character_differences || []).slice(0, 2).map((diff) => `
      <span class="code-chip">L${diff.line_no + 1}:C${diff.char_position + 1} ${escapeHtml(diff.reason)}</span>
    `).join("")}
  `;
}

function renderInjections() {
  const records = state.data?.injection_log || [];
  $("#injectionCount").textContent = records.length;
  const body = $("#injectionBody");
  if (!records.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty-cell">暂无注入。运行“注入演示”可观察四阶段业务码。</td></tr>`;
    return;
  }
  body.innerHTML = records.map((record) => `
    <tr>
      <td>${record.sequence}</td>
      <td><span class="small-chip">${escapeHtml(record.stage)}</span></td>
      <td><span class="code-chip">${escapeHtml(record.code)}</span></td>
      <td class="box-code">${escapeHtml(record.page_id || "—")} / ${record.line_no ?? "page"}</td>
    </tr>
  `).join("");
}

function renderDegradations() {
  const pages = state.data?.report?.pages || [];
  const items = pages.flatMap((page) =>
    (page.degradations || []).map((degradation) => ({ ...degradation, page_id: page.page_id }))
  );
  $("#degradationCount").textContent = items.length;
  const list = $("#degradationList");
  if (!items.length) {
    list.innerHTML = `<div class="empty-state">暂无降级。正常路径的四阶段产物已提交。</div>`;
    return;
  }
  list.innerHTML = items.map((item) => `
    <article class="chain-card">
      <div class="chain-head">
        <span class="small-chip">${escapeHtml(item.stage)}</span>
        <span class="chain-category">${escapeHtml(item.category)}</span>
        <span class="code-chip">${escapeHtml(item.trigger_code)}</span>
      </div>
      <div class="chain-meta">${escapeHtml(item.page_id)}${item.line_no != null ? ` · line ${item.line_no}` : ""}</div>
    </article>
  `).join("");
}

function renderSelfcheck() {
  const check = state.data?.selfcheck;
  const status = $("#selfcheckStatus");
  const body = $("#selfcheckBody");
  if (!check) {
    status.className = "status-chip neutral";
    status.textContent = "未运行";
    body.innerHTML = `<div class="empty-state">运行流水后执行跨阶段一致性校验。</div>`;
    return;
  }
  const ok = check.ok !== false;
  status.className = `status-chip ${ok ? "ok" : "fail"}`;
  status.textContent = ok ? "PASS" : "FAIL";
  const checks = check.checks || [];
  body.innerHTML = `
    ${checks.map((item) => `
      <div class="check-item ${ok ? "" : "fail"}"><span class="check-mark">${ok ? "✓" : "!"}</span>${escapeHtml(item)}</div>
    `).join("")}
    <div class="check-item"><span class="check-mark">✓</span>阶段页面集合：${Object.entries(check.stage_page_counts || {}).map(([stage, count]) => `S${stage}=${count}`).join(" · ")}</div>
  `;
}

function renderRecovery() {
  const recovery = state.data?.recovery_report;
  const status = $("#recoveryStatus");
  const body = $("#recoveryBody");
  if (!recovery) {
    status.className = "status-chip neutral";
    status.textContent = "未验证";
    body.innerHTML = `<div class="empty-state">点击“恢复演示”在第 2 阶段提交后模拟终止，再验证恢复报告逐字节一致。</div>`;
    return;
  }
  const ok = recovery.byte_identical;
  status.className = `status-chip ${ok ? "ok" : "fail"}`;
  status.textContent = ok ? "BYTE-IDENTICAL" : "MISMATCH";
  body.innerHTML = `
    <div class="recovery-kpis">
      <div class="kpi"><div class="kpi-label">RESUME FROM</div><div class="kpi-value">Stage ${recovery.resumed_from_stage}</div></div>
      <div class="kpi"><div class="kpi-label">REPORT BYTES</div><div class="kpi-value">${ok ? "一致" : "不一致"}</div></div>
      <div class="kpi"><div class="kpi-label">CRASH AFTER</div><div class="kpi-value">Stage ${recovery.simulated_crash_after_stage}</div></div>
      <div class="kpi"><div class="kpi-label">RECOVERED HASH</div><div class="kpi-value">${escapeHtml(shortHash(recovery.recovered_report_checksum, 10))}</div></div>
    </div>
    <div class="recovery-note">
      已提交页面不重算；未提交的第 3 阶段中间结果从阶段起点重建。
      重算集合与复用集合分离，最终报告与不间断运行逐字节一致。
    </div>
  `;
}

function renderFingerprints() {
  const report = state.data?.report;
  const config = state.data?.config;
  const template = state.data?.template_library;
  const snapshot = state.data?.snapshots;
  $("#reportChecksum").textContent = report?.report_checksum || "—";
  const codes = report?.business_codes || [];
  const grid = $("#fingerprintGrid");
  if (!report) {
    grid.innerHTML = `
      <div class="fingerprint"><div class="fingerprint-label">STATUS</div><div class="fingerprint-value">尚无运行指纹</div></div>
      <div class="fingerprint"><div class="fingerprint-label">TEMPLATE</div><div class="fingerprint-value">${escapeHtml(template?.version || "—")}</div></div>
      <div class="fingerprint"><div class="fingerprint-label">CONFIG</div><div class="fingerprint-value">${escapeHtml(config?.version || "—")}</div></div>
      <div class="fingerprint"><div class="fingerprint-label">SNAPSHOT MAX</div><div class="fingerprint-value">v${snapshot?.max_version || 0}</div></div>
    `;
    return;
  }
  grid.innerHTML = `
    <div class="fingerprint">
      <div class="fingerprint-label">REPORT CHECKSUM</div>
      <div class="fingerprint-value">${escapeHtml(report.report_checksum)}</div>
    </div>
    <div class="fingerprint">
      <div class="fingerprint-label">TEMPLATE · ${escapeHtml(template?.version || "—")}</div>
      <div class="fingerprint-value">${escapeHtml(template?.checksum || "—")}</div>
    </div>
    <div class="fingerprint">
      <div class="fingerprint-label">CONFIG · ${escapeHtml(config?.version || "—")}</div>
      <div class="fingerprint-value">${escapeHtml(config?.checksum || "—")}</div>
    </div>
    <div class="fingerprint">
      <div class="fingerprint-label">BUSINESS CODES</div>
      <div class="fingerprint-value">
        ${codes.length ? codes.map((code) => `<span class="code-chip">${escapeHtml(code)}</span>`).join(" ") : "正常路径 · 无业务错误"}
      </div>
    </div>
  `;
}

function renderLatestError() {
  const error = state.data?.latest_error;
  if (!error) return;
  $("#errorCode").textContent = error.code || "E_INTERNAL";
  $("#errorMessage").textContent = [error.message, error.stage, error.page_id, error.field].filter(Boolean).join(" · ");
  $("#errorBanner").classList.remove("hidden");
}

async function action(name) {
  const routes = {
    run: ["/api/run", { injections: [], reset: false }],
    injection: ["/api/demo/injection", {}],
    downgrade: ["/api/demo/downgrade", {}],
    recovery: ["/api/demo/recovery", {}],
    upgrade: ["/api/template-upgrade", { version: "glyph-v2" }],
    reset: ["/api/reset", {}],
  };
  const labels = {
    run: "正在运行四阶段流水",
    injection: "正在执行四阶段异常注入",
    downgrade: "正在执行降级链路演示",
    recovery: "正在模拟终止并恢复",
    upgrade: "正在整体升级模板库",
    reset: "正在重置快照并重跑",
  };
  if (!routes[name]) return;
  showBusy(labels[name]);
  try {
    const [route, body] = routes[name];
    const payload = await api(route, { method: "POST", body: JSON.stringify(body) });
    if (name === "recovery") {
      showToast(payload.recovery.byte_identical ? "恢复报告与不间断运行逐字节一致。" : "恢复报告不一致！");
    } else if (name === "upgrade") {
      showToast("模板库已整体升级；仅受影响页面重算识别与聚合。");
    } else if (name === "injection") {
      showToast("注入序列完成，已在证据台记录阶段、页面、行号与降级链。");
    } else {
      showToast("四阶段流水已完成，快照已提交。");
    }
    state.editorDirty = false;
    await refresh();
  } catch (error) {
    showError(error);
    showToast(error.message, "error");
    await refresh().catch(() => {});
  } finally {
    hideBusy();
  }
}

$$("[data-action]").forEach((button) => {
  button.addEventListener("click", async (event) => {
    const name = button.dataset.action;
    if (name === "menu") {
      const menu = $("#moreMenu");
      const open = menu.classList.toggle("open");
      button.setAttribute("aria-expanded", String(open));
      return;
    }
    if (name === "dismiss-error") {
      $("#errorBanner").classList.add("hidden");
      return;
    }
    if (name === "add-line") {
      collectEditor();
      const page = selectedRecord();
      const height = page?.image_shape?.[0] || 288;
      const width = page?.image_shape?.[1] || 360;
      state.editorLines.push({
        line_no: state.editorLines.length,
        text: "OCR",
        truth_box: [24, Math.min(height - 18, 20 + state.editorLines.length * 34), Math.min(width - 1, 90), Math.min(height - 1, 36 + state.editorLines.length * 34)],
      });
      state.editorDirty = true;
      renderEditor();
      return;
    }
    if (name === "save-page") {
      showBusy("正在保存页面与重绘画布");
      try {
        await savePage();
      } catch (error) {
        showError(error);
        showToast(error.message, "error");
      } finally {
        hideBusy();
      }
      return;
    }
    await action(name);
  });
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".menu-wrap")) {
    $("#moreMenu").classList.remove("open");
  }
});

refresh().catch((error) => {
  showError(error);
  showToast("无法载入工作区状态。", "error");
});

