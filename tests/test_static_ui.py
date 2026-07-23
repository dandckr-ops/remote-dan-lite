from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC_DIR = ROOT / "remote_dan" / "static"
EXPECTED_TABS = [
    "overview", "bus-sniffer", "scope", "serial", "can", "modbus",
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


def test_console_declares_nine_accessible_primary_tabs() -> None:
    parser = parse_console()

    assert [tab["data-tab"] for tab in parser.tabs] == EXPECTED_TABS
    assert all(
        tab["text"].lower().endswith(name.replace("-", " "))
        for tab, name in zip(parser.tabs, EXPECTED_TABS, strict=True)
    )
    assert [panel["data-panel"] for panel in parser.panels] == EXPECTED_TABS

    for index, name in enumerate(EXPECTED_TABS):
        tab = parser.tabs[index]
        panel = parser.panels[index]
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
    assert [tab["data-tab"] for tab in parser.tabs] == EXPECTED_TABS
    assert [panel["data-panel"] for panel in parser.panels] == EXPECTED_TABS

    for index, name in enumerate(EXPECTED_TABS):
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

    styles = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".can-decode-table-scroll" in styles
    assert "overflow-x: auto" in styles
    assert "@media (max-width: 620px)" in styles
    assert "max-width: 100%" in styles
    assert "can-decode-v1" in index
