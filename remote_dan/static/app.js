const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  status: null,
  runs: [],
  scopeCatalog: null,
  usbRouting: {
    applying: false,
    available: false,
    devices: {},
    initialRoutes: {},
    inventoryRevision: null,
    routes: {},
  },
  canDecode: null,
  canComparison: null,
};
const canDecodeRequestGate = CanRequestGate.createLatestRequestGate();
const canComparisonRequestGate = CanRequestGate.createLatestRequestGate();
let canFilterTimer = null;

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

function formatBitrate(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return "—";
  if (numeric >= 1_000_000) {
    const megabits = numeric / 1_000_000;
    return `${Number.isInteger(megabits) ? megabits.toFixed(0) : megabits.toFixed(2)} Mbit/s`;
  }
  const kilobits = numeric / 1000;
  return `${Number.isInteger(kilobits) ? kilobits.toFixed(0) : kilobits.toFixed(1)} kbit/s`;
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
    const serial = status.serial_hardware || {};
    $("#serial-device-status").textContent = serial.device_present ? "C662 ready" : "C662 absent";
    $("#serial-device-status").className = `status-label ${serial.device_present ? "live" : "error"}`;
  } catch (error) {
    $("#service-state").classList.remove("ready");
    $("#service-state span").textContent = "Service unavailable";
    $("#hardware-note").textContent = error.message;
  }
}

const usbRouteLabels = {
  local: "Local to Remote Dan Lite",
  virtualhere: "Forward through VirtualHere",
};

function setUsbRoutingMessage(text = "", kind = "") {
  setMessage("#usb-routing-message", text, kind);
}

function usbRoutingChanges() {
  const routing = state.usbRouting;
  return Object.entries(routing.routes)
    .filter(([key, route]) => routing.initialRoutes[key] !== route)
    .map(([key, route]) => ({
      key,
      name: routing.devices[key] || key,
      from: routing.initialRoutes[key],
      to: route,
    }));
}

function updateUsbRoutingControls() {
  const routing = state.usbRouting;
  const disabled = !routing.available || routing.applying;
  $$("#usb-routing-list select").forEach((route) => {
    route.disabled = disabled;
  });
  $("#usb-routing-apply").disabled = disabled || usbRoutingChanges().length === 0;
}

function usbRouteValue(device) {
  return device.route === "virtualhere" ? "virtualhere" : "local";
}

async function applyUsbRouting() {
  const routing = state.usbRouting;
  const changes = usbRoutingChanges();
  if (!changes.length) {
    setUsbRoutingMessage("No USB routing changes to apply.");
    return;
  }
  const changeSummary = changes.map(({name, key, from, to}) => (
    `${name} (${key})\n  ${usbRouteLabels[from]} → ${usbRouteLabels[to]}`
  )).join("\n\n");
  if (!window.confirm(`Apply USB routing changes?\n\n${changeSummary}`)) {
    setUsbRoutingMessage("USB routing changes were not applied.");
    return;
  }

  routing.applying = true;
  updateUsbRoutingControls();
  setUsbRoutingMessage("Applying USB routing changes…");
  try {
    await getJson("/api/usb/routing/apply", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        inventory_revision: routing.inventoryRevision,
        routes: routing.routes,
        confirmed: true,
      }),
    });
    await loadUsbRouting();
    setUsbRoutingMessage("USB routing changes applied and inventory refreshed.", "success");
  } catch (error) {
    setUsbRoutingMessage(`USB routing failed: ${error.message}`, "error");
  } finally {
    state.usbRouting.applying = false;
    updateUsbRoutingControls();
  }
}

async function loadUsbRouting() {
  const list = $("#usb-routing-list");
  const status = $("#usb-routing-status");
  try {
    const inventory = await getJson("/api/usb/devices");
    const control = inventory.routing_control || {};
    const devices = inventory.devices || [];
    const available = Boolean(control.available && control.inventory_revision);
    state.usbRouting = {
      applying: false,
      available,
      devices: Object.fromEntries(devices.map((device) => [
        device.key,
        device.product_name || `${device.vendor_id}:${device.product_id}`,
      ])),
      initialRoutes: Object.fromEntries(devices.map((device) => [device.key, usbRouteValue(device)])),
      inventoryRevision: control.inventory_revision || null,
      routes: Object.fromEntries(devices.map((device) => [device.key, usbRouteValue(device)])),
    };
    status.textContent = available ? "Routing ready" : "Read-only inventory";
    status.className = `status-label ${available ? "live" : "planned"}`;
    list.replaceChildren(...devices.map((device) => {
      const row = document.createElement("article");
      row.className = "usb-routing-device";
      const identity = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = device.product_name || `${device.vendor_id}:${device.product_id}`;
      const detail = document.createElement("small");
      detail.textContent = [
        `${device.vendor_id}:${device.product_id}`,
        device.serial ? `S/N ${device.serial}` : "no serial",
        device.topology_path,
      ].join(" · ");
      identity.append(name, detail);
      const route = document.createElement("select");
      route.setAttribute("aria-label", `Routing for ${name.textContent}`);
      route.dataset.usbRouteKey = device.key;
      route.disabled = !available;
      [
        ["local", "Local to Remote Dan Lite"],
        ["virtualhere", "Forward through VirtualHere"],
      ].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        route.append(option);
      });
      route.value = state.usbRouting.routes[device.key];
      route.addEventListener("change", () => {
        state.usbRouting.routes[device.key] = route.value;
        updateUsbRoutingControls();
      });
      row.append(identity, route);
      return row;
    }));
    if (!devices.length) {
      list.textContent = "No USB devices detected.";
    }
    if (!available) {
      $("#usb-routing-apply").title = control.reason || "VirtualHere routing is not commissioned.";
    }
    updateUsbRoutingControls();
  } catch (error) {
    status.textContent = "Inventory unavailable";
    status.className = "status-label error";
    list.textContent = `USB inventory failed: ${error.message}`;
    state.usbRouting = {...state.usbRouting, available: false};
    updateUsbRoutingControls();
    setUsbRoutingMessage(`USB inventory failed: ${error.message}`, "error");
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
  const countUnit = isSerialRun(run) ? "bytes" : (isModbusRun(run) ? "hosts" : "samples");
  $("#latest-window").textContent = run.preset
    ? `${run.preset.toUpperCase()} · ${(run.samples || 0).toLocaleString()} ${countUnit}`
    : "—";
  $("#latest-time").textContent = formatTimestamp(run.captured_at);
}

