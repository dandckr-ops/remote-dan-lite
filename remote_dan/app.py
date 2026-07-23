from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from remote_dan import __version__
from remote_dan.capture import CaptureBackend, CaptureManager, CaptureRequest, SimulatorBackend
from remote_dan.database import EvidenceDatabase
from remote_dan.hardware import probe_pico_hardware

STATIC_DIR = Path(__file__).with_name("static")


class CapturePayload(BaseModel):
    label: str = Field(default="field capture", min_length=1, max_length=80)
    preset: Literal["short", "medium", "long"] = "short"
    mode: Literal["auto", "hardware", "simulator"] = "auto"
    capture_type: Literal["scope", "serial", "can", "test"] = "scope"
    session_id: int | None = Field(default=None, ge=1)
    test_type: str | None = Field(default=None, min_length=1, max_length=80)


def _list_manifests(data_dir: Path) -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for path in sorted(data_dir.glob("*/manifest.json"), reverse=True):
        try:
            manifests.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return manifests


def create_app(
    data_dir: Path | str = "/var/lib/remote-dan-lite/captures",
    db_path: Path | str | None = None,
    hardware_probe: Callable[[], dict[str, object]] = probe_pico_hardware,
    hardware_backend: CaptureBackend | None = None,
) -> FastAPI:
    capture_dir = Path(data_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)
    database = EvidenceDatabase(
        Path(db_path) if db_path is not None else capture_dir.with_suffix(".sqlite3")
    )
    database.initialize()
    simulator = CaptureManager(
        capture_dir,
        backend=SimulatorBackend(),
        database=database,
    )

    app = FastAPI(
        title="Remote Dan Lite",
        version=__version__,
        description="Traceworks field capture appliance",
    )
    app.state.capture_dir = capture_dir
    app.state.database = database
    app.state.hardware_probe = hardware_probe
    app.state.simulator = simulator
    app.state.hardware_manager = (
        CaptureManager(capture_dir, backend=hardware_backend, database=database)
        if hardware_backend is not None
        else None
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/artifacts", StaticFiles(directory=capture_dir), name="artifacts")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, object]:
        hardware = hardware_probe()
        hardware_ready = bool(
            hardware.get("driver_available") and hardware.get("device_present")
        )
        return {
            "service": "remote-dan-lite",
            "version": __version__,
            "hostname": socket.gethostname(),
            "capture_ready": True,
            "default_backend": "hardware" if hardware_ready else "simulator",
            "hardware": hardware,
        }

    @app.get("/api/captures")
    def captures() -> list[dict[str, object]]:
        return _list_manifests(capture_dir)

    @app.post("/api/captures", status_code=201)
    def create_capture(payload: CapturePayload) -> dict[str, object]:
        hardware = hardware_probe()
        hardware_ready = bool(
            hardware.get("driver_available") and hardware.get("device_present")
        )
        requested_hardware = payload.mode == "hardware" or (
            payload.mode == "auto" and hardware_ready
        )
        if requested_hardware:
            if not hardware_ready:
                raise HTTPException(status_code=503, detail=str(hardware.get("reason")))
            if app.state.hardware_manager is None:
                from remote_dan.pico import PicoPS2000ABackend

                app.state.hardware_manager = CaptureManager(
                    capture_dir,
                    backend=PicoPS2000ABackend(),
                    database=database,
                )
            try:
                return app.state.hardware_manager.run(
                    CaptureRequest(
                        label=payload.label,
                        preset=payload.preset,
                        mode="hardware",
                        capture_type=payload.capture_type,
                        session_id=payload.session_id,
                        test_type=payload.test_type,
                    )
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"PS2000A acquisition failed: {exc}",
                ) from exc
        try:
            return simulator.run(
                CaptureRequest(
                    label=payload.label,
                    preset=payload.preset,
                    mode="simulator",
                    capture_type=payload.capture_type,
                    session_id=payload.session_id,
                    test_type=payload.test_type,
                )
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/captures/{run_id}")
    def capture_manifest(run_id: str) -> dict[str, object]:
        if not run_id or any(part in run_id for part in ("/", "\\", "..")):
            raise HTTPException(status_code=404, detail="capture not found")
        path = capture_dir / run_id / "manifest.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="capture not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/evidence/captures/{capture_id}")
    def evidence_capture(capture_id: int) -> dict[str, object]:
        record = database.get_capture(capture_id)
        if record is None:
            raise HTTPException(status_code=404, detail="capture not found")
        return record

    return app


def main() -> None:
    import uvicorn

    data_dir = Path(os.environ.get("REMOTE_DAN_DATA_DIR", "/var/lib/remote-dan-lite/captures"))
    db_path = Path(
        os.environ.get("REMOTE_DAN_DB_PATH", "/var/lib/remote-dan-lite/remote-dan.sqlite3")
    )
    host = os.environ.get("REMOTE_DAN_HOST", "0.0.0.0")
    port = int(os.environ.get("REMOTE_DAN_PORT", "8776"))
    uvicorn.run(
        create_app(data_dir=data_dir, db_path=db_path),
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
