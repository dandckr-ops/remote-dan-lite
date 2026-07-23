from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from remote_dan.can_decode import (
    CanDecodeManager,
    CanDecodeRequest,
    CanDecodeSourceNotFound,
    eligible_bus_survey_classification,
    read_authoritative_artifact,
)
from remote_dan.database import EvidenceDatabase


def _bits(value: int, width: int) -> list[int]:
    return [int(bit) for bit in f"{value:0{width}b}"]


def _crc15(bits: list[int]) -> list[int]:
    crc = 0
    for bit in bits:
        feedback = bit ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if feedback:
            crc ^= 0x4599
    return _bits(crc, 15)


def _stuff(bits: list[int]) -> list[int]:
    wire: list[int] = []
    previous: int | None = None
    run = 0
    for bit in bits:
        if previous is not None and run == 5:
            stuff = 1 - previous
            wire.append(stuff)
            previous = stuff
            run = 1
        wire.append(bit)
        if bit == previous:
            run += 1
        else:
            previous = bit
            run = 1
    return wire


def _frame(identifier: int, payload: bytes) -> list[int]:
    body = [0] + _bits(identifier, 11) + [0, 0, 0] + _bits(len(payload), 4)
    body += [bit for value in payload for bit in _bits(value, 8)]
    return _stuff(body + _crc15(body)) + [1, 0, 1] + [1] * 7 + [1] * 3