function isBusSurveyRun(run) {
  return run?.capture_type === "bus_survey" || run?.profile === "bus-sniffer";
}

function showBusSurvey(run) {
  if (!run) return;
  const classification = run.summary?.classification;
  if (!classification) return;
  $("#sniffer-topology").textContent = classification.electrical_topology || "—";
  $("#sniffer-family").textContent = classification.family || "—";
  $("#sniffer-rate").textContent = classification.candidate_bitrate_bps
    ? formatBitrate(classification.candidate_bitrate_bps)
    : "Unresolved";
  $("#sniffer-confidence").textContent = (classification.confidence || "none").toUpperCase();
  $("#sniffer-workspace").textContent = classification.workspace || "Bus Sniffer";
  $("#sniffer-device").textContent = classification.input_device || "No recommendation";
  $("#sniffer-reason").textContent = classification.reason || "No defensible classification.";
  $("#sniffer-boundary").textContent = classification.boundary || "Remain passive.";
  const status = $("#sniffer-status");
  const classificationStatus = classification.status || "unresolved";
  status.textContent = classificationStatus === "classified" ? "Classified" : classificationStatus.replaceAll("_", " ");
  status.className = `status-label ${classificationStatus === "classified" ? "live" : "error"}`;
  setImage($("#sniffer-overview"), $("#sniffer-empty-preview"), run);
  const report = $("#sniffer-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");

  const open = $("#sniffer-open-tab");
  const target = classification.workspace;
  const routable = ["medium", "high"].includes(classification.confidence)
    && ["scope", "serial", "can", "modbus"].includes(target);
  open.classList.toggle("hidden", !routable);
  open.dataset.targetTab = routable ? target : "";
  if (target === "serial" && classification.candidate_bitrate_bps) {
    const option = [...$("#serial-baud").options].find(
      (item) => Number(item.value) === Number(classification.candidate_bitrate_bps),
    );
    if (option) $("#serial-baud").value = option.value;
  }
}

function bindBusSurveyForm() {
  const harness = $("#sniffer-harness");
  const mode = $("#sniffer-mode");
  const button = $("#sniffer-button");
  const safetyChecks = [
    $("#sniffer-low-voltage"),
    $("#sniffer-common-reference"),
    $("#sniffer-probe-rating"),
    $("#sniffer-passive-only"),
  ];
  const updateHarnessGate = () => {
    const verified = harness.value !== "unverified";
    const hardwareSafe = mode.value !== "hardware" || safetyChecks.every((item) => item.checked);
    button.disabled = !verified || !hardwareSafe;
    if (!verified) {
      setMessage("#sniffer-message", "Select the exact commissioned or protected harness. Software cannot make an unknown ground connection safe.");
    } else if (!hardwareSafe) {
      setMessage("#sniffer-message", "Hardware capture is blocked until all four electrical-safety attestations are recorded.", "error");
    } else {
      setMessage("#sniffer-message", mode.value === "hardware" ? "Harness and safety attestations recorded. Survey remains receive-only." : "Simulator selected; no physical signal connection is used.");
    }
  };
  harness.addEventListener("change", updateHarnessGate);
  mode.addEventListener("change", updateHarnessGate);
  safetyChecks.forEach((item) => item.addEventListener("change", updateHarnessGate));
  $("#sniffer-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (harness.value === "unverified") {
      updateHarnessGate();
      return;
    }
    button.disabled = true;
    setMessage("#sniffer-message", "Collecting fast, context, and sparse passive windows…");
    try {
      const run = await getJson("/api/bus-surveys", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          label: $("#sniffer-label").value,
          harness: harness.value,
          mode: mode.value,
          low_voltage_confirmed: safetyChecks[0].checked,
          common_reference_confirmed: safetyChecks[1].checked,
          probe_rating_confirmed: safetyChecks[2].checked,
          passive_only_confirmed: safetyChecks[3].checked,
        }),
      });
      const classification = run.summary?.classification || {};
      setMessage("#sniffer-message", `${run.run_id} completed. ${classification.family || "Bus unresolved"} · ${(classification.confidence || "none").toUpperCase()} confidence.`, "success");
      await refreshRuns();
    } catch (error) {
      setMessage("#sniffer-message", `Bus survey failed: ${error.message}`, "error");
    } finally {
      updateHarnessGate();
    }
  });
  $("#sniffer-open-tab").addEventListener("click", () => {
    const target = $("#sniffer-open-tab").dataset.targetTab;
    if (target) activateTab(target, {focus: true});
  });
  updateHarnessGate();
}

function isModbusRun(run) {
  return run?.capture_type === "modbus_scan" || run?.profile === "modbus";
}

