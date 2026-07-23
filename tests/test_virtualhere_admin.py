from __future__ import annotations

from pathlib import Path

import pytest

from remote_dan.virtualhere_admin import VirtualHereApplyError, apply_allowed_devices


def test_apply_writes_only_the_allowlist_then_restarts_service(tmp_path: Path) -> None:
    config = tmp_path / "config.ini"
    config.write_text("TCPPort=7575\nAllowedDevices=10c4/ea60\nClaimPorts=0\n", encoding="utf-8")
    calls: list[str] = []

    apply_allowed_devices(
        config,
        ("084f/c050",),
        restart=lambda: calls.append("restart"),
        is_active=lambda: True,
    )

    assert config.read_text(encoding="utf-8") == "TCPPort=7575\nAllowedDevices=084f/c050\nClaimPorts=0\n"
    assert calls == ["restart"]


def test_apply_restores_prior_config_when_restarted_service_is_not_healthy(tmp_path: Path) -> None:
    config = tmp_path / "config.ini"
    original = "TCPPort=7575\nAllowedDevices=10c4/ea60\n"
    config.write_text(original, encoding="utf-8")
    calls: list[str] = []

    with pytest.raises(VirtualHereApplyError, match="restored"):
        apply_allowed_devices(
            config,
            ("084f/c050",),
            restart=lambda: calls.append("restart"),
            is_active=lambda: False,
        )

    assert config.read_text(encoding="utf-8") == original
    assert calls == ["restart", "restart"]
