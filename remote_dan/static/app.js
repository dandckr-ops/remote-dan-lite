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
  obd: {
    subtab: "live-data",
    status: null,
    liveData: null,
    faults: null,
    vehicleInfo: null,
    customers: [],
    vehicles: [],
    sessions: [],
    pollTimer: null,
    liveRequest: null,
    statusEpoch: 0,
    faultsEpoch: 0,
    vehicleEpoch: 0,
    savePending: false,
    saveOperationIds: {},
  },
};
const canDecodeRequestGate = CanRequestGate.createLatestRequestGate();

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
  if (selectedName === "obd") {
    const selectedTab = tabs.find((tab) => tab.dataset.tab === "obd");
    selectedTab?.scrollIntoView({block: "nearest", inline: "nearest"});
    refreshObdStatus();
    loadObdRecords();
    scheduleObdLivePoll();
  } else {
    stopObdLivePoll();
  }
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
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
      : typeof payload.detail === "object" && payload.detail !== null
        ? JSON.stringify(payload.detail)
        : payload.detail;
    const error = new Error(detail || `${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }
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
      setMessage("#sniffer-message", "Harness and safety attestations recorded. Survey remains receive-only.");
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

function isObdRun(run) {
  return run?.capture_type === "obd_scan" || run?.profile === "obd";
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

function renderCanDecode() {
  const result = state.canDecode;
  if (!result) return;
  const filter = $("#can-identifier-filter").value.trim().toLowerCase();
  const changingOnly = $("#can-changing-only").checked;
  const identifiers = (result.identifiers || []).filter((item) => {
    const matchesText = !filter || String(item.identifier_hex || "").toLowerCase().includes(filter);
    return matchesText && (!changingOnly || Number(item.payload_change_count) > 0);
  });
  const changingIdentifiers = new Set(
    (result.identifiers || [])
      .filter((item) => Number(item.payload_change_count) > 0)
      .map((item) => Number(item.identifier)),
  );
  const frames = (result.frames || []).filter((frame) => {
    const matchesText = !filter || String(frame.identifier_hex || "").toLowerCase().includes(filter);
    return matchesText && (!changingOnly || changingIdentifiers.has(Number(frame.identifier)));
  });

  const identifierRows = identifiers.map((item) => {
    const row = document.createElement("tr");
    const frequency = Number(item.mean_frequency_hz);
    const period = Number(item.mean_period_us);
    const cadence = Number.isFinite(frequency) && Number.isFinite(period)
      ? `${formatMetric(period, 1)} µs · ${formatMetric(frequency, 1)} Hz`
      : "One frame · cadence unavailable";
    row.append(
      tableCell(item.identifier_hex || "—"),
      tableCell(item.extended ? "Extended" : "Standard"),
      tableCell(Number(item.frame_count || 0).toLocaleString()),
      tableCell(cadence),
      tableCell(Number(item.payload_change_count || 0).toLocaleString()),
      tableCell(item.last_payload_hex || "(remote / empty)"),
    );
    return row;
  });
  if (!identifierRows.length) {
    const row = document.createElement("tr");
    const cell = tableCell("No identifiers match the current bounded filter.");
    cell.colSpan = 6;
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
    if (state.canDecode?.run_id) loadCanDecodeResult(state.canDecode.run_id);
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
    const sampleUnit = isSerialRun(run) ? "BYTES" : (isModbusRun(run) ? "HOSTS" : (isObdRun(run) ? "OBSERVATIONS" : "SAMPLES"));
    window.textContent = `${(run.profile || "legacy").toUpperCase()} · ${(run.preset || "unknown").toUpperCase()} · ${(run.samples || 0).toLocaleString()} ${sampleUnit}`;
    const links = document.createElement("div");
    links.className = "artifact-links";
    if (isCanDecodeRun(run)) {
      links.append(
        artifactLink(run, "frames.jsonl", "FRAMES"),
        artifactLink(run, "identifiers.csv", "IDENTIFIERS"),
        artifactLink(run, "summary.json", "JSON"),
      );
    } else if (isObdRun(run)) {
      links.append(
        artifactLink(run, "obd-snapshot.json", "OBD JSON"),
        artifactLink(run, "manifest.json", "MANIFEST"),
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

const obdTabs = $$('[role="tab"][data-obd-tab]');
const obdPanels = $$('[role="tabpanel"][data-obd-panel]');
const obdTabNames = obdTabs.map((tab) => tab.dataset.obdTab);

function activateObdTab(name, {focus = false} = {}) {
  const selected = obdTabNames.includes(name) ? name : "live-data";
  state.obd.subtab = selected;
  obdTabs.forEach((tab) => {
    const active = tab.dataset.obdTab === selected;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  obdPanels.forEach((panel) => {
    panel.hidden = panel.dataset.obdPanel !== selected;
  });
  if (selected === "live-data") scheduleObdLivePoll();
  else stopObdLivePoll();
}

function bindObdTabs() {
  obdTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateObdTab(tab.dataset.obdTab));
    tab.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % obdTabs.length;
      if (event.key === "ArrowLeft") nextIndex = (index - 1 + obdTabs.length) % obdTabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = obdTabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      activateObdTab(obdTabs[nextIndex].dataset.obdTab, {focus: true});
    });
  });
}

function clearObdReadings(reason = "Disconnected. Previous readings are no longer current.") {
  state.obd.faultsEpoch += 1;
  state.obd.vehicleEpoch += 1;
  state.obd.saveOperationIds = {};
  state.obd.liveData = null;
  state.obd.faults = null;
  state.obd.vehicleInfo = null;
  const liveEmpty = document.createElement("p");
  liveEmpty.className = "empty-list";
  liveEmpty.textContent = "No current live values.";
  $("#obd-live-list").replaceChildren(liveEmpty);
  $("#obd-live-updated").textContent = "Last update: —";
  setMessage("#obd-live-message", reason);
  renderDtcList("#obd-stored-list", "#obd-stored-count", "#obd-stored-status", [], "not_read", "No current stored DTC snapshot.");
  renderDtcList("#obd-pending-list", "#obd-pending-count", "#obd-pending-status", [], "not_read", "No current pending DTC snapshot.");
  renderDtcList("#obd-permanent-list", "#obd-permanent-count", "#obd-permanent-status", [], "not_read", "No current permanent DTC snapshot.");
  const readinessEmpty = document.createElement("p");
  readinessEmpty.className = "empty-list";
  readinessEmpty.textContent = "No current readiness response.";
  $("#obd-readiness-list").replaceChildren(readinessEmpty);
  setMessage("#obd-fault-message", reason);
  $("#obd-vin").textContent = "—";
  $("#obd-vin-ecu").textContent = "—";
  $("#obd-vin-validation").textContent = "Not read";
  $("#obd-vin-coverage").textContent = "—";
  $("#obd-vin-result").classList.remove("success", "error");
  setMessage("#obd-vehicle-message", reason);
}

function renderObdStatus(status) {
  state.obd.status = status;
  const connected = Boolean(status.connected);
  if (!connected) {
    stopObdLivePoll();
    clearObdReadings();
  }
  $("#obd-connection-status").textContent = connected ? "Connected" : "Disconnected";
  $("#obd-connection-status").className = `status-label ${connected ? "live" : "planned"}`;
  $("#obd-provider").textContent = status.provider || "—";
  $("#obd-protocol").textContent = status.protocol || "—";
  $("#obd-ecu-summary").textContent = (status.responder_ids || []).join(", ") || "—";
  $("#obd-voltage").textContent = Number.isFinite(Number(status.voltage)) ? `${formatMetric(status.voltage, 1)} V` : "—";
  $("#obd-connect-button").disabled = connected;
  $("#obd-disconnect-button").disabled = !connected;
  $("#obd-provider-mode").disabled = connected;
  $("#obd-session-select").disabled = connected;
  if (status.session_id) $("#obd-session-select").value = String(status.session_id);
  const canRead = connected;
  const canSave = connected && Boolean(status.session_id) && !state.obd.savePending;
  $("#obd-live-save-button").disabled = !canSave;
  $("#obd-fault-refresh-button").disabled = !canRead;
  $("#obd-fault-save-button").disabled = !canSave;
  $("#obd-vehicle-refresh-button").disabled = !canRead;
  $("#obd-vehicle-save-button").disabled = !canSave;
  setMessage("#obd-message", connected
    ? `${status.adapter_identity || status.provider} · ${status.protocol}`
    : "Select a session for durable evidence, then connect.", connected ? "success" : "");
}

async function refreshObdStatus({schedule = true} = {}) {
  const epoch = ++state.obd.statusEpoch;
  try {
    const status = await getJson("/api/obd/status");
    if (epoch !== state.obd.statusEpoch) return null;
    renderObdStatus(status);
    if (schedule) scheduleObdLivePoll();
    return status;
  } catch (error) {
    if (epoch === state.obd.statusEpoch) {
      setMessage("#obd-message", `OBD status unavailable: ${error.message}`, "error");
    }
    return null;
  }
}

function renderObdLiveData(payload) {
  state.obd.liveData = payload;
  const list = $("#obd-live-list");
  const values = payload.values || [];
  if (!values.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No supported live values returned.";
    list.replaceChildren(empty);
  } else {
    list.replaceChildren(...values.map((item) => {
      const card = document.createElement("article");
      card.className = `obd-live-card ${item.fresh === false ? "stale" : "fresh"}`;
      card.setAttribute("role", "listitem");
      const label = document.createElement("span");
      const value = document.createElement("strong");
      const detail = document.createElement("small");
      label.textContent = `${item.pid} · ${item.name}`;
      const numeric = Number(item.value);
      value.textContent = `${Number.isFinite(numeric) ? formatMetric(numeric, Math.abs(numeric) < 10 ? 2 : 1) : "—"} ${item.unit || ""}`.trim();
      detail.textContent = `${item.ecu || "ECU ?"} · ${item.fresh === false ? "Stale" : "Fresh"}`;
      card.append(label, value, detail);
      return card;
    }));
  }
  $("#obd-live-updated").textContent = `Last update: ${formatTimestamp(payload.sampled_at)}`;
  setMessage("#obd-live-message", payload.errors?.length
    ? `${values.length} values · ${payload.errors.length} communication/decode errors`
    : `${values.length} supported values · no reported errors`, payload.errors?.length ? "error" : "success");
}

function stopObdLivePoll() {
  if (state.obd.pollTimer !== null) {
    window.clearTimeout(state.obd.pollTimer);
    state.obd.pollTimer = null;
  }
  if (state.obd.liveRequest) {
    state.obd.liveRequest.abort();
    state.obd.liveRequest = null;
  }
}

function scheduleObdLivePoll(delayOverride = null) {
  const topLevelActive = !$("#panel-obd").hidden;
  if (!topLevelActive || state.obd.subtab !== "live-data" || !state.obd.status?.connected) return;
  if (state.obd.pollTimer !== null || state.obd.liveRequest) return;
  const delay = delayOverride ?? (state.obd.liveData ? 1200 : 0);
  state.obd.pollTimer = window.setTimeout(async () => {
    state.obd.pollTimer = null;
    const controller = new AbortController();
    const epoch = state.obd.statusEpoch;
    state.obd.liveRequest = controller;
    let nextDelay = null;
    try {
      const payload = await getJson("/api/obd/live", {signal: controller.signal});
      if (epoch === state.obd.statusEpoch && state.obd.status?.connected) {
        renderObdLiveData(payload);
        nextDelay = !(payload.values || []).length && (payload.errors || []).length ? 5000 : 1200;
      }
    } catch (error) {
      if (error.name !== "AbortError" && epoch === state.obd.statusEpoch) {
        nextDelay = 5000;
        setMessage("#obd-live-message", `Live read failed: ${error.message}`, "error");
        const refreshed = await refreshObdStatus({schedule: false});
        if (refreshed && !refreshed.connected) nextDelay = null;
      }
    } finally {
      if (state.obd.liveRequest === controller) state.obd.liveRequest = null;
      const recoveryDelay = nextDelay ?? (state.obd.status?.connected ? 1200 : null);
      if (!controller.signal.aborted && recoveryDelay !== null) scheduleObdLivePoll(recoveryDelay);
    }
  }, delay);
}

function renderDtcList(selector, countSelector, statusSelector, items, status, emptyText) {
  const hasDefensibleCount = status === "complete" || status === "partial";
  $(countSelector).textContent = hasDefensibleCount ? String(items.length) : "—";
  const statusLabel = status === "no_data" ? "Unavailable"
    : status === "error" ? "Read failed"
    : status === "partial" ? "Partial read"
    : status === "complete" ? "Read complete"
    : "Not read";
  $(statusSelector).textContent = statusLabel;
  const list = $(selector);
  if (!items.length) {
    const item = document.createElement("li");
    item.textContent = emptyText;
    list.replaceChildren(item);
    return;
  }
  list.replaceChildren(...items.map((dtc) => {
    const item = document.createElement("li");
    const code = document.createElement("strong");
    const description = document.createElement("span");
    const ecu = document.createElement("small");
    code.textContent = dtc.code;
    description.textContent = dtc.description || "Description unavailable";
    ecu.textContent = `Source ${dtc.ecu || "unknown"}`;
    item.append(code, description, ecu);
    return item;
  }));
}

function renderObdFaults(payload) {
  state.obd.faults = payload;
  renderDtcList("#obd-stored-list", "#obd-stored-count", "#obd-stored-status", payload.stored || [], payload.stored_status, payload.stored_status === "error" ? "Mode $03 read failed." : payload.stored_status === "no_data" ? "Unavailable — ECM returned NO DATA for Mode $03." : "ECM returned zero confirmed/stored DTCs.");
  renderDtcList("#obd-pending-list", "#obd-pending-count", "#obd-pending-status", payload.pending || [], payload.pending_status, payload.pending_status === "error" ? "Mode $07 read failed." : payload.pending_status === "no_data" ? "Unavailable — ECM returned NO DATA for Mode $07." : "ECM returned zero pending DTCs.");
  renderDtcList("#obd-permanent-list", "#obd-permanent-count", "#obd-permanent-status", payload.permanent || [], payload.permanent_status, payload.permanent_status === "error" ? "Mode $0A read failed." : payload.permanent_status === "no_data" ? "Unavailable — ECM returned NO DATA for Mode $0A." : "ECM returned zero permanent DTCs.");
  const readiness = payload.readiness || [];
  const readinessList = $("#obd-readiness-list");
  if (!readiness.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No readiness response returned.";
    readinessList.replaceChildren(empty);
  } else {
    readinessList.replaceChildren(...readiness.map((item) => {
      const card = document.createElement("article");
      const heading = document.createElement("strong");
      const detail = document.createElement("span");
      heading.textContent = `${item.ecu} · MIL ${item.mil_on ? "ON" : "OFF"} · ${item.dtc_count} stored`;
      detail.textContent = item.incomplete?.length ? `Incomplete: ${item.incomplete.join(", ")}` : "All supported monitors complete";
      card.append(heading, detail);
      return card;
    }));
  }
  const states = ["stored", "pending", "permanent"];
  const failedDtcStates = states.filter((stateName) => ["error", "partial"].includes(payload[`${stateName}_status`]));
  const unavailableDtcStates = states.filter((stateName) => payload[`${stateName}_status`] === "no_data");
  const errorCount = (payload.errors || []).length;
  const readinessStoredCount = readiness.reduce((sum, item) => sum + Number(item.dtc_count || 0), 0);
  const storedCountMismatch = readiness.length > 0 && payload.stored_status === "complete" && readinessStoredCount !== (payload.stored?.length || 0);
  const stateLabels = {stored: "confirmed/stored", pending: "pending", permanent: "permanent"};
  const summaries = states.map((stateName) => {
    const status = payload[`${stateName}_status`];
    return status === "complete" || status === "partial"
      ? `${payload[stateName]?.length || 0} ${stateLabels[stateName]}`
      : `${stateLabels[stateName]} unavailable`;
  }).join(" · ");
  if (storedCountMismatch) {
    setMessage("#obd-fault-message", `Count mismatch: readiness reports ${readinessStoredCount} stored DTCs but Mode $03 decoded ${payload.stored?.length || 0}. Treat this read as unverified.`, "error");
  } else if (failedDtcStates.length === 3) {
    setMessage("#obd-fault-message", "All DTC service reads failed; zero counts are not valid no-code results.", "error");
  } else if (failedDtcStates.length || errorCount) {
    setMessage("#obd-fault-message", `${summaries}. ${errorCount} communication/decode errors. Generic emissions only.`, "error");
  } else if (unavailableDtcStates.length) {
    const unavailableLabels = unavailableDtcStates.map((stateName) => stateLabels[stateName]).join(" and ");
    const agreement = readiness.length ? " Mode $03 decoded count agrees with PID $0101." : "";
    setMessage("#obd-fault-message", `${summaries}. ECM returned NO DATA for ${unavailableLabels} DTC service.${agreement} Generic emissions only.`);
  } else {
    const agreement = readiness.length ? " Mode $03 decoded count agrees with PID $0101." : "";
    setMessage("#obd-fault-message", `${summaries}.${agreement} Generic emissions only. Read at ${formatTimestamp(payload.observed_at)}.`, "success");
  }
}

async function readObdFaults() {
  const epoch = state.obd.statusEpoch;
  const requestEpoch = ++state.obd.faultsEpoch;
  try {
    setMessage("#obd-fault-message", "Reading readiness and stored, pending, and permanent DTCs…");
    const payload = await getJson("/api/obd/faults");
    if (epoch === state.obd.statusEpoch && requestEpoch === state.obd.faultsEpoch && state.obd.status?.connected) renderObdFaults(payload);
  } catch (error) {
    if (epoch === state.obd.statusEpoch && requestEpoch === state.obd.faultsEpoch) setMessage("#obd-fault-message", `Fault read failed: ${error.message}`, "error");
  }
}

function renderObdVehicleInfo(payload) {
  state.obd.vehicleInfo = payload;
  const first = payload.vins?.[0];
  const vinNode = $("#obd-vin");
  const resultNode = $("#obd-vin-result");
  vinNode.textContent = first?.vin || "Not reported";
  $("#obd-vin-ecu").textContent = first?.ecu || "—";
  $("#obd-vin-validation").textContent = first ? "Valid 17-character Mode $09 VIN" : "Unavailable";
  $("#obd-vin-coverage").textContent = payload.vins?.length
    ? `${payload.vins.length} ECM response${payload.vins.length === 1 ? "" : "s"} contained a valid VIN`
    : "No valid VIN response";
  const errorCount = (payload.errors || []).length;
  const successful = payload.vin_status === "complete" && !payload.vin_mismatch;
  resultNode.classList.toggle("success", successful);
  resultNode.classList.toggle("error", payload.vin_mismatch || payload.vin_status === "partial" || payload.vin_status === "error");
  if (payload.vin_mismatch) {
    setMessage("#obd-vehicle-message", "Warning: different ECUs reported different VINs. No VIN was selected silently.", "error");
  } else if (payload.vin_status === "partial") {
    setMessage("#obd-vehicle-message", `VIN decoded from ${first?.ecu || "one ECM"}, but ${errorCount} other Mode $09 response errors make this a partial read.`, "error");
  } else if (payload.vin_status === "error") {
    setMessage("#obd-vehicle-message", `VIN read failed with ${errorCount} communication/decode errors.`, "error");
  } else if (payload.vin_status === "no_data") {
    setMessage("#obd-vehicle-message", "VIN unavailable: the ECM returned no Mode $09 PID $02 data.");
  } else {
    setMessage("#obd-vehicle-message", `VIN READ SUCCESSFULLY · Valid 17-character VIN from ECM response ${first.ecu}.`, "success");
    window.requestAnimationFrame(() => {
      vinNode.focus({preventScroll: true});
      vinNode.scrollIntoView({behavior: "smooth", block: "nearest"});
    });
  }
}

async function readObdVehicleInfo() {
  const epoch = state.obd.statusEpoch;
  const requestEpoch = ++state.obd.vehicleEpoch;
  try {
    setMessage("#obd-vehicle-message", "Reading Mode $09 PID $02 VIN…");
    const payload = await getJson("/api/obd/vehicle-info");
    if (epoch === state.obd.statusEpoch && requestEpoch === state.obd.vehicleEpoch && state.obd.status?.connected) renderObdVehicleInfo(payload);
  } catch (error) {
    if (epoch === state.obd.statusEpoch && requestEpoch === state.obd.vehicleEpoch) setMessage("#obd-vehicle-message", `Vehicle-info read failed: ${error.message}`, "error");
  }
}

function createOperationId() {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID();
  if (typeof cryptoApi?.getRandomValues !== "function") {
    throw new Error("This browser cannot generate a secure evidence operation ID.");
  }
  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

async function saveObdSnapshot(kind, label, messageSelector) {
  if (state.obd.savePending) return;
  state.obd.savePending = true;
  if (state.obd.status) renderObdStatus(state.obd.status);
  try {
    const operationId = state.obd.saveOperationIds[kind] || createOperationId();
    state.obd.saveOperationIds[kind] = operationId;
    const run = await getJson("/api/obd/snapshots", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind, label, operation_id: operationId}),
    });
    delete state.obd.saveOperationIds[kind];
    setMessage(messageSelector, `${run.run_id} saved with hashed JSON and manifest artifacts.`, "success");
    await refreshRuns();
    await loadObdRecords();
  } catch (error) {
    setMessage(messageSelector, `Evidence save failed: ${error.message}`, "error");
  } finally {
    state.obd.savePending = false;
    if (state.obd.status) renderObdStatus(state.obd.status);
  }
}

function option(value, label) {
  const item = document.createElement("option");
  item.value = String(value);
  item.textContent = label;
  return item;
}

function renderObdRecords() {
  const sessionSelect = $("#obd-session-select");
  const selected = sessionSelect.value || (state.obd.status?.session_id ? String(state.obd.status.session_id) : "");
  sessionSelect.replaceChildren(option("", "No session selected"), ...state.obd.sessions.map((item) => option(item.session_id, `#${item.session_id} · ${item.vehicle?.display_name || "Unassigned vehicle"} · ${item.customer?.name || "Unassigned customer"}`)));
  if (selected && state.obd.sessions.some((item) => String(item.session_id) === selected)) sessionSelect.value = selected;
  $("#obd-session-customer").replaceChildren(option("", "Select customer"), ...state.obd.customers.map((item) => option(item.id, item.company ? `${item.name} · ${item.company}` : item.name)));
  $("#obd-session-vehicle").replaceChildren(option("", "Select vehicle"), ...state.obd.vehicles.map((item) => option(item.id, item.vin ? `${item.display_name} · ${item.vin}` : item.display_name)));
  const list = $("#obd-record-list");
  if (!state.obd.sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No diagnostic sessions stored.";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...state.obd.sessions.map((item) => {
    const card = document.createElement("article");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    const metadata = document.createElement("small");
    const use = document.createElement("button");
    title.textContent = `#${item.session_id} · ${item.case?.title || item.purpose}`;
    detail.textContent = `${item.customer?.name || "Unassigned"} · ${item.vehicle?.display_name || "Unassigned vehicle"}`;
    metadata.textContent = `${item.status} · ${formatTimestamp(item.started_at)}`;
    use.type = "button";
    use.className = "plain-button";
    use.textContent = "Select";
    use.disabled = Boolean(state.obd.status?.connected);
    use.addEventListener("click", () => {
      $("#obd-session-select").value = String(item.session_id);
      setMessage("#obd-record-message", `Session #${item.session_id} selected for the next OBD connection.`, "success");
    });
    card.append(title, detail, metadata, use);
    return card;
  }));
}

