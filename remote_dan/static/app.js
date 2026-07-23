const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  status: null,
  runs: [],
  scopeCatalog: null,
};

const tabs = $$('[role="tab"][data-tab]');
const panels = $$('[role="tabpanel"][data-panel]');
const tabNames = tabs.map((tab) => tab.dataset.tab);
const channelLetters = ["A", "B", "C", "D"];

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
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${selectedName}`);
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

function setMessage(selector, text, kind = "") {
  const message = $(selector);
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

function formatRange(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  if (numeric < 1) return `${Math.round(numeric * 1000)} mV`;
  return `${Number.isInteger(numeric) ? numeric.toFixed(0) : numeric.toFixed(2)} V`;
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

function channelControl(letter, name) {
  return $(`#scope-channel-${letter}-${name}`);
}

function updateChannelRow(letter) {
  const row = $(`[data-scope-channel="${letter}"]`);
  const enabled = channelControl(letter, "enabled").checked;
  const inputRange = Number(channelControl(letter, "range").value);
  const attenuation = Number(channelControl(letter, "attenuation").value);
  if (attenuation === 20 && inputRange > 20) {
    channelControl(letter, "range").value = "20";
  }
  const scaledRange = Number(channelControl(letter, "range").value) * attenuation;
  row.classList.toggle("disabled", !enabled);
  channelControl(letter, "external").textContent = enabled
    ? `±${formatRange(scaledRange)} scaled`
    : "Disabled";
}

function setChannelControls(config) {
  const letter = config.channel;
  channelControl(letter, "enabled").checked = Boolean(config.enabled);
  channelControl(letter, "label").value = config.label;
  channelControl(letter, "range").value = String(Number(config.input_range_v));
  channelControl(letter, "attenuation").value = String(Number(config.attenuation));
  channelControl(letter, "coupling").value = config.coupling;
  updateChannelRow(letter);
}

