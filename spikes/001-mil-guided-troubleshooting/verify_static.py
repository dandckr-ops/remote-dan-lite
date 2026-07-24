from pathlib import Path

ROOT = Path(__file__).parent
html = (ROOT / "index.html").read_text(encoding="utf-8")
css = (ROOT / "styles.css").read_text(encoding="utf-8")
js = (ROOT / "app.js").read_text(encoding="utf-8")

required_html = [
    "MIL-Aware Guided Troubleshooting",
    "INTERACTIVE CONCEPT · MOCK DATA",
    "P0102",
    "P0113",
    "P0028",
    'id="start-guide"',
    'id="guided-workflow"',
    'id="event-koeo"',
    'id="event-idle"',
    'id="event-2500"',
    'id="event-snap"',
    'id="escalate-scope"',
    'id="scope-handoff"',
    "Vehicle-specific pinout required",
    "Evidence collected",
    "lb/min",
    "g/s",
]
for token in required_html:
    assert token in html, f"missing HTML contract: {token}"

for token in ["@media", "--live", ".workflow-step", ".pid-card", ".scope-channel", ".mock-banner"]:
    assert token in css, f"missing CSS contract: {token}"

for token in ["eventProfiles", "setActiveEvent", "start-guide", "escalate-scope", "aria-current"]:
    assert token in js, f"missing interaction contract: {token}"

assert "Mode $04" not in html
assert "Clear codes" not in html
print("MIL_GUIDED_MOCK_STATIC_CONTRACT_OK")
