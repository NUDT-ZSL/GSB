const $ = (selector) => document.querySelector(selector);
const state = { busy: false };

async function call(path, body = null) {
  if (state.busy) return;
  state.busy = true;
  document.querySelectorAll("button").forEach((button) => (button.disabled = true));
  $("#request-state").textContent = "running";
  try {
    const response = await fetch(path, {
      method: body === null ? "GET" : "POST",
      headers: body === null ? {} : { "Content-Type": "application/json" },
      body: body === null ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    $("#output").textContent = JSON.stringify(data, null, 2);
    if (data.status) renderStatus(data.status);
    else if (data.schema === "alertlab.status/v1") renderStatus(data);
    $("#request-state").textContent = response.ok ? "complete" : `http ${response.status}`;
    if (!response.ok) $("#request-state").textContent = data.error?.code || `http ${response.status}`;
  } catch (error) {
    $("#output").textContent = String(error);
    $("#request-state").textContent = "network error";
  } finally {
    state.busy = false;
    document.querySelectorAll("button").forEach((button) => (button.disabled = false));
  }
}

function renderStatus(status) {
  if (!status?.loaded) {
    $("#m-view").textContent = "—";
    $("#m-clusters").textContent = "—";
    $("#m-candidates").textContent = "—";
    $("#m-late").textContent = "—";
    $("#clusters").innerHTML = '<p class="empty">尚未载入规则快照。</p>';
    return;
  }
  $("#m-view").textContent = `v${status.rule_view.view_id}`;
  $("#m-clusters").textContent = status.summary.cluster_count;
  $("#m-candidates").textContent = status.summary.candidate_count;
  $("#m-late").textContent = status.summary.late_event_count;
  $("#result-rev").textContent = `revision ${status.result_revision}`;
  const container = $("#clusters");
  container.innerHTML = "";
  if (!status.clusters.length) {
    container.innerHTML = '<p class="empty">当前视图没有事件簇。</p>';
    return;
  }
  const template = $("#cluster-template");
  for (const cluster of status.clusters) {
    const node = template.content.cloneNode(true);
    node.querySelector(".severity").classList.add(cluster.representative.severity);
    node.querySelector(".rule").textContent = cluster.rule_name;
    node.querySelector(".service").textContent = cluster.service;
    node.querySelector(".cluster-id").textContent = cluster.cluster_id.slice(0, 12);
    node.querySelector(".cluster-stats").textContent =
      `代表 seq=${cluster.representative.seq} / 实例 ${cluster.representative.instance_id} · ` +
      `${cluster.occurrence_count} 条告警 · 抑制 ${cluster.suppressed_count} 条`;
    const table = node.querySelector(".candidate-table");
    cluster.root_candidates.forEach((candidate) => {
      const row = document.createElement("div");
      row.className = "candidate";
      row.innerHTML =
        `<span class="rank">#${candidate.rank}</span>` +
        `<span>${candidate.candidate_id}</span>` +
        `<span class="path">${candidate.dependency_path.join(" → ")}</span>` +
        `<span class="time">${candidate.time_diff_ms === null ? "no signal" : candidate.time_diff_ms + "ms"}</span>`;
      table.appendChild(row);
    });
    container.appendChild(node);
  }
}

$("#load").addEventListener("click", () => {
  const body = {
    v1_path: $("#v1-path").value,
    v2_path: $("#v2-path").value,
    topology_path: $("#topology-path").value,
    events_path: $("#events-path").value,
  };
  if ($("#clock").value.trim()) body.logical_clock_ms = Number($("#clock").value.trim());
  call("/load", body);
});
$("#patch-dep").addEventListener("click", () => call("/patch", { patch_file: "samples/patches/dependency_patch.json" }));
$("#patch-combined").addEventListener("click", () => call("/patch", { patch_file: "samples/patches/combined_patch.json" }));
$("#verify").addEventListener("click", () => call("/verify"));
$("#reset").addEventListener("click", () => call("/reset", {}));
call("/status");