function showModbus(run) {
  if (!run) return;
  const summary = run.summary || {};
  $("#modbus-device-count").textContent = Number(summary.device_count || 0).toLocaleString();
  $("#modbus-confirmed-count").textContent = Number(summary.confirmed_modbus_count || 0).toLocaleString();
  $("#modbus-anybus-count").textContent = Number(summary.anybus_count || 0).toLocaleString();
  $("#modbus-write-count").textContent = Number(summary.writes_performed || 0).toLocaleString();
  setImage($("#modbus-overview"), $("#modbus-empty-preview"), run);
  const report = $("#modbus-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");

  const devices = summary.devices || [];
  const list = $("#modbus-device-list");
  if (!devices.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No Modbus or Anybus identity was observed in this bounded scan.";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...devices.map((device) => {
    const card = document.createElement("article");
    card.className = "modbus-device-card";
    const heading = document.createElement("div");
    const ip = document.createElement("strong");
    const kind = document.createElement("span");
    ip.textContent = device.ip || "unknown address";
    kind.textContent = device.kind === "anybus_hicp" ? "ANYBUS / HICP" : "MODBUS TCP";
    heading.append(ip, kind);
    const identity = document.createElement("p");
    identity.textContent = [
      device.vendor_name,
      device.product_code || device.fieldbus_type,
      device.revision || device.module_version,
    ].filter(Boolean).join(" · ") || "Identity unavailable";
    const facts = document.createElement("small");
    facts.textContent = [
      device.mac,
      device.port ? `TCP/${device.port}` : null,
      device.unit_id !== undefined ? `Unit ${device.unit_id}` : null,
      device.state,
      `${device.confidence || "unknown"} confidence`,
    ].filter(Boolean).join(" · ");
    card.append(heading, identity, facts);
    return card;
  }));
}

async function loadModbusNetworks() {
  const select = $("#modbus-subnet");
  const button = $("#modbus-scan-button");
  try {
    const inventory = await getJson("/api/modbus/networks");
    const networks = inventory.networks || [];
    select.replaceChildren(...networks.map((item) => {
      const option = document.createElement("option");
      option.value = item.network;
      option.dataset.interface = item.interface;
      option.textContent = `${item.interface} · ${item.network} · local ${item.address}`;
      return option;
    }));
    button.disabled = networks.length === 0;
    if (!networks.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No connected private IPv4 network";
      select.append(option);
      setMessage("#modbus-message", "No connected IPv4 scan scope is available. Routed or arbitrary targets are not accepted.", "error");
    }
  } catch (error) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Network inventory unavailable";
    select.replaceChildren(option);
    button.disabled = true;
    setMessage("#modbus-message", `Network inventory failed: ${error.message}`, "error");
  }
}

function isSerialRun(run) {
  return run?.capture_type === "serial" || run?.profile === "serial";
}

function showSerial(run) {
  if (!run) return;
  const analysis = run.summary?.serial_analysis;
  setImage($("#serial-overview"), $("#serial-empty-preview"), run);
  const report = $("#serial-latest-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");
  if (!analysis) return;

  const errors = analysis.receiver_errors || {};
  const errorTotal = Object.values(errors).reduce((sum, value) => sum + (Number(value) || 0), 0);
  const protocol = analysis.protocol || {};
  $("#serial-byte-count").textContent = Number(analysis.byte_count || 0).toLocaleString();
  $("#serial-byte-rate").textContent = formatMetric(analysis.bytes_per_second, 1);
  $("#serial-framing").textContent = analysis.framing?.label || "—";
  $("#serial-protocol").textContent = protocol.name || "Higher layer unresolved";
  $("#serial-valid-frames").textContent = Number(protocol.valid_frame_count || 0).toLocaleString();
  $("#serial-printable").textContent = `${formatMetric(analysis.printable_percent, 1)}%`;
  $("#serial-errors").textContent = errorTotal.toLocaleString();
  $("#serial-confidence").textContent = (protocol.confidence || "none").toUpperCase();
  $("#serial-text-preview").textContent = analysis.text_preview || "No decoded text.";
  $("#serial-hex-preview").textContent = analysis.hex_preview || "No received bytes.";

  const status = $("#serial-analysis-status");
  if (analysis.status === "no_activity") {
    status.textContent = "No activity";
    status.className = "status-label error";
  } else {
    status.textContent = "Analyzed";
    status.className = "status-label live";
  }
  const details = [
    `${formatMetric(analysis.duration_ms, 0)} ms receive window`,
    analysis.framing?.source === "inferred" ? "Framing inferred" : "Framing configured by operator",
    run.summary?.device,
    ...(protocol.evidence || []),
    ...(analysis.warnings || []),
  ].filter(Boolean);
  $("#serial-analysis-detail").textContent = details.join(" · ");
}

function showCanAnalysis(run) {
  const analysis = run?.summary?.can_analysis;
  const status = $("#can-analysis-status");
  const reset = () => {
    $("#can-load").textContent = "—";
    $("#can-bus-type").textContent = "—";
    $("#can-nominal-rate").textContent = "—";
    $("#can-data-rate").textContent = "—";
    $("#can-protocol").textContent = "—";
    $("#can-id-format").textContent = "—";
    $("#can-frame-count").textContent = "—";
    $("#can-analysis-confidence").textContent = "—";
  };
  reset();
  if (!analysis) {
    status.textContent = "Not analyzed";
    status.className = "status-label planned";
    $("#can-analysis-detail").textContent = "This evidence predates passive CAN analysis. Run the Analyze window; old evidence remains unchanged.";
    return;
  }
  if (analysis.status !== "analyzed") {
    status.textContent = analysis.status === "no_bus_activity" ? "No bus activity" : "Insufficient evidence";
    status.className = "status-label error";
    $("#can-bus-type").textContent = analysis.physical_layer || "Unresolved";
    $("#can-analysis-confidence").textContent = "None";
    $("#can-analysis-detail").textContent = (analysis.warnings || []).join(" ") || "The capture did not contain enough defensible timing evidence.";
    return;
  }

  status.textContent = "Analyzed";
  status.className = "status-label live";
  $("#can-load").textContent = formatMetric(analysis.bus_load_percent, 1);
  $("#can-bus-type").textContent = analysis.bus_type || "CAN-family";
  $("#can-nominal-rate").textContent = formatBitrate(analysis.nominal_bitrate_bps);
  $("#can-data-rate").textContent = analysis.fd_brs_observed
    ? formatBitrate(analysis.data_bitrate_bps)
    : (analysis.bus_type === "CAN FD" ? "No BRS" : "N/A");
  $("#can-protocol").textContent = analysis.protocol?.name || "Higher layer unresolved";
  $("#can-id-format").textContent = analysis.identifier_format || "—";
  $("#can-frame-count").textContent = Number.isFinite(Number(analysis.frame_count))
    ? Number(analysis.frame_count).toLocaleString()
    : "—";
  $("#can-analysis-confidence").textContent = (analysis.confidence || "unknown").toUpperCase();

  const protocolEvidence = analysis.protocol?.evidence || [];
  const warnings = analysis.warnings || [];
  const quality = analysis.signal_quality || {};
  const details = [
    `${formatMetric(analysis.observation_window_ms, 2)} ms observation`,
    `${formatMetric(analysis.samples_per_nominal_bit, 1)} samples/nominal bit`,
    `${analysis.crc_valid_header_count ?? 0} CRC-valid / ${analysis.decoded_header_count ?? 0} decoded headers`,
    analysis.bus_load_method,
    ...protocolEvidence,
    Number.isFinite(Number(quality.differential_span_v))
      ? `${formatMetric(quality.differential_span_v, 2)} V dominant differential span`
      : null,
    ...warnings,
  ].filter(Boolean);
  $("#can-analysis-detail").textContent = details.join(" · ");
}

