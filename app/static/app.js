const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const PHASE_LABELS = {
  baseline: "基础期",
  build: "建设期",
  deload: "减量期",
  peak: "巅峰期",
  recovery: "恢复期",
};

const ACTION_LABELS = {
  sprint_run: "短跑",
  jump: "跳跃",
  squat: "深蹲",
  lunge: "弓步",
  deadlift: "硬拉",
  clean_jerk: "抓举挺举",
  medicine_ball_throw: "药球投掷",
  gymnastics_tumble: "体操翻滚",
  swim_pull: "游泳划臂",
  rowing_drive: "赛艇拉桨",
  balance_hold: "平衡控制",
  agility_cut: "变向切入",
};

const ACTION_CODES = {
  sprint_run: "SP",
  jump: "JP",
  squat: "SQ",
  lunge: "LN",
  deadlift: "DL",
  clean_jerk: "CJ",
  medicine_ball_throw: "MB",
  gymnastics_tumble: "GY",
  swim_pull: "SW",
  rowing_drive: "RW",
  balance_hold: "BH",
  agility_cut: "AG",
};

const BODY_PART_LABELS = {
  ankle: "踝",
  knee: "膝",
  hip: "髋",
  lower_back: "下背",
  shoulder: "肩",
  elbow: "肘",
  wrist: "腕",
  neck: "颈",
};

const state = {
  meta: null,
  athletes: [],
  requests: [],
  verification: null,
  consistency: null,
  currentId: null,
  detail: null,
  selectedClipId: null,
  activeTab: "ranking",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function number(value, digits = 2) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
}

function percent(value, digits = 1) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : "—";
}

function clamp(value, low = 0, high = 100) {
  return Math.max(low, Math.min(high, Number(value) || 0));
}

function phaseLabel(phase) {
  return PHASE_LABELS[phase] || phase || "未标注";
}

function phaseBadge(phase) {
  return `<span class="phase-badge phase-${escapeHtml(phase)}">${escapeHtml(phaseLabel(phase))}</span>`;
}

function athleteById(athleteId) {
  return state.athletes.find((athlete) => athlete.athlete_id === athleteId);
}

function requestByName(requestId) {
  return state.requests.find((request) => request.request_id === requestId);
}

