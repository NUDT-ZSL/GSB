const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  busy: false,
  jobId: null,
  plan: null,
  currentFrame: null,
  decodedFrames: [],
};

const els = {
  status: $("#globalStatus"),
  statusText: $("#globalStatus span"),
  run: $("#runButton"),
  fault: $("#faultButton"),
  budget: $("#budgetButton"),
  reset: $("#resetButton"),
  prompt: $("#promptInput"),
  seed: $("#seedInput"),
  frames: $("#framesInput"),
  steps: $("#stepsInput"),
  budgetMs: $("#budgetInput"),
  block: $("#blockSizeInput"),
  compare: $("#compareBlockInput"),
  anchor: $("#anchorToggle"),
  anchorStrength: $("#anchorStrengthInput"),
  canvas: $("#previewCanvas"),
  empty: $("#canvasEmpty"),
  latentHash: $("#latentHash"),
  metricBlock: $("#metricBlock"),
  metricBlockSub: $("#metricBlockSub"),
  metricBudget: $("#metricBudget"),
  metricActual: $("#metricActual"),
  metricEstimate: $("#metricEstimate"),
  metricReady: $("#metricReady"),
  metricFrames: $("#metricFrames"),
  metricSnapshot: $("#metricSnapshot"),
  metricConsistency: $("#metricConsistency"),
  metricConsistencySub: $("#metricConsistencySub"),
  consistencyMetric: $("#consistencyMetric"),
  stageClock: $("#stageClock"),
  previewBadge: $("#previewBadge"),
  frameCaption: $("#frameCaption"),
  frameSha: $("#frameSha"),
  frameStrip: $("#frameStrip"),
  lastErrorCode: $("#lastErrorCode"),
  lastErrorMessage: $("#lastErrorMessage"),
  lastErrorDetails: $("#lastErrorDetails"),
  errorList: $("#errorList"),
  arbitration: $("#arbitrationList"),
  degradation: $("#degradationBadge"),
  snapshotTable: $("#snapshotTable"),
  refreshSnapshots: $("#refreshSnapshots"),
  consistencyText: $("#consistencyText"),
  healthLine: $("#healthLine"),
};

function setStatus(kind, text) {
  els.status.className = `status-pill ${kind}`;
  els.statusText.textContent = text;
}

function setBusy(value) {
  state.busy = value;
  [els.run, els.fault, els.budget].forEach((button) => { button.disabled = value; });
}

function setStage(stage, status) {
  const node = $(`.stage[data-stage="${stage}"]`);
  if (!node) return;
  node.classList.remove("active", "done", "failed");
  if (status) node.classList.add(status);
  node.querySelector("b").textContent = {
    active: "处理中",
    done: "完成",
    failed: "失败",
  }[status] || "待机";
}

