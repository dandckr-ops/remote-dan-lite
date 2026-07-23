from __future__ import annotations

import pytest

from remote_dan.obd_protocol import (
    OBDProtocolError,
    decode_dtc_payload,
    decode_live_pid,
    decode_readiness,
    decode_supported_pids,
    decode_vin,
    parse_elm_response,
)


FORESTER_SUPPORTED = "7E8 06 41 00 BE 3F A8 13\r>"
FORESTER_VIN = """7E8 10 14 49 02 01 4A 46
7E8 21 32 53 48 36 33 36 38
7E8 22 39 47 37 32 39 30 37
7E8 23 39 00 00 00 00 00 00
>"""


def test_parse_elm_response_extracts_single_frame_payload_by_ecu() -> None:
    payloads = parse_elm_response(FORESTER_SUPPORTED)

    assert payloads == {"7E8": bytes.fromhex("41 00 BE 3F A8 13")}


def test_parse_elm_response_reassembles_multiframe_vin() -> None:
    payloads = parse_elm_response(FORESTER_VIN)

    assert payloads["7E8"] == bytes.fromhex(
        "49 02 01 4A 46 32 53 48 36 33 36 38 39 47 37 32 39 30 37 39"
    )


def test_parse_elm_response_keeps_multiple_ecu_responders_separate() -> None:
    payloads = parse_elm_response(
        "7E8 04 41 0C 1A F8 00 00\r"
        "7E9 04 41 0C 0F A0 00 00\r>"
    )

    assert payloads["7E8"] == bytes.fromhex("41 0C 1A F8")
    assert payloads["7E9"] == bytes.fromhex("41 0C 0F A0")


def test_parse_elm_response_rejects_isotp_sequence_gap() -> None:
    broken = FORESTER_VIN.replace("7E8 22", "7E8 24")

    with pytest.raises(OBDProtocolError, match="sequence"):
        parse_elm_response(broken)


def test_decode_supported_pid_bitmap_matches_forester_page_zero() -> None:
    payload = parse_elm_response(FORESTER_SUPPORTED)["7E8"]

    supported = decode_supported_pids(payload)

    assert supported == {
        0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D,
        0x0E, 0x0F, 0x10, 0x11, 0x13, 0x15, 0x1C, 0x1F, 0x20,
    }


@pytest.mark.parametrize(
    ("payload_hex", "name", "value", "unit"),
    [
        ("41 04 80", "Calculated engine load", pytest.approx(50.196, abs=0.001), "%"),
        ("41 05 3E", "Engine coolant temperature", 22.0, "°C"),
        ("41 0B 64", "Intake manifold pressure", 100.0, "kPa"),
        ("41 0C 1A F8", "Engine speed", 1726.0, "rpm"),
        ("41 0D 58", "Vehicle speed", 88.0, "km/h"),
        ("41 0F 50", "Intake air temperature", 40.0, "°C"),
        ("41 10 01 7C", "Mass air flow", 3.8, "g/s"),
        ("41 11 2C", "Throttle position", pytest.approx(17.255, abs=0.001), "%"),
        ("41 2F 80", "Fuel level", pytest.approx(50.196, abs=0.001), "%"),
        ("41 42 36 B0", "Control module voltage", 14.0, "V"),
    ],
)
def test_decode_live_pid_applies_sae_formula(
    payload_hex: str,
    name: str,
    value: float,
    unit: str,
) -> None:
    decoded = decode_live_pid(
        bytes.fromhex(payload_hex),
        ecu="7E8",
        sampled_at="2026-07-23T15:46:02+00:00",
    )

    assert decoded["name"] == name
    assert decoded["value"] == value
    assert decoded["unit"] == unit
    assert decoded["ecu"] == "7E8"
    assert decoded["fresh"] is True


def test_decode_readiness_preserves_mil_count_and_supported_monitors() -> None:
    readiness = decode_readiness(bytes.fromhex("41 01 03 07 E5 00"), ecu="7E8")

    assert readiness["mil_on"] is False
    assert readiness["dtc_count"] == 3
    assert readiness["ignition_type"] == "spark"
    assert readiness["incomplete"] == []
    assert {
        "Misfire", "Fuel system", "Comprehensive components", "Catalyst",
        "Evaporative system", "Oxygen sensor", "Oxygen sensor heater", "EGR/VVT",
    } <= set(readiness["supported"])


def test_decode_dtc_payload_returns_stored_codes_and_generic_descriptions() -> None:
    dtcs = decode_dtc_payload(
        bytes.fromhex("43 03 01 02 01 13 00 28"),
        state="stored",
        ecu="7E8",
    )

    assert [item["code"] for item in dtcs] == ["P0102", "P0113", "P0028"]
    assert all(item["state"] == "stored" for item in dtcs)
    assert dtcs[0]["description"].lower().startswith("mass or volume air flow")


def test_decode_dtc_payload_accepts_empty_pending_response() -> None:
    assert decode_dtc_payload(
        bytes.fromhex("47 00"), state="pending", ecu="7E8"
    ) == []


def test_decode_vin_extracts_and_validates_seventeen_characters() -> None:
    vin = decode_vin(parse_elm_response(FORESTER_VIN)["7E8"])

    assert vin == "JF2SH63689G729079"


def test_decode_vin_rejects_invalid_characters() -> None:
    with pytest.raises(OBDProtocolError, match="VIN"):
        decode_vin(bytes.fromhex("49 02 01") + b"JF2SH63689I729079")
