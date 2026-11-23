DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tunnel Workbench</title>
  <style>
    :root { color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    body { margin: 0; background: #10151c; color: #d9e2ec; }
    header { padding: 18px 24px; border-bottom: 1px solid #263241; background: #121923; }
    h1 { margin: 0; font-size: 20px; letter-spacing: .04em; }
    main { padding: 20px 24px 40px; }
    section { margin-bottom: 24px; }
    h2 { font-size: 14px; color: #8fb3d9; text-transform: uppercase; letter-spacing: .08em; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #263241; text-align: left; vertical-align: top; }
    th { color: #7e93aa; font-weight: 600; }
    .ok { color: #67d391; }
    .bad { color: #ff7b72; }
    .warn { color: #e3b341; }
    .muted { color: #7e93aa; }
    #meta { margin-top: 6px; font-size: 12px; color: #7e93aa; }
    pre { white-space: pre-wrap; background: #0b1016; border: 1px solid #263241; padding: 12px; max-height: 360px; overflow: auto; }
  </style>
</head>
<body>
  <header>
    <h1>Tunnel Workbench</h1>
    <div id="meta">Loading diagnostics...</div>
  </header>
  <main>
    <section>
      <h2>Tunnel Watermarks</h2>
      <table>
        <thead><tr>
          <th>Tunnel</th><th>Status</th><th>Outcome</th><th>Watermark</th>
          <th>In-flight</th><th>MTU</th><th>Retrans</th><th>Profile</th><th>Business code</th>
        </tr></thead>
        <tbody id="tunnels"></tbody>
      </table>
    </section>
    <section>
      <h2>Verification</h2>
      <div id="verify" class="muted">Waiting...</div>
      <pre id="anomalies"></pre>
    </section>
  </main>
  <script>
    const escapeHtml = (value) => String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
    const intervalText = (intervals) => (intervals || [])
      .map(([start, end]) => start === end ? String(start) : `${start}-${end}`).join(", ") || "-";
    async function refresh() {
      try {
        const [statusResponse, verifyResponse] = await Promise.all([
          fetch("/api/status"), fetch("/api/verify")
        ]);
        const status = await statusResponse.json();
        const verify = await verifyResponse.json();
        const rows = (status.tunnels || []).map((tunnel) => {
          const statusClass = tunnel.status === "COMPLETED" ? "ok"
            : (tunnel.status === "FAILED" ? "bad" : "warn");
          return `<tr>
            <td>${escapeHtml(tunnel.tunnel)}</td>
            <td class="${statusClass}">${escapeHtml(tunnel.status)}</td>
            <td>${escapeHtml(tunnel.outcome)}</td>
            <td>${escapeHtml(tunnel.fragment_watermark)}</td>
            <td>${escapeHtml(intervalText(tunnel.inflight_intervals))}</td>
            <td>${escapeHtml(tunnel.current_mtu)}</td>
            <td>${escapeHtml(tunnel.retransmission_count)}</td>
            <td>${escapeHtml(tunnel.profile_version)} / bound ${escapeHtml(tunnel.bound_profile_version)}</td>
            <td>${escapeHtml(tunnel.business_code)}</td>
          </tr>`;
        }).join("");
        document.getElementById("tunnels").innerHTML = rows ||
          '<tr><td colspan="9" class="muted">No tunnels loaded.</td></tr>';
        const verifyNode = document.getElementById("verify");
        verifyNode.className = verify.ok ? "ok" : "bad";
        verifyNode.textContent = verify.ok
          ? `ok=true · checked_runs=${verify.checked_runs} · anomalies=0`
          : `ok=false · anomalies=${(verify.anomalies || []).length}`;
        document.getElementById("anomalies").textContent =
          JSON.stringify(verify.anomalies || [], null, 2);
        document.getElementById("meta").textContent =
          `${status.tunnels ? status.tunnels.length : 0} tunnels · stable JSON source: /api/status`;
      } catch (error) {
        document.getElementById("meta").textContent = `Diagnostics error: ${error}`;
      }
    }
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""

