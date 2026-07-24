from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
STATIC_DIR = ROOT / "remote_dan" / "static"
EXPECTED_TABS = [
    "overview", "bus-sniffer", "scope", "serial", "can", "obd", "modbus",
    "load-bank", "tests", "timeline", "evidence",
]
TOUR_EXPECTED_TABS = [
    "overview", "bus-sniffer", "scope", "serial", "can", "obd", "modbus",
    "tests", "timeline", "evidence",
]


class ConsoleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tabs: list[dict[str, str]] = []
        self.panels: list[dict[str, str]] = []
        self.ids: set[str] = set()
        self._active_tab: dict[str, str] | None = None
        self._active_tab_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "button" and values.get("role") == "tab":
            self._active_tab = values
            self._active_tab_text = []
        if values.get("role") == "tabpanel":
            self.panels.append(values)

    def handle_data(self, data: str) -> None:
        if self._active_tab is not None:
            self._active_tab_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._active_tab is not None:
            self._active_tab["text"] = " ".join("".join(self._active_tab_text).split())
            self.tabs.append(self._active_tab)
            self._active_tab = None
            self._active_tab_text = []


def parse_console() -> ConsoleParser:
    parser = ConsoleParser()
    parser.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    parser.close()
    return parser


def test_console_declares_eleven_accessible_primary_tabs() -> None:
    parser = parse_console()
    primary_tabs = [tab for tab in parser.tabs if "data-tab" in tab]
    primary_panels = [panel for panel in parser.panels if "data-panel" in panel]

    assert [tab["data-tab"] for tab in primary_tabs] == EXPECTED_TABS
    assert all(
        tab["text"].lower().endswith(name.replace("-", " "))
        for tab, name in zip(primary_tabs, EXPECTED_TABS, strict=True)
    )
    assert [panel["data-panel"] for panel in primary_panels] == EXPECTED_TABS

    for index, name in enumerate(EXPECTED_TABS):
        tab = primary_tabs[index]
        panel = primary_panels[index]
        assert tab["id"] == f"tab-{name}"
        assert tab["aria-controls"] == f"panel-{name}"
        assert tab["aria-selected"] == ("true" if index == 0 else "false")
        assert panel["id"] == f"panel-{name}"
        assert panel["aria-labelledby"] == f"tab-{name}"
        assert ("hidden" in panel) is (index != 0)


def test_console_tour_is_an_accessible_offline_preview_without_live_actions() -> None:
    tour = ROOT / "docs" / "console-tour.html"
    markup = tour.read_text(encoding="utf-8")

    assert "Offline interactive documentation preview" in markup
    assert "representative data" in markup
    assert "No appliance APIs or live hardware actions" in markup
    assert "fetch(" not in markup
    assert "/api/" not in markup
    assert "<link " not in markup
    assert "<script src=" not in markup

    parser = ConsoleParser()
    parser.feed(markup)
    parser.close()
    assert [tab["data-tab"] for tab in parser.tabs] == TOUR_EXPECTED_TABS
    assert [panel["data-panel"] for panel in parser.panels] == TOUR_EXPECTED_TABS

    for index, name in enumerate(TOUR_EXPECTED_TABS):
        tab = parser.tabs[index]
        panel = parser.panels[index]
        assert tab["id"] == f"tour-tab-{name}"
        assert tab["aria-controls"] == f"tour-panel-{name}"
        assert tab["aria-selected"] == ("true" if index == 0 else "false")
        assert panel["id"] == f"tour-panel-{name}"
        assert panel["aria-labelledby"] == f"tour-tab-{name}"
        assert ("hidden" in panel) is (index != 0)

        panel_markup = markup.split(f'id="tour-panel-{name}"', 1)[1].split("</section>", 1)[0]
        assert "Capability" in panel_markup
        assert "Boundary / safety" in panel_markup

    assert 'addEventListener("keydown"' in markup
    assert '"ArrowRight"' in markup
    assert '"ArrowLeft"' in markup
    assert "window.location.hash" in markup
    assert "hashchange" in markup


def test_readme_tour_link_uses_live_pages_url() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = "[Open the interactive console tour](https://dandckr-ops.github.io/remote-dan-lite/console-tour.html)"

    assert expected in readme
    assert "](docs/console-tour.html)" not in readme


