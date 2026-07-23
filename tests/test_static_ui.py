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
        "capture-form",
        "vbat-value",
        "vbat-detail",
        "scope-vbat-value",
        "scope-vbat-detail",
        "overview",
        "can-overview",
        "timeline-list",
        "run-list",
    }
    assert required_ids <= parser.ids

    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert index.count("data-vbat-value") == 2
    assert index.count("data-vbat-detail") == 2


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
    assert '$$("[data-vbat-value]")' in script
    assert '$$("[data-vbat-detail]")' in script
