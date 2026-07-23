from __future__ import annotations

import csv
from pathlib import Path

from fastapi.testclient import TestClient

from remote_dan.app import create_app


def test_scope_profiles_api_exposes_controls_without_network_profile(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/api/scope/profiles")

    assert response.status_code == 200
    payload = response.json()
    assert [profile["name"] for profile in payload["profiles"]] == [
        "general",
        "secondary-ignition",
        "crankshaft-vr",
        "crankshaft-hall",
        "injector-primary",
    ]
    assert payload["presets"]["10s"]["duration_ms"] == 9999.96
    assert payload["input_ranges_v"] == [
        0.02,
        0.05,
        0.1,
        0.2,
        0.5,
        1.0,
        2.0,
        5.0,
        10.0,
        20.0,
    ]
    assert payload["attenuations"] == [1.0, 10.0, 20.0]
    assert payload["couplings"] == ["DC", "AC"]


def test_scope_profile_capture_writes_generic_evidence_without_can_assumptions(
    tmp_path: Path,
) -> None:
    capture_dir = tmp_path / "captures"
    client = TestClient(create_app(data_dir=capture_dir))

    response = client.post(
        "/api/captures",
        json={
            "label": "Hall crank proof",
            "preset": "short",
            "mode": "simulator",
            "capture_type": "scope",
            "profile": "crankshaft-hall",
        },
    )

    assert response.status_code == 201, response.text
    manifest = response.json()
    assert manifest["profile"] == "crankshaft-hall"
    assert manifest["capture_type"] == "scope"
    assert manifest["channels"] == ["Crankshaft Hall"]
    assert manifest["scope_config"][0] == {
        "channel": "A",
        "enabled": True,
        "label": "Crankshaft Hall",
        "input_range_v": 10.0,
        "attenuation": 1.0,
        "coupling": "DC",
        "external_range_v": 10.0,
    }
    assert set(manifest["summary"]["channel_stats"]) == {"Crankshaft Hall"}
    assert "differential_b_minus_c" not in manifest["summary"]

    run_dir = capture_dir / manifest["run_id"]
    with (run_dir / "capture.csv").open(newline="") as handle:
        header = next(csv.reader(handle))
    assert header == ["time_us", "a_crankshaft_hall_v"]
    assert (run_dir / "overview.png").stat().st_size > 0
    assert (run_dir / "report.pdf").stat().st_size > 0


def test_scope_capture_accepts_valid_per_channel_overrides(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    channels = [
        {
            "channel": "A",
            "enabled": True,
            "label": "Pressure transducer",
            "input_range_v": 0.2,
            "attenuation": 20.0,
            "coupling": "DC",
        },
        *(
            {
                "channel": letter,
                "enabled": False,
                "label": f"Channel {letter}",
                "input_range_v": 20.0,
                "attenuation": 1.0,
                "coupling": "DC",
            }
            for letter in ("B", "C", "D")
        ),
    ]

    response = client.post(
        "/api/captures",
        json={
            "label": "custom range proof",
            "preset": "short",
            "mode": "simulator",
            "capture_type": "scope",
            "profile": "general",
            "channels": channels,
        },
    )

    assert response.status_code == 201, response.text
    manifest = response.json()
    assert manifest["channels"] == ["Pressure transducer"]
    assert manifest["scope_config"][0]["input_range_v"] == 0.2
    assert manifest["scope_config"][0]["attenuation"] == 20.0
    assert manifest["scope_config"][0]["external_range_v"] == 4.0


def test_scope_channel_validation_fails_closed_on_bad_or_unsafe_configs(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    base = {
        "label": "invalid scope config",
        "preset": "short",
        "mode": "simulator",
        "capture_type": "scope",
        "profile": "general",
    }

    no_channels = client.post(
        "/api/captures",
        json={**base, "channels": []},
    )
    assert no_channels.status_code == 422

    unsafe_20_to_1 = client.post(
        "/api/captures",
        json={
            **base,
            "channels": [
                {
                    "channel": letter,
                    "enabled": letter == "A",
                    "label": f"Channel {letter}",
                    "input_range_v": 50.0 if letter == "A" else 20.0,
                    "attenuation": 20.0 if letter == "A" else 1.0,
                    "coupling": "DC",
                }
                for letter in ("A", "B", "C", "D")
            ],
        },
    )
    assert unsafe_20_to_1.status_code == 422
    assert "input_range_v" in str(unsafe_20_to_1.json()["detail"])

    network_override = client.post(
        "/api/captures",
        json={
            **base,
            "profile": "network",
            "channels": [
                {
                    "channel": letter,
                    "enabled": letter == "A",
                    "label": f"Channel {letter}",
                    "input_range_v": 20.0,
                    "attenuation": 1.0,
                    "coupling": "DC",
                }
                for letter in ("A", "B", "C", "D")
            ],
        },
    )
    assert network_override.status_code == 422
    assert "network profile" in network_override.json()["detail"]


def test_can_analysis_window_is_network_only_and_returns_analysis(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))

    valid = client.post(
        "/api/captures",
        json={
            "label": "CAN signal intelligence",
            "preset": "can-analysis",
            "mode": "simulator",
            "capture_type": "can",
            "profile": "network",
        },
    )
    assert valid.status_code == 201, valid.text
    assert valid.json()["summary"]["can_analysis"]["status"] == "analyzed"

    invalid = client.post(
        "/api/captures",
        json={
            "label": "wrong lane",
            "preset": "can-analysis",
            "mode": "simulator",
            "capture_type": "scope",
            "profile": "general",
        },
    )
    assert invalid.status_code == 422
    assert "commissioned network harness" in invalid.json()["detail"]