def test_console_declares_accessible_obd_workspace_and_nested_tabs() -> None:
    parser = parse_console()
    obd_tabs = [tab for tab in parser.tabs if "data-obd-tab" in tab]
    obd_panels = [panel for panel in parser.panels if "data-obd-panel" in panel]
    names = ["live-data", "faults", "vehicle-info", "records"]

    assert [tab["data-obd-tab"] for tab in obd_tabs] == names
    assert [panel["data-obd-panel"] for panel in obd_panels] == names
    for index, name in enumerate(names):
        assert obd_tabs[index]["id"] == f"obd-tab-{name}"
        assert obd_tabs[index]["aria-controls"] == f"obd-panel-{name}"
        assert obd_tabs[index]["aria-selected"] == ("true" if index == 0 else "false")
        assert obd_panels[index]["id"] == f"obd-panel-{name}"
        assert obd_panels[index]["aria-labelledby"] == f"obd-tab-{name}"
        assert ("hidden" in obd_panels[index]) is (index != 0)

    required_ids = {
        "obd-session-select", "obd-provider-mode", "obd-connect-button",
        "obd-disconnect-button", "obd-connection-status", "obd-provider",
        "obd-protocol", "obd-ecu-summary", "obd-live-list",
        "obd-live-message", "obd-live-updated", "obd-live-save-button",
        "obd-fault-refresh-button", "obd-fault-save-button", "obd-stored-list",
        "obd-pending-list", "obd-permanent-list", "obd-readiness-list",
        "obd-clear-button", "obd-clear-blocker", "obd-vehicle-refresh-button",
        "obd-vehicle-save-button", "obd-vin", "obd-record-list",
        "obd-customer-form", "obd-vehicle-form", "obd-session-form",
        "obd-vehicle-make", "obd-vehicle-model",
    }
    assert required_ids <= parser.ids

    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    obd_markup = index.split('id="panel-obd"', 1)[1].split('id="panel-modbus"', 1)[0]
    assert "SAE J1979 emissions diagnostics" in obd_markup
    assert "commissioned only for ISO 15765-4 CAN 11/500" in obd_markup
    assert "does not prove the fault was repaired" in obd_markup
    assert "readiness monitors" in obd_markup
    assert "Permanent DTCs generally cannot be erased" in obd_markup
    assert "authenticated operator identity" in obd_markup
    assert 'id="obd-clear-button"' in obd_markup and "disabled" in obd_markup
    assert 'id="obd-live-message" aria-live=' not in obd_markup
    assert 'id="obd-vehicle-make-model"' not in obd_markup