function isCanDecodeRun(run) {
  return run?.capture_type === "can_decode";
}

function tableCell(text) {
  const cell = document.createElement("td");
  cell.textContent = String(text);
  return cell;
}

function renderCanCapabilities(result) {
  const capabilities = result.capabilities || {};
  const capabilityText = document.createElement("p");
  capabilityText.textContent = `Sampled-waveform analysis: ${capabilities.sampled_waveform_analysis_available ? "available" : "unavailable"}. Scope acquisition: ${capabilities.scope_acquisition_available ? "available" : "unavailable"}. Long listen-only CAN adapter: ${capabilities.long_listen_only_can_adapter_available ? "available" : "not commissioned"}. SocketCAN/provider: ${capabilities.socketcan_or_provider_available ? "available" : "absent"}. Transmit/replay/query: ${capabilities.transmit_available || capabilities.replay_available || capabilities.query_available ? "authority present" : "unavailable"}.`;
  const capabilityHeading = document.createElement("strong");
  capabilityHeading.textContent = "Capability boundary";
  $("#can-capability-panel").replaceChildren(capabilityHeading, capabilityText);
}

function renderCanDiagnostics(result) {
  renderCanCapabilities(result);
  if (Number(result.artifact_schema_version) !== 2) {
    const unavailable = "v2 diagnostics unavailable for legacy evidence";
    for (const selector of [
      "#can-capture-duration", "#can-sample-interval", "#can-frame-rate",
      "#can-occupancy", "#can-ack-summary", "#can-integrity-counts",
      "#can-dominant-levels", "#can-recessive-levels", "#can-differential-span",
      "#can-transition-timing",
    ]) {
      $(selector).textContent = unavailable;
    }
    const badge = document.createElement("span");
    badge.className = "status-label planned";
    badge.textContent = `Legacy CAN Decode schema v${result.artifact_schema_version ?? "1"} · CAN Analysis v2 diagnostics unavailable`;
    $("#can-provenance-badges").replaceChildren(badge);
    return;
  }
  const physical = result.physical_layer_diagnostics || {};
  const integrity = result.integrity_diagnostics || {};
  const levelText = (stateLevel) => {
    const level = stateLevel || {};
    return `CAN-H ${formatMetric(level.can_h_v?.median, 2)} V · CAN-L ${formatMetric(level.can_l_v?.median, 2)} V · differential ${formatMetric(level.differential_v?.median, 2)} V · common-mode ${formatMetric(level.common_mode_v?.median, 2)} V`;
  };
  $("#can-capture-duration").textContent = `${formatMetric(Number(physical.capture_duration_us) / 1000, 3)} ms`;
  $("#can-sample-interval").textContent = `${formatMetric(physical.sample_interval_us, 3)} µs`;
  $("#can-frame-rate").textContent = `${formatMetric(integrity.observed_window_frame_rate_hz, 1)} Hz`;
  $("#can-occupancy").textContent = `${formatMetric(integrity.validated_classical_frame_wire_occupancy_percent, 2)}%`;
  $("#can-ack-summary").textContent = `${Number(integrity.ack_dominant_count || 0)} / ${Number(integrity.ack_recessive_count || 0)}`;
  $("#can-integrity-counts").textContent = `${Number(integrity.validated_frame_count || 0)} / ${Number(integrity.rejected_candidate_count || 0)} / ${Number(integrity.unsupported_fd_candidate_count || 0)}`;
  $("#can-dominant-levels").textContent = levelText(physical.dominant);
  $("#can-recessive-levels").textContent = levelText(physical.recessive);
  $("#can-differential-span").textContent = `${formatMetric(physical.differential_span_v, 2)} V sampled differential span`;
  const timing = physical.transition_timing || {};
  $("#can-transition-timing").textContent = timing.available
    ? `${formatMetric(timing.rise_time_us, 3)} µs rise · ${formatMetric(timing.fall_time_us, 3)} µs fall`
    : `${timing.reason || "Unavailable"} · ${Number(timing.edge_count || 0)} observed state edges`;

  const badges = [];
  for (const text of [
    `Artifact schema v${result.artifact_schema_version ?? "?"}`,
    `Decoder v${result.decoder_algorithm_version ?? "?"}`,
    `Analyzer v${result.analyzer_version ?? "?"}`,
    result.source_sha256 ? `Source SHA-256 ${String(result.source_sha256).slice(0, 12)}…` : null,
    result.source_captured_at ? `Source ${new Date(result.source_captured_at).toISOString()}` : null,
  ].filter(Boolean)) {
    const badge = document.createElement("span");
    badge.className = "status-label live";
    badge.textContent = text;
    if (text.startsWith("Source SHA-256")) badge.title = `Source SHA-256 ${result.source_sha256}`;
    badges.push(badge);
  }
  $("#can-provenance-badges").replaceChildren(...badges);
}

