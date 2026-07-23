from __future__ import annotations

from remote_dan.serial_analysis import SerialFraming, analyze_serial_capture


def _modbus_crc(payload: bytes) -> bytes:
    crc = 0xFFFF
    for value in payload:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def _dnp_crc(payload: bytes) -> bytes:
    crc = 0
    for value in payload:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return (crc ^ 0xFFFF).to_bytes(2, "little")


def _dnp3_frame(payload: bytes, destination: int) -> bytes:
    header = bytes.fromhex("0564") + bytes([5 + len(payload), 0xC4])
    header += destination.to_bytes(2, "little") + bytes.fromhex("0100")
    wire = header + _dnp_crc(header)
    for offset in range(0, len(payload), 16):
        block = payload[offset:offset + 16]
        wire += block + _dnp_crc(block)
    return wire


def _sel_fast_crc(payload: bytes) -> bytes:
    crc = 0xFFFF
    for value in payload:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "big")


def _sel_fast_frame(function: int, payload: bytes) -> bytes:
    body = bytes.fromhex("A54600") + bytes(5) + bytes([0, function, 0xC0]) + payload
    body = body[:2] + bytes([len(body) + 2]) + body[3:]
    return body + _sel_fast_crc(body)


def test_crc_valid_modbus_rtu_is_identified_but_corrupt_frame_is_not() -> None:
    payload = bytes.fromhex("010304000A0014")
    second_payload = bytes.fromhex("010302002A")
    frame = payload + _modbus_crc(payload)
    second_frame = second_payload + _modbus_crc(second_payload)
    framing = SerialFraming(baud=9600, data_bits=8, parity="E", stop_bits=1)

    valid = analyze_serial_capture(
        data=frame + second_frame,
        frames=[frame, second_frame],
        framing=framing,
        duration_s=1.0,
    )
    corrupt = analyze_serial_capture(
        data=frame[:-1] + bytes([frame[-1] ^ 0x01]),
        frames=[frame[:-1] + bytes([frame[-1] ^ 0x01])],
        framing=framing,
        duration_s=1.0,
    )

    assert valid["status"] == "analyzed"
    assert valid["protocol"]["name"] == "Modbus RTU"
    assert valid["protocol"]["confidence"] == "high"
    assert valid["protocol"]["valid_frame_count"] == 2
    assert valid["framing"] == {
        "baud": 9600,
        "data_bits": 8,
        "parity": "E",
        "stop_bits": 1,
        "label": "9600 8E1",
        "source": "configured",
    }
    assert corrupt["protocol"]["name"] != "Modbus RTU"
    assert corrupt["protocol"]["valid_frame_count"] == 0


def test_sel_ascii_requires_sel_identity_and_terminal_structure() -> None:
    framing = SerialFraming(baud=9600, data_bits=8, parity="N", stop_bits=1)
    sel_data = b"\r\nFID=SEL-751A-R100-V0\r\nDEVID=FEEDER_1\r\nPARTNO=751A01\r\n=>"
    generic_data = b"temperature=72\r\nstatus=ok\r\n>"

    sel = analyze_serial_capture(
        data=sel_data,
        frames=[sel_data],
        framing=framing,
        duration_s=2.0,
    )
    generic = analyze_serial_capture(
        data=generic_data,
        frames=[generic_data],
        framing=framing,
        duration_s=2.0,
    )

    assert sel["protocol"]["name"] == "SEL ASCII / terminal"
    assert sel["protocol"]["confidence"] == "high"
    assert "SEL-751A" in sel["text_preview"]
    assert sel["printable_percent"] > 90
    assert generic["protocol"]["name"] == "ASCII terminal / text"
    assert generic["protocol"]["confidence"] == "medium"


def test_sel_banner_and_prompt_remain_a_candidate_without_structured_identity() -> None:
    data = b"\r\nSEL-751A Feeder Protection Relay\r\n=>"
    result = analyze_serial_capture(
        data=data,
        frames=[data],
        framing=SerialFraming(baud=9600),
        duration_s=1.0,
    )

    assert result["protocol"]["name"] == "SEL ASCII candidate"
    assert result["protocol"]["confidence"] == "medium"


def test_silence_and_receiver_errors_fail_closed() -> None:
    result = analyze_serial_capture(
        data=b"",
        frames=[],
        framing=SerialFraming(baud=19200, data_bits=8, parity="O", stop_bits=1),
        duration_s=5.0,
        receiver_errors={"parity": 2, "framing": 1, "break": 0, "overrun": 0},
    )

    assert result["status"] == "no_activity"
    assert result["protocol"] == {
        "name": "No serial activity",
        "confidence": "none",
        "valid_frame_count": 0,
        "evidence": [],
    }
    assert result["receiver_errors"]["parity"] == 2
    assert result["warnings"] == [
        "No bytes were received; baud, parity, and protocol cannot be inferred from silence."
    ]


