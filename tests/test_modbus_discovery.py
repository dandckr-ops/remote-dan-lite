from __future__ import annotations

import json
import struct

import pytest

from remote_dan.modbus_discovery import (
    HICPDevice,
    ModbusDevice,
    build_device_id_request,
    parse_connected_networks,
    parse_device_id_response,
    parse_hicp_response,
    scan_modbus_network,
    validate_scan_subnet,
)


HICP_SAMPLE = (
    b"Protocol version = 1.30;FB type = ModbusGW;Module version = 1.22.0;"
    b"Kernel version = 1.2.25;MAC = 00-30-11-FA-82-C5;IP = 192.168.1.99;"
    b"SN = 255.255.255.0;GW = 192.168.1.1;DHCP = OFF;PSWD = ON;HN = admin;"
    b"S = transient-secret;\x00"
)


def _device_id_response(
    *, transaction_id: int = 0x5244, unit_id: int = 0xFF
) -> bytes:
    objects = (
        bytes([0x00, 0x03]) + b"HMS"
        + bytes([0x01, 0x06]) + b"Anybus"
        + bytes([0x02, 0x06]) + b"1.22.0"
    )
    pdu = bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, 0x03]) + objects
    return struct.pack(">HHHB", transaction_id, 0, len(pdu) + 1, unit_id) + pdu


def test_parse_hicp_response_returns_only_safe_validated_identity_fields() -> None:
    device = parse_hicp_response(HICP_SAMPLE, source_ip="192.168.1.99")

    assert device.ip == "192.168.1.99"
    assert device.source_ip == "192.168.1.99"
    assert device.mac == "00:30:11:FA:82:C5"
    assert device.fieldbus_type == "ModbusGW"
    assert device.module_version == "1.22.0"
    assert device.address_scope == "on_link"
    assert device.datagram_length == len(HICP_SAMPLE)
    assert len(device.datagram_sha256) == 64
    assert "transient-secret" not in json.dumps(device.as_dict())
    assert device.as_dict()["state"] == "hicp_identity_observed"
    assert device.as_dict()["confidence"] == "medium"


@pytest.mark.parametrize(
    "payload",
    [
        HICP_SAMPLE.replace(b"MAC = 00-30-11-FA-82-C5;", b""),
        HICP_SAMPLE.replace(b"IP = 192.168.1.99;", b"IP = gateway.local;"),
        HICP_SAMPLE.replace(b"MAC = 00-30-11-FA-82-C5;", b"MAC = not-a-mac;"),
        HICP_SAMPLE[:-1],
        HICP_SAMPLE.replace(b"DHCP = OFF;", b"DHCP = MAYBE;"),
        HICP_SAMPLE.replace(b"SN = 255.255.255.0;", b"SN = 255.0.255.0;"),
        HICP_SAMPLE.replace(b"MAC = 00-30-11-FA-82-C5;", b"MAC = 01-30-11-FA-82-C5;"),
        HICP_SAMPLE.replace(b"DHCP = OFF;", b"DHCP = OFF;DHCP = ON;"),
        HICP_SAMPLE.replace(b"FB type = ModbusGW;", b"FB type = Mod\x00busGW;"),
        b"not hicp",
    ],
)
def test_parse_hicp_response_rejects_forged_or_malformed_packets(payload: bytes) -> None:
    with pytest.raises((UnicodeError, ValueError)):
        parse_hicp_response(payload, source_ip="192.168.1.99")


def test_build_and_parse_read_device_identification_transaction() -> None:
    request = build_device_id_request(transaction_id=0x5244, unit_id=0xFF)

    assert request == bytes.fromhex("524400000005ff2b0e0100")
    parsed = parse_device_id_response(
        _device_id_response(), transaction_id=0x5244, unit_id=0xFF
    )
    assert parsed == {
        "confirmed": True,
        "exception_code": None,
        "read_device_id_code": 1,
        "conformity_level": 1,
        "more_follows": False,
        "next_object_id": 0,
        "objects": {"vendor_name": "HMS", "product_code": "Anybus", "revision": "1.22.0"},
    }


def test_valid_modbus_exception_confirms_protocol_without_claiming_identity() -> None:
    pdu = bytes([0xAB, 0x01])
    response = struct.pack(">HHHB", 0x5244, 0, len(pdu) + 1, 1) + pdu

    parsed = parse_device_id_response(response, transaction_id=0x5244, unit_id=1)

    assert parsed["confirmed"] is True
    assert parsed["exception_code"] == 1
    assert parsed["objects"] == {}