def test_console_exposes_shared_capture_views_and_digital_battery_readout() -> None:
    parser = parse_console()

    required_ids = {
        "scope-capture-form",
        "scope-profile",
        "scope-window",
        "scope-autoscale",
        "scope-apply-20x",
        "scope-reset-profile",
        "scope-primary-label",
        "scope-primary-value",
        "scope-primary-detail",
        "scope-overview",
        "sniffer-form",
        "sniffer-harness",
        "sniffer-button",
        "sniffer-low-voltage",
        "sniffer-common-reference",
        "sniffer-probe-rating",
        "sniffer-passive-only",
        "sniffer-status",
        "sniffer-topology",
        "sniffer-family",
        "sniffer-rate",
        "sniffer-confidence",
        "sniffer-workspace",
        "sniffer-device",
        "sniffer-reason",
        "sniffer-boundary",
        "sniffer-open-tab",
        "sniffer-overview",
        "serial-capture-form",
        "serial-capture-button",
        "serial-message",
        "serial-duration",
        "serial-mode",
        "serial-baud",
        "serial-data-bits",
        "serial-parity",
        "serial-stop-bits",
        "serial-overview",
        "serial-text-preview",
        "serial-hex-preview",
        "serial-analysis-status",
        "serial-byte-count",
        "serial-byte-rate",
        "serial-framing",
        "serial-protocol",
        "serial-valid-frames",
        "serial-printable",
        "serial-errors",
        "serial-confidence",
        "serial-analysis-detail",
        "can-capture-form",
        "can-capture-button",
        "can-message",
        "vbat-value",
        "vbat-detail",
        "can-overview",
        "can-analysis-status",
        "can-load",
        "can-bus-type",
        "can-nominal-rate",
        "can-data-rate",
        "can-protocol",
        "can-id-format",
        "can-frame-count",
        "can-analysis-confidence",
        "can-analysis-detail",
        "modbus-scan-form",
        "modbus-subnet",
        "modbus-mode",
        "modbus-scan-button",
        "modbus-message",
        "modbus-device-count",
        "modbus-confirmed-count",
        "modbus-anybus-count",
        "modbus-write-count",
        "modbus-device-list",
        "modbus-overview",
        "timeline-list",
        "run-list",
    }
    assert required_ids <= parser.ids

    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    overview_markup = index.split('id="panel-overview"', 1)[1].split('id="panel-bus-sniffer"', 1)[0]
    can_markup = index.split('id="panel-can"', 1)[1].split('id="panel-modbus"', 1)[0]
    serial_markup = index.split('id="panel-serial"', 1)[1].split('id="panel-can"', 1)[0]
    sniffer_markup = index.split('id="panel-bus-sniffer"', 1)[1].split('id="panel-scope"', 1)[0]
    modbus_markup = index.split('id="panel-modbus"', 1)[1].split('id="panel-tests"', 1)[0]
    assert "data-vbat-value" not in overview_markup
    assert "data-vbat-detail" not in overview_markup
    assert "Battery voltage" not in overview_markup
    assert "data-vbat-value" in can_markup
    assert "data-vbat-detail" in can_markup
    assert "Battery voltage" in can_markup
    assert 'class="can-battery-gauge"' in can_markup
    assert can_markup.index("data-vbat-value") < can_markup.index('id="can-overview"')
    assert can_markup.index('id="can-overview"') < can_markup.index('id="can-analysis-status"')
    assert 'value="can-analysis"' in can_markup
    assert "Bus load" in can_markup
    assert "Protocol fingerprint" in can_markup
    assert "RX only" in serial_markup
    assert "Configured framing" in serial_markup
    assert "Protocol fingerprint" in serial_markup
    assert "transmit" not in serial_markup.lower()
    assert serial_markup.index('id="serial-overview"') < serial_markup.index('id="serial-analysis-status"')
    assert "unverified — blocked" in sniffer_markup
    assert "Software cannot make an unknown ground connection safe" in sniffer_markup
    assert "no register writes" in modbus_markup
    assert "Writes performed" in modbus_markup
    assert "Not implemented" in modbus_markup
    assert index.count("data-vbat-value") == 1
    assert index.count("data-vbat-detail") == 1
    assert index.count("data-scope-profile=") == 5
    assert index.count("data-scope-channel=") == 4
    for value in (
        "secondary-ignition",
        "crankshaft-vr",
        "crankshaft-hall",
        "injector-primary",
        "1s",
        "2s",
        "5s",
        "10s",
        "Auto-scale ranges",
        "20:1 on enabled",
    ):
        assert value in index


def test_overview_declares_usb_routing_apply_feedback_and_client_flow() -> None:
    parser = parse_console()
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert {"usb-routing-list", "usb-routing-status", "usb-routing-apply", "usb-routing-message"} <= parser.ids
    overview_markup = index.split('id="panel-overview"', 1)[1].split('id="panel-bus-sniffer"', 1)[0]
    assert "USB routing" in overview_markup
    assert "Local to Remote Dan Lite" in overview_markup
    assert "Forward through VirtualHere" in overview_markup
    assert 'id="usb-routing-apply"' in overview_markup
    assert "disabled" in overview_markup.split('id="usb-routing-apply"', 1)[1].split(">", 1)[0]
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "usbRouting" in script
    assert "function loadUsbRouting" in script
    assert 'getJson("/api/usb/devices")' in script
    assert 'getJson("/api/usb/routing/apply"' in script
    assert "inventory_revision" in script
    assert "confirmed: true" in script
    assert "window.confirm" in script
    assert "function usbRoutingChanges" in script
    assert 'route.addEventListener("change"' in script
    assert "loadUsbRouting();" in script
    assert "USB routing failed:" in script