def test_iec_60870_5_101_requires_a_valid_frame_checksum() -> None:
    body = bytes([0x49, 0x01])
    second_body = bytes([0x5B, 0x02])
    valid_frame = bytes([0x10]) + body + bytes([sum(body) & 0xFF, 0x16])
    second_frame = bytes([0x10]) + second_body + bytes([sum(second_body) & 0xFF, 0x16])
    corrupt_frame = valid_frame[:-2] + bytes([valid_frame[-2] ^ 1, 0x16])
    framing = SerialFraming(baud=19200, data_bits=8, parity="E", stop_bits=1)

    valid = analyze_serial_capture(
        data=valid_frame + second_frame,
        frames=[valid_frame, second_frame],
        framing=framing,
        duration_s=1.0,
    )
    corrupt = analyze_serial_capture(
        data=corrupt_frame,
        frames=[corrupt_frame],
        framing=framing,
        duration_s=1.0,
    )

    assert valid["protocol"]["name"] == "IEC 60870-5-101"
    assert valid["protocol"]["confidence"] == "high"
    assert valid["protocol"]["valid_frame_count"] == 2
    assert corrupt["protocol"]["name"] != "IEC 60870-5-101"


def test_one_checksum_valid_frame_is_only_a_protocol_candidate() -> None:
    modbus_payload = bytes.fromhex("010302002A")
    modbus_frame = modbus_payload + _modbus_crc(modbus_payload)
    iec_body = bytes([0x49, 0x01])
    iec_frame = bytes([0x10]) + iec_body + bytes([sum(iec_body) & 0xFF, 0x16])

    modbus = analyze_serial_capture(
        data=modbus_frame,
        frames=[modbus_frame],
        framing=SerialFraming(baud=9600, parity="E"),
        duration_s=1.0,
    )
    iec = analyze_serial_capture(
        data=iec_frame,
        frames=[iec_frame],
        framing=SerialFraming(baud=19200, parity="E"),
        duration_s=1.0,
    )

    assert modbus["protocol"]["name"] == "Modbus RTU candidate"
    assert modbus["protocol"]["confidence"] == "medium"
    assert iec["protocol"]["name"] == "IEC 60870-5-101 candidate"
    assert iec["protocol"]["confidence"] == "medium"


def test_two_complete_dnp3_frames_require_header_and_block_crcs() -> None:
    first = _dnp3_frame(bytes.fromhex("C0013C0206"), destination=1)
    second = _dnp3_frame(bytes.fromhex("C1013C0306"), destination=2)
    result = analyze_serial_capture(
        data=first + second,
        frames=[first, second],
        framing=SerialFraming(baud=9600, parity="N"),
        duration_s=1.0,
    )
    corrupt = analyze_serial_capture(
        data=first[:-1] + bytes([first[-1] ^ 1]),
        frames=[first[:-1] + bytes([first[-1] ^ 1])],
        framing=SerialFraming(baud=9600),
        duration_s=1.0,
    )

    assert result["protocol"]["name"] == "DNP3 serial"
    assert result["protocol"]["confidence"] == "high"
    assert result["protocol"]["valid_frame_count"] == 2
    assert corrupt["protocol"]["name"] != "DNP3 serial"


def test_sel_fast_message_requires_a546_length_function_and_crc() -> None:
    first = _sel_fast_frame(0x80, bytes.fromhex("01020304"))
    second = _sel_fast_frame(0x81, bytes.fromhex("05060708"))
    result = analyze_serial_capture(
        data=first + second,
        frames=[first, second],
        framing=SerialFraming(baud=38400),
        duration_s=1.0,
    )
    lone = analyze_serial_capture(
        data=first,
        frames=[first],
        framing=SerialFraming(baud=38400),
        duration_s=1.0,
    )

    assert result["protocol"]["name"] == "SEL Fast Message"
    assert result["protocol"]["confidence"] == "high"
    assert result["protocol"]["valid_frame_count"] == 2
    assert lone["protocol"]["name"] == "SEL Fast Message candidate"
    assert lone["protocol"]["confidence"] == "medium"


def test_modbus_is_capped_at_candidate_without_reliable_frame_boundaries() -> None:
    first_payload = bytes.fromhex("010304000A0014")
    second_payload = bytes.fromhex("010302002A")
    first = first_payload + _modbus_crc(first_payload)
    second = second_payload + _modbus_crc(second_payload)

    result = analyze_serial_capture(
        data=first + second,
        frames=[first, second],
        framing=SerialFraming(baud=9600, parity="E"),
        duration_s=1.0,
        frame_boundaries_reliable=False,
    )

    assert result["protocol"]["name"] == "Modbus RTU candidate"
    assert result["protocol"]["confidence"] == "medium"
    assert "USB read chunks" in result["warnings"][0]
