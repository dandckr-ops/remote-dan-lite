from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class SerialFraming:
    baud: int
    data_bits: int = 8
    parity: str = "N"
    stop_bits: int = 1

    def __post_init__(self) -> None:
        if self.baud <= 0:
            raise ValueError("baud must be positive")
        if self.data_bits not in (5, 6, 7, 8):
            raise ValueError("data bits must be 5, 6, 7, or 8")
        if self.parity not in ("N", "E", "O"):
            raise ValueError("parity must be N, E, or O")
        if self.stop_bits not in (1, 2):
            raise ValueError("stop bits must be 1 or 2")

    @property
    def label(self) -> str:
        return f"{self.baud} {self.data_bits}{self.parity}{self.stop_bits}"

    def as_dict(self, source: str = "configured") -> dict[str, Any]:
        return {
            "baud": self.baud,
            "data_bits": self.data_bits,
            "parity": self.parity,
            "stop_bits": self.stop_bits,
            "label": self.label,
            "source": source,
        }


def _modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _valid_modbus_frame(frame: bytes) -> bool:
    if not 4 <= len(frame) <= 256:
        return False
    if _modbus_crc(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        return False
    address, function = frame[0], frame[1]
    if address > 247:
        return False
    base_function = function & 0x7F
    if base_function not in {1, 2, 3, 4, 5, 6, 15, 16}:
        return False
    if function & 0x80:
        return len(frame) == 5 and 1 <= frame[2] <= 11
    if function in {5, 6}:
        return len(frame) == 8
    if function in {1, 2}:
        return len(frame) == 8 or (len(frame) == frame[2] + 5 and frame[2] > 0)
    if function in {3, 4}:
        return len(frame) == 8 or (
            len(frame) == frame[2] + 5 and frame[2] > 0 and frame[2] % 2 == 0
        )
    if function == 15:
        return len(frame) == 8 or (
            len(frame) >= 10 and frame[6] == (int.from_bytes(frame[4:6], "big") + 7) // 8
            and len(frame) == frame[6] + 9
        )
    if function == 16:
        return len(frame) == 8 or (
            len(frame) >= 10 and frame[6] == 2 * int.from_bytes(frame[4:6], "big")
            and len(frame) == frame[6] + 9
        )
    return False


def _valid_iec101_frame(frame: bytes) -> bool:
    if len(frame) in (5, 6) and frame[0] == 0x10 and frame[-1] == 0x16:
        return sum(frame[1:-2]) & 0xFF == frame[-2]
    if len(frame) >= 7 and frame[:1] == b"\x68" and frame[3:4] == b"\x68":
        length = frame[1]
        if frame[2] != length or len(frame) != length + 6 or frame[-1] != 0x16:
            return False
        return sum(frame[4:4 + length]) & 0xFF == frame[-2]
    return False


def _crc16_reflected(data: bytes, *, initial: int, polynomial: int, xor_out: int) -> int:
    crc = initial
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ polynomial if crc & 1 else crc >> 1
    return crc ^ xor_out


def _valid_dnp3_frame(frame: bytes) -> bool:
    if len(frame) < 10 or frame[:2] != b"\x05\x64":
        return False
    length = frame[2]
    if length < 5:
        return False
    user_length = length - 5
    block_count = (user_length + 15) // 16
    if len(frame) != 10 + user_length + 2 * block_count:
        return False
    expected_header_crc = _crc16_reflected(
        frame[:8], initial=0, polynomial=0xA6BC, xor_out=0xFFFF
    )
    if int.from_bytes(frame[8:10], "little") != expected_header_crc:
        return False
    control = frame[3]
    prm = bool(control & 0x40)
    function = control & 0x0F
    legal_functions = {0, 1, 2, 3, 4, 9} if prm else {0, 1, 11, 14, 15}
    if function not in legal_functions or int.from_bytes(frame[6:8], "little") == 0xFFFF:
        return False
    offset = 10
    remaining = user_length
    while remaining:
        block_length = min(16, remaining)
        block = frame[offset:offset + block_length]
        received_crc = int.from_bytes(
            frame[offset + block_length:offset + block_length + 2], "little"
        )
        expected_crc = _crc16_reflected(
            block, initial=0, polynomial=0xA6BC, xor_out=0xFFFF
        )
        if received_crc != expected_crc:
            return False
        offset += block_length + 2
        remaining -= block_length
    return True


_SEL_FAST_FUNCTIONS = {
    0x00, 0x01, 0x02, 0x05, 0x10, 0x16, 0x18, 0x30, 0x31, 0x33,
    0x80, 0x81, 0x82, 0x85, 0x90, 0x96, 0x98, 0xB0, 0xB1, 0xB3,
}


def _valid_sel_fast_message(frame: bytes) -> bool:
    if len(frame) < 13 or frame[:2] != b"\xA5\x46" or frame[2] != len(frame):
        return False
    if frame[9] not in _SEL_FAST_FUNCTIONS:
        return False
    expected_crc = _crc16_reflected(
        frame[:-2], initial=0xFFFF, polynomial=0xA001, xor_out=0
    )
    return int.from_bytes(frame[-2:], "big") == expected_crc


def _printable_percent(data: bytes) -> float:
    if not data:
        return 0.0
    printable = sum(value in (9, 10, 13) or 32 <= value <= 126 for value in data)
    return printable * 100.0 / len(data)


def analyze_serial_capture(
    *,
    data: bytes,
    frames: list[bytes],
    framing: SerialFraming,
    duration_s: float,
    receiver_errors: dict[str, int] | None = None,
    frame_boundaries_reliable: bool = True,
) -> dict[str, Any]:
    errors = {
        name: int((receiver_errors or {}).get(name, 0))
        for name in (
            "parity",
            "framing",
            "break",
            "overrun",
            "buffer_overrun",
            "marked_parity_or_framing",
            "truncated_marker",
        )
    }
    valid_modbus_frames = {frame for frame in frames if _valid_modbus_frame(frame)}
    valid_iec101_frames = {frame for frame in frames if _valid_iec101_frame(frame)}
    valid_dnp3_frames = {frame for frame in frames if _valid_dnp3_frame(frame)}
    valid_sel_fast_frames = {frame for frame in frames if _valid_sel_fast_message(frame)}
    valid_modbus = len(valid_modbus_frames)
    valid_iec101 = len(valid_iec101_frames)
    valid_dnp3 = len(valid_dnp3_frames)
    valid_sel_fast = len(valid_sel_fast_frames)
    printable_percent = _printable_percent(data)
    text = data.decode("ascii", errors="replace")
    sel_identity = re.search(r"\bSEL-[A-Z0-9]", text, flags=re.IGNORECASE) is not None
    terminal_structure = re.search(
        r"(?:^|\r?\n)(?:=|=>|=>>|==>>)\s*$",
        text,
    ) is not None
    structured_fields = set(re.findall(
        r"(?m)^(FID|BFID|CID|DEVID|DEVCODE|PARTNO|CONFIG)=",
        text,
    ))
    if not data:
        protocol = {
            "name": "No serial activity",
            "confidence": "none",
            "valid_frame_count": 0,
            "evidence": [],
        }
    elif valid_sel_fast:
        protocol = {
            "name": (
                "SEL Fast Message"
                if valid_sel_fast >= 2
                else "SEL Fast Message candidate"
            ),
            "confidence": "high" if valid_sel_fast >= 2 else "medium",
            "valid_frame_count": valid_sel_fast,
            "evidence": [
                f"{valid_sel_fast} nonidentical A5 46 message(s) passed length, function, and CRC-16 validation"
            ],
        }
    elif valid_dnp3:
        protocol = {
            "name": "DNP3 serial" if valid_dnp3 >= 2 else "DNP3 serial candidate",
            "confidence": "high" if valid_dnp3 >= 2 else "medium",
            "valid_frame_count": valid_dnp3,
            "evidence": [
                f"{valid_dnp3} nonidentical frame(s) passed DNP3 link length, control, header CRC, and block CRC validation"
            ],
        }
    elif valid_modbus:
        high_confidence_modbus = valid_modbus >= 2 and frame_boundaries_reliable
        protocol = {
            "name": "Modbus RTU" if high_confidence_modbus else "Modbus RTU candidate",
            "confidence": "high" if high_confidence_modbus else "medium",
            "valid_frame_count": valid_modbus,
            "evidence": [
                f"{valid_modbus} nonidentical candidate frame(s) passed Modbus function, length, and CRC-16 validation"
            ],
        }
    elif valid_iec101:
        protocol = {
            "name": (
                "IEC 60870-5-101"
                if valid_iec101 >= 2
                else "IEC 60870-5-101 candidate"
            ),
            "confidence": "high" if valid_iec101 >= 2 else "medium",
            "valid_frame_count": valid_iec101,
            "evidence": [f"{valid_iec101} frame(s) passed IEC-101 length and checksum validation"],
        }
    elif sel_identity and terminal_structure and len(structured_fields) >= 2 and printable_percent >= 85.0:
        protocol = {
            "name": "SEL ASCII / terminal",
            "confidence": "high",
            "valid_frame_count": 0,
            "evidence": [
                "SEL terminal prompt and multiple structured identity fields observed in printable line-oriented traffic"
            ],
        }
    elif sel_identity and terminal_structure and printable_percent >= 85.0:
        protocol = {
            "name": "SEL ASCII candidate",
            "confidence": "medium",
            "valid_frame_count": 0,
            "evidence": ["SEL product text and terminal prompt observed without a structured identity block"],
        }
    elif printable_percent >= 85.0 and ("\r" in text or "\n" in text):
        protocol = {
            "name": "ASCII terminal / text",
            "confidence": "medium",
            "valid_frame_count": 0,
            "evidence": ["Predominantly printable line-oriented traffic observed"],
        }
    else:
        protocol = {
            "name": "Higher layer unresolved",
            "confidence": "low",
            "valid_frame_count": 0,
            "evidence": [],
        }
    warnings: list[str] = []
    if valid_modbus and not frame_boundaries_reliable:
        warnings.append(
            "USB read chunks are not wire-level frame boundaries; Modbus RTU remains a candidate without independent silent-interval evidence."
        )
    if not data:
        warnings.append(
            "No bytes were received; baud, parity, and protocol cannot be inferred from silence."
        )
    return {
        "status": "analyzed" if data else "no_activity",
        "byte_count": len(data),
        "bytes_per_second": len(data) / duration_s if duration_s > 0 else 0.0,
        "duration_ms": max(duration_s, 0.0) * 1000.0,
        "printable_percent": printable_percent,
        "text_preview": text[:2000],
        "hex_preview": data[:512].hex(" "),
        "framing": framing.as_dict(),
        "receiver_errors": errors,
        "protocol": protocol,
        "frame_boundaries_reliable": frame_boundaries_reliable,
        "warnings": warnings,
    }
