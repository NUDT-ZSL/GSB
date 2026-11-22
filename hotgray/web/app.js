"use strict";

const state = {
  snapshot: null,
  errorCatalog: null,
  loading: false,
  lastError: null,
};

const $ = (id) => document.getElementById(id);

function shortDigest(value, keep = 12) {
  if (!value) return "—";
  const text = String(value);
  const hex = text.startsWith("sha256:") ? text.slice(7) : text;
  return hex.slice(0, keep);
}

function fmtRatio(value) {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(value === 0 || value === 1 ? 0 : 1)}%`;
}

function fmtMs(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toLocaleString("en-US")} ms`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function empty(message) {
  return `<div class="empty">${escapeHtml(message)}</div>`;
}

function statusClass(value) {
  return `status-${String(value || "").replaceAll("-", "_")}`;
}

async function fetchStatus() {
  if (state.loading) return;
  state.loading = true;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.snapshot = await response.json();
    state.lastError = null;
    $("connection-state").className = "connection live";
    $("connection-state").textContent = "LIVE";
    $("error-banner").classList.add("hidden");
    render(state.snapshot);
  } catch (error) {
    state.lastError = error;
    $("connection-state").className = "connection down";
    $("connection-state").textContent = "OFFLINE";
    const banner = $("error-banner");
    banner.textContent = `诊断快照读取失败：${error.message}`;
    banner.classList.remove("hidden");
  } finally {
    state.loading = false;
  }
}

