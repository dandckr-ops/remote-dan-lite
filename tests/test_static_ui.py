from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC_DIR = ROOT / "remote_dan" / "static"
EXPECTED_TABS = ["overview", "scope", "serial", "can", "tests", "timeline", "evidence"]


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


def test_console_declares_seven_accessible_primary_tabs_without_modbus() -> None:
    parser = parse_console()

    assert [tab["data-tab"] for tab in parser.tabs] == EXPECTED_TABS
    assert all(
        tab["text"].lower().endswith(name)
        for tab, name in zip(parser.tabs, EXPECTED_TABS, strict=True)
    )
    assert [panel["data-panel"] for panel in parser.panels] == EXPECTED_TABS
    assert "modbus" not in {tab["data-tab"] for tab in parser.tabs}

    for index, name in enumerate(EXPECTED_TABS):
        tab = parser.tabs[index]
        panel = parser.panels[index]
        assert tab["id"] == f"tab-{name}"
        assert tab["aria-controls"] == f"panel-{name}"
        assert tab["aria-selected"] == ("true" if index == 0 else "false")
        assert panel["id"] == f"panel-{name}"
        assert panel["aria-labelledby"] == f"tab-{name}"
        assert ("hidden" in panel) is (index != 0)


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
        "timeline-list",
        "run-list",
    }
    assert required_ids <= parser.ids

    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    overview_markup = index.split('id="panel-overview"', 1)[1].split('id="panel-scope"', 1)[0]
    can_markup = index.split('id="panel-can"', 1)[1].split('id="panel-tests"', 1)[0]
    serial_markup = index.split('id="panel-serial"', 1)[1].split('id="panel-can"', 1)[0]
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


def test_console_script_implements_mouse_keyboard_hash_and_voltage_behavior() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

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
    assert "function showSerial" in script
    assert "function showCanAnalysis" in script
    assert "function formatBitrate" in script
    assert 'profile: "network"' in script
    assert "preview_channels" in script
    assert "Legacy network preview withheld" in script