def test_load_bank_tab_declares_ownership_collection_and_evidence_controls() -> None:
    parser = parse_console()
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    loadbank_markup = index.split('id="panel-load-bank"', 1)[1].split(
        'id="panel-tests"', 1
    )[0]
    required_ids = {
        "loadbank-owner-status",
        "loadbank-owner-form",
        "loadbank-owner-off",
        "loadbank-owner-rdl",
        "loadbank-owner-windows",
        "loadbank-owner-apply",
        "loadbank-mode-message",
        "loadbank-discover",
        "loadbank-controller",
        "loadbank-duration",
        "loadbank-customer",
        "loadbank-work-order",
        "loadbank-generator",
        "loadbank-technician",
        "loadbank-start",
        "loadbank-stop",
        "loadbank-captured",
        "loadbank-expected",
        "loadbank-quality",
        "loadbank-session-list",
        "loadbank-message",
    }

    assert required_ids <= parser.ids
    assert "Current Collection Owner" in loadbank_markup
    assert "Off" in loadbank_markup
    assert "Remote Dan Lite" in loadbank_markup
    assert "Windows Workstation" in loadbank_markup
    assert "192.168.1.99:502 · Unit 125" in loadbank_markup
    assert "Local RDL Auto Detect" in loadbank_markup
    assert "RDL polling is disabled" in loadbank_markup
    assert "Windows communicates directly over Ethernet" in loadbank_markup
    assert "routing through RDL" not in loadbank_markup
    assert 'id="loadbank-duration"' in loadbank_markup
    assert 'min="15"' in loadbank_markup
    assert 'max="1440"' in loadbank_markup
    assert 'step="15"' in loadbank_markup
    for control in ("loadbank-discover", "loadbank-controller", "loadbank-start"):
        declaration = loadbank_markup.split(f'id="{control}"', 1)[1].split(">", 1)[0]
        assert "disabled" in declaration


def test_load_bank_script_gates_local_controls_and_uses_only_rdl_proxy() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "loadBank" in script
    assert "function updateLoadBankControls" in script
    assert 'state.loadBank.owner === "rdl"' in script
    assert "discover.disabled = !ownsCollection" in script
    assert "controller.disabled = !ownsCollection" in script
    assert "start.disabled = !ownsCollection" in script
    assert "window.confirm" in script
    assert "Windows collector is stopped" in script
    assert "confirmed_external_stopped" in script
    assert "Boolean(loadBank.activeSession)" in script
    assert '"captured_snapshots"' in script
    assert '"expected_snapshots"' in script
    assert 'getJson("/api/loadbank/status")' in script
    assert 'getJson("/api/loadbank/discovery"' in script
    assert 'getJson("/api/loadbank/ownership"' in script
    assert 'getJson("/api/loadbank/sessions"' in script
    assert 'getJson("/api/loadbank/sessions/active/stop"' in script
    assert 'href = `/api/loadbank/sessions/${encodeURIComponent(sessionUuid)}/download`' in script
    assert "REMOTE_DAN_LOADBANK_PASSWORD" not in script
    assert "upstream_url" not in script


def test_load_bank_mobile_layout_contains_intrinsic_width_and_reveals_active_tab() -> None:
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "/static/app.css?v=obd-fault-summary-v5-loadbank-owner-2" in markup
    assert "/static/app.js?v=obd-fault-summary-v5-loadbank-owner-2" in markup
    assert ".loadbank-owner-choices {" in stylesheet
    assert "min-width: 0" in stylesheet
    assert ".loadbank-owner-choices legend" in stylesheet
    assert "white-space: normal" in stylesheet
    assert "scrollIntoView" in script
    assert 'inline: "center"' in script