async function fetchErrorCatalog() {
  try {
    const response = await fetch("/api/error-catalog", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.errorCatalog = payload.errors || [];
    renderErrorCatalog(state.errorCatalog);
  } catch (error) {
    const root = $("error-catalog");
    root.innerHTML = empty(`错误码目录读取失败：${error.message}`);
  }
}

function render(snapshot) {
  const round = snapshot.round;
  const clock = snapshot.logical_clock;
  const replicas = [...snapshot.replicas].sort((a, b) => a.replica_id.localeCompare(b.replica_id));
  const requests = [...snapshot.requests].sort((a, b) =>
    a.replica_id.localeCompare(b.replica_id) ||
    Number(a.started_at_ms) - Number(b.started_at_ms) ||
    a.request_id.localeCompare(b.request_id)
  );
  const invalidations = [...snapshot.invalidations].sort((a, b) =>
    a.replica_id.localeCompare(b.replica_id) ||
    Number(a.version) - Number(b.version) ||
    a.cache_type.localeCompare(b.cache_type) ||
    a.decision.localeCompare(b.decision) ||
    a.id - b.id
  );

  $("clock-value").textContent = fmtMs(clock.watermark_ms);
  $("clock-detail").textContent = `watermark · tolerance ±${clock.tolerance_ms} ms`;
  $("round-status").textContent = round.status;
  $("round-status").className = `metric-value ${statusClass(round.status)}`;
  $("round-id").textContent = `${round.round_id} · v${round.baseline_version} → v${round.target_version}`;
  $("level-value").textContent = round.current_level;
  $("level-total").textContent = ` / ${round.stages.length}`;
  $("ratio-value").textContent = `当前目标流量 ${fmtRatio(round.current_ratio)}`;
  const notReady = snapshot.not_ready || [];
  $("readiness-value").textContent = notReady.length === 0 ? "READY" : `${notReady.length} BLOCKED`;
  $("readiness-value").className = `metric-value ${notReady.length === 0 ? "status-ready" : "status-rollback_not_achieved"}`;
  $("readiness-detail").textContent = notReady.length === 0
    ? `目标 v${round.target_version} 分片与配置完整`
    : "存在未提交分片、配置或副本状态";
  $("materialization-value").textContent = snapshot.materializations.count;
  const activeRequests = requests.filter((item) => item.status === "in_progress").length;
  $("request-value").textContent = activeRequests;
  $("request-detail").textContent = `累计 ${requests.length} · 进行中 ${activeRequests}`;
  $("footer-round").textContent = `${round.round_id} · logical ${round.logical_watermark_ms} ms`;

  renderTimeline(round, snapshot);
  renderReplicas(replicas, round);
  renderDependencyGraph(snapshot);
  renderShardChain(snapshot);
  renderInvalidations(invalidations, notReady, snapshot.diagnostics || []);
  renderRequests(requests);
  renderEvents(snapshot.events || []);
  renderBindings(snapshot.version_bindings || {});
}

function renderTimeline(round, snapshot) {
  const timeline = $("timeline");
  timeline.textContent = "";
  const blockedLevels = new Set(
    (snapshot.events || [])
      .filter((event) => event.event_type === "stage_blocked")
      .map((event) => event.payload?.level)
  );
  const base = el("div", "timeline-step done");
  base.innerHTML = `
    <div class="timeline-level"><span>BASELINE</span><span>v${round.baseline_version}</span></div>
    <div class="timeline-ratio">0%</div>
    <div class="timeline-at">起点 · 可复用基线缓存</div>`;
  timeline.append(base);
  for (const stage of round.stages) {
    const node = el("div", "timeline-step");
    const isActive = round.current_level === stage.level;
    const isDone = round.current_level > stage.level;
    const isBlocked = blockedLevels.has(stage.level) && round.current_level < stage.level;
    if (isDone) node.classList.add("done");
    if (isActive) node.classList.add("active");
    if (isBlocked) node.classList.add("blocked");
    node.innerHTML = `
      <div class="timeline-level"><span>LEVEL ${stage.level}</span><span>v${round.target_version}</span></div>
      <div class="timeline-ratio">${fmtRatio(stage.ratio)}</div>
      <div class="timeline-at">计划 ${fmtMs(stage.at_ms)}</div>`;
    timeline.append(node);
  }

  const banner = $("rollback-banner");
  if (["rolling_back", "rollback_not_achieved", "rolled_back"].includes(round.status)) {
    banner.classList.remove("hidden");
    if (round.status === "rolled_back") {
      banner.textContent = `已在回退窗口内恢复基线 v${round.baseline_version}。原因：${round.rollback_reason || "—"}`;
    } else if (round.status === "rollback_not_achieved") {
      banner.textContent = `回退未达成：截止 ${fmtMs(round.rollback_deadline_ms)}，未在窗口内完成倒序恢复。原因：${round.rollback_reason || "—"}`;
    } else {
      banner.textContent = `回退中：失败时刻 ${fmtMs(round.failure_at_ms)}，截止 ${fmtMs(round.rollback_deadline_ms)}，按档位倒序恢复基线 v${round.baseline_version}。`;
    }
  } else {
    banner.classList.add("hidden");
    banner.textContent = "";
  }
}

function renderReplicas(replicas, round) {
  const grid = $("replica-grid");
  grid.textContent = "";
  if (!replicas.length) {
    grid.innerHTML = empty("没有副本状态");
    return;
  }
  for (const replica of replicas) {
    const card = el("article", "replica-card");
    if (["rollback_not_achieved", "loading"].includes(replica.state)) card.classList.add("failed");
    if (!replica.eligible) card.classList.add("control");
    const stateChip = escapeHtml(replica.state);
    const shards = replica.shard_chain || [];
    const invalidated = replica.invalidated || [];
    const reasons = invalidated.slice(0, 3).map((item) => `
      <div class="reason-item"><strong>${escapeHtml(item.trigger)}</strong> · ${escapeHtml(item.cache_type)} · ${escapeHtml(item.path?.at(-1) || "")}</div>
    `).join("");
    card.innerHTML = `
      <div class="replica-top">
        <div>
          <div class="replica-id">${escapeHtml(replica.replica_id)}</div>
          <div class="replica-sub">${replica.eligible ? `灰度成员 · 档位 ${round.current_level}` : "非灰度副本 · 基线保护"}</div>
        </div>
        <span class="state-chip ${escapeHtml(replica.state)}">${stateChip}</span>
      </div>
      <div class="version-flow">
        <span class="version-node">v${replica.baseline_version}</span>
        <span class="version-arrow">→</span>
        <span class="version-node current">v${replica.serving_version}</span>
        <span class="version-arrow">·</span>
        <span class="mono">${fmtRatio(replica.gray_ratio)}</span>
      </div>
      <div class="ratio-bar"><div class="ratio-fill" style="width:${Math.max(0, Math.min(100, Number(replica.gray_ratio) * 100))}%"></div></div>
      <div class="ratio-caption"><span>目标流量</span><span>${shards.length} shards</span></div>
      <div class="shard-strip" title="请求绑定分片摘要链">
        ${shards.map((shard) => `<span class="shard-cell" title="${escapeHtml(shard.layer)}#${shard.shard_index} ${escapeHtml(shard.digest)}"></span>`).join("")}
      </div>
      <div class="replica-foot">
        <span>binding ${escapeHtml(shortDigest(replica.version_binding?.binding_digest))}</span>
        <span>${replica.failure_reason ? "FAILED" : "CACHE OK"}</span>
      </div>
      <div class="reason-list">
        ${reasons || `<div class="reason-item">无目标缓存失效 · 基线逐位可复用</div>`}
        ${invalidated.length > 3 ? `<div class="reason-item">另有 ${invalidated.length - 3} 条失效记录</div>` : ""}
      </div>`;
    grid.append(card);
  }
}

function svgNode(svg, x, y, width, height, title, subtitle, tone) {
  const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("x", x);
  rect.setAttribute("y", y);
  rect.setAttribute("width", width);
  rect.setAttribute("height", height);
  rect.setAttribute("rx", 8);
  rect.setAttribute("fill", tone.fill);
  rect.setAttribute("stroke", tone.stroke);
  rect.setAttribute("stroke-width", "1");
  group.append(rect);
  const titleNode = document.createElementNS("http://www.w3.org/2000/svg", "text");
  titleNode.setAttribute("x", x + 9);
  titleNode.setAttribute("y", y + 18);
  titleNode.setAttribute("fill", tone.text);
  titleNode.setAttribute("font-size", "10");
  titleNode.setAttribute("font-family", "Consolas, monospace");
  titleNode.setAttribute("font-weight", "700");
  titleNode.textContent = title;
  group.append(titleNode);
  const subNode = document.createElementNS("http://www.w3.org/2000/svg", "text");
  subNode.setAttribute("x", x + 9);
  subNode.setAttribute("y", y + 32);
  subNode.setAttribute("fill", tone.sub);
  subNode.setAttribute("font-size", "8");
  subNode.setAttribute("font-family", "Consolas, monospace");
  subNode.textContent = subtitle;
  group.append(subNode);
  svg.append(group);
  return { group, x, y, width, height };
}

function svgLine(svg, x1, y1, x2, y2, color) {
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  const mid = (x1 + x2) / 2;
  line.setAttribute("d", `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", color);
  line.setAttribute("stroke-width", "1.2");
  line.setAttribute("stroke-opacity", "0.55");
  svg.append(line);
}

function renderDependencyGraph(snapshot) {
  const svg = $("dependency-graph");
  svg.textContent = "";
  const round = snapshot.round;
  const binding = snapshot.version_bindings?.[String(round.target_version)] ||
    snapshot.version_bindings?.[String(round.baseline_version)];
  $("graph-note").textContent = binding ? `目标组合 ${shortDigest(binding.binding_digest)}` : "目标组合尚未提交";
  if (!binding) {
    svg.innerHTML = `<text x="380" y="175" text-anchor="middle" fill="#587082" font-size="12">目标版本组合尚未提交</text>`;
    return;
  }

  const tones = {
    teal: { fill: "rgba(55,224,194,0.10)", stroke: "rgba(55,224,194,0.45)", text: "#37e0c2", sub: "#7ecfbd" },
    blue: { fill: "rgba(104,168,255,0.09)", stroke: "rgba(104,168,255,0.40)", text: "#9ec8ff", sub: "#7e9fbe" },
    violet: { fill: "rgba(182,153,255,0.09)", stroke: "rgba(182,153,255,0.36)", text: "#c1aaff", sub: "#9385bd" },
    amber: { fill: "rgba(255,190,92,0.10)", stroke: "rgba(255,190,92,0.42)", text: "#ffbe5c", sub: "#b89b6d" },
  };

  const chain = binding.shard_chain || [];
  const layers = [...new Set(chain.map((item) => item.layer))].sort();
  const layerNodes = [];
  layers.slice(0, 5).forEach((layer, index) => {
    const count = chain.filter((item) => item.layer === layer).length;
    const node = svgNode(
      svg, 28, 40 + index * 55, 150, 40,
      layer, `${count} shards · ${shortDigest(chain.find((item) => item.layer === layer)?.digest, 10)}`, tones.blue
    );
    layerNodes.push(node);
  });
  const modelNode = svgNode(svg, 280, 135, 185, 48, `model:v${binding.version}`, shortDigest(binding.binding_digest, 18), tones.teal);
  const resources = Object.values(binding.resources || {})
    .sort((a, b) => String(a.resource_id || a.kind).localeCompare(String(b.resource_id || b.kind)));
  const resourceNodes = [];
  resources.slice(0, 6).forEach((resource, index) => {
    const label = resource.resource_id || `${resource.kind}:${resource.revision}`;
    const node = svgNode(
      svg, 558, 28 + index * 48, 174, 37,
      label, `${resource.kind} · ${shortDigest(resource.digest, 12)}`, tones.violet
    );
    resourceNodes.push(node);
  });
  for (const node of layerNodes) {
    svgLine(svg, node.x + node.width, node.y + node.height / 2, modelNode.x, modelNode.y + modelNode.height / 2, "#4c8ec7");
  }
  for (const node of resourceNodes) {
    svgLine(svg, modelNode.x + modelNode.width, modelNode.y + modelNode.height / 2, node.x, node.y + node.height / 2, "#8a7bc0");
  }
  const cacheNames = ["load_cache", "warmup", "inference"];
  cacheNames.forEach((name, index) => {
    const node = svgNode(
      svg, 250 + index * 165, 286, 145, 36,
      name, index === 0 ? "layer/shard refs" : index === 1 ? "load + configs" : "warmup snapshot", tones.amber
    );
    svgLine(svg, modelNode.x + modelNode.width / 2, modelNode.y + modelNode.height, node.x + node.width / 2, node.y, "#b98e50");
  });
}

function renderShardChain(snapshot) {
  const root = $("shard-chain");
  root.textContent = "";
  const versions = Object.keys(snapshot.version_bindings || {}).map(Number).sort((a, b) => a - b);
  if (versions.length === 0) {
    root.innerHTML = empty("没有已提交的分片摘要链");
    return;
  }
  const chains = versions.map((version) => ({
    version,
    chain: snapshot.version_bindings[String(version)]?.shard_chain || [],
  }));
  const layerNames = [...new Set(chains.flatMap(({ chain }) => chain.map((item) => item.layer)))].sort();
  for (const layer of layerNames) {
    const layerBox = el("div", "shard-layer");
    const indices = [...new Set(chains.flatMap(({ chain }) =>
      chain.filter((item) => item.layer === layer).map((item) => item.shard_index)
    ))].sort((a, b) => a - b);
    const head = el("div", "shard-layer-head");
    head.innerHTML = `
      <span class="shard-layer-name">${escapeHtml(layer)}</span>
      <span class="shard-layer-meta">${indices.length} shards · ${chains.map(({ version }) => `v${version}`).join(" / ")}</span>`;
    layerBox.append(head);
    for (const index of indices) {
      const row = el("div", "shard-row");
      row.append(el("span", "shard-index", `shard ${String(index).padStart(2, "0")}`));
      const digests = chains.map(({ version, chain }) =>
        chain.find((item) => item.layer === layer && item.shard_index === index)?.digest || null
      );
      const allEqual = digests.every((digest) => digest === digests[0]);
      chains.forEach(({ version }, versionIndex) => {
        const digest = digests[versionIndex];
        const pill = el("span", "digest-pill", `v${version} · ${shortDigest(digest, 18)}`);
        pill.title = digest || "未提交";
        if (digest && !allEqual) pill.classList.add("changed");
        if (digest && allEqual) pill.classList.add("reused");
        row.append(pill);
      });
      layerBox.append(row);
    }
    root.append(layerBox);
  }
}

function pathFlow(path) {
  if (!path || !path.length) return "";
  return path.map((node, index) => `
    ${index ? '<span class="path-arrow">→</span>' : ""}
    <span class="path-node">${escapeHtml(node)}</span>
  `).join("");
}

function renderInvalidations(invalidations, notReady, diagnostics) {
  $("invalidation-count").textContent = `${invalidations.length} 条裁决`;
  const list = $("invalidation-list");
  list.textContent = "";
  if (!invalidations.length) {
    list.innerHTML = empty("补丁尚未提交，暂无失效裁决");
  } else {
    for (const item of invalidations) {
      const box = el("div", `ledger-item ${item.decision}`);
      box.innerHTML = `
        <div class="ledger-top">
          <span class="ledger-title">${escapeHtml(item.replica_id)} · ${escapeHtml(item.cache_type)} · v${item.version}</span>
          <span class="ledger-meta">${escapeHtml(item.decision)} · ${escapeHtml(item.trigger)}</span>
        </div>
        <div class="path-flow">${pathFlow(item.path)}</div>`;
      list.append(box);
    }
  }

  const issueList = $("not-ready-list");
  issueList.textContent = "";
  if (!notReady.length && !diagnostics.length) {
    issueList.innerHTML = empty("目标版本全部分片与配置就绪");
  } else {
    for (const issue of notReady) {
      const box = el("div", "ledger-item issue");
      const details = Object.entries(issue)
        .filter(([key]) => !["message", "type"].includes(key))
        .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : value}`)
        .join(" · ");
      box.innerHTML = `
        <div class="ledger-top">
          <span class="ledger-title">${escapeHtml(issue.type)}</span>
          <span class="ledger-meta">${escapeHtml(issue.version ? `v${issue.version}` : "")}</span>
        </div>
        <div class="ledger-meta">${escapeHtml(issue.message || details)}</div>
        ${details ? `<div class="path-flow"><span class="path-node">${escapeHtml(details)}</span></div>` : ""}`;
      issueList.append(box);
    }
    for (const diagnostic of diagnostics) {
      const box = el("div", "ledger-item issue");
      box.innerHTML = `
        <div class="ledger-top">
          <span class="ledger-title">${escapeHtml(diagnostic.code)} · ${escapeHtml(diagnostic.name)}</span>
          <span class="ledger-meta">${fmtMs(diagnostic.at_ms)}</span>
        </div>
        <div class="ledger-meta">${escapeHtml(diagnostic.message)}</div>
        <div class="path-flow"><span class="path-node">${escapeHtml(JSON.stringify(diagnostic.details))}</span></div>`;
      issueList.append(box);
    }
  }
}

