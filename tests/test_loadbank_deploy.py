from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_remote_dan_unit_wires_only_loopback_collector_and_server_credential() -> None:
    unit = (ROOT / "deploy" / "remote-dan-lite.service").read_text(encoding="utf-8")

    assert "Wants=network-online.target basler-loadbank-web.service" in unit
    assert "REMOTE_DAN_LOADBANK_URL=http://127.0.0.1:8777" in unit
    assert (
        "REMOTE_DAN_LOADBANK_PASSWORD_FILE="
        "/run/credentials/remote-dan-lite.service/loadbank-password"
    ) in unit
    assert "LoadCredential=loadbank-password:/etc/basler-loadbank/web-password" in unit
    assert "REMOTE_DAN_LOADBANK_TIMEOUT_SECONDS=10.0" in unit
    assert "REMOTE_DAN_LOADBANK_ALLOWED_ORIGINS=" in unit
    assert "http://192.168.1.225:8776" in unit
    assert "http://100.81.68.47:8776" in unit
    assert "http://remote-dan-lite-01.blowfish-delta.ts.net:8776" in unit
    assert "REMOTE_DAN_LOADBANK_PASSWORD=" not in unit