def test_console_script_has_valid_javascript_syntax() -> None:
    result = subprocess.run(
        ["node", "--check", str(STATIC_DIR / "app.js")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_console_script_implements_mouse_keyboard_hash_and_voltage_behavior() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "innerHTML" not in script
    assert "function activateTab" in script
    assert 'setAttribute("aria-selected"' in script
    assert ".hidden =" in script
    assert 'addEventListener("keydown"' in script
    assert '"ArrowRight"' in script
    assert '"ArrowLeft"' in script
    assert "window.location.hash" in script
    assert "history.replaceState" in script
    assert "function formatVoltage" in script
    assert ".toFixed(2)" in script
    assert '$("[data-vbat-value]")' in script
    assert '$("[data-vbat-detail]")' in script
    assert "function applyScopeProfile" in script
    assert "function collectScopeChannels" in script
    assert "function suggestScopeRange" in script
    assert "function bindScopeCaptureForm" in script
    assert "function bindCanCaptureForm" in script
    assert "function bindSerialCaptureForm" in script
    assert "function bindBusSurveyForm" in script
    assert "function showBusSurvey" in script
    assert 'getJson("/api/bus-surveys"' in script
    assert "targetTab" in script
    assert "function bindModbusScanForm" in script
    assert "function loadModbusNetworks" in script
    assert "function showModbus" in script
    assert 'getJson("/api/modbus/networks")' in script
    assert 'getJson("/api/modbus/scans"' in script
    assert "function showSerial" in script
    assert "function showCanAnalysis" in script
    assert "function formatBitrate" in script
    assert 'profile: "network"' in script
    assert "preview_channels" in script
    assert "Legacy network preview withheld" in script
    assert "function activateObdTab" in script
    assert "function bindObdTabs" in script
    assert "function loadObdRecords" in script
    assert "function refreshObdStatus" in script
    assert "function scheduleObdLivePoll" in script
    assert "function stopObdLivePoll" in script
    assert "function renderObdLiveData" in script
    assert "function renderObdFaults" in script
    assert "function renderObdVehicleInfo" in script
    assert 'getJson("/api/obd/status")' in script
    assert 'getJson("/api/obd/live"' in script
    assert 'getJson("/api/obd/faults")' in script
    assert 'getJson("/api/obd/vehicle-info")' in script
    assert 'getJson("/api/obd/snapshots"' in script
    assert "state.obd.pollTimer = window.setTimeout" in script
    assert "state.obd.statusEpoch" in script
    assert "epoch !== state.obd.statusEpoch" in script
    assert "clearObdReadings" in script
    assert "createOperationId()" in script
    assert "operation_id: operationId" in script
    assert "state.obd.savePending" in script


def test_can_tab_exposes_accessible_bounded_existing_capture_decode_contract() -> None:
    parser = parse_console()
    required_ids = {
        "can-decode-form",
        "can-decode-source",
        "can-decode-label",
        "can-decode-button",
        "can-decode-message",
        "can-identifier-filter",
        "can-changing-only",
        "can-identifier-table",
        "can-identifier-rows",
        "can-frame-table",
        "can-frame-rows",
        "can-decode-row-truth",
        "can-frames-artifact",
        "can-identifiers-artifact",
    }
    assert required_ids <= parser.ids
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    can_markup = index.split('id="panel-can"', 1)[1].split('id="panel-modbus"', 1)[0]
    assert "Decode existing capture" in can_markup
    assert "generic Classical CAN decoding only" in can_markup
    assert "no DBC or OEM signal meaning" in can_markup
    assert "Identifier inventory" in can_markup
    assert "Validated frames" in can_markup
    assert "aria-live" in can_markup
    assert "<caption" in can_markup

    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for unsafe_api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe_api not in script
    assert "function loadCanDecodeSources" in script
    assert "function bindCanDecodeForm" in script
    assert "function renderCanDecode" in script
    assert 'getJson("/api/can-decode-sources")' in script
    assert 'getJson("/api/can-decodes"' in script
    assert "bindCanDecodeForm();" in script
    assert "frames_truncated" in script
    assert "identifier_limit" in script
    assert '$("#can-decode-button").disabled = !$("#can-decode-source").value;' in script

    styles = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".can-decode-table-scroll" in styles
    assert "overflow-x: auto" in styles
    assert "@media (max-width: 620px)" in styles
    assert "max-width: 100%" in styles
    assert "can-decode-v1" in index


def test_console_styles_obd_workspace_for_desktop_and_mobile() -> None:
    styles = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    for selector in (
        ".obd-toolbar", ".obd-connection-grid", ".obd-tab-bar",
        ".obd-live-grid", ".obd-fault-grid", ".obd-record-grid",
        ".obd-clear-panel", ".danger-button",
    ):
        assert selector in styles
    assert "@media (max-width: 620px)" in styles
    assert ".obd-tab-bar { min-width: 560px; }" in styles
    assert ".obd-tab-bar { min-width: 0; grid-template-columns: 1fr 1fr; }" not in styles


def test_obd_browser_code_fences_reads_recovers_polling_and_has_uuid_fallback() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "faultsEpoch: 0" in script
    assert "vehicleEpoch: 0" in script
    assert "const requestEpoch = ++state.obd.faultsEpoch" in script
    assert "const requestEpoch = ++state.obd.vehicleEpoch" in script
    assert "nextDelay = 5000" in script
    assert "function createOperationId()" in script
    assert "cryptoApi.getRandomValues" in script
    assert "state.obd.saveOperationIds = {}" in script
    assert "const failedDtcStates" in script
    assert "All DTC service reads failed" in script
    assert "nextDelay ?? (state.obd.status?.connected ? 1200 : null)" in script


def test_changed_static_assets_use_combined_release_cache_key() -> None:
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "/static/app.css?v=obd-fault-summary-v5-loadbank-owner-2" in markup
    assert "/static/app.js?v=obd-fault-summary-v5-loadbank-owner-2" in markup
    assert "/static/can_request_gate.js?v=can-decode-v1-remediation2" in markup
    assert "bus-discovery-1" not in markup


def test_obd_fault_and_vehicle_views_state_exact_scope_and_availability() -> None:
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    fault_panel = markup.split('id="obd-panel-faults"', 1)[1].split('id="obd-panel-vehicle-info"', 1)[0]
    vehicle_panel = markup.split('id="obd-panel-vehicle-info"', 1)[1].split('id="obd-panel-records"', 1)[0]

    assert "Generic emissions DTCs only" in fault_panel
    assert "Mode $03 · Confirmed/stored" in fault_panel
    assert "Mode $07 · Pending" in fault_panel
    assert "Mode $0A · Permanent" in fault_panel
    assert all(f'id="obd-{state}-status"' in fault_panel for state in ("stored", "pending", "permanent"))
    assert 'class="obd-fault-summary"' in fault_panel
    assert 'id="obd-confirmed-summary-count"' in fault_panel
    assert 'id="obd-confirmed-summary-label"' in fault_panel
    assert "Vehicle ID" in vehicle_panel
    assert "Mode $09 PID $02 VIN only" in vehicle_panel
    assert "VIN validation" in vehicle_panel
    assert "VIN reporting coverage" in vehicle_panel
    assert 'class="obd-vin-result"' in vehicle_panel
    assert 'id="obd-vin" aria-live="polite" tabindex="-1"' in vehicle_panel
    assert "Read VIN from ECM" in vehicle_panel
    assert "<dt>Protocol</dt>" not in vehicle_panel
    assert "<dt>Adapter</dt>" not in vehicle_panel
    assert "<dt>Detected ECUs</dt>" not in vehicle_panel
    assert 'payload.vin_status === "partial"' in script
    assert 'payload.vin_status === "no_data"' in script
    assert "VIN READ SUCCESSFULLY" in script
    assert 'status === "no_data" ? "Unavailable"' in script
    assert "CONFIRMED/STORED EMISSIONS DTC" in script
    assert ".obd-vin-result" in (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".obd-fault-summary" in (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def test_operator_source_selectors_do_not_offer_simulator_or_auto_fallback() -> None:
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    expected = {
        "sniffer-mode": "hardware",
        "scope-mode": "hardware",
        "serial-mode": "hardware",
        "can-mode": "hardware",
        "obd-provider-mode": "hardware",
        "modbus-mode": "network",
    }

    for select_id, only_value in expected.items():
        select = markup.split(f'id="{select_id}"', 1)[1].split("</select>", 1)[0]
        assert 'value="simulator"' not in select
        assert 'value="auto"' not in select
        assert select.count("<option") == 1
        assert f'value="{only_value}"' in select

    assert "Capture a receive window from the C662 or simulator." not in markup
    assert "Simulator selected; no physical signal connection is used." not in (
        STATIC_DIR / "app.js"
    ).read_text(encoding="utf-8")