def _source_capture(
    root: Path,
    database: EvidenceDatabase,
    *,
    run_id: str = "source-can-001",
    capture_type: str = "can",
    sample_interval_us: float = 0.1,
    session_id: int | None = None,
) -> tuple[Path, int]:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    bits = [1] * 20
    for frame in (
        _frame(0x100, b"\x10\x20"),
        _frame(0x200, b"\xAA\xBB"),
        _frame(0x100, b"\x10\x21"),
    ):
        bits.extend(frame)
        bits.extend([1] * 20)
    samples_per_bit = round(2.0 / sample_interval_us)
    logical = np.repeat(np.asarray(bits, dtype=np.int8), samples_per_bit)
    dominant = (logical == 0).astype(np.float64)
    time_us = np.arange(logical.size, dtype=np.float64) * sample_interval_us
    stack = np.column_stack((time_us, 13.8 + np.zeros_like(time_us), 2.5 + dominant, 2.5 - dominant))
    np.savetxt(
        run_dir / "capture.csv",
        stack,
        delimiter=",",
        header="time_us,vbat_v,can_h_v,can_l_v",
        comments="",
        fmt="%.8g",
    )
    capture_id = database.create_capture(
        session_id=session_id,
        run_id=run_id,
        captured_at="2026-07-23T12:00:00+00:00",
        capture_type=capture_type,
        label="Synthetic CAN source",
        backend="synthetic-test",
        preset="can-analysis",
        samples=int(time_us.size),
        sample_interval_us=sample_interval_us,
        duration_ms=float(time_us[-1] / 1000.0),
        status="complete",
        metadata={"profile": "network"},
    )
    manifest = {
        "run_id": run_id,
        "capture_id": capture_id,
        "captured_at": "2026-07-23T12:00:00+00:00",
        "capture_type": capture_type,
        "profile": "network",
        "label": "Synthetic CAN source",
        "sample_interval_us": sample_interval_us,
        "artifacts": ["capture.csv", "manifest.json"],
        "sha256": {"capture.csv": _sha256(run_dir / "capture.csv")},
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for filename, kind, media_type in (
        ("capture.csv", "waveform", "text/csv"),
        ("manifest.json", "manifest", "application/json"),
    ):
        path = run_dir / filename
        database.add_artifact(
            capture_id=capture_id,
            kind=kind,
            filename=filename,
            relative_path=f"{run_id}/{filename}",
            media_type=media_type,
            size_bytes=path.stat().st_size,
            sha256=_sha256(path),
        )
    return run_dir, capture_id


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_artifact_record(
    database: EvidenceDatabase,
    capture_id: int,
    run_dir: Path,
    filename: str,
) -> None:
    path = run_dir / filename
    with database._connect() as connection:
        connection.execute(
            """
            UPDATE artifacts
            SET relative_path = ?, size_bytes = ?, sha256 = ?
            WHERE capture_id = ? AND filename = ?
            """,
            (
                f"{run_dir.name}/{filename}",
                path.stat().st_size,
                _sha256(path),
                capture_id,
                "capture.csv" if filename == "fast.csv" else filename,
            ),
        )
        if filename == "fast.csv":
            connection.execute(
                "UPDATE artifacts SET filename = 'fast.csv' WHERE capture_id = ? AND filename = 'capture.csv'",
                (capture_id,),
            )


def test_child_decode_preserves_source_and_registers_hashed_lineage(tmp_path: Path) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    session_id = database.create_session(purpose="Synthetic passive CAN evidence")
    source_dir, source_capture_id = _source_capture(
        root, database, session_id=session_id
    )
    source_manifest_before = (source_dir / "manifest.json").read_bytes()
    source_waveform_before = (source_dir / "capture.csv").read_bytes()
    source_record_before = database.get_capture(source_capture_id)
    source_sha = _sha256(source_dir / "capture.csv")
    manager = CanDecodeManager(root, database=database)

    manifest = manager.run(CanDecodeRequest(
        source_run_id="source-can-001",
        label="Synthetic decode proof",
    ))

    assert (source_dir / "manifest.json").read_bytes() == source_manifest_before
    assert (source_dir / "capture.csv").read_bytes() == source_waveform_before
    assert database.get_capture(source_capture_id) == source_record_before
    assert manifest["capture_type"] == "can_decode"
    assert manifest["source_run_id"] == "source-can-001"
    assert manifest["source_capture_id"] == source_capture_id
    assert manifest["source_artifact"] == "capture.csv"
    assert manifest["source_sha256"] == source_sha
    assert manifest["writes_performed"] == 0
    assert manifest["frame_count"] == 3
    assert manifest["backend"] == "can-decoder-v1"
    assert manifest["profile"] == "can-decode"
    assert manifest["preset"] == "500000bps"
    assert manifest["samples"] == 3
    assert manifest["sample_interval_us"] == pytest.approx(0.1)
    assert manifest["duration_ms"] > 0
    assert manifest["identifier_count"] == 2
    assert manifest["rejected_candidate_count"] == 0
    assert manifest["can_polarity"] == "expected"
    child_dir = root / str(manifest["run_id"])
    assert set(manifest["artifacts"]) == {
        "frames.jsonl", "identifiers.csv", "summary.json", "manifest.json",
    }
    for filename, digest in manifest["sha256"].items():
        assert _sha256(child_dir / filename) == digest
    frames = [json.loads(line) for line in (child_dir / "frames.jsonl").read_text().splitlines()]
    assert [frame["timestamp_us"] for frame in frames] == sorted(
        frame["timestamp_us"] for frame in frames
    )
    saved = database.get_capture(int(manifest["capture_id"]))
    assert saved is not None
    assert saved["capture_type"] == "can_decode"
    assert saved["session_id"] == session_id
    assert saved["status"] == "complete"
    assert saved["metadata"]["source_run_id"] == "source-can-001"
    assert saved["metadata"]["source_sha256"] == source_sha
    assert saved["metadata"]["capture_id"] == manifest["capture_id"]
    assert {item["filename"] for item in saved["artifacts"]} == set(manifest["artifacts"])
    assert all(len(item["sha256"]) == 64 for item in saved["artifacts"])


def test_bus_survey_can_candidate_uses_fast_waveform(tmp_path: Path) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    source_dir, capture_id = _source_capture(root, database, run_id="survey-can-001")
    (source_dir / "capture.csv").rename(source_dir / "fast.csv")
    manifest = json.loads((source_dir / "manifest.json").read_text())
    manifest["capture_type"] = "bus_survey"
    manifest["profile"] = "bus-sniffer"
    manifest["summary"] = {
        "classification": {"family": "CAN-family", "confidence": "low", "workspace": "can"}
    }
    manifest["sha256"] = {"fast.csv": _sha256(source_dir / "fast.csv")}
    (source_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with database._connect() as connection:
        connection.execute(
            "UPDATE captures SET capture_type = 'bus_survey', metadata_json = ? WHERE id = ?",
            (json.dumps({"profile": "bus-sniffer"}), capture_id),
        )
    _refresh_artifact_record(database, capture_id, source_dir, "fast.csv")
    _refresh_artifact_record(database, capture_id, source_dir, "manifest.json")

    decoded = CanDecodeManager(root, database=database).run(
        CanDecodeRequest(source_run_id="survey-can-001", label="Survey decode")
    )

    assert decoded["source_artifact"] == "fast.csv"
    assert decoded["frame_count"] == 3


def test_bus_survey_eligibility_includes_differential_ambiguity_but_not_uart() -> None:
    assert eligible_bus_survey_classification({
        "status": "ambiguous",
        "topology": "Differential pair",
        "family": "Differential digital or bus candidate — unresolved",
        "workspace": "scope",
    })
    assert eligible_bus_survey_classification({
        "status": "ambiguous",
        "electrical_topology": "Differential pair",
        "family": "Differential digital or bus candidate — unresolved",
        "workspace": "scope",
    })
    assert not eligible_bus_survey_classification({
        "status": "classified",
        "topology": "Differential pair",
        "family": "RS-485/422-like balanced UART",
        "workspace": "bus-sniffer",
    })


def test_source_validation_fails_closed_for_unsafe_or_ineligible_inputs(tmp_path: Path) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    manager = CanDecodeManager(root, database=database)

    with pytest.raises(ValueError, match="run ID"):
        manager.run(CanDecodeRequest(source_run_id="../escape", label="bad"))
    with pytest.raises(CanDecodeSourceNotFound, match="not found"):
        manager.run(CanDecodeRequest(source_run_id="missing-run", label="missing"))

    _source_capture(root, database, run_id="serial-source", capture_type="serial")
    with pytest.raises(ValueError, match="not an eligible"):
        manager.run(CanDecodeRequest(source_run_id="serial-source", label="bad type"))

    missing_dir, _ = _source_capture(root, database, run_id="missing-artifact")
    (missing_dir / "capture.csv").unlink()
    with pytest.raises(ValueError, match="is missing"):
        manager.run(CanDecodeRequest(source_run_id="missing-artifact", label="missing file"))

    _source_capture(root, database, run_id="undersampled", sample_interval_us=2.0)
    with pytest.raises(ValueError, match="sampling resolution"):
        manager.run(CanDecodeRequest(source_run_id="undersampled", label="too slow"))

    malformed_dir, _ = _source_capture(root, database, run_id="malformed")
    (malformed_dir / "capture.csv").write_text("time_us,can_h_v,can_l_v\nnot,numeric,data\n")
    malformed_manifest = json.loads((malformed_dir / "manifest.json").read_text())
    malformed_manifest["sha256"]["capture.csv"] = _sha256(malformed_dir / "capture.csv")
    (malformed_dir / "manifest.json").write_text(json.dumps(malformed_manifest))
    malformed_record = database.get_capture_by_run_id("malformed")
    assert malformed_record is not None
    _refresh_artifact_record(database, int(malformed_record["id"]), malformed_dir, "capture.csv")
    _refresh_artifact_record(database, int(malformed_record["id"]), malformed_dir, "manifest.json")
    with pytest.raises(ValueError, match="malformed"):
        manager.run(CanDecodeRequest(source_run_id="malformed", label="malformed"))

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}")
    (root / "linked-source").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        manager.run(CanDecodeRequest(source_run_id="linked-source", label="escape"))

    assert not list(root.glob(".*.partial-*"))


def test_waveform_loader_accepts_exact_historical_schemas_and_rejects_bad_headers(
    tmp_path: Path,
) -> None:
    rows = "0,13.8,3.5,1.5\n0.1,13.8,2.5,2.5\n0.2,13.8,3.5,1.5\n"
    schemas = (
        "time_us,vbat_v,can_h_v,can_l_v",
        "time_us,VBAT,CAN-H,CAN-L",
    )
    for index, header in enumerate(schemas):
        path = tmp_path / f"schema-{index}.csv"
        path.write_text(header + "\n" + rows, encoding="utf-8")
        time_us, can_h, can_l = CanDecodeManager._load_waveform(path)
        assert time_us.tolist() == [0.0, 0.1, 0.2]
        assert can_h.tolist() == [3.5, 2.5, 3.5]
        assert can_l.tolist() == [1.5, 2.5, 1.5]

    protected = tmp_path / "protected.csv"
    protected.write_text("time_us,B,C\n0,3.5,1.5\n0.1,2.5,2.5\n0.2,3.5,1.5\n")
    assert CanDecodeManager._load_waveform(protected)[1].tolist() == [3.5, 2.5, 3.5]

    for header in (
        "time_us,can_h_v,can_h_v,can_l_v",
        "time_us,can_h_v,can_l_v,mystery",
        "can_h_v,time_us,can_l_v",
    ):
        bad = tmp_path / "bad.csv"
        bad.write_text(header + "\n0,3.5,1.5,0\n0.1,2.5,2.5,0\n0.2,3.5,1.5,0\n")
        with pytest.raises(ValueError, match="header|schema|column"):
            CanDecodeManager._load_waveform(bad)


def test_source_hash_and_sqlite_run_id_are_authoritative(tmp_path: Path) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    source_dir, source_capture_id = _source_capture(root, database)
    manifest_path = source_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["capture_id"] = source_capture_id + 999
    manifest_path.write_text(json.dumps(manifest))
    _refresh_artifact_record(
        database, source_capture_id, source_dir, "manifest.json"
    )

    child = CanDecodeManager(root, database=database).run(
        CanDecodeRequest(source_run_id="source-can-001", label="authoritative lineage")
    )
    assert child["source_capture_id"] == source_capture_id

    source_dir2, _ = _source_capture(root, database, run_id="tampered-source")
    with (source_dir2 / "capture.csv").open("a", encoding="utf-8") as handle:
        handle.write("999,13.8,2.5,2.5\n")
    with pytest.raises(ValueError, match="size|hash"):
        CanDecodeManager(root, database=database).run(
            CanDecodeRequest(source_run_id="tampered-source", label="reject tamper")
        )


def test_source_manifest_must_be_a_json_object(tmp_path: Path) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    source_dir, source_capture_id = _source_capture(root, database)
    (source_dir / "manifest.json").write_text("[]\n", encoding="utf-8")
    _refresh_artifact_record(
        database, source_capture_id, source_dir, "manifest.json"
    )

    with pytest.raises(ValueError, match="manifest.*malformed"):
        CanDecodeManager(root, database=database).run(
            CanDecodeRequest(source_run_id="source-can-001", label="reject non-object")
        )


@pytest.mark.parametrize("replacement", ("symlink", "directory"))
def test_source_artifact_open_rejects_symlink_and_non_regular_replacements(
    tmp_path: Path,
    replacement: str,
) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    source_dir, _ = _source_capture(root, database)
    waveform = source_dir / "capture.csv"
    original = tmp_path / "original.csv"
    waveform.rename(original)
    if replacement == "symlink":
        waveform.symlink_to(original)
        message = "symlink"
    else:
        waveform.mkdir()
        message = "regular file"

    with pytest.raises(ValueError, match=message):
        CanDecodeManager(root, database=database).run(
            CanDecodeRequest(source_run_id="source-can-001", label="reject replacement")
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("orphan", "SQLite"),
        ("pending", "complete"),
        ("type", "type"),
        ("profile", "profile"),
        ("missing_artifact", "artifact"),
        ("path", "path"),
        ("size", "size"),
        ("hash", "hash"),
    ],
)
def test_decode_requires_authoritative_complete_sqlite_parent(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    source_dir, capture_id = _source_capture(root, database)
    if mutation == "orphan":
        database.delete_capture(capture_id)
    elif mutation == "pending":
        database.set_capture_status(capture_id, "pending")
    elif mutation == "type":
        with database._connect() as connection:
            connection.execute("UPDATE captures SET capture_type = 'serial' WHERE id = ?", (capture_id,))
    elif mutation == "profile":
        database.set_capture_metadata(capture_id, {"profile": "general"})
    else:
        with database._connect() as connection:
            artifact = connection.execute(
                "SELECT id FROM artifacts WHERE capture_id = ? AND filename = 'capture.csv'",
                (capture_id,),
            ).fetchone()
            assert artifact is not None
            if mutation == "missing_artifact":
                connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact["id"],))
            elif mutation == "path":
                connection.execute(
                    "UPDATE artifacts SET relative_path = 'other/capture.csv' WHERE id = ?",
                    (artifact["id"],),
                )
            elif mutation == "size":
                connection.execute(
                    "UPDATE artifacts SET size_bytes = size_bytes + 1 WHERE id = ?",
                    (artifact["id"],),
                )
            else:
                connection.execute(
                    "UPDATE artifacts SET sha256 = ? WHERE id = ?",
                    ("0" * 64, artifact["id"]),
                )

    with pytest.raises(ValueError, match=message):
        CanDecodeManager(root, database=database).run(
            CanDecodeRequest(source_run_id="source-can-001", label="reject")
        )

    assert [path.name for path in root.iterdir()] == [source_dir.name]


