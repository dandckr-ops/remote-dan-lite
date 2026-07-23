# OBD workspace activation and rollback

This runbook activates the governed OBD workspace on an existing Remote Dan Lite appliance. It assumes the branch/release has already passed simulator tests and direct **read-only** OBDLink SX acceptance.

## Safety boundary

- The OBD provider sends active SAE J1979 requests.
- Hardware Mode `$04` is disabled. The UI and API must continue to return a blocker until authenticated operator identity and the full prepare/commit audit policy are commissioned.
- Do not substitute `/dev/ttyUSB*` for a `/dev/serial/by-id/...` device identity.
- Stop any vendor application, terminal, ModemManager probe, or other process that owns the adapter before connecting.
- The new application migrates SQLite schema v1 to v2 at startup. Old code must never be started against the migrated database because the old initializer writes `user_version = 1`. Rollback therefore restores the v1 backup **before** starting the previous release.

## 1. Record the current deployment

```bash
sudo systemctl status remote-dan-lite --no-pager
sudo systemctl cat remote-dan-lite
sudo readlink -f /opt/remote-dan-lite
sudo -u remotedan /opt/remote-dan-lite/.venv/bin/python - <<'PY'
import sqlite3
p = '/var/lib/remote-dan-lite/remote-dan.sqlite3'
c = sqlite3.connect(p)
print('schema', c.execute('PRAGMA user_version').fetchone()[0])
print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0])
PY
```

Record the currently deployed commit/package and keep its source and venv available for rollback.

## 2. Create an online v1 database backup

This uses SQLite's backup API and does not copy a potentially changing WAL pair blindly.

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o root -g root -m 0700 /var/backups/remote-dan-lite
sudo python3 - "$stamp" <<'PY'
import sqlite3, sys
stamp = sys.argv[1]
src = sqlite3.connect('/var/lib/remote-dan-lite/remote-dan.sqlite3')
dst = sqlite3.connect(f'/var/backups/remote-dan-lite/remote-dan-before-obd-{stamp}.sqlite3')
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
sudo chmod 0600 /var/backups/remote-dan-lite/remote-dan-before-obd-"$stamp".sqlite3
sudo sh -c "sha256sum /var/backups/remote-dan-lite/remote-dan-before-obd-$stamp.sqlite3 > /var/backups/remote-dan-lite/remote-dan-before-obd-$stamp.sqlite3.sha256"
sudo cat /var/backups/remote-dan-lite/remote-dan-before-obd-"$stamp".sqlite3.sha256
sudo python3 - "$stamp" <<'PY'
import json, sqlite3, sys
stamp = sys.argv[1]
with sqlite3.connect('/var/lib/remote-dan-lite/remote-dan.sqlite3') as c:
    counts = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('captures', 'artifacts')}
with open(f'/var/backups/remote-dan-lite/counts-before-obd-{stamp}.json', 'w', encoding='utf-8') as output:
    json.dump(counts, output, sort_keys=True)
    output.write('\n')
PY
```

Keep this root-owned backup outside `/var/lib/remote-dan-lite`; the service can write that entire state tree and must not be able to alter its rollback material. Keep the exact backup and count-file paths with the release record.

## 3. Install adapter ownership policy

```bash
sudo install -m 0644 deploy/99-remote-dan-obdlink-sx.rules \
  /etc/udev/rules.d/99-remote-dan-obdlink-sx.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
```

Unplug/replug the SX, then verify:

```bash
udevadm info --query=property \
  --name=/dev/serial/by-id/usb-ScanTool.net_LLC_OBDLink_SX_REPLACE_WITH_UNIT_ID-if00-port0
