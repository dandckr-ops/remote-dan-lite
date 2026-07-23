from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    vin_serial TEXT,
    make TEXT,
    model TEXT,
    year INTEGER,
    engine TEXT,
    asset_tag TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_assets_vin_serial ON assets(vin_serial);
CREATE INDEX IF NOT EXISTS idx_assets_asset_tag ON assets(asset_tag);

CREATE TABLE IF NOT EXISTS diagnostic_cases (
    id INTEGER PRIMARY KEY,
    asset_id INTEGER REFERENCES assets(id) ON DELETE RESTRICT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL,
    complaint TEXT,
    customer_name TEXT,
    location TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_asset_id ON diagnostic_cases(asset_id);
CREATE INDEX IF NOT EXISTS idx_cases_status ON diagnostic_cases(status);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    case_id INTEGER REFERENCES diagnostic_cases(id) ON DELETE RESTRICT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    purpose TEXT NOT NULL,
    operator_name TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_case_id ON sessions(case_id);

CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL UNIQUE,
    captured_at TEXT NOT NULL,
    capture_type TEXT NOT NULL,
    test_type TEXT,
    label TEXT NOT NULL,
    backend TEXT NOT NULL,
    preset TEXT,
    samples INTEGER,
    sample_interval_us REAL,
    duration_ms REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_captures_session_id ON captures(session_id);
CREATE INDEX IF NOT EXISTS idx_captures_captured_at ON captures(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_captures_capture_type ON captures(capture_type);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    UNIQUE(capture_id, filename)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_capture_id ON artifacts(capture_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_sha256 ON artifacts(sha256);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    physical_channel TEXT,
    units TEXT,
    scale REAL,
    coupling TEXT,
    input_range TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(capture_id, name)
);

CREATE TABLE IF NOT EXISTS event_markers (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    capture_id INTEGER REFERENCES captures(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    offset_us REAL,
    marker_type TEXT NOT NULL DEFAULT 'operator',
    label TEXT NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_markers_session_id ON event_markers(session_id);
CREATE INDEX IF NOT EXISTS idx_markers_capture_id ON event_markers(capture_id);

CREATE TABLE IF NOT EXISTS test_results (
    id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    test_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    outcome TEXT,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    result_json TEXT NOT NULL DEFAULT '{}',
    operator_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_test_results_capture_id ON test_results(capture_id);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EvidenceDatabase:
    """SQLite repository for Remote Dan evidence metadata and lineage."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create_asset(
        self,
        *,
        asset_type: str,
        display_name: str,
        vin_serial: str | None = None,
        make: str | None = None,
        model: str | None = None,
        year: int | None = None,
        engine: str | None = None,
        asset_tag: str | None = None,
        notes: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO assets (
                    created_at, asset_type, display_name, vin_serial, make,
                    model, year, engine, asset_tag, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(), asset_type, display_name, vin_serial, make,
                    model, year, engine, asset_tag, notes,
                ),
            )
            return int(cursor.lastrowid)

    def create_case(
        self,
        *,
        title: str,
        asset_id: int | None = None,
        complaint: str | None = None,
        customer_name: str | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO diagnostic_cases (
                    asset_id, opened_at, title, complaint,
                    customer_name, location, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (asset_id, _utc_now(), title, complaint, customer_name, location, notes),
            )
            return int(cursor.lastrowid)

    def create_session(
        self,
        *,
        purpose: str,
        case_id: int | None = None,
        operator_name: str | None = None,
        notes: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sessions (case_id, started_at, purpose, operator_name, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (case_id, _utc_now(), purpose, operator_name, notes),
            )
            return int(cursor.lastrowid)

    def create_capture(
        self,
        *,
        run_id: str,
        captured_at: str,
        capture_type: str,
        label: str,
        backend: str,
        session_id: int | None = None,
        test_type: str | None = None,
        preset: str | None = None,
        samples: int | None = None,
        sample_interval_us: float | None = None,
        duration_ms: float | None = None,
        status: str = "pending",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO captures (
                    session_id, run_id, captured_at, capture_type, test_type,
                    label, backend, preset, samples, sample_interval_us,
                    duration_ms, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, run_id, captured_at, capture_type, test_type,
                    label, backend, preset, samples, sample_interval_us,
                    duration_ms, status, json.dumps(metadata or {}, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def add_artifact(
        self,
        *,
        capture_id: int,
        kind: str,
        filename: str,
        relative_path: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
    ) -> int:
        with self._connect() as connection:
            capture = connection.execute(
                "SELECT 1 FROM captures WHERE id = ?",
                (capture_id,),
            ).fetchone()
            if capture is None:
                raise ValueError("capture does not exist")
            cursor = connection.execute(
                """
                INSERT INTO artifacts (
                    capture_id, created_at, kind, filename, relative_path,
                    media_type, size_bytes, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id, _utc_now(), kind, filename, relative_path,
                    media_type, size_bytes, sha256,
                ),
            )
            return int(cursor.lastrowid)

    def set_capture_status(self, capture_id: int, status: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE captures SET status = ? WHERE id = ?",
                (status, capture_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("capture does not exist")

    def delete_capture(self, capture_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM captures WHERE id = ?", (capture_id,))

    def get_capture(self, capture_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    cp.*,
                    s.id AS session_record_id,
                    s.case_id AS session_case_id,
                    s.started_at AS session_started_at,
                    s.ended_at AS session_ended_at,
                    s.status AS session_status,
                    s.purpose AS session_purpose,
                    s.operator_name AS session_operator_name,
                    s.notes AS session_notes,
                    dc.id AS case_record_id,
                    dc.asset_id AS case_asset_id,
                    dc.opened_at AS case_opened_at,
                    dc.closed_at AS case_closed_at,
                    dc.status AS case_status,
                    dc.title AS case_title,
                    dc.complaint AS case_complaint,
                    dc.customer_name AS case_customer_name,
                    dc.location AS case_location,
                    dc.notes AS case_notes,
                    a.id AS asset_record_id,
                    a.created_at AS asset_created_at,
                    a.asset_type,
                    a.display_name AS asset_display_name,
                    a.vin_serial,
                    a.make,
                    a.model,
                    a.year,
                    a.engine,
                    a.asset_tag,
                    a.notes AS asset_notes
                FROM captures cp
                LEFT JOIN sessions s ON s.id = cp.session_id
                LEFT JOIN diagnostic_cases dc ON dc.id = s.case_id
                LEFT JOIN assets a ON a.id = dc.asset_id
                WHERE cp.id = ?
                """,
                (capture_id,),
            ).fetchone()
            if row is None:
                return None

            artifacts = connection.execute(
                """
                SELECT id, kind, filename, relative_path, media_type, size_bytes, sha256
                FROM artifacts
                WHERE capture_id = ?
                ORDER BY id
                """,
                (capture_id,),
            ).fetchall()

            record: dict[str, Any] = {
                "id": row["id"],
                "session_id": row["session_id"],
                "run_id": row["run_id"],
                "captured_at": row["captured_at"],
                "capture_type": row["capture_type"],
                "test_type": row["test_type"],
                "label": row["label"],
                "backend": row["backend"],
                "preset": row["preset"],
                "samples": row["samples"],
                "sample_interval_us": row["sample_interval_us"],
                "duration_ms": row["duration_ms"],
                "status": row["status"],
                "metadata": json.loads(row["metadata_json"]),
                "artifacts": [dict(artifact) for artifact in artifacts],
                "session": None,
                "case": None,
                "asset": None,
            }
            if row["session_record_id"] is not None:
                record["session"] = {
                    "id": row["session_record_id"],
                    "case_id": row["session_case_id"],
                    "started_at": row["session_started_at"],
                    "ended_at": row["session_ended_at"],
                    "status": row["session_status"],
                    "purpose": row["session_purpose"],
                    "operator_name": row["session_operator_name"],
                    "notes": row["session_notes"],
                }
            if row["case_record_id"] is not None:
                record["case"] = {
                    "id": row["case_record_id"],
                    "asset_id": row["case_asset_id"],
                    "opened_at": row["case_opened_at"],
                    "closed_at": row["case_closed_at"],
                    "status": row["case_status"],
                    "title": row["case_title"],
                    "complaint": row["case_complaint"],
                    "customer_name": row["case_customer_name"],
                    "location": row["case_location"],
                    "notes": row["case_notes"],
                }
            if row["asset_record_id"] is not None:
                record["asset"] = {
                    "id": row["asset_record_id"],
                    "created_at": row["asset_created_at"],
                    "asset_type": row["asset_type"],
                    "display_name": row["asset_display_name"],
                    "vin_serial": row["vin_serial"],
                    "make": row["make"],
                    "model": row["model"],
                    "year": row["year"],
                    "engine": row["engine"],
                    "asset_tag": row["asset_tag"],
                    "notes": row["asset_notes"],
                }
            return record
