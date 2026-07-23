from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import ipaddress
import json
import math
import re
import secrets
import socket
import struct
import subprocess
import time
from typing import Callable, Iterable, Sequence


MAX_SCAN_HOSTS = 256
MAX_SCAN_WORKERS = 8
MAX_HICP_DEVICES = 32
DEFAULT_SCAN_WORKERS = 4
DEVICE_ID_UNIT = 0xFF
_HICP_REQUIRED = (
    "protocol version", "fb type", "module version", "mac", "ip",
    "sn", "gw", "dhcp", "pswd",
)
_MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}$")
_VALID_EXCEPTIONS = {1, 2, 3, 4, 5, 6, 8, 10, 11}
_OBJECT_NAMES = {
    0x00: "vendor_name",
    0x01: "product_code",
    0x02: "revision",
    0x03: "vendor_url",
    0x04: "product_name",
    0x05: "model_name",
    0x06: "user_application_name",
}


@dataclass(frozen=True)
class HICPDevice:
    source_ip: str
    ip: str
    mac: str
    protocol_version: str
    fieldbus_type: str
    module_version: str
    subnet_mask: str
    gateway: str
    dhcp: str
    password_protected: str
    hostname: str
    address_scope: str = "on_link"
    datagram_length: int = 0
    datagram_sha256: str = ""
    ingress_ifindex: int | None = None

    @property
    def is_modbus_gateway(self) -> bool:
        return self.fieldbus_type.strip().lower() == "modbusgw"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "anybus_hicp",
            "state": "hicp_identity_observed",
            "confidence": "medium" if self.address_scope == "on_link" else "low",
            **asdict(self),
            "is_modbus_gateway": self.is_modbus_gateway,
        }


@dataclass(frozen=True)
class ModbusDevice:
    ip: str
    port: int
    state: str
    confidence: str
    unit_id: int
    latency_ms: float
    vendor_name: str | None
    product_code: str | None
    revision: str | None
    exception_code: int | None

    def as_dict(self) -> dict[str, object]:
        return {"kind": "modbus_tcp", **asdict(self)}


@dataclass(frozen=True)
class ModbusProbeResult:
    device: ModbusDevice | None
    transaction: dict[str, object]