function resetStages() {
  $$(".stage").forEach((node) => {
    node.classList.remove("active", "done", "failed");
    node.querySelector("b").textContent = "待机";
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { ok: false, error: { code: "E_INTERNAL", message: "无法解析响应", details: {} } };
  }
  if (!response.ok || payload.ok === false) {
    const error = payload.error || { code: "E_INTERNAL", message: "请求失败", details: {} };
    displayError(error);
    throw error;
  }
  return payload;
}

function displayError(error) {
  els.lastErrorCode.textContent = error.code;
  els.lastErrorMessage.textContent = error.message;
  els.lastErrorDetails.textContent = JSON.stringify(error.details || {}, null, 2);
  setStatus("failed", error.code);
}

function displayRecentErrors(errors = []) {
  els.errorList.innerHTML = "";
  errors.slice(0, 6).forEach((error) => {
    const row = document.createElement("div");
    row.className = "error-item";
    row.innerHTML = `<code>${escapeHtml(error.code || "E_INTERNAL")}</code><span>${escapeHtml(error.message || "")}</span>`;
    els.errorList.appendChild(row);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function buildConfig(mode) {
  const budget = mode === "budget" ? 0.001 : Number(els.budgetMs.value);
  const config = {
    prompt: els.prompt.value,
    seed: Number(els.seed.value),
    frames: Number(els.frames.value),
    steps: Number(els.steps.value),
    width: 192,
    height: 108,
    block_size: Number(els.block.value),
    latency_budget_ms: budget,
    max_frames: 240,
    max_chunk_bytes: 67108864,
    max_concurrent_decode_tasks: 4,
    anchor_strength: Number(els.anchorStrength.value),
  };
  if (els.anchor.checked || mode === "anchor_fault") {
    config.anchor_path = "samples/reference.ppm";
  }
  return config;
}

function updateSummary(job) {
  if (!job) return;
  state.jobId = job.job_id;
  state.plan = job.plan || state.plan;
  if (job.generation) {
    setStage("prompt", "done");
    setStage("seed", "done");
    setStage("denoise", "done");
    els.latentHash.textContent = job.generation.latents_sha256 || "—";
    els.stageClock.textContent = `${Number(job.generation.total_ms).toFixed(2)} ms`;
  }
  if (job.plan) {
    const plan = job.plan;
    els.metricBlock.textContent = `${plan.selected_block_size} 帧`;
    els.metricBlockSub.textContent = `请求 ${plan.requested_block_size} · ${plan.num_blocks} 块`;
    els.metricBudget.textContent = `${Number(plan.latency_budget_ms).toFixed(1)} ms`;
    els.metricEstimate.textContent = `估算 ${Number(plan.estimated_first_block_ms).toFixed(3)} ms`;
    els.degradation.textContent = plan.degradations?.length ? plan.degradations.join(" · ") : "NONE";
    renderArbitration(plan);
  }
  if (job.ready) {
    const ready = job.ready;
    els.metricReady.textContent = `${ready.chunk_count} / ${ready.total_chunks}`;
    els.metricFrames.textContent = `${ready.frames} / ${ready.total_frames} 帧可见`;
    els.previewBadge.textContent = ready.chunk_count ? `${ready.frames} FRAMES READY` : "NO FRAME";
  }
  if (job.first_block_actual_ms != null) {
    els.metricActual.textContent = `${Number(job.first_block_actual_ms).toFixed(3)} ms`;
  }
  if (job.snapshot_versions) {
    const versions = job.snapshot_versions;
    els.metricSnapshot.textContent = [versions.decode_params, versions.chunk_plan, versions.ready_blocks]
      .map((value) => value ?? "—").join(" / ");
  }
  if (job.snapshots) renderSnapshots(job.snapshots);
  if (job.consistency?.equal) {
    els.metricConsistency.textContent = "逐字节一致";
    els.metricConsistencySub.textContent = `${job.consistency.bytes_compared.toLocaleString()} bytes · 全量扫描`;
    els.consistencyMetric.classList.remove("failed");
    els.consistencyMetric.classList.add("success");
    els.consistencyText.className = "success";
    els.consistencyText.textContent = `${job.consistency.frames} 帧完全一致 · ${job.consistency.sha256.slice(0, 16)}…`;
  }
  if (job.status === "complete") {
    setStatus("success", "完整视频就绪");
    setStage("decode", "done");
    setStage("verify", "done");
  } else if (job.status === "failed") {
    setStatus("failed", job.last_error?.code || "失败");
  }
}

function renderArbitration(plan) {
  els.arbitration.innerHTML = "";
  const items = [
    ...(plan.arbitration || []),
    {
      rule: "latency_estimate",
      decision: `${Number(plan.estimated_first_block_ms).toFixed(3)} ms`,
      reason: `${plan.estimate_basis?.measured_block_frames} 帧 × ${plan.estimate_basis?.decode_stages} 解码阶段；本地循环实测。`,
    },
  ];
  items.forEach((item) => {
    const node = document.createElement("div");
    node.className = "arbitration-item";
    node.innerHTML = `<code>${escapeHtml(item.rule)}</code><p><strong>${escapeHtml(item.decision)}</strong> · ${escapeHtml(item.reason || "")}</p>`;
    els.arbitration.appendChild(node);
  });
}

function renderSnapshots(snapshots) {
  if (!snapshots) return;
  els.snapshotTable.innerHTML = "";
  Object.entries(snapshots).forEach(([category, value]) => {
    const node = document.createElement("div");
    node.className = "snapshot-row";
    if (value.error) {
      node.innerHTML = `<span>${category}</span><strong>${escapeHtml(value.error)}</strong><code>version=${value.version ?? "unknown"}</code>`;
    } else {
      node.innerHTML = `<span>${category}</span><strong>v${value.version}</strong><code>${escapeHtml(value.checksum)}</code>`;
    }
    els.snapshotTable.appendChild(node);
  });
}

async function refreshState() {
  try {
    const payload = await api("/api/state");
    displayRecentErrors(payload.recent_errors || []);
    const visibleJob = payload.current_job
      || (payload.jobs?.length ? payload.jobs[payload.jobs.length - 1] : null);
    if (visibleJob) updateSummary(visibleJob);
    if (payload.snapshots) renderSnapshots(payload.snapshots);
    if (payload.startup_error) displayError(payload.startup_error);
  } catch {
    // Errors are displayed by api().
  }
}

async function drawFrame(frameIndex, jobId = state.jobId) {
  if (jobId == null || frameIndex == null) return;
  const image = new Image();
  image.onload = () => {
    const canvas = els.canvas;
    const context = canvas.getContext("2d", { alpha: false });
    context.imageSmoothingEnabled = false;
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    $(".canvas-shell").classList.add("has-frame");
    els.frameCaption.textContent = `帧 ${String(frameIndex).padStart(3, "0")}`;
    els.frameSha.textContent = `${image.naturalWidth}×${image.naturalHeight} RGB`;
    state.currentFrame = frameIndex;
    $$(".frame-dot").forEach((dot) => {
      dot.classList.toggle("active", Number(dot.dataset.frame) === frameIndex);
    });
  };
  image.src = `/api/jobs/${encodeURIComponent(jobId)}/frames/${frameIndex}.png?t=${Date.now()}`;
}

function addFrameButtons(block) {
  for (let index = block.start_frame; index < block.end_frame_exclusive; index += 1) {
    if (state.decodedFrames.includes(index)) continue;
    state.decodedFrames.push(index);
    const button = document.createElement("button");
    button.className = "frame-dot";
    button.dataset.frame = index;
    button.textContent = String(index).padStart(2, "0");
    button.title = `查看第 ${index} 帧`;
    button.addEventListener("click", () => drawFrame(index));
    els.frameStrip.appendChild(button);
  }
}

async function renderBlockPreview(block) {
  addFrameButtons(block);
  for (let index = block.start_frame; index < block.end_frame_exclusive; index += 1) {
    await drawFrame(index);
    await new Promise((resolve) => setTimeout(resolve, 70));
  }
}

async function runPipeline(mode = "normal") {
  if (state.busy) return;
  setBusy(true);
  resetStages();
  state.decodedFrames = [];
  els.frameStrip.innerHTML = "";
  $(".canvas-shell").classList.remove("has-frame");
  els.metricConsistency.textContent = "校验中";
  els.consistencyMetric.classList.remove("success", "failed");
  els.consistencyText.textContent = "等待整段解码";
  els.lastErrorCode.textContent = "—";
  els.lastErrorMessage.textContent = "处理中。";
  els.lastErrorDetails.textContent = "error.details = null";
  setStatus("running", mode === "budget" ? "预算压力测试" : "生成中");
  setStage("prompt", "active");

  try {
    const payload = {
      config: buildConfig(mode),
      compare_block_size: Number(els.compare.value),
    };
    if (mode === "anchor_fault") payload.fault_anchor = true;
    const start = await api("/api/jobs", { method: "POST", body: payload });
    const job = start.job;
    updateSummary(job);
    setStage("prompt", "done");
    setStage("seed", "done");
    setStage("denoise", "done");
    setStage("decode", "active");
    setStatus("running", "分块解码");

    for (let index = 0; index < job.plan.num_blocks; index += 1) {
      const chunk = await api(`/api/jobs/${encodeURIComponent(job.job_id)}/chunk`, {
        method: "POST",
        body: { chunk_index: index },
      });
      updateSummary(chunk.job);
      await renderBlockPreview(chunk.block);
    }

    setStage("decode", "done");
    setStage("verify", "active");
    setStatus("running", "整段一致性核验");
    const completion = await api(`/api/jobs/${encodeURIComponent(job.job_id)}/complete`, {
      method: "POST",
      body: { compare_block_size: Number(els.compare.value) },
    });
    updateSummary(completion.job);
    const consistency = completion.consistency;
    const alternative = completion.alternative.consistency;
    els.metricConsistency.textContent = "逐字节一致";
    els.metricConsistencySub.textContent = `${consistency.bytes_compared.toLocaleString()} bytes · 两次分块比对`;
    els.consistencyMetric.classList.add("success");
    els.consistencyText.className = "success";
    els.consistencyText.textContent = `${consistency.frames} 帧完全一致 · 对照块 ${completion.alternative.block_size} · ${completion.whole.sha256.slice(0, 16)}…`;
    if (alternative && !alternative.equal) throw { code: "E_CHUNK_MISMATCH", message: "对照块不一致", details: alternative };
    setStage("verify", "done");
    setStatus("success", "演示完成");
    els.previewBadge.textContent = "COMPLETE VIDEO";
    const link = document.createElement("a");
    link.href = completion.video.raw_url;
    link.download = `${job.job_id}.rgb`;
    link.className = "text-button download-link";
    link.textContent = "下载 RGB";
    els.frameStrip.appendChild(link);
    refreshState();
  } catch (error) {
    setStage("decode", error.code === "E_ANCHOR_MISMATCH" ? "failed" : "done");
    if (error.code === "E_LATENCY_BUDGET") {
      els.metricActual.textContent = error.details?.first_block_actual_ms != null
        ? `${Number(error.details.first_block_actual_ms).toFixed(3)} ms`
        : "未开始";
    }
    displayError(error);
    refreshState();
  } finally {
    setBusy(false);
  }
}

els.run.addEventListener("click", () => runPipeline("normal"));
els.fault.addEventListener("click", () => runPipeline("anchor_fault"));
els.budget.addEventListener("click", () => runPipeline("budget"));
els.reset.addEventListener("click", async () => {
  try {
    await api("/api/reset", { method: "POST", body: {} });
    setStatus("", "计划租约已释放");
  } catch {
    // Errors are displayed by api().
  }
});
els.refreshSnapshots.addEventListener("click", refreshState);

document.addEventListener("keydown", (event) => {
  if (event.key.toLowerCase() === "r" && !event.metaKey && !event.ctrlKey && !state.busy) {
    if (event.target.tagName !== "TEXTAREA" && event.target.tagName !== "INPUT") runPipeline("normal");
  }
});

async function boot() {
  try {
    const health = await api("/api/health");
    els.healthLine.textContent = `NumPy ${health.numpy} · Python ${health.python} · local only`;
  } catch {
    // The dashboard remains usable if health is temporarily unavailable.
  }
  refreshState();
  const versionContext = els.canvas.getContext("2d", { alpha: false });
  versionContext.fillStyle = "#070a10";
  versionContext.fillRect(0, 0, els.canvas.width, els.canvas.height);
}

boot();