@pytest.mark.parametrize(
    "response",
    [
        _device_id_response(transaction_id=7),
        struct.pack(">HHHB", 0x5244, 1, 3, 0xFF) + bytes([0xAB, 1]),
        struct.pack(">HHHB", 0x5244, 0, 250, 0xFF) + bytes([0xAB, 1]),
        struct.pack(">HHHB", 0x5244, 0, 3, 0xFF) + bytes([0xAB, 7]),
        _device_id_response()[:-1],
    ],
)
def test_device_identification_rejects_invalid_mbap_or_partial_payload(response: bytes) -> None:
    with pytest.raises(ValueError, match="Modbus"):
        parse_device_id_response(response, transaction_id=0x5244, unit_id=0xFF)


def test_connected_networks_and_scan_scope_are_bounded_to_live_private_interfaces() -> None:
    payload = json.dumps(
        [
            {
                "ifname": "eth0",
                "ifindex": 2,
                "addr_info": [
                    {"family": "inet", "local": "192.168.50.20", "prefixlen": 24, "scope": "global"}
                ],
            },
            {
                "ifname": "lo",
                "addr_info": [
                    {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}
                ],
            },
        ]
    )

    networks = parse_connected_networks(payload)
    selected = validate_scan_subnet(
        "192.168.50.0/24", connected_networks=networks, max_hosts=256
    )

    assert networks == ({
        "interface": "eth0",
        "ifindex": 2,
        "address": "192.168.50.20",
        "network": "192.168.50.0/24",
    },)
    assert str(selected) == "192.168.50.0/24"


@pytest.mark.parametrize(
    ("subnet", "message"),
    [
        ("192.168.48.0/22", "at most"),
        ("10.10.10.0/24", "connected"),
        ("8.8.8.0/24", "RFC1918"),
        ("192.0.2.0/24", "RFC1918"),
        ("::1/128", "IPv4"),
        ("192.168.50.20/32", "ordinary"),
    ],
)
def test_scan_scope_rejects_broad_routed_public_or_ipv6_targets(
    subnet: str, message: str
) -> None:
    connected = (
        {"interface": "eth0", "address": "192.168.50.20", "network": "192.168.48.0/22"},
        {"interface": "eth1", "address": "8.8.8.8", "network": "8.8.8.0/24"},
    )
    with pytest.raises(ValueError, match=message):
        validate_scan_subnet(subnet, connected_networks=connected, max_hosts=256)


def test_link_local_interface_inventory_is_retained() -> None:
    networks = parse_connected_networks([{
        "ifname": "eth0",
        "ifindex": 2,
        "addr_info": [{
            "family": "inet",
            "local": "169.254.10.20",
            "prefixlen": 24,
            "scope": "link",
        }],
    }])

    assert networks[0]["network"] == "169.254.10.0/24"


def test_scan_lists_hicp_gateway_and_confirmed_modbus_device_without_probing_gateway() -> None:
    in_scope_hicp = HICP_SAMPLE.replace(b"192.168.1.99", b"192.168.1.30")
    gateway = parse_hicp_response(in_scope_hicp, source_ip="192.168.1.30")
    probes: list[str] = []

    def probe(host: str, **_: object) -> ModbusDevice | None:
        probes.append(host)
        if host == "192.168.1.20":
            return ModbusDevice(
                ip=host,
                port=502,
                state="confirmed",
                confidence="high",
                unit_id=0xFF,
                latency_ms=12.5,
                vendor_name="Basler Electric",
                product_code="DGC-2020HD",
                revision="1.0",
                exception_code=None,
            )
        return None

    report = scan_modbus_network(
        "192.168.1.16/28",
        connected_networks=(
            {"interface": "eth0", "address": "192.168.1.18", "network": "192.168.1.0/24"},
        ),
        hicp_discoverer=lambda **_: (gateway,),
        modbus_probe=probe,
        workers=2,
    )

    assert "192.168.1.30" not in probes
    assert [item["kind"] for item in report["devices"]] == ["modbus_tcp", "anybus_hicp"]
    assert report["devices"][0]["ip"] == "192.168.1.20"
    assert report["devices"][1]["ip"] == "192.168.1.30"
    assert report["writes_performed"] == 0
    assert report["scope"]["host_count"] == 14