@pytest.mark.parametrize(
    "failure_stage",
    ["after_row", "artifact_registration", "publication", "after_completion"],
)
def test_failure_injection_removes_child_rows_and_partial_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    root = tmp_path / "captures"
    database = EvidenceDatabase(tmp_path / "evidence.sqlite3")
    database.initialize()
    source_dir, source_capture_id = _source_capture(root, database)
    source_manifest = (source_dir / "manifest.json").read_bytes()
    source_waveform = (source_dir / "capture.csv").read_bytes()
    manager = CanDecodeManager(root, database=database)

    if failure_stage == "after_row":
        monkeypatch.setattr(manager, "_write_frames", lambda *_args: (_ for _ in ()).throw(
            RuntimeError("injected after child row")
        ))
    elif failure_stage == "artifact_registration":
        monkeypatch.setattr(
            database,
            "complete_capture_with_artifacts",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("injected artifact registration")),
        )
    elif failure_stage == "publication":
        original_rename = Path.rename

        def fail_publication(path: Path, target: Path) -> Path:
            if ".partial-" in path.name:
                raise RuntimeError("injected publication")
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", fail_publication)
    else:
        original_complete = database.complete_capture_with_artifacts

        def fail_after_completion(*args: object) -> None:
            original_complete(*args)
            raise RuntimeError("injected after completion")

        monkeypatch.setattr(database, "complete_capture_with_artifacts", fail_after_completion)

    with pytest.raises(RuntimeError, match="injected"):
        manager.run(CanDecodeRequest(source_run_id="source-can-001", label="failure proof"))

    assert database.get_capture(source_capture_id) is not None
    assert database.get_capture_by_run_id("source-can-001") is not None
    assert (source_dir / "manifest.json").read_bytes() == source_manifest
    assert (source_dir / "capture.csv").read_bytes() == source_waveform
    assert not list(root.glob(".*.partial-*"))
    assert [path.name for path in root.iterdir()] == ["source-can-001"]


def test_authoritative_read_rejects_intermediate_run_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "captures"
    run_id = "directory-swap-source"
    run_dir = root / run_id
    outside = tmp_path / "outside"
    run_dir.mkdir(parents=True)
    outside.mkdir()
    expected = b'{"trusted":true}\n'
    (run_dir / "manifest.json").write_bytes(expected)
    (outside / "manifest.json").write_bytes(expected)
    record = {
        "run_id": run_id,
        "artifacts": [{
            "filename": "manifest.json",
            "relative_path": f"{run_id}/manifest.json",
            "size_bytes": len(expected),
            "sha256": hashlib.sha256(expected).hexdigest(),
        }],
    }
    original_open = os.open
    swapped = False

    def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and str(path).endswith("manifest.json"):
            swapped = True
            saved = root / f"{run_id}.saved"
            run_dir.rename(saved)
            run_dir.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(ValueError, match="replaced|symlink|cannot be opened"):
        read_authoritative_artifact(root, record, "manifest.json", max_bytes=1024)