function showToast(message, kind = "") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show ${kind}`.trim();
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.className = "toast";
  }, 4200);
}

function setLoading(visible, text = "正在重新计算") {
  const overlay = $("#loadingOverlay");
  overlay.hidden = !visible;
  const label = $("span", overlay);
  if (label) label.textContent = text;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = payload?.error || {};
    const problem = new Error(error.message || `请求失败 (${response.status})`);
    problem.code = error.code || "HTTP_ERROR";
    problem.details = error.details;
    throw problem;
  }
  return payload;
}

function updateGlobalStatus() {
  const pill = $("#globalStatus");
  const report = state.verification;
  if (!report) {
    pill.className = "status-pill loading";
    pill.innerHTML = `<span class="status-dot"></span><span>等待核对</span>`;
    return;
  }
  if (report.passed) {
    pill.className = "status-pill passed";
    pill.innerHTML = `<span class="status-dot"></span><span>独立参考核对通过 · ${report.request_count}/${report.request_count}</span>`;
    pill.title = "名次、三路得分、得分分解、乱序不变性与同分落位均通过";
  } else {
    pill.className = "status-pill failed";
    pill.innerHTML = `<span class="status-dot"></span><span>核对存在偏差 · 请查看验证报告</span>`;
    pill.title = report.first_rank_deviation
      ? `首个偏差请求：${report.first_rank_deviation.request_id}`
      : "验证未通过";
  }
}

async function refreshRequests() {
  const payload = await api("/api/requests");
  state.requests = payload.items;
  renderRequestFilters();
  renderRequestList();
}

async function refreshConsistency() {
  try {
    state.consistency = await api("/api/consistency");
  } catch {
    state.consistency = null;
  }
  if (state.detail) renderConsistency();
}

function renderRequestFilters() {
  const athleteSelect = $("#athleteFilter");
  const previousAthlete = athleteSelect.value;
  athleteSelect.innerHTML = `<option value="">全部运动员</option>${state.athletes
    .map(
      (athlete) =>
        `<option value="${escapeHtml(athlete.athlete_id)}">${escapeHtml(athlete.name)} · ${escapeHtml(athlete.athlete_id)}</option>`
    )
    .join("")}`;
  athleteSelect.value = previousAthlete;

  const phaseSelect = $("#phaseFilter");
  const previousPhase = phaseSelect.value;
  phaseSelect.innerHTML = `<option value="">全部阶段</option>${state.meta.phases
    .map(
      (phase) =>
        `<option value="${escapeHtml(phase)}">${escapeHtml(phaseLabel(phase))}</option>`
    )
    .join("")}`;
  phaseSelect.value = previousPhase;
}

function filteredRequests() {
  const query = $("#searchInput").value.trim().toLowerCase();
  const athlete = $("#athleteFilter").value;
  const phase = $("#phaseFilter").value;
  return state.requests.filter((request) => {
    if (athlete && request.athlete_id !== athlete) return false;
    if (phase && request.phase !== phase) return false;
    if (!query) return true;
    const profile = athleteById(request.athlete_id);
    return `${request.request_id} ${request.athlete_id} ${profile?.name || ""}`
      .toLowerCase()
      .includes(query);
  });
}

function renderRequestList() {
  const list = $("#requestList");
  const items = filteredRequests();
  $("#requestCount").textContent = String(items.length);
  if (!items.length) {
    list.innerHTML = `<div class="inline-empty">没有匹配的排序请求</div>`;
    return;
  }
  list.innerHTML = items
    .map((request) => {
      const athlete = athleteById(request.athlete_id);
      const summary = request.result_summary;
      const focusCount = summary?.focus_review_count ?? 0;
      const count = request.candidate_ids.length;
      const weights = summary?.weights || request.weights;
      const active = request.request_id === state.currentId ? " active" : "";
      return `
        <button class="request-item${active}" type="button" data-request-id="${escapeHtml(request.request_id)}">
          <div class="request-item-top">
            <span class="request-athlete">${escapeHtml(athlete?.name || request.athlete_id)}</span>
            ${phaseBadge(request.phase)}
          </div>
          <div class="request-code">${escapeHtml(request.request_id)}</div>
          <div class="request-item-meta">
            <span class="mini-badge">${count} 候选</span>
            <span class="mini-badge risk">Q ${Math.round((weights?.quality || 0) * 100)} / R ${Math.round((weights?.risk || 0) * 100)} / P ${Math.round((weights?.profile || 0) * 100)}</span>
            <span class="focus-mini">回看 ${focusCount}</span>
          </div>
        </button>`;
    })
    .join("");
  $$(".request-item", list).forEach((button) => {
    button.addEventListener("click", () => loadRequest(button.dataset.requestId));
  });
}

async function loadRequest(requestId) {
  if (!requestId) return;
  state.currentId = requestId;
  renderRequestList();
  setLoading(true, "正在读取排序请求");
  try {
    let detail = await api(`/api/requests/${encodeURIComponent(requestId)}`);
    if (!detail.result) {
      await api(`/api/requests/${encodeURIComponent(requestId)}/rank`, {
        method: "POST",
        body: "{}",
      });
      detail = await api(`/api/requests/${encodeURIComponent(requestId)}`);
    }
    state.detail = detail;
    state.selectedClipId = detail.result.ranking[0]?.clip_id || null;
    $("#emptyState").hidden = true;
    $("#workspace").hidden = false;
    renderWorkspace();
  } catch (error) {
    showToast(`${error.code || "ERROR"} · ${error.message}`, "error");
  } finally {
    setLoading(false);
  }
}

function currentResult() {
  return state.detail?.result || null;
}

function renderWorkspace() {
  renderHero();
  renderWeightControls();
  renderKpis();
  renderRanking();
  renderBreakdown();
  renderFocus();
  renderConsistency();
  activateTab(state.activeTab);
}

function renderHero() {
  const detail = state.detail;
  const athlete = athleteById(detail.athlete_id) || detail.profile_snapshot;
  const result = currentResult();
  const phase = result?.phase || detail.phase;
  const name = athlete.name || detail.athlete_id;
  $("#athleteAvatar").textContent = [...name][0] || "A";
  $("#athleteName").textContent = name;
  $("#requestIdText").textContent = `${detail.request_id} · ${detail.candidate_ids.length} 个候选片段`;
  $("#athleteMeta").textContent = `${athlete.specialty} · 训练 ${athlete.training_years} 年 · 伤病标签 ${athlete.injury_tags?.length || 0} 条`;
  $("#injuryTags").innerHTML = (athlete.injury_tags || [])
    .map((tag) => `<span class="injury-tag">${escapeHtml(tag)}</span>`)
    .join("");
  const badge = $("#requestPhaseBadge");
  badge.textContent = phaseLabel(phase);
  badge.className = `phase-badge phase-${phase}`;

  const select = $("#phaseSelect");
  select.innerHTML = state.meta.phases
    .map(
      (item) =>
        `<option value="${escapeHtml(item)}">${escapeHtml(phaseLabel(item))}</option>`
    )
    .join("");
  select.value = phase;
}

function currentWeights() {
  return {
    quality: Number($("#qualityWeight").value) / 100,
    risk: Number($("#riskWeight").value) / 100,
    profile: Number($("#profileWeight").value) / 100,
  };
}

function normalizedWeights() {
  const raw = currentWeights();
  const total = raw.quality + raw.risk + raw.profile;
  if (total <= 0) throw new Error("三项权重不能同时为 0");
  const quality = Math.round((raw.quality / total) * 1_000_000) / 1_000_000;
  const risk = Math.round((raw.risk / total) * 1_000_000) / 1_000_000;
  const profile = Math.round((1 - quality - risk) * 1_000_000) / 1_000_000;
  return { quality, risk, profile };
}

function renderWeightControls() {
  const result = currentResult();
  const weights = result?.weights || state.detail.weights;
  $("#qualityWeight").value = String(Math.round(weights.quality * 100));
  $("#riskWeight").value = String(Math.round(weights.risk * 100));
  $("#profileWeight").value = String(Math.round(weights.profile * 100));
  updateWeightDisplay();
  $("#weightHint").textContent = `${phaseLabel(result?.phase || state.detail.phase)}请求配置中的声明权重`;
  $("#weightHint").className = "weight-hint";
}

function updateWeightDisplay() {
  const weights = currentWeights();
  const total = weights.quality + weights.risk + weights.profile;
  $("#qualityWeightValue").textContent = `${Math.round(weights.quality * 100)}%`;
  $("#riskWeightValue").textContent = `${Math.round(weights.risk * 100)}%`;
  $("#profileWeightValue").textContent = `${Math.round(weights.profile * 100)}%`;
  $("#weightSum").textContent = `${Math.round(total * 100)}%`;
  const hint = $("#weightHint");
  if (Math.abs(total - 1) > 0.0001) {
    hint.textContent = `保存时将按 ${Math.round(total * 100)}% 归一化`;
    hint.className = "weight-hint changed";
  } else {
    hint.textContent = "权重和为 100%，可保存重排";
    hint.className = "weight-hint";
  }
}

function renderKpis() {
  const result = currentResult();
  const consistency = state.consistency?.request_details?.find(
    (item) => item.request_id === result.request_id
  );
  $("#kpiCandidates").textContent = String(result.candidate_count);
  $("#kpiUplift").textContent = String(result.injury_uplift_candidates.length);
  $("#kpiFocus").textContent = String(result.focus_review_count);
  $("#kpiAgreement").textContent = consistency
    ? percent(consistency.pairwise_agreement)
    : "—";
}

function movementHtml(item) {
  if (item.rank_change > 0) {
    return `<span class="movement up">↑ ${item.rank_change}</span>`;
  }
  if (item.rank_change < 0) {
    return `<span class="movement down">↓ ${Math.abs(item.rank_change)}</span>`;
  }
  return `<span class="movement same">—</span>`;
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action;
}

function actionCode(action) {
  return ACTION_CODES[action] || String(action || "??").slice(0, 2).toUpperCase();
}

function renderRanking() {
  const result = currentResult();
  const body = $("#rankingBody");
  body.innerHTML = result.ranking
    .map((item) => {
      const uplift = item.injury_risk_uplift.length > 0;
      const selected = item.clip_id === state.selectedClipId ? " selected" : "";
      return `
        <tr class="${selected}" data-clip-id="${escapeHtml(item.clip_id)}">
          <td><span class="rank-number">${item.rank}</span></td>
          <td>${movementHtml(item)}</td>
          <td>
            <div class="clip-cell">
              <span class="action-glyph">${escapeHtml(actionCode(item.action_category))}</span>
              <div class="clip-copy">
                <div class="clip-name">${escapeHtml(item.clip_id)}</div>
                <div class="clip-sub">
                  ${escapeHtml(actionLabel(item.action_category))} · ${item.repetition_count} 次 · ${number(item.duration_sec, 1)}s
                  ${uplift ? `<span class="risk-badge">伤病 +${number(item.score_breakdown.load_risk.injury_adjustment, 1)}</span>` : ""}
                </div>
              </div>
            </div>
          </td>
          <td class="numeric"><span class="metric quality">${number(item.scores.quality)}</span></td>
          <td class="numeric"><span class="metric risk">${number(item.scores.load_risk)}</span></td>
          <td class="numeric"><span class="metric profile">${number(item.scores.profile_match)}</span></td>
          <td class="numeric fused-cell">
            <strong>${number(item.scores.fused, 3)}</strong>
            <span style="--score:${clamp(item.scores.fused)}%"></span>
          </td>
          <td><button class="row-action" type="button" title="查看得分分解">›</button></td>
        </tr>`;
    })
    .join("");
  $$("#rankingBody tr").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedClipId = row.dataset.clipId;
      renderRanking();
      renderBreakdown();
      activateTab("breakdown");
    });
  });
}

function selectedCandidate() {
  const result = currentResult();
  return (
    result.ranking.find((item) => item.clip_id === state.selectedClipId) ||
    result.ranking[0]
  );
}

function renderBreakdown() {
  const container = $("#breakdownContent");
  const item = selectedCandidate();
  if (!item) {
    container.innerHTML = `<div class="breakdown-empty">该请求暂无候选片段</div>`;
    return;
  }
  state.selectedClipId = item.clip_id;
  const breakdown = item.score_breakdown;
  const riskFeatures = breakdown.load_risk.features;
  const allFeatures = [
    ...breakdown.quality.features.map((feature) => ({ ...feature, route: "quality" })),
    ...riskFeatures.map((feature) => ({ ...feature, route: "risk" })),
    ...breakdown.profile_match.features.map((feature) => ({ ...feature, route: "profile" })),
  ];
  const adjustments = breakdown.load_risk.injury_adjustments;
  const alignment = item.alignment;
  const statsRows = Object.entries(item.statistics)
    .map(
      ([channel, stats]) => `
        <tr>
          <td>${escapeHtml(channel)}</td>
          <td>${number(stats.mean)}</td>
          <td>${number(stats.variance)}</td>
          <td>${number(stats.peak_factor)}</td>
          <td>${percent(stats.zero_crossing_rate, 0)}</td>
          <td>${percent(stats.rhythm_stability, 0)}</td>
        </tr>`
    )
    .join("");

  container.innerHTML = `
    <div class="breakdown-head">
      <div class="breakdown-title">
        <h3>${escapeHtml(item.clip_id)}</h3>
        <p>${escapeHtml(actionLabel(item.action_category))} · 粗排名次 ${item.prior_rank} → 精排名次 ${item.rank} · ${escapeHtml(item.movement === "up" ? "名次上升" : item.movement === "down" ? "名次下滑" : "名次不变")}</p>
      </div>
      <div class="fused-hero">
        <span>融合精排得分</span>
        <strong>${number(item.scores.fused, 3)}</strong>
      </div>
    </div>
    <div class="route-grid">
      <article class="route-card quality">
        <div class="route-card-head"><span>动作质量</span><strong>${number(item.scores.quality)}</strong></div>
        <div class="route-bar" style="--width:${clamp(item.scores.quality)}%"></div>
        <div class="route-foot"><span>融合贡献</span><b>${number(breakdown.quality.contribution)}</b></div>
      </article>
      <article class="route-card risk">
        <div class="route-card-head"><span>负荷风险</span><strong>${number(item.scores.load_risk)}</strong></div>
        <div class="route-bar" style="--width:${clamp(item.scores.load_risk)}%"></div>
        <div class="route-foot"><span>风险控制分贡献</span><b>${number(breakdown.load_risk.contribution)}</b></div>
      </article>
      <article class="route-card profile">
        <div class="route-card-head"><span>档案匹配</span><strong>${number(item.scores.profile_match)}</strong></div>
        <div class="route-bar" style="--width:${clamp(item.scores.profile_match)}%"></div>
        <div class="route-foot"><span>融合贡献</span><b>${number(breakdown.profile_match.contribution)}</b></div>
      </article>
    </div>
    ${
      adjustments.length
        ? adjustments
            .map(
              (adjustment) => `
                <div class="risk-callout">
                  <div class="risk-callout-head">
                    <span>伤病标签触发负荷风险上调 · ${escapeHtml(BODY_PART_LABELS[adjustment.body_part] || adjustment.body_part)}</span>
                    <strong>+${number(adjustment.adjustment_delta)}</strong>
                  </div>
                  <p>
                    命中标签 <strong>${adjustment.matched_tags.map(escapeHtml).join("、")}</strong>；
                    动作类别 <strong>${escapeHtml(actionLabel(adjustment.action_category))}</strong>；
                    峰值负荷 <strong>${number(adjustment.peak_load)}</strong>；
                    同一伤病部位只计一次，风险 ${number(adjustment.before_risk)} → <strong>${number(adjustment.after_risk)}</strong>。
                  </p>
                </div>`
            )
            .join("")
        : `<div class="risk-callout"><div class="risk-callout-head"><span>未触发伤病负荷上调</span><strong>0.0</strong></div><p>该片段未同时满足旧伤部位、动作应力类别与峰值负荷门槛。</p></div>`
    }
    <div class="detail-grid">
      <section class="detail-card">
        <h4>特征贡献明细</h4>
        <div class="feature-list">
          ${allFeatures
            .map(
              (feature) => `
                <div class="feature-row">
                  <span class="feature-label">${feature.route === "quality" ? "质量" : feature.route === "risk" ? "风险" : "档案"} · ${escapeHtml(feature.label)}</span>
                  <span class="feature-value">${number(feature.component)} → ${number(feature.contribution)}</span>
                  <span class="feature-track ${feature.route}" style="--width:${clamp(feature.component)}%"></span>
                </div>`
            )
            .join("")}
        </div>
      </section>
      <section class="detail-card">
        <h4>档案与标签数值对齐</h4>
        <div class="chips">
          <span class="chip">action_code ${alignment.action_code}</span>
          <span class="chip">specialty_code ${alignment.specialty_code}</span>
          <span class="chip">injury_codes [${alignment.injury_body_codes.join(", ")}]</span>
          <span class="chip">vector ${alignment.combined_vector.reduce((sum, value) => sum + value, 0)} / ${alignment.combined_vector.length}</span>
          ${alignment.injury_body_parts
            .map(
              (part) =>
                `<span class="chip orange">${escapeHtml(BODY_PART_LABELS[part] || part)}</span>`
            )
            .join("")}
        </div>
        <p class="definition-note">标签排序后映射到同一部位向量；签名 ${escapeHtml(alignment.signature.slice(0, 12))}…</p>
        <h4 style="margin-top:13px">传感器统一统计</h4>
        <table class="stats-table">
          <thead><tr><th>通道</th><th>均值</th><th>方差</th><th>峰值因子</th><th>过零率</th><th>节奏稳定</th></tr></thead>
          <tbody>${statsRows}</tbody>
        </table>
      </section>
    </div>`;
}

function renderFocus() {
  const result = currentResult();
  const container = $("#focusContent");
  const dropCount = result.rank_drop_candidates.length;
  const upliftCount = result.injury_uplift_candidates.length;
  const focus = result.focus_review;
  container.innerHTML = `
    <div class="focus-summary">
      <div class="focus-summary-card"><span>伤病风险上调</span><strong>${upliftCount}</strong></div>
      <div class="focus-summary-card"><span>下滑 ≥ ${result.rank_drop_threshold} 位</span><strong>${dropCount}</strong></div>
      <div class="focus-summary-card"><span>重点回看片段</span><strong>${focus.length}</strong></div>
    </div>
    ${
      focus.length
        ? `<div class="focus-list">${focus
            .map((entry) => {
              const isRisk = entry.injury_adjustments.length > 0;
              const adjustmentText = entry.injury_adjustments
                .map(
                  (adjustment) =>
                    `${adjustment.matched_tags.join("、")} · ${actionLabel(adjustment.action_category)} · 峰值 ${number(adjustment.peak_load, 1)} · +${number(adjustment.adjustment_delta, 1)}`
                )
                .join("；");
              return `
                <article class="focus-item ${isRisk ? "risk" : ""}">
                  <span class="focus-rank">${entry.rank}</span>
                  <div class="focus-copy">
                    <h4>${escapeHtml(entry.clip_id)}</h4>
                    <p>${escapeHtml(actionLabel(entry.action_category))} · 粗排 ${entry.prior_rank} → 精排 ${entry.rank}</p>
                    <div>
                      ${entry.reasons.map((reason) => `<span class="focus-chip ${isRisk ? "risk" : ""}">${escapeHtml(reason)}</span>`).join("")}
                    </div>
                    ${adjustmentText ? `<p style="margin-top:7px;color:#ffc39a">${escapeHtml(adjustmentText)}</p>` : ""}
                  </div>
                  <span class="focus-score">${number(entry.fused_score, 3)}</span>
                </article>`;
            })
            .join("")}</div>`
        : `<div class="inline-empty">当前请求没有需要重点回看的片段</div>`
    }
    <p class="definition-note">清单至少覆盖全部伤病风险上调片段，以及精排名次相对粗排下滑达到 ${result.rank_drop_threshold} 位的片段。</p>`;
}

function renderConsistency() {
  const container = $("#consistencyContent");
  const report = state.consistency;
  if (!report) {
    container.innerHTML = `<div class="inline-empty">一致性报告尚未生成</div>`;
    return;
  }
  const overall = report.overall;
  const current = report.request_details.find(
    (item) => item.request_id === state.currentId
  );
  const groups = report.groups || [];
  container.innerHTML = `
    <div class="consistency-hero">
      <div class="consistency-metric"><span>成对比较一致率</span><strong>${percent(overall.pairwise_agreement)}</strong><small>正式精排 vs 独立参考</small></div>
      <div class="consistency-metric"><span>关键片段命中率</span><strong>${percent(overall.key_hit_rate)}</strong><small>Top ${report.top_k} 交集</small></div>
      <div class="consistency-metric"><span>名次扰动幅度</span><strong>${number(overall.rank_disturbance, 3)}</strong><small>平均绝对名次差</small></div>
      <div class="consistency-metric"><span>相对粗排平均变化</span><strong>${number(overall.mean_abs_rank_change_vs_prior, 2)}</strong><small>${overall.candidate_count} 个候选片段</small></div>
    </div>
    ${
      current
        ? `<div class="risk-callout" style="border-color:rgba(93,215,219,.18);background:rgba(93,215,219,.05)">
            <div class="risk-callout-head" style="color:#b9f1f2"><span>当前请求一致性</span><span>${current.consistent ? "已通过" : "存在偏差"}</span></div>
            <p>成对一致率 <strong>${percent(current.pairwise_agreement)}</strong> · Top ${report.top_k} 命中 <strong>${percent(current.key_hit_rate)}</strong> · 名次扰动 <strong>${number(current.rank_disturbance, 3)}</strong> · 重点清单覆盖 <strong>${percent(current.focus_hit_rate)}</strong></p>
          </div>`
        : ""
    }
    <div class="table-scroll">
      <table class="consistency-table">
        <thead>
          <tr>
            <th>运动员 / 阶段</th>
            <th>请求</th>
            <th>候选</th>
            <th>成对一致率</th>
            <th>Top ${report.top_k} 命中</th>
            <th>名次扰动</th>
            <th>粗排变化</th>
            <th>一致请求</th>
          </tr>
        </thead>
        <tbody>
          ${groups
            .map((group) => {
              const athlete = athleteById(group.athlete_id);
              return `
                <tr>
                  <td>${escapeHtml(athlete?.name || group.athlete_id)} <span class="muted">/ ${escapeHtml(phaseLabel(group.phase))}</span></td>
                  <td class="mono">${group.request_count}</td>
                  <td class="mono">${group.candidate_count}</td>
                  <td><span class="agreement-pill">${percent(group.pairwise_agreement)}</span></td>
                  <td class="mono">${percent(group.key_hit_rate)}</td>
                  <td class="mono">${number(group.rank_disturbance, 3)}</td>
                  <td class="mono">${number(group.mean_abs_rank_change_vs_prior, 2)}</td>
                  <td class="mono">${group.consistent_request_count}/${group.request_count}</td>
                </tr>`;
            })
            .join("")}
        </tbody>
      </table>
    </div>
    <p class="definition-note">按运动员与训练周期阶段分组；成对比较一致率、关键片段命中率与名次扰动均以独立精排参考为真值。整体样本：${overall.request_count} 请求 / ${overall.candidate_count} 候选。</p>`;
}

function activateTab(name) {
  state.activeTab = name;
  $$(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  $$(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${name}`);
  });
  if (name === "breakdown") renderBreakdown();
  if (name === "consistency") renderConsistency();
}