function applyScopeProfile(name) {
  if (!state.scopeCatalog) return;
  const profile = state.scopeCatalog.profiles.find((item) => item.name === name);
  if (!profile) return;
  $("#scope-profile").value = profile.name;
  $("#scope-window").value = profile.preset;
  $("#scope-profile-description").textContent = profile.description;
  const warning = $("#scope-profile-warning");
  warning.textContent = profile.warning || "";
  warning.classList.toggle("hidden", !profile.warning);
  $$('[data-scope-profile]').forEach((button) => {
    const selected = button.dataset.scopeProfile === profile.name;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  profile.channels.forEach(setChannelControls);
  $("#scope-label").value = `${profile.label.toLowerCase()} capture`;
  setMessage("#scope-message", "Profile loaded. Verify every range, probe, coupling, and connection before arming.");
}

function collectScopeChannels() {
  return channelLetters.map((letter) => ({
    channel: letter,
    enabled: channelControl(letter, "enabled").checked,
    label: channelControl(letter, "label").value.trim(),
    input_range_v: Number(channelControl(letter, "range").value),
    attenuation: Number(channelControl(letter, "attenuation").value),
    coupling: channelControl(letter, "coupling").value,
  }));
}

function suggestScopeRange(config, stats, overflow, availableRanges) {
  const currentRange = Number(config.input_range_v);
  const attenuation = Number(config.attenuation) || 1;
  const peakEngineering = Math.max(Math.abs(Number(stats?.min)), Math.abs(Number(stats?.max)));
  if (!Number.isFinite(peakEngineering)) return currentRange;
  const peakAtScope = peakEngineering / attenuation;
  let target = peakAtScope * 1.25;
  if (overflow || peakAtScope >= currentRange * 0.98) {
    target = Math.max(target, currentRange * 1.25);
  }
  const maximum = availableRanges.at(-1);
  const allowed = availableRanges.filter((value) => value <= maximum);
  return allowed.find((value) => value >= target) || allowed.at(-1);
}

function autoScaleFromLatestScope() {
  if (!state.scopeCatalog) return;
  const run = state.runs.find((item) => item.profile && item.profile !== "network" && item.capture_type === "scope");
  if (!run) {
    setMessage("#scope-message", "Auto-scale needs one completed Scope capture first.", "error");
    return;
  }
  applyScopeProfile(run.profile);
  const stats = run.summary?.channel_stats || {};
  const overflow = new Set(run.overflow_channels || run.summary?.overflow_channels || []);
  run.scope_config.forEach((config) => {
    setChannelControls(config);
    if (!config.enabled) return;
    const suggested = suggestScopeRange(
      config,
      stats[config.label],
      overflow.has(config.channel),
      state.scopeCatalog.input_ranges_v,
    );
    channelControl(config.channel, "range").value = String(suggested);
    updateChannelRow(config.channel);
  });
  setMessage(
    "#scope-message",
    "Next-capture ranges suggested from the latest peaks with 25% headroom. Review them before arming.",
    "success",
  );
}

async function loadScopeProfiles() {
  try {
    state.scopeCatalog = await getJson("/api/scope/profiles");
    applyScopeProfile($("#scope-profile").value || "general");
  } catch (error) {
    setMessage("#scope-message", `Could not load Scope profiles: ${error.message}`, "error");
  }
}

function bindScopeControls() {
  $$('[data-scope-profile]').forEach((button) => {
    button.addEventListener("click", () => applyScopeProfile(button.dataset.scopeProfile));
  });
  channelLetters.forEach((letter) => {
    ["enabled", "range", "attenuation"].forEach((name) => {
      channelControl(letter, name).addEventListener("change", () => updateChannelRow(letter));
    });
    updateChannelRow(letter);
  });
  $("#scope-reset-profile").addEventListener("click", () => applyScopeProfile($("#scope-profile").value));
  $("#scope-apply-20x").addEventListener("click", () => {
    channelLetters.forEach((letter) => {
      if (!channelControl(letter, "enabled").checked) return;
      channelControl(letter, "attenuation").value = "20";
      if (Number(channelControl(letter, "range").value) > 20) {
        channelControl(letter, "range").value = "20";
      }
      updateChannelRow(letter);
    });
    setMessage("#scope-message", "20:1 scaling applied to enabled channels. Verify the physical attenuator and its rating.");
  });
  $("#scope-autoscale").addEventListener("click", autoScaleFromLatestScope);
}

function artifactUrl(run, name) {
  return `/artifacts/${encodeURIComponent(run.run_id)}/${name}`;
}

function setImage(image, emptyState, run) {
  image.src = `${artifactUrl(run, "overview.png")}?v=${encodeURIComponent(run.captured_at)}`;
  image.classList.remove("hidden");
  emptyState.classList.add("hidden");
  image.closest(".scope-grid").classList.add("has-image");
}

function isNetworkRun(run) {
  const stats = run.summary?.channel_stats || {};
  return run.profile === "network" || (stats.VBAT && stats["CAN-H"] && stats["CAN-L"]);
}

function isScopeRun(run) {
  return run.profile && run.profile !== "network" && run.capture_type === "scope";
}

function showLatestMetadata(run) {
  if (!run) return;
  $("#latest-label").textContent = run.label || "—";
  $("#latest-profile").textContent = (run.profile || (isNetworkRun(run) ? "network" : "legacy")).toUpperCase();
  $("#latest-backend").textContent = (run.backend || "—").toUpperCase();
  $("#latest-window").textContent = run.preset
    ? `${run.preset.toUpperCase()} · ${(run.samples || 0).toLocaleString()} samples`
    : "—";
  $("#latest-time").textContent = formatTimestamp(run.captured_at);
}

function showNetwork(run) {
  if (!run) return;
  const previewChannels = run.summary?.preview_channels || [];
  const canOnlyPreview = previewChannels.length === 2
    && previewChannels[0] === "CAN-H"
    && previewChannels[1] === "CAN-L";
  if (canOnlyPreview) {
    setImage($("#can-overview"), $("#can-empty-preview"), run);
  } else {
    $("#can-overview").classList.add("hidden");
    $("#can-empty-preview").classList.remove("hidden");
    $("#can-empty-preview strong").textContent = "Legacy network preview withheld";
    $("#can-empty-preview span").textContent = "Its image may include a VBAT waveform. Run a new network capture for CAN-H/CAN-L-only evidence.";
  }
  const report = $("#can-latest-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");

  const summary = run.summary || {};
  const stats = summary.channel_stats || {};
  const vbat = stats.VBAT || {};
  $("[data-vbat-value]").textContent = formatVoltage(vbat.mean);
  $("[data-vbat-detail]").textContent = Number.isFinite(Number(vbat.mean))
    ? `Min ${formatVoltage(vbat.min)} V · max ${formatVoltage(vbat.max)} V · ripple ${formatVoltage(vbat.p2p)} V p-p · raw samples retained`
    : "VBAT statistics are unavailable; raw artifacts remain unchanged.";
  $("#can-h-mean").textContent = formatMetric(stats["CAN-H"]?.mean);
  $("#can-l-mean").textContent = formatMetric(stats["CAN-L"]?.mean);
  $("#can-diff-p2p").textContent = formatMetric(summary.differential_b_minus_c?.p2p);
  $("#can-common-mean").textContent = formatMetric(summary.common_mode?.mean);
  $("#can-correlation").textContent = formatMetric(summary.can_h_can_l_correlation, 3);
}

function showScope(run) {
  if (!run) return;
  setImage($("#scope-overview"), $("#scope-empty-preview"), run);
  const report = $("#scope-latest-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");

  const config = (run.scope_config || []).find((item) => item.enabled);
  const stats = config ? run.summary?.channel_stats?.[config.label] : null;
  if (!config || !stats) return;
  const peak = Math.max(Math.abs(Number(stats.min)), Math.abs(Number(stats.max)));
  const overflow = (run.overflow_channels || []).includes(config.channel);
  $("#scope-primary-label").textContent = `${config.channel} · ${config.label} peak`;
  $("#scope-primary-value").textContent = formatVoltage(peak);
  $("#scope-primary-detail").textContent = `${overflow ? "OVERFLOW · " : ""}Min ${formatVoltage(stats.min)} V · max ${formatVoltage(stats.max)} V · ${formatVoltage(stats.p2p)} V p-p · ±${formatRange(config.external_range_v)} scaled`;
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
    window.textContent = `${(run.profile || "legacy").toUpperCase()} · ${(run.preset || "unknown").toUpperCase()} · ${(run.samples || 0).toLocaleString()} SAMPLES`;
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
    detail.textContent = `${formatTimestamp(run.captured_at)} · ${(run.backend || "unknown").toUpperCase()} · ${(run.profile || "legacy").toUpperCase()} · ${(run.preset || "unknown").toUpperCase()}`;
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
    showLatestMetadata(runs[0]);
    showNetwork(runs.find(isNetworkRun));
    showScope(runs.find(isScopeRun));
  } catch (error) {
    setMessage("#scope-message", `Could not load evidence list: ${error.message}`, "error");
    setMessage("#can-message", `Could not load evidence list: ${error.message}`, "error");
  }
}

function bindScopeCaptureForm() {
  $("#scope-capture-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#scope-capture-button");
    const channels = collectScopeChannels();
    if (!channels.some((channel) => channel.enabled)) {
      setMessage("#scope-message", "Enable at least one Scope channel.", "error");
      return;
    }
    button.disabled = true;
    setMessage("#scope-message", "Scope armed. Acquiring and packaging evidence…");
    try {
      const run = await getJson("/api/captures", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          label: $("#scope-label").value,
          preset: $("#scope-window").value,
          mode: $("#scope-mode").value,
          capture_type: "scope",
          profile: $("#scope-profile").value,
          channels,
        }),
      });
      setMessage("#scope-message", `${run.run_id} completed. Scope evidence is updated.`, "success");
      await refreshRuns();
    } catch (error) {
      setMessage("#scope-message", `Scope capture failed: ${error.message}`, "error");
    } finally {
      button.disabled = false;
    }
  });
}

function bindCanCaptureForm() {
  $("#can-capture-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#can-capture-button");
    button.disabled = true;
    setMessage("#can-message", "Network capture armed. Acquiring VBAT, CAN-H, and CAN-L…");
    try {
      const run = await getJson("/api/captures", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          label: $("#can-label").value,
          preset: $("#can-window").value,
          mode: $("#can-mode").value,
          capture_type: "can",
          profile: "network",
        }),
      });
      setMessage("#can-message", `${run.run_id} completed. CAN evidence is updated.`, "success");
      await refreshRuns();
    } catch (error) {
      setMessage("#can-message", `Network capture failed: ${error.message}`, "error");
    } finally {
      button.disabled = false;
    }
  });
}

bindTabs();
bindScopeControls();
bindScopeCaptureForm();
bindCanCaptureForm();
$("#refresh").addEventListener("click", refreshRuns);
loadScopeProfiles();
refreshStatus();
refreshRuns();
setInterval(refreshStatus, 15_000);