def _finite_positive(name: str, value: float, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be greater than 0 and no more than {maximum}")
    return value


def _bounded_int(name: str, value: int, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _ipv4(value: str, field: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"HICP {field} must be an IPv4 literal") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(f"HICP {field} must be an IPv4 literal")
    return address


def _normalize_mac(value: str) -> str:
    if not _MAC.fullmatch(value):
        raise ValueError("HICP MAC must contain six hexadecimal octets")
    canonical = ":".join(part.upper() for part in value.replace("-", ":").split(":"))
    octets = bytes.fromhex(canonical.replace(":", ""))
    if octets == b"\x00" * 6 or octets == b"\xff" * 6 or octets[0] & 1:
        raise ValueError("HICP MAC must be a nonzero unicast address")
    return canonical


def parse_hicp_response(payload: bytes, *, source_ip: str) -> HICPDevice:
    if not 1 <= len(payload) <= 2048:
        raise ValueError("HICP datagram must contain 1 to 2048 bytes")
    if not payload.endswith(b"\x00") or b"\x00" in payload[:-1]:
        raise ValueError("HICP datagram must contain exactly one terminal NUL")
    text = payload[:-1].decode("ascii")
    if any(ord(character) < 32 or ord(character) > 126 for character in text):
        raise ValueError("HICP fields must contain printable ASCII")
    fields: dict[str, str] = {}
    items = [item for item in text.split(";") if item.strip()]
    if len(items) > 32:
        raise ValueError("HICP datagram contains too many fields")
    for item in items:
        if "=" not in item:
            raise ValueError("HICP fields require a key/value separator")
        key, value = item.split("=", 1)
        normalized = key.strip().lower()
        if normalized in fields:
            raise ValueError(f"duplicate HICP field: {normalized}")
        fields[normalized] = value.strip()
    missing = [name for name in _HICP_REQUIRED if not fields.get(name)]
    if missing:
        raise ValueError(f"missing required HICP fields: {', '.join(missing)}")
    source = _ipv4(source_ip, "source IP")
    advertised = _ipv4(fields["ip"], "IP")
    if source.is_multicast or source.is_unspecified or advertised.is_multicast or advertised.is_unspecified:
        raise ValueError("HICP source and advertised IP must be usable unicast addresses")
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){1,3}", fields["protocol version"]):
        raise ValueError("HICP protocol version has invalid syntax")
    subnet_mask = fields.get("sn", "")
    gateway = fields.get("gw", "")
    if subnet_mask:
        _ipv4(subnet_mask, "subnet mask")
        try:
            ipaddress.ip_network(f"0.0.0.0/{subnet_mask}")
        except ValueError as exc:
            raise ValueError("HICP subnet mask must be contiguous") from exc
    if gateway:
        parsed_gateway = _ipv4(gateway, "gateway")
        if parsed_gateway.is_multicast or parsed_gateway.is_loopback:
            raise ValueError("HICP gateway must be unicast or 0.0.0.0")
    for field in ("dhcp", "pswd"):
        if fields[field] not in {"ON", "OFF"}:
            raise ValueError(f"HICP {field.upper()} must be ON or OFF")
    for field in ("protocol version", "fb type", "module version", "hn"):
        if len(fields.get(field, "")) > 128:
            raise ValueError(f"HICP {field} exceeds the field limit")
    return HICPDevice(
        source_ip=str(source),
        ip=str(advertised),
        mac=_normalize_mac(fields["mac"]),
        protocol_version=fields["protocol version"],
        fieldbus_type=fields["fb type"],
        module_version=fields.get("module version", ""),
        subnet_mask=subnet_mask,
        gateway=gateway,
        dhcp=fields.get("dhcp", ""),
        password_protected=fields.get("pswd", ""),
        hostname=fields.get("hn", ""),
        address_scope="on_link" if source == advertised else "conflict",
        datagram_length=len(payload),
        datagram_sha256=hashlib.sha256(payload).hexdigest(),
    )


def build_device_id_request(*, transaction_id: int, unit_id: int) -> bytes:
    if not 0 <= transaction_id <= 0xFFFF:
        raise ValueError("transaction_id must be between 0 and 65535")
    if not 0 <= unit_id <= 0xFF:
        raise ValueError("unit_id must be between 0 and 255")
    pdu = bytes([0x2B, 0x0E, 0x01, 0x00])
    return struct.pack(">HHHB", transaction_id, 0, len(pdu) + 1, unit_id) + pdu


def parse_device_id_response(
    response: bytes, *, transaction_id: int, unit_id: int
) -> dict[str, object]:
    if len(response) < 9:
        raise ValueError("invalid Modbus response: incomplete MBAP/PDU")
    returned_id, protocol_id, length, returned_unit = struct.unpack(">HHHB", response[:7])
    if returned_id != transaction_id:
        raise ValueError("invalid Modbus response: transaction ID mismatch")
    if protocol_id != 0:
        raise ValueError("invalid Modbus response: protocol ID is not zero")
    if returned_unit != unit_id:
        raise ValueError("invalid Modbus response: Unit ID mismatch")
    if not 3 <= length <= 254 or len(response) != 6 + length:
        raise ValueError("invalid Modbus response: MBAP length mismatch")
    pdu = response[7:]
    if pdu[0] == 0xAB:
        if len(pdu) != 2 or pdu[1] not in _VALID_EXCEPTIONS:
            raise ValueError("invalid Modbus exception response")
        return {
            "confirmed": True,
            "exception_code": pdu[1],
            "read_device_id_code": None,
            "conformity_level": None,
            "more_follows": False,
            "next_object_id": None,
            "objects": {},
        }
    if len(pdu) < 7 or pdu[0:2] != b"\x2b\x0e" or pdu[2] != 1:
        raise ValueError("invalid Modbus Read Device Identification response")
    if pdu[3] not in (1, 2, 3, 0x81, 0x82, 0x83) or pdu[4] not in (0x00, 0xFF):
        raise ValueError("invalid Modbus device identification control fields")
    object_count = pdu[6]
    if not 1 <= object_count <= 3:
        raise ValueError("invalid Modbus basic device identification object count")
    index = 7
    objects: dict[str, str] = {}
    object_ids: list[int] = []
    for _ in range(object_count):
        if index + 2 > len(pdu):
            raise ValueError("invalid Modbus device identification object header")
        object_id, object_length = pdu[index], pdu[index + 1]
        if object_ids and object_id <= object_ids[-1]:
            raise ValueError("invalid Modbus device identification object order")
        object_ids.append(object_id)
        index += 2
        if index + object_length > len(pdu):
            raise ValueError("invalid Modbus device identification object length")
        raw = pdu[index:index + object_length]
        index += object_length
        key = _OBJECT_NAMES.get(object_id, f"object_{object_id}")
        objects[key] = raw.decode("utf-8", "replace").strip("\x00\r\n ")
    if index != len(pdu):
        raise ValueError("invalid Modbus device identification trailing data")
    if not object_ids or object_ids[0] != 0:
        raise ValueError("invalid Modbus basic identity: first object must be vendor name")
    if pdu[4] == 0x00 and pdu[5] != 0:
        raise ValueError("invalid Modbus device identification continuation state")
    if pdu[4] == 0xFF and pdu[5] <= object_ids[-1]:
        raise ValueError("invalid Modbus next object ID")
    return {
        "confirmed": True,
        "exception_code": None,
        "read_device_id_code": pdu[2],
        "conformity_level": pdu[3],
        "more_follows": pdu[4] == 0xFF,
        "next_object_id": pdu[5],
        "objects": objects,
    }


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Modbus connection closed before response completed")
        data.extend(chunk)
    return bytes(data)


def probe_modbus_device(
    host: str,
    *,
    port: int = 502,
    connect_timeout: float = 0.3,
    response_timeout: float = 1.25,
    transaction_id: int | None = None,
    unit_id: int = DEVICE_ID_UNIT,
) -> ModbusProbeResult:
    connect_timeout = _finite_positive("connect_timeout", connect_timeout, maximum=0.75)
    response_timeout = _finite_positive("response_timeout", response_timeout, maximum=1.5)
    transaction_id = transaction_id or secrets.randbelow(0xFFFF) + 1
    request = build_device_id_request(transaction_id=transaction_id, unit_id=unit_id)
    started = time.monotonic()
    base: dict[str, object] = {
        "target_ip": host,
        "port": port,
        "transaction_id": transaction_id,
        "unit_id": unit_id,
        "request_adu_hex": request.hex(),
        "response_adu_hex": None,
    }
    try:
        connection = socket.create_connection((host, port), timeout=connect_timeout)
    except socket.timeout:
        return ModbusProbeResult(None, {**base, "outcome": "connect_timeout"})
    except ConnectionRefusedError as exc:
        return ModbusProbeResult(None, {**base, "outcome": "connect_refused", "errno": exc.errno})
    except OSError as exc:
        return ModbusProbeResult(None, {**base, "outcome": "connect_error", "errno": exc.errno})
    with connection:
        try:
            connection.settimeout(response_timeout)
            connection.sendall(request)
            header = _receive_exact(connection, 7)
            _, _, length, _ = struct.unpack(">HHHB", header)
            if not 3 <= length <= 254:
                raise ValueError("invalid Modbus response: MBAP length out of range")
            response = header + _receive_exact(connection, length - 1)
            parsed = parse_device_id_response(
                response, transaction_id=transaction_id, unit_id=unit_id
            )
            objects = parsed["objects"]
            complete_identity = all(objects.get(name) for name in (
                "vendor_name", "product_code", "revision"
            ))
            state = (
                "modbus_confirmed_exception"
                if parsed["exception_code"] is not None
                else (
                    "identity_confirmed"
                    if complete_identity
                    else "modbus_confirmed_identity_partial"
                )
            )
            device = ModbusDevice(
                ip=host,
                port=port,
                state=state,
                confidence="high" if complete_identity else "medium",
                unit_id=unit_id,
                latency_ms=(time.monotonic() - started) * 1000.0,
                vendor_name=objects.get("vendor_name"),
                product_code=objects.get("product_code"),
                revision=objects.get("revision"),
                exception_code=parsed["exception_code"],
            )
            outcome = (
                "confirmed_exception"
                if parsed["exception_code"] is not None
                else "confirmed_identity"
            )
            return ModbusProbeResult(device, {
                **base,
                "response_adu_hex": response[:260].hex(),
                "outcome": outcome,
                "latency_ms": device.latency_ms,
                "objects": objects,
                "exception_code": parsed["exception_code"],
            })
        except socket.timeout:
            outcome = "response_timeout"
            response = b""
        except ConnectionError:
            outcome = "eof"
            response = b""
        except OSError as exc:
            outcome = "reset"
            response = b""
            base["errno"] = exc.errno
        except ValueError as exc:
            outcome = "malformed"
            response = locals().get("response", b"")
            base["error"] = str(exc)
        device = ModbusDevice(
            ip=host,
            port=port,
            state="tcp_502_candidate",
            confidence="low",
            unit_id=unit_id,
            latency_ms=(time.monotonic() - started) * 1000.0,
            vendor_name=None,
            product_code=None,
            revision=None,
            exception_code=None,
        )
        return ModbusProbeResult(device, {
            **base,
            "response_adu_hex": response[:260].hex() if response else None,
            "outcome": outcome,
            "latency_ms": device.latency_ms,
        })


def parse_connected_networks(payload: str | bytes | Sequence[dict[str, object]]) -> tuple[dict[str, str], ...]:
    records = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    networks: list[dict[str, str]] = []
    for record in records:
        interface = str(record.get("ifname", ""))
        if not interface or interface == "lo":
            continue
        for address in record.get("addr_info", []):
            if address.get("family") != "inet":
                continue
            local = ipaddress.ip_address(str(address.get("local", "")))
            if not isinstance(local, ipaddress.IPv4Address) or local.is_loopback:
                continue
            scope = address.get("scope")
            if scope != "global" and not (scope == "link" and local.is_link_local):
                continue
            prefix = int(address.get("prefixlen", 32))
            network = ipaddress.ip_interface(f"{local}/{prefix}").network
            networks.append({
                "interface": interface,
                "ifindex": int(record.get("ifindex", 0)),
                "address": str(local),
                "network": str(network),
            })
    return tuple(sorted(networks, key=lambda item: (item["interface"], item["network"])))


def connected_ipv4_networks() -> tuple[dict[str, str], ...]:
    completed = subprocess.run(
        ["ip", "-j", "-4", "addr", "show", "up"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return parse_connected_networks(completed.stdout)


def _usable_host_count(network: ipaddress.IPv4Network) -> int:
    if network.prefixlen >= 31:
        return network.num_addresses
    return max(network.num_addresses - 2, 0)


def bounded_scan_networks(
    connected_networks: Sequence[dict[str, str]],
    *,
    max_hosts: int = MAX_SCAN_HOSTS,
) -> tuple[dict[str, str], ...]:
    """Return selectable local scopes that already satisfy the host cap."""
    _bounded_int("max_hosts", max_hosts, maximum=MAX_SCAN_HOSTS)
    choices: list[dict[str, str]] = []
    for item in connected_networks:
        if str(item["interface"]).lower().startswith(
            ("lo", "docker", "br-", "veth", "tailscale", "wg", "tun", "tap")
        ):
            continue
        connected = ipaddress.ip_network(item["network"], strict=False)
        address = ipaddress.ip_address(item["address"])
        if not isinstance(connected, ipaddress.IPv4Network) or not isinstance(
            address, ipaddress.IPv4Address
        ):
            continue
        selected = connected
        while _usable_host_count(selected) > max_hosts:
            selected = ipaddress.ip_network(
                f"{address}/{selected.prefixlen + 1}", strict=False
            )
        choice = {
            "interface": item["interface"],
            "ifindex": item.get("ifindex", 0),
            "address": str(address),
            "network": str(selected),
        }
        if selected != connected:
            choice["connected_network"] = str(connected)
        choices.append(choice)
    return tuple(choices)


def validate_scan_subnet(
    subnet: str,
    *,
    connected_networks: Sequence[dict[str, str]],
    interface: str | None = None,
    max_hosts: int = MAX_SCAN_HOSTS,
) -> ipaddress.IPv4Network:
    _bounded_int("max_hosts", max_hosts, maximum=MAX_SCAN_HOSTS)
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        raise ValueError("scan subnet must be valid CIDR notation") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError("scan supports IPv4 networks only")
    if network.prefixlen >= 31:
        raise ValueError("scan subnet must include ordinary network and broadcast addresses")
    host_count = _usable_host_count(network)
    if host_count > max_hosts:
        raise ValueError(f"scan subnet must contain at most {max_hosts} usable hosts")
    rfc1918 = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if not (network.is_link_local or any(network.subnet_of(block) for block in rfc1918)):
        raise ValueError("scan subnet must be RFC1918 private or IPv4 link-local")
    connected = [
        ipaddress.ip_network(item["network"], strict=False)
        for item in connected_networks
        if interface is None or item["interface"] == interface
    ]
    if not any(network.subnet_of(candidate) for candidate in connected):
        raise ValueError("scan subnet must be inside a connected local interface network")
    return network


def discover_hicp(
    *,
    timeout: float = 1.5,
    source_ip: str,
    ifindex: int,
    broadcast: str,
) -> tuple[HICPDevice, ...]:
    timeout = _finite_positive("timeout", timeout, maximum=2.0)
    if not isinstance(ifindex, int) or ifindex <= 0:
        raise ValueError("HICP discovery requires a valid interface index")
    source_address = str(_ipv4(source_ip, "source IP"))
    broadcast_address = str(_ipv4(broadcast, "broadcast"))
    devices: dict[str, HICPDevice] = {}
    ip_pktinfo = getattr(socket, "IP_PKTINFO", 8)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, ip_pktinfo, 1)
        sock.bind((source_address, 3250))
        sock.settimeout(min(timeout, 0.2))
        packet_info = struct.pack(
            "I4s4s", ifindex, socket.inet_aton(source_address), b"\x00" * 4
        )
        sock.sendmsg(
            [b"MODULE SCAN\x00"],
            [(socket.IPPROTO_IP, ip_pktinfo, packet_info)],
            0,
            (broadcast_address, 3250),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(devices) < MAX_HICP_DEVICES:
            try:
                payload, ancillary, _, source = sock.recvmsg(
                    2048, socket.CMSG_SPACE(struct.calcsize("I4s4s"))
                )
            except socket.timeout:
                continue
            if source[1] != 3250:
                continue
            ingress_ifindex = None
            for level, kind, data in ancillary:
                if level == socket.IPPROTO_IP and kind == ip_pktinfo:
                    ingress_ifindex = struct.unpack("I4s4s", data[:12])[0]
                    break
            if ingress_ifindex != ifindex or payload.startswith(b"MODULE SCAN"):
                continue
            try:
                device = parse_hicp_response(payload, source_ip=source[0])
            except (UnicodeError, ValueError):
                continue
            device = replace(device, ingress_ifindex=ingress_ifindex)
            previous = devices.get(device.mac)
            if previous is not None and previous != device:
                devices[device.mac] = replace(previous, address_scope="conflict")
            else:
                devices[device.mac] = device
    return tuple(sorted(devices.values(), key=lambda item: (item.ip, item.mac)))


def scan_modbus_network(
    subnet: str,
    *,
    connected_networks: Sequence[dict[str, str]],
    interface: str | None = None,
    hicp_discoverer: Callable[..., tuple[HICPDevice, ...]] = discover_hicp,
    modbus_probe: Callable[..., ModbusProbeResult | ModbusDevice | None] = probe_modbus_device,
    connect_timeout: float = 0.3,
    response_timeout: float = 1.25,
    hicp_timeout: float = 1.5,
    workers: int = DEFAULT_SCAN_WORKERS,
    max_hosts: int = MAX_SCAN_HOSTS,
    deadline_s: float = 30.0,
) -> dict[str, object]:
    connect_timeout = _finite_positive("connect_timeout", connect_timeout, maximum=0.75)
    response_timeout = _finite_positive("response_timeout", response_timeout, maximum=1.5)
    hicp_timeout = _finite_positive("hicp_timeout", hicp_timeout, maximum=2.0)
    deadline_s = _finite_positive("deadline_s", deadline_s, maximum=30.0)
    workers = _bounded_int("workers", workers, maximum=MAX_SCAN_WORKERS)
    network = validate_scan_subnet(
        subnet,
        connected_networks=connected_networks,
        interface=interface,
        max_hosts=max_hosts,
    )
    matching_interfaces = [
        item
        for item in connected_networks
        if (interface is None or item["interface"] == interface)
        and ipaddress.ip_address(item["address"]) in network
    ]
    if len(matching_interfaces) != 1:
        raise ValueError("scan requires exactly one selected connected interface")
    selected = matching_interfaces[0]
    selected_ifindex = int(selected.get("ifindex", 0))
    started = time.monotonic()
    deadline = started + deadline_s
    warnings: list[str] = []
    try:
        hicp_raw = hicp_discoverer(
            timeout=hicp_timeout,
            source_ip=str(selected["address"]),
            ifindex=selected_ifindex,
            broadcast=str(network.broadcast_address),
        )
    except (OSError, ValueError) as exc:
        hicp_raw = ()
        warnings.append(f"HICP discovery unavailable: {exc}")
    hicp_devices: list[HICPDevice] = []
    for device in hicp_raw[:MAX_HICP_DEVICES]:
        source_on_link = ipaddress.ip_address(device.source_ip) in network
        if not source_on_link:
            continue
        advertised_on_link = ipaddress.ip_address(device.ip) in network
        if not advertised_on_link and device.address_scope != "conflict":
            device = replace(device, address_scope="foreign_address")
        hicp_devices.append(device)
    excluded = {
        device.ip
        for device in hicp_devices
        if device.address_scope == "on_link" and ipaddress.ip_address(device.ip) in network
    }
    local_addresses = {
        item["address"]
        for item in connected_networks
        if ipaddress.ip_address(item["address"]) in network
    }
    hosts = [
        str(host)
        for host in network.hosts()
        if str(host) not in excluded and str(host) not in local_addresses
    ]
    found: list[ModbusDevice] = []
    transactions: list[dict[str, object]] = []
    host_iter = iter(hosts)
    pool = ThreadPoolExecutor(max_workers=min(workers, max(len(hosts), 1)))
    pending: dict[object, str] = {}

    def submit_next() -> bool:
        if time.monotonic() >= deadline:
            return False
        try:
            host = next(host_iter)
        except StopIteration:
            return False
        future = pool.submit(
            modbus_probe,
            host,
            connect_timeout=connect_timeout,
            response_timeout=response_timeout,
        )
        pending[future] = host
        return True

    for _ in range(min(workers, len(hosts))):
        submit_next()
    truncated = False
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            truncated = True
            break
        done, _ = wait(tuple(pending), timeout=remaining, return_when=FIRST_COMPLETED)
        if not done:
            truncated = True
            break
        for future in done:
            host = pending.pop(future)
            try:
                result = future.result()
            except (ConnectionError, OSError, ValueError) as exc:
                transactions.append({
                    "target_ip": host,
                    "port": 502,
                    "outcome": "probe_error",
                    "error": str(exc),
                })
            else:
                if isinstance(result, ModbusProbeResult):
                    transactions.append(result.transaction)
                    if result.device is not None:
                        found.append(result.device)
                elif isinstance(result, ModbusDevice):
                    found.append(result)
                    transactions.append({
                        "target_ip": host,
                        "port": 502,
                        "outcome": "injected_probe_result",
                    })
                else:
                    transactions.append({
                        "target_ip": host,
                        "port": 502,
                        "outcome": "no_response",
                    })
            submit_next()
    if pending:
        for future, host in list(pending.items()):
            if future.cancel():
                transactions.append({
                    "target_ip": host,
                    "port": 502,
                    "outcome": "deadline_cancelled",
                })
    pool.shutdown(wait=True, cancel_futures=True)
    devices = [item.as_dict() for item in found]
    devices.extend(item.as_dict() for item in hicp_devices)
    devices.sort(key=lambda item: ipaddress.ip_address(str(item["ip"])))
    confirmed_states = {
        "identity_confirmed",
        "modbus_confirmed_identity_partial",
        "modbus_confirmed_exception",
    }
    outcome_counts: dict[str, int] = {}
    for transaction in transactions:
        outcome = str(transaction.get("outcome", "unknown"))
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "scan_type": "read_only_modbus_discovery",
        "scope": {
            "interface": selected["interface"],
            "ifindex": selected_ifindex,
            "source_ipv4": selected["address"],
            "subnet": str(network),
            "host_count": _usable_host_count(network),
            "probed_hosts": len(transactions),
            "workers": min(workers, max(len(hosts), 1)),
            "connect_timeout_ms": connect_timeout * 1000.0,
            "response_timeout_ms": response_timeout * 1000.0,
            "hicp_timeout_ms": hicp_timeout * 1000.0,
            "deadline_ms": deadline_s * 1000.0,
        },
        "devices": devices,
        "transactions": transactions,
        "outcome_counts": outcome_counts,
        "truncated": truncated,
        "device_count": len(devices),
        "confirmed_modbus_count": sum(
            item.get("kind") == "modbus_tcp" and item.get("state") in confirmed_states
            for item in devices
        ),
        "anybus_count": sum(item.get("kind") == "anybus_hicp" for item in devices),
        "writes_performed": 0,
        "requests_used": ["HMS HICP MODULE SCAN", "Modbus 43/14 Read Device Identification"],
        "warnings": warnings,
        "duration_ms": (time.monotonic() - started) * 1000.0,
    }