async function saveWeights(mode) {
  if (!state.detail) return;
  let weights;
  try {
    weights = normalizedWeights();
  } catch (error) {
    showToast(error.message, "error");
    return;
  }
  const phase = $("#phaseSelect").value;
  setLoading(true, mode === "save" ? "正在保存配置并重排" : "正在按当前权重重排");
  try {
    let detail;
    if (mode === "save") {
      detail = await api(`/api/requests/${encodeURIComponent(state.currentId)}`, {
        method: "PATCH",
        body: JSON.stringify({ phase, weights }),
      });
    } else {
      await api(
        `/api/requests/${encodeURIComponent(state.currentId)}/rank`,
        {
          method: "POST",
          body: JSON.stringify({ phase, weights }),
        }
      );
      detail = await api(`/api/requests/${encodeURIComponent(state.currentId)}`);
    }
    state.detail = detail;
    state.selectedClipId = detail.result.ranking[0]?.clip_id || null;
    renderWorkspace();
    await refreshRequests();
    showToast(
      `已按 Q ${Math.round(weights.quality * 100)} / R ${Math.round(weights.risk * 100)} / P ${Math.round(weights.profile * 100)} 完成重排`,
      "success"
    );
  } catch (error) {
    showToast(`${error.code || "ERROR"} · ${error.message}`, "error");
  } finally {
    setLoading(false);
  }
}