function renderCanTimeline(result) {
  const advertisedLimit = Math.min(200, Number(result.timeline_limit || 200));
  const entries = (result.timeline || []).slice(0, advertisedLimit).map((frame) => {
    const row = document.createElement("div");
    row.className = "can-timeline-row";
    row.setAttribute("role", "listitem");
    const time = document.createElement("span");
    time.textContent = `${formatMetric(frame.timestamp_us, 1)} µs`;
    const identifier = document.createElement("strong");
    identifier.textContent = `${frame.identifier_hex || "—"} · ${frame.extended ? "29-bit" : "11-bit"}`;
    const payload = document.createElement("code");
    payload.textContent = frame.payload_hex || "RTR / empty";
    row.append(time, identifier, payload);
    return row;
  });
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.setAttribute("role", "listitem");
    empty.textContent = "No validated timeline rows match the bounded filter.";
    entries.push(empty);
  }
  $("#can-frame-timeline").replaceChildren(...entries);
}

function renderCanPayloadHeatmap(result) {
  const heatmap = result.payload_heatmap || {};
  const rows = [];
  const truth = document.createElement("p");
  truth.className = "can-heatmap-truth";
  truth.textContent = `${heatmap.returned_identifier_count || 0} of ${heatmap.total_identifier_count || 0} filtered identifiers · ${heatmap.bin_count || 0} one-bit bins · ${heatmap.cell_semantics || "No heatmap evidence"}`;
  rows.push(truth);
  const legend = document.createElement("p");
  legend.className = "can-heat-legend";
  legend.textContent = "Byte / bit columns B0.7 through B7.0. Every focusable cell shows its comparable flip count; color intensity is secondary.";
  rows.push(legend);
  const header = document.createElement("div");
  header.className = "can-heat-row can-heat-header";
  const headerLabel = document.createElement("strong");
  headerLabel.textContent = "Identifier";
  const headerCells = document.createElement("div");
  headerCells.className = "can-heat-cells";
  for (let index = 0; index < 64; index += 1) {
    const heading = document.createElement("span");
    heading.textContent = `B${Math.floor(index / 8)}.${7 - (index % 8)}`;
    headerCells.append(heading);
  }
  header.append(headerLabel, headerCells);
  rows.push(header);
  const advertisedRows = Math.min(200, Number(heatmap.returned_identifier_count || 200));
  for (const item of (heatmap.identifiers || []).slice(0, advertisedRows)) {
    const row = document.createElement("div");
    row.className = "can-heat-row";
    const label = document.createElement("strong");
    label.textContent = `${item.identifier_hex} ${item.format}`;
    const cells = document.createElement("div");
    cells.className = "can-heat-cells";
    (item.cells || []).slice(0, 64).forEach((count, index) => {
      const cell = document.createElement("span");
      cell.className = `can-heat-cell heat-${Math.min(4, Number(count || 0))}`;
      cell.tabIndex = 0;
      cell.textContent = String(Number(count || 0));
      const coordinate = `byte ${Math.floor(index / 8)}, bit ${7 - (index % 8)}`;
      cell.title = `${coordinate}: ${Number(count || 0)} comparable flips`;
      cell.setAttribute("aria-label", `${item.identifier_hex} ${item.format}, ${coordinate}, ${Number(count || 0)} comparable flips`);
      cells.append(cell);
    });
    row.append(label, cells);
    rows.push(row);
  }
  $("#can-payload-heatmap").replaceChildren(...rows);
}

function loadCanComparisonOptions() {
  canComparisonRequestGate.invalidate();
  const runs = (state.runs || []).filter((run) => isCanDecodeRun(run) && Number(run.artifact_schema_version) === 2);
  const makeOptions = () => runs.map((run) => {
    const option = document.createElement("option");
    option.value = run.run_id;
    const childTime = run.captured_at ? new Date(run.captured_at).toISOString() : "unknown child time";
    const sourceHash = run.source_sha256 ? ` · source SHA-256 ${String(run.source_sha256).slice(0, 12)}…` : "";
    option.textContent = `${run.label || "CAN Decode"} · child run ${run.run_id} · ${childTime}${sourceHash}`;
    return option;
  });
  const baseline = $("#can-compare-baseline");
  const candidate = $("#can-compare-candidate");
  baseline.replaceChildren(...makeOptions());
  candidate.replaceChildren(...makeOptions());
  if (runs.length > 1) {
    baseline.value = runs[1].run_id;
    candidate.value = runs[0].run_id;
  }
  resetCanComparison();
  $("#can-compare-button").disabled = runs.length < 2 || baseline.value === candidate.value;
}