function renderRequests(requests) {
  const body = $("request-table");
  body.textContent = "";
  if (!requests.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.innerHTML = empty("尚无推理请求");
    row.append(cell);
    body.append(row);
    return;
  }
  for (const request of requests) {
    const row = document.createElement("tr");
    const lane = request.version === state.snapshot.round.target_version ? "target" : "baseline";
    row.innerHTML = `
      <td class="mono">${escapeHtml(request.request_id)}</td>
      <td class="mono">${escapeHtml(request.replica_id)}</td>
      <td><span class="lane-chip ${lane}">${lane}</span></td>
      <td class="mono">v${request.version} / L${request.level}</td>
      <td class="mono" title="${escapeHtml(request.binding_digest)}">${escapeHtml(shortDigest(request.binding_digest, 16))}</td>
      <td class="mono" title="${escapeHtml(request.result_digest || "")}">${escapeHtml(shortDigest(request.result_digest, 16))}</td>
      <td><span class="state-chip ${request.status === "completed" ? "gray" : "loading"}">${escapeHtml(request.status)}</span></td>
      <td class="mono muted">${fmtMs(request.started_at_ms)} → ${request.completed_at_ms ? fmtMs(request.completed_at_ms) : "…"}</td>`;
    body.append(row);
  }
}

function renderEvents(events) {
  const root = $("event-list");
  root.textContent = "";
  const ordered = [...events].sort((a, b) => Number(a.at_ms) - Number(b.at_ms) || Number(a.id) - Number(b.id));
  if (!ordered.length) {
    root.innerHTML = empty("暂无事务事件");
    return;
  }
  for (const event of ordered) {
    const row = el("div", "event-row");
    row.innerHTML = `
      <span class="event-time">+ ${fmtMs(event.at_ms)}</span>
      <span class="event-type">${escapeHtml(event.event_type)}</span>
      <span class="event-payload">${escapeHtml(JSON.stringify(event.payload))}</span>`;
    root.append(row);
  }
}