async function runVerification() {
  const button = $("#verifyBtn");
  button.disabled = true;
  setLoading(true, "正在重算独立参考并核对全部请求");
  try {
    state.verification = await api("/api/verify", { method: "POST" });
    updateGlobalStatus();
    await refreshConsistency();
    await refreshRequests();
    if (state.currentId) {
      await loadRequest(state.currentId);
    }
    showToast(
      state.verification.passed
        ? `核对通过：${state.verification.request_count} 个请求，0 个得分偏差`
        : "核对未通过，请查看 artifacts/verification.json",
      state.verification.passed ? "success" : "error"
    );
  } catch (error) {
    showToast(`${error.code || "ERROR"} · ${error.message}`, "error");
  } finally {
    button.disabled = false;
    setLoading(false);
  }
}

function closeCreateModal() {
  $("#createModal").hidden = true;
  $("#createError").hidden = true;
}

function updateCreateCandidateCount() {
  const count = $$("#createCandidates input:checked").length;
  $("#createCandidateCount").textContent = String(count);
}

async function loadCreateCandidates() {
  const athleteId = $("#createAthlete").value;
  const container = $("#createCandidates");
  if (!athleteId) {
    container.innerHTML = `<div class="inline-empty">请先选择运动员</div>`;
    return;
  }
  container.innerHTML = `<div class="inline-empty">正在读取候选片段…</div>`;
  try {
    const payload = await api(
      `/api/athletes/${encodeURIComponent(athleteId)}/clips`
    );
    container.innerHTML = payload.items
      .map(
        (clip, index) => `
          <label class="candidate-option">
            <input type="checkbox" value="${escapeHtml(clip.clip_id)}" ${index < 12 ? "checked" : ""}>
            <span class="candidate-option-copy">
              <strong>${escapeHtml(clip.clip_id)}</strong>
              <small>${escapeHtml(actionLabel(clip.action_category))} · Q ${number(clip.manual_quality_score, 1)} · ${number(clip.duration_sec, 1)}s</small>
            </span>
          </label>`
      )
      .join("");
    updateCreateCandidateCount();
  } catch (error) {
    container.innerHTML = `<div class="inline-empty">${escapeHtml(error.code || "ERROR")} · ${escapeHtml(error.message)}</div>`;
  }
}

