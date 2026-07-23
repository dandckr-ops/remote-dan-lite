const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  status: null,
  runs: [],
};

const tabs = $$('[role="tab"][data-tab]');
const panels = $$('[role="tabpanel"][data-panel]');
const tabNames = tabs.map((tab) => tab.dataset.tab);

function activateTab(name, {updateHash = true, focus = false} = {}) {
  const selectedName = tabNames.includes(name) ? name : "overview";
  tabs.forEach((tab) => {
    const active = tab.dataset.tab === selectedName;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  panels.forEach((panel) => {
    panel.hidden = panel.dataset.panel !== selectedName;
  });
  if (updateHash) {
    history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}#${selectedName}`,
    );
  }
}

function bindTabs() {
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      activateTab(tabs[nextIndex].dataset.tab, {focus: true});
    });
  });

  $$('[data-open-tab]').forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.openTab, {focus: true}));
  });

  window.addEventListener("hashchange", () => {
    activateTab(window.location.hash.slice(1), {updateHash: false});
  });

  activateTab(window.location.hash.slice(1) || "overview", {updateHash: false});
}

function setMessage(text, kind = "") {
  const message = $("#message");
  message.textContent = text;
  message.className = `message ${kind}`.trim();
}

function formatVoltage(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : "--.--";
}

function formatMetric(value, digits = 2) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "—";
}

function formatTimestamp(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  return payload;
}

async function refreshStatus() {
  try {
    const status = await getJson("/api/status");
    state.status = status;
    $("#service-state").classList.add("ready");
    $("#service-state span").textContent = "Capture service ready";
    $("#hostname").textContent = status.hostname;
    $("#service-version").textContent = `v${status.version}`;
    $("#backend").textContent = status.default_backend;
    $("#driver").textContent = status.hardware.driver_available ? "available" : "not installed";
    $("#device").textContent = status.hardware.device_present ? "detected" : "not attached";
    $("#hardware-note").textContent = status.hardware.reason;
  } catch (error) {
    $("#service-state").classList.remove("ready");
    $("#service-state span").textContent = "Service unavailable";
    $("#hardware-note").textContent = error.message;
  }
}

function artifactUrl(run, name) {
  return `/artifacts/${encodeURIComponent(run.run_id)}/${name}`;
}

function setImage(image, emptyState, run) {
  image.src = `${artifactUrl(run, "overview.png")}?v=${encodeURIComponent(run.captured_at)}`;
  image.classList.remove("hidden");
  emptyState.classList.add("hidden");
}

function showLatest(run) {
  if (!run) return;

  setImage($("#overview"), $("#empty-preview"), run);
  setImage($("#can-overview"), $("#can-empty-preview"), run);

  const report = $("#latest-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");

  $("#latest-label").textContent = run.label || "—";
  $("#latest-backend").textContent = (run.backend || "—").toUpperCase();
  $("#latest-window").textContent = run.preset
    ? `${run.preset.toUpperCase()} · ${(run.samples || 0).toLocaleString()} samples`
    : "—";
  $("#latest-time").textContent = formatTimestamp(run.captured_at);

  const summary = run.summary || {};
  const stats = summary.channel_stats || {};
  const vbat = stats.VBAT || {};
  $("#vbat-value").textContent = formatVoltage(vbat.mean);
  $("#vbat-detail").textContent = Number.isFinite(Number(vbat.mean))
    ? `Min ${formatVoltage(vbat.min)} V · max ${formatVoltage(vbat.max)} V · ripple ${formatVoltage(vbat.p2p)} V p-p · raw samples retained`
    : "VBAT statistics are unavailable for this capture; raw artifacts remain unchanged.";

  $("#can-h-mean").textContent = formatMetric(stats["CAN-H"]?.mean);
  $("#can-l-mean").textContent = formatMetric(stats["CAN-L"]?.mean);
  $("#can-diff-p2p").textContent = formatMetric(summary.differential_b_minus_c?.p2p);
  $("#can-common-mean").textContent = formatMetric(summary.common_mode?.mean);
  $("#can-correlation").textContent = formatMetric(summary.can_h_can_l_correlation, 3);
}

function artifactLink(run, name, label) {
  const link = document.createElement("a");
  link.href = artifactUrl(run, name);
  link.textContent = label;
  return link;
}

function renderRuns(runs) {
  const list = $("#run-list");
  if (!runs.length) {
    list.innerHTML = '<p class="empty-list">No evidence packages yet.</p>';
    return;
  }

  list.replaceChildren(...runs.map((run) => {
    const card = document.createElement("article");
    card.className = "run-card";

    const identity = document.createElement("div");
    const label = document.createElement("strong");
    const when = document.createElement("small");
    label.textContent = run.label;
    when.textContent = formatTimestamp(run.captured_at);
    identity.append(label, when);

    const backend = document.createElement("span");
    backend.className = run.backend === "simulator" ? "sim" : "hardware";
    backend.textContent = (run.backend || "unknown").toUpperCase();

    const window = document.createElement("span");
    window.textContent = `${(run.preset || "unknown").toUpperCase()} · ${(run.samples || 0).toLocaleString()} SAMPLES`;

    const links = document.createElement("nav");
    links.setAttribute("aria-label", `Artifacts for ${run.label}`);
    links.append(
      artifactLink(run, "overview.png", "PNG"),
      artifactLink(run, "capture.csv", "CSV"),
      artifactLink(run, "summary.json", "JSON"),
      artifactLink(run, "report.pdf", "PDF"),
    );

    card.append(identity, backend, window, links);
    return card;
  }));
}

function renderTimeline(runs) {
  const list = $("#timeline-list");
  if (!runs.length) {
    list.innerHTML = '<p class="empty-list">No capture events yet.</p>';
    return;
  }

  list.replaceChildren(...runs.map((run, index) => {
    const event = document.createElement("article");
    event.className = "timeline-event";

    const marker = document.createElement("span");
    marker.className = "timeline-marker";
    marker.textContent = String(runs.length - index).padStart(2, "0");

    const body = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    title.textContent = run.label;
    detail.textContent = `${formatTimestamp(run.captured_at)} · ${(run.backend || "unknown").toUpperCase()} · ${(run.preset || "unknown").toUpperCase()}`;
    body.append(title, detail);

    const link = artifactLink(run, "manifest.json", "Manifest");
    event.append(marker, body, link);
    return event;
  }));
}

async function refreshRuns() {
  try {
    const runs = await getJson("/api/captures");
    state.runs = runs;
    renderRuns(runs);
    renderTimeline(runs);
    if (runs.length) showLatest(runs[0]);
  } catch (error) {
    setMessage(`Could not load evidence list: ${error.message}`, "error");
  }
}

function bindCaptureForm() {
  $("#capture-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#capture-button");
    button.disabled = true;
    setMessage("Capture armed. Acquiring and packaging evidence…");
    try {
      const run = await getJson("/api/captures", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          label: $("#label").value,
          preset: $("#preset").value,
          mode: $("#mode").value,
          capture_type: "scope",
        }),
      });
      setMessage(`${run.run_id} completed. Shared evidence views are updated.`, "success");
      await refreshRuns();
    } catch (error) {
      setMessage(`Capture failed: ${error.message}`, "error");
    } finally {
      button.disabled = false;
    }
  });
}

bindTabs();
bindCaptureForm();
$("#refresh").addEventListener("click", refreshRuns);
refreshStatus();
refreshRuns();
setInterval(refreshStatus, 15_000);
