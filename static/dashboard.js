const state = {
  data: null,
  busy: false,
};

const $ = (selector) => document.querySelector(selector);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function appendAll(parent, children) {
  for (const child of children) {
    if (child) parent.appendChild(child);
  }
  return parent;
}

function fmt(value, digits = 4) {
  if (value === null || value === undefined) return "--";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(digits);
  }
  return String(value);
}

function shortHash(value) {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : "--";
}

function showToast(title, message, isError = false) {
  const toast = node("div", `toast${isError ? " error" : ""}`);
  toast.appendChild(node("strong", "", title));
  toast.appendChild(node("p", "", message));
  $("#toast-stack").appendChild(toast);
  window.setTimeout(() => toast.remove(), 5200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.message || "请求失败");
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function refresh() {
  try {
    state.data = await api("/api/state");
    render(state.data);
  } catch (error) {
    showToast("状态加载失败", error.message, true);
  }
}

function setBusy(value) {
  state.busy = value;
  document.querySelectorAll("button[data-action]").forEach((button) => {
    button.disabled = value;
  });
}

async function runAction(action) {
  if (state.busy) return;
  setBusy(true);
  const routes = {
    "commit-next": ["/api/commit-next", "下一篇已增量提交", (r) => `文档 ${r.report.document_id} 完成；复用 ${r.report.reused_chain_ids.length} 条链。`],
    conflict: ["/api/demo/conflict", "冲突仲裁完成", (r) => {
      const record = r.conflict_records?.[0];
      return record
        ? `${record.event_uid} 选择 ${record.chosen_chain}，拒绝 ${record.rejected.map((x) => x.chain_id).join("、")}。`
        : "已加载冲突仲裁结果。";
    }],
    degrade: ["/api/demo/degrade", "降级合并完成", (r) => {
      const d = r.report?.degradation;
      return d
        ? `${d.category}：下限 ${d.effective_floor}，精算 ${r.report.comparisons_performed}/${d.comparisons_planned} 对。`
        : "D07 已存在，显示既有阶段结果。";
    }],
    invalidate: ["/api/demo/invalidate", "增量重算完成", (r) => {
      const item = r.invalidation;
      return `从 ${item.start_document || "无"} 起重算；重算 ${item.recalculated_chain_ids.length} 条，复用 ${item.reused_chain_ids.length} 条。`;
    }],
    rollback: ["/api/probes/rollback", "", null],
    corruption: ["/api/probes/corruption", "", null],
    busy: ["/api/probes/stage-busy", "", null],
    reset: ["/api/reset", "工作台已重置", () => "恢复 D01–D03 基线并清空运行时缓存。"],
  };
  const [path, successTitle, describe] = routes[action];
  try {
    const payload = await api(path, {
      method: "POST",
      body: action === "invalidate" ? {} : undefined,
    });
    if (successTitle) {
      showToast(successTitle, describe ? describe(payload) : "操作完成。");
    }
  } catch (error) {
    const payload = error.payload || {};
    showToast(
      payload.code || "E_REQUEST",
      error.message,
      true,
    );
  } finally {
    setBusy(false);
    await refresh();
  }
}

function renderMetrics(data) {
  const metrics = $("#metrics");
  metrics.replaceChildren();
  const stages = data.stages || [];
  const last = data.last_stage || {};
  const cacheStats = data.cache || {};
  const checks = data.last_self_check;
  const cards = [
    ["events", "局部事件", `${data.counter.documents_processed}/${data.counter.documents_total} 文档`, "文档内局部编号唯一"],
    ["chains", "共指链", data.counter.chains, "单归属校验通过"],
    ["candidates", "候选对", data.counter.candidates || data.resources.candidate_count, "证据分项可独立重算"],
    ["conflicts", "冲突记录", data.counter.chain_conflicts + data.counter.argument_conflicts, `链间 ${data.counter.chain_conflicts} / 论元 ${data.counter.argument_conflicts}`],
    ["selfcheck", "一致性", checks?.ok ? "PASS" : "WAIT", "增量 vs 全量重建"],
    ["cachehits", "缓存命中", cacheStats.hits ?? 0, `对象复用 ${cacheStats.object_reuses ?? 0} 次`],
  ];
  for (const [key, label, value, note] of cards) {
    const card = node("article", `metric-card metric-${key}`);
    card.appendChild(node("span", "metric-value", value));
    card.appendChild(node("span", "metric-label", label));
    card.appendChild(node("span", "metric-note", note));
    metrics.appendChild(card);
  }
  $("#seed-label").textContent = `seed ${data.app.seed}`;
  $("#runtime-path").textContent = data.app.runtime_path;
  $("#doc-progress").textContent = `${data.counter.documents_processed}/${data.counter.documents_total} processed`;
  $("#arb-count").textContent = `${data.counter.arbitration_records} 条`;
  $("#cache-badge").textContent = `CACHE ${cacheStats.hits ?? 0}H / ${cacheStats.misses ?? 0}M`;
  const lastError = $("#last-error-code");
  lastError.textContent = data.last_error_code || "NO_ERROR";
  lastError.classList.toggle("has-error", Boolean(data.last_error_code));
  $("#selfcheck-badge").textContent = checks?.ok ? "PARITY OK" : "PENDING";
}

function renderDocuments(data) {
  const list = $("#doc-list");
  list.replaceChildren();
  for (const doc of data.documents) {
    const item = node("article", `doc-item${doc.processed ? " done" : ""}`);
    const top = node("div", "doc-topline");
    top.appendChild(node("span", "doc-id", doc.doc_id));
    top.appendChild(node("span", `doc-state${doc.processed ? " done" : ""}`, doc.processed ? "已入链" : "待处理"));
    item.appendChild(top);
    item.appendChild(node("div", "doc-source", `${doc.source_label} · trust ${fmt(doc.trust, 2)}`));
    item.appendChild(node("div", "doc-text", doc.content));
    const badges = node("div", "doc-badges");
    badges.appendChild(node("span", "mini-tag", `DOC#${doc.doc_index}`));
    badges.appendChild(node("span", "mini-tag", `${doc.event_count} EVENT`));
    badges.appendChild(node("span", "mini-tag", `T${fmt(doc.trust, 2)}`));
    item.appendChild(badges);
    list.appendChild(item);
  }
}

function svgElement(tag, attrs = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    element.setAttribute(key, String(value));
  }
  return element;
}