function renderCanComparison(comparison) {
  const rows = [];
  const summary = document.createElement("p");
  const truncated = comparison.identifier_deltas_truncated
    ? ` Showing ${comparison.identifier_delta_returned_count || 0} bounded deltas.`
    : "";
  summary.textContent = `${comparison.common_identifier_total_count || 0} common · ${comparison.observed_only_in_baseline_total_count || 0} observed only in baseline · ${comparison.observed_only_in_candidate_total_count || 0} observed only in candidate.${truncated}${comparison.same_source_warning ? ` ${comparison.same_source_warning}` : ""}`;
  rows.push(summary);
  for (const delta of comparison.identifier_deltas || []) {
    const row = document.createElement("div");
    row.className = "can-comparison-row";
    const label = document.createElement("strong");
    label.textContent = `${delta.identifier_hex} ${delta.extended ? "29-bit" : "11-bit"}`;
    const detail = document.createElement("span");
    detail.textContent = `Observed-window rate Δ ${formatMetric(delta.observed_window_rate_hz_delta, 1)} Hz · mean period Δ ${formatMetric(delta.mean_period_us_delta, 1)} µs · payload-state change Δ ${formatMetric(delta.payload_state_change_percent_delta, 1)} points`;
    row.append(label, detail);
    rows.push(row);
  }
  $("#can-compare-results").replaceChildren(...rows);
  const baseline = comparison.provenance?.baseline || {};
  const candidate = comparison.provenance?.candidate || {};
  $("#can-comparison-provenance").textContent = `Authoritative child chains verified · baseline ${baseline.run_id || "unknown"} · candidate ${candidate.run_id || "unknown"}`;
  $("#can-comparison-provenance").className = "status-label live";
}

function resetCanComparison(message = "Select two distinct immutable CAN Decode v2 children.") {
  state.canComparison = null;
  $("#can-compare-results").replaceChildren();
  $("#can-comparison-provenance").textContent = "No comparison loaded";
  $("#can-comparison-provenance").className = "status-label planned";
  setMessage("#can-compare-message", message);
}

function bindCanComparisonForm() {
  const baseline = $("#can-compare-baseline");
  const candidate = $("#can-compare-candidate");
  const syncButton = () => {
    $("#can-compare-button").disabled = !baseline.value || !candidate.value || baseline.value === candidate.value;
  };
  const update = () => {
    canComparisonRequestGate.invalidate();
    resetCanComparison();
    syncButton();
  };
  baseline.addEventListener("change", update);
  candidate.addEventListener("change", update);
  $("#can-compare-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const generation = canComparisonRequestGate.begin();
    const query = new URLSearchParams({baseline_run_id: baseline.value, candidate_run_id: candidate.value});
    $("#can-compare-button").disabled = true;
    setMessage("#can-compare-message", "Comparing two authoritative immutable children…");
    try {
      const comparison = await getJson(`/api/can-decode-comparisons?${query.toString()}`);
      if (!canComparisonRequestGate.isCurrent(generation)) return;
      state.canComparison = comparison;
      renderCanComparison(comparison);
      setMessage("#can-compare-message", "Comparison complete. Counts and rates remain qualified by each observed window.", "success");
    } catch (error) {
      if (!canComparisonRequestGate.isCurrent(generation)) return;
      setMessage("#can-compare-message", `CAN comparison failed: ${error.message}`, "error");
    } finally {
      if (canComparisonRequestGate.isCurrent(generation)) syncButton();
    }
  });
}

function renderCanDecode() {
  const result = state.canDecode;
  if (!result) return;
  renderCanDiagnostics(result);
  renderCanTimeline(result);
  renderCanPayloadHeatmap(result);
  const filter = $("#can-identifier-filter").value.trim().toLowerCase();
  const changingOnly = $("#can-changing-only").checked;
  const identifiers = (result.identifiers || []).filter((item) => {
    const matchesText = !filter || String(item.identifier_hex || "").toLowerCase().includes(filter);
    return matchesText && (!changingOnly || Number(item.payload_state_change_count) > 0);
  });
  const identifierKey = (item) => `${Number(item.identifier)}:${item.extended ? 1 : 0}`;
  const changingIdentifiers = new Set(
    (result.identifiers || [])
      .filter((item) => Number(item.payload_state_change_count) > 0)
      .map(identifierKey),
  );
  const frames = (result.frames || []).filter((frame) => {
    const matchesText = !filter || String(frame.identifier_hex || "").toLowerCase().includes(filter);
    return matchesText && (!changingOnly || changingIdentifiers.has(identifierKey(frame)));
  });

  const identifierRows = identifiers.map((item) => {
    const row = document.createElement("tr");
    const frequency = Number(item.mean_frequency_hz);
    const period = Number(item.mean_period_us);
    const cadence = Number.isFinite(frequency) && Number.isFinite(period)
      ? `${formatMetric(period, 1)} µs · ${formatMetric(frequency, 1)} Hz`
      : "One frame · cadence unavailable";
    const jitter = Number.isFinite(Number(item.inter_arrival_stddev_us))
      ? `${formatMetric(item.inter_arrival_stddev_us, 1)} µs · ${item.interval_count} intervals`
      : `${Number(item.interval_count || 0)} intervals · spread unavailable`;
    row.append(
      tableCell(item.identifier_hex || "—"),
      tableCell(item.extended ? "Extended" : "Standard"),
      tableCell(Number(item.frame_count || 0).toLocaleString()),
      tableCell(cadence),
      tableCell(jitter),
      tableCell(`${Number(item.payload_state_change_count || 0).toLocaleString()} · ${formatMetric(item.payload_state_change_percent, 1)}%`),
      tableCell(item.last_payload_hex || "(remote / empty)"),
    );
    return row;
  });
  if (!identifierRows.length) {
    const row = document.createElement("tr");
    const cell = tableCell("No identifiers match the current bounded filter.");
    cell.colSpan = 7;
    row.append(cell);
    identifierRows.push(row);
  }
  $("#can-identifier-rows").replaceChildren(...identifierRows);

  const frameRows = frames.map((frame) => {
    const row = document.createElement("tr");
    row.append(
      tableCell(`${formatMetric(frame.timestamp_us, 1)} µs`),
      tableCell(frame.identifier_hex || "—"),
      tableCell(frame.extended ? "Extended" : "Standard"),
      tableCell(frame.dlc ?? "—"),
      tableCell(frame.payload_hex || "(remote / empty)"),
      tableCell(frame.crc_valid ? "Valid" : "Excluded"),
    );
    return row;
  });
  if (!frameRows.length) {
    const row = document.createElement("tr");
    const cell = tableCell("No validated frames match the current bounded filter.");
    cell.colSpan = 6;
    row.append(cell);
    frameRows.push(row);
  }
  $("#can-frame-rows").replaceChildren(...frameRows);

  const frameTruncation = result.frames_truncated ? " Frames are truncated; full JSONL is retained." : "";
  const identifierTruncation = result.identifiers_truncated ? " Identifiers are truncated; full CSV is retained." : "";
  $("#can-decode-row-truth").textContent = `Showing ${identifiers.length} filtered identifier rows and ${frames.length} filtered frame rows. API returned ${result.returned_identifier_count} of ${result.total_identifier_count} identifiers (limit ${result.identifier_limit}) and ${result.returned_frame_count} of ${result.total_frame_count} frames (limit ${result.frame_limit}).${identifierTruncation}${frameTruncation}`;

  const frameArtifact = $("#can-frames-artifact");
  const identifierArtifact = $("#can-identifiers-artifact");
  frameArtifact.href = result.artifact_urls.frames_jsonl;
  identifierArtifact.href = result.artifact_urls.identifiers_csv;
  frameArtifact.classList.remove("hidden");
  identifierArtifact.classList.remove("hidden");
  const status = $("#can-decode-status");
  status.textContent = `${result.total_frame_count} valid · ${result.can_polarity || "expected"}`;
  status.className = "status-label live";
}