async function loadObdRecords() {
  try {
    const [customers, vehicles, sessions] = await Promise.all([
      getJson("/api/customers"), getJson("/api/vehicles"), getJson("/api/diagnostic-sessions"),
    ]);
    state.obd.customers = customers;
    state.obd.vehicles = vehicles;
    state.obd.sessions = sessions;
    renderObdRecords();
  } catch (error) {
    setMessage("#obd-record-message", `Could not load records: ${error.message}`, "error");
  }
}

function bindObdControls() {
  $("#obd-connect-button").addEventListener("click", async () => {
    const button = $("#obd-connect-button");
    const epoch = ++state.obd.statusEpoch;
    button.disabled = true;
    setMessage("#obd-message", "Connecting and discovering supported PIDs…");
    try {
      const sessionValue = $("#obd-session-select").value;
      const status = await getJson("/api/obd/connect", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({mode: $("#obd-provider-mode").value, session_id: sessionValue ? Number(sessionValue) : null}),
      });
      if (epoch !== state.obd.statusEpoch) return;
      renderObdStatus(status);
      scheduleObdLivePoll();
    } catch (error) {
      if (epoch === state.obd.statusEpoch) {
        renderObdStatus({connected: false, responder_ids: []});
        setMessage("#obd-message", `Connection failed: ${error.message}`, "error");
        button.disabled = false;
      }
    }
  });
  $("#obd-disconnect-button").addEventListener("click", async () => {
    const epoch = ++state.obd.statusEpoch;
    stopObdLivePoll();
    try {
      const status = await getJson("/api/obd/disconnect", {method: "POST"});
      if (epoch === state.obd.statusEpoch) renderObdStatus(status);
    } catch (error) {
      if (epoch === state.obd.statusEpoch) {
        renderObdStatus({connected: false, responder_ids: []});
        setMessage("#obd-message", `Disconnected with cleanup error: ${error.message}`, "error");
      }
    }
  });
  $("#obd-fault-refresh-button").addEventListener("click", readObdFaults);
  $("#obd-fault-save-button").addEventListener("click", () => saveObdSnapshot("faults", "OBD fault and readiness snapshot", "#obd-fault-message"));
  $("#obd-vehicle-refresh-button").addEventListener("click", readObdVehicleInfo);
  $("#obd-vehicle-save-button").addEventListener("click", () => saveObdSnapshot("vehicle_info", "OBD vehicle information", "#obd-vehicle-message"));
  $("#obd-live-save-button").addEventListener("click", () => saveObdSnapshot("live", "OBD live-data snapshot", "#obd-live-message"));
  $("#obd-record-refresh-button").addEventListener("click", loadObdRecords);

  $("#obd-customer-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await getJson("/api/customers", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: $("#obd-customer-name").value, company: $("#obd-customer-company").value || null, phone: $("#obd-customer-phone").value || null, email: $("#obd-customer-email").value || null})});
      event.currentTarget.reset();
      setMessage("#obd-record-message", "Customer created.", "success");
      await loadObdRecords();
    } catch (error) { setMessage("#obd-record-message", `Customer creation failed: ${error.message}`, "error"); }
  });
  $("#obd-vehicle-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await getJson("/api/vehicles", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({display_name: $("#obd-vehicle-name").value, vin: $("#obd-vehicle-vin").value || null, make: $("#obd-vehicle-make").value || null, model: $("#obd-vehicle-model").value || null, year: $("#obd-vehicle-year").value ? Number($("#obd-vehicle-year").value) : null})});
      event.currentTarget.reset();
      setMessage("#obd-record-message", "Vehicle created.", "success");
      await loadObdRecords();
    } catch (error) { setMessage("#obd-record-message", `Vehicle creation failed: ${error.message}`, "error"); }
  });
  $("#obd-session-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const created = await getJson("/api/diagnostic-sessions", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({customer_id: Number($("#obd-session-customer").value), vehicle_id: Number($("#obd-session-vehicle").value), title: $("#obd-session-title").value, purpose: $("#obd-session-purpose").value})});
      await loadObdRecords();
      $("#obd-session-select").value = String(created.session_id);
      setMessage("#obd-record-message", `Diagnostic session #${created.session_id} created and selected.`, "success");
    } catch (error) { setMessage("#obd-record-message", `Session creation failed: ${error.message}`, "error"); }
  });
}

bindTabs();
bindObdTabs();
bindObdControls();
bindScopeControls();
bindScopeCaptureForm();
bindBusSurveyForm();
bindSerialCaptureForm();
bindCanCaptureForm();
bindCanDecodeForm();
bindModbusScanForm();
$("#usb-routing-apply").addEventListener("click", applyUsbRouting);
$("#refresh").addEventListener("click", refreshRuns);
loadScopeProfiles();
loadModbusNetworks();
loadUsbRouting();
refreshStatus();
refreshRuns();
setInterval(refreshStatus, 15_000);
