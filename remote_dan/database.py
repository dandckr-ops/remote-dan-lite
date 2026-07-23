from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


SCHEMA_VERSION = 2

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


SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    company TEXT,
    phone TEXT,
    email TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cases_customer_id ON diagnostic_cases(customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicle_vin_unique
    ON assets(upper(trim(vin_serial)))
    WHERE asset_type = 'vehicle' AND vin_serial IS NOT NULL AND length(trim(vin_serial)) > 0;

CREATE TABLE IF NOT EXISTS obd_connections (
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id) ON DELETE RESTRICT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    adapter_identity TEXT NOT NULL,
    stable_path TEXT,
    protocol TEXT NOT NULL,
    responder_ids_json TEXT NOT NULL DEFAULT '[]',
    voltage REAL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_obd_connections_session_id
    ON obd_connections(session_id);

CREATE TABLE IF NOT EXISTS obd_snapshots (
    id INTEGER PRIMARY KEY,
    connection_id INTEGER REFERENCES obd_connections(id) ON DELETE RESTRICT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE RESTRICT,
    capture_id INTEGER REFERENCES captures(id) ON DELETE RESTRICT,
    captured_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    protocol TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'complete',
    raw_response_json TEXT NOT NULL DEFAULT '{}',
    parsed_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_obd_snapshots_session_id
    ON obd_snapshots(session_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_obd_snapshots_capture_id
    ON obd_snapshots(capture_id);

CREATE TABLE IF NOT EXISTS obd_dtc_records (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES obd_snapshots(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('stored', 'pending', 'permanent')),
    ecu TEXT NOT NULL,
    code TEXT NOT NULL,
    description TEXT,
    UNIQUE(snapshot_id, state, ecu, code)
);

CREATE INDEX IF NOT EXISTS idx_obd_dtc_snapshot_id
    ON obd_dtc_records(snapshot_id);

CREATE TABLE IF NOT EXISTS obd_live_values (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES obd_snapshots(id) ON DELETE CASCADE,
    pid TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL,
    unit TEXT,
    ecu TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    fresh INTEGER NOT NULL CHECK(fresh IN (0, 1)),
    raw_hex TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_obd_live_snapshot_id
    ON obd_live_values(snapshot_id);

CREATE TABLE IF NOT EXISTS obd_clear_events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
    connection_id INTEGER NOT NULL REFERENCES obd_connections(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    confirmation_text TEXT NOT NULL,
    command TEXT NOT NULL CHECK(command = '04'),
    before_snapshot_id INTEGER REFERENCES obd_snapshots(id) ON DELETE RESTRICT,
    after_snapshot_id INTEGER REFERENCES obd_snapshots(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL,
    response_json TEXT NOT NULL DEFAULT '{}',
    ambiguous INTEGER NOT NULL CHECK(ambiguous IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_obd_clear_session_id
    ON obd_clear_events(session_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_obd_clear_events_no_update
BEFORE UPDATE ON obd_clear_events
BEGIN
    SELECT RAISE(ABORT, 'obd_clear_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_obd_clear_events_no_delete
BEFORE DELETE ON obd_clear_events
BEGIN
    SELECT RAISE(ABORT, 'obd_clear_events are append-only');
END;
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
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            connection.executescript(SCHEMA_SQL)
            case_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(diagnostic_cases)")
            }
            if "customer_id" not in case_columns:
                connection.execute(
                    "ALTER TABLE diagnostic_cases ADD COLUMN customer_id INTEGER "
                    "REFERENCES customers(id) ON DELETE RESTRICT"
                )
            connection.executescript(SCHEMA_V2_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create_customer(
        self,
        *,
        name: str,
        company: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        notes: str | None = None,
    ) -> int:
        customer_name = name.strip()
        if not customer_name:
            raise ValueError("customer name is required")
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO customers (
                    created_at, updated_at, name, company, phone, email, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    customer_name,
                    company.strip() if company else None,
                    phone.strip() if phone else None,
                    email.strip() if email else None,
                    notes.strip() if notes else None,
                ),
            )
            return int(cursor.lastrowid)

    def list_customers(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, updated_at, name, company, phone, email, notes
                FROM customers
                ORDER BY name COLLATE NOCASE, id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def create_vehicle(
        self,
        *,
        display_name: str,
        vin: str | None = None,
        make: str | None = None,
        model: str | None = None,
        year: int | None = None,
        engine: str | None = None,
        asset_tag: str | None = None,
        notes: str | None = None,
    ) -> int:
        name = display_name.strip()
        if not name:
            raise ValueError("vehicle display name is required")
        normalized_vin = vin.strip().upper() if vin and vin.strip() else None
        with self._connect() as connection:
            if normalized_vin is not None:
                duplicate = connection.execute(
                    """
                    SELECT id FROM assets
                    WHERE asset_type = 'vehicle' AND upper(trim(vin_serial)) = ?
                    """,
                    (normalized_vin,),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("VIN already exists")
            cursor = connection.execute(
                """
                INSERT INTO assets (
                    created_at, asset_type, display_name, vin_serial, make,
                    model, year, engine, asset_tag, notes
                ) VALUES (?, 'vehicle', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    name,
                    normalized_vin,
                    make.strip() if make else None,
                    model.strip() if model else None,
                    year,
                    engine.strip() if engine else None,
                    asset_tag.strip() if asset_tag else None,
                    notes.strip() if notes else None,
                ),
            )
            return int(cursor.lastrowid)

    def list_vehicles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id, created_at, display_name, vin_serial AS vin,
                    make, model, year, engine, asset_tag, notes
                FROM assets
                WHERE asset_type = 'vehicle'
                ORDER BY display_name COLLATE NOCASE, id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def create_diagnostic_session(
        self,
        *,
        customer_id: int,
        vehicle_id: int,
        title: str,
        purpose: str,
        complaint: str | None = None,
        operator_name: str | None = None,
        location: str | None = None,
        notes: str | None = None,
    ) -> dict[str, int]:
        case_title = title.strip()
        session_purpose = purpose.strip()
        if not case_title or not session_purpose:
            raise ValueError("title and purpose are required")
        with self._connect() as connection:
            customer = connection.execute(
                "SELECT id, name FROM customers WHERE id = ?",
                (customer_id,),
            ).fetchone()
            if customer is None:
                raise ValueError("customer does not exist")
            vehicle = connection.execute(
                "SELECT id FROM assets WHERE id = ? AND asset_type = 'vehicle'",
                (vehicle_id,),
            ).fetchone()
            if vehicle is None:
                raise ValueError("vehicle does not exist")
            case_cursor = connection.execute(
                """
                INSERT INTO diagnostic_cases (
                    asset_id, customer_id, opened_at, title, complaint,
                    customer_name, location, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vehicle_id,
                    customer_id,
                    _utc_now(),
                    case_title,
                    complaint.strip() if complaint else None,
                    customer["name"],
                    location.strip() if location else None,
                    notes.strip() if notes else None,
                ),
            )
            case_id = int(case_cursor.lastrowid)
            session_cursor = connection.execute(
                """
                INSERT INTO sessions (case_id, started_at, purpose, operator_name, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    _utc_now(),
                    session_purpose,
                    operator_name.strip() if operator_name else None,
                    notes.strip() if notes else None,
                ),
            )
            return {
                "customer_id": customer_id,
                "vehicle_id": vehicle_id,
                "case_id": case_id,
                "session_id": int(session_cursor.lastrowid),
            }

    def list_diagnostic_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.id AS session_id, s.started_at, s.ended_at,
                    s.status AS session_status, s.purpose, s.operator_name,
                    s.notes AS session_notes, dc.id AS case_id,
                    dc.title AS case_title, dc.complaint, dc.location,
                    c.id AS customer_id, c.name AS customer_name,
                    c.company AS customer_company, c.phone AS customer_phone,
                    c.email AS customer_email, a.id AS vehicle_id,
                    a.display_name AS vehicle_display_name, a.vin_serial,
                    a.make, a.model, a.year, a.engine, a.asset_tag
                FROM sessions s
                JOIN diagnostic_cases dc ON dc.id = s.case_id
                LEFT JOIN customers c ON c.id = dc.customer_id
                LEFT JOIN assets a ON a.id = dc.asset_id
                ORDER BY s.started_at DESC, s.id DESC
                """
            ).fetchall()
            return [
                {
                    "session_id": row["session_id"],
                    "case_id": row["case_id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "status": row["session_status"],
                    "purpose": row["purpose"],
                    "operator_name": row["operator_name"],
                    "notes": row["session_notes"],
                    "case": {
                        "title": row["case_title"],
                        "complaint": row["complaint"],
                        "location": row["location"],
                    },
                    "customer": (
                        {
                            "id": row["customer_id"],
                            "name": row["customer_name"],
                            "company": row["customer_company"],
                            "phone": row["customer_phone"],
                            "email": row["customer_email"],
                        }
                        if row["customer_id"] is not None else None
                    ),
                    "vehicle": (
                        {
                            "id": row["vehicle_id"],
                            "display_name": row["vehicle_display_name"],
                            "vin": row["vin_serial"],
                            "make": row["make"],
                            "model": row["model"],
                            "year": row["year"],
                            "engine": row["engine"],
                            "asset_tag": row["asset_tag"],
                        }
                        if row["vehicle_id"] is not None else None
                    ),
                }
                for row in rows
            ]

    def create_obd_connection(
        self,
        *,
        session_id: int | None,
        provider: str,
        adapter_identity: str,
        stable_path: str | None,
        protocol: str,
        responder_ids: list[str],
        voltage: float | None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO obd_connections (
                    session_id, started_at, status, provider, adapter_identity,
                    stable_path, protocol, responder_ids_json, voltage
                ) VALUES (?, ?, 'connected', ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _utc_now(),
                    provider,
                    adapter_identity,
                    stable_path,
                    protocol,
                    json.dumps(responder_ids, sort_keys=True),
                    voltage,
                ),
            )
            return int(cursor.lastrowid)

    def close_obd_connection(
        self,
        connection_id: int,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE obd_connections
                SET ended_at = ?, status = ?, error = ?
                WHERE id = ? AND ended_at IS NULL
                """,
                (_utc_now(), status, error, connection_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("active OBD connection does not exist")

    def create_obd_snapshot(
        self,
        *,
        connection_id: int | None,
        session_id: int | None,
        capture_id: int | None,
        kind: str,
        provider: str,
        protocol: str,
        raw_responses: dict[str, Any],
        parsed: dict[str, Any],
        status: str = "complete",
        error: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO obd_snapshots (
                    connection_id, session_id, capture_id, captured_at, kind,
                    provider, protocol, status, raw_response_json, parsed_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    connection_id,
                    session_id,
                    capture_id,
                    _utc_now(),
                    kind,
                    provider,
                    protocol,
                    status,
                    json.dumps(raw_responses, sort_keys=True),
                    json.dumps(parsed, sort_keys=True),
                    error,
                ),
            )
            return int(cursor.lastrowid)

    def add_obd_dtcs(
        self,
        snapshot_id: int,
        dtcs: list[dict[str, Any]],
    ) -> None:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM obd_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone() is None:
                raise ValueError("OBD snapshot does not exist")
            connection.executemany(
                """
                INSERT INTO obd_dtc_records (
                    snapshot_id, state, ecu, code, description
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item["state"],
                        item["ecu"],
                        item["code"],
                        item.get("description"),
                    )
                    for item in dtcs
                ],
            )

    def add_obd_live_values(
        self,
        snapshot_id: int,
        values: list[dict[str, Any]],
    ) -> None:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM obd_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone() is None:
                raise ValueError("OBD snapshot does not exist")
            connection.executemany(
                """
                INSERT INTO obd_live_values (
                    snapshot_id, pid, name, value, unit, ecu, sampled_at,
                    fresh, raw_hex, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item["pid"],
                        item["name"],
                        item.get("value"),
                        item.get("unit"),
                        item["ecu"],
                        item["sampled_at"],
                        int(bool(item["fresh"])),
                        item.get("raw_hex"),
                        item.get("error"),
                    )
                    for item in values
                ],
            )

    def get_obd_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM obd_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                return None
            dtcs = connection.execute(
                """
                SELECT id, state, ecu, code, description
                FROM obd_dtc_records
                WHERE snapshot_id = ?
                ORDER BY id
                """,
                (snapshot_id,),
            ).fetchall()
            values = connection.execute(
                """
                SELECT id, pid, name, value, unit, ecu, sampled_at,
                       fresh, raw_hex, error
                FROM obd_live_values
                WHERE snapshot_id = ?
                ORDER BY id
                """,
                (snapshot_id,),
            ).fetchall()
            result = dict(row)
            result["raw_responses"] = json.loads(result.pop("raw_response_json"))
            result["parsed"] = json.loads(result.pop("parsed_json"))
            result["dtcs"] = [dict(item) for item in dtcs]
            result["live_values"] = [
                {**dict(item), "fresh": bool(item["fresh"])} for item in values
            ]
            return result

    def list_obd_snapshots(self, session_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, connection_id, session_id, capture_id, captured_at,
                       kind, provider, protocol, status, error
                FROM obd_snapshots
                WHERE session_id = ?
                ORDER BY captured_at DESC, id DESC
                """,
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_obd_clear_event(
        self,
        *,
        session_id: int,
        connection_id: int,
        actor: str,
        confirmation_text: str,
        command: str,
        before_snapshot_id: int | None,
        after_snapshot_id: int | None,
        outcome: str,
        response: dict[str, Any],
        ambiguous: bool,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO obd_clear_events (
                    session_id, connection_id, created_at, actor,
                    confirmation_text, command, before_snapshot_id,
                    after_snapshot_id, outcome, response_json, ambiguous
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    connection_id,
                    _utc_now(),
                    actor,
                    confirmation_text,
                    command,
                    before_snapshot_id,
                    after_snapshot_id,
                    outcome,
                    json.dumps(response, sort_keys=True),
                    int(ambiguous),
                ),
            )
            return int(cursor.lastrowid)

    def get_obd_clear_event(self, event_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM obd_clear_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["response"] = json.loads(result.pop("response_json"))
            result["ambiguous"] = bool(result["ambiguous"])
            return result

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

    def complete_capture_with_artifacts(
        self,
        capture_id: int,
        artifacts: list[dict[str, Any]],
    ) -> None:
        """Register the complete artifact set and publish the capture atomically."""
        with self._connect() as connection:
            capture = connection.execute(
                "SELECT status FROM captures WHERE id = ?",
                (capture_id,),
            ).fetchone()
            if capture is None:
                raise ValueError("capture does not exist")
            if capture["status"] != "pending":
                raise ValueError("capture is not pending")
            for artifact in artifacts:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        capture_id, created_at, kind, filename, relative_path,
                        media_type, size_bytes, sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        capture_id,
                        _utc_now(),
                        artifact["kind"],
                        artifact["filename"],
                        artifact["relative_path"],
                        artifact["media_type"],
                        artifact["size_bytes"],
                        artifact["sha256"],
                    ),
                )
            cursor = connection.execute(
                "UPDATE captures SET status = 'complete' WHERE id = ? AND status = 'pending'",
                (capture_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("capture completion state changed")

    def list_complete_captures(
        self,
        *,
        capture_types: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        parameters: list[Any] = []
        type_clause = ""
        if capture_types:
            placeholders = ", ".join("?" for _ in capture_types)
            type_clause = f" AND capture_type IN ({placeholders})"
            parameters.extend(capture_types)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id
                FROM captures
                WHERE status = 'complete'{type_clause}
                ORDER BY captured_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            record
            for row in rows
            if (record := self.get_capture(int(row["id"]))) is not None
        ]

    def set_capture_status(self, capture_id: int, status: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE captures SET status = ? WHERE id = ?",
                (status, capture_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("capture does not exist")

    def set_capture_metadata(self, capture_id: int, metadata: dict[str, Any]) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE captures SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, sort_keys=True), capture_id),
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

    def get_capture_by_run_id(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM captures WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self.get_capture(int(row["id"])) if row is not None else None
