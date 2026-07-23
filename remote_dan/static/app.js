const $ = (selector) => document.querySelector(selector);

function setMessage(text, kind = "") {
  const message = $("#message");
  message.textContent = text;
  message.className = `message ${kind}`.trim();
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
    $("#service-state").classList.add("ready");
    $("#service-state span").textContent = "Capture service ready";
    $("#hostname").textContent = status.hostname;
    $("#service-version").textContent = `v${status.version}`;
    $("#backend").textContent = status.default_backend;
    $("#driver").textContent = status.hardware.driver_available ? "available" : "not installed";
    $("#device").textContent = status.hardware.device_present ? "detected" : "not attached";
    $("#hardware-note").textContent = status.hardware.reason;
  } catch (error) {
    $("#service-state span").textContent = "Service unavailable";
    $("#hardware-note").textContent = error.message;
  }
}

function artifactUrl(run, name) {
  return `/artifacts/${encodeURIComponent(run.run_id)}/${name}`;
}

function showLatest(run) {
  if (!run) return;
  const overview = $("#overview");
  overview.src = `${artifactUrl(run, "overview.png")}?v=${encodeURIComponent(run.captured_at)}`;
  overview.classList.remove("hidden");
  $("#empty-preview").classList.add("hidden");
  const report = $("#latest-report");
  report.href = artifactUrl(run, "report.pdf");
  report.classList.remove("hidden");
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
    const when = new Date(run.captured_at).toLocaleString();
    card.innerHTML = `
      <div><strong></strong><small></small></div>
      <span class="sim"></span>
      <span></span>
      <nav>
        <a>PNG</a><a>CSV</a><a>PDF</a>
      </nav>`;
    card.querySelector("strong").textContent = run.label;
    card.querySelector("small").textContent = when;
    const spans = card.querySelectorAll(":scope > span");
    spans[0].textContent = run.backend.toUpperCase();
    spans[1].textContent = `${run.preset.toUpperCase()} · ${run.samples.toLocaleString()} SAMPLES`;
    const links = card.querySelectorAll("a");
    links[0].href = artifactUrl(run, "overview.png");
    links[1].href = artifactUrl(run, "capture.csv");
    links[2].href = artifactUrl(run, "report.pdf");
    return card;
  }));
  showLatest(runs[0]);
}

async function refreshRuns() {
  try {
    renderRuns(await getJson("/api/captures"));
  } catch (error) {
    setMessage(`Could not load evidence list: ${error.message}`, "error");
  }
}

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
      }),
    });
    setMessage(`${run.run_id} completed. Field Journal package is ready.`, "success");
    await refreshRuns();
  } catch (error) {
    setMessage(`Capture failed: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
});

$("#refresh").addEventListener("click", refreshRuns);
refreshStatus();
refreshRuns();
setInterval(refreshStatus, 15_000);