async function openCreateModal() {
  const athleteSelect = $("#createAthlete");
  const currentAthlete = state.detail?.athlete_id;
  athleteSelect.innerHTML = state.athletes
    .map(
      (athlete) =>
        `<option value="${escapeHtml(athlete.athlete_id)}">${escapeHtml(athlete.name)} · ${escapeHtml(athlete.specialty)}</option>`
    )
    .join("");
  if (currentAthlete) athleteSelect.value = currentAthlete;

  const phaseSelect = $("#createPhase");
  phaseSelect.innerHTML = state.meta.phases
    .map(
      (phase) =>
        `<option value="${escapeHtml(phase)}">${escapeHtml(phaseLabel(phase))}</option>`
    )
    .join("");
  $("#createModal").hidden = false;
  $("#createError").hidden = true;
  await loadCreateCandidates();
}

async function submitCreateRequest(event) {
  event.preventDefault();
  const selected = $$("#createCandidates input:checked").map(
    (input) => input.value
  );
  const errorBox = $("#createError");
  if (!selected.length) {
    errorBox.textContent = "请至少选择一个候选片段";
    errorBox.hidden = false;
    return;
  }
  const button = $("#createSubmitBtn");
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const detail = await api("/api/requests", {
      method: "POST",
      body: JSON.stringify({
        athlete_id: $("#createAthlete").value,
        phase: $("#createPhase").value,
        candidate_clip_ids: selected,
      }),
    });
    closeCreateModal();
    await refreshRequests();
    await loadRequest(detail.request_id);
    showToast(`已提交请求 ${detail.request_id} 并完成精排`, "success");
  } catch (error) {
    errorBox.textContent = `${error.code || "ERROR"} · ${error.message}`;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  $("#searchInput").addEventListener("input", renderRequestList);
  $("#athleteFilter").addEventListener("change", renderRequestList);
  $("#phaseFilter").addEventListener("change", renderRequestList);
  $("#qualityWeight").addEventListener("input", updateWeightDisplay);
  $("#riskWeight").addEventListener("input", updateWeightDisplay);
  $("#profileWeight").addEventListener("input", updateWeightDisplay);
  $("#phaseSelect").addEventListener("change", () => {
    const defaults = state.meta.phase_weights[$("#phaseSelect").value];
    $("#qualityWeight").value = String(Math.round(defaults.quality * 100));
    $("#riskWeight").value = String(Math.round(defaults.risk * 100));
    $("#profileWeight").value = String(Math.round(defaults.profile * 100));
    updateWeightDisplay();
    $("#weightHint").textContent = `${phaseLabel($("#phaseSelect").value)}默认权重，保存后生效`;
    $("#weightHint").className = "weight-hint changed";
  });
  $("#saveConfigBtn").addEventListener("click", () => saveWeights("save"));
  $("#rerankBtn").addEventListener("click", () => saveWeights("rank"));
  $("#verifyBtn").addEventListener("click", runVerification);
  $("#newRequestBtn").addEventListener("click", openCreateModal);
  $("#createAthlete").addEventListener("change", loadCreateCandidates);
  $("#createCandidates").addEventListener("change", updateCreateCandidateCount);
  $("#createForm").addEventListener("submit", submitCreateRequest);
  $$("[data-modal-close]").forEach((element) => {
    element.addEventListener("click", closeCreateModal);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#createModal").hidden) {
      closeCreateModal();
    }
  });
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  });
}

async function init() {
  bindEvents();
  try {
    const [meta, athletesPayload, requestsPayload, verification, consistency] =
      await Promise.all([
        api("/api/meta"),
        api("/api/athletes"),
        api("/api/requests"),
        api("/api/verification"),
        api("/api/consistency"),
      ]);
    state.meta = meta;
    state.athletes = athletesPayload.items;
    state.requests = requestsPayload.items;
    state.verification = verification;
    state.consistency = consistency;
    updateGlobalStatus();
    renderRequestFilters();
    renderRequestList();
    if (state.requests.length) {
      await loadRequest(state.requests[0].request_id);
    }
  } catch (error) {
    updateGlobalStatus();
    showToast(`${error.code || "ERROR"} · ${error.message}`, "error");
    $("#emptyState").innerHTML = `
      <div class="empty-icon">!</div>
      <h2>页面载入失败</h2>
      <p>${escapeHtml(error.code || "ERROR")} · ${escapeHtml(error.message)}</p>`;
  }
}

document.addEventListener("DOMContentLoaded", init);
