# Remote Dan Lite

Remote Dan Lite is the Traceworks capture-plane prototype for the RootSignal field diagnostic sidecar.

The service exposes a local web console, creates bounded capture windows, and packages each run as Field Journal evidence:

- raw CSV
- JSON summary
- PNG waveform overview
- PDF report
- SHA-256 manifest

Capture metadata and diagnostic lineage live in SQLite. Large evidence files remain on the filesystem and are linked from the database by capture ID, artifact ID, relative path, media type, byte size, and SHA-256 checksum.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
REMOTE_DAN_DATA_DIR=/tmp/remote-dan-lite/captures \
REMOTE_DAN_DB_PATH=/tmp/remote-dan-lite/remote-dan.sqlite3 \
.venv/bin/remote-dan-lite
```

Open `http://127.0.0.1:8776/`.

## Capture backends

- `simulator`: deterministic VBAT and complementary CAN-H/CAN-L traces for tests and demonstrations.
- `auto`: selects hardware only when both the native PS2000A library and a Pico USB device are visible; otherwise selects the simulator.
- `hardware`: the verified production Pi 5 path using the connected PicoScope 2406B; it fails closed when the native driver or device is missing.

### Pi 5 ARM64 driver

Pico's general Linux page still describes the Raspberry Pi path as `armhf`, but the current PicoScope 7 Early Access repository publishes a native ARM64 `libps2000a` package. The prototype host uses that ARM64 package and exposes the library through the system dynamic-linker cache. Hardware mode still fails closed unless both the driver and a Pico USB device are detected; simulated data is never presented as hardware evidence.

## Evidence database

SQLite schema version 1 records:

- assets/machines, including type, display name, VIN/serial, make, model, year, engine, and asset tag
- diagnostic cases, including complaint, customer/site, location, status, and notes
- diagnostic sessions, including purpose, operator, status, and timestamps
- captures, including scope/serial/CAN/test type, backend, preset, timing, sample count, and test type
- artifacts, including their owning capture ID, artifact ID, relative path, MIME type, size, and checksum
- channel configuration, event markers, and structured test-result records for later dashboard work

The filesystem remains authoritative for artifact bytes. SQLite owns metadata, lineage, and lookup. `GET /api/evidence/captures/{capture_id}` returns the capture record with its asset/case/session lineage and artifact IDs.

## Deployment

The example unit is `deploy/remote-dan-lite.service`. It runs as the dedicated `remotedan` account, writes capture packages under `/var/lib/remote-dan-lite/captures`, and keeps its SQLite index at `/var/lib/remote-dan-lite/remote-dan.sqlite3`.

`deploy/rem-01.traefik.yml` is a sanitized private-ingress example. Before using it, replace the example hostname, backend address, and allowlist with values appropriate to the deployment. Keep port `8776` on a private service network; do not expose the raw application listener directly to the public Internet.

Recommended network roles:

- wired Ethernet: primary capture and artifact-transfer path
- saved Wi-Fi: fallback underlay
- a private overlay such as Tailscale: stable management path
- authenticated reverse proxy: browser publication and TLS termination