function svgText(text, x, y, attrs = {}) {
  const element = svgElement("text", {
    x,
    y,
    fill: "#a9c2cc",
    "font-size": 10,
    "font-family": "'Cascadia Code', 'SFMono-Regular', Consolas, monospace",
    ...attrs,
  });
  element.textContent = text;
  return element;
}

function renderTopology(data) {
  const svg = $("#topology");
  svg.replaceChildren();
  const chains = data.chains || [];
  const maxMembers = Math.max(1, ...chains.map((chain) => chain.members.length));
  const width = Math.max(820, 190 + maxMembers * 205);
  const height = Math.max(500, 92 + chains.length * 124);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("height", String(height));

  const defs = svgElement("defs");
  const marker = svgElement("marker", {
    id: "arrow-red",
    viewBox: "0 0 10 10",
    refX: 8,
    refY: 5,
    markerWidth: 5,
    markerHeight: 5,
    orient: "auto-start-reverse",
  });
  marker.appendChild(svgElement("path", {
    d: "M 0 0 L 10 5 L 0 10 z",
    fill: "#ff7b82",
  }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  if (!chains.length) {
    svg.appendChild(svgText("暂无共指链", width / 2 - 36, height / 2, { "font-size": 14 }));
    $("#chain-strip").replaceChildren(node("div", "empty", "等待增量提交。"));
    return;
  }

  const centers = new Map();
  chains.forEach((chain, chainIndex) => {
    const laneY = 78 + chainIndex * 124;
    svg.appendChild(svgElement("rect", {
      x: 110,
      y: laneY - 48,
      width: width - 128,
      height: 96,
      rx: 13,
      fill: chainIndex % 2 === 0 ? "rgba(12,35,46,0.56)" : "rgba(8,25,35,0.56)",
      stroke: "rgba(125,175,196,0.12)",
    }));
    svg.appendChild(svgText(chain.chain_id.replace("CH-", ""), 20, laneY - 9, {
      fill: "#62e6d6",
      "font-size": 11,
      "font-weight": 700,
    }));
    svg.appendChild(svgText(`${chain.members.length} EVENTS`, 20, laneY + 8, {
      fill: "#718b96",
      "font-size": 8,
    }));
    svg.appendChild(svgText(`FIRST ${chain.first_seen}`, 20, laneY + 23, {
      fill: "#5b7480",
      "font-size": 8,
    }));

    chain.members.forEach((member, memberIndex) => {
      const x = 144 + memberIndex * 198;
      const y = laneY - 28;
      if (memberIndex > 0) {
        svg.appendChild(svgElement("line", {
          x1: x - 38,
          y1: laneY,
          x2: x - 6,
          y2: laneY,
          stroke: "rgba(98,230,214,0.62)",
          "stroke-width": 1.5,
        }));
      }
      svg.appendChild(svgElement("rect", {
        x,
        y,
        width: 174,
        height: 56,
        rx: 9,
        fill: "rgba(14,42,53,0.94)",
        stroke: "rgba(98,230,214,0.32)",
      }));
      svg.appendChild(svgElement("circle", {
        cx: x + 12,
        cy: y + 14,
        r: 3,
        fill: "#62e6d6",
      }));
      svg.appendChild(svgText(member.event_uid, x + 22, y + 17, {
        fill: "#e2f0f3",
        "font-size": 9,
        "font-weight": 700,
      }));
      svg.appendChild(svgText(`“${member.trigger}” / ${member.trigger_lemma}`, x + 12, y + 34, {
        fill: "#9eb6bf",
        "font-size": 8.5,
      }));
      svg.appendChild(svgText(member.source, x + 12, y + 48, {
        fill: "#5f7b86",
        "font-size": 7.5,
      }));
      centers.set(member.event_uid, { x: x + 87, y: y + 28 });
    });
  });

  for (const edge of data.candidate_edges || []) {
    if (edge.status !== "cross_chain_conflict") continue;
    const left = centers.get(edge.left_event);
    const right = centers.get(edge.right_event);
    if (!left || !right) continue;
    const middleX = (left.x + right.x) / 2;
    const lift = Math.abs(right.y - left.y) > 50 ? 46 : -38;
    const path = svgElement("path", {
      d: `M ${left.x} ${left.y} C ${middleX} ${left.y + lift}, ${middleX} ${right.y + lift}, ${right.x} ${right.y}`,
      fill: "none",
      stroke: "rgba(255,123,130,0.68)",
      "stroke-width": 1.1,
      "stroke-dasharray": "5 5",
      "marker-end": "url(#arrow-red)",
    });
    path.appendChild(svgElement("title", {}));
    path.lastChild.textContent = `${edge.candidate_pair_id} · ${edge.total}`;
    svg.appendChild(path);
  }

  const strip = $("#chain-strip");
  strip.replaceChildren();
  for (const chain of chains) {
    const card = node("article", "chain-card");
    const title = node("div", "chain-title");
    title.appendChild(node("strong", "", chain.chain_id));
    title.appendChild(node("span", "", `n=${chain.members.length}`));
    card.appendChild(title);
    const argumentsText = Object.entries(chain.argument_resolutions)
      .map(([role, resolution]) => `${role}:${resolution.winner?.value ?? "--"}`)
      .join(" · ");
    card.appendChild(node("div", "chain-meta", argumentsText || "无论元"));
    card.appendChild(node("div", "chain-meta", `max evidence ${fmt(chain.max_evidence, 4)}`));
    strip.appendChild(card);
  }
}

function renderDiagnostics(data) {
  const resourceList = $("#resource-list");
  resourceList.replaceChildren();
  const last = data.last_stage || {};
  const resourceRows = [
    ["候选比较", `${fmt(last.comparisons_performed)} / ${fmt(last.comparisons_planned)}`, "本阶段精算 / 计划"],
    ["比较上限", fmt(data.resources.max_candidate_comparisons), "max_candidate_comparisons"],
    ["链内存", `${fmt(data.resources.estimated_chain_memory_bytes)} B`, `上限 ${fmt(data.resources.max_chain_memory_bytes)} B`],
    ["单文档事件", fmt(data.resources.max_doc_events), "max_doc_events"],
  ];
  for (const [name, value, note] of resourceRows) {
    const row = node("div", "resource-row");
    const top = node("div", "resource-top");
    top.appendChild(node("span", "", name));
    top.appendChild(node("strong", "", value));
    row.appendChild(top);
    const progress = node("div", "progress");
    const bar = node("i");
    let ratio = 0.2;
    if (name === "候选比较") {
      ratio = last.comparisons_planned ? last.comparisons_performed / last.comparisons_planned : 0;
    } else if (name === "比较上限") {
      ratio = last.comparisons_planned ? Math.min(1, last.comparisons_planned / data.resources.max_candidate_comparisons) : 0;
    } else if (name === "链内存") {
      ratio = Math.min(1, data.resources.estimated_chain_memory_bytes / data.resources.max_chain_memory_bytes);
    }
    bar.style.width = `${Math.max(3, Math.min(100, ratio * 100))}%`;
    progress.appendChild(bar);
    row.appendChild(progress);
    row.appendChild(node("div", "metric-note", note));
    resourceList.appendChild(row);
  }

  const snapshots = $("#snapshot-list");
  snapshots.replaceChildren();
  const labels = { chains: "链状态", trust_table: "可信度表", arbitration: "仲裁记录" };
  for (const [key, snapshot] of Object.entries(data.snapshots)) {
    const item = node("div", "snapshot-item");
    item.appendChild(node("span", "snap-name", labels[key] || key));
    item.appendChild(node("span", "snap-version", `v${snapshot.version}`));
    item.appendChild(node("span", "snap-checksum", shortHash(snapshot.checksum)));
    snapshots.appendChild(item);
  }

  const errors = $("#error-list");
  errors.replaceChildren();
  if (!data.recent_errors.length) {
    errors.appendChild(node("div", "empty", "暂无业务错误。"));
  } else {
    for (const error of data.recent_errors) {
      const item = node("div", "error-item");
      item.appendChild(node("code", "", `#${error.sequence} ${error.code}`));
      item.appendChild(node("p", "", error.message));
      errors.appendChild(item);
    }
  }
}

function renderArbitration(data) {
  const list = $("#arbitration-list");
  list.replaceChildren();
  const records = [...(data.arbitration_records || [])].reverse();
  if (!records.length) {
    list.appendChild(node("div", "empty", "尚未触发仲裁。"));
    return;
  }
  for (const record of records) {
    const isChain = record.kind === "chain_conflict";
    const card = node("article", `arb-record ${isChain ? "chain" : "argument"}`);
    const head = node("div", "arb-head");
    head.appendChild(node("span", "arb-id", record.record_id));
    head.appendChild(node("span", `arb-kind${isChain ? "" : " argument"}`, isChain ? "链间冲突" : "论元矛盾"));
    head.appendChild(node("span", "mini-tag", `S${String(record.stage_seq).padStart(4, "0")}`));
    card.appendChild(head);
    const body = node("div", "arb-body");

    if (isChain) {
      const chosen = body;
      chosen.appendChild(node("span", "", "事件 "));
      chosen.appendChild(node("strong", "", record.event_uid));
      chosen.appendChild(node("span", "", " 仲裁选择 "));
      chosen.appendChild(node("strong", "", record.chosen_chain));
      const table = node("div", "evidence-table");
      table.appendChild(node("span", "head", "候选链"));
      table.appendChild(node("span", "head", "证据强度"));
      table.appendChild(node("span", "head", "链规模"));
      for (const proposal of record.proposals) {
        table.appendChild(node("span", "", proposal.chain_id));
        table.appendChild(node("span", "num", fmt(proposal.evidence_strength, 5)));
        table.appendChild(node("span", "num", proposal.chain_size));
      }
      card.appendChild(body);
      card.appendChild(table);
      const rejected = node("div", "rejected-list");
      for (const item of record.rejected) {
        rejected.appendChild(node("span", "rejected-chip", `拒绝 ${item.chain_id} · ${item.reason}`));
      }
      card.appendChild(rejected);
    } else {
      body.appendChild(node("span", "", `链 ${record.chain_id} / 角色 ${record.role}：胜出 `));
      body.appendChild(node("strong", "", record.chosen_value));
      body.appendChild(node("span", "", `，落选 ${record.contending_value}。`));
      const table = node("div", "evidence-table");
      table.appendChild(node("span", "head", "论元值"));
      table.appendChild(node("span", "head", "支持度"));
      table.appendChild(node("span", "head", "可信度"));
      for (const item of record.evidence) {
        table.appendChild(node("span", "", item.value));
        table.appendChild(node("span", "num", fmt(item.support, 4)));
        table.appendChild(node("span", "num", fmt(item.max_trust, 4)));
      }
      card.appendChild(body);
      card.appendChild(table);
    }
    list.appendChild(card);
  }
}

function renderArguments(data) {
  const view = $("#argument-spectrum");
  view.replaceChildren();
  let rendered = 0;
  for (const chain of data.chains || []) {
    const roles = Object.entries(chain.argument_resolutions || {});
    if (!roles.length) continue;
    rendered += 1;
    const block = node("article", "arg-chain");
    const head = node("div", "arg-chain-head");
    head.appendChild(node("strong", "", chain.chain_id));
    head.appendChild(node("span", "", `${chain.members.length} events`));
    block.appendChild(head);
    for (const [role, resolution] of roles) {
      const section = node("div", "arg-role");
      const title = node("div", "arg-role-title");
      title.appendChild(node("span", "", `角色 ${role}`));
      const winnerLabel = node("span");
      winnerLabel.appendChild(node("strong", "", resolution.winner?.value ?? "--"));
      title.appendChild(winnerLabel);
      section.appendChild(title);
      const groups = [];
      if (resolution.winner) {
        groups.push({ ...resolution.winner, winner: true });
      }
      for (const loser of resolution.losers || []) {
        groups.push({ ...loser, winner: false });
      }
      const maxSupport = Math.max(1, ...groups.map((group) => Number(group.support) || 0));
      for (const group of groups) {
        const row = node("div", "evidence-row");
        row.appendChild(node("span", `evidence-value${group.winner ? "" : " loser"}`, `${group.winner ? "✓" : "×"} ${group.value}`));
        const bar = node("div", `evidence-bar${group.winner ? "" : " loser"}`);
        const fill = node("i");
        fill.style.width = `${Math.max(3, (Number(group.support) / maxSupport) * 100)}%`;
        bar.appendChild(fill);
        row.appendChild(bar);
        row.appendChild(node("span", "evidence-num", `S ${fmt(group.support, 4)} / T ${fmt(group.max_trust, 4)}`));
        section.appendChild(row);
      }
      block.appendChild(section);
    }
    view.appendChild(block);
  }
  if (!rendered) view.appendChild(node("div", "empty", "暂无论元证据。"));
}

function renderInvalidation(data) {
  const view = $("#invalidation-view");
  view.replaceChildren();
  const item = data.last_invalidation;
  if (!item) {
    view.appendChild(node("div", "empty", "点击“修订 D06 并增量重算”查看受影响后缀与复用链清单。"));
    return;
  }
  const summary = node("div", "invalidation-status");
  summary.appendChild(node("strong", "", `起点 ${item.start_document || "无"} · ${item.reason}`));
  summary.appendChild(node("div", "", `重算文档：${item.recomputed_document_ids.join("、") || "无"}`));
  summary.appendChild(node("div", "", `复用文档：${item.reused_document_ids.join("、") || "无"}`));
  summary.appendChild(node("div", "", `阈值 ${item.threshold_before} → ${item.threshold_after} · trust ${item.trust_version_before} → ${item.trust_version_after}`));
  view.appendChild(summary);

  const split = node("div", "split-columns");
  for (const [title, ids, className] of [
    ["重算链", item.recalculated_chain_ids, "recalc"],
    ["复用链", item.reused_chain_ids, "reuse"],
  ]) {
    const box = node("div", "list-box");
    box.appendChild(node("h3", "", `${title} · ${ids.length}`));
    if (!ids.length) {
      box.appendChild(node("span", "id-token", "无"));
    } else {
      for (const id of ids) box.appendChild(node("span", `id-token ${className}`, id));
    }
    split.appendChild(box);
  }
  view.appendChild(split);

  if (item.affected_candidate_pairs?.length) {
    const box = node("div", "list-box");
    box.appendChild(node("h3", "", `受影响候选对 · ${item.affected_candidate_pairs.length}`));
    const pairs = node("div", "pair-list");
    for (const pair of item.affected_candidate_pairs.slice(0, 8)) {
      const row = node("div", "pair-row");
      row.appendChild(node("span", "", pair.pair_id));
      row.appendChild(node("strong", "", `${fmt(pair.old_total, 5)} → ${fmt(pair.new_total, 5)}`));
      pairs.appendChild(row);
    }
    box.appendChild(pairs);
    view.appendChild(box);
  }
}

function renderSelfCheck(data) {
  const view = $("#selfcheck-view");
  view.replaceChildren();
  const check = data.last_self_check;
  if (!check) {
    view.appendChild(node("div", "empty", "提交后自动执行增量与全量重建一致性校验。"));
    return;
  }
  const hero = node("div", "selfcheck-hero");
  hero.appendChild(node("div", "selfcheck-check", check.ok ? "✓" : "!"));
  const text = node("div");
  text.appendChild(node("strong", "", check.ok ? "增量结果与全量重建一致" : "检测到增量偏差"));
  text.appendChild(node("span", "", `检查点阶段 S${String(check.checked_at_stage).padStart(4, "0")}`));
  hero.appendChild(text);
  view.appendChild(hero);

  const grid = node("div", "check-grid");
  const cells = [
    ["链成员集合", `${check.chain_count} / ${check.full_rebuild_chain_count}`],
    ["候选证据复算", check.candidate_evidence_recomputed],
    ["事件扫描覆盖", check.event_scan_coverage],
    ["链间仲裁一致", check.chain_arbitration_parity ? "YES" : "NO"],
    ["降级阶段", check.degraded_stages?.join("、") || "无"],
    ["候选总数", data.resources.candidate_count],
  ];
  for (const [label, value] of cells) {
    const cell = node("div", "check-cell");
    cell.appendChild(node("span", "", label));
    cell.appendChild(node("strong", "", value));
    grid.appendChild(cell);
  }
  view.appendChild(grid);

  if (data.last_stage?.degraded) {
    const d = data.last_stage.degradation;
    view.appendChild(node("div", "degraded-callout", `降级类别 ${d.category}：常规下限 ${d.original_floor}，实际 ${d.effective_floor}；计划比较 ${d.comparisons_planned}，精算 ${data.last_stage.comparisons_performed}，上限 ${d.comparison_limit}。`));
  }
}

function render(data) {
  renderMetrics(data);
  renderDocuments(data);
  renderTopology(data);
  renderDiagnostics(data);
  renderArbitration(data);
  renderArguments(data);
  renderInvalidation(data);
  renderSelfCheck(data);
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  runAction(button.dataset.action);
});

refresh();