async function loadCanDecodeSources() {
  const select = $("#can-decode-source");
  try {
    const payload = await getJson("/api/can-decode-sources");
    const sources = payload.sources || [];
    const options = sources.map((source) => {
      const option = document.createElement("option");
      option.value = source.run_id;
      option.textContent = `${source.label || source.run_id} · ${formatTimestamp(source.captured_at)} · ${String(source.capture_type || "CAN").replace("_", " ")}`;
      return option;
    });
    if (!options.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No eligible existing CAN capture";
      options.push(option);
    }
    select.replaceChildren(...options);
    $("#can-decode-button").disabled = !sources.length;
  } catch (error) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Source list unavailable";
    select.replaceChildren(option);
    $("#can-decode-button").disabled = true;
    setMessage("#can-decode-message", `Could not load CAN decode sources: ${error.message}`, "error");
  }
}

async function loadCanDecodeResult(runId, requestGeneration = null) {
  if (requestGeneration === null) requestGeneration = canDecodeRequestGate.begin();
  if (!runId) return false;
  try {
    const query = new URLSearchParams();
    const identifier = $("#can-identifier-filter").value.trim();
    if (identifier) query.set("identifier", identifier);
    if ($("#can-changing-only").checked) query.set("changing_only", "true");
    const suffix = query.size ? `?${query.toString()}` : "";
    const result = await getJson(`/api/can-decodes/${encodeURIComponent(runId)}${suffix}`);
    if (!canDecodeRequestGate.isCurrent(requestGeneration)) return false;
    state.canDecode = result;
    renderCanDecode();
    return true;
  } catch (error) {
    if (!canDecodeRequestGate.isCurrent(requestGeneration)) return false;
    setMessage("#can-decode-message", `Could not load CAN decode result: ${error.message}`, "error");
    return false;
  }
}

function bindCanDecodeForm() {
  $("#can-decode-source").addEventListener("change", () => {
    canDecodeRequestGate.invalidate();
    $("#can-decode-button").disabled = !$("#can-decode-source").value;
  });
  $("#can-decode-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#can-decode-button");
    const sourceRunId = $("#can-decode-source").value;
    if (!sourceRunId) {
      setMessage("#can-decode-message", "Choose an eligible existing capture.", "error");
      return;
    }
    const requestGeneration = canDecodeRequestGate.begin();
    button.disabled = true;
    setMessage("#can-decode-message", "Decoding immutable source into passive child evidence…");
    try {
      const result = await getJson("/api/can-decodes", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          source_run_id: sourceRunId,
          label: $("#can-decode-label").value,
        }),
      });
      if (!canDecodeRequestGate.isCurrent(requestGeneration)) return;
      const loaded = await loadCanDecodeResult(result.run_id, requestGeneration);
      if (!loaded || !canDecodeRequestGate.isCurrent(requestGeneration)) return;
      setMessage(
        "#can-decode-message",
        `${result.run_id} completed with ${result.frame_count} validated Classical CAN frames and 0 writes.`,
        "success",
      );
      if (!canDecodeRequestGate.isCurrent(requestGeneration)) return;
      await refreshRuns();
    } catch (error) {
      if (!canDecodeRequestGate.isCurrent(requestGeneration)) return;
      setMessage("#can-decode-message", `CAN decode failed: ${error.message}`, "error");
    } finally {
      if (canDecodeRequestGate.isCurrent(requestGeneration)) {
        button.disabled = !$("#can-decode-source").value;
      }
    }
  });
  $("#can-identifier-filter").addEventListener("input", () => {
    window.clearTimeout(canFilterTimer);
    canDecodeRequestGate.invalidate();
    canFilterTimer = window.setTimeout(() => {
      if (state.canDecode?.run_id) loadCanDecodeResult(state.canDecode.run_id);
    }, 250);
  });
  $("#can-changing-only").addEventListener("change", () => {
    if (state.canDecode?.run_id) loadCanDecodeResult(state.canDecode.run_id);
  });
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
  showCanAnalysis(run);
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
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No evidence packages yet.";
    list.replaceChildren(empty);
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
    backend.className = (run.backend || "").includes("simulator") ? "sim" : "hardware";
    backend.textContent = (run.backend || "unknown").toUpperCase();
    const window = document.createElement("span");
    const sampleUnit = isSerialRun(run) ? "BYTES" : (isModbusRun(run) ? "HOSTS" : "SAMPLES");
    window.textContent = `${(run.profile || "legacy").toUpperCase()} · ${(run.preset || "unknown").toUpperCase()} · ${(run.samples || 0).toLocaleString()} ${sampleUnit}`;
    const links = document.createElement("div");
    links.className = "artifact-links";
    if (isCanDecodeRun(run)) {
      links.append(
        artifactLink(run, "frames.jsonl", "FRAMES"),
        artifactLink(run, "identifiers.csv", "IDENTIFIERS"),
        artifactLink(run, "summary.json", "JSON"),
      );
    } else if (isBusSurveyRun(run)) {
      links.append(
        artifactLink(run, "fast.csv", "FAST"),
        artifactLink(run, "context.csv", "CONTEXT"),
        artifactLink(run, "sparse.csv", "SPARSE"),
        artifactLink(run, "summary.json", "JSON"),
        artifactLink(run, "report.pdf", "PDF"),
      );
    } else if (isModbusRun(run)) {
      links.append(
        artifactLink(run, "devices.csv", "INVENTORY"),
        artifactLink(run, "scan.json", "SCAN"),
        artifactLink(run, "summary.json", "JSON"),
        artifactLink(run, "report.pdf", "PDF"),
      );
    } else if (isSerialRun(run)) {
      links.append(
        artifactLink(run, "capture.bin", "BIN"),
        artifactLink(run, "chunks.jsonl", "TIMING"),
        artifactLink(run, "transcript.txt", "TEXT"),
        artifactLink(run, "summary.json", "JSON"),
        artifactLink(run, "report.pdf", "PDF"),
      );
    } else {
      links.append(
        artifactLink(run, "overview.png", "PNG"),
        artifactLink(run, "capture.csv", "CSV"),
        artifactLink(run, "summary.json", "JSON"),
        artifactLink(run, "report.pdf", "PDF"),
      );
    }
    card.append(identity, backend, window, links);
    return card;
  }));
}