function renderBindings(bindings) {
  const root = $("binding-list");
  root.textContent = "";
  const entries = Object.entries(bindings).sort(([a], [b]) => Number(a) - Number(b));
  if (!entries.length) {
    root.innerHTML = empty("没有完整已提交的版本组合");
    return;
  }
  for (const [version, binding] of entries) {
    const box = el("div", "binding-item");
    const resources = Object.values(binding.resources || {})
      .sort((a, b) => String(a.resource_id).localeCompare(String(b.resource_id)));
    box.innerHTML = `
      <div class="binding-head">
        <span class="binding-version">model v${escapeHtml(version)}</span>
        <span class="ledger-meta">${binding.shard_chain?.length || 0} shards committed</span>
      </div>
      <div class="binding-digest">${escapeHtml(binding.binding_digest || "")}</div>
      <div class="binding-resources">
        ${resources.map((resource) => `<span class="resource-chip">${escapeHtml(resource.resource_id || resource.kind)}:${escapeHtml(shortDigest(resource.digest, 8))}</span>`).join("")}
      </div>`;
    root.append(box);
  }
}

function renderErrorCatalog(errors) {
  const root = $("error-catalog");
  root.textContent = "";
  const ordered = [...errors].sort((a, b) => a.code.localeCompare(b.code));
  for (const item of ordered) {
    const box = el("div", "catalog-item");
    box.innerHTML = `
      <div class="catalog-code">${escapeHtml(item.code)}</div>
      <div class="catalog-name">${escapeHtml(item.name)}</div>
      <div class="catalog-title">${escapeHtml(item.title)}</div>
      <div class="catalog-fields">${escapeHtml((item.diagnostic_fields || []).join(" · ") || "无额外字段")}</div>`;
    root.append(box);
  }
}

$("refresh-button").addEventListener("click", fetchStatus);
fetchStatus();
fetchErrorCatalog();