```

Required properties are `ID_MODEL=OBDLink_SX`, the expected serial identity, and `ID_MM_DEVICE_IGNORE=1`. The service account must be in `dialout`; the provided systemd unit already includes that supplementary group.

## 4. Configure the stable path

Create `/etc/remote-dan-lite/environment` as root:

```ini
REMOTE_DAN_OBD_DEVICE=/dev/serial/by-id/usb-ScanTool.net_LLC_OBDLink_SX_REPLACE_WITH_UNIT_ID-if00-port0
REMOTE_DAN_OBD_LOCK_PATH=/var/lib/remote-dan-lite/obdlink-sx.lock
```

```bash
sudo chown root:root /etc/remote-dan-lite/environment
sudo chmod 0640 /etc/remote-dan-lite/environment
```

The repository's unit reads this file with `EnvironmentFile=-...`. Hardware connect fails closed if `REMOTE_DAN_OBD_DEVICE` is absent.

## 5. Install and start the verified release

Use the appliance's existing release mechanism to place the verified source/wheel and venv under `/opt/remote-dan-lite`. The release must include `pyserial>=3.5,<4` and the updated unit file.

```bash
sudo systemctl stop remote-dan-lite
# Install the verified release and updated unit here.
sudo systemctl daemon-reload
sudo systemctl start remote-dan-lite
sudo systemctl status remote-dan-lite --no-pager
```

Do not delete the prior release or the v1 database backup.

## 6. Post-start acceptance

Verify migration and service health:

```bash
sudo -u remotedan /opt/remote-dan-lite/.venv/bin/python - <<'PY'
import sqlite3
p = '/var/lib/remote-dan-lite/remote-dan.sqlite3'
c = sqlite3.connect(p)
print('schema', c.execute('PRAGMA user_version').fetchone()[0])
print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0])
for table in ('customers', 'obd_connections', 'obd_snapshots', 'obd_dtc_records', 'obd_live_values', 'obd_clear_events'):
    assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone(), table
print('obd tables ok')
PY
curl --fail --silent --show-error http://127.0.0.1:8776/api/obd/status
```

Compare current `captures` and `artifacts` counts to the root-owned pre-activation count file before creating any acceptance evidence. Both must match exactly.

Then use the browser:

1. Create/select a customer, vehicle, and diagnostic session.
2. Connect **Simulator** and verify Live Data, Faults, Vehicle Info, Records, and one saved JSON/manifest evidence package.
3. Disconnect the simulator.
4. With the vehicle in a known safe state, connect **OBDLink SX**.
5. Verify product identity, protocol, ECU responders, supply voltage, RPM/readiness, DTC classes, and VIN.
6. Save one read-only evidence snapshot and verify both artifact checksums.
7. Confirm the clear button remains disabled and `POST /api/obd/faults/clear/prepare` returns HTTP 403.
8. Disconnect and confirm no process retains the tty.

No hardware write/clear command is part of acceptance.

## 7. Rollback

If any acceptance gate fails:

```bash
sudo systemctl stop remote-dan-lite
```

1. Preserve the failed v2 database and logs for analysis.
2. Move the failed v2 main database plus any `-wal` and `-shm` sidecars into a timestamped root-only quarantine directory. Never place a v1 main file beside v2 sidecars.
3. Verify the root-owned backup checksum, then restore the exact pre-OBD v1 backup to a sidecar-free `/var/lib/remote-dan-lite/remote-dan.sqlite3` with owner `remotedan:remotedan` and mode `0640`.
4. Restore the previous source/venv and previous unit file.
5. Remove or move `/etc/remote-dan-lite/environment` if the old release does not know those variables.
6. Before starting old code, verify `PRAGMA user_version = 1`, `PRAGMA integrity_check = ok`, zero foreign-key violations, and exact pre-activation capture/artifact counts. Then run `systemctl daemon-reload`, start the old release, and verify UI health and artifact download.

Example database restore after preserving the failed v2 file:

```bash
sudo systemctl stop remote-dan-lite
failed=/var/backups/remote-dan-lite/failed-v2-$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o root -g root -m 0700 "$failed"
for path in /var/lib/remote-dan-lite/remote-dan.sqlite3{,-wal,-shm}; do
  sudo test ! -e "$path" || sudo mv "$path" "$failed"/
done
sudo sha256sum -c /var/backups/remote-dan-lite/remote-dan-before-obd-REPLACE.sqlite3.sha256
sudo install -o remotedan -g remotedan -m 0640 \
  /var/backups/remote-dan-lite/remote-dan-before-obd-REPLACE.sqlite3 \
  /var/lib/remote-dan-lite/remote-dan.sqlite3
sudo -u remotedan python3 - <<'PY'
import sqlite3
p = '/var/lib/remote-dan-lite/remote-dan.sqlite3'
with sqlite3.connect(p) as c:
    assert c.execute('PRAGMA user_version').fetchone()[0] == 1
    assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
print('v1 rollback database verified')
PY
sudo systemctl daemon-reload
sudo systemctl start remote-dan-lite
```

The udev ignore rule may remain; it only prevents ModemManager probing the OBDLink SX. Remove it and reload udev only if the adapter must return to a different host workflow that depends on ModemManager.