function renderTimeline(runs) {
  const list = $("#timeline-list");
  if (!runs.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No capture events yet.";
    list.replaceChildren(empty);
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
    showBusSurvey(runs.find(isBusSurveyRun));
    showModbus(runs.find(isModbusRun));
    showSerial(runs.find(isSerialRun));
    showNetwork(runs.find(isNetworkRun));
    showScope(runs.find(isScopeRun));
    await loadCanDecodeSources();
    loadCanComparisonOptions();
    const latestDecode = runs.find(isCanDecodeRun);
    if (latestDecode && state.canDecode?.run_id !== latestDecode.run_id) {
      await loadCanDecodeResult(latestDecode.run_id);
    }
  } catch (error) {
    setMessage("#scope-message", `Could not load evidence list: ${error.message}`, "error");
    setMessage("#serial-message", `Could not load evidence list: ${error.message}`, "error");
    setMessage("#can-message", `Could not load evidence list: ${error.message}`, "error");
    setMessage("#modbus-message", `Could not load evidence list: ${error.message}`, "error");
    setMessage("#sniffer-message", `Could not load evidence list: ${error.message}`, "error");
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

function bindSerialCaptureForm() {
  $("#serial-capture-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#serial-capture-button");
    button.disabled = true;
    setMessage("#serial-message", "RX window open. Listening without writing…");
    try {
      const run = await getJson("/api/serial/captures", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          label: $("#serial-label").value,
          duration_s: Number($("#serial-duration").value),
          mode: $("#serial-mode").value,
          baud: Number($("#serial-baud").value),
          data_bits: Number($("#serial-data-bits").value),
          parity: $("#serial-parity").value,
          stop_bits: Number($("#serial-stop-bits").value),
        }),
      });
      setMessage("#serial-message", `${run.run_id} completed. Raw RX and timing evidence are retained.`, "success");
      await refreshRuns();
    } catch (error) {
      setMessage("#serial-message", `Serial receive failed: ${error.message}`, "error");
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

function bindModbusScanForm() {
  $("#modbus-scan-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#modbus-scan-button");
    const networkSelect = $("#modbus-subnet");
    const subnet = networkSelect.value;
    const interfaceName = networkSelect.selectedOptions[0]?.dataset.interface;
    if (!subnet || !interfaceName) {
      setMessage("#modbus-message", "Choose a connected IPv4 subnet before scanning.", "error");
      return;
    }
    button.disabled = true;
    setMessage("#modbus-message", "Scanning the selected connected subnet with read-only identity requests…");
    try {
      const run = await getJson("/api/modbus/scans", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          label: $("#modbus-label").value,
          interface: interfaceName,
          subnet,
          mode: $("#modbus-mode").value,
          connect_timeout_ms: Number($("#modbus-timeout").value),
          response_timeout_ms: 1250,
          hicp_timeout_ms: 1500,
          workers: 4,
        }),
      });
      const count = Number(run.summary?.device_count || 0);
      setMessage("#modbus-message", `${run.run_id} completed. ${count} identity candidate${count === 1 ? "" : "s"} retained with 0 writes.`, "success");
      await refreshRuns();
    } catch (error) {
      setMessage("#modbus-message", `Modbus discovery failed: ${error.message}`, "error");
    } finally {
      button.disabled = false;
    }
  });
}

bindTabs();
bindScopeControls();
bindScopeCaptureForm();
bindBusSurveyForm();
bindSerialCaptureForm();
bindCanCaptureForm();
bindCanDecodeForm();
bindCanComparisonForm();
bindModbusScanForm();
$("#usb-routing-apply").addEventListener("click", applyUsbRouting);
$("#refresh").addEventListener("click", refreshRuns);
loadScopeProfiles();
loadModbusNetworks();
loadUsbRouting();
refreshStatus();
refreshRuns();
setInterval(refreshStatus, 15_000);